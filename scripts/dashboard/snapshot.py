"""Read-only snapshots of every bot directory under ``data/live``.

Everything here reads files the bots already write; nothing is created,
modified or deleted. All file access goes through ``fileio`` so a read
can never make a bot's ``os.replace``/``os.remove`` fail on Windows.

Liveness reuses the daily monitor's own rules (``classify_heartbeat``,
``pid_alive``, ``RECENT_CRASH_DAYS``) so the page and the monitor digest
never disagree about whether a bot is alive or hung.

Times: trade and bar timestamps are TPE wall-clock strings. Column 0 of
decisions.csv is the MACHINE clock, so only ``bar_dt`` (column 1) is used.
Chart times are TPE wall-clock encoded as if UTC, which makes the chart
axis show TPE regardless of the viewer's own timezone.
"""

from __future__ import annotations

import csv
import io
import os
import re
from datetime import datetime, timedelta, timezone

from scripts.monitor.check_bots import (
    HEARTBEAT_FILENAME, RECENT_CRASH_DAYS, classify_heartbeat,
)
from scripts.monitor.check_logs import PATTERNS
from scripts.monitor.common import (
    TZ_TPE, discover_bot_dirs, pid_alive, redact,
)
from scripts.dashboard import fileio, real_pnl
from src.live.headless import stop_requested
from src.utils.log_redact import scrub_ids

# A stopped bot with no file activity in this window is retired and is
# hidden unless the viewer asks for it.
ACTIVE_DAYS = 7.0
MAX_TRADES = 200
MAX_DECISIONS = 60
MAX_LOG_LINES = 150

_BARS_RE = re.compile(r"^bars_1m_(\d{8})\.csv$")
_DEBUG_RE = re.compile(r"^debug_(\d{8})\.log$")
# Discord channel/user IDs (snowflakes) and TW national-ID-shaped values
# appear in debug logs; neither may leave the box through the page. Digit
# lookarounds, not \b: "channel_<id>" has no word boundary before the id.
_RE_SNOWFLAKE = re.compile(r"(?<!\d)\d{17,20}(?!\d)")
_RE_TW_ID = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][12]\d{8}(?!\d)")


def redact_line(text: str) -> str:
    """Monitor redaction + Capital account/order ids + snowflakes + TW ids."""
    out = scrub_ids(redact(text))
    out = _RE_SNOWFLAKE.sub("<id>", out)
    return _RE_TW_ID.sub("<id>", out)


def log_severity(line: str) -> str:
    """P1/P2/P3 when the line carries a known check_logs signature, else ""."""
    low = line.lower()
    for sev in ("P1", "P2", "P3"):
        if any(p.lower() in low for p in PATTERNS[sev]):
            return sev
    return ""


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):   # Infinity / NaN in JSON
        return default


def parse_tpe(text) -> datetime | None:
    """``YYYY-MM-DD HH:MM[:SS]`` or ``YYYY/MM/DD HH:MM`` as TPE wall time."""
    if not isinstance(text, str):
        return None
    head = text.strip().replace("/", "-").replace("T", " ")[:16]
    try:
        return datetime.strptime(head, "%Y-%m-%d %H:%M").replace(tzinfo=TZ_TPE)
    except ValueError:
        return None


def chart_time(dt: datetime) -> int:
    """TPE wall-clock seconds encoded as UTC (see module docstring)."""
    wall = dt.astimezone(TZ_TPE).replace(tzinfo=timezone.utc)
    return int(wall.timestamp())


def read_lock(bot_dir: str) -> tuple[bool, int | None]:
    """``(lock_exists, pid_or_None)``. Shared open: never blocks a stop."""
    path = os.path.join(bot_dir, ".lock")
    if not os.path.isfile(path):
        return False, None
    text = fileio.read_text(path)
    if text is None:
        return True, None
    try:
        return True, int(text.strip())
    except ValueError:
        return True, None


def _newest(bot_dir: str, pattern: re.Pattern) -> list[tuple[str, str]]:
    """``[(yyyymmdd, path)]`` newest first."""
    out = []
    try:
        names = os.listdir(bot_dir)
    except OSError:
        return out
    for name in names:
        m = pattern.match(name)
        if m:
            out.append((m.group(1), os.path.join(bot_dir, name)))
    out.sort(reverse=True)
    return out


def _mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def last_activity_ts(bot_dir: str) -> float | None:
    stamps = [_mtime(os.path.join(bot_dir, n))
              for n in (".lock", "session.json", "decisions.csv",
                        HEARTBEAT_FILENAME, "regime_state.json")]
    logs = _newest(bot_dir, _DEBUG_RE)
    if logs:
        stamps.append(_mtime(logs[0][1]))
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def bot_state(now: datetime, bot_dir: str, *, pid_alive_fn=pid_alive,
              activity_ts: float | None = None) -> dict:
    """State code plus detail. Codes:

    RUNNING   lock PID alive, heartbeat fresh
    LAGGING   lock PID alive, heartbeat missed a few ticks (monitor STALE)
    HUNG      lock PID alive, main loop stopped pumping
    NO_HEARTBEAT  lock PID alive, heartbeat missing/corrupt/foreign PID.
              Liveness is unproven (the monitor's UNKNOWN), so the page
              flags it rather than calling it running.
    CRASHED   lock left behind by a dead PID, bot active recently or with
              no activity evidence at all (monitor grades both P1)
    STOPPED   no lock (clean stop), or a dead-PID lock on a retired bot
    UNKNOWN   lock file unreadable
    """
    has_lock, pid = read_lock(bot_dir)
    hb = fileio.read_json(os.path.join(bot_dir, HEARTBEAT_FILENAME))
    out = {"state": "STOPPED", "pid": pid, "heartbeat_age_s": None,
           "inflight_call": "", "inflight_age_s": None}
    if not has_lock:
        return out
    if pid is None:
        out["state"] = "UNKNOWN"
        return out
    if not pid_alive_fn(pid):
        recent = (activity_ts is None or
                  now.timestamp() - activity_ts <= RECENT_CRASH_DAYS * 86400)
        out["state"] = "CRASHED" if recent else "STOPPED"
        return out
    hb_state, detail = classify_heartbeat(now, hb, pid_alive_fn, pid,
                                          has_lock=True)
    out["heartbeat_age_s"] = detail.get("age_s")
    out["inflight_call"] = detail.get("inflight_name") or ""
    out["inflight_age_s"] = detail.get("inflight_age_s")
    out["state"] = {"ALIVE": "RUNNING", "STALE": "LAGGING",
                    "HUNG": "HUNG"}.get(hb_state, "NO_HEARTBEAT")
    return out


def _strategy_label(sess: dict) -> str:
    if sess.get("regime_mode"):
        return (f"Regime L={sess.get('long_strategy', '')} "
                f"S={sess.get('short_strategy', '')}")
    return str(sess.get("strategy") or "")


def last_bar(bot_dir: str) -> tuple[datetime | None, int | None]:
    """Newest 1-min bar's ``(open time, close)`` from the newest bars file."""
    for _day, path in _newest(bot_dir, _BARS_RE)[:2]:
        for line in reversed(fileio.tail_lines(path, 5, max_bytes=4096)):
            parts = line.split(",")
            if len(parts) < 5:
                continue
            dt = parse_tpe(parts[0])
            close = _to_int(parts[4], 0)
            if dt and close:
                return dt, close
    return None, None


def read_decisions(bot_dir: str, n: int) -> list[dict]:
    path = os.path.join(bot_dir, "decisions.csv")
    lines = fileio.tail_lines(path, n + 1)
    rows = []
    for row in csv.reader(io.StringIO("\n".join(lines))):
        if len(row) < 8 or row[0] == "datetime":
            continue
        rows.append({"bar_dt": row[1], "strategy": row[2], "action": row[3],
                     "side": row[4], "tag": row[5], "price": _to_int(row[6]),
                     "reason": redact_line(",".join(row[7:]))})
    return rows[-n:]


def _position(broker: dict, point_value: int, last_close: int | None) -> dict | None:
    size = _to_int(broker.get("position_size"))
    if size <= 0:
        return None
    side = str(broker.get("position_side") or "")
    sim = _to_int(broker.get("entry_price"))
    real = _to_int(broker.get("real_entry_price"))
    entry = real or sim
    unrealized = None
    if last_close and entry:
        sign = 1 if side.upper() == "LONG" else -1
        unrealized = (last_close - entry) * sign * size * point_value
    return {"side": side, "size": size, "entry_price": sim,
            "real_entry_price": real, "entry_dt": broker.get("_entry_dt") or "",
            "unrealized": unrealized}


_stat_cache: dict[str, tuple[int, int, object]] = {}


def _read_if_changed(path: str, load, valid):
    """``load(path)``, re-run only when the file's size or mtime changed.

    Any open handle, even a share-delete one, makes the bot's next
    ``os.replace`` onto that file fail: session.json then falls back to a
    non-atomic remove+rename, and the regime store writers have no
    fallback at all (a failed save loses a classification, a history row
    or the executed flag). These files change on trade events or once a
    night, so polling their stat keeps content opens rare. Returns None
    when the file is missing; mid-write garbage keeps the last good value.
    """
    try:
        st = os.stat(path)
    except OSError:
        _stat_cache.pop(path, None)
        return None
    hit = _stat_cache.get(path)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    data = load(path)
    if not valid(data):
        return hit[2] if hit else None
    _stat_cache[path] = (st.st_mtime_ns, st.st_size, data)
    return data


def read_session(bot_dir: str) -> dict:
    """session.json, re-read only when it changed (see ``_read_if_changed``)."""
    data = _read_if_changed(os.path.join(bot_dir, "session.json"),
                            fileio.read_json, lambda d: isinstance(d, dict))
    return data if isinstance(data, dict) else {}


def trading_day_start(now: datetime) -> datetime:
    """Start of the TAIFEX trading day containing ``now``: the most recent
    night-session open (15:00 TPE of the previous business day). Falls
    back to TPE midnight when the session calendar is unavailable."""
    try:
        from src.regime.switch_logic import latest_night_session
        night = latest_night_session(now)
        if night is not None:
            return night.open_dt.astimezone(TZ_TPE)
    except Exception:  # noqa: BLE001 — a broken calendar must not break the page
        pass
    return now.astimezone(TZ_TPE).replace(hour=0, minute=0, second=0, microsecond=0)


def bot_summary(now: datetime, bot_dir: str, *, pid_alive_fn=pid_alive) -> dict:
    name = os.path.basename(os.path.normpath(bot_dir))
    activity = last_activity_ts(bot_dir)
    info = {"id": name, "symbol": name.partition("_")[0],
            "bot_name": name.partition("_")[2] or name,
            "last_activity_age_s": (None if activity is None
                                    else round(now.timestamp() - activity)),
            **bot_state(now, bot_dir, pid_alive_fn=pid_alive_fn,
                        activity_ts=activity)}
    sess = read_session(bot_dir)
    broker = sess.get("broker") if isinstance(sess.get("broker"), dict) else {}
    trades = broker.get("trades") if isinstance(broker.get("trades"), list) else []
    trades = [t for t in trades if isinstance(t, dict)]
    point_value = _to_int(sess.get("point_value") or broker.get("point_value"), 1)
    day_start = trading_day_start(now)
    todays = [t for t in trades
              if (exit_dt := parse_tpe(t.get("exit_dt"))) is not None
              and exit_dt >= day_start]
    bar_dt, last_close = last_bar(bot_dir)
    wins = sum(1 for t in trades if _to_int(t.get("pnl")) > 0)
    info.update({
        "strategy": _strategy_label(sess),
        "trading_mode": str(sess.get("trading_mode") or ""),
        "interval_s": _to_int(sess.get("target_interval")),
        "point_value": point_value,
        "trades": len(trades),
        "win_rate": round(wins / len(trades), 3) if trades else None,
        "pnl_total": _to_int(broker.get("_cumulative_pnl")),
        "pnl_today": sum(_to_int(t.get("pnl")) for t in todays),
        "trades_today": len(todays),
        "trading_day_start": day_start.strftime("%Y-%m-%d %H:%M"),
        "position": _position(broker, point_value, last_close),
        "last_price": last_close,
        "last_bar_dt": bar_dt.strftime("%Y-%m-%d %H:%M") if bar_dt else "",
        "saved_at": str(sess.get("saved_at") or ""),
        "real": real_pnl.summarize(trades, point_value, day_start),
    })
    decisions = read_decisions(bot_dir, 1)
    info["last_decision"] = decisions[-1] if decisions else None
    info["regime"] = (regime_summary(read_regime_state(bot_dir), sess)
                      if _is_regime_bot(sess) else None)
    info["pending"] = pending(bot_dir)
    running = info["state"] in ("RUNNING", "LAGGING", "HUNG", "NO_HEARTBEAT",
                                "CRASHED", "UNKNOWN")
    info["active"] = running or (
        activity is not None and now.timestamp() - activity <= ACTIVE_DAYS * 86400)
    return info


# The file LiveRunner._check_mode_override consumes (src/live/live_runner.py).
MODE_OVERRIDE_FILENAME = "mode_override.json"


def mode_override_path(bot_dir: str) -> str:
    return os.path.join(bot_dir, MODE_OVERRIDE_FILENAME)


def pending(bot_dir: str) -> dict:
    """Control files written but not yet consumed by the bot: the headless
    STOP file, and a queued mode_override.json (applied at the next bar)."""
    data = fileio.read_json(mode_override_path(bot_dir))
    mode = data.get("trading_mode") if isinstance(data, dict) else None
    return {"stop": stop_requested(bot_dir),
            "mode": mode if isinstance(mode, str) else None}


def _broken_bot(bot_dir: str, exc: Exception) -> dict:
    """Stand-in row so one malformed directory cannot blank the fleet."""
    name = os.path.basename(os.path.normpath(bot_dir))
    return {"id": name, "symbol": name.partition("_")[0],
            "bot_name": name.partition("_")[2] or name, "state": "UNKNOWN",
            "error": redact_line(f"{type(exc).__name__}: {exc}"), "pid": None,
            "heartbeat_age_s": None, "inflight_call": "", "inflight_age_s": None,
            "last_activity_age_s": None, "strategy": "", "trading_mode": "",
            "interval_s": 0, "point_value": 1, "trades": 0, "win_rate": None,
            "pnl_total": 0, "pnl_today": 0, "trades_today": 0, "position": None,
            "last_price": None, "last_bar_dt": "", "saved_at": "", "real": None,
            "last_decision": None, "regime": None,
            "pending": {"stop": False, "mode": None}, "active": True}


def market_state(now: datetime) -> dict:
    try:
        from src.regime.switch_logic import current_session
        sess = current_session(now)
    except Exception:  # noqa: BLE001 — a broken calendar must not break the page
        return {"session": None, "closes_at": "", "degraded": True}
    if sess is None:
        return {"session": None, "closes_at": "", "degraded": False}
    return {"session": sess.slot,
            "closes_at": sess.close_dt.astimezone(TZ_TPE).strftime("%H:%M"),
            "degraded": False}


def fleet(now: datetime, base_dir: str, *, host: str = "",
          pid_alive_fn=pid_alive) -> dict:
    bots = []
    for d in discover_bot_dirs(base_dir):
        try:
            bots.append(bot_summary(now, d, pid_alive_fn=pid_alive_fn))
        except Exception as exc:  # noqa: BLE001 — isolate one bad bot dir
            bots.append(_broken_bot(d, exc))
    return {"host": host, "now": now.astimezone(TZ_TPE).strftime("%Y-%m-%d %H:%M:%S"),
            "market": market_state(now), "bots": bots}


def find_bot_dir(base_dir: str, bot_id: str) -> str | None:
    """Exact basename match among discovered bots — never a joined user path."""
    for d in discover_bot_dirs(base_dir):
        if os.path.basename(os.path.normpath(d)) == bot_id:
            return d
    return None


def read_bars(path: str) -> list[dict]:
    """1-min candles, sorted and de-duplicated (last wins).

    One out-of-order time silently blanks a lightweight-charts pane
    (issue #97), so the order is enforced here, not trusted.
    """
    text = fileio.read_text(path)
    if not text:
        return []
    by_time: dict[int, dict] = {}
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) < 5:
            continue
        dt = parse_tpe(parts[0])
        if dt is None:
            continue
        try:
            o, h, lo, c = (int(float(x)) for x in parts[1:5])
        except (ValueError, OverflowError):
            continue
        t = chart_time(dt)
        by_time[t] = {"time": t, "open": o, "high": h, "low": lo, "close": c,
                      "volume": _to_int(parts[5]) if len(parts) > 5 else 0}
    return [by_time[t] for t in sorted(by_time)]


def _trade_row(t: dict, point_value: int | None = None) -> dict:
    """One trade for the page. With ``point_value`` it also carries the
    real-order classification (``real_pnl.classify``)."""
    extra = {}
    if point_value is not None:
        c = real_pnl.classify(t, point_value)
        extra = {"real_status": c["status"], "real_pnl": c["real_pnl"],
                 "real_reason": c["reason"]}
    return {**extra, "side": str(t.get("side") or ""), "qty": _to_int(t.get("qty"), 1),
            "entry_dt": str(t.get("entry_dt") or ""),
            "entry_price": _to_int(t.get("entry_price")),
            "real_entry_price": _to_int(t.get("real_entry_price")),
            "exit_dt": str(t.get("exit_dt") or ""),
            "exit_price": _to_int(t.get("exit_price")),
            "real_exit_price": _to_int(t.get("real_exit_price")),
            "pnl": _to_int(t.get("pnl")), "exit_tag": str(t.get("exit_tag") or ""),
            "source": str(t.get("source") or "")}


def trade_markers(trades: list[dict], t0: int, t1: int) -> list[dict]:
    """Entry/exit markers that fall inside the charted ``[t0, t1]`` range."""
    marks = []
    for t in trades:
        long_ = t["side"].upper() == "LONG"
        for key, kind in (("entry_dt", "entry"), ("exit_dt", "exit")):
            dt = parse_tpe(t[key])
            if dt is None:
                continue
            ts = chart_time(dt)
            if t0 <= ts <= t1:
                marks.append({"time": ts - ts % 60, "kind": kind, "long": long_,
                              "price": t["entry_price" if kind == "entry" else "exit_price"],
                              "pnl": t["pnl"] if kind == "exit" else None})
    marks.sort(key=lambda m: m["time"])
    return marks


def equity_points(trades: list[dict]) -> list[dict]:
    """Cumulative realized P&L at each exit, one point per chart time."""
    total = 0
    by_time: dict[int, int] = {}
    for t in trades:
        total += t["pnl"]
        dt = parse_tpe(t["exit_dt"])
        if dt is not None:
            by_time[chart_time(dt)] = total
    return [{"time": k, "value": by_time[k]} for k in sorted(by_time)]


BARS_DAYS_DEFAULT = 3
BARS_DAYS_MAX = 10


def bars_window(bot_dir: str, before: str | None = None,
                days: int = BARS_DAYS_DEFAULT, after: str | None = None) -> dict:
    """1-min candles from up to ``days`` daily files, stitched into one series.

    ``before`` (YYYYMMDD) pages backwards: only files strictly older are
    read, so the page can prepend history as the viewer scrolls left.
    ``after`` refreshes forwards: every file from that day on, so a tab
    that slept across midnight fills the gap; ``complete`` is False when
    more files than ``days`` qualify and the page should reload instead.
    A night session split across two calendar files comes back in one
    piece. Markers cover every trade entry/exit inside the returned span.
    """
    days = max(1, min(int(days), BARS_DAYS_MAX))
    files = _newest(bot_dir, _BARS_RE)
    if before:
        files = [(d, path) for d, path in files if d < before]
    if after:
        files = [(d, path) for d, path in files if d >= after]
    chunk, rest = files[:days], files[days:]
    by_time: dict[int, dict] = {}
    for _day, path in reversed(chunk):          # oldest first; later wins
        for bar in read_bars(path):
            by_time[bar["time"]] = bar
    bars = [by_time[t] for t in sorted(by_time)]
    markers = []
    if bars:
        trades = [_trade_row(t) for t in _session_trades(bot_dir)]
        markers = trade_markers(trades, bars[0]["time"], bars[-1]["time"] + 59)
    return {"bars": bars, "markers": markers,
            "oldest_day": chunk[-1][0] if chunk else (before or ""),
            "newest_day": chunk[0][0] if chunk else "",
            "has_more": bool(rest), "complete": not (after and rest)}


def _session_trades(bot_dir: str) -> list[dict]:
    broker = read_session(bot_dir).get("broker")
    raw = broker.get("trades") if isinstance(broker, dict) else None
    return [t for t in raw if isinstance(t, dict)] if isinstance(raw, list) else []


# ── Regime-switching bots ───────────────────────────────────────────────

REGIME_HISTORY_ROWS = 60


def _is_regime_bot(sess: dict) -> bool:
    return bool(sess.get("regime_mode"))


def read_regime_state(bot_dir: str) -> dict | None:
    """regime_state.json, re-read only when it changed."""
    return _read_if_changed(os.path.join(bot_dir, "regime_state.json"),
                            fileio.read_json, lambda d: isinstance(d, dict))


def read_regime_history(bot_dir: str, n: int = REGIME_HISTORY_ROWS) -> list[dict]:
    """Last ``n`` rows of regime_history.csv, re-read only when it changed."""
    rows = _read_if_changed(os.path.join(bot_dir, "regime_history.csv"),
                            lambda p: _load_regime_history(p, n),
                            lambda r: isinstance(r, list))
    return rows or []


def _load_regime_history(path: str, n: int) -> list[dict]:
    header = fileio.head_line(path)
    if not header:
        return []
    cols = next(csv.reader([header]))
    if "date" not in cols:
        return []
    rows = []
    for line in fileio.tail_lines(path, n + 1):
        if line == header:
            continue
        row = dict(zip(cols, next(csv.reader([line]), [])))
        if row.get("date"):
            rows.append(row)
    return rows[-n:]


def _pause(state: dict) -> dict | None:
    """Flip-counter pause as the monitor states it (active while
    paused_until_session >= session_count), or None."""
    until, count = state.get("paused_until_session"), state.get("session_count")
    if (isinstance(until, int) and isinstance(count, int)
            and not isinstance(until, bool) and until >= count):
        return {"until_session": until, "session_count": count}
    return None


def regime_summary(state: dict | None, sess: dict) -> dict | None:
    """Compact fleet-row view, or None for a non-regime deploy (a plain
    deploy can leave an old regime_state.json behind)."""
    if not _is_regime_bot(sess):
        return None
    state = state or {}
    nxt = state.get("next_session") if isinstance(state.get("next_session"), dict) else {}
    return {"effective": str(state.get("effective_regime") or ""),
            "since": str(state.get("effective_since") or ""),
            "active_leg": str(sess.get("active_leg") or ""),
            "pause": _pause(state),
            "next_action": str(nxt.get("action") or ""),
            "classified": bool(state.get("last_assessed")),
            "news_suppressed": _news_blocks_leg(sess)}


def regime_flags(now: datetime, bot_dir: str, state: dict | None) -> list[dict]:
    """The daily monitor's own state findings, graded the way check_regime
    grades them: ``_check_state`` (or its P1 for a corrupt/missing state
    file), then demoted to P3 with the same tag when the bot showed no
    activity since the last completed night opened (``_was_running``).
    Only regime deploys reach here, so the non-regime demotion never applies."""
    try:
        from scripts.monitor import check_regime as cr
        from scripts.monitor.common import Finding
        sl = cr._switch_logic()
        degraded, _detail = cr._holiday_calendar_status(now, sl)
        name = os.path.basename(os.path.normpath(bot_dir))
        lines, findings = [], []
        if isinstance(state, dict):
            cr._check_state(now, bot_dir, name, state, sl, lines, findings,
                            calendar_degraded=degraded)
        else:
            findings.append(Finding(
                "P1", "regime",
                f"{name}: regime_state.json is corrupt or unreadable — the "
                f"brain cannot restore its regime across a restart"))
        active = cr._was_running(bot_dir, sl.last_completed_night(now) if sl else None)
    except Exception as exc:  # noqa: BLE001 — flags are advisory
        return [{"sev": "P3", "text": redact_line(f"regime check unavailable: {exc}")}]
    out = []
    for f in findings:
        if not active and f.level != "P3":
            out.append({"sev": "P3", "text": redact_line(f"[stopped bot] {f.message}")})
        else:
            out.append({"sev": f.level, "text": redact_line(f.message)})
    return out


def _history_row(row: dict) -> dict:
    keep = ("date", "session", "raw_regime", "effective_regime", "confirm_count",
            "decision", "strategy_active", "applied", "trades", "trading_mode",
            "votes", "adx", "close")
    out = {k: redact_line(str(row.get(k) or "")) for k in keep}
    out["pnl"] = _to_int(row.get("pnl"))
    try:
        from src.regime.episodes import format_votes_cell
        out["votes"] = redact_line(format_votes_cell(str(row.get("votes") or "")))
    except Exception:  # noqa: BLE001
        pass
    return out


def regime_band(history: list[dict]) -> list[dict]:
    """Effective regime over time for the chart band: each NIGHT row's
    classification takes effect at that night's close (open date 15:00
    + 14 h) and holds until the next one."""
    band = []
    for row in history:
        regime = (row.get("effective_regime") or "").strip()
        if (row.get("session") or "").upper() != "NIGHT" or not regime:
            continue
        opened = parse_tpe(f"{row.get('date', '')} 15:00")
        if opened is None:
            continue
        band.append({"time": chart_time(opened) + 14 * 3600, "regime": regime,
                     "strategy": redact_line(str(row.get("strategy_active") or ""))})
    band.sort(key=lambda b: b["time"])
    return band


def _read_json_dict(path: str):
    """Vote / bridge-state JSON, re-read only when the file changed.

    The bridges replace these files with a bare ``os.replace`` (no
    retry), and any open handle makes that replace fail, losing the vote
    or state write. A stat-gated read opens a file only right after a
    bridge wrote it, minutes before its next write.
    """
    return _read_if_changed(path, fileio.read_json, lambda d: isinstance(d, dict))


def _news_blocks_leg(sess: dict) -> bool:
    """Does the news breaker stop the ACTIVE leg right now? The runner's
    own rule (``BreakerState.gates_leg`` with the bot's suppress scope),
    not just "a breaker is set": under ``conflicting_leg`` a bearish
    signal leaves a short leg trading."""
    news = sess.get("news")
    if not isinstance(news, dict):
        return False
    try:
        from src.news.circuit_breaker import BreakerState
        state = BreakerState.from_dict(news)
        return bool(state.gates_leg(str(sess.get("active_leg") or "idle"),
                                    str(sess.get("news_suppress_scope") or "both")))
    except Exception:  # noqa: BLE001 — fall back to "set means blocked"
        return bool(news.get("signal_suppressed") or news.get("event_suppressed"))


def _vote_will_count(vote: dict, limits: dict, classify_at: datetime) -> tuple[bool, str]:
    """``(counts, why_not)`` for a pending vote at the coming classification,
    the runner's rule (``_read_regime_vote``): older than its source's
    ``nightly_vote_max_age_h`` at classify time is dropped unless it was
    hot-lane admitted for that session."""
    source = str(vote.get("source") or "").split("-")[0].strip().upper()
    limit = (limits or {}).get(source)
    if limit is None:
        return True, ""
    if vote.get("admitted_at"):
        return True, ""
    fired = parse_tpe(str(vote.get("fired_at") or ""))
    if fired is None:
        return True, ""
    age_h = (classify_at - fired).total_seconds() / 3600.0
    if age_h > float(limit):
        return False, f"分類時已超過 {float(limit):g} 小時（{age_h:.1f} 小時），將不計入"
    return True, ""


# The desktop tab's labels are bilingual; the page is zh-TW only.
_VOTE_LABELS = {"W2": "W2 跨市場", "W3": "W3 RSS 新聞", "W4": "W4 籌碼"}


def _zh_consumed(line: str) -> str:
    """vote_status.consumed_votes_line without its English glosses."""
    return (line.replace("無投票 none", "無投票")
                .replace(" — 加速確認 accelerated confirm", "（加速確認）"))


def vote_detail(now: datetime, bot_dir: str, sess: dict, state: dict,
                news_paths: dict | None) -> dict | None:
    """External regime votes and the news breaker, as the GUI regime tab
    shows them (``src.news.vote_status``), read without blocking the bot.

    *news_paths* = ``{"regime_vote_path", "signal_path", "rss_state_file"}``
    from the bot's settings.yaml; None when the dashboard has no settings.
    Paths never leave the server.
    """
    try:
        from src.news.vote_status import collect_vote_status, consumed_votes_line
        from src.regime.switch_logic import upcoming_night_session
    except Exception:  # noqa: BLE001 — votes are advisory
        return None
    paths = news_paths or {}
    sources = []
    tonight = ""
    # The runner reads votes only with a news config (``--news``); for any
    # other regime bot a pending vote is never used.
    consumes = bool(sess.get("news_enabled"))
    if consumes and (paths.get("regime_vote_path") or paths.get("signal_path")):
        from src.news.vote_status import read_pending_votes
        upcoming = upcoming_night_session(now)
        tonight = upcoming.key if upcoming else ""
        classify_at = (upcoming.close_dt - timedelta(minutes=2)) if upcoming else now
        raw_votes = read_pending_votes(paths.get("regime_vote_path", ""), tonight,
                                       _read_json_dict)
        report = collect_vote_status(paths.get("regime_vote_path", ""),
                                     paths.get("signal_path", ""),
                                     paths.get("rss_state_file", ""),
                                     tonight, now, read_json=_read_json_dict)
        for st in report.sources:
            counts, why_not = (True, "")
            if st.vote:
                counts, why_not = _vote_will_count(raw_votes.get(st.source) or {},
                                                   paths.get("vote_age_limits_h") or {},
                                                   classify_at)
            sources.append({"source": st.source, "will_count": counts, "why_not": why_not,
                            "label": _VOTE_LABELS.get(st.source, redact_line(st.label)),
                            "vote": st.vote, "vote_expired": st.vote_expired,
                            "vote_target": st.vote_target, "known": st.known,
                            "stale": st.stale, "last_check": st.last_check[:16].replace("T", " "),
                            "context": redact_line(st.context)})
    feats = state.get("last_features") if isinstance(state.get("last_features"), dict) else {}
    news = sess.get("news") if isinstance(sess.get("news"), dict) else {}
    try:
        from scripts.monitor.check_bots import count_decisions
        suppressed_24h = count_decisions(now, bot_dir, "NEWS_SUPPRESSED")
    except Exception:  # noqa: BLE001
        suppressed_24h = None
    return {
        "configured": bool(sources),
        "consumes_votes": consumes,
        "tonight": tonight,
        "sources": sources,
        "consumed": redact_line(_zh_consumed(consumed_votes_line(feats))),
        "consumed_rule": redact_line(str(feats.get("_vote_rule") or "")),
        "last_assessed": str(state.get("last_assessed") or ""),
        "news_enabled": bool(sess.get("news_enabled")),
        "breaker": {
            "blocks_active_leg": _news_blocks_leg(sess),
            "signal_suppressed": bool(news.get("signal_suppressed")),
            "event_suppressed": bool(news.get("event_suppressed")),
            "reason": redact_line(str(news.get("suppressed_reason") or "")),
            "event_name": redact_line(str(news.get("event_name") or "")),
            "direction": str(news.get("signal_direction") or ""),
            "severity": str(news.get("signal_severity") or ""),
            "since": str(news.get("signal_suppressed_at") or "")[:16].replace("T", " "),
            "suppressed_bars_24h": suppressed_24h,
        },
    }


def regime_detail(now: datetime, bot_dir: str, sess: dict,
                  news_paths: dict | None = None) -> dict | None:
    if not _is_regime_bot(sess):
        return None
    raw_state = read_regime_state(bot_dir)
    state = raw_state or {}
    history = read_regime_history(bot_dir)
    feats = state.get("last_features") if isinstance(state.get("last_features"), dict) else {}
    nxt = state.get("next_session") if isinstance(state.get("next_session"), dict) else {}
    num = ("adx", "plus_di", "minus_di", "atr_ratio", "ema_slope", "ema_50", "last_close")
    return {
        **(regime_summary(state, sess) or {}),
        "raw": str(state.get("raw_regime") or ""),
        "pending_label": str(state.get("pending_label") or ""),
        "pending_count": _to_int(state.get("pending_count")),
        "manual_override": str(state.get("manual_override") or ""),
        "last_assessed": str(state.get("last_assessed") or ""),
        "long_strategy": str(sess.get("long_strategy") or ""),
        "short_strategy": str(sess.get("short_strategy") or ""),
        "news_enabled": bool(sess.get("news_enabled")),
        "next": {"date": str(nxt.get("date") or ""), "action": str(nxt.get("action") or ""),
                 "strategy": str(nxt.get("strategy") or ""),
                 "reason": redact_line(str(nxt.get("reason") or "")),
                 "executed": bool(nxt.get("executed")),
                 "executed_at": str(nxt.get("executed_at") or "")},
        "features": {k: feats.get(k) for k in num
                     if isinstance(feats.get(k), (int, float))
                     and not isinstance(feats.get(k), bool)},
        "votes": [redact_line(str(v)) for v in (feats.get("_vote_sources") or [])
                  if isinstance(v, (str, int, float))],
        "flags": regime_flags(now, bot_dir, raw_state),
        "history": [_history_row(r) for r in reversed(history)],
        "band": regime_band(history),
        "vote_status": vote_detail(now, bot_dir, sess, state, news_paths),
    }


def bot_detail(now: datetime, bot_dir: str, *, pid_alive_fn=pid_alive,
               news_paths: dict | None = None) -> dict:
    """Everything for the bot page except candles (``bars_window``)."""
    summary = bot_summary(now, bot_dir, pid_alive_fn=pid_alive_fn)
    sess = read_session(bot_dir)
    trades = [_trade_row(t, summary["point_value"]) for t in _session_trades(bot_dir)]
    logs = _newest(bot_dir, _DEBUG_RE)
    log_path = logs[0][1] if logs else None
    log_lines = []
    if log_path:
        for line in fileio.tail_lines(log_path, MAX_LOG_LINES):
            clean = redact_line(line)
            log_lines.append({"text": clean, "sev": log_severity(clean)})
    return {"bot": summary, "equity": equity_points(trades),
            "trades": list(reversed(trades))[:MAX_TRADES],
            "decisions": list(reversed(read_decisions(bot_dir, MAX_DECISIONS))),
            "log_file": os.path.basename(log_path) if log_path else "",
            "log": log_lines,
            "regime": regime_detail(now, bot_dir, sess, news_paths)}
