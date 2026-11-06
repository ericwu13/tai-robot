"""Per-bot position log — data foundation for several real bots per account.

Data collection only: nothing here feeds a trading decision.

The account's OpenInterest book is one NET row per product, so a bot
cannot see its own position in it once another bot (or a manual order)
trades the same product on the same account. This module keeps the bot's
own view — ``bot_position`` — built purely from fills of orders THIS bot
sent, keyed by the seq no ``SendFutureOrderCLR`` returned, and logs it
beside the account net so a divergence is visible:

    data/live/<bot_id>/position_log.jsonl      (append-only, one JSON per line)

Record::

    {"ts", "bot_id", "account", "product", "order_seq", "side": "BUY"|"SELL",
     "qty", "fill_price", "price_source": "deal"|"fill_poller"|"",
     "action": "ENTRY"|"EXIT"|"RESUME",
     "net_position_after": int|null,   # account net, this product, AFTER the fill
     "net_status": "answered"|"unanswered",
     "bot_position_after": int}

``net_position_after`` must describe the book after the fill, so a fill
record waits for the first OpenInterest snapshot whose query STARTED after
the fill (query generation newer than the one current at fill time) and
completed (``end`` / ``flat``). If none completes within ``net_timeout_s``,
or the bot stops first, the record is written with ``null`` and
``net_status: "unanswered"`` — never a pre-fill net presented as post-fill.

Every public method is safe to call from the trading path: failures are
logged and swallowed.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

logger = logging.getLogger(__name__)

POSITION_LOG_NAME = "position_log.jsonl"
NET_TIMEOUT_S = 120.0
_TPE = timezone(timedelta(hours=8))
_ACTIONS = {"entry": "ENTRY", "exit": "EXIT"}


def _now_iso() -> str:
    return datetime.now(_TPE).isoformat(timespec="seconds")


def _side_code(side) -> str:
    """BUY/SELL from the order's buy_sell (0/1) or a report side (B/S)."""
    if side in (0, "0", "B", "BUY", "buy"):
        return "BUY"
    if side in (1, "1", "S", "SELL", "sell"):
        return "SELL"
    return ""


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class PositionLedger:
    """One bot's own position from its own order seq numbers."""

    def __init__(self, bot_dir: str, *, bot_id: str,
                 account: Callable[[], str] | str = "",
                 net_timeout_s: float = NET_TIMEOUT_S,
                 now_iso: Callable[[], str] = _now_iso,
                 monotonic: Callable[[], float] = time.monotonic):
        self.path = os.path.join(bot_dir, POSITION_LOG_NAME)
        self.bot_id = bot_id
        self._account = account
        self._timeout = net_timeout_s
        self._now_iso = now_iso
        self._mono = monotonic
        self.position = 0
        self._orders: dict[str, dict] = {}       # seq -> sent order
        self._applied: set[str] = set()           # seqs already counted
        self._last_seq: dict[str, str] = {}       # action_type -> newest seq
        self._pending: list[dict] = []            # records waiting for a net

    # ── helpers ──

    def _account_no(self) -> str:
        try:
            value = self._account() if callable(self._account) else self._account
        except Exception:  # noqa: BLE001
            value = ""
        return str(value or "")

    def _write(self, record: dict) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            return True
        except Exception as exc:  # noqa: BLE001 — logging must not touch trading
            logger.warning("position log write failed (%s): %s", self.path, exc)
            return False

    # ── order / fill events ──

    def register_order(self, seq, action_type: str, side, qty: int, product: str) -> None:
        """An order THIS bot sent was accepted (SendFutureOrderCLR code 0)."""
        seq = str(seq or "").strip()
        if not seq or action_type not in _ACTIONS:
            return
        self._orders[seq] = {"action": action_type, "side": _side_code(side),
                             "qty": max(1, _int_or_none(qty) or 1), "product": product}
        self._last_seq[action_type] = seq
        if len(self._orders) > 256:                       # bound memory
            for old in list(self._orders)[:64]:
                self._orders.pop(old, None)

    def record_fill(self, seq, price, *, source: str, oi_generation: int | None,
                    qty: int | None = None) -> dict | None:
        """Count one fill of our own order. Each seq counts once.

        A deal row arriving after the poller fallback already counted the
        seq only upgrades a still-pending record's price.
        """
        seq = str(seq or "").strip()
        order = self._orders.get(seq)
        if order is None:
            return None                                   # not ours
        if seq in self._applied:
            if source == "deal":
                for rec in self._pending:
                    if rec["order_seq"] == seq and rec["price_source"] != "deal":
                        rec["fill_price"] = _int_or_none(price)
                        rec["price_source"] = "deal"
            return None
        self._applied.add(seq)
        n = max(1, _int_or_none(qty) or order["qty"])
        self.position += n if order["side"] == "BUY" else -n
        rec = {
            "ts": self._now_iso(), "bot_id": self.bot_id, "account": self._account_no(),
            "product": order["product"], "order_seq": seq, "side": order["side"],
            "qty": n, "fill_price": _int_or_none(price), "price_source": source,
            "action": _ACTIONS[order["action"]],
            "net_position_after": None, "net_status": "unanswered",
            "bot_position_after": self.position,
        }
        self._pending.append({**rec, "_gen": oi_generation, "_at": self._mono()})
        return rec

    def confirm_latest(self, action_type: str, price, *, source: str,
                       oi_generation: int | None) -> dict | None:
        """Fill confirmed without a deal row (OpenInterest path): count the
        newest order of that action if its deal row has not counted it."""
        seq = self._last_seq.get(action_type, "")
        if not seq or seq in self._applied:
            return None
        return self.record_fill(seq, price, source=source, oi_generation=oi_generation)

    # ── net position ──

    def on_oi_complete(self, generation: int | None, net: int) -> int:
        """A snapshot completed. Write records whose fill predates its query."""
        if generation is None or not self._pending:
            return 0
        written, keep = 0, []
        for rec in self._pending:
            gen = rec["_gen"]
            if gen is not None and generation > gen:
                written += self._emit(rec, net)
            else:
                keep.append(rec)
        self._pending = keep
        return written

    def flush_expired(self) -> int:
        now = self._mono()
        expired = [r for r in self._pending if now - r["_at"] >= self._timeout]
        self._pending = [r for r in self._pending if now - r["_at"] < self._timeout]
        return sum(self._emit(r, None) for r in expired)

    def flush_all(self) -> int:
        pending, self._pending = self._pending, []
        return sum(self._emit(r, None) for r in pending)

    def _emit(self, pending: dict, net) -> int:
        rec = {k: v for k, v in pending.items() if not k.startswith("_")}
        rec["net_position_after"] = net
        rec["net_status"] = "answered" if net is not None else "unanswered"
        return 1 if self._write(rec) else 0

    # ── resume ──

    def resume(self, *, net: int | None, product: str, fill_price=None) -> dict:
        """Clean restart: restore ``bot_position`` from the log's last record
        and log a RESUME line with the account net seen at startup."""
        last = last_record(self.path)
        self.position = (_int_or_none(last.get("bot_position_after")) or 0) if last else 0
        rec = {
            "ts": self._now_iso(), "bot_id": self.bot_id, "account": self._account_no(),
            "product": product, "order_seq": "", "side": "", "qty": 0,
            "fill_price": _int_or_none(fill_price), "price_source": "",
            "action": "RESUME", "net_position_after": net,
            "net_status": "answered" if net is not None else "unanswered",
            "bot_position_after": self.position,
        }
        self._write(rec)
        return rec


# ── reading ────────────────────────────────────────────────────────────

def read_records(path: str, limit: int | None = None) -> list[dict]:
    """Records oldest-first; unparseable lines are skipped."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out[-limit:] if limit else out


def last_record(path: str) -> dict | None:
    recs = read_records(path, limit=1)
    return recs[-1] if recs else None


def is_mismatch(rec: dict) -> bool:
    """The bot's own position disagrees with the account net after a fill.

    Only answered nets count; a single bot alone on its product should
    always match, so any mismatch is another order source (a second bot,
    a manual order) or a missed fill.
    """
    net = rec.get("net_position_after")
    if net is None:
        return False
    return _int_or_none(net) != _int_or_none(rec.get("bot_position_after"))
