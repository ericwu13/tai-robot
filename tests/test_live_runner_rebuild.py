"""Tests for LiveRunner._rebuild_timeframe — cross-timeframe regime swaps.

The switching runner may swap between legs whose strategies run on
different timeframes. When that happens ``swap_strategy`` calls
``_rebuild_timeframe`` to re-aggregate the accumulated 1-min history to
the new interval before committing the swap.
"""

from datetime import datetime, timedelta

from src.market_data.models import Bar
from src.backtest.broker import OrderSide
from src.backtest.strategy import BacktestStrategy
from src.live.live_runner import LiveRunner, LiveState
from src.live.bar_aggregator import aggregate_bars


# ── Non-trading strategies (keep the broker flat so swap gates pass) ──


class NoopLong15m(BacktestStrategy):
    name = "NoopLong15m"
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        pass

    def required_bars(self):
        return 2


class NoopShort30m(BacktestStrategy):
    name = "NoopShort30m"
    kline_type = 0
    kline_minute = 30

    def on_bar(self, bar, data_store, broker):
        pass

    def required_bars(self):
        return 2


class NoopShort30mHighReq(BacktestStrategy):
    name = "NoopShort30mHighReq"
    kline_type = 0
    kline_minute = 30

    def on_bar(self, bar, data_store, broker):
        pass

    def required_bars(self):
        return 1000


def _kline(dt_str, o=22500, h=22510, l=22490, c=22505, v=100):
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    return f"{dt.strftime('%m/%d/%Y %H:%M')},{o},{h},{l},{c},{v}"


def _warmup_15m(count=20, base_date="2026-07-09"):
    lines = []
    for i in range(count):
        h = 9 + (i * 15) // 60
        m = (i * 15) % 60
        dt_str = f"{base_date} {h:02d}:{m:02d}"
        price = 22500 + i * 10
        lines.append(_kline(dt_str, price, price + 10, price - 5, price + 3, 50 + i))
    return lines


def _one_min_bars(start="2026-07-10 08:45", minutes=136):
    """Contiguous 1-min bars from *start* (default 08:45 → 11:00)."""
    base = datetime.strptime(start, "%Y-%m-%d %H:%M")
    bars = []
    for i in range(minutes):
        dt = base + timedelta(minutes=i)
        price = 22000 + i
        bars.append(Bar("TX00", dt, price, price + 8, price - 4, price + 2, 100 + i, 60))
    return bars


def _make_runner(tmp_path, feed_minutes=136):
    """A 15-min runner with ``feed_minutes`` of accumulated 1-min history."""
    runner = LiveRunner(NoopLong15m(), "TX00", log_dir=str(tmp_path))
    runner.feed_warmup_bars(_warmup_15m())  # → RUNNING
    runner._is_reloading = False
    for bar in _one_min_bars(minutes=feed_minutes):
        runner.feed_1m_bar(bar)
    return runner


class TestRebuildBoundaries:
    def test_rebuilt_bars_match_aggregate_bars(self, tmp_path):
        runner = _make_runner(tmp_path)
        bars_1m = list(runner._1m_bars)
        assert bars_1m  # sanity: 1-min history accumulated

        # aggregate_bars flushes the trailing partial as its LAST element;
        # the rebuild keeps that partial inside the aggregator instead.
        expected = aggregate_bars(bars_1m, 1800)
        assert len(expected) >= 3  # several complete bars + a partial

        ok, reason = runner.swap_strategy(NoopShort30m(), "NoopShort30m")
        assert ok is True, reason
        assert runner.target_interval == 1800

        # Completed bars == everything except the flushed partial.
        assert runner._aggregated_bars == expected[:-1]
        # data_store holds the same completed bars.
        assert runner.data_store.get_bars() == expected[:-1]
        # The trailing partial lives inside the new aggregator, un-flushed.
        assert runner.aggregator.get_partial_bar() == expected[-1]

    def test_next_1m_bar_continues_partial_not_double_counts(self, tmp_path):
        runner = _make_runner(tmp_path)
        ok, _ = runner.swap_strategy(NoopShort30m(), "NoopShort30m")
        assert ok
        completed_before = len(runner._aggregated_bars)
        partial_before = runner.aggregator.get_partial_bar()
        assert partial_before is not None

        # Feed the very next 1-min bar (11:00 was last → 11:01 continues
        # the 10:45–11:14 window, does NOT open a new aggregated bar).
        nxt = Bar("TX00", datetime(2026, 7, 10, 11, 1),
                  22200, 22210, 22190, 22205, 120, 60)
        runner.feed_1m_bar(nxt)
        assert len(runner._aggregated_bars) == completed_before  # no new bar
        partial_after = runner.aggregator.get_partial_bar()
        assert partial_after.volume == partial_before.volume + 120


class TestRebuildInsufficientHistory:
    def test_refused_and_state_untouched(self, tmp_path):
        runner = _make_runner(tmp_path)
        agg_before = runner.aggregator
        store_before = runner.data_store
        interval_before = runner.target_interval
        strat_before = runner.strategy

        ok, reason = runner.swap_strategy(
            NoopShort30mHighReq(), "NoopShort30mHighReq")
        assert ok is False
        assert "insufficient" in reason
        # No state committed by the refused rebuild.
        assert runner.aggregator is agg_before
        assert runner.data_store is store_before
        assert runner.target_interval == interval_before
        assert runner.strategy is strat_before

    def test_refused_when_no_1m_history(self, tmp_path):
        runner = _make_runner(tmp_path, feed_minutes=0)
        ok, reason = runner.swap_strategy(NoopShort30m(), "NoopShort30m")
        assert ok is False
        assert "insufficient" in reason


class TestRebuildBarIndexMonotonic:
    def test_bar_index_not_reset_across_rebuild(self, tmp_path):
        runner = _make_runner(tmp_path)
        idx_before = runner._bar_index
        assert idx_before > 0
        ok, _ = runner.swap_strategy(NoopShort30m(), "NoopShort30m")
        assert ok
        # Rebuild must NOT reset the running index (old broker trade
        # indices still reference it).
        assert runner._bar_index == idx_before

    def test_bar_index_continues_incrementing_after_rebuild(self, tmp_path):
        runner = _make_runner(tmp_path)
        ok, _ = runner.swap_strategy(NoopShort30m(), "NoopShort30m")
        assert ok
        idx_before = runner._bar_index
        # Feed enough 1-min bars to close the next 30-min boundary so a new
        # aggregated bar is processed and the index advances monotonically.
        base = datetime(2026, 7, 10, 11, 1)
        for i in range(30):
            dt = base + timedelta(minutes=i)
            runner.feed_1m_bar(
                Bar("TX00", dt, 22200, 22210, 22190, 22205, 100, 60))
        assert runner._bar_index > idx_before


class TestRebuildSameTFUnaffected:
    def test_same_timeframe_swap_does_not_rebuild(self, tmp_path):
        runner = _make_runner(tmp_path)
        agg_before = runner.aggregator
        store_before = runner.data_store

        class NoopLong15mB(BacktestStrategy):
            name = "NoopLong15mB"
            kline_type = 0
            kline_minute = 15

            def on_bar(self, bar, data_store, broker):
                pass

            def required_bars(self):
                return 2

        ok, _ = runner.swap_strategy(NoopLong15mB(), "NoopLong15mB")
        assert ok
        # Same TF → no rebuild: aggregator + store identity preserved.
        assert runner.aggregator is agg_before
        assert runner.data_store is store_before
        assert runner.target_interval == 900
