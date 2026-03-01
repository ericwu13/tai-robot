"""Tests for H4 Bollinger Long strategy logic."""

from datetime import datetime, timedelta

import pytest

from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.backtest.broker import SimulatedBroker, OrderSide
from src.strategy.examples.h4_bollinger_long import H4BollingerLongStrategy


def make_bars(prices, start_dt=None):
    """Create bars from (open, high, low, close) tuples."""
    if start_dt is None:
        start_dt = datetime(2025, 1, 2, 8, 45)
    bars = []
    for i, (o, h, l, c) in enumerate(prices):
        bars.append(Bar(
            symbol="TX00",
            dt=start_dt + timedelta(hours=4 * i),
            open=o, high=h, low=l, close=c,
            volume=1000, interval=14400,
        ))
    return bars


@pytest.fixture
def strategy():
    return H4BollingerLongStrategy(bb_period=20, bb_std=2.0, sl_offset=20, tp_offset=50)


@pytest.fixture
def broker():
    return SimulatedBroker(point_value=200)


@pytest.fixture
def data_store():
    return DataStore(max_bars=100)


class TestBreakoutEntry:
    def test_bullish_breakout_above_basis(self, strategy, broker, data_store):
        """Strong bullish candle closing above middle band should trigger entry."""
        # Build up 19 bars of flat data to establish Bollinger Bands
        base = 20000
        warmup = [(base, base + 10, base - 10, base) for _ in range(19)]
        bars = make_bars(warmup)
        for b in bars:
            data_store.add_bar(b)

        # Bar 20: strong bullish candle (body > 66% of range)
        # With 19 bars at 20000, basis ~ 20000
        breakout_bar = Bar(
            symbol="TX00",
            dt=datetime(2025, 1, 6, 8, 45),
            open=20010, high=20110, low=20000, close=20100,  # body=90, range=110, 81%
            volume=2000, interval=14400,
        )
        data_store.add_bar(breakout_bar)

        ctx = broker.context
        strategy.on_bar(breakout_bar, data_store, ctx)

        assert len(broker._pending_entries) == 1
        assert broker._pending_entries[0].side == OrderSide.LONG

    def test_no_entry_when_bearish(self, strategy, broker, data_store):
        """Bearish candle should not trigger breakout entry."""
        base = 20000
        warmup = [(base, base + 10, base - 10, base) for _ in range(19)]
        bars = make_bars(warmup)
        for b in bars:
            data_store.add_bar(b)

        bearish_bar = Bar(
            symbol="TX00",
            dt=datetime(2025, 1, 6, 8, 45),
            open=20100, high=20110, low=20000, close=20010,
            volume=2000, interval=14400,
        )
        data_store.add_bar(bearish_bar)

        ctx = broker.context
        strategy.on_bar(bearish_bar, data_store, ctx)

        assert len(broker._pending_entries) == 0


class TestPullbackEntry:
    def test_pullback_pattern(self, strategy, broker, data_store):
        """Low pierces basis, close above basis, small body, long lower shadow."""
        base = 20000
        warmup = [(base, base + 10, base - 10, base) for _ in range(19)]
        bars = make_bars(warmup)
        for b in bars:
            data_store.add_bar(b)

        # Pullback bar: low below basis, close above, small body, long lower shadow
        # basis ~ 20000; body=10, range=100 (10%), lower_shadow=90 (90%)
        pullback_bar = Bar(
            symbol="TX00",
            dt=datetime(2025, 1, 6, 8, 45),
            open=20000, high=20010, low=19910, close=20010,  # body=10, range=100
            volume=2000, interval=14400,
        )
        data_store.add_bar(pullback_bar)

        ctx = broker.context
        strategy.on_bar(pullback_bar, data_store, ctx)

        assert len(broker._pending_entries) == 1


class TestExitOrders:
    def test_exit_queued_when_in_position(self, strategy, broker, data_store):
        """Once in position, exit orders should be queued each bar."""
        base = 20000
        warmup = [(base, base + 10, base - 10, base) for _ in range(19)]
        bars = make_bars(warmup)
        for b in bars:
            data_store.add_bar(b)

        # Enter position
        entry_bar = Bar(
            symbol="TX00",
            dt=datetime(2025, 1, 6, 8, 45),
            open=20010, high=20110, low=20000, close=20100,
            volume=2000, interval=14400,
        )
        data_store.add_bar(entry_bar)
        ctx = broker.context
        strategy.on_bar(entry_bar, data_store, ctx)
        broker.on_bar_close(19, entry_bar.close)

        # Now in position -- next bar should queue exit
        broker._pending_exits.clear()
        next_bar = Bar(
            symbol="TX00",
            dt=datetime(2025, 1, 6, 12, 45),
            open=20110, high=20150, low=20090, close=20120,
            volume=1500, interval=14400,
        )
        data_store.add_bar(next_bar)
        strategy.on_bar(next_bar, data_store, ctx)

        assert len(broker._pending_exits) == 1
        exit_order = broker._pending_exits[0]
        assert exit_order.stop == entry_bar.low - 20  # sl = 20000 - 20 = 19980
        assert exit_order.limit is not None  # TP = upper - 50

    def test_stop_loss_price(self, strategy, broker, data_store):
        """Stop loss should be entry bar's low minus sl_offset."""
        base = 20000
        warmup = [(base, base + 10, base - 10, base) for _ in range(19)]
        bars = make_bars(warmup)
        for b in bars:
            data_store.add_bar(b)

        entry_bar = Bar(
            symbol="TX00",
            dt=datetime(2025, 1, 6, 8, 45),
            open=20010, high=20110, low=19990, close=20100,
            volume=2000, interval=14400,
        )
        data_store.add_bar(entry_bar)
        ctx = broker.context
        strategy.on_bar(entry_bar, data_store, ctx)

        assert strategy._sl_price == 19990 - 20  # 19970


class TestNoEntry:
    def test_insufficient_data(self, strategy, broker, data_store):
        """Should not trade with fewer than bb_period bars."""
        bar = Bar(
            symbol="TX00",
            dt=datetime(2025, 1, 2, 8, 45),
            open=20000, high=20100, low=19900, close=20050,
            volume=1000, interval=14400,
        )
        data_store.add_bar(bar)
        ctx = broker.context
        strategy.on_bar(bar, data_store, ctx)

        assert len(broker._pending_entries) == 0

    def test_no_pyramiding(self, strategy, broker, data_store):
        """Should not enter again while already in position."""
        base = 20000
        warmup = [(base, base + 10, base - 10, base) for _ in range(19)]
        bars = make_bars(warmup)
        for b in bars:
            data_store.add_bar(b)

        entry_bar = Bar(
            symbol="TX00",
            dt=datetime(2025, 1, 6, 8, 45),
            open=20010, high=20110, low=20000, close=20100,
            volume=2000, interval=14400,
        )
        data_store.add_bar(entry_bar)
        ctx = broker.context
        strategy.on_bar(entry_bar, data_store, ctx)
        broker.on_bar_close(19, entry_bar.close)

        # Clear and try again -- should not enter
        broker._pending_entries.clear()
        bar2 = Bar(
            symbol="TX00",
            dt=datetime(2025, 1, 6, 12, 45),
            open=20110, high=20210, low=20100, close=20200,
            volume=2000, interval=14400,
        )
        data_store.add_bar(bar2)
        strategy.on_bar(bar2, data_store, ctx)

        assert len(broker._pending_entries) == 0
