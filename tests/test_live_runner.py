"""Tests for LiveRunner: warmup, feed, dedup, strategy execution, stop."""

import os
from datetime import datetime

from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.backtest.broker import SimulatedBroker, BrokerContext, OrderSide
from src.backtest.strategy import BacktestStrategy
from src.live.live_runner import LiveRunner, LiveState, is_market_open
from src.live.bar_aggregator import aggregate_bars


# ── Simple test strategy ──

class AlwaysLongStrategy(BacktestStrategy):
    """Enter long on every bar (for testing)."""
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        if broker.position_size == 0:
            broker.entry("test_long", OrderSide.LONG)

    def required_bars(self) -> int:
        return 2


class LongWithExitStrategy(BacktestStrategy):
    """Enter long and set TP/SL exit (for testing tick-level exits)."""
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        if broker.position_size == 0:
            broker.entry("Long", OrderSide.LONG)
        else:
            broker.exit("Exit", "Long", limit=bar.close + 100, stop=bar.close - 50)

    def required_bars(self) -> int:
        return 2


class NeverTradeStrategy(BacktestStrategy):
    """Does nothing (for testing warmup/feed without trades)."""
    kline_type = 0
    kline_minute = 240

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        pass

    def required_bars(self) -> int:
        return 5


# ── Helpers ──

def _kline(dt_str, o=22500, h=22510, l=22490, c=22505, v=100):
    """Create a KLine string in Capital API format: MM/DD/YYYY HH:MM,O,H,L,C,V"""
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    return f"{dt.strftime('%m/%d/%Y %H:%M')},{o},{h},{l},{c},{v}"


def _klines_1m(base_date, start_min, count, base_price=22500):
    """Generate count 1-min KLine strings starting from start_min."""
    lines = []
    for i in range(count):
        m = start_min + i
        h = m // 60
        mm = m % 60
        dt_str = f"{base_date} {h:02d}:{mm:02d}"
        price = base_price + i
        lines.append(_kline(dt_str, price, price+10, price-5, price+3, 50+i))
    return lines


# ── Tests ──

class TestLiveRunnerWarmup:
    def test_warmup_populates_datastore(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))

        assert runner.state == LiveState.IDLE

        # Feed warmup bars (H4 bars in KLine format)
        warmup = [
            _kline("2026-02-25 09:00", 22000, 22100, 21900, 22050, 500),
            _kline("2026-02-25 12:00", 22050, 22150, 22000, 22100, 400),
            _kline("2026-02-26 09:00", 22100, 22200, 22050, 22180, 600),
            _kline("2026-02-26 12:00", 22180, 22250, 22100, 22200, 350),
            _kline("2026-02-27 09:00", 22200, 22300, 22150, 22280, 450),
        ]
        count = runner.feed_warmup_bars(warmup)

        assert count == 5
        assert runner.state == LiveState.RUNNING
        assert len(runner.data_store) == 5

    def test_get_warmup_params(self):
        strategy = AlwaysLongStrategy()  # 15-min
        runner = LiveRunner(strategy, "TX00")
        params = runner.get_warmup_params()

        assert params["kline_type"] == 0
        assert params["kline_minute"] == 15
        assert params["interval"] == 900
        assert params["days_back"] > 0


class TestLiveRunnerFeed:
    def test_feed_1m_aggregates_to_15m(self, tmp_path):
        strategy = AlwaysLongStrategy()  # 15-min
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))

        # Warmup with enough 15-min bars
        warmup = [
            _kline("2026-02-28 09:00", 22000, 22100, 21900, 22050, 500),
            _kline("2026-02-28 09:15", 22050, 22150, 22000, 22100, 400),
        ]
        runner.feed_warmup_bars(warmup)

        # Feed 15 one-minute bars (09:00-09:14) — should NOT emit aggregated bar yet
        lines = _klines_1m("2026-03-01", start_min=540, count=15)  # 09:00-09:14
        completed = runner.feed_1m_bars(lines)
        assert len(completed) == 0

        # Feed 09:15 — crosses 15-min boundary, emits the 09:00 bar
        lines2 = [_kline("2026-03-01 09:15", 22515, 22525, 22510, 22520, 65)]
        completed2 = runner.feed_1m_bars(lines2)
        assert len(completed2) == 1
        assert completed2[0].dt == datetime(2026, 3, 1, 9, 0)
        assert completed2[0].interval == 900

    def test_dedup_overlapping_polls(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        # First poll: bars at 09:00, 09:01, 09:02
        lines1 = _klines_1m("2026-03-01", 540, 3)
        runner.feed_1m_bars(lines1)

        # Second poll overlaps: 09:01, 09:02, 09:03
        lines2 = _klines_1m("2026-03-01", 541, 3)
        runner.feed_1m_bars(lines2)

        # Should have seen 4 unique bars, not 6
        assert runner.get_status()["bars_1m"] == 4

    def test_feed_before_running_ignored(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        # Still IDLE, not RUNNING
        lines = _klines_1m("2026-03-01", 540, 5)
        completed = runner.feed_1m_bars(lines)
        assert completed == []

    def test_csv_files_created(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        lines = _klines_1m("2026-03-01", 540, 3)
        runner.feed_1m_bars(lines)
        runner.stop()

        assert (tmp_path / "bars_1m_20260301.csv").exists()

    def test_feed_1m_bar_accepts_bar_objects(self, tmp_path):
        """feed_1m_bar() accepts Bar objects directly (for tick-based feed)."""
        strategy = AlwaysLongStrategy()  # 15-min
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))

        warmup = [
            _kline("2026-02-28 09:00", 22000, 22100, 21900, 22050, 500),
            _kline("2026-02-28 09:15", 22050, 22150, 22000, 22100, 400),
        ]
        runner.feed_warmup_bars(warmup)

        # Feed 15 individual Bar objects (09:00-09:14)
        for i in range(15):
            m = 540 + i  # 09:00 + i minutes
            h = m // 60
            mm = m % 60
            bar = Bar(
                symbol="TX00",
                dt=datetime(2026, 3, 1, h, mm),
                open=22500 + i, high=22510 + i, low=22490 + i,
                close=22505 + i, volume=50 + i, interval=60,
            )
            result = runner.feed_1m_bar(bar)
            assert result is None  # no aggregated bar yet

        # Feed 09:15 — crosses 15-min boundary
        bar_15 = Bar(
            symbol="TX00",
            dt=datetime(2026, 3, 1, 9, 15),
            open=22515, high=22525, low=22510, close=22520,
            volume=65, interval=60,
        )
        agg = runner.feed_1m_bar(bar_15)
        assert agg is not None
        assert agg.dt == datetime(2026, 3, 1, 9, 0)
        assert agg.interval == 900

    def test_feed_1m_bar_dedup(self, tmp_path):
        """feed_1m_bar() deduplicates bars with same datetime."""
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        bar = Bar(
            symbol="TX00", dt=datetime(2026, 3, 1, 9, 0),
            open=22500, high=22510, low=22490, close=22505,
            volume=100, interval=60,
        )
        runner.feed_1m_bar(bar)
        runner.feed_1m_bar(bar)  # duplicate
        assert runner.get_status()["bars_1m"] == 1

    def test_feed_1m_bar_before_running_returns_none(self, tmp_path):
        """feed_1m_bar() returns None when state is not RUNNING."""
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        bar = Bar(
            symbol="TX00", dt=datetime(2026, 3, 1, 9, 0),
            open=22500, high=22510, low=22490, close=22505,
            volume=100, interval=60,
        )
        result = runner.feed_1m_bar(bar)
        assert result is None


class TestLiveRunnerStrategy:
    def test_strategy_runs_on_aggregated_bar(self, tmp_path):
        strategy = AlwaysLongStrategy()  # requires 2 bars, enters long
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))

        # Warmup with 2 bars (meets required_bars)
        warmup = [
            _kline("2026-02-28 09:00", 22000, 22100, 21900, 22050, 500),
            _kline("2026-02-28 09:15", 22050, 22150, 22000, 22100, 400),
        ]
        runner.feed_warmup_bars(warmup)

        # Feed 1-min bars spanning one 15-min period + boundary cross
        lines = _klines_1m("2026-03-01", 540, 16)  # 09:00-09:15
        completed = runner.feed_1m_bars(lines)

        # After aggregated bar emitted, strategy should have triggered entry
        assert len(completed) == 1
        # Position should be open (filled at bar close)
        assert runner.broker.position_size > 0

    def test_callbacks_fire(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        received_bars = []
        runner.on("on_1m_bar", lambda b: received_bars.append(b))

        lines = _klines_1m("2026-03-01", 540, 3)
        runner.feed_1m_bars(lines)

        assert len(received_bars) == 3


class TestLiveRunnerStop:
    def test_stop_returns_summary(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        summary = runner.stop()
        assert runner.state == LiveState.STOPPED
        assert "trades" in summary
        assert "pnl" in summary
        assert "bars_1m" in summary

    def test_stop_force_closes_position(self, tmp_path):
        strategy = AlwaysLongStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))

        warmup = [
            _kline("2026-02-28 09:00", 22000, 22100, 21900, 22050, 500),
            _kline("2026-02-28 09:15", 22050, 22150, 22000, 22100, 400),
        ]
        runner.feed_warmup_bars(warmup)

        # Trigger entry via aggregated bar
        lines = _klines_1m("2026-03-01", 540, 16)
        runner.feed_1m_bars(lines)
        assert runner.broker.position_size > 0

        summary = runner.stop()
        assert runner.broker.position_size == 0
        assert summary["trades"] >= 1

    def test_double_stop_safe(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))
        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        runner.stop()
        summary2 = runner.stop()
        assert summary2 is not None


class TestLiveRunnerResults:
    def test_get_result_compatible(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))
        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        result = runner.get_result()
        assert result.strategy_name == "NeverTradeStrategy"
        assert result.bars_processed == 5
        assert hasattr(result, "trades")
        assert hasattr(result, "equity_curve")
        assert hasattr(result, "metrics")

    def test_get_status(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))
        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        status = runner.get_status()
        assert status["state"] == "RUNNING"
        assert status["position"] == "Flat"
        assert status["trades"] == 0
        assert status["pnl"] == 0
        assert isinstance(status["market_open"], bool)


class TestMarketHours:
    """Use 2026-03-02 (Monday) and 2026-03-03 (Tuesday) for weekday tests."""

    def test_am_session(self):
        assert is_market_open(datetime(2026, 3, 2, 9, 0))   # Mon
        assert is_market_open(datetime(2026, 3, 2, 8, 45))
        assert is_market_open(datetime(2026, 3, 2, 13, 44))

    def test_am_closed(self):
        assert not is_market_open(datetime(2026, 3, 2, 13, 45))
        assert not is_market_open(datetime(2026, 3, 2, 14, 0))
        assert not is_market_open(datetime(2026, 3, 2, 14, 59))

    def test_pm_session(self):
        assert is_market_open(datetime(2026, 3, 2, 15, 0))   # Mon PM
        assert is_market_open(datetime(2026, 3, 2, 20, 0))
        assert is_market_open(datetime(2026, 3, 2, 23, 59))

    def test_night_session(self):
        # Tue after midnight (night carryover from Mon PM session)
        assert is_market_open(datetime(2026, 3, 3, 0, 0))
        assert is_market_open(datetime(2026, 3, 3, 3, 0))
        assert is_market_open(datetime(2026, 3, 3, 4, 59))

    def test_early_morning_closed(self):
        assert not is_market_open(datetime(2026, 3, 3, 5, 0))   # Tue
        assert not is_market_open(datetime(2026, 3, 3, 6, 0))
        assert not is_market_open(datetime(2026, 3, 3, 8, 44))

    def test_monday_no_night_carryover(self):
        # Monday before 05:00 — no night session from Sunday
        assert not is_market_open(datetime(2026, 3, 2, 3, 0))
        assert not is_market_open(datetime(2026, 3, 2, 4, 59))

    def test_saturday_night_carryover(self):
        # Saturday 00:00-04:59 — Friday night session still running
        assert is_market_open(datetime(2026, 3, 7, 0, 30))   # Sat
        assert is_market_open(datetime(2026, 3, 7, 4, 59))

    def test_saturday_after_close(self):
        assert not is_market_open(datetime(2026, 3, 7, 5, 0))   # Sat
        assert not is_market_open(datetime(2026, 3, 7, 9, 0))
        assert not is_market_open(datetime(2026, 3, 7, 15, 0))

    def test_sunday_fully_closed(self):
        assert not is_market_open(datetime(2026, 3, 8, 0, 0))   # Sun
        assert not is_market_open(datetime(2026, 3, 8, 9, 0))
        assert not is_market_open(datetime(2026, 3, 8, 20, 0))


# ── Helper for generating Bar objects ──

def _make_1m_bars(base_date, start_min, count, symbol="TX00", base_price=22500):
    """Generate count 1-min Bar objects starting from start_min (minutes since midnight)."""
    bars = []
    for i in range(count):
        m = start_min + i
        h = m // 60
        mm = m % 60
        price = base_price + i
        bars.append(Bar(
            symbol=symbol,
            dt=datetime(int(base_date[:4]), int(base_date[5:7]), int(base_date[8:10]), h, mm),
            open=price, high=price + 10, low=price - 5, close=price + 3,
            volume=50 + i, interval=60,
        ))
    return bars


class TestAggregateBars:
    def test_aggregate_bars_empty(self):
        result = aggregate_bars([], 900)
        assert result == []

    def test_aggregate_bars_1m_passthrough(self):
        bars = _make_1m_bars("2026-03-01", 540, 5)
        result = aggregate_bars(bars, 60)
        assert len(result) == 5
        # Should be a copy, not the same list
        assert result is not bars
        assert result[0].dt == bars[0].dt

    def test_aggregate_bars_1m_to_15m(self):
        # 30 bars from 09:00-09:29 → 2 completed 15-min bars (09:00, 09:15)
        bars = _make_1m_bars("2026-03-01", 540, 30)
        result = aggregate_bars(bars, 900)
        # 09:00-09:14 = 1 bar, 09:15-09:29 = 1 bar (flushed as partial)
        assert len(result) == 2
        assert result[0].dt == datetime(2026, 3, 1, 9, 0)
        assert result[0].interval == 900
        assert result[1].dt == datetime(2026, 3, 1, 9, 15)

    def test_aggregate_bars_1m_to_1h(self):
        # 120 bars from 09:00-10:59 → 2 completed hourly bars
        bars = _make_1m_bars("2026-03-01", 540, 120)
        result = aggregate_bars(bars, 3600)
        assert len(result) == 2
        assert result[0].dt == datetime(2026, 3, 1, 9, 0)
        assert result[1].dt == datetime(2026, 3, 1, 10, 0)


class TestLiveRunner1mBars:
    def test_1m_bars_stored(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        lines = _klines_1m("2026-03-01", 540, 5)
        runner.feed_1m_bars(lines)

        stored = runner.get_1m_bars()
        assert len(stored) == 5
        assert stored[0].dt == datetime(2026, 3, 1, 9, 0)

    def test_get_bars_at_native_interval(self, tmp_path):
        """get_bars_at_interval(native) returns same as get_bars()."""
        strategy = NeverTradeStrategy()  # H4 (target_interval=14400)
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        native = runner.get_bars()
        at_interval = runner.get_bars_at_interval(runner.target_interval)
        assert len(native) == len(at_interval)
        for a, b in zip(native, at_interval):
            assert a.dt == b.dt

    def test_get_bars_at_different_interval(self, tmp_path):
        """get_bars_at_interval(900) re-aggregates 1m bars to 15-min."""
        strategy = NeverTradeStrategy()  # H4
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        # Feed 30 one-minute bars (09:00-09:29)
        lines = _klines_1m("2026-03-01", 540, 30)
        runner.feed_1m_bars(lines)

        bars_15m = runner.get_bars_at_interval(900)
        assert len(bars_15m) == 2
        assert bars_15m[0].dt == datetime(2026, 3, 1, 9, 0)
        assert bars_15m[0].interval == 900
        assert bars_15m[1].dt == datetime(2026, 3, 1, 9, 15)


class TestLiveRunnerLock:
    def test_acquire_and_release_lock(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path),
                            bot_name="TestBot")
        runner.acquire_lock()
        assert os.path.isfile(runner._lock_path)

        is_locked, pid = LiveRunner.check_lock(runner.bot_dir)
        assert is_locked
        assert pid == os.getpid()

        runner.release_lock()
        is_locked, _ = LiveRunner.check_lock(runner.bot_dir)
        assert not is_locked

    def test_stop_releases_lock(self, tmp_path):
        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path),
                            bot_name="TestBot")
        runner.acquire_lock()

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)
        runner.stop()

        assert not os.path.isfile(runner._lock_path)

    def test_dead_pid_not_locked(self, tmp_path):
        """A lock file with a non-existent PID is not considered locked."""
        bot_dir = os.path.join(str(tmp_path), "TX00_TestBot")
        os.makedirs(bot_dir, exist_ok=True)
        lock_path = os.path.join(bot_dir, ".lock")
        with open(lock_path, "w") as f:
            f.write("999999999")  # non-existent PID

        is_locked, pid = LiveRunner.check_lock(bot_dir)
        assert not is_locked
        assert pid == 999999999

    def test_bot_dir_for(self, tmp_path):
        result = LiveRunner.bot_dir_for(str(tmp_path), "TX00", "MyBot")
        assert result == os.path.join(str(tmp_path), "TX00_MyBot")


class TestCheckTickExit:
    """Tests for LiveRunner.check_tick_exit() — real-time TP/SL on every tick."""

    def _make_runner_with_position(self, tmp_path, entry_price=22500,
                                   tp_price=22600, sl_price=22450):
        """Create a LiveRunner in RUNNING state with an open position and pending exits."""
        strategy = LongWithExitStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200, log_dir=str(tmp_path))

        # Warmup (transitions state to RUNNING)
        warmup = [
            _kline("2026-02-25 08:45", 22400, 22500, 22400, 22490, 100),
            _kline("2026-02-25 09:00", 22490, 22510, 22480, 22500, 100),
        ]
        runner.feed_warmup_bars(warmup)
        assert runner.state == LiveState.RUNNING

        # Feed bars to trigger entry and exit order
        bar1 = Bar(symbol="TX00", dt=datetime(2026, 2, 25, 9, 15),
                   open=entry_price, high=entry_price+10,
                   low=entry_price-10, close=entry_price,
                   volume=100, interval=900)
        runner._process_aggregated_bar(bar1)
        assert runner.broker.position_size == 1

        bar2 = Bar(symbol="TX00", dt=datetime(2026, 2, 25, 9, 30),
                   open=entry_price+5, high=entry_price+10,
                   low=entry_price-5, close=entry_price+5,
                   volume=100, interval=900)
        runner._process_aggregated_bar(bar2)
        # Strategy queued exit with TP/SL on bar2
        assert len(runner.broker._pending_exits) > 0

        # Override exit prices for precise testing
        runner.broker._pending_exits[0].limit = tp_price
        runner.broker._pending_exits[0].stop = sl_price

        return runner

    def test_tp_fills_at_tick_price(self, tmp_path):
        """Tick price >= TP limit should trigger immediate exit at TP price."""
        runner = self._make_runner_with_position(tmp_path, tp_price=22600, sl_price=22450)

        result = runner.check_tick_exit(22610, "2026-02-25 09:31:15")
        assert result is not None
        assert result["price"] == 22600  # fills at limit, not tick price
        assert runner.broker.position_size == 0
        assert runner.broker.trades[-1].pnl > 0

    def test_sl_fills_at_tick_price(self, tmp_path):
        """Tick price <= SL stop should trigger immediate exit at SL price."""
        runner = self._make_runner_with_position(tmp_path, tp_price=22600, sl_price=22450)

        result = runner.check_tick_exit(22440, "2026-02-25 09:31:30")
        assert result is not None
        assert result["price"] == 22450  # fills at stop, not tick price
        assert runner.broker.position_size == 0
        assert runner.broker.trades[-1].pnl < 0

    def test_no_exit_when_price_between_tp_sl(self, tmp_path):
        """Tick price between SL and TP should not trigger exit."""
        runner = self._make_runner_with_position(tmp_path, tp_price=22600, sl_price=22450)

        result = runner.check_tick_exit(22520, "2026-02-25 09:31:45")
        assert result is None
        assert runner.broker.position_size == 1

    def test_no_exit_when_flat(self, tmp_path):
        """check_tick_exit should return None when no position open."""
        strategy = LongWithExitStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200, log_dir=str(tmp_path))
        warmup = [
            _kline("2026-02-25 08:45", 22400, 22500, 22400, 22490, 100),
            _kline("2026-02-25 09:00", 22490, 22510, 22480, 22500, 100),
        ]
        runner.feed_warmup_bars(warmup)

        result = runner.check_tick_exit(22500, "2026-02-25 09:00:00")
        assert result is None

    def test_no_exit_when_not_running(self, tmp_path):
        """check_tick_exit should return None when runner is not RUNNING."""
        strategy = LongWithExitStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200, log_dir=str(tmp_path))
        assert runner.state == LiveState.IDLE

        result = runner.check_tick_exit(22500)
        assert result is None

    def test_tick_exit_clears_pending_exits(self, tmp_path):
        """After tick exit triggers, pending exits should be cleared."""
        runner = self._make_runner_with_position(tmp_path, tp_price=22600, sl_price=22450)
        assert len(runner.broker._pending_exits) > 0

        runner.check_tick_exit(22610, "2026-02-25 09:32:00")
        assert len(runner.broker._pending_exits) == 0
