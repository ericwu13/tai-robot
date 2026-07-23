"""Real-time per-order fill tracking from SKReplyLib.OnNewData (issue #92).

OnNewData is the ONLY per-fill price source in the Capital API:

- ``GetFulfillReport(..., 4)`` ("合併同商品") merges all fills of the same
  product+side into one row whose price is the DAY-CUMULATIVE AVERAGE.
  It equals the actual fill price only for the first fill of each side per
  day — using it as a fill-price source is what produced the wrong exit
  prices in issue #92 (Discord showed 44,794 = yesterday's average while
  the bill showed 44,622).
- OpenInterest avg_cost only exists while a position is open, so it can
  confirm entries but never exits.
- OnNewData type-"D" (deal) rows carry the actual fill price and qty for
  ONE order, keyed by the 13-digit seq no that ``SendFutureOrderCLR``
  returned — unambiguous even with several bots on the same account.

``RealFillTracker`` correlates the two: the GUI registers each accepted
real order's seq no, then feeds every OnNewData row through
``on_new_data()``; matching deal rows come back as ``TrackedFill`` with
the order's registered context (action type, bar-index snapshot for the
broker's guarded writes, sim price for the Discord correction message).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

# OnNewData comma-field indices (Capital API 2.13.57, futures TF).
# Source: the SDK's own example, PythonExample/Reply_Service/Reply.py:
#   [0]  委託序號 KeyNo — 13-digit seq, matches SendFutureOrderCLR's return
#   [2]  委託種類 Type — N=new, C=cancel, U=qty chg, P=price chg, D=deal/fill
#   [3]  委託狀態 OrderErr — "Y" marks an error row
#   [8]  商品代碼 product
#   [10] 委託書號 book no
#   [11] 價格 — the actual fill price on D rows
#   [20] 數量 — the fill qty on D rows
#   [23],[24] date, time
_IDX_SEQ = 0
_IDX_TYPE = 2
_IDX_ERR = 3
_IDX_PRODUCT = 8
_IDX_BOOK = 10
_IDX_PRICE = 11
_IDX_QTY = 20
_IDX_DATE = 23
_IDX_TIME = 24
_MIN_FIELDS = 25


@dataclass
class NewDataRow:
    """One parsed OnNewData report row (any type, not just fills)."""
    seq_no: str
    row_type: str
    order_err: str
    product: str
    book_no: str
    price: float
    qty: int
    date: str
    time: str

    @property
    def is_fill(self) -> bool:
        return self.row_type == "D" and self.order_err != "Y" and self.price > 0


def parse_new_data(raw) -> NewDataRow | None:
    """Parse an OnNewData bstrData string. None if not a report row."""
    if not raw or not isinstance(raw, str):
        return None
    fields = raw.split(",")
    if len(fields) < _MIN_FIELDS:
        return None
    try:
        price = float(fields[_IDX_PRICE].strip() or 0)
    except (ValueError, TypeError):
        price = 0.0
    try:
        qty = int(float(fields[_IDX_QTY].strip() or 0))
    except (ValueError, TypeError):
        qty = 0
    return NewDataRow(
        seq_no=fields[_IDX_SEQ].strip(),
        row_type=fields[_IDX_TYPE].strip(),
        order_err=fields[_IDX_ERR].strip(),
        product=fields[_IDX_PRODUCT].strip(),
        book_no=fields[_IDX_BOOK].strip(),
        price=price,
        qty=qty,
        date=fields[_IDX_DATE].strip(),
        time=fields[_IDX_TIME].strip(),
    )


@dataclass
class TrackedFill:
    """A deal row matched to one of our registered orders."""
    seq_no: str
    action_type: str  # "entry" | "exit"
    price: int        # TAIFEX futures prices are integers
    qty: int
    bar_index: int    # broker snapshot taken at order-send time
    sim_price: int    # simulated price at order-send time
    fill_dt: str      # "YYYYMMDD HH:MM:SS" straight from the row


class RealFillTracker:
    """Match OnNewData deal rows to orders this bot actually sent.

    Rows whose seq no was never registered (another bot or a manual order
    on the same account) are ignored — that isolation is the point.
    """

    _MAX_ORDERS = 32  # bound memory; far above any realistic open-order count

    def __init__(self) -> None:
        self._orders: OrderedDict[str, dict] = OrderedDict()

    def reset(self) -> None:
        self._orders.clear()

    def register_order(self, seq_no, action_type: str,
                       bar_index: int, sim_price: int) -> None:
        """Track an accepted real order (call on SendFutureOrderCLR code==0)."""
        seq_no = str(seq_no).strip()
        if not seq_no:
            return
        self._orders[seq_no] = {
            "action_type": action_type,
            "bar_index": bar_index,
            "sim_price": int(sim_price),
            "fill": None,
        }
        while len(self._orders) > self._MAX_ORDERS:
            self._orders.popitem(last=False)

    def on_new_data(self, raw) -> TrackedFill | None:
        """Feed one OnNewData row; returns the fill if it's ours and new.

        Duplicate deal rows for an already-filled order return None (all
        orders are 1-lot, so the first deal row is the whole fill).
        """
        row = parse_new_data(raw)
        if row is None or not row.is_fill:
            return None
        order = self._orders.get(row.seq_no)
        if order is None:
            return None
        if order["fill"] is not None:
            return None
        fill = TrackedFill(
            seq_no=row.seq_no,
            action_type=order["action_type"],
            price=int(round(row.price)),
            qty=row.qty,
            bar_index=order["bar_index"],
            sim_price=order["sim_price"],
            fill_dt=f"{row.date} {row.time}".strip(),
        )
        order["fill"] = fill
        return fill

    def fill_for(self, seq_no) -> TrackedFill | None:
        """The recorded fill for a seq no, or None."""
        order = self._orders.get(str(seq_no).strip())
        return order["fill"] if order else None


# ── GetFulfillReport(1) per-fill detail parsing ──
#
# Format 1 = "全部" — each physical fill is a separate CSV row.
# Field layout (Capital API 2.13.57, same structure as format 4 but
# not merged): [0]=seq, [15]=side(B/S), [19]=price, [20]=qty,
# [21]=N(new)/O(close), [23]=date.

# Field indices for GetFulfillReport (distinct from OnNewData layout).
_FR_IDX_SEQ = 0
_FR_IDX_SIDE = 15
_FR_IDX_PRICE = 19
_FR_IDX_QTY = 20
_FR_IDX_NC = 21
_FR_IDX_DATE = 23
_FR_MIN_FIELDS = 24


@dataclass
class FulfillReportRow:
    """One parsed GetFulfillReport row."""
    seq_no: str
    side: str
    price: float
    qty: int
    new_close: str
    date: str


def parse_fulfill_report(raw: str) -> list[FulfillReportRow]:
    """Parse a GetFulfillReport(1) result into individual rows.

    Skips error/empty lines (starting with '001' or '##').
    """
    if not raw or not isinstance(raw, str):
        return []
    results: list[FulfillReportRow] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("001") or line.startswith("##"):
            continue
        fields = line.split(",")
        if len(fields) < _FR_MIN_FIELDS:
            continue
        try:
            price = float(fields[_FR_IDX_PRICE].strip() or 0)
        except (ValueError, TypeError):
            price = 0.0
        try:
            qty = int(float(fields[_FR_IDX_QTY].strip() or 0))
        except (ValueError, TypeError):
            qty = 0
        results.append(FulfillReportRow(
            seq_no=fields[_FR_IDX_SEQ].strip(),
            side=fields[_FR_IDX_SIDE].strip(),
            price=price,
            qty=qty,
            new_close=fields[_FR_IDX_NC].strip(),
            date=fields[_FR_IDX_DATE].strip(),
        ))
    return results


def match_fill_in_report(rows: list[FulfillReportRow],
                         target_seq: str) -> int:
    """Find the fill price for a specific order seq no.

    Returns the integer fill price, or 0 if not found / bad data.
    Rejects rows where qty != 1 (aggregated rows from wrong format).
    """
    for row in rows:
        if row.seq_no != target_seq:
            continue
        if row.qty != 1:
            continue
        if row.price <= 0:
            continue
        return int(round(row.price))
    return 0
