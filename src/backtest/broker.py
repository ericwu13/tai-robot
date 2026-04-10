"""Simulated broker for backtesting: order matching, position tracking, trade recording."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class OrderSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class Order:
    tag: str
    side: OrderSide
    qty: int = 1
    limit: int | float | None = None
    stop: int | float | None = None
    from_entry: str = ""


@dataclass
class Trade:
    tag: str
    side: OrderSide
    qty: int
    entry_price: int
    exit_price: int
    entry_bar_index: int
    exit_bar_index: int
    pnl: int = 0
    exit_tag: str = ""
    entry_dt: str = ""   # ISO format datetime string
    exit_dt: str = ""    # ISO format datetime string
    # Real-order fill data (issue #45). Populated only in auto/semi_auto
    # mode when the real broker confirms a fill. 0 / "" mean "not filled /
    # paper mode / fill race dropped". See SimulatedBroker.real_entry_price.
    real_entry_price: int = 0
    real_entry_dt: str = ""
    real_exit_price: int = 0    # Phase 2 — not yet captured
    real_exit_dt: str = ""      # Phase 2 — not yet captured


class BrokerContext:
    """Strategy-facing API that mirrors TradingView's strategy.entry()/exit().

    Strategies call entry() and exit() here; actual order matching
    is done by SimulatedBroker after on_bar() returns.
    """

    def __init__(self, broker: SimulatedBroker):
        self._broker = broker

    @property
    def position_size(self) -> int:
        return self._broker.position_size

    @property
    def trades(self) -> list:
        """Read-only access to completed trades (for loss counting, etc.)."""
        return list(self._broker.trades)

    @property
    def entry_price(self) -> int:
        """Simulated entry price of the current open position (0 if flat).

        This is the price the strategy DECIDED to enter at (usually
        ``bar.close`` at signal time). For stop-loss calculations in
        live/auto mode prefer :meth:`effective_entry_price` so the
        stop tracks the REAL fill price when available.
        """
        return self._broker.entry_price

    @property
    def real_entry_price(self) -> int:
        """Real broker-confirmed entry price (0 if paper/unconfirmed).

        Populated only in auto/semi_auto mode when OpenInterest
        reports the position avg_cost. Use :meth:`effective_entry_price`
        instead of reading this directly — it handles the fallback
        to simulated entry price cleanly.
        """
        return self._broker.real_entry_price

    def effective_entry_price(self) -> int:
        """Best-available stop reference: real if confirmed, else simulated.

        Use this in strategies for entry-relative stop calculations::

            def on_bar(self, bar, data_store, broker):
                if broker.position_size > 0:
                    stop = broker.effective_entry_price() - 50
                    broker.exit("exit", "Long", stop=stop)

        In paper mode, during the pre-confirmation window, or if the
        fill confirmation never arrives, this falls back to the
        simulated ``entry_price`` — so the call is safe in all modes.
        """
        return self._broker.effective_entry_price()

    def entry(self, tag: str, side: OrderSide, qty: int = 1) -> None:
        self._broker.queue_entry(Order(tag=tag, side=side, qty=qty))

    def exit(
        self,
        tag: str,
        from_entry: str,
        limit: int | None = None,
        stop: int | None = None,
    ) -> None:
        self._broker.queue_exit(Order(
            tag=tag, side=OrderSide.LONG, qty=0,
            limit=limit, stop=stop,
            from_entry=from_entry,
        ))

    def close(self, from_entry: str, tag: str = "close") -> None:
        """Market-close the position from the given entry. Fills at current bar's close.

        Use this instead of exit() when you want an immediate close without
        setting limit/stop prices (like TradingView's strategy.close()).
        """
        self._broker.queue_market_close(tag, from_entry)


class SimulatedBroker:
    """Order matching engine for backtesting.

    Fill semantics (matching TradingView process_orders_on_close=true):
    - Bar N: strategy on_bar() queues entry -> fills at bar N close
    - Bar N+1 onward: exit limit/stop checked against each bar's OHLC
    - Ambiguous bar (both SL and TP hit): open <= stop -> SL first; else TP first
    - End of data: force-close any open position at last bar's close
    """

    def __init__(self, point_value: int = 1):
        self.point_value = point_value
        self.position_size: int = 0
        self.position_side: OrderSide | None = None
        self.entry_price: int = 0
        self.entry_tag: str = ""
        self.entry_bar_index: int = 0
        self._entry_dt: str = ""
        self._current_bar_dt: str = ""
        # Real-fill tracking for the CURRENT open position (issue #45).
        # Set by the GUI's _on_fill_confirmed when the real broker confirms
        # an entry fill. 0 means "not confirmed yet / paper mode / no real
        # order". Copied into Trade.real_entry_price on close, then reset.
        self.real_entry_price: int = 0
        self.real_entry_dt: str = ""

        self.trades: list[Trade] = []
        self.equity_curve: list[int] = []
        self._cumulative_pnl: int = 0

        self._pending_entries: list[Order] = []
        self._pending_exits: list[Order] = []
        self._pending_market_closes: list[tuple[str, str]] = []  # (tag, from_entry)
        self._bar_index: int = 0
        self._exit_bar_index: int = -1  # last bar an exit filled on

        # Last exit metadata — read by live_runner to determine real order type
        self.last_exit_type: str = ""  # "limit", "stop", "close", "force_close"
        self.last_exit_limit: int | None = None  # strategy's original limit price

    @property
    def context(self) -> BrokerContext:
        return BrokerContext(self)

    def queue_entry(self, order: Order) -> None:
        self._pending_entries.append(order)

    def queue_exit(self, order: Order) -> None:
        # Replace existing exit with same from_entry (TradingView semantics)
        self._pending_exits = [
            o for o in self._pending_exits if o.from_entry != order.from_entry
        ]
        # Auto-round float prices to int (TAIFEX prices are integers).
        # AI-generated strategies sometimes pass raw float calculations
        # like limit=35307.30 which should be 35307.
        if order.limit is not None:
            order.limit = int(round(order.limit))
        if order.stop is not None:
            order.stop = int(round(order.stop))
        self._pending_exits.append(order)

    def queue_market_close(self, tag: str, from_entry: str) -> None:
        """Queue a market close — fills at current bar's close."""
        self._pending_market_closes.append((tag, from_entry))

    def on_bar_close(self, bar_index: int, close: int, bar_dt: str = "") -> None:
        """Process pending market closes and entry orders at bar close.

        Order: market closes first, then entries.
        Same-bar re-entry is allowed (matches TradingView semantics):
        exit fills intra-bar at TP/SL price, entry fills at bar close.
        In live trading, tick-level exit detection separates these naturally.
        """
        self._bar_index = bar_index
        self._current_bar_dt = bar_dt

        # Process market closes first
        for tag, from_entry in self._pending_market_closes:
            if self.position_size > 0 and self.entry_tag == from_entry:
                self.last_exit_type = "close"
                self.last_exit_limit = None
                self._close_position(tag, close, bar_index)
                break
        self._pending_market_closes.clear()

        for order in self._pending_entries:
            if self.position_size == 0:
                self.position_size = order.qty
                self.position_side = order.side
                self.entry_price = close
                self.entry_tag = order.tag
                self.entry_bar_index = bar_index
                self._entry_dt = bar_dt
                # Clear stale exit metadata from previous trade
                self.last_exit_type = ""
                self.last_exit_limit = None
                # Belt-and-braces reset of real-fill state for the new
                # position. Normally _close_position already cleared these
                # when the previous trade closed, but a late OpenInterest
                # callback for the previous trade could have slipped past
                # the _on_fill_confirmed guard and planted a stale value.
                # Clearing here guarantees the new trade starts clean
                # (issue #45 late-arrival race).
                self.real_entry_price = 0
                self.real_entry_dt = ""
        self._pending_entries.clear()

    def check_exits(self, bar_index: int, open_: int, high: int, low: int, close: int, bar_dt: str = "") -> None:
        """Check pending exit orders against this bar's OHLC."""
        self._bar_index = bar_index
        self._current_bar_dt = bar_dt
        if self.position_size == 0 or not self._pending_exits:
            return

        for order in self._pending_exits:
            if self.position_size == 0:
                break

            limit = order.limit
            stop = order.stop

            if limit is None and stop is None:
                continue

            fill_price = self._resolve_exit(
                open_, high, low, limit, stop, self.position_side,
            )
            if fill_price is not None:
                # Determine if this was a limit (TP) or stop (SL) fill
                if limit is not None and stop is None:
                    self.last_exit_type = "limit"
                elif stop is not None and limit is None:
                    self.last_exit_type = "stop"
                else:
                    # Both set — check which side the fill is on
                    if self.position_side == OrderSide.LONG:
                        self.last_exit_type = "stop" if fill_price <= stop else "limit"
                    else:
                        self.last_exit_type = "stop" if fill_price >= stop else "limit"
                self.last_exit_limit = int(limit) if limit is not None else None
                self._close_position(order.tag, fill_price, bar_index)
                break

    def _resolve_exit(
        self,
        open_: int,
        high: int,
        low: int,
        limit: int | None,
        stop: int | None,
        side: OrderSide | None,
    ) -> int | None:
        """Determine if and at what price an exit fills on this bar.

        For LONG positions:
          - limit (take profit) fills at limit if high >= limit; at open if open >= limit (gap up)
          - stop (stop loss) fills at stop if low <= stop; at open if open <= stop (gap down)
        For ambiguous bars (both hit), use open to disambiguate:
          - if open <= stop: stop fills first (gap down)
          - else: limit fills first
        """
        if side == OrderSide.LONG:
            limit_hit = limit is not None and high >= limit
            stop_hit = stop is not None and low <= stop
            stop_fill = min(open_, stop) if (stop_hit and stop is not None and open_ <= stop) else stop
            limit_fill = max(open_, limit) if (limit_hit and limit is not None and open_ >= limit) else limit
        else:  # SHORT
            limit_hit = limit is not None and low <= limit
            stop_hit = stop is not None and high >= stop
            stop_fill = max(open_, stop) if (stop_hit and stop is not None and open_ >= stop) else stop
            limit_fill = min(open_, limit) if (limit_hit and limit is not None and open_ <= limit) else limit

        if limit_hit and stop_hit:
            if side == OrderSide.LONG:
                return stop_fill if open_ <= stop else limit_fill
            else:
                return stop_fill if open_ >= stop else limit_fill
        elif stop_hit:
            return stop_fill
        elif limit_hit:
            return limit_fill
        return None

    def _close_position(self, tag: str, exit_price: int | float, bar_index: int) -> None:
        exit_price = int(round(exit_price))  # TAIFEX prices are integers
        if self.position_side == OrderSide.LONG:
            pnl = (exit_price - self.entry_price) * self.position_size * self.point_value
        else:
            pnl = (self.entry_price - exit_price) * self.position_size * self.point_value

        trade = Trade(
            tag=self.entry_tag,
            side=self.position_side,
            qty=self.position_size,
            entry_price=self.entry_price,
            exit_price=exit_price,
            entry_bar_index=self.entry_bar_index,
            exit_bar_index=bar_index,
            pnl=pnl,
            exit_tag=tag,
            entry_dt=self._entry_dt,
            exit_dt=self._current_bar_dt,
            # Snapshot the real-fill state for this position before reset.
            # In paper mode or if the real fill never confirmed, these are
            # 0/"" and the Trades tab will render them as "--".
            real_entry_price=self.real_entry_price,
            real_entry_dt=self.real_entry_dt,
        )
        self.trades.append(trade)
        self._cumulative_pnl += pnl
        self.equity_curve.append(self._cumulative_pnl)

        self.position_size = 0
        self.position_side = None
        self.entry_price = 0
        self.entry_tag = ""
        # Reset real-fill state so the next trade starts clean. The
        # late-arrival guard in _on_fill_confirmed also rejects writes
        # when position_size == 0, so two layers of defense (issue #45).
        self.real_entry_price = 0
        self.real_entry_dt = ""
        self._pending_exits.clear()
        self._exit_bar_index = bar_index

    def effective_entry_price(self) -> int:
        """Return the best-available stop-reference price.

        Returns the real fill price if it has been confirmed for the
        current position, otherwise falls back to the simulated entry
        price. Returns 0 when flat.

        Strategies that place stops relative to the entry should use
        this rather than ``self.entry_price`` directly so the stop
        naturally tracks where the order REALLY filled in auto mode
        (issue #45), while degrading gracefully in paper mode or
        during the pre-confirmation window.
        """
        if self.real_entry_price > 0:
            return self.real_entry_price
        return self.entry_price

    def try_set_real_entry_price(
        self,
        price: int,
        entry_bar_index: int,
        fill_dt: str = "",
    ) -> bool:
        """Guarded write of the real (broker-confirmed) entry fill price.

        Race guard for issue #45. Rejects the write when any of the
        following hold, leaving ``real_entry_price`` unchanged:

        - position is flat (stale callback for an already-closed trade)
        - ``real_entry_price`` already set (double confirmation / replay)
        - ``entry_bar_index`` doesn't match the current position's
          (late callback for a previous trade whose ``_close_position``
          has already run)
        - ``price <= 0`` (bad parse upstream)

        Returns True if the write was accepted, False otherwise. Call
        sites should log a ``FILL_RACE_SKIP`` diagnostic when rejected.
        """
        if price <= 0:
            return False
        if self.position_size == 0:
            return False
        if self.real_entry_price != 0:
            return False
        if self.entry_bar_index != entry_bar_index:
            return False
        self.real_entry_price = int(price)
        self.real_entry_dt = fill_dt
        return True

    def force_close(self, bar_index: int, close: int, bar_dt: str = "") -> None:
        """Force-close any open position at end of data."""
        if self.position_size > 0:
            self._current_bar_dt = bar_dt
            self.last_exit_type = "force_close"
            self.last_exit_limit = None
            self._close_position("force_close", close, bar_index)

    def record_equity(self) -> None:
        """Record equity point for bars without trades."""
        if len(self.equity_curve) == 0 or self.equity_curve[-1] != self._cumulative_pnl:
            pass  # equity only changes on trade close

    def to_dict(self) -> dict:
        """Serialize broker state for session persistence."""
        return {
            "point_value": self.point_value,
            "position_size": self.position_size,
            "position_side": self.position_side.value if self.position_side else None,
            "entry_price": self.entry_price,
            "entry_tag": self.entry_tag,
            "entry_bar_index": self.entry_bar_index,
            "real_entry_price": self.real_entry_price,
            "real_entry_dt": self.real_entry_dt,
            "trades": [
                {
                    "tag": t.tag, "side": t.side.value, "qty": t.qty,
                    "entry_price": t.entry_price, "exit_price": t.exit_price,
                    "entry_bar_index": t.entry_bar_index,
                    "exit_bar_index": t.exit_bar_index,
                    "pnl": t.pnl, "exit_tag": t.exit_tag,
                    "entry_dt": t.entry_dt, "exit_dt": t.exit_dt,
                    "real_entry_price": t.real_entry_price,
                    "real_entry_dt": t.real_entry_dt,
                    "real_exit_price": t.real_exit_price,
                    "real_exit_dt": t.real_exit_dt,
                }
                for t in self.trades
            ],
            "equity_curve": list(self.equity_curve),
            "_cumulative_pnl": self._cumulative_pnl,
            "_bar_index": self._bar_index,
            "_exit_bar_index": self._exit_bar_index,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SimulatedBroker":
        """Restore broker state from a serialized dict.

        All new fields use ``.get()`` with safe defaults so old session
        files (before issue #45) load without error.
        """
        broker = cls(point_value=data.get("point_value", 1))
        broker.position_size = data.get("position_size", 0)
        side = data.get("position_side")
        broker.position_side = OrderSide(side) if side else None
        broker.entry_price = data.get("entry_price", 0)
        broker.entry_tag = data.get("entry_tag", "")
        broker.entry_bar_index = data.get("entry_bar_index", 0)
        broker.real_entry_price = data.get("real_entry_price", 0)
        broker.real_entry_dt = data.get("real_entry_dt", "")
        broker.trades = [
            Trade(
                tag=t["tag"], side=OrderSide(t["side"]), qty=t["qty"],
                entry_price=t["entry_price"], exit_price=t["exit_price"],
                entry_bar_index=t["entry_bar_index"],
                exit_bar_index=t["exit_bar_index"],
                pnl=t["pnl"], exit_tag=t.get("exit_tag", ""),
                entry_dt=t.get("entry_dt", ""), exit_dt=t.get("exit_dt", ""),
                real_entry_price=t.get("real_entry_price", 0),
                real_entry_dt=t.get("real_entry_dt", ""),
                real_exit_price=t.get("real_exit_price", 0),
                real_exit_dt=t.get("real_exit_dt", ""),
            )
            for t in data.get("trades", [])
        ]
        broker.equity_curve = list(data.get("equity_curve", []))
        broker._cumulative_pnl = data.get("_cumulative_pnl", 0)
        broker._bar_index = data.get("_bar_index", 0)
        broker._exit_bar_index = data.get("_exit_bar_index", -1)
        return broker
