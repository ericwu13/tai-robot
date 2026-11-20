"""籌碼走勢 vs 台指: W4's foreign-investor flow history on one daily axis
(``GET /api/chips/trend[?symbol=]``).

Rows come from the bridge's ``chips_history.jsonl`` (every ``daily`` row
W4 ever recorded; ``chips_monitor.HISTORY_NAME``) merged with the
``daily`` map of ``.chips_state.json`` (the last 60 dates, so a host whose
history file is still new shows its recent days). Per date the highest
source wins — live > log / state > backfill — and within a source the
later line wins (a TWSE retry, a forced re-run).

Per day: net OI level, ΔOI (what W4 votes on), the foreign cash-equity
net buy, W4's decision and why, and two rolling sums over the last
FLOW_WINDOW rows of an unbroken run: ``flow`` (ΔOI) and ``equity_flow``,
their daily averages (``flow_avg`` / ``equity_avg``, the page's 5-day
lines) and ``verdict`` (``flow_verdict``: W4's daily thresholds applied to
those averages — the page's one-line summary).
A missing ΔOI or a gap of more than MAX_PREV_GAP_DAYS calendar days
between rows restarts both, the same rule W4 uses to refuse a stale
baseline. Settlement days are flagged: expiring contracts drop out of OI
that day, so ΔOI moves mechanically.

``price``: one day-session (08:45-13:45) candle per date of an index-
futures symbol, from the bots' 1-min CSVs via ``news_trend``'s cached
30-min buckets — no new file reads.

Thresholds mirror ``chips_monitor`` (tests pin them): importing the
bridge module here would reconfigure the server's stdout.

Read-only: files go through ``fileio`` (share-delete) and the history is
re-parsed only when its stat changes. Times are chart seconds (TPE date
at 00:00 encoded as UTC, the page's convention).
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta

from scripts.dashboard import fileio, news_trend
from scripts.dashboard.bridge_panels import (
    CHIPS_STATE_NAME, DECISION_LABELS, _beside, _num, _str, reason_zh)

HISTORY_NAME = "chips_history.jsonl"           # chips_monitor.HISTORY_NAME
HYSTERESIS_CONTRACTS = 1_000                   # chips_monitor.HYSTERESIS_CONTRACTS
STRONG_CONTRACTS = 5_000                       # chips_monitor.STRONG_CONTRACTS
EQUITY_FLOW_THRESHOLD_NTD = 5_000_000_000      # chips_monitor.EQUITY_FLOW_THRESHOLD_NTD
MAX_PREV_GAP_DAYS = 5                          # chips_monitor.MAX_PREV_GAP_DAYS
FLOW_WINDOW = 5                                # trading rows in the rolling sums
DAY_SESSION = ((8, 45), (13, 45))
SRC_RANK = {"live": 3, "log": 2, "state": 2, "backfill": 1}

_lock = threading.Lock()
_hist_cache: dict = {}


def _parse_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and len(str(row.get("date", ""))) == 8:
            out.append(row)
    return out


def read_history(path: str) -> list[dict]:
    """History lines in file order (shared; don't mutate)."""
    key = news_trend._stat_key(path)
    if key is None:
        return []
    with _lock:
        hit = _hist_cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
    rows = _parse_lines(fileio.read_text(path) or "")
    with _lock:
        _hist_cache.clear()
        _hist_cache[path] = (key, rows)
    return rows


def merge_rows(lines: list[dict], state_daily) -> dict[str, dict]:
    """``{YYYYMMDD: row}``: highest source per date, later line within one."""
    out: dict[str, dict] = {}

    def put(day: str, row: dict) -> None:
        cur = out.get(day)
        if cur is None or SRC_RANK.get(row.get("src"), 0) >= SRC_RANK.get(cur.get("src"), 0):
            out[day] = row

    if isinstance(state_daily, dict):
        for day, row in sorted(state_daily.items()):
            if isinstance(row, dict) and len(str(day)) == 8:
                put(str(day), {**row, "date": str(day), "src": "state"})
    for row in lines:
        put(str(row["date"]), row)
    return out


def _day(key: str) -> date | None:
    try:
        return datetime.strptime(key, "%Y%m%d").date()
    except ValueError:
        return None


def _chart_day(d: date) -> int:
    return news_trend._chart(datetime(d.year, d.month, d.day))


def _is_settlement(d: date) -> bool:
    try:
        from src.market_data.holidays import is_settlement_day  # noqa: PLC0415
        return bool(is_settlement_day(d))
    except Exception:  # noqa: BLE001 — a calendar bug must not blank the chart
        return False


def kind_of(row: dict) -> str:
    """Marker class: vote / hedge (contradiction) / pending / baseline / none."""
    reason = str(row.get("reason") or "")
    if row.get("decision") in DECISION_LABELS:
        return "vote"
    if row.get("final") is False:
        return "pending"
    if "contradiction" in reason:
        return "hedge"
    if "no recent OI history" in reason:
        return "baseline"
    return "none"


def build_days(rows: dict[str, dict]) -> list[dict]:
    out, run, prev = [], [], None
    for key in sorted(rows):
        d = _day(key)
        if d is None:
            continue
        row = rows[key]
        delta = _num(row.get("delta_oi"))
        equity = _num(row.get("equity_net"))
        if delta is None or (prev is not None and (d - prev).days > MAX_PREV_GAP_DAYS):
            run = []
        prev = d
        if delta is not None:
            run = (run + [(delta, equity)])[-FLOW_WINDOW:]
        full = len(run) == FLOW_WINDOW
        flow = sum(x for x, _ in run) if run else None
        eq = [e for _, e in run if e is not None]
        decision = row.get("decision") if row.get("decision") in DECISION_LABELS else ""
        equity_flow = sum(eq) if eq else None
        flow_avg = flow / len(run) if run else None
        equity_avg = equity_flow / len(eq) if eq else None
        out.append({
            "time": _chart_day(d), "date": d.isoformat(),
            "net_oi": _num(row.get("net_oi")), "delta_oi": delta, "equity_net": equity,
            "flow": flow, "equity_flow": equity_flow, "flow_days": len(run),
            "flow_avg": flow_avg, "equity_avg": equity_avg,
            "flow_full": full, "verdict": flow_verdict(flow_avg, equity_avg), "decision": decision,
            "decision_label": DECISION_LABELS.get(decision, "不投票"),
            "kind": kind_of(row), "final": row.get("final") is not False,
            "settlement": _is_settlement(d),
            "session": _str(row.get("session"), 20),
            "reason_zh": reason_zh(row.get("reason")),
            "src": _str(row.get("src"), 12) or "live",
        })
    return out


def build_price(file_list: list[tuple[str, str]], since: str) -> list[dict]:
    """Day-session daily candles from the cached 30-min buckets."""
    (h0, m0), (h1, m1) = DAY_SESSION
    days: dict[date, list] = {}
    for day, path in file_list:
        if since and day < since:
            continue
        for b, v in news_trend._file_buckets(path).items():
            wall = news_trend._wall(b)
            hm = (wall.hour, wall.minute)
            if not ((h0, m0) <= hm < (h1, m1)):
                continue
            cur = days.get(wall.date())
            if cur is None:
                days[wall.date()] = [b, b, v[2], v[3], v[4], v[5]]
                continue
            if b < cur[0]:
                cur[0], cur[2] = b, v[2]
            if b >= cur[1]:
                cur[1], cur[5] = b, v[5]
            cur[3] = max(cur[3], v[3])
            cur[4] = min(cur[4], v[4])
    return [{"time": _chart_day(d), "open": v[2], "high": v[3], "low": v[4], "close": v[5]}
            for d, v in sorted(days.items())]


def _sign(x) -> int:
    return 0 if not x else (1 if x > 0 else -1)


def flow_verdict(flow_avg, equity_avg) -> str:
    """One word for the rolling window: who moved, and did they agree.

    A side counts as moving when its DAILY AVERAGE over the window clears
    W4's own daily threshold, with ``chips_monitor.decide``'s boundaries
    (|ΔOI| >= HYSTERESIS_CONTRACTS, |equity| > EQUITY_FLOW_THRESHOLD_NTD), so
    a few hundred contracts of drift never reads as "inflow" and the page's
    average line leaving the grey band means the same as this word.
    ``""`` = not enough data.
    """
    if flow_avg is None or equity_avg is None:
        return ""
    fut = 0 if abs(flow_avg) < HYSTERESIS_CONTRACTS else _sign(flow_avg)
    eq = _sign(equity_avg) if abs(equity_avg) > EQUITY_FLOW_THRESHOLD_NTD else 0
    if fut and eq:
        return "diverge" if fut != eq else ("agree_in" if fut > 0 else "agree_out")
    if fut:
        return "fut_in" if fut > 0 else "fut_out"
    if eq:
        return "eq_in" if eq > 0 else "eq_out"
    return "quiet"


def trend(paths: dict | None, read_json, base_dir: str, symbol: str = "") -> dict:
    base = (paths or {}).get("regime_vote_path") or ""
    if not base:
        return {"available": False, "reason": "not_configured"}
    state = read_json(_beside(base, CHIPS_STATE_NAME))
    lines = read_history(_beside(base, HISTORY_NAME))
    daily = state.get("daily") if isinstance(state, dict) else None
    rows = merge_rows(lines, daily)
    days = build_days(rows)
    files = news_trend.symbol_files(base_dir) if base_dir else {}
    sym = news_trend.pick_symbol(files, symbol)
    since = (datetime.strptime(min(rows), "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d") \
        if rows else ""
    price = build_price(files.get(sym, []), since) if sym and days else []
    last = days[-1] if days else None
    summary = None
    if last:
        summary = {"date": last["date"], "flow": last["flow"],
                   "equity_flow": last["equity_flow"], "flow_days": last["flow_days"],
                   "flow_avg": last["flow_avg"], "equity_avg": last["equity_avg"],
                   "agree":(_sign(last["flow"]) == _sign(last["equity_flow"]) != 0)
                   if last["flow"] is not None and last["equity_flow"] is not None else None,
                   "verdict": last["verdict"]}
    sources: dict[str, int] = {}
    for d in days:
        sources[d["src"]] = sources.get(d["src"], 0) + 1
    return {"available": True, "reason": "" if days else "empty",
            "thresholds": {"hysteresis": HYSTERESIS_CONTRACTS, "strong": STRONG_CONTRACTS,
                           "equity_ntd": EQUITY_FLOW_THRESHOLD_NTD,
                           "max_gap_days": MAX_PREV_GAP_DAYS, "window": FLOW_WINDOW},
            "days": days, "price": price, "symbol": sym,
            "symbols": sorted(files, key=lambda s: (not news_trend._is_index(s), s)),
            "summary": summary, "sources": sources}
