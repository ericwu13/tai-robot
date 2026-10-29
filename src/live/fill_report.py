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

GetFulfillReport(1) is the backup when no deal row arrives (issue #218).
Those rows have no order seq. The accept row's book number is the
primary key; otherwise the match is product family, side, new/close,
qty 1, and a fill time in ``[send-2s, send+90s]``.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.market_data.kline_config import SYMBOL_CONFIG, get_near_month_symbol
from src.utils.log_redact import scrub_ids

# OnNewData comma-field indices (Capital API 2.13.57, futures TF).
# Source: the SDK's own example, PythonExample/Reply_Service/Reply.py:
#   [0]  委託序號 KeyNo — 13-digit seq, matches SendFutureOrderCLR's return
#        *** Type-D (deal/fill) rows have EMPTY field[0]; the seq is at
#        field[-1] (last field, index 47 in a 48-field row). parse_new_data
#        falls back to field[-1] when field[0] is empty on D rows. ***
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
    seq = fields[_IDX_SEQ].strip()
    row_type = fields[_IDX_TYPE].strip()
    if not seq and row_type == "D":
        seq = fields[-1].strip()
    return NewDataRow(
        seq_no=seq,
        row_type=row_type,
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
        # Report rows already written onto a trade. A later exit must not
        # be credited with the same GetFulfillReport row (issue #218).
        self._credited: set[tuple] = set()
        self._credited_order: list[tuple] = []

    def reset(self) -> None:
        self._orders.clear()
        self._credited.clear()
        self._credited_order.clear()

    def register_order(self, seq_no, action_type: str,
                       bar_index: int, sim_price: int, *,
                       product: str = "", side: str = "",
                       new_close: str = "", send_time=None,
                       qty: int = 1) -> None:
        """Track an accepted real order (call on SendFutureOrderCLR code==0).

        ``product`` is the symbol we sent (``TM0000`` / ``TMF00``). ``side``
        is ``B`` or ``S``. ``new_close`` is the report letter the fill will
        carry (``N`` new, ``O`` offset) — not the integer ``sNewClose``.
        ``send_time`` is when the order was accepted. The book number is
        not known yet; an OnNewData accept row fills it in.
        """
        seq_no = str(seq_no).strip()
        if not seq_no:
            return
        self._orders[seq_no] = {
            "action_type": action_type,
            "bar_index": bar_index,
            "sim_price": int(sim_price),
            "fill": None,
            "book_no": "",
            "product": str(product or "").strip(),
            "side": str(side or "").strip().upper(),
            "new_close": str(new_close or "").strip().upper(),
            "send_time": send_time,
            "qty": int(qty or 1),
        }
        while len(self._orders) > self._MAX_ORDERS:
            self._orders.popitem(last=False)

    def book_for(self, seq_no) -> str:
        """Book number captured from the accept row, or ""."""
        order = self._orders.get(str(seq_no).strip())
        return order.get("book_no", "") if order else ""

    def note_credited(self, row_key) -> None:
        """Remember a report row that was written onto a trade."""
        if row_key is None or row_key in self._credited:
            return
        self._credited.add(row_key)
        self._credited_order.append(row_key)
        while len(self._credited_order) > 64:
            old = self._credited_order.pop(0)
            self._credited.discard(old)

    def on_new_data(self, raw) -> TrackedFill | None:
        """Feed one OnNewData row; returns the fill if it's ours and new.

        An accept row (type ``N``) for one of our seqs stores field ``[10]``
        (the book number) on that order. Deal rows are matched by seq as
        before. Duplicate deal rows for an already-filled order return None
        (all orders are 1-lot, so the first deal row is the whole fill).
        """
        row = parse_new_data(raw)
        if row is None:
            return None
        order = self._orders.get(row.seq_no)
        # Issue #218: the accept row carries the book number OnNewData
        # uses at field [10]. Headless sessions may never deliver this
        # row; the report fallback does not need it.
        if (order is not None and row.row_type == "N"
                and row.book_no and not order.get("book_no")):
            order["book_no"] = row.book_no
        if not row.is_fill:
            return None
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

    def match_report(self, seq_no, rows) -> FillMatch:
        """Match a GetFulfillReport(1) parse against one registered order.

        Rows already credited to an earlier trade are excluded. The
        caller credits the returned key only after the price is written.
        """
        order = self._orders.get(str(seq_no).strip())
        if order is None:
            return FillMatch(0, "none")
        return match_order_report(order, rows, credited=self._credited)


# ── GetFulfillReport(1) per-fill detail (issue #218) ──
#
# Format 1 = "全部". Each physical fill is one CSV row. The layout is
# NOT the format-4 layout (and these rows carry no 13-digit
# SendFutureOrderCLR seq). Indices confirmed from production rows:
#   [7]  short book number (委託書號 — the same value OnNewData field
#        [10] carries on the accept row)
#   [8]  long book number
#   [9]  date YYYYMMDD
#   [10] time HHMMSS
#   [12] product, the front-month contract (TMFK6), not the order
#        symbol (TM0000 / TMF00)
#   [21] side B/S
#   [25] price
#   [26] qty
#   [27] new/close N=new, O=offset
# A row shorter than _FR_MIN_FIELDS is ignored. That rejects the old
# synthetic 24-field fixture, whose indices were invented.

_FR_IDX_BOOK = 7
_FR_IDX_BOOK_LONG = 8
_FR_IDX_DATE = 9
_FR_IDX_TIME = 10
_FR_IDX_PRODUCT = 12
_FR_IDX_SIDE = 21
_FR_IDX_PRICE = 25
_FR_IDX_QTY = 26
_FR_IDX_NC = 27
_FR_MIN_FIELDS = 28

# Fallback window around the order's send time. The broker stamps the
# fill at second resolution, and two of the #218 exits landed on the
# second after send, so the window opens 2s before send.
FALLBACK_BEFORE_S = 2
FALLBACK_AFTER_S = 90

# GetFulfillReport refuses a second query inside 5s (M999). Polls stay
# 5.5s apart. The chain gives up once the next poll would start after
# about 90s, or after this many counted attempts (M999 does not count).
FR_MIN_GAP_MS = 5500
FR_RETRY_WINDOW_MS = 90_000
FR_MAX_ATTEMPTS = 16

_MONTH_LETTERS = set("ABCDEFGHIJKL")


@dataclass
class FulfillReportRow:
    """One parsed GetFulfillReport(1) row. No order seq — these rows
    do not carry one."""
    book_no: str
    book_long: str
    date: str
    time: str
    product: str
    side: str
    price: float
    qty: int
    new_close: str


@dataclass(frozen=True)
class FillQuery:
    """What we know about the order we are looking for.

    ``book_no`` is empty when OnNewData never delivered an accept row
    (the headless case). ``product`` is the symbol we sent.
    """
    book_no: str = ""
    product: str = ""
    side: str = ""
    new_close: str = ""
    send_time: datetime | None = None
    qty: int = 1


@dataclass(frozen=True)
class FillMatch:
    """``price`` 0 means nothing was assigned. ``reason`` is
    ``book``, ``fallback``, ``none``, or ``ambiguous``."""
    price: int
    reason: str
    row_key: tuple | None = None
    candidates: int = 0


def is_fulfill_rate_limited(raw: str) -> bool:
    """True when the broker answered M999 (5s query gap not elapsed)."""
    if not raw or not isinstance(raw, str):
        return False
    for line in raw.splitlines():
        if line.strip().startswith("M999"):
            return True
    return False


def parse_fulfill_report(raw: str) -> list[FulfillReportRow]:
    """Parse a GetFulfillReport(1) result into individual rows.

    Skips blank lines, ``001`` / ``##`` error lines, and ``M999``
    rate-limit lines. A line with fewer than ``_FR_MIN_FIELDS`` fields
    is dropped.
    """
    if not raw or not isinstance(raw, str):
        return []
    results: list[FulfillReportRow] = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("001") or line.startswith("##"):
            continue
        if line.startswith("M999"):
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
            book_no=fields[_FR_IDX_BOOK].strip(),
            book_long=fields[_FR_IDX_BOOK_LONG].strip(),
            date=fields[_FR_IDX_DATE].strip(),
            time=fields[_FR_IDX_TIME].strip(),
            product=fields[_FR_IDX_PRODUCT].strip(),
            side=fields[_FR_IDX_SIDE].strip().upper(),
            price=price,
            qty=qty,
            new_close=fields[_FR_IDX_NC].strip().upper(),
        ))
    return results


def report_row_key(row: FulfillReportRow) -> tuple:
    """Identity of one fill, so a later trade can exclude it."""
    price = int(round(row.price)) if row.price else 0
    return (row.book_no, row.book_long, row.date, row.time,
            row.product, row.side, row.new_close, row.qty, price)


def product_family(symbol: str) -> str:
    """Commodity family of an order symbol or a contract code.

    ``TM0000`` and ``TMF00`` are the micro-TAIEX order/quote symbols
    (family ``TMF``). ``TMFK6`` is that family's November 2026 contract.
    A contract is ``{family}{month letter}{year digit}``.
    """
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    for key, cfg in SYMBOL_CONFIG.items():
        order_sym = str(cfg.get("order_symbol") or "").upper()
        if s == key.upper() or (order_sym and s == order_sym):
            fam = str(cfg.get("taifex_id") or "").upper()
            if fam:
                return fam
    if (len(s) >= 3 and s[-2] in _MONTH_LETTERS and s[-1].isdigit()):
        return s[:-2]
    return s


def front_month_contract(family: str, when: datetime | None) -> str:
    """Front-month contract for ``family`` at ``when``, or ""."""
    if not family or when is None:
        return ""
    try:
        return get_near_month_symbol(family, when).upper()
    except Exception:
        return ""


def _row_dt(row: FulfillReportRow) -> datetime | None:
    date = row.date.strip()
    clock = row.time.strip()
    if len(date) != 8 or not clock.isdigit():
        return None
    clock = clock.zfill(6)
    if len(clock) < 6:
        return None
    try:
        return datetime.strptime(date + clock[:6], "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _in_send_window(row: FulfillReportRow, send_time: datetime | None) -> bool:
    """True when the fill stamp is in ``[send-2s, send+90s]``.

    Both ends are whole seconds: the broker stamp is HHMMSS, and send
    time is truncated to the second before the window is applied.
    """
    if send_time is None:
        return False
    row_dt = _row_dt(row)
    if row_dt is None:
        return False
    send = send_time.replace(tzinfo=None) if send_time.tzinfo else send_time
    send_s = send.replace(microsecond=0)
    lower = send_s - timedelta(seconds=FALLBACK_BEFORE_S)
    upper = send_s + timedelta(seconds=FALLBACK_AFTER_S)
    return lower <= row_dt <= upper


def _book_equal(row: FulfillReportRow, book: str) -> bool:
    book = book.strip()
    if not book:
        return False
    return book == row.book_no or book == row.book_long


def _fallback_ok(row: FulfillReportRow, query: FillQuery) -> bool:
    if row.qty != 1:
        return False
    if query.side and row.side != query.side.strip().upper():
        return False
    if query.new_close and row.new_close != query.new_close.strip().upper():
        return False
    if not _in_send_window(row, query.send_time):
        return False
    family = product_family(query.product)
    if not family or product_family(row.product) != family:
        return False
    return True


def match_fill_in_report(rows: list[FulfillReportRow],
                         query: FillQuery, *,
                         credited: set | None = None) -> FillMatch:
    """Price of our fill, or 0 when nothing may be assigned.

    Primary key: book-number equality with the accept-row book number.
    Fallback (no book, or the book is not in the report): same product
    family, side, new/close, qty 1, and a fill time in
    ``[send-2s, send+90s]``. The fallback also requires the contract to
    be the front month, and only when exactly one month of the family
    is in that set. Two months, or two rows, assign nothing.

    Rows in ``credited`` were already written onto an earlier trade.
    """
    credited = credited or set()
    usable = [r for r in rows
              if r.price > 0 and report_row_key(r) not in credited]

    book = (query.book_no or "").strip()
    if book:
        hits = [r for r in usable if _book_equal(r, book)]
        if len(hits) == 1:
            row = hits[0]
            return FillMatch(int(round(row.price)), "book",
                             report_row_key(row), 1)
        if len(hits) > 1:
            return FillMatch(0, "ambiguous", None, len(hits))

    candidates = [r for r in usable if _fallback_ok(r, query)]
    if not candidates:
        return FillMatch(0, "none", None, 0)
    months = {r.product.strip().upper() for r in candidates}
    if len(months) != 1:
        return FillMatch(0, "ambiguous", None, len(candidates))
    family = product_family(query.product)
    front = front_month_contract(family, query.send_time)
    only = next(iter(months))
    if front and only != front:
        return FillMatch(0, "none", None, len(candidates))
    if len(candidates) != 1:
        return FillMatch(0, "ambiguous", None, len(candidates))
    row = candidates[0]
    return FillMatch(int(round(row.price)), "fallback",
                     report_row_key(row), 1)


def next_exit_fill_retry(attempt: int, elapsed_ms: int, *,
                         rate_limited: bool, matched: bool
                         ) -> tuple[int | None, int, bool]:
    """``(delay_ms, attempt, exhausted)`` after one exit-fill poll.

    ``delay_ms`` is None when the chain stops. ``exhausted`` is True
    only when it stops without a fill. A rate-limited poll (M999) does
    not consume an attempt; the next poll is still ``FR_MIN_GAP_MS``
    later. No poll is scheduled that would start after
    ``FR_RETRY_WINDOW_MS`` from the retry start. ``elapsed_ms`` is how
    long the chain has already been running.
    """
    if matched:
        return None, attempt, False
    new_attempt = attempt if rate_limited else attempt + 1
    if new_attempt >= FR_MAX_ATTEMPTS:
        return None, new_attempt, True
    delay = FR_MIN_GAP_MS
    if elapsed_ms + delay > FR_RETRY_WINDOW_MS:
        return None, new_attempt, True
    return delay, new_attempt, False


def mask_new_data_row(raw: str, account: str = "") -> str:
    """Mask account and L/F-prefixed ids in a raw OnNewData line.

    The seq and the price stay so a missed exit deal can be diagnosed.
    """
    if not raw or not isinstance(raw, str):
        return ""
    text = raw
    acct = (account or "").strip()
    if acct:
        text = text.replace(acct, "***")
    return scrub_ids(text)


def match_order_report(order: dict, rows: list[FulfillReportRow], *,
                       credited: set | None = None) -> FillMatch:
    """Match ``rows`` against one tracked order dict."""
    send = order.get("send_time")
    query = FillQuery(
        book_no=str(order.get("book_no") or ""),
        product=str(order.get("product") or ""),
        side=str(order.get("side") or ""),
        new_close=str(order.get("new_close") or ""),
        send_time=send,
        qty=int(order.get("qty") or 1),
    )
    return match_fill_in_report(rows, query, credited=credited)
