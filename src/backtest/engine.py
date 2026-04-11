"""Backtest engine: replays historical bars through a strategy."""

from __future__ import annotations

from ..market_data.models import Bar
from ..market_data.data_store import DataStore
from .broker import SimulatedBroker
from .strategy import BacktestStrategy
from .metrics import PerformanceMetrics, calculate_metrics


class BacktestResult:
    """Container for backtest output."""

    def __init__(
        self,
        strategy_name: str,
        broker: SimulatedBroker,
        bars_processed: int,
    ):
        self.strategy_name = strategy_name
        self.broker = broker
        self.trades = broker.trades
        self.equity_curve = broker.equity_curve
        self.bars_processed = bars_processed
        self.metrics: PerformanceMetrics = calculate_metrics(broker.trades, broker.equity_curve)


class BacktestEngine:
    """Replays a list of bars through a strategy and simulated broker.

    Two fill modes (selectable via ``fill_mode`` ctor arg):

    ``"on_close"`` (default — TradingView process_orders_on_close=true):
        1. Feed bar into DataStore
        2. Check pending exit orders against this bar's OHLC
        3. Run strategy.on_bar() which may queue new entry/exit orders
        4. Process pending entry orders at this bar's close
        Minimum trade lifetime: 1 bar.

    ``"next_open"`` (TradingView process_orders_on_close=false default):
        1. Feed bar into DataStore
        2. Fill pending entries (queued on prior bar) at THIS bar's open
        3. Check pending exit orders against this bar's OHLC — INCLUDES
           the position just opened by step 2, enabling same-bar enter+exit
        4. Run strategy.on_bar() which may queue new entry/exit orders
           for the NEXT bar's open
        5. Process pending market closes at this bar's close
        Minimum trade lifetime: 0 bars (enter+exit on the same bar).
    """

    def __init__(
        self,
        strategy: BacktestStrategy,
        point_value: int = 1,
        max_bars: int = 5000,
        fill_mode: str = "on_close",
    ):
        self.strategy = strategy
        self.broker = SimulatedBroker(point_value=point_value, fill_mode=fill_mode)
        self.data_store = DataStore(max_bars=max_bars)

    def run(self, bars: list[Bar]) -> BacktestResult:
        required = self.strategy.required_bars()
        ctx = self.broker.context

        for i, bar in enumerate(bars):
            self.data_store.add_bar(bar)

            bar_dt = bar.dt.strftime("%Y-%m-%d %H:%M") if bar.dt else ""

            # next_open mode: fill entries queued on the prior bar at THIS
            # bar's open. on_close mode: no-op (entries fill in on_bar_close).
            self.broker.on_bar_open(i, bar.open, bar_dt)

            # Check exits against this bar's OHLC. In next_open mode this
            # check sees any position just opened by on_bar_open above —
            # that's how same-bar entry+exit becomes possible.
            if i > 0 or self.broker.position_size > 0:
                self.broker.check_exits(i, bar.open, bar.high, bar.low, bar.close, bar_dt)

            # Run strategy once enough bars accumulated
            if len(self.data_store) >= required:
                self.strategy.on_bar(bar, self.data_store, ctx)

            # Process market closes (and, in on_close mode, fill entries)
            self.broker.on_bar_close(i, bar.close, bar_dt)

        # Force close any open position at end of data
        if bars:
            last_dt = bars[-1].dt.strftime("%Y-%m-%d %H:%M") if bars[-1].dt else ""
            if (self.broker.position_size > 0
                    and self.broker.entry_price > 0
                    and bars[-1].close > 0):
                pct = abs(bars[-1].close - self.broker.entry_price) / self.broker.entry_price
                if pct > 0.20:
                    print(f"[WARNING] force_close: price {bars[-1].close} deviates "
                          f"{pct:.0%} from entry {self.broker.entry_price} at {last_dt} "
                          f"— data may be corrupted")
            self.broker.force_close(len(bars) - 1, bars[-1].close, last_dt)

        return BacktestResult(
            strategy_name=self.strategy.name,
            broker=self.broker,
            bars_processed=len(bars),
        )
