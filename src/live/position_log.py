"""Per-bot position log — data foundation for several real bots per account.

Data collection only: nothing here feeds a trading decision.

The account's OpenInterest book is one NET row per product, so a bot
cannot see its own position in it once another bot (or a manual order)
trades the same product on the same account. This module keeps the bot's
own view — ``bot_position`` — built purely from fills of orders THIS bot
sent, keyed by the seq no ``SendFutureOrderCLR`` returned, and logs it
beside the account net so a divergence is visible:

    data/live/<bot_id>/position_log.jsonl      (append-only, one JSON per line)

The file is an append-only event journal. A fill is written immediately,
before the OpenInterest wait, so a crash cannot drop a known fill. The
account net arrives later as a correlated status, never as a second fill.

Events (new journals)::

    ORDER         accepted order. Restores the in-memory registry only.
                  Replaying it never creates a fill or changes position.
    FILL          one own fill, ``net_status: "pending"``,
                  ``net_position_after: null``. Counted once.
    FILL_REFINE   a later deal price for a fill whose net is still open.
                  Not a fill.
    NET_UPDATE    correlated answer (``answered``), timeout
                  (``unanswered``), or stop flush (``flush_unknown``).
                  Not a fill. Unknown stays null — never 0 and never an
                  average.
    RESUME        explicit restart line. Does not change position.

``read_records`` projects these to the logical record #250 readers use.
Public names are unchanged::

    {"ts", "bot_id", "account", "product", "order_seq", "side": "BUY"|"SELL",
     "qty", "fill_price", "price_source": "deal"|"fill_poller"|"",
     "action": "ENTRY"|"EXIT"|"RESUME",
     "net_position_after": int|null,
     "net_status": "pending"|"answered"|"unanswered"|"flush_unknown",
     "bot_position_after": int}

A line with no ``event`` and ``action`` ENTRY, EXIT, or RESUME is a
finalized pre-change record. It is replayed as-is. Historical files are
never rewritten, and ORDER lines are not invented for them.

Replay rules, applied only by explicit ``resume()`` (a new ledger is flat):

* A plain deploy does not adopt the previous journal. Inspection goes
  through ``read_records``. The live ledger loads orders, pending nets,
  and position only when ``resume()`` runs.
* New ``FILL`` events move position by their signed qty, once per seq.
  ``ORDER`` never does. ``NET_UPDATE`` never does. A new ``RESUME`` line
  does not either.
* A pre-change line (no ``event``) does not move position by its qty.
  The last such line's ``bot_position_after`` is the baseline, which can
  disagree with the signed sum. Its seqs are remembered so a later deal
  does not count them again. No ORDER line is written for them.
* A restored pending fill keeps the generation number on disk, but the
  in-memory comparison uses a sentinel: the counter in
  ``account_monitor`` restarts every process, so the stored number is
  not compared with the new process. The first completed snapshot here
  may answer it. The 120 s timeout clock also restarts with the process.
* A failed append is logged and not treated as durable. In-memory
  position moves only after that append succeeds, so a write error does
  not leave a position the next restart cannot see. Nothing here raises
  into the trading path.

``net_position_after`` describes the book after the fill. A pending fill
takes the first snapshot whose query generation is newer than the one
current at fill time. Timeout and stop write null.
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
_TYPE_BY_ACTION = {"ENTRY": "entry", "EXIT": "exit"}
_TERMINAL = {"answered", "unanswered", "flush_unknown"}
# Restored pending fills must not be compared with the previous process's
# OpenInterest counter. Any completed snapshot in this process is newer.
_RESTORED_OI_GEN = -1
_PUBLIC_KEYS = (
    "ts", "bot_id", "account", "product", "order_seq", "side", "qty",
    "fill_price", "price_source", "action", "net_position_after",
    "net_status", "bot_position_after",
)


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


def _public_record(rec: dict) -> dict:
    """Logical #250 shape. Internal event fields stay on disk only."""
    return {key: rec.get(key) for key in _PUBLIC_KEYS}


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
        self._pending: list[dict] = []            # fills waiting for a net
        self._anon = 0
        self._recovered = False
        self._resume_written = False
        self._legacy_anchor: dict | None = None

    # ── helpers ──

    def _account_no(self) -> str:
        try:
            value = self._account() if callable(self._account) else self._account
        except Exception:  # noqa: BLE001
            value = ""
        return str(value or "")

    def _append_line(self, record: dict) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            return True
        except Exception as exc:  # noqa: BLE001 — logging must not touch trading
            logger.warning("position log write failed (%s): %s", self.path, exc)
            return False

    def _write(self, record: dict) -> bool:
        """Append one event. If that line has no own-position field, append
        the last fill in the shape #250's ``last_record`` already reads.

        The anchor has no ``event`` key and the same order seq as the fill,
        so replay sets the saved position and does not count another lot.
        """
        if not self._append_line(record):
            return False
        anchor = self._legacy_anchor
        if anchor and "bot_position_after" not in record:
            self._append_line(dict(anchor))
        return True

    def _note_anchor(self, public: dict) -> None:
        self._legacy_anchor = {key: public.get(key) for key in _PUBLIC_KEYS}

    def recover(self) -> None:
        """Replay the journal once per ledger.

        A second call does not reset ``_gen`` or the timeout clock of
        fills already loaded in this process, and it does not append.
        ``__init__`` does not call this. The explicit resume path does,
        before it tries to build the optional RESUME line.
        """
        if self._recovered:
            return
        self._recovered = True
        self._recover()

    def _recover(self) -> None:
        """Replay from an empty slate. Does not write. May reset clocks.

        Callers that must not disturb an already-loaded ledger use
        :meth:`recover` instead.
        """
        self.position = 0
        self._orders = {}
        self._applied = set()
        self._last_seq = {}
        self._pending = []
        self._anon = 0
        self._legacy_anchor = None
        try:
            rows = read_raw_records(self.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "position log replay failed (%s): %s", self.path, type(exc).__name__)
            return
        for row in rows:
            try:
                self._replay_one(row)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "position log replay skipped a row (%s): %s",
                    self.path, type(exc).__name__)
        self._bound_orders()

    def _replay_one(self, rec: dict) -> None:
        if _is_legacy_line(rec):
            self._replay_legacy(rec)
            return
        event = rec.get("event")
        if event == "ORDER":
            self._replay_order(rec)
            return
        if event == "FILL_REFINE":
            self._replay_refine(rec)
            return
        if event == "NET_UPDATE":
            # Status only. A net line never creates a fill or moves position,
            # including when the fill line is missing.
            seq = str(rec.get("order_seq") or "").strip()
            if seq:
                self._pending = [p for p in self._pending if p.get("order_seq") != seq]
            return
        if event == "RESUME" or rec.get("action") == "RESUME":
            return
        if event == "FILL":
            self._replay_fill(rec)

    def _replay_legacy(self, rec: dict) -> None:
        """Pre-#252 line: snapshot baseline, seq remembered, qty not summed."""
        action = rec.get("action")
        seq = str(rec.get("order_seq") or "").strip()
        if action in ("ENTRY", "EXIT") and seq:
            self._applied.add(seq)
            side = _side_code(rec.get("side"))
            qty = _int_or_none(rec.get("qty"))
            if qty is None or qty < 1:
                qty = 1
            if side in ("BUY", "SELL") and seq not in self._orders:
                kind = _TYPE_BY_ACTION.get(action, "")
                if kind:
                    self._orders[seq] = {
                        "action": kind, "side": side, "qty": qty,
                        "product": rec.get("product") or "",
                    }
            self._note_anchor(rec)
        pos = _int_or_none(rec.get("bot_position_after"))
        if pos is not None:
            self.position = pos
            if self._legacy_anchor is not None:
                self._legacy_anchor["bot_position_after"] = self.position
            elif action == "RESUME":
                self._note_anchor(rec)

    def _replay_order(self, rec: dict) -> None:
        seq = str(rec.get("order_seq") or "").strip()
        action = rec.get("action_type") or _TYPE_BY_ACTION.get(rec.get("action"), "")
        if not seq or action not in _ACTIONS:
            return
        qty = max(1, _int_or_none(rec.get("qty")) or 1)
        self._orders[seq] = {
            "action": action,
            "side": _side_code(rec.get("side")),
            "qty": qty,
            "product": rec.get("product") or "",
            "oi_generation": _int_or_none(rec.get("oi_generation")),
        }
        self._last_seq[action] = seq

    def _replay_fill(self, rec: dict) -> None:
        seq = str(rec.get("order_seq") or "").strip()
        if seq:
            ident = seq
        else:
            self._anon += 1
            ident = f"#{self._anon}"
        if ident in self._applied:
            return
        side = _side_code(rec.get("side"))
        if side not in ("BUY", "SELL"):
            return
        qty = _int_or_none(rec.get("qty"))
        if qty is None or qty < 1:
            qty = 1
        self.position += qty if side == "BUY" else -qty
        self._applied.add(ident)
        action = rec.get("action_type") or _TYPE_BY_ACTION.get(rec.get("action"), "")
        if seq and seq not in self._orders and action in _ACTIONS:
            # In-memory only, so a still-open deal can refine the price.
            # Nothing is appended: historical journals gain no ORDER line.
            self._orders[seq] = {
                "action": action, "side": side, "qty": qty,
                "product": rec.get("product") or "",
            }
        status = rec.get("net_status")
        self._note_anchor(_public_record(rec))
        if status in _TERMINAL:
            return
        public = _public_record(rec)
        public["side"] = side
        public["qty"] = qty
        public["order_seq"] = seq
        public["net_status"] = status or "pending"
        public["net_position_after"] = None
        self._note_anchor(public)
        # The number on the FILL line stays on disk. This process must not
        # compare it with a counter that started over at zero.
        self._pending.append({
            **public,
            "_gen": _RESTORED_OI_GEN,
            "_at": self._mono(),
        })

    def _replay_refine(self, rec: dict) -> None:
        seq = str(rec.get("order_seq") or "").strip()
        if not seq:
            return
        price = _int_or_none(rec.get("fill_price"))
        source = rec.get("price_source") or "deal"
        for pend in self._pending:
            if pend.get("order_seq") == seq and pend.get("price_source") != "deal":
                pend["fill_price"] = price
                pend["price_source"] = source
                return

    def _bound_orders(self) -> None:
        if len(self._orders) <= 256:
            return
        keep = set(self._last_seq.values())
        keep.update(p.get("order_seq") for p in self._pending)
        for old in list(self._orders):
            if len(self._orders) <= 256:
                break
            if old in keep:
                continue
            self._orders.pop(old, None)

    # ── order / fill events ──

    def register_order(self, seq, action_type: str, side, qty: int, product: str,
                       oi_generation: int | None = None) -> None:
        """An order THIS bot sent was accepted (SendFutureOrderCLR code 0).

        The ORDER line is appended before the registry is updated. A failed
        append leaves the registry unchanged: a later restart must not
        confirm an order that never became durable.
        """
        seq = str(seq or "").strip()
        if not seq or action_type not in _ACTIONS:
            return
        side_code = _side_code(side)
        n = max(1, _int_or_none(qty) or 1)
        generation = _int_or_none(oi_generation)
        event = {
            "event": "ORDER",
            "ts": self._now_iso(),
            "bot_id": self.bot_id,
            "account": self._account_no(),
            "product": product,
            "order_seq": seq,
            "action": _ACTIONS[action_type],
            "action_type": action_type,
            "side": side_code,
            "qty": n,
            "oi_generation": generation,
        }
        if not self._write(event):
            return
        self._orders[seq] = {
            "action": action_type, "side": side_code, "qty": n,
            "product": product, "oi_generation": generation,
        }
        self._last_seq[action_type] = seq
        if len(self._orders) > 256:                       # bound memory
            for old in list(self._orders)[:64]:
                self._orders.pop(old, None)

    def record_fill(self, seq, price, *, source: str, oi_generation: int | None,
                    qty: int | None = None) -> dict | None:
        """Count one fill of our own order. Each seq counts once.

        The fill line is appended before position moves. A deal row arriving
        after the poller fallback already counted the seq only upgrades a
        still-pending record's price, and only after that upgrade is itself
        appended.
        """
        seq = str(seq or "").strip()
        order = self._orders.get(seq)
        if order is None:
            return None                                   # not ours
        if seq in self._applied:
            if source == "deal":
                self._refine_pending(seq, price)
            return None
        n = max(1, _int_or_none(qty) or order["qty"])
        new_pos = self.position + (n if order["side"] == "BUY" else -n)
        rec = {
            "ts": self._now_iso(), "bot_id": self.bot_id, "account": self._account_no(),
            "product": order["product"], "order_seq": seq, "side": order["side"],
            "qty": n, "fill_price": _int_or_none(price), "price_source": source,
            "action": _ACTIONS[order["action"]],
            "net_position_after": None, "net_status": "pending",
            "bot_position_after": new_pos,
        }
        generation = _int_or_none(oi_generation)
        if not self._write({**rec, "event": "FILL", "oi_generation": generation}):
            return None
        self.position = new_pos
        self._applied.add(seq)
        self._pending.append({**rec, "_gen": generation, "_at": self._mono()})
        self._note_anchor(rec)
        return rec

    def _refine_pending(self, seq: str, price) -> None:
        price_i = _int_or_none(price)
        for rec in self._pending:
            if rec["order_seq"] != seq or rec["price_source"] == "deal":
                continue
            event = {
                "event": "FILL_REFINE",
                "ts": self._now_iso(),
                "bot_id": self.bot_id,
                "account": self._account_no(),
                "order_seq": seq,
                "fill_price": price_i,
                "price_source": "deal",
            }
            if not self._write(event):
                return
            rec["fill_price"] = price_i
            rec["price_source"] = "deal"
            if self._legacy_anchor and self._legacy_anchor.get("order_seq") == seq:
                self._legacy_anchor["fill_price"] = price_i
                self._legacy_anchor["price_source"] = "deal"
            return

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
        """A snapshot completed. Settle fills whose query generation is older.

        The account net never creates a fill. A failed status append leaves
        the fill pending so a later snapshot can try again.
        """
        if generation is None or not self._pending:
            return 0
        written, keep = 0, []
        for rec in self._pending:
            gen = rec.get("_gen")
            if gen is not None and generation > gen:
                if self._write(self._net_event(rec, net, "answered")):
                    written += 1
                else:
                    keep.append(rec)
            else:
                keep.append(rec)
        self._pending = keep
        return written

    def flush_expired(self) -> int:
        now = self._mono()
        expired = [r for r in self._pending if now - r["_at"] >= self._timeout]
        keep = [r for r in self._pending if now - r["_at"] < self._timeout]
        written, still = self._settle(expired, None, "unanswered")
        self._pending = keep + still
        return written

    def flush_all(self) -> int:
        pending, self._pending = self._pending, []
        written, still = self._settle(pending, None, "flush_unknown")
        self._pending = still
        return written

    def _settle(self, pending: list[dict], net, status: str) -> tuple[int, list]:
        written, keep = 0, []
        for rec in pending:
            if self._write(self._net_event(rec, net, status)):
                written += 1
            else:
                keep.append(rec)
        return written, keep

    def _net_event(self, pending: dict, net, status: str) -> dict:
        rec = {k: v for k, v in pending.items() if not str(k).startswith("_")}
        rec["event"] = "NET_UPDATE"
        # null stays null. A real flat book is the integer 0, not a guess.
        rec["net_position_after"] = None if net is None else _int_or_none(net)
        rec["net_status"] = status
        rec["net_ts"] = self._now_iso()
        return rec

    # ── resume ──

    def resume(self, *, net: int | None, product: str, fill_price=None) -> dict:
        """Replay once, then append at most one RESUME line.

        The line is display only. It does not count a fill, and the
        account net does not replace the restored position. A second call
        does not replay and does not append another line.
        """
        self.recover()
        if self._resume_written:
            return dict(self._resume_record)
        rec = {
            "ts": self._now_iso(), "bot_id": self.bot_id, "account": self._account_no(),
            "product": product, "order_seq": "", "side": "", "qty": 0,
            "fill_price": _int_or_none(fill_price), "price_source": "",
            "action": "RESUME", "net_position_after": net,
            "net_status": "answered" if net is not None else "unanswered",
            "bot_position_after": self.position,
        }
        if self._write({**rec, "event": "RESUME"}):
            self._resume_written = True
            self._resume_record = dict(rec)
        return rec


# ── reading ────────────────────────────────────────────────────────────

def read_raw_records(path: str, *, max_bytes: int | None = None,
                     opener: Callable | None = None) -> list[dict]:
    """Journal events oldest-first. Unparseable lines are skipped.

    ``max_bytes`` reads only the tail of a large file. A partial first
    line caused by that seek is dropped. With neither ``max_bytes`` nor
    ``opener``, ledger replay uses a normal text ``open``. ``opener`` is
    ``opener(path) -> binary file``; the dashboard passes the shared
    reader so a Windows handle allows delete.
    """
    try:
        if max_bytes is None and opener is None:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
            truncated = False
        else:
            if opener is None:
                handle = open(path, "rb")
            else:
                handle = opener(path)
            with handle as f:
                if max_bytes is None:
                    blob = f.read()
                    truncated = False
                else:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - max(0, int(max_bytes))))
                    blob = f.read()
                    truncated = size > len(blob)
            lines = blob.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if truncated and lines:
        lines = lines[1:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def project_logical_records(rows: list) -> list[dict]:
    """Last effective #250 record for each own fill, in fill order.

    ORDER lines are not records. NET_UPDATE and FILL_REFINE change the
    matching fill's status or price. A net line with no fill in this
    window is shown as that fill's snapshot (the tail may start mid-journal)
    and is never used to invent a position from the account net. A finalized
    legacy line, which has no ``event``, is already one logical record.
    """
    logical: list[dict] = []
    index: dict[str, int] = {}
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        event = rec.get("event")
        action = rec.get("action")
        seq = str(rec.get("order_seq") or "").strip()
        if event == "ORDER":
            continue
        if event == "FILL_REFINE":
            if seq in index:
                slot = logical[index[seq]]
                if rec.get("fill_price") is not None:
                    slot["fill_price"] = _int_or_none(rec.get("fill_price"))
                if rec.get("price_source"):
                    slot["price_source"] = rec.get("price_source")
            continue
        if event == "NET_UPDATE":
            if seq in index:
                slot = logical[index[seq]]
                # Own position stays the fill's. The net line cannot replace it.
                slot["net_position_after"] = rec.get("net_position_after")
                slot["net_status"] = rec.get("net_status")
                if rec.get("fill_price") is not None:
                    slot["fill_price"] = rec.get("fill_price")
                if rec.get("price_source"):
                    slot["price_source"] = rec.get("price_source")
            elif action in ("ENTRY", "EXIT"):
                pub = _public_record(rec)
                if seq:
                    index[seq] = len(logical)
                logical.append(pub)
            continue
        if event == "RESUME" or action == "RESUME":
            logical.append(_public_record(rec))
            continue
        if event == "FILL" or (event in (None, "") and action in ("ENTRY", "EXIT")):
            if seq and seq in index:
                continue
            pub = _public_record(rec)
            if seq:
                index[seq] = len(logical)
            logical.append(pub)
    return logical


def read_records(path: str, limit: int | None = None, *,
                 max_bytes: int | None = None,
                 opener: Callable | None = None) -> list[dict]:
    """Logical records oldest-first.

    Every event in the read window is projected first. ``limit`` then
    keeps the last that many logical rows. It is not a count of raw
    journal lines, so trailing ORDER events do not hide an earlier fill.
    ``max_bytes`` bounds a large file to its tail; ``None`` reads it all.
    """
    logical = project_logical_records(
        read_raw_records(path, max_bytes=max_bytes, opener=opener))
    return logical[-limit:] if limit else logical


def last_record(path: str) -> dict | None:
    recs = read_records(path, limit=1)
    return recs[-1] if recs else None


def _is_legacy_line(rec: dict) -> bool:
    """A finalized pre-#252 record: no event type, action ENTRY/EXIT/RESUME."""
    if not isinstance(rec, dict) or rec.get("event") not in (None, ""):
        return False
    return rec.get("action") in ("ENTRY", "EXIT", "RESUME")


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
