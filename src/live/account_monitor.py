"""Account monitoring: position tracking, P&L computation, fill parsing.

GUI-independent — no Tkinter, no COM. Receives parsed data and returns
formatted display values. run_backtest.py handles COM calls and widget
updates.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ── Data classes ──

@dataclass
class AccountDisplay:
    """Formatted account data ready for display."""
    position_text: str = ""
    equity: str = "--"
    available: str = "--"
    float_pnl: str = "--"
    realized_pnl: str = "--"
    fees: str = "--"
    net_pnl: str = "--"
    net_pnl_int: int = 0  # raw int for TradingGuard.update_pnl()
    maint_rate: str = "--"
    valid: bool = False  # True if rights data was successfully parsed


@dataclass
class FillsResult:
    """Parsed fill report."""
    count: int = 0
    entries: list[str] = field(default_factory=list)  # formatted fill strings
    new_entries: list[str] = field(default_factory=list)  # fills not seen before
    # NOTE: display-only. GetFulfillReport(4) merges fills by commodity —
    # its price column is a day-cumulative average, never a fill price
    # (issue #92). Real fill prices come from OnNewData (src/live/fill_report).


# ── Pure parsing functions (moved from run_backtest.py) ──

def open_interest_query_rejected(code) -> bool:
    """True when ``GetOpenInterestGW`` will not deliver a snapshot.

    ``0`` is success. ``None`` means the caller did not receive a code, so
    the deploy path still waits for the callback. Any other value is a
    broker rejection: the position is UNKNOWN and waiting cannot help
    (issue #202).
    """
    if code is None:
        return False
    try:
        return int(code) != 0
    except (TypeError, ValueError):
        return True


def parse_open_interest(bstr: str) -> dict | None:
    """Parse OnOpenInterest callback data into a dict.

    Returns None for error/empty/end-of-data codes (001, 970, 980, ##).
    Fields: product, side(B/S), qty, daytrade_qty, avg_cost, fee, tax_rate
    """
    vals = bstr.split(",")
    if len(vals) < 10 or vals[0].strip() in ("001", "970", "980", "##"):
        return None
    try:
        return {
            "market": vals[0].strip(),      # TF=futures, TO=options
            "product": vals[2].strip(),
            "side": vals[3].strip(),        # B=long, S=short
            "qty": int(vals[4].strip() or 0),
            "daytrade_qty": int(vals[5].strip() or 0),
            "avg_cost": vals[6].strip(),
            "fee": vals[7].strip(),
            "tax_rate": vals[8].strip(),
        }
    except (ValueError, IndexError):
        return None


def parse_future_rights(bstr: str) -> dict | None:
    """Parse OnFutureRights callback data into a dict.

    Returns None for sentinel/error codes (##, 970, 980).
    Key fields: balance, float_pnl, equity, excess_margin, unrealized,
                orig_margin, maint_margin, maint_rate, currency, available
    """
    vals = bstr.split(",")
    if len(vals) < 35 or vals[0].strip() in ("##", "970", "980"):
        return None
    try:
        return {
            "balance": vals[0].strip(),         # 帳戶餘額
            "float_pnl": vals[1].strip(),       # 浮動損益
            "realized_cost": vals[2].strip(),   # 已實現費用
            "tax": vals[3].strip(),             # 交易稅
            "equity": vals[6].strip(),          # 權益數
            "excess_margin": vals[7].strip(),   # 超額保證金
            "realized_pnl": vals[11].strip(),   # 期貨平倉損益
            "unrealized": vals[12].strip(),     # 盤中未實現
            "orig_margin": vals[13].strip(),    # 原始保證金
            "maint_margin": vals[14].strip(),   # 維持保證金
            "maint_rate": vals[24].strip(),     # 維持率
            "currency": vals[25].strip(),       # 幣別
            "available": vals[31].strip(),      # 可用餘額
            "risk": vals[34].strip(),           # 風險指標
        }
    except (ValueError, IndexError):
        return None


# Capital OnOpenInterest field 0. TF = futures, TO = options. A TX option
# (``TO``, product ``TX…``) shares the ``TX`` prefix with TX futures, so a
# prefix check alone cannot tell them apart (issue #145).
FUTURES_OI_MARKET = "TF"


def oi_product_match(row: dict, prefix: str, market: str | None = None) -> bool:
    """True when ``row`` is a non-zero position in this product.

    ``prefix`` is the order-symbol prefix (``TM``, ``TX``, ``MT``).
    ``market`` is the OI market code; pass :data:`FUTURES_OI_MARKET` to
    ignore options and any other book. An empty prefix matches nothing
    (``startswith("")`` would match every product).
    """
    if not prefix or not isinstance(row, dict):
        return False
    product = row.get("product", "") or ""
    if not str(product).startswith(prefix):
        return False
    if market is not None and str(row.get("market", "") or "").strip() != market:
        return False
    return row.get("qty", 0) != 0


def product_signed_position(positions: list[dict], prefix: str,
                            market: str | None = None) -> int:
    """Signed qty of the first non-zero row for this prefix and market.

    Positive = long, negative = short, 0 = flat. Qty=0 rows are skipped
    so a leftover flat line cannot mask a later real row.
    """
    if not prefix:
        return 0
    for row in positions:
        if not oi_product_match(row, prefix, market):
            continue
        qty = row.get("qty", 0) or 0
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            continue
        return qty if row.get("side") == "B" else -qty
    return 0


def close_order_side(
    positions: list[dict],
    prefix: str,
    signed: int,
    last_side: int | None = None,
    *,
    snapshot_received: bool = False,
    market: str | None = None,
    sim_size: int = 0,
    sim_side: str | None = None,
) -> int | None:
    """Buy/sell code that flattens this bot's product, or None to refuse.

    Returns ``0`` (BUY) or ``1`` (SELL). Issue #145.

    A non-zero OI row for this ``prefix`` + ``market`` supplies the side
    (long → SELL, short → BUY). Any other row — a TO option, a different
    prefix, a qty=0 line — is ignored.

    When an OpenInterest snapshot has landed and there is no such row,
    refuse. ``signed == 0`` is that flat book: do not fall through to
    ``last_side`` or the simulated broker. Those fallbacks on a flat
    account used to send a close, and ``sNewClose=2`` (auto) opens a
    naked position. ``signed`` must be this product's qty
    (:func:`product_signed_position`), not a prefix-only scan.

    A matching row wins over a stale ``signed`` of 0, so a qty=0 line
    that an unfiltered scan reported as flat cannot hide a later real row.

    Fallbacks run only when no snapshot has arrived (the book is unknown):
    reverse ``last_side`` (0=BUY, 1=SELL), else reverse the sim side.
    """
    for row in positions or ():
        if oi_product_match(row, prefix, market):
            return 1 if row.get("side") == "B" else 0

    if snapshot_received and signed == 0:
        return None
    if snapshot_received:
        # Answered book, no row for this product. A non-zero ``signed``
        # here was not computed from these rows (e.g. a prefix-only scan
        # that counted an option). Do not invent a side from it.
        return None

    if last_side is not None:
        return 1 - int(last_side)
    if sim_size != 0:
        side = (sim_side or "LONG").strip().upper()
        return 1 if side == "LONG" else 0
    return None


def resume_real_position_ok(signed_pos: int, sim_side: str,
                            snapshot_received: bool) -> tuple[bool, str]:
    """Decide whether a restored SIM position may be confirmed against the
    REAL account on session resume (issue #79 follow-up).

    Args:
        signed_pos: real signed position qty for the symbol prefix
            (positive = long, negative = short, 0 = flat).
        sim_side: the restored broker's ``position_side`` value —
            ``"LONG"`` / ``"SHORT"``.
        snapshot_received: True once an OnOpenInterest snapshot has actually
            landed for the current query. False means we simply do not know
            what the real account holds — an empty position list is NOT
            evidence of a flat account.

    Returns:
        ``(ok, reason_key)`` with ``reason_key`` in
        ``{"match", "flat", "opposite", "unknown"}``. Only ``"match"`` is
        safe to confirm: confirming while the real book holds the OPPOSITE
        side would let the bot auto-send a close (sNewClose=2) in the wrong
        direction, i.e. ADD to the real position instead of closing it.
        ``"unknown"`` also covers an unrecognised ``sim_side`` — direction
        cannot be compared, so it is never confirmed.
    """
    if not snapshot_received:
        return False, "unknown"
    side = (sim_side or "").strip().upper()
    if side not in ("LONG", "SHORT"):
        return False, "unknown"
    if signed_pos == 0:
        return False, "flat"
    if (side == "LONG" and signed_pos > 0) or (side == "SHORT" and signed_pos < 0):
        return True, "match"
    return False, "opposite"


def fmt_money(val: str) -> str:
    """Format a numeric string with comma separators."""
    try:
        n = float(val)
        if n == int(n):
            return f"{int(n):,}"
        return f"{n:,.2f}"
    except (ValueError, TypeError):
        return val


# Monotonic across reset() and across AccountMonitor instances. A row
# tagged for an earlier query must not match a later object's generation 1.
_oi_generation_clock = 0


def _allocate_oi_generation() -> int:
    global _oi_generation_clock
    _oi_generation_clock += 1
    return _oi_generation_clock


# ── AccountMonitor class ──

class AccountMonitor:
    """Tracks real account state: positions, equity, P&L, fills.

    Receives parsed data from COM callbacks. Computes display values.
    No Tkinter, no COM dependencies.
    """

    def __init__(self) -> None:
        self.positions: list[dict] = []
        self.rights: dict = {}
        self._prev_fill_counts: dict[str, int] = {}
        # An empty ``positions`` list means "we don't know yet", NOT "the
        # account is flat" — the callback is asynchronous. Which query a
        # reply answers is the generation below, not a flag ``clear_positions``
        # can reset (issue #202). ``_oi_answered_gen == -1`` means nothing
        # has been answered. ``_oi_saw_001`` is this generation's no-data
        # marker; a ``##`` end marker is an empty book only after it.
        # ``_oi_flat_conflict`` means a position row was already credited
        # and a later ``001`` disagreed. The book stays UNKNOWN.
        self._oi_query_gen: int = _allocate_oi_generation()
        self._oi_answered_gen: int = -1
        self._oi_saw_001: bool = False
        self._oi_flat_conflict: bool = False

    @property
    def oi_query_generation(self) -> int:
        """Generation of the latest query on this monitor."""
        return self._oi_query_gen

    @property
    def oi_snapshot_received(self) -> bool:
        """True only when the latest query has been answered.

        A reply captured under an older generation does not count, and
        ``clear_positions`` does not clear this.
        """
        return self._oi_answered_gen == self._oi_query_gen

    def begin_oi_query(self) -> int:
        """Start a GetOpenInterestGW query and return its generation token.

        Clears the position list so the reply can rebuild it. The previous
        generation's answer stays recorded, but it no longer satisfies
        ``oi_snapshot_received``.
        """
        self._oi_query_gen = _allocate_oi_generation()
        self._oi_saw_001 = False
        self._oi_flat_conflict = False
        self.clear_positions()
        return self._oi_query_gen

    def _accepts_oi_generation(self, generation: int | None) -> bool:
        """Untagged callers credit the current query. A mismatched token does not."""
        return generation is None or generation == self._oi_query_gen

    def _mark_oi_answered(self) -> None:
        self._oi_answered_gen = self._oi_query_gen

    def _mark_oi_unknown(self) -> None:
        """Drop this generation's answer. Rows already stored stay put."""
        if self._oi_answered_gen == self._oi_query_gen:
            self._oi_answered_gen = -1

    def reset(self) -> None:
        """Clear all state."""
        self.positions.clear()
        self.rights.clear()
        self._prev_fill_counts.clear()
        # Full state wipe → the snapshot we held is gone too; back to unknown.
        # The generation clock only moves forward, so a row tagged before
        # this reset cannot match the generation allocated here.
        self._oi_answered_gen = -1
        self._oi_saw_001 = False
        self._oi_flat_conflict = False
        self._oi_query_gen = _allocate_oi_generation()

    # ── Position tracking ──

    def add_position(self, parsed: dict, *, generation: int | None = None) -> bool:
        """Add a parsed open interest entry for ``generation``.

        Returns False when ``generation`` is not the latest query. The
        position list is left unchanged in that case.
        """
        if not self._accepts_oi_generation(generation):
            return False
        self.positions.append(parsed)
        self._mark_oi_answered()
        return True

    def clear_positions(self) -> None:
        """Clear the position list.

        Does not change whether the current query has been answered.
        Starting a new query is ``begin_oi_query`` — a shared flag reset
        here used to drop a reply that had already landed (issue #202).
        """
        self.positions.clear()

    def set_flat(self, *, generation: int | None = None) -> bool:
        """Record an answered empty book (``001``, the no-data marker).

        Returns False when ``generation`` is not the latest query, without
        clearing positions that belong to that newer query.
        """
        if not self._accepts_oi_generation(generation):
            return False
        self.positions.clear()
        self._mark_oi_answered()
        return True

    def apply_open_interest(self, raw: str, *, generation: int | None = None) -> str:
        """Apply one OnOpenInterest row. Credit it only to the latest query.

        Returns ``position``, ``flat``, ``end``, ``stale``, or ``ignored``.

        ``001`` is an answered empty book. The evidenced flat sequence is
        ``001`` then ``##`` (#139). A ``##`` end marker counts as that
        empty book only when this generation already saw ``001`` and no
        position row. A bare ``##``, or ``970``/``980`` followed by ``##``,
        leaves the query unanswered. ``##`` after real rows completes the
        snapshot and does not wipe them. A ``001`` (and the ``##`` that
        follows it) after rows were already credited to this generation
        is a conflict: the rows stay, and the book is UNKNOWN. A
        generation other than the latest query is ``stale`` and changes
        nothing.
        """
        if not self._accepts_oi_generation(generation):
            return "stale"
        text = raw if isinstance(raw, str) else ""
        parsed = parse_open_interest(text)
        if parsed is not None:
            self.add_position(parsed)
            return "position"
        first = text.split(",")[0].strip() if text else ""
        if first == "001":
            if self.positions or self._oi_flat_conflict:
                self._oi_flat_conflict = True
                self._mark_oi_unknown()
                return "ignored"
            self._oi_saw_001 = True
            self.set_flat()
            return "flat"
        if first == "##":
            if self._oi_flat_conflict:
                self._mark_oi_unknown()
                return "ignored"
            if self.positions:
                self._mark_oi_answered()
                return "end"
            if self._oi_saw_001:
                self.set_flat()
                return "flat"
            return "ignored"
        return "ignored"

    def update_rights(self, parsed: dict) -> None:
        """Store parsed future rights data."""
        self.rights = parsed

    def get_signed_position(self, prefix: str, market: str | None = None) -> int:
        """Get signed position qty matching a symbol prefix.

        Positive = long, negative = short, 0 = flat.
        Prefix is typically first 2 chars of order symbol (e.g., "TX", "TM").

        Without ``market``, the first prefix match is returned, including a
        qty=0 row. Resume reconcile and fill-poll depend on that. Pass
        ``market`` (``TF`` for this bot's futures) to skip other markets
        and qty=0 rows — a TX option must not count as a TX future
        (issue #145).
        """
        if not prefix:
            return 0
        if market is not None:
            return product_signed_position(self.positions, prefix, market)
        for p in self.positions:
            if p.get("product", "").startswith(prefix):
                qty = p.get("qty", 0)
                return qty if p.get("side") == "B" else -qty
        return 0

    def has_open_positions(self) -> bool:
        """True if any stored OI row has non-zero qty.

        Confirmed (#139 debug_20260920.log): Capital follows
        ``001,查無資料`` with a ``##`` end-of-data terminator. Pre-fix
        ``parse_open_interest`` ingested that row as qty=0 / empty side,
        so a truthy ``positions`` list showed 「空 SHORT x0」 on
        load-existing. Distinct from #107 (ghost *non-zero* 持倉 after
        resume).
        """
        return any(p.get("qty", 0) != 0 for p in self.positions)

    def first_open_position(self, prefix: str | None = None,
                            market: str | None = None) -> dict | None:
        """First OI row with non-zero qty, or None if nothing matches.

        No arguments: any non-zero row (issue #139 — skip qty=0 / ``##``).
        With ``prefix`` and/or ``market``: only this product. A TO option
        must not be returned for a TF futures close (issue #145). The
        manual-close decision itself is :func:`close_order_side`.
        """
        if prefix is None and market is None:
            for p in self.positions:
                if p.get("qty", 0) != 0:
                    return p
            return None
        for p in self.positions:
            if oi_product_match(p, prefix or "", market):
                return p
        return None

    # ── Display computation ──

    def compute_display(self) -> AccountDisplay:
        """Compute formatted display values from positions and rights.

        Returns an AccountDisplay dataclass with all formatted strings.
        """
        d = AccountDisplay()

        # Positions
        if self.positions:
            parts = []
            for p in self.positions:
                side = "LONG" if p["side"] == "B" else "SHORT"
                parts.append(f"{side} x{p['qty']} {p['product']} @{p['avg_cost']}")
            d.position_text = " | ".join(parts)

        # Rights / equity
        r = self.rights
        if not r:
            return d

        d.equity = fmt_money(r.get("equity", "--"))
        d.available = fmt_money(r.get("available", "--"))
        d.float_pnl = fmt_money(r.get("float_pnl", "--"))
        d.realized_pnl = fmt_money(r.get("realized_pnl", "--"))

        try:
            realized = int(float(r.get("realized_pnl", "0") or "0"))
            fee = int(float(r.get("realized_cost", "0") or "0"))
            tax = int(float(r.get("tax", "0") or "0"))
            float_pnl = int(float(r.get("float_pnl", "0") or "0"))
            d.fees = f"{fee + tax:,} ({fee:,}+{tax:,})"
            net = realized - fee - tax + float_pnl
            d.net_pnl = f"{net:+,}"
            d.net_pnl_int = net
            d.valid = True
        except (ValueError, TypeError):
            d.fees = f"{r.get('realized_cost', '--')}+{r.get('tax', '--')}"
            d.net_pnl = "--"

        d.maint_rate = f"{r.get('maint_rate', '--')}%"
        return d

    # ── Fill parsing ──

    def parse_fills(self, fills_raw: str | tuple, label: str) -> FillsResult:
        """Parse GetFulfillReport/GetOrderReport result.

        Returns FillsResult with parsed entries and new entries (not seen
        in the previous call for the same label).
        """
        result = FillsResult()

        # COM may return (string, code) tuple or just string
        if isinstance(fills_raw, (tuple, list)):
            fills_raw = fills_raw[0] if fills_raw else ""
        if not fills_raw or not isinstance(fills_raw, str):
            return result

        lines = [ln.strip() for ln in fills_raw.split("\n") if ln.strip()]
        if not lines or lines[0].startswith("001") or lines[0].startswith("##"):
            return result

        for line in lines:
            fields = line.split(",")
            if len(fields) >= 24:
                side = fields[15].strip()
                price = fields[19].strip()
                qty = fields[20].strip()
                new_close = fields[21].strip()  # N=new, O=close/offset
                date = fields[23].strip()
                side_str = "BUY" if side == "B" else "SELL"
                nc_str = "\u958b" if new_close == "N" else "\u5e73"
                try:
                    price_f = float(price)
                except (ValueError, TypeError):
                    price_f = None
                if price_f is not None:
                    result.entries.append(
                        f"{date} {side_str}{nc_str} x{qty} @{price_f:,.1f}")
                else:
                    result.entries.append(
                        f"{date} {side_str}{nc_str} x{qty} @{price}")

        result.count = len(result.entries)

        # Detect new fills since last call for this label
        prev_count = self._prev_fill_counts.get(label, 0)
        if result.count > prev_count:
            result.new_entries = result.entries[prev_count:]
        self._prev_fill_counts[label] = result.count

        return result

    # ── Fill poll support ──

    def update_fill_poll_position(self, raw_data: str, parsed: dict | None,
                                  prefix: str) -> int | None:
        """Process OnOpenInterest callback during fill polling.

        Returns the new signed position if relevant to the prefix,
        or None if the data doesn't match.
        """
        if parsed and parsed.get("product", "").startswith(prefix):
            qty = parsed.get("qty", 0)
            return qty if parsed.get("side") == "B" else -qty
        elif raw_data.strip().startswith("001"):
            # "查無資料" — no position at all
            return 0
        return None
