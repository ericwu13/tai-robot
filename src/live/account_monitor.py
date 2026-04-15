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
    # Structured fill records aligned with `entries` (same order, same length).
    # Each dict has: side ("B"/"S"), qty (int), price (float), new_close
    # ("N"=new/開, "O"=close/平), date (str). Used by Phase 2 real exit price
    # tracking — callers pick fills with new_close == "O" and apply to the
    # most recent trade via try_set_real_exit_price.
    parsed: list[dict] = field(default_factory=list)
    new_parsed: list[dict] = field(default_factory=list)


# ── Pure parsing functions (moved from run_backtest.py) ──

def parse_open_interest(bstr: str) -> dict | None:
    """Parse OnOpenInterest callback data into a dict.

    Returns None for error/empty codes (001, 970).
    Fields: product, side(B/S), qty, daytrade_qty, avg_cost, fee, tax_rate
    """
    vals = bstr.split(",")
    if len(vals) < 10 or vals[0] in ("001", "970", "980"):
        return None
    try:
        return {
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


def fmt_money(val: str) -> str:
    """Format a numeric string with comma separators."""
    try:
        n = float(val)
        if n == int(n):
            return f"{int(n):,}"
        return f"{n:,.2f}"
    except (ValueError, TypeError):
        return val


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

    def reset(self) -> None:
        """Clear all state."""
        self.positions.clear()
        self.rights.clear()
        self._prev_fill_counts.clear()

    # ── Position tracking ──

    def add_position(self, parsed: dict) -> None:
        """Add a parsed open interest entry."""
        self.positions.append(parsed)

    def clear_positions(self) -> None:
        """Clear position list (before rebuilding from callbacks)."""
        self.positions.clear()

    def set_flat(self) -> None:
        """Called when '001' (no positions) is received."""
        self.positions.clear()

    def update_rights(self, parsed: dict) -> None:
        """Store parsed future rights data."""
        self.rights = parsed

    def get_signed_position(self, prefix: str) -> int:
        """Get signed position qty matching a symbol prefix.

        Positive = long, negative = short, 0 = flat.
        Prefix is typically first 2 chars of order symbol (e.g., "TX", "TM").
        """
        if not prefix:
            return 0
        for p in self.positions:
            if p.get("product", "").startswith(prefix):
                qty = p.get("qty", 0)
                return qty if p.get("side") == "B" else -qty
        return 0

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
                # Parse numeric fields once; store None on failure.
                try:
                    price_f = float(price)
                except (ValueError, TypeError):
                    price_f = None
                try:
                    qty_i = int(qty)
                except (ValueError, TypeError):
                    qty_i = 0
                if price_f is not None:
                    result.entries.append(
                        f"{date} {side_str}{nc_str} x{qty} @{price_f:,.1f}")
                else:
                    result.entries.append(
                        f"{date} {side_str}{nc_str} x{qty} @{price}")
                result.parsed.append({
                    "side": side, "qty": qty_i, "price": price_f,
                    "new_close": new_close, "date": date,
                })

        result.count = len(result.entries)

        # Detect new fills since last call for this label
        prev_count = self._prev_fill_counts.get(label, 0)
        if result.count > prev_count:
            result.new_entries = result.entries[prev_count:]
            result.new_parsed = result.parsed[prev_count:]
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
