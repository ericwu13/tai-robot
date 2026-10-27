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
from datetime import datetime, timezone

from scripts.monitor.check_bots import (
    HEARTBEAT_FILENAME, RECENT_CRASH_DAYS, classify_heartbeat,
)
from scripts.monitor.check_logs import PATTERNS
from scripts.monitor.common import (
    TZ_TPE, discover_bot_dirs, pid_alive, redact,
)
from scripts.dashboard import fileio

# A stopped bot with no file activity in this window is retired and is
# hidden unless the viewer asks for it.
ACTIVE_DAYS = 7.0
MAX_TRADES = 200
MAX_DECISIONS = 60
MAX_LOG_LINES = 150
MAX_DAYS = 14

_BARS_RE = re.compile(r"^bars_1m_(\d{8})\.csv$")
_DEBUG_RE = re.compile(r"^debug_(\d{8})\.log$")
# Discord channel/user IDs (snowflakes) and TW national-ID-shaped values
# appear in debug logs; neither may leave the box through the page.
_RE_SNOWFLAKE = re.compile(r"\b\d{17,20}\b")
_RE_TW_ID = re.compile(r"\b[A-Z][12]\d{8}\b")


def redact_line(text: str) -> str:
    out = redact(text)
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
    except (TypeError, ValueError):
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
              for n in ("session.json", "decisions.csv", HEARTBEAT_FILENAME)]
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
    NO_HEARTBEAT  lock PID alive, no usable heartbeat (older build)
    CRASHED   lock left behind by a dead PID, bot active recently
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
        recent = (activity_ts is not None and
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


def bot_summary(now: datetime, bot_dir: str, *, pid_alive_fn=pid_alive) -> dict:
    name = os.path.basename(os.path.normpath(bot_dir))
    activity = last_activity_ts(bot_dir)
    info = {"id": name, "symbol": name.partition("_")[0],
            "bot_name": name.partition("_")[2] or name,
            "last_activity_age_s": (None if activity is None
                                    else round(now.timestamp() - activity)),
            **bot_state(now, bot_dir, pid_alive_fn=pid_alive_fn,
                        activity_ts=activity)}
    sess = fileio.read_json(os.path.join(bot_dir, "session.json"))
    sess = sess if isinstance(sess, dict) else {}
    broker = sess.get("broker") if isinstance(sess.get("broker"), dict) else {}
    trades = broker.get("trades") if isinstance(broker.get("trades"), list) else []
    point_value = _to_int(sess.get("point_value") or broker.get("point_value"), 1)
    today = now.astimezone(TZ_TPE).date().isoformat()
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
        "pnl_today": sum(_to_int(t.get("pnl")) for t in trades
                         if str(t.get("exit_dt") or "")[:10] == today),
        "trades_today": sum(1 for t in trades
                            if str(t.get("exit_dt") or "")[:10] == today),
        "position": _position(broker, point_value, last_close),
        "last_price": last_close,
        "last_bar_dt": bar_dt.strftime("%Y-%m-%d %H:%M") if bar_dt else "",
        "saved_at": str(sess.get("saved_at") or ""),
    })
    decisions = read_decisions(bot_dir, 1)
    info["last_decision"] = decisions[-1] if decisions else None
    running = info["state"] in ("RUNNING", "LAGGING", "HUNG", "NO_HEARTBEAT",
                                "CRASHED", "UNKNOWN")
    info["active"] = running or (
        activity is not None and now.timestamp() - activity <= ACTIVE_DAYS * 86400)
    return info


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
    bots = [bot_summary(now, d, pid_alive_fn=pid_alive_fn)
            for d in discover_bot_dirs(base_dir)]
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
        except ValueError:
            continue
        t = chart_time(dt)
        by_time[t] = {"time": t, "open": o, "high": h, "low": lo, "close": c,
                      "volume": _to_int(parts[5]) if len(parts) > 5 else 0}
    return [by_time[t] for t in sorted(by_time)]


def _trade_row(t: dict) -> dict:
    return {"side": str(t.get("side") or ""), "qty": _to_int(t.get("qty"), 1),
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


def bot_detail(now: datetime, bot_dir: str, day: str | None = None, *,
               pid_alive_fn=pid_alive) -> dict:
    summary = bot_summary(now, bot_dir, pid_alive_fn=pid_alive_fn)
    bar_files = _newest(bot_dir, _BARS_RE)[:MAX_DAYS]
    days = [d for d, _ in bar_files]
    if day not in days:
        day = days[0] if days else None
    bars = read_bars(dict(bar_files)[day]) if day else []
    sess = fileio.read_json(os.path.join(bot_dir, "session.json"))
    broker = (sess or {}).get("broker") if isinstance(sess, dict) else None
    raw_trades = (broker or {}).get("trades") if isinstance(broker, dict) else None
    trades = [_trade_row(t) for t in (raw_trades or []) if isinstance(t, dict)]
    markers = trade_markers(trades, bars[0]["time"], bars[-1]["time"] + 59) if bars else []

    logs = dict(_newest(bot_dir, _DEBUG_RE))
    log_path = logs.get(day) or (logs[max(logs)] if logs else None)
    log_lines = []
    if log_path:
        for line in fileio.tail_lines(log_path, MAX_LOG_LINES):
            clean = redact_line(line)
            log_lines.append({"text": clean, "sev": log_severity(clean)})
    return {"bot": summary, "days": days, "day": day, "bars": bars,
            "markers": markers, "equity": equity_points(trades),
            "trades": list(reversed(trades))[:MAX_TRADES],
            "decisions": list(reversed(read_decisions(bot_dir, MAX_DECISIONS))),
            "log_file": os.path.basename(log_path) if log_path else "",
            "log": log_lines}
