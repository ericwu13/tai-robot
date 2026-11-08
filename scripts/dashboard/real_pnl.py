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
    estimated   one price trusted, the other never recorded (sim used); or a
                CONFIRMED real order (``source == "real"``) whose pre-#92
                prices were replaced by the simulated ones
    unreliable  a recorded price fails the trust rule (kept out of totals)
    missing     a real order with no recorded fill price at all
    ""          not a real-order trade

Pre-#92 prices on a confirmed real order are replaced, not excluded: the
June-July 2026 real trades made most of the account's gains, and their
simulated P&L tracks the account balance within ~2k NTD a month (Steven
audit, docs/real_order_signature.md), while the recorded day averages do
not. Rows that are not ``source == "real"`` keep the exclusion: an
unaudited legacy row with only a planted price may be a paper trade.

All amounts are gross: before broker fees and the futures transaction tax.

Known limits (no field in the data can tell these apart):
- the boundary is #92's SHIP date; a bot still running an older build
  after it would record day averages with later timestamps;
- the entry side is gated by the same date although it never depended
  on #92 (OpenInterest ``avg_cost`` since #45, deal rows later), which
  only makes the rule more conservative;
- the ``avg_cost`` entry fallback matches a 2-character product prefix
  and blends if another position on the same commodity is open;
- with partial fills only the first deal row is kept (all live bots
  trade one lot).
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


PRE_92 = "recorded before #92 (2026-07-26)"


def _price_check(real: int, sim: int, when: datetime | None) -> str:
    """"" when the recorded real price is trustworthy, else the reason."""
    if when is None or when < REAL_PRICES_TRUSTED_FROM:
        return PRE_92
    if sim > 0 and abs(real - sim) > sim * IMPLAUSIBLE_FRACTION:
        return f"implausible fill price {real} (sim {sim})"
    return ""


def is_real_order(trade: dict) -> bool:
    """``source == "real"``, a recorded order seq, or (legacy rows only)
    any recorded real fill price.

    The broker relabels a trade "paper" when no real ENTRY fill was
    confirmed, which also happens when the order did reach the account
    but its confirmation was lost (a dropped race, an OpenInterest error,
    a missed deal row).

    Builds that stamp ``entry_order_seq`` / ``exit_order_seq`` (the
    SendFutureOrderCLR seq, written when the order is accepted) carry the
    proof on the trade itself, so for those rows the seq decides and a
    stray price is not proof. Older rows lack the keys; for them a recorded
    real price is the only evidence an order went out. Pre-#92 paper rows
    can also carry a day-average exit price planted from GetFulfillReport;
    ``docs/real_order_signature.md`` is the log audit that clears those.
    """
    if str(trade.get("source") or "") == "real":
        return True
    if "entry_order_seq" in trade or "exit_order_seq" in trade:
        return bool(str(trade.get("entry_order_seq") or "").strip()
                    or str(trade.get("exit_order_seq") or "").strip())
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
    problems, replaced = [], []
    confirmed = str(trade.get("source") or "") == "real"
    side = str(trade.get("side") or "").upper()
    if side not in ("LONG", "SHORT"):
        problems.append(f"unknown side {side or '(none)'}")
    if real_entry:
        why = _price_check(real_entry, sim_entry,
                           _parse(trade.get("real_entry_dt")) or _parse(trade.get("entry_dt")))
        if why == PRE_92 and confirmed:
            real_entry = 0
            replaced.append("entry")
        elif why:
            problems.append(f"entry: {why}")
    if real_exit:
        why = _price_check(real_exit, sim_exit,
                           _parse(trade.get("real_exit_dt")) or _parse(trade.get("exit_dt")))
        if why == PRE_92 and confirmed:
            real_exit = 0
            replaced.append("exit")
        elif why:
            problems.append(f"exit: {why}")
    if problems:
        out.update(status="unreliable", reason="; ".join(problems))
        return out
    entry = real_entry or sim_entry
    exit_ = real_exit or sim_exit
    sign = 1 if side == "LONG" else -1
    qty = _int(trade.get("qty")) or 1
    real_pnl = (exit_ - entry) * sign * qty * point_value
    out.update(status="real" if real_entry and real_exit else "estimated",
               real_pnl=real_pnl, slippage=real_pnl - _int(trade.get("pnl")))
    if replaced:
        out["reason"] = (f"{' and '.join(replaced)} price recorded before #92 "
                         "(2026-07-26); sim price used")
    elif out["status"] == "estimated":
        if not real_exit and trade.get("real_exit_estimated"):
            out["reason"] = ("exit fill not captured; sim price kept (est.)")
        else:
            out["reason"] = ("exit fill not recorded (the position may still be open); sim price used"
                             if not real_exit else "entry fill not recorded; sim price used")
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
    # Today's figure is also published real-ONLY. "estimated" substitutes the
    # simulated price for the side that was never recorded, which is fine in a
    # running total but misreads as "what the account made today" on a day
    # whose handful of trades are mostly estimates.
    today_real = [(t, c) for t, c in today if c["status"] == "real"]
    return {
        "pnl_total": sum(c["real_pnl"] for _, c in counted),
        "pnl_today": sum(c["real_pnl"] for _, c in today),
        "pnl_today_real": sum(c["real_pnl"] for _, c in today_real),
        "trades_today_real": len(today_real),
        "sim_pnl_same_trades": sum(_int(t.get("pnl")) for t, _ in counted),
        "slippage_total": sum(c["slippage"] for _, c in counted),
        "trades": len(counted),
        "trades_today": len(today),
        "estimated": sum(1 for _, c in counted if c["status"] == "estimated"),
        "unreliable": sum(1 for _, c in rows if c["status"] == "unreliable"),
        "missing": sum(1 for _, c in rows if c["status"] == "missing"),
        "trusted_from": REAL_PRICES_TRUSTED_FROM.strftime("%Y-%m-%d"),
    }
