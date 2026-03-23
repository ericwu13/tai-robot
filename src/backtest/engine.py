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

    Fill semantics match TradingView with process_orders_on_close=true:
    1. Feed bar into DataStore
    2. Check pending exit orders against this bar's OHLC
    3. Run strategy.on_bar() which may queue new entry/exit orders
    4. Process pending entry orders at this bar's close
    """

    def __init__(
        self,
        strategy: BacktestStrategy,
        point_value: int = 1,
        max_bars: int = 5000,
        commission_per_contract: int = 0,
    ):
        self.strategy = strategy
        self.broker = SimulatedBroker(point_value=point_value, commission_per_contract=commission_per_contract)
        self.data_store = DataStore(max_bars=max_bars)

    def run(self, bars: list[Bar]) -> BacktestResult:
        required = self.strategy.required_bars()
        ctx = self.broker.context

        for i, bar in enumerate(bars):
            self.data_store.add_bar(bar)

            bar_dt = bar.dt.strftime("%Y-%m-%d %H:%M") if bar.dt else ""

            # Check exit orders from previous bar against this bar's OHLC
            if i > 0:
                self.broker.check_exits(i, bar.open, bar.high, bar.low, bar.close, bar_dt)

            # Run strategy once enough bars accumulated
            if len(self.data_store) >= required:
                self.strategy.on_bar(bar, self.data_store, ctx)

            # Fill entry orders at this bar's close
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
