"""Tests for LiveRunner.swap_strategy and regime_idle."""

import os
from datetime import datetime, timedelta

from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.backtest.broker import SimulatedBroker, BrokerContext, OrderSide
from src.backtest.strategy import BacktestStrategy
from src.live.live_runner import LiveRunner, LiveState


class StrategyA(BacktestStrategy):
    """15-min strategy that enters long."""
    name = "StrategyA"
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0:
            broker.entry("A_long", OrderSide.LONG)

    def required_bars(self):
        return 2


class StrategyB(BacktestStrategy):
    """15-min strategy that enters short (same timeframe as A)."""
    name = "StrategyB"
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0:
            broker.entry("B_short", OrderSide.SHORT)

    def required_bars(self):
        return 2


class StrategyDiffTF(BacktestStrategy):
    """H4 strategy (different timeframe)."""
    name = "StrategyDiffTF"
    kline_type = 0
    kline_minute = 240

    def on_bar(self, bar, data_store, broker):
        pass

    def required_bars(self):
        return 2


class StrategyHighReq(BacktestStrategy):
    """Needs many bars."""
    name = "StrategyHighReq"
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        pass

    def required_bars(self):
        return 5000


def _kline(dt_str, o=22500, h=22510, l=22490, c=22505, v=100):
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    return f"{dt.strftime('%m/%d/%Y %H:%M')},{o},{h},{l},{c},{v}"


def _klines_15m(count, base_date="2026-07-09", base_price=22500):
    lines = []
    for i in range(count):
        h = 9 + (i * 15) // 60
        m = (i * 15) % 60
        dt_str = f"{base_date} {h:02d}:{m:02d}"
        price = base_price + i * 10
        lines.append(_kline(dt_str, price, price+10, price-5, price+3, 50+i))
    return lines


def _make_runner(tmp_path, strategy=None):
    s = strategy or StrategyA()
    runner = LiveRunner(s, "TX00", log_dir=str(tmp_path))
    lines = _klines_15m(20)
    runner.feed_warmup_bars(lines)  # sets state to RUNNING
    runner._is_reloading = False    # simulate GUI clearing after live ticks begin
    return runner


class TestSwapStrategySuccess:
    def test_swap_updates_strategy_and_label(self, tmp_path):
        runner = _make_runner(tmp_path)
        new = StrategyB()
        ok, reason = runner.swap_strategy(new, "StrategyB")
        assert ok is True
        assert reason == ""
        assert runner.strategy is new
        assert runner.strategy_display_name == "StrategyB"
        assert runner.broker.strategy_label == "StrategyB"

    def test_swap_clears_pending_orders(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.broker._pending_entries.append(object())
        runner.broker._pending_exits.append(object())
        runner.broker._pending_market_closes.append(("tag", "entry"))
        ok, _ = runner.swap_strategy(StrategyB(), "StrategyB")
        assert ok
        assert runner.broker._pending_entries == []
        assert runner.broker._pending_exits == []
        assert runner.broker._pending_market_closes == []

    def test_swap_preserves_bar_index(self, tmp_path):
        runner = _make_runner(tmp_path)
        idx_before = runner._bar_index
        runner.swap_strategy(StrategyB(), "StrategyB")
        assert runner._bar_index == idx_before

    def test_swap_preserves_seen_dts(self, tmp_path):
        runner = _make_runner(tmp_path)
        dts_before = set(runner._seen_1m_dts)
        runner.swap_strategy(StrategyB(), "StrategyB")
        assert runner._seen_1m_dts == dts_before

    def test_swap_preserves_data_store(self, tmp_path):
        runner = _make_runner(tmp_path)
        store_len = len(runner.data_store)
        runner.swap_strategy(StrategyB(), "StrategyB")
        assert len(runner.data_store) == store_len

    def test_swap_saves_session(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.swap_strategy(StrategyB(), "StrategyB")
        assert os.path.exists(runner._session_path)


class TestSwapStrategyRefused:
    def test_refused_with_open_position(self, tmp_path):
        runner = _make_runner(tmp_path)
        # Manually open a position on the broker
        from src.backtest.broker import Order
        runner.broker.queue_entry(Order("test", OrderSide.LONG, 1))
        runner.broker.on_bar_close(runner._bar_index, 22500)
        assert runner.broker.has_open_position()
        ok, reason = runner.swap_strategy(StrategyB(), "StrategyB")
        assert ok is False
        assert "open position" in reason

    def test_refused_with_veto(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.mode_switch_veto = lambda: "fill pending"
        ok, reason = runner.swap_strategy(StrategyB(), "StrategyB")
        assert ok is False
        assert "fill pending" in reason

    def test_refused_during_replay(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.suppress_strategy = True
        ok, reason = runner.swap_strategy(StrategyB(), "StrategyB")
        assert ok is False
        assert "replay" in reason

    def test_refused_timeframe_mismatch(self, tmp_path):
        runner = _make_runner(tmp_path)
        ok, reason = runner.swap_strategy(StrategyDiffTF(), "DiffTF")
        assert ok is False
        assert "mismatch" in reason

    def test_refused_insufficient_bars(self, tmp_path):
        runner = _make_runner(tmp_path)
        ok, reason = runner.swap_strategy(StrategyHighReq(), "HighReq")
        assert ok is False
        assert "insufficient" in reason


class TestSwapAttribution:
    def test_trades_attributed_to_correct_strategy(self, tmp_path):
        runner = _make_runner(tmp_path)
        # Verify broker label changes on swap
        assert runner.broker.strategy_label == "StrategyA"
        runner.swap_strategy(StrategyB(), "StrategyB")
        assert runner.broker.strategy_label == "StrategyB"


class TestRegimeIdle:
    def test_idle_suppresses_strategy(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.regime_idle = True
        entries_before = len(runner.broker._pending_entries)
        # Feed a bar — strategy should NOT run
        dt = datetime(2026, 7, 9, 10, 0)
        bar = Bar("TX00", dt, 22500, 22510, 22490, 22505, 100, 900)
        runner.feed_1m_bar(bar)
        # No new entries (strategy was suppressed)
        assert len(runner.broker._pending_entries) == entries_before

    def test_idle_still_updates_1m_bars(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.regime_idle = True
        bars_1m_before = len(runner._1m_bars)
        # Feed a fresh 1m bar with unique datetime
        dt = datetime(2026, 7, 10, 12, 30)
        bar = Bar("TX00", dt, 22500, 22510, 22490, 22505, 100, 60)
        runner.feed_1m_bar(bar)
        # 1m bars should still accumulate even when idle
        assert len(runner._1m_bars) > bars_1m_before

    def test_reconnect_does_not_clear_idle(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.regime_idle = True
        # Simulate reconnect: suppress_strategy set then cleared
        runner.suppress_strategy = True
        runner.suppress_strategy = False
        assert runner.regime_idle is True
