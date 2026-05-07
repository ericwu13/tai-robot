"""Backtest engine: replays historical bars through a strategy."""

from __future__ import annotations

from datetime import timedelta

from ..market_data.models import Bar
from ..market_data.data_store import DataStore
from ..live.bar_aggregator import BarAggregator
from .broker import SimulatedBroker
from .strategy import BacktestStrategy
from .metrics import PerformanceMetrics, calculate_metrics


# Map (kline_type, kline_minute) to interval in seconds. Mirrors the table
# in src/live/live_runner.py — keep in sync if either side gains a new key.
_INTERVAL_SECONDS = {
    (0, 240): 14400,
    (0, 60): 3600,
    (0, 30): 1800,
    (0, 15): 900,
    (0, 5): 300,
    (0, 1): 60,
    (4, 1): 86400,
}


def strategy_primary_interval(strategy: BacktestStrategy) -> int:
    """Return the strategy's primary bar interval in seconds."""
    kt = strategy.kline_type
    km = strategy.kline_minute
    if kt == 0:
        return km * 60
    return _INTERVAL_SECONDS.get((kt, km), 0)


def validate_htf_intervals(primary_interval: int, htf_intervals: list[int]) -> None:
    """Raise ValueError if any HTF interval is invalid for *primary_interval*.

    Rules: each HTF interval must be > primary AND an exact multiple.
    """
    for iv in htf_intervals:
        if iv <= primary_interval:
            raise ValueError(
                f"HTF interval {iv}s must be larger than "
                f"primary interval {primary_interval}s"
            )
        if iv % primary_interval != 0:
            raise ValueError(
                f"HTF interval {iv}s must be exact multiple of "
                f"primary interval {primary_interval}s"
            )


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

        # MTF setup. Empty intervals = single-TF, no aggregators created,
        # zero overhead in the run loop.
        self._primary_interval = strategy_primary_interval(strategy)
        self._htf_intervals: list[int] = list(getattr(strategy, "htf_intervals", []) or [])
        if self._htf_intervals:
            validate_htf_intervals(self._primary_interval, self._htf_intervals)
            self._htf_aggregators: dict[int, BarAggregator] = {}
            symbol = "BACKTEST"
            for iv in self._htf_intervals:
                self.data_store._register_htf(iv, max_bars=max_bars)
                self._htf_aggregators[iv] = BarAggregator(symbol, iv)
            self._htf_required = strategy.htf_required_bars() or {}
        else:
            self._htf_aggregators = {}
            self._htf_required = {}

    def _prepare_bars(self, bars: list[Bar]) -> list[Bar]:
        """If 1-min input is given for a >1-min primary MTF strategy, aggregate up.

        Aggregation is opt-in via ``htf_intervals`` so single-TF
        strategies see exactly the bars their caller passed in — that
        preserves all existing backtests, where the convention is to
        pass primary-interval bars (or, in some cases, 1-min bars
        regardless of the strategy's declared primary). MTF strategies
        explicitly opt into the 1-min canonical input contract.
        """
        if not bars or not self._htf_intervals:
            return bars
        primary = self._primary_interval
        if primary <= 0:
            return bars
        first = bars[0]
        if first.interval == 60 and primary > 60:
            agg = BarAggregator(first.symbol, primary)
            out: list[Bar] = []
            for b in bars:
                completed = agg.on_bar(b)
                if completed is not None:
                    out.append(completed)
            tail = agg.flush()
            if tail is not None:
                out.append(tail)
            return out
        return bars

    def _process_htf(self, primary_bar: Bar) -> None:
        """Feed *primary_bar* to each HTF aggregator and push completions."""
        if not self._htf_aggregators:
            return
        for iv, agg in self._htf_aggregators.items():
            completed = agg.on_bar(primary_bar)
            if completed is not None:
                self.data_store._add_htf_bar(iv, completed)

    def _warmup_satisfied(self, required: int) -> bool:
        if len(self.data_store) < required:
            return False
        for iv, n in self._htf_required.items():
            if self.data_store._htf_len(iv) < n:
                return False
        return True

    def run(self, bars: list[Bar]) -> BacktestResult:
        required = self.strategy.required_bars()
        ctx = self.broker.context

        # If the source feed is 1-min but the strategy's primary is larger,
        # aggregate to primary on the fly. Otherwise feed primary bars
        # straight through. This keeps the canonical "1-min input" path
        # available without breaking existing tests that pass primary
        # bars directly (e.g. H4 fixtures).
        bars = self._prepare_bars(bars)

        for i, bar in enumerate(bars):
            self.data_store.add_bar(bar)
            self._process_htf(bar)

            # Second precision for entry/exit timestamps. Backtest bars are
            # always minute-aligned so seconds are :00 by construction; the
            # wider format keeps consistency with live mode timestamps.
            bar_dt = bar.dt.strftime("%Y-%m-%d %H:%M:%S") if bar.dt else ""
            bar_close_dt = ""
            if bar.dt and bar.interval:
                bar_close_dt = (bar.dt + timedelta(seconds=bar.interval)
                               ).strftime("%Y-%m-%d %H:%M:%S")
            else:
                bar_close_dt = bar_dt

            # next_open mode: fill entries queued on the prior bar at THIS
            # bar's open. on_close mode: no-op (entries fill in on_bar_close).
            # Keep bar_dt (open time) — next_open entries fill at bar open.
            self.broker.on_bar_open(i, bar.open, bar_dt)

            # Check exits against this bar's OHLC. In next_open mode this
            # check sees any position just opened by on_bar_open above —
            # that's how same-bar entry+exit becomes possible.
            if i > 0 or self.broker.position_size > 0:
                self.broker.check_exits(i, bar.open, bar.high, bar.low, bar.close, bar_close_dt)

            # Run strategy once enough bars accumulated (primary + HTF warmup)
            if self._warmup_satisfied(required):
                old_exits = len(self.broker._pending_exits)
                self.strategy.on_bar(bar, self.data_store, ctx)

                # Catch-up exit check: if strategy just queued new exits
                # while a position is open, check them immediately against
                # this bar's OHLC.  Matches live_runner safety net so
                # strategies that set TP/SL one bar late still resolve
                # correctly on the signal bar.
                if (len(self.broker._pending_exits) > old_exits
                        and self.broker.position_size > 0):
                    self.broker.check_exits(
                        i, bar.open, bar.high, bar.low, bar.close, bar_close_dt)

            # Process market closes (and, in on_close mode, fill entries)
            self.broker.on_bar_close(i, bar.close, bar_close_dt)

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
