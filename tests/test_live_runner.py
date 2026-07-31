"""Tests for LiveRunner: warmup, feed, dedup, strategy execution, stop."""

import os
import subprocess
import sys
from datetime import datetime, timedelta

from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.backtest.broker import SimulatedBroker, BrokerContext, OrderSide
from src.backtest.strategy import BacktestStrategy
from src.live.live_runner import LiveRunner, LiveState, is_market_open
from src.live.bar_aggregator import BarAggregator, aggregate_bars


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


class OneMinRecordingStrategy(BacktestStrategy):
    """1-min strategy that records each bar it receives (for issue #44 test)."""
    kline_type = 0
    kline_minute = 1

    def __init__(self) -> None:
        self.seen_bars: list[Bar] = []

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        self.seen_bars.append(bar)

    def required_bars(self) -> int:
        return 1


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
        # 120 bars from 08:45-10:44 → session-aligned 1H boundaries at 08:45, 09:45
        bars = _make_1m_bars("2026-03-02", 525, 120)  # 525 min = 08:45
        result = aggregate_bars(bars, 3600)
        assert len(result) == 2
        assert result[0].dt == datetime(2026, 3, 2, 8, 45)
        assert result[1].dt == datetime(2026, 3, 2, 9, 45)


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

    @staticmethod
    def _write_lock(tmp_path, content) -> tuple[str, str]:
        bot_dir = os.path.join(str(tmp_path), "TX00_TestBot")
        os.makedirs(bot_dir, exist_ok=True)
        lock_path = os.path.join(bot_dir, ".lock")
        with open(lock_path, "w") as f:
            f.write(str(content))
        return bot_dir, lock_path

    def test_dead_pid_not_locked(self, tmp_path):
        """A lock file with a non-existent PID is not considered locked."""
        bot_dir, lock_path = self._write_lock(tmp_path, 999999999)

        is_locked, pid = LiveRunner.check_lock(bot_dir)
        assert not is_locked
        assert pid == 999999999
        # Stale lock is self-healed (deleted) so it can't block later deploys
        assert not os.path.isfile(lock_path)

    def test_really_dead_pid_not_locked(self, tmp_path):
        """A PID that WAS a real process on this console is not locked.

        Regression test for the stale-lock false conflict: os.kill(pid, 0)
        on Windows is GenerateConsoleCtrlEvent, which keeps succeeding for
        a dead child that shared our console. A fabricated PID (previous
        test) does not catch that — the process must have really existed.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        proc.kill()
        proc.wait()
        bot_dir, lock_path = self._write_lock(tmp_path, proc.pid)

        is_locked, pid = LiveRunner.check_lock(bot_dir)
        assert not is_locked
        assert pid == proc.pid
        assert not os.path.isfile(lock_path)

    def test_live_foreign_pid_is_locked(self, tmp_path):
        """A running process that is not ours still counts as locked."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            bot_dir, lock_path = self._write_lock(tmp_path, proc.pid)

            is_locked, pid = LiveRunner.check_lock(bot_dir)
            assert is_locked
            assert pid == proc.pid
            assert os.path.isfile(lock_path)  # live lock is left alone
        finally:
            proc.kill()
            proc.wait()

    def test_corrupt_lock_not_locked(self, tmp_path):
        """Unreadable lock content is treated as stale and cleaned up."""
        bot_dir, lock_path = self._write_lock(tmp_path, "not-a-pid")

        is_locked, pid = LiveRunner.check_lock(bot_dir)
        assert not is_locked
        assert pid == 0
        assert not os.path.isfile(lock_path)

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
        """Tick price <= SL stop should trigger exit at the actual tick price."""
        runner = self._make_runner_with_position(tmp_path, tp_price=22600, sl_price=22450)

        result = runner.check_tick_exit(22440, "2026-02-25 09:31:30")
        assert result is not None
        assert result["price"] == 22440  # fills at tick (market) price, not stop level
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

    def test_sl_with_float_stop_fills_at_integer_tick(self, tmp_path):
        """ATR-computed float stop (e.g. 22450.7) must fill at the integer
        tick price, not the rounded float. Regression test for #38."""
        runner = self._make_runner_with_position(tmp_path, tp_price=22600, sl_price=22450)
        # Simulate an ATR-based float stop level
        runner.broker._pending_exits[0].stop = 22450.73

        # Tick at 22450 triggers the stop (22450 <= 22450.73)
        result = runner.check_tick_exit(22450, "2026-02-25 09:31:30")
        assert result is not None
        assert result["price"] == 22450  # actual tick, not 22451 (rounded float)
        assert runner.broker.trades[-1].exit_price == 22450

    def test_no_duplicate_trade_close_after_tick_exit(self, tmp_path):
        """After a tick exit closes the position, the next aggregated bar
        must NOT fire a duplicate TRADE_CLOSE. Regression test for #37/#38.

        Root cause: check_tick_exit used _bar_index (next bar) instead of
        _bar_index-1 (current bar), so _check_for_trade_close matched the
        next bar and logged a ghost TRADE_CLOSE.
        """
        runner = self._make_runner_with_position(tmp_path, tp_price=22600, sl_price=22450)

        decisions = []
        runner.on("on_decision", lambda d: decisions.append(d))

        # Tick exit closes the position
        result = runner.check_tick_exit(22440, "2026-02-25 09:32:00")
        assert result is not None
        assert runner.broker.position_size == 0

        trade_closes_before = len([d for d in decisions if d["action"] == "TRADE_CLOSE"])
        assert trade_closes_before == 1  # one TRADE_CLOSE from tick exit

        # Process the NEXT aggregated bar — must NOT fire another TRADE_CLOSE
        bar3 = Bar(symbol="TX00", dt=datetime(2026, 2, 25, 9, 45),
                   open=22440, high=22460, low=22430, close=22455,
                   volume=100, interval=900)
        runner._process_aggregated_bar(bar3)

        trade_closes_after = len([d for d in decisions if d["action"] == "TRADE_CLOSE"])
        assert trade_closes_after == 1, (
            f"Expected 1 TRADE_CLOSE, got {trade_closes_after} — "
            f"duplicate fired on next bar"
        )

    def test_strategy_can_enter_after_tick_exit_on_next_bar(self, tmp_path):
        """After tick exit, strategy should be able to enter on the next bar
        without interference from a ghost TRADE_CLOSE."""
        runner = self._make_runner_with_position(tmp_path, tp_price=22600, sl_price=22450)

        decisions = []
        runner.on("on_decision", lambda d: decisions.append(d))

        # Tick exit
        runner.check_tick_exit(22440, "2026-02-25 09:32:00")
        assert runner.broker.position_size == 0

        # Next bar — strategy should enter (LongWithExitStrategy enters when flat)
        bar3 = Bar(symbol="TX00", dt=datetime(2026, 2, 25, 9, 45),
                   open=22450, high=22470, low=22440, close=22460,
                   volume=100, interval=900)
        runner._process_aggregated_bar(bar3)

        # Should have: 1 TRADE_CLOSE (tick) + 1 ENTRY_FILL (new bar)
        actions = [d["action"] for d in decisions]
        assert actions.count("TRADE_CLOSE") == 1, f"Ghost TRADE_CLOSE: {actions}"
        assert "ENTRY_FILL" in actions, f"Strategy didn't re-enter: {actions}"
        assert runner.broker.position_size == 1


# ── Regression: issue #44 — 1-min strategy and chart lag ──

class TestLiveRunner1mNoLagIssue44:
    """Regression tests for issue #44.

    Before the fix, BarAggregator held each 1-min bar as _current and only
    emitted it when the NEXT 1-min bar arrived. For a 1-min strategy this
    meant:
      - Strategy ran on bar[N-1] when bar[N] arrived (1-min data staleness)
      - get_bars() lagged 1 bar behind
      - Live chart showed bar[N-2] when wall clock was on bar[N] (2 min lag)
    """

    def test_1m_strategy_runs_on_current_bar(self, tmp_path):
        """Strategy must receive each 1-min bar immediately, not 1 bar late."""
        strategy = OneMinRecordingStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))
        assert runner.target_interval == 60

        # feed_warmup_bars only seeds the data_store; it doesn't call
        # strategy.on_bar. After warmup, state transitions IDLE→RUNNING so
        # feed_1m_bar is accepted.
        runner.feed_warmup_bars([_kline("2026-02-28 09:00", 22500)])
        assert len(strategy.seen_bars) == 0  # warmup doesn't invoke strategy

        # Feed 3 live 1-min bars
        for i in range(3):
            bar = Bar(
                symbol="TX00", dt=datetime(2026, 3, 1, 9, i),
                open=22500 + i, high=22510 + i, low=22490 + i,
                close=22505 + i, volume=50, interval=60,
            )
            result = runner.feed_1m_bar(bar)
            # Each 1-min bar must be returned as an aggregated bar (pass-through)
            assert result is not None, (
                f"bar {i} not emitted — 1-min aggregator is lagging")
            assert result.dt == datetime(2026, 3, 1, 9, i)

        # Strategy must have received all 3 live bars on the SAME iteration
        # as the bar arrived — no 1-bar lag.
        assert len(strategy.seen_bars) == 3
        assert [b.dt.minute for b in strategy.seen_bars] == [0, 1, 2]
        # Bar values are the current bar, not the previous one
        assert strategy.seen_bars[-1].close == 22507

    def test_1m_get_bars_includes_latest(self, tmp_path):
        """get_bars() must include the just-fed 1-min bar, not lag by 1."""
        strategy = OneMinRecordingStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))

        runner.feed_warmup_bars([_kline("2026-02-28 09:00", 22500)])
        warmup_count = len(runner.get_bars())

        # Feed a single live 1-min bar
        bar = Bar(
            symbol="TX00", dt=datetime(2026, 3, 1, 9, 0),
            open=22600, high=22610, low=22590, close=22605,
            volume=50, interval=60,
        )
        runner.feed_1m_bar(bar)

        bars = runner.get_bars()
        # Before fix: len(bars) == warmup_count (live bar held by aggregator)
        # After fix:  len(bars) == warmup_count + 1
        assert len(bars) == warmup_count + 1
        assert bars[-1].dt == datetime(2026, 3, 1, 9, 0)
        assert bars[-1].close == 22605

    def test_h1_get_bars_at_interval_includes_partial(self, tmp_path):
        """H1 strategy: get_bars_at_interval(3600) must include in-progress H1.

        Issue #44 H1 manifestation: the in-progress H1 bar was never in the
        chart's source list until the next H1 boundary, leaving chart ~60 min
        behind wall clock mid-hour.
        """
        strategy = NeverTradeStrategy()  # kline_minute=240 (H4)
        # Override to make it H1 for this test
        strategy.kline_minute = 60
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))
        # Re-derive target_interval after class override
        runner.target_interval = 3600
        from src.live.bar_aggregator import BarAggregator as _BA
        runner.aggregator = _BA("TX00", 3600)
        # Force RUNNING state (tests skip full warmup flow)
        runner.state = LiveState.RUNNING

        # Feed 5 live 1-min bars mid-hour (09:45-09:49, in H1 boundary 09:45)
        for m in range(5):
            bar = Bar(
                symbol="TX00", dt=datetime(2026, 3, 2, 9, 45 + m),
                open=22500 + m, high=22510 + m, low=22490 + m,
                close=22505 + m, volume=50, interval=60,
            )
            runner.feed_1m_bar(bar)

        # _aggregated_bars is empty (no H1 boundary crossed yet)
        # but get_bars_at_interval(3600) should include the partial H1
        bars = runner.get_bars_at_interval(3600)
        assert len(bars) == 1, "Partial H1 bar must be visible"
        assert bars[-1].dt == datetime(2026, 3, 2, 9, 45)
        # Partial contains aggregated data from the 5 1-min bars fed
        assert bars[-1].close == 22509  # last 1-min close


# ── Regression: issue #45 — CSV reload gap in live chart ──

class TestReload1mBarsIssue45:
    """Regression tests for issue #45.

    Before the fix, ``reload_1m_bars`` populated only ``_1m_bars`` and
    ``_seen_1m_dts`` but never ``_aggregated_bars``. For 1-min native
    strategies this left the live chart (which draws from
    ``_aggregated_bars``) with a ~10-hour gap between the warmup end
    and the first live bar, because:

    (a) The COM warmup API doesn't return bars for the currently
        in-progress trading session.
    (b) The historical tick-replay that SHOULD fill the gap rebuilds
        those bars via ``BarBuilder``, but they get deduplicated
        against ``_seen_1m_dts`` (populated by this same reload) and
        silently dropped before reaching ``_aggregated_bars``.

    The fix:
    - ``feed_warmup_bars`` now populates ``_seen_1m_dts`` for 1-min
      strategies so CSV/history bars overlapping warmup are deduped.
    - ``reload_1m_bars`` now merges new CSV bars into
      ``_aggregated_bars`` and rebuilds ``data_store`` in sorted order
      for 1-min native strategies.
    """

    @staticmethod
    def _write_bars_csv(path: str, bars: list[Bar]) -> None:
        """Write bars to a CSV file in the format reload_1m_bars expects."""
        with open(path, "w", encoding="utf-8") as f:
            f.write("datetime,open,high,low,close,volume\n")
            for b in bars:
                f.write(
                    f"{b.dt.strftime('%Y/%m/%d %H:%M')},"
                    f"{b.open},{b.high},{b.low},{b.close},{b.volume}\n"
                )

    @staticmethod
    def _make_1m_bar(dt: datetime, c: int = 22500) -> Bar:
        return Bar(
            symbol="TX00", dt=dt,
            open=c, high=c + 10, low=c - 10, close=c,
            volume=10, interval=60,
        )

    def test_feed_warmup_populates_seen_dts_for_1min(self, tmp_path):
        """Warmup must track bar dts in _seen_1m_dts for 1-min strategies."""
        strategy = OneMinRecordingStrategy()  # kline_minute=1
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [
            _kline("2026-02-28 09:00", 22500),
            _kline("2026-02-28 09:01", 22510),
            _kline("2026-02-28 09:02", 22520),
        ]
        runner.feed_warmup_bars(warmup)

        assert len(runner._seen_1m_dts) == 3
        assert datetime(2026, 2, 28, 9, 0) in runner._seen_1m_dts
        assert datetime(2026, 2, 28, 9, 1) in runner._seen_1m_dts
        assert datetime(2026, 2, 28, 9, 2) in runner._seen_1m_dts

    def test_feed_warmup_leaves_seen_dts_empty_for_h4(self, tmp_path):
        """H4 warmup must NOT populate _seen_1m_dts (H4 bars are not 1-min)."""
        strategy = NeverTradeStrategy()  # kline_minute=240 (H4)
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        assert runner._seen_1m_dts == set()

    def test_reload_fills_aggregated_bars_for_1min(self, tmp_path):
        """CSV bars must be merged into _aggregated_bars for 1-min strategies.

        Before the fix, _aggregated_bars only had warmup bars; the CSV
        gap-fill lived in _1m_bars only, leaving a visible gap in the
        live chart.
        """
        strategy = OneMinRecordingStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        # Warmup: 3 bars at 09:00-09:02
        warmup = [
            _kline("2026-02-28 09:00", 22500),
            _kline("2026-02-28 09:01", 22510),
            _kline("2026-02-28 09:02", 22520),
        ]
        runner.feed_warmup_bars(warmup)
        assert len(runner.get_bars()) == 3  # warmup only

        # Write CSV with 2 new bars at 09:03 and 09:04
        csv_path = os.path.join(runner.bot_dir, "bars_1m_20260228.csv")
        csv_bars = [
            self._make_1m_bar(datetime(2026, 2, 28, 9, 3), c=22530),
            self._make_1m_bar(datetime(2026, 2, 28, 9, 4), c=22540),
        ]
        self._write_bars_csv(csv_path, csv_bars)

        count = runner.reload_1m_bars()

        assert count == 2
        # _aggregated_bars now has warmup + CSV bars (sorted)
        agg = runner.get_bars()
        assert len(agg) == 5
        assert [b.dt.minute for b in agg] == [0, 1, 2, 3, 4]
        assert agg[-1].close == 22540
        # _warmup_bar_count bumped so get_live_bars() is still empty
        assert runner._warmup_bar_count == 5
        assert runner.get_live_bars() == []
        # data_store was rebuilt in sorted order
        store_bars = runner.data_store.get_bars()
        assert [b.dt.minute for b in store_bars] == [0, 1, 2, 3, 4]

    def test_reload_dedupes_overlap_with_warmup_for_1min(self, tmp_path):
        """CSV bars already in warmup must NOT be re-added (would double-count)."""
        strategy = OneMinRecordingStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        # Warmup bars at 09:00-09:02 (explicit close via keyword arg)
        warmup = [
            _kline("2026-02-28 09:00", c=22500),
            _kline("2026-02-28 09:01", c=22510),
            _kline("2026-02-28 09:02", c=22520),
        ]
        runner.feed_warmup_bars(warmup)

        # CSV has bars 09:01-09:04 (09:01 and 09:02 overlap warmup)
        csv_path = os.path.join(runner.bot_dir, "bars_1m_20260228.csv")
        csv_bars = [
            self._make_1m_bar(datetime(2026, 2, 28, 9, 1), c=99),  # dup
            self._make_1m_bar(datetime(2026, 2, 28, 9, 2), c=99),  # dup
            self._make_1m_bar(datetime(2026, 2, 28, 9, 3), c=22530),
            self._make_1m_bar(datetime(2026, 2, 28, 9, 4), c=22540),
        ]
        self._write_bars_csv(csv_path, csv_bars)

        count = runner.reload_1m_bars()

        assert count == 2  # only 09:03 and 09:04 are new
        agg = runner.get_bars()
        assert len(agg) == 5
        assert [b.dt.minute for b in agg] == [0, 1, 2, 3, 4]
        # Warmup's 09:01/09:02 values survived — CSV's 99 did NOT overwrite
        assert agg[1].close == 22510
        assert agg[2].close == 22520
        # _1m_bars also has no duplicates
        assert len(runner._1m_bars) == 5

    def test_reload_merges_reaggregated_bars_for_h4(self, tmp_path):
        """H4 strategies: CSV 1-min bars must be re-aggregated into H4 and
        merged into _aggregated_bars (issue #96).

        Before the fix, _aggregated_bars was left untouched for non-1-min
        strategies, leaving a hole in DataStore when warmup excluded the
        just-ended session.
        """
        strategy = NeverTradeStrategy()  # kline_minute=240 (H4)
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)
        agg_before_count = len(runner.get_bars())

        # Write a 1-min CSV (different timeframe from the H4 strategy)
        csv_path = os.path.join(runner.bot_dir, "bars_1m_20260301.csv")
        csv_bars = [
            self._make_1m_bar(datetime(2026, 3, 1, 9, m), c=22500 + m)
            for m in range(5)
        ]
        self._write_bars_csv(csv_path, csv_bars)

        count = runner.reload_1m_bars()

        assert count == 5
        # _aggregated_bars now includes the re-aggregated H4 bar
        assert len(runner.get_bars()) == agg_before_count + 1
        # _1m_bars has the CSV bars for charting re-aggregation
        assert len(runner._1m_bars) == 5

    def test_reload_preserves_sorted_order_with_older_csv(self, tmp_path):
        """Merged _aggregated_bars must be sorted even if CSV has OLDER bars.

        E.g. a CSV with data before the warmup range should produce a
        sorted list with those older bars at the front.
        """
        strategy = OneMinRecordingStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        # Warmup: 09:02-09:04 (mid-range) — explicit close via keyword
        warmup = [
            _kline("2026-02-28 09:02", c=22502),
            _kline("2026-02-28 09:03", c=22503),
            _kline("2026-02-28 09:04", c=22504),
        ]
        runner.feed_warmup_bars(warmup)

        # CSV has 09:00, 09:01 (before warmup) AND 09:05 (after)
        csv_path = os.path.join(runner.bot_dir, "bars_1m_20260228.csv")
        csv_bars = [
            self._make_1m_bar(datetime(2026, 2, 28, 9, 0), c=22500),
            self._make_1m_bar(datetime(2026, 2, 28, 9, 1), c=22501),
            self._make_1m_bar(datetime(2026, 2, 28, 9, 5), c=22505),
        ]
        self._write_bars_csv(csv_path, csv_bars)

        runner.reload_1m_bars()

        agg = runner.get_bars()
        assert len(agg) == 6
        assert [b.dt.minute for b in agg] == [0, 1, 2, 3, 4, 5]
        assert [b.close for b in agg] == [
            22500, 22501, 22502, 22503, 22504, 22505]

    def test_reload_no_csv_files_is_noop(self, tmp_path):
        """If there are no CSV files, reload_1m_bars is a no-op."""
        strategy = OneMinRecordingStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        warmup = [_kline("2026-02-28 09:00", 22500)]
        runner.feed_warmup_bars(warmup)
        agg_before = list(runner.get_bars())

        count = runner.reload_1m_bars()

        assert count == 0
        assert runner.get_bars() == agg_before

    def test_reload_data_store_deque_bounded(self, tmp_path):
        """Rebuilt data_store must respect the original maxlen."""
        strategy = OneMinRecordingStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))
        original_maxlen = runner.data_store._bars.maxlen

        warmup = [_kline("2026-02-28 09:00", 22500)]
        runner.feed_warmup_bars(warmup)

        csv_path = os.path.join(runner.bot_dir, "bars_1m_20260228.csv")
        csv_bars = [
            self._make_1m_bar(datetime(2026, 2, 28, 9, m), c=22500 + m)
            for m in range(1, 5)
        ]
        self._write_bars_csv(csv_path, csv_bars)

        runner.reload_1m_bars()

        assert runner.data_store._bars.maxlen == original_maxlen


# ── Regression: issue #79 — reload progress yields (UI freeze) ──

class TestReload1mBarsProgressIssue79:
    """reload_1m_bars must report progress so the GUI can pump the Tk event
    loop and stay responsive while re-feeding thousands of saved bars."""

    @staticmethod
    def _write_many_bars(path: str, n: int) -> None:
        base = datetime(2026, 2, 28, 0, 0)
        with open(path, "w", encoding="utf-8") as f:
            f.write("datetime,open,high,low,close,volume\n")
            for i in range(n):
                dt = base + timedelta(minutes=i)
                c = 22500 + i
                f.write(f"{dt.strftime('%Y/%m/%d %H:%M')},{c},{c+5},{c-5},{c},10\n")

    def test_progress_cb_called_and_order_preserved(self, tmp_path):
        strategy = OneMinRecordingStrategy()  # 1-min native → heavy rebuild path
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))

        n = 1300  # > 2 * _RELOAD_PROGRESS_EVERY (500)
        csv_path = os.path.join(runner.bot_dir, "bars_1m_20260228.csv")
        self._write_many_bars(csv_path, n)

        calls: list[tuple[int, int]] = []
        count = runner.reload_1m_bars(progress_cb=lambda d, t: calls.append((d, t)))

        assert count == n
        # Progress fired at least once per heavy loop.
        assert len(calls) >= 2
        # Every tick reports a positive batch count (multiple of the stride).
        assert all(d > 0 and d % runner._RELOAD_PROGRESS_EVERY == 0
                   for d, _ in calls)
        # Parse phase reports total=0 (unknown); rebuild phase reports the
        # real total once known.
        assert any(t == 0 for _, t in calls)      # parse phase
        assert any(t == n for _, t in calls)      # rebuild phase, total known
        # Bars are still in chronological order (callback only observes).
        agg = runner.get_bars()
        assert [b.dt for b in agg] == sorted(b.dt for b in agg)
        assert len(agg) == n

    def test_no_callback_is_safe(self, tmp_path):
        """Omitting progress_cb (default None) must not raise."""
        strategy = OneMinRecordingStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))
        csv_path = os.path.join(runner.bot_dir, "bars_1m_20260228.csv")
        self._write_many_bars(csv_path, 600)
        assert runner.reload_1m_bars() == 600

    def test_broken_callback_does_not_break_reload(self, tmp_path):
        """A raising progress_cb must be swallowed — reload still completes."""
        strategy = OneMinRecordingStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))
        csv_path = os.path.join(runner.bot_dir, "bars_1m_20260228.csv")
        self._write_many_bars(csv_path, 600)

        def _boom(done, total):
            raise RuntimeError("UI callback blew up")

        assert runner.reload_1m_bars(progress_cb=_boom) == 600


# ── Regression: issue #96 — H1 reload fills DataStore gap ──

class TestReload1mBarsIssue96:
    """Pre-open redeploy with saved CSV must fill DataStore for ALL timeframes.

    Before the fix, ``reload_1m_bars`` only merged bars into
    ``_aggregated_bars`` / DataStore for 1-min strategies. For H1/H4
    strategies the just-ended night session's bars were missing from
    DataStore after reload — the dedup set swallowed tick-replay bars
    and indicators computed from insufficient history, producing zero
    signals.
    """

    @staticmethod
    def _write_bars_csv(path: str, bars: list[Bar]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write("datetime,open,high,low,close,volume\n")
            for b in bars:
                f.write(
                    f"{b.dt.strftime('%Y/%m/%d %H:%M')},"
                    f"{b.open},{b.high},{b.low},{b.close},{b.volume}\n"
                )

    @staticmethod
    def _make_1m_bar(dt: datetime, c: int = 22500) -> Bar:
        return Bar(
            symbol="TX00", dt=dt,
            open=c, high=c + 10, low=c - 10, close=c,
            volume=10, interval=60,
        )

    def _make_h1_runner(self, tmp_path):
        """Create an H1 LiveRunner (kline_minute=60)."""
        strategy = NeverTradeStrategy()
        strategy.kline_minute = 60
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))
        runner.target_interval = 3600
        runner.aggregator = BarAggregator("TX00", 3600)
        return runner

    def test_h1_reload_fills_datastore_from_csv(self, tmp_path):
        """H1 strategy: CSV 1-min bars must be re-aggregated and merged
        into _aggregated_bars and DataStore (issue #96).

        Reproduces the actual scenario: bot redeployed at ~08:00 on
        Jul 24. Warmup returns bars up to Jul 23 AM close. CSV has the
        night session bars (Jul 23 15:00 – Jul 24 05:00). Without the
        fix, these H1 bars were missing from DataStore.
        """
        runner = self._make_h1_runner(tmp_path)

        # Warmup: 10 H1 bars covering previous sessions
        warmup = [
            _kline(f"2026-07-{d:02d} {h:02d}:00", 22500 + d * 10)
            for d in range(21, 24) for h in [9, 10, 11]
        ]
        # Only take first 10 for a clean warmup
        warmup = warmup[:10]
        runner.feed_warmup_bars(warmup)
        agg_count_after_warmup = len(runner.get_bars())
        store_count_after_warmup = len(runner.data_store.get_bars())

        # Write CSV with night session bars (Jul 23 15:00–19:59 = 5 hours
        # of 1-min bars, which should re-aggregate into 5 H1 bars)
        csv_path = os.path.join(runner.bot_dir, "bars_1m_20260723.csv")
        night_bars = []
        for h in range(15, 20):
            for m in range(60):
                dt = datetime(2026, 7, 23, h, m)
                night_bars.append(self._make_1m_bar(dt, c=22600 + h * 10 + m))
        self._write_bars_csv(csv_path, night_bars)

        count = runner.reload_1m_bars()

        assert count == len(night_bars)
        # H1 bars from the night session should now be in _aggregated_bars
        agg_bars = runner.get_bars()
        assert len(agg_bars) > agg_count_after_warmup
        # DataStore also received the re-aggregated bars
        store_bars = runner.data_store.get_bars()
        assert len(store_bars) > store_count_after_warmup
        # Night H1 bars should start at 15:00
        new_h1_bars = agg_bars[agg_count_after_warmup:]
        assert new_h1_bars[0].dt == datetime(2026, 7, 23, 15, 0)
        # Each new bar should be H1 interval
        assert all(b.interval == 3600 for b in new_h1_bars)

    def test_h1_reload_dedupes_overlap_with_warmup(self, tmp_path):
        """H1 reload must not duplicate bars already present from warmup."""
        runner = self._make_h1_runner(tmp_path)

        # Warmup includes an H1 bar at 09:00
        warmup = [_kline("2026-07-23 09:00", 22500)]
        runner.feed_warmup_bars(warmup)
        assert len(runner.get_bars()) == 1

        # CSV also covers 09:00–09:59 (same H1 window) plus 10:00–10:59
        csv_path = os.path.join(runner.bot_dir, "bars_1m_20260723.csv")
        csv_bars = [
            self._make_1m_bar(datetime(2026, 7, 23, h, m), c=22500 + h * 10)
            for h in [9, 10] for m in range(60)
        ]
        self._write_bars_csv(csv_path, csv_bars)

        runner.reload_1m_bars()

        agg_bars = runner.get_bars()
        # 09:00 bar from warmup + 10:00 bar from CSV = 2 (no duplicate 09:00)
        h1_dts = [b.dt for b in agg_bars]
        assert len(h1_dts) == len(set(h1_dts)), "duplicate H1 bars after reload"

    def test_jimmy_jul24_redeploy_scenario(self, tmp_path):
        """Reproduce jimmy8399's exact Jul 24 scenario.

        Bot redeployed at ~08:00. Warmup returns AM bars up to Jul 23
        13:44. Night session CSV (Jul 23 15:00 – Jul 24 04:59) exists
        on disk. After reload, H1 DataStore must include the night
        session bars so the strategy has enough history to compute
        indicators and fire signals.
        """
        runner = self._make_h1_runner(tmp_path)

        # Warmup: AM session bars from prior days (Jul 21-23, 08:45-13:44)
        warmup_bars = []
        for d in range(21, 24):
            for h in [8, 9, 10, 11, 12, 13]:
                if h == 8:
                    warmup_bars.append(
                        _kline(f"2026-07-{d:02d} 08:45", 22500 + d * 10))
                else:
                    warmup_bars.append(
                        _kline(f"2026-07-{d:02d} {h:02d}:00", 22500 + d * 10 + h))
        runner.feed_warmup_bars(warmup_bars)
        warmup_count = len(runner.get_bars())
        assert warmup_count > 0

        # Night session CSV: Jul 23 15:00 – Jul 24 04:59 (14 hours = 840 bars)
        csv_path = os.path.join(runner.bot_dir, "bars_1m_20260724.csv")
        night_bars = []
        # Jul 23 15:00-23:59
        for h in range(15, 24):
            for m in range(60):
                night_bars.append(
                    self._make_1m_bar(datetime(2026, 7, 23, h, m), c=22700 + h))
        # Jul 24 00:00-04:59
        for h in range(0, 5):
            for m in range(60):
                night_bars.append(
                    self._make_1m_bar(datetime(2026, 7, 24, h, m), c=22700 + h))
        self._write_bars_csv(csv_path, night_bars)

        count = runner.reload_1m_bars()
        assert count == len(night_bars)

        # DataStore must now include night session H1 bars
        store_bars = runner.data_store.get_bars()
        store_dts = [b.dt for b in store_bars]
        # Night session should produce H1 bars at 15:00, 16:00, ..., 04:00
        assert datetime(2026, 7, 23, 15, 0) in store_dts, \
            "Night H1 bar at 15:00 missing from DataStore"
        assert datetime(2026, 7, 24, 4, 0) in store_dts, \
            "Night H1 bar at 04:00 missing from DataStore"
        # Total bar count should be warmup + night H1 bars
        assert len(store_bars) > warmup_count
        # _warmup_bar_count updated so get_live_bars() returns empty
        assert runner.get_live_bars() == []

    def test_reload_no_new_bars_leaves_state_unchanged(self, tmp_path):
        """If CSV has no new bars (all deduped), _aggregated_bars is untouched."""
        runner = self._make_h1_runner(tmp_path)

        warmup = [_kline("2026-07-23 09:00", 22500)]
        runner.feed_warmup_bars(warmup)
        agg_before = list(runner.get_bars())

        # Empty CSV → 0 new bars
        count = runner.reload_1m_bars()
        assert count == 0
        assert runner.get_bars() == agg_before


# ── Regression: issue #45 — real_entry_price flow into Trades ──

class TestRealEntryPriceLiveRunnerIssue45:
    """End-to-end tests that real_entry_price flows through the
    LiveRunner's various exit paths (tick exit, force close, stop)."""

    def test_tick_exit_preserves_real_entry_price(self, tmp_path):
        """check_tick_exit closes the trade — Trade must carry real_entry_price."""
        from src.backtest.broker import OrderSide

        strategy = LongWithExitStrategy()  # enters long, sets TP/SL
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))

        # Warmup enough bars for required_bars
        warmup = [_kline(f"2026-02-{d:02d} 09:00", 22500) for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        # Feed a 15-min bar to trigger entry
        lines = _klines_1m("2026-03-01", 540, 15)  # 09:00-09:14
        runner.feed_1m_bars(lines)
        # Enter on the boundary
        lines = _klines_1m("2026-03-01", 555, 1, base_price=22500)  # 09:15
        runner.feed_1m_bars(lines)
        # Now second 15-min boundary sets up the exit
        lines = _klines_1m("2026-03-01", 556, 15, base_price=22500)
        runner.feed_1m_bars(lines)
        lines = _klines_1m("2026-03-01", 571, 1, base_price=22500)  # 09:31
        runner.feed_1m_bars(lines)

        # By now strategy should be in a position
        if runner.broker.position_size == 0:
            pytest.skip("position did not open in fixture setup")

        # Simulate auto-mode real fill confirmation
        runner.broker.real_entry_price = 22505
        runner.broker.real_entry_dt = "2026-03-01T09:15:03"
        assert runner.broker.position_side == OrderSide.LONG

        # Tick exit via stop-loss (stop should be 22505 - 50 = 22455)
        result = runner.check_tick_exit(22440, "2026-03-01 09:32:00")
        assert result is not None, "tick exit should fire"

        assert len(runner.broker.trades) >= 1
        t = runner.broker.trades[-1]
        assert t.real_entry_price == 22505, (
            f"tick exit lost real_entry_price; got {t.real_entry_price}")
        assert t.real_entry_dt == "2026-03-01T09:15:03"
        # Broker field reset after close
        assert runner.broker.real_entry_price == 0

    def test_runner_stop_force_close_preserves_real_entry_price(self, tmp_path):
        """runner.stop() → force_close must preserve real_entry_price."""
        strategy = LongWithExitStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00", 22500) for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        lines = _klines_1m("2026-03-01", 540, 16)  # triggers 15-min aggregation + entry
        runner.feed_1m_bars(lines)
        if runner.broker.position_size == 0:
            pytest.skip("position did not open in fixture setup")

        runner.broker.real_entry_price = 22508
        runner.broker.real_entry_dt = "2026-03-01T09:15:03"

        # Stop the runner — should force-close the open position
        runner.stop()

        closed_trade = runner.broker.trades[-1]
        assert closed_trade.real_entry_price == 22508
        assert closed_trade.real_entry_dt == "2026-03-01T09:15:03"

    def test_effective_entry_price_through_broker_context(self, tmp_path):
        """Strategy reading broker.effective_entry_price() gets real when set,
        falls back to sim otherwise."""
        strategy = LongWithExitStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))

        warmup = [_kline(f"2026-02-{d:02d} 09:00", 22500) for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        lines = _klines_1m("2026-03-01", 540, 16)
        runner.feed_1m_bars(lines)
        if runner.broker.position_size == 0:
            pytest.skip("position did not open in fixture setup")

        ctx = runner.broker.context

        # Pre-confirmation → fallback to simulated
        sim_entry = runner.broker.entry_price
        assert ctx.effective_entry_price() == sim_entry

        # Post-confirmation → real
        runner.broker.real_entry_price = 22508
        assert ctx.effective_entry_price() == 22508


class TestLoad1mBarsFromCsvs:
    """load_1m_bars_from_csvs — pure reader for the evolution pipeline."""

    def test_empty_dir(self, tmp_path):
        from src.live.live_runner import load_1m_bars_from_csvs
        assert load_1m_bars_from_csvs(str(tmp_path), "TX00") == []

    def test_loads_sorted_across_files(self, tmp_path):
        from src.live.live_runner import load_1m_bars_from_csvs
        (tmp_path / "bars_1m_20260611.csv").write_text(
            "datetime,open,high,low,close,volume\n"
            "2026/06/11 09:01,200,210,190,205,10\n",
            encoding="utf-8")
        (tmp_path / "bars_1m_20260610.csv").write_text(
            "datetime,open,high,low,close,volume\n"
            "2026/06/10 09:02,105,115,95,110,12\n"
            "2026/06/10 09:01,100,110,90,105,10\n",
            encoding="utf-8")
        bars = load_1m_bars_from_csvs(str(tmp_path), "TX00")
        assert len(bars) == 3
        assert [b.dt for b in bars] == sorted(b.dt for b in bars)
        assert bars[0].close == 105
        assert all(b.interval == 60 for b in bars)
        assert all(b.symbol == "TX00" for b in bars)

    def test_dedup_last_file_wins(self, tmp_path):
        from src.live.live_runner import load_1m_bars_from_csvs
        (tmp_path / "bars_1m_20260610.csv").write_text(
            "datetime,open,high,low,close,volume\n"
            "2026/06/10 09:01,100,110,90,105,10\n",
            encoding="utf-8")
        (tmp_path / "bars_1m_20260611.csv").write_text(
            "datetime,open,high,low,close,volume\n"
            "2026/06/10 09:01,999,999,999,999,9\n",
            encoding="utf-8")
        bars = load_1m_bars_from_csvs(str(tmp_path), "TX00")
        assert len(bars) == 1
        assert bars[0].close == 999

    def test_garbage_rows_skipped(self, tmp_path):
        from src.live.live_runner import load_1m_bars_from_csvs
        (tmp_path / "bars_1m_20260610.csv").write_text(
            "datetime,open,high,low,close,volume\n"
            "garbage,row\n"
            "not-a-date,1,2,3,4,5\n"
            "2026/06/10 09:01,100,110,90,105,10\n",
            encoding="utf-8")
        bars = load_1m_bars_from_csvs(str(tmp_path), "TX00")
        assert len(bars) == 1


class TestResetBarMonotonicity:
    """Issue #78: reconnect must reset the out-of-order bar guards on the
    primary aggregator AND all HTF aggregators, so the first bar after the
    replay gap is always accepted."""

    def test_reset_clears_primary_and_htf_trackers(self, tmp_path):
        strategy = AlwaysLongStrategy()  # 15-min primary
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))
        # Force an HTF aggregator to exist alongside the primary one.
        runner._htf_aggregators[14400] = BarAggregator("TX00", 14400)

        # Advance the guards past a known time on every aggregator.
        seed = Bar(symbol="TX00", dt=datetime(2026, 3, 2, 9, 0), open=100,
                   high=110, low=90, close=105, volume=10, interval=60)
        runner.aggregator.on_bar(seed)
        runner._htf_aggregators[14400].on_bar(seed)
        assert runner.aggregator._last_bar_time is not None
        assert runner._htf_aggregators[14400]._last_bar_time is not None

        runner.reset_bar_monotonicity()

        assert runner.aggregator._last_bar_time is None
        assert runner._htf_aggregators[14400]._last_bar_time is None


class TestBlockedSignalLog:
    """Issue #62: when the risk gate / TradingGuard rejects a strategy signal
    (daily loss limit, fill pending, halted, no real position, settlement
    window), it must be recorded as a SIGNAL_BLOCKED decision so the block
    appears in decisions.csv AND the UI event log instead of being silent."""

    def _running_runner(self, tmp_path):
        """A RUNNING runner with one completed aggregated bar (09:00 15-min)."""
        strategy = AlwaysLongStrategy()  # 15-min
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))
        warmup = [
            _kline("2026-02-28 09:00", 22000, 22100, 21900, 22050, 500),
            _kline("2026-02-28 09:15", 22050, 22150, 22000, 22100, 400),
        ]
        runner.feed_warmup_bars(warmup)
        # 09:00-09:14 then 09:15 crosses the 15-min boundary → 09:00 bar closes.
        runner.feed_1m_bars(_klines_1m("2026-03-01", start_min=540, count=15))
        runner.feed_1m_bars([_kline("2026-03-01 09:15", 22515, 22525, 22510, 22520, 65)])
        return runner

    def test_blocked_entry_emits_signal_blocked_decision(self, tmp_path):
        runner = self._running_runner(tmp_path)
        emitted = []
        runner.on("on_decision", emitted.append)

        # Simulate the daily-loss-limit block (guard.BLOCK_ENTRY): a long
        # entry signal at 22,505 rejected because the limit was reached.
        runner.log_blocked_signal(
            "ENTRY_FILL", "LONG", 22505, "DAILY_LOSS_LIMIT",
            "daily loss limit reached (10,000 NTD)")

        blocked = [d for d in emitted if d["action"] == "SIGNAL_BLOCKED"]
        assert len(blocked) == 1
        d = blocked[0]
        assert d["side"] == "LONG"
        assert d["price"] == 22505
        assert d["tag"] == "DAILY_LOSS_LIMIT"
        assert "daily loss limit" in d["reason"]
        assert d["strategy"] == runner.strategy.name
        # Tied to the triggering K-bar (the last completed aggregated bar).
        assert d["bar_dt"] == runner._aggregated_bars[-1].dt

    def test_blocked_signal_written_to_decisions_csv(self, tmp_path):
        runner = self._running_runner(tmp_path)
        runner.log_blocked_signal(
            "ENTRY_FILL", "SHORT", 22480, "FILL_PENDING",
            "waiting for entry fill confirmation")
        runner.stop()  # flush + close csv handles

        import csv
        with open(tmp_path / "decisions.csv", "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        header, data = rows[0], rows[1:]
        assert header == ["datetime", "bar_dt", "strategy", "action",
                          "side", "tag", "price", "reason"]
        blocked = [r for r in data if r[3] == "SIGNAL_BLOCKED"]
        assert len(blocked) == 1
        r = blocked[0]
        assert r[4] == "SHORT"          # side
        assert r[5] == "FILL_PENDING"   # tag = reason code
        assert r[6] == "22480"          # price
        assert "waiting for entry fill" in r[7]  # reason

    def test_blocked_signal_falls_back_to_now_without_bars(self, tmp_path):
        """No completed bar yet → bar_dt falls back to wall-clock, no crash."""
        strategy = AlwaysLongStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))
        emitted = []
        runner.on("on_decision", emitted.append)

        runner.log_blocked_signal(
            "TRADE_CLOSE", "LONG", 22600, "NO_REAL_POSITION",
            "exit — no real entry was confirmed")

        assert len(emitted) == 1
        assert emitted[0]["action"] == "SIGNAL_BLOCKED"
        assert emitted[0]["bar_dt"] is not None


class TestRestoreSessionTradeSource:
    """restore_session must re-set trade_source from the current trading_mode."""

    def test_trade_source_restored_after_resume(self, tmp_path):
        strategy = AlwaysLongStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))
        runner.trading_mode = "semi_auto"
        runner.broker.trade_source = "real"
        runner.state = LiveState.RUNNING

        # Simulate a saved session (from_dict does NOT restore trade_source)
        saved = runner.broker.to_dict()
        session_data = {"broker": saved, "bar_index": 5}

        # Before fix, trade_source would be "" after from_dict
        runner.restore_session(session_data)
        assert runner.broker.trade_source == "real"

    def test_trade_source_paper_mode(self, tmp_path):
        strategy = AlwaysLongStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))
        runner.trading_mode = "paper"

        saved = runner.broker.to_dict()
        runner.restore_session({"broker": saved, "bar_index": 0})
        assert runner.broker.trade_source == "paper"


class TestDoubleResubscribeDedupRegression:
    """Issue #98: calling reset_bar_monotonicity twice (double resubscribe)
    must NOT pop good bars from the dedup set.

    Before the fix, each call unconditionally popped max(_seen_1m_dts),
    removing good completed bars. When history replayed those bars, they
    were treated as unseen and written to CSV again — duplicate rows.
    """

    def test_double_reset_without_held_bar_no_csv_duplicates(self, tmp_path):
        """Ingest 04:57 and 04:58 bars, call reset twice (no held bar),
        replay those bars, assert CSV has unique timestamps."""
        strategy = OneMinRecordingStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))
        # Warm up so state is RUNNING
        warmup = [_kline("2026-03-01 04:55", 22000, 22100, 21900, 22050, 500)]
        runner.feed_warmup_bars(warmup)
        # Transition to RUNNING by feeding 1-min bars past the warmup
        runner.feed_1m_bars([
            _kline("2026-03-01 04:56", 22050, 22060, 22040, 22055, 60),
            _kline("2026-03-01 04:57", 22055, 22065, 22045, 22060, 70),
        ])
        assert runner.state == LiveState.RUNNING

        # Feed the two bars that will be the "good" bars at session end
        bar_0457 = Bar(symbol="TX00", dt=datetime(2026, 3, 1, 4, 57),
                       open=22055, high=22065, low=22045, close=22060,
                       volume=70, interval=60)
        bar_0458 = Bar(symbol="TX00", dt=datetime(2026, 3, 1, 4, 58),
                       open=22060, high=22070, low=22050, close=22065,
                       volume=80, interval=60)
        runner.feed_1m_bar(bar_0457)
        runner.feed_1m_bar(bar_0458)

        # Double resubscribe with NO held bar (session gap scenario)
        runner.reset_bar_monotonicity(bar_was_truncated=False)
        runner.reset_bar_monotonicity(bar_was_truncated=False)

        # Replay the same bars
        runner.feed_1m_bar(bar_0457)
        runner.feed_1m_bar(bar_0458)

        # Read all CSV files and collect timestamps
        csv_dir = runner.csv_logger._base_dir
        timestamps = []
        import csv as csv_mod
        import glob
        for f in sorted(glob.glob(os.path.join(csv_dir, "bars_1m_*.csv"))):
            with open(f) as fh:
                reader = csv_mod.reader(fh)
                next(reader, None)  # skip header
                for row in reader:
                    timestamps.append(row[0])

        assert len(timestamps) == len(set(timestamps)), (
            f"Duplicate timestamps in CSV: {timestamps}")
        assert timestamps == sorted(timestamps), (
            f"Timestamps not strictly increasing: {timestamps}")

    def test_reset_with_held_bar_allows_replay(self, tmp_path):
        """When a truncated bar exists (bar_was_truncated=True), the dedup
        pop should happen so the full replayed bar can replace it."""
        strategy = OneMinRecordingStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path))
        warmup = [_kline("2026-03-01 09:00", 22000, 22100, 21900, 22050, 500)]
        runner.feed_warmup_bars(warmup)
        runner.feed_1m_bars([
            _kline("2026-03-01 09:01", 22050, 22060, 22040, 22055, 60),
            _kline("2026-03-01 09:02", 22055, 22065, 22045, 22060, 70),
        ])

        bar_0902 = Bar(symbol="TX00", dt=datetime(2026, 3, 1, 9, 2),
                       open=22055, high=22065, low=22045, close=22060,
                       volume=70, interval=60)
        runner.feed_1m_bar(bar_0902)
        assert datetime(2026, 3, 1, 9, 2) in runner._seen_1m_dts

        # Simulate: BarBuilder had a partial bar → truncated
        runner.reset_bar_monotonicity(bar_was_truncated=True)
        assert datetime(2026, 3, 1, 9, 2) not in runner._seen_1m_dts

        # Replay the full bar — should be accepted
        runner.feed_1m_bar(bar_0902)
        assert datetime(2026, 3, 1, 9, 2) in runner._seen_1m_dts


class TestSessionKeyDelegation:
    """_session_key delegates to switch_logic.session_slot."""

    def test_session_key_returns_tuple(self, tmp_path):
        strategy = AlwaysLongStrategy()
        runner = LiveRunner(strategy, "TX00", log_dir=str(tmp_path))
        key = runner._session_key()
        assert isinstance(key, tuple)
        assert len(key) == 2
        date_str, slot = key
        assert slot in ("DAY", "NIGHT")
        assert len(date_str) == 10  # YYYY-MM-DD
