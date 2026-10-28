"""Real-order P&L from recorded fill prices, with an explicit trust rule.

The broker's ``pnl`` on every trade is the SIMULATED result. A trade that
went to the account also carries ``real_entry_price`` / ``real_exit_price``
(the actual fills), and ``source == "real"`` once a real entry fill was
confirmed (``SimulatedBroker`` downgrades an unconfirmed one to "paper").
Trades from builds before ``source`` existed have it empty.

Recorded real prices are only trustworthy from #92 on (v2.17.8, shipped
2026-07-26). Before it, the exit price could come from
``GetFulfillReport(..., 4)``, a day-cumulative AVERAGE (off by hundreds of
points), and some early rows hold a parsed field instead of a price (41,
133, 200 ...). No trade field says which code path captured a price, so
the ship date is the boundary. After it, real and simulated prices agree
within tens of points on the live bots; a price more than
``IMPLAUSIBLE_FRACTION`` away from the simulated one is still treated as a
data error, not slippage.

Per trade:
    real        both prices trusted
    estimated   one price trusted, the other never recorded (sim used)
    unreliable  a recorded price fails the trust rule (kept out of totals)
    missing     a real order with no recorded fill price at all
    ""          not a real-order trade

All amounts are gross: before broker fees and the futures transaction tax.
"""

from __future__ import annotations

from datetime import datetime

from scripts.monitor.common import TZ_TPE

# #92 (OnNewData deal rows as the per-fill price source) shipped in v2.17.8.
REAL_PRICES_TRUSTED_FROM = datetime(2026, 7, 26, tzinfo=TZ_TPE)
IMPLAUSIBLE_FRACTION = 0.02          # ~900 points on TMF at 45,000

COUNTED = ("real", "estimated")


def _int(value) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _parse(text) -> datetime | None:
    if not isinstance(text, str) or not text.strip():
        return None
    head = text.strip().replace("/", "-").replace("T", " ")[:16]
    try:
        return datetime.strptime(head, "%Y-%m-%d %H:%M").replace(tzinfo=TZ_TPE)
    except ValueError:
        return None


def _price_check(real: int, sim: int, when: datetime | None) -> str:
    """"" when the recorded real price is trustworthy, else the reason."""
    if when is None or when < REAL_PRICES_TRUSTED_FROM:
        return "recorded before #92 (2026-07-26)"
    if sim > 0 and abs(real - sim) > sim * IMPLAUSIBLE_FRACTION:
        return f"implausible fill price {real} (sim {sim})"
    return ""


def is_real_order(trade: dict) -> bool:
    source = str(trade.get("source") or "")
    if source:
        return source == "real"
    return bool(_int(trade.get("real_entry_price")) or _int(trade.get("real_exit_price")))


def classify(trade: dict, point_value: int) -> dict:
    """``{"status", "real_pnl", "slippage", "reason"}`` for one trade."""
    out = {"status": "", "real_pnl": None, "slippage": None, "reason": ""}
    if not is_real_order(trade):
        return out
    sim_entry, sim_exit = _int(trade.get("entry_price")), _int(trade.get("exit_price"))
    real_entry, real_exit = _int(trade.get("real_entry_price")), _int(trade.get("real_exit_price"))
    if not real_entry and not real_exit:
        out.update(status="missing", reason="no recorded fill price")
        return out
    problems = []
    if real_entry:
        why = _price_check(real_entry, sim_entry,
                           _parse(trade.get("real_entry_dt")) or _parse(trade.get("entry_dt")))
        if why:
            problems.append(f"entry: {why}")
    if real_exit:
        why = _price_check(real_exit, sim_exit,
                           _parse(trade.get("real_exit_dt")) or _parse(trade.get("exit_dt")))
        if why:
            problems.append(f"exit: {why}")
    if problems:
        out.update(status="unreliable", reason="; ".join(problems))
        return out
    entry = real_entry or sim_entry
    exit_ = real_exit or sim_exit
    sign = 1 if str(trade.get("side") or "").upper() == "LONG" else -1
    qty = _int(trade.get("qty")) or 1
    real_pnl = (exit_ - entry) * sign * qty * point_value
    out.update(status="real" if real_entry and real_exit else "estimated",
               real_pnl=real_pnl, slippage=real_pnl - _int(trade.get("pnl")))
    if out["status"] == "estimated":
        out["reason"] = ("exit" if not real_exit else "entry") + " fill not recorded; sim price used"
    return out


def summarize(trades: list[dict], point_value: int, day_start: datetime) -> dict | None:
    """Per-bot totals, or None when the bot never placed a real order."""
    rows = [(t, classify(t, point_value)) for t in trades]
    rows = [(t, c) for t, c in rows if c["status"]]
    if not rows:
        return None
    counted = [(t, c) for t, c in rows if c["status"] in COUNTED]
    today = [(t, c) for t, c in counted
             if (dt := _parse(t.get("exit_dt"))) is not None and dt >= day_start]
    return {
        "pnl_total": sum(c["real_pnl"] for _, c in counted),
        "pnl_today": sum(c["real_pnl"] for _, c in today),
        "sim_pnl_same_trades": sum(_int(t.get("pnl")) for t, _ in counted),
        "slippage_total": sum(c["slippage"] for _, c in counted),
        "trades": len(counted),
        "trades_today": len(today),
        "estimated": sum(1 for _, c in counted if c["status"] == "estimated"),
        "unreliable": sum(1 for _, c in rows if c["status"] == "unreliable"),
        "missing": sum(1 for _, c in rows if c["status"] == "missing"),
        "trusted_from": REAL_PRICES_TRUSTED_FROM.strftime("%Y-%m-%d"),
    }
