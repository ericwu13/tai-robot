"""W2 ticker strip and W4 籌碼 panel for the 新聞訊號 view (display-only).

Both read what the bridges persist for display and nothing feeds back
into a vote:

- W2: ``crossmarket_quotes.json`` beside ``news.signal_path`` (one row per
  symbol per pass).  Bridges on an older checkout do not write it yet;
  then the strip falls back to ``monitor_state.json`` ``last_result``.
- W4: ``.chips_state.json`` beside ``news.regime_vote_path`` — ``daily``
  (decision rows) when present, else just the ΔOI baseline ``history``.

Every file goes through the caller's ``read_json`` (the dashboard passes
its stat-gated share-delete reader) and every field is optional.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

_TZ_TPE = timezone(timedelta(hours=8))

QUOTES_NAME = "crossmarket_quotes.json"
W2_STATE_NAME = "monitor_state.json"
CHIPS_STATE_NAME = ".chips_state.json"

# W2 polls every 2 minutes from 08:00 to 05:00 TPE.  Three missed passes
# inside that window = stale.  (vote_status uses 24h because it only asks
# "is the vote source alive at all"; a ticker 20 minutes old is wrong.)
W2_STALE_S = 6 * 60
W2_WINDOW = (8, 5)                  # active from 08:00 until 05:00 next day
CHIPS_MAX_GAP_DAYS = 5              # chips_monitor.MAX_PREV_GAP_DAYS
CHIPS_ROWS = 6
SPARK_DAYS = 40

TIERS = (("signal", "訊號層", "下跌且達票數門檻可觸發 risk_off"),
         ("alert", "警示層", "只通知、計入票數，不會單獨觸發訊號"))

SYMBOL_NAMES = {
    "SOXX": "費半 ETF", "TSM": "台積電 ADR", "QQQ": "那斯達克 100",
    "ASML.AS": "艾司摩爾", "^STOXX50E": "歐洲 50", "NQ=F": "那指期貨",
    "^N225": "日經 225", "^KS11": "韓國綜合", "^HSI": "恆生指數",
    "^TWII": "台灣加權", "000001.SS": "上證指數",
}

FIRED_LABELS = {
    "signal-down": "訊號層下跌（risk_off）",
    "signal-down(no-quorum)": "訊號層下跌（未達票數，只通知）",
    "signal-up": "訊號層上漲（只通知）",
    "alert-down": "警示層下跌", "alert-up": "警示層上漲",
}

DECISION_LABELS = {"trending-up": "偏多投票", "trending-down": "偏空投票"}


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _ts(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_TZ_TPE)


def _str(value, limit: int = 300) -> str:
    text = str(value or "") if not isinstance(value, (dict, list)) else ""
    return text[:limit]


def _beside(path: str, name: str) -> str:
    return os.path.join(os.path.dirname(str(path)) or ".", name) if path else ""


# ── W2 ───────────────────────────────────────────────────────────────────

def w2_window_start(now: datetime) -> datetime | None:
    """Start of the active polling window containing *now*, or None when
    *now* is in the 05:00-08:00 idle gap."""
    now = now.astimezone(_TZ_TPE)
    start_h, end_h = W2_WINDOW
    if now.hour >= start_h:
        return now.replace(hour=start_h, minute=0, second=0, microsecond=0)
    if now.hour < end_h:
        return (now - timedelta(days=1)).replace(hour=start_h, minute=0,
                                                 second=0, microsecond=0)
    return None


def w2_stale(now: datetime, last: datetime | None) -> tuple[bool, bool]:
    """``(stale, idle)`` for W2 at *now* given its last pass."""
    start = w2_window_start(now)
    if start is None:
        return False, True
    if last is None:
        return True, False
    if (now - start).total_seconds() <= W2_STALE_S:
        return False, False                     # window just opened
    return (now - last).total_seconds() > W2_STALE_S, False


def _stale_label(reason: str) -> str:
    if not reason:
        return ""
    if reason == "fetch_failed":
        return "抓取失敗"
    if "allowance" in reason:
        return "報價過舊"
    if "period" in reason or "last bar" in reason:
        return "非交易時段"
    return reason


def _quote_row(sym: str, q: dict) -> dict:
    pct = _num(q.get("pct"))
    down, up = _num(q.get("down")), _num(q.get("up"))
    vdown, vup = _num(q.get("vote_down")), _num(q.get("vote_up"))
    fresh = q.get("fresh") is True
    breach = vote = ""
    if fresh and pct is not None:
        if down is not None and pct <= down:
            breach = "down"
        elif up is not None and pct >= up:
            breach = "up"
        if vdown is not None and pct <= vdown:
            vote = "down"
        elif vup is not None and pct >= vup:
            vote = "up"
    reason = _str(q.get("stale_reason"), 120)
    return {"symbol": sym, "name": SYMBOL_NAMES.get(sym, sym), "pct": pct,
            "price": _num(q.get("price")), "prev_close": _num(q.get("prev_close")),
            "age_s": _num(q.get("age_s")), "fresh": fresh,
            "stale_reason": "" if fresh else reason,
            "stale_label": "" if fresh else _stale_label(reason),
            "down": down, "up": up, "vote_down": vdown, "vote_up": vup,
            "breach": breach, "vote": vote}


def crossmarket(now: datetime, paths: dict | None, read_json) -> dict:
    signal_path = (paths or {}).get("signal_path") or ""
    if not signal_path:
        return {"configured": False, "known": False}
    data = read_json(_beside(signal_path, QUOTES_NAME))
    state = read_json(_beside(signal_path, W2_STATE_NAME)) or {}
    if not isinstance(data, dict) or not isinstance(data.get("quotes"), dict):
        last = _ts(state.get("last_check"))
        stale, idle = w2_stale(now, last)
        return {"configured": True, "known": False,
                "last_check": _str(state.get("last_check"), 25),
                "last_result": _str(state.get("last_result")),
                "stale": stale and last is not None, "idle": idle}
    asof = _ts(data.get("asof"))
    stale, idle = w2_stale(now, asof)
    groups = {tier: [] for tier, _, _ in TIERS}
    for sym, q in data["quotes"].items():
        if not isinstance(q, dict):
            continue
        tier = q.get("tier") if q.get("tier") in groups else "alert"
        groups[tier].append(_quote_row(str(sym), q))
    quorum = data.get("quorum") if isinstance(data.get("quorum"), dict) else {}

    def syms(key):
        v = quorum.get(key)
        return [str(s) for s in v] if isinstance(v, list) else []

    last_fired = data.get("last_fired") if isinstance(data.get("last_fired"), dict) else None
    fired_what = [str(x) for x in (last_fired or {}).get("what") or []
                  if isinstance(x, str)]
    min_symbols = quorum.get("min_symbols")
    return {
        "configured": True, "known": True,
        "asof": _str(data.get("asof"), 25),
        "age_s": int((now - asof).total_seconds()) if asof else None,
        "stale": stale, "idle": idle,
        "quorum": {"direction": _str(quorum.get("direction"), 20),
                   "up": syms("up"), "down": syms("down"),
                   "min_symbols": min_symbols if isinstance(min_symbols, int) else 2},
        "last_fired": ({"at": _str(last_fired.get("at"), 25),
                        "what": fired_what,
                        "labels": [FIRED_LABELS.get(x, x) for x in fired_what]}
                       if last_fired else None),
        "fetch_failed": len([s for s in data.get("fetch_failed") or []
                             if isinstance(s, str)]),
        "groups": [{"tier": tier, "label": label, "hint": hint, "rows": groups[tier]}
                   for tier, label, hint in TIERS],
    }


# ── W4 ───────────────────────────────────────────────────────────────────

def reason_zh(reason: str) -> str:
    r = str(reason or "")
    if "hysteresis" in r:
        return "ΔOI 未達 1,000 口，訊號太弱，不投票"
    if "contradiction" in r:
        return "期貨與現貨方向相反（多半是避險），不投票"
    if r.startswith("strong"):
        return "ΔOI 達 5,000 口以上，強訊號"
    if "equity flow agrees" in r:
        return "ΔOI 偏弱，但外資現貨買賣超同向確認"
    if "no confirming equity flow" in r:
        return "ΔOI 偏弱且現貨沒有同向確認，不投票"
    if "no recent OI history" in r:
        return "缺少前一交易日未平倉，僅記錄今日水位"
    if "TWSE equity flow unavailable" in r:
        return "證交所現貨資料暫時取不到，無法排除避險，不投票（稍後重試）"
    return r


def _fmt_date(key: str) -> str:
    k = "".join(ch for ch in str(key) if ch.isdigit())
    return f"{k[:4]}-{k[4:6]}-{k[6:8]}" if len(k) == 8 else str(key)


def _gap_days(a: str, b: str) -> int | None:
    try:
        return (datetime.strptime(b, "%Y%m%d") - datetime.strptime(a, "%Y%m%d")).days
    except (TypeError, ValueError):
        return None


def chips(now: datetime, paths: dict | None, read_json) -> dict:
    base = (paths or {}).get("regime_vote_path") or ""
    if not base:
        return {"configured": False, "known": False}
    state = read_json(_beside(base, CHIPS_STATE_NAME))
    if not isinstance(state, dict):
        return {"configured": True, "known": False}
    history = state.get("history") if isinstance(state.get("history"), dict) else {}
    daily = state.get("daily") if isinstance(state.get("daily"), dict) else {}
    levels: dict[str, float] = {}
    for d, v in history.items():
        if _num(v) is not None:
            levels[str(d)] = _num(v)
    for d, row in daily.items():
        if isinstance(row, dict) and _num(row.get("net_oi")) is not None:
            levels[str(d)] = _num(row.get("net_oi"))
    spark = [{"date": _fmt_date(d), "net_oi": v}
             for d, v in sorted(levels.items())[-SPARK_DAYS:]]

    rows = []
    for d in sorted(daily, reverse=True)[:CHIPS_ROWS]:
        row = daily[d]
        if not isinstance(row, dict):
            continue
        decision = row.get("decision") if row.get("decision") in DECISION_LABELS else ""
        rows.append({"date": _fmt_date(d), "net_oi": _num(row.get("net_oi")),
                     "delta_oi": _num(row.get("delta_oi")),
                     "equity_net": _num(row.get("equity_net")),
                     "decision": decision,
                     "decision_label": DECISION_LABELS.get(decision, "不投票"),
                     "reason": _str(row.get("reason")),
                     "reason_zh": reason_zh(row.get("reason")),
                     "final": row.get("final") is not False,
                     "session": _str(row.get("session"), 20),
                     "at": _str(row.get("at"), 25)})

    # Level + Δ for the headline: the newest decision row, else the
    # baseline history (bridges on an older checkout write no ``daily``).
    latest = None
    if rows:
        latest = {k: rows[0][k] for k in ("date", "net_oi", "delta_oi")}
    elif levels:
        dates = sorted(levels)
        last = dates[-1]
        delta = None
        if len(dates) > 1:
            gap = _gap_days(dates[-2], last)
            if gap is not None and gap <= CHIPS_MAX_GAP_DAYS:
                delta = levels[last] - levels[dates[-2]]
        latest = {"date": _fmt_date(last), "net_oi": levels[last], "delta_oi": delta}
    return {"configured": True, "known": True, "has_daily": bool(rows),
            "last_check": _str(state.get("last_check"), 25),
            "last_result": _str(state.get("last_result")),
            "last_decided": _fmt_date(state.get("last_decided_date") or "")
            if state.get("last_decided_date") else "",
            "latest": latest, "days": rows, "spark": spark}
