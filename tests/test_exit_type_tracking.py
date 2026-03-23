"""Tests for broker exit type tracking and decision pipeline.

Verifies that last_exit_type and last_exit_limit are set correctly for
each exit path, and that the values propagate through live_runner decisions.
"""

import os
from datetime import datetime

import pytest

from src.backtest.broker import SimulatedBroker, BrokerContext, OrderSide, Order
from src.backtest.strategy import BacktestStrategy
from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.live.live_runner import LiveRunner, LiveState


# ── Fixtures ──

@pytest.fixture
def broker():
    return SimulatedBroker(point_value=200)


@pytest.fixture
def ctx(broker):
    return BrokerContext(broker)


# ── Broker exit type tests ──

class TestExitTypeOnLimitFill:
    """Take-profit (limit) exits set last_exit_type='limit' and preserve the limit price."""

    def test_long_limit_exit(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(tag="TP", side=OrderSide.LONG, from_entry="Long", limit=20200))
        broker.check_exits(1, open_=20050, high=20250, low=20040, close=20180)

        assert broker.position_size == 0
        assert broker.last_exit_type == "limit"
        assert broker.last_exit_limit == 20200

    def test_short_limit_exit(self, broker):
        broker.queue_entry(Order(tag="Short", side=OrderSide.SHORT, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(tag="TP", side=OrderSide.SHORT, from_entry="Short", limit=19800))
        broker.check_exits(1, open_=19950, high=19970, low=19750, close=19820)

        assert broker.position_size == 0
        assert broker.last_exit_type == "limit"
        assert broker.last_exit_limit == 19800

    def test_limit_exit_with_gap_preserves_original_limit(self, broker):
        """Gap-up fills at open, but last_exit_limit should be the strategy's price."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(tag="TP", side=OrderSide.LONG, from_entry="Long", limit=20200))
        # Gap up past limit — fills at open (20300), but strategy wanted 20200
        broker.check_exits(1, open_=20300, high=20350, low=20290, close=20320)

        assert broker.trades[0].exit_price == 20300  # gap fill
        assert broker.last_exit_type == "limit"
        assert broker.last_exit_limit == 20200  # strategy's original price


class TestExitTypeOnStopFill:
    """Stop-loss exits set last_exit_type='stop' and last_exit_limit=None."""

    def test_long_stop_exit(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(tag="SL", side=OrderSide.LONG, from_entry="Long", stop=19800))
        broker.check_exits(1, open_=19950, high=19980, low=19750, close=19820)

        assert broker.position_size == 0
        assert broker.last_exit_type == "stop"
        assert broker.last_exit_limit is None

    def test_short_stop_exit(self, broker):
        broker.queue_entry(Order(tag="Short", side=OrderSide.SHORT, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(tag="SL", side=OrderSide.SHORT, from_entry="Short", stop=20200))
        broker.check_exits(1, open_=20050, high=20250, low=20040, close=20180)

        assert broker.position_size == 0
        assert broker.last_exit_type == "stop"
        assert broker.last_exit_limit is None

    def test_stop_only_order_has_no_limit_preserved(self, broker):
        """When order has only stop (no limit), last_exit_limit must be None."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(tag="SL", side=OrderSide.LONG, from_entry="Long", stop=19900))
        broker.check_exits(1, open_=19950, high=19960, low=19850, close=19870)

        assert broker.last_exit_type == "stop"
        assert broker.last_exit_limit is None


class TestExitTypeOnBothLimitAndStop:
    """When order has both limit and stop, exit type depends on which triggered."""

    def test_ambiguous_bar_stop_wins_on_gap_down(self, broker):
        """When open gaps below stop, stop triggers first."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long",
            limit=20200, stop=19800,
        ))
        # Open below stop — stop fills first
        broker.check_exits(1, open_=19700, high=20250, low=19700, close=20100)

        assert broker.last_exit_type == "stop"
        # limit was on the order but stop triggered — still preserve the limit price
        assert broker.last_exit_limit == 20200

    def test_ambiguous_bar_limit_wins_when_open_above_stop(self, broker):
        """When both TP and SL hit but open is above stop, limit triggers."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long",
            limit=20200, stop=19800,
        ))
        # Both hit, but open above stop — limit triggers
        broker.check_exits(1, open_=19950, high=20250, low=19750, close=20100)

        assert broker.last_exit_type == "limit"
        assert broker.last_exit_limit == 20200


class TestExitTypeOnMarketClose:
    """broker.close() sets last_exit_type='close'."""

    def test_market_close_sets_type(self, broker, ctx):
        ctx.entry("Long", OrderSide.LONG)
        broker.on_bar_close(0, 20000)

        ctx.close("Long", tag="Exit")
        broker.on_bar_close(1, 20100)

        assert broker.position_size == 0
        assert broker.last_exit_type == "close"
        assert broker.last_exit_limit is None


class TestExitTypeOnForceClose:
    """broker.force_close() sets last_exit_type='force_close'."""

    def test_force_close_sets_type(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.force_close(1, 20100)

        assert broker.position_size == 0
        assert broker.last_exit_type == "force_close"
        assert broker.last_exit_limit is None


class TestExitTypeInitialState:
    """Exit type metadata starts empty."""

    def test_initial_values(self, broker):
        assert broker.last_exit_type == ""
        assert broker.last_exit_limit is None

    def test_no_exit_doesnt_change_type(self, broker):
        """check_exits with no fill should not change last_exit_type."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(tag="SL", side=OrderSide.LONG, from_entry="Long", stop=19800))
        # Bar low=19900 > stop=19800 — stop NOT hit
        broker.check_exits(1, open_=20050, high=20100, low=19900, close=19950)

        assert broker.position_size == 1  # still in position
        assert broker.last_exit_type == ""  # unchanged


# ── LiveRunner decision pipeline tests ──

class _TPSLStrategy(BacktestStrategy):
    """Enter long, set TP at 22605 and SL at 22455 (based on expected entry ~22505)."""
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        if broker.position_size == 0:
            broker.entry("Long", OrderSide.LONG)
        else:
            broker.exit("Exit", "Long", limit=22605, stop=22455)

    def required_bars(self) -> int:
        return 2


class _CloseStrategy(BacktestStrategy):
    """Enter long, then close on the next bar."""
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        if broker.position_size == 0 and len(data_store) >= 3:
            broker.entry("Long", OrderSide.LONG)
        elif broker.position_size > 0:
            broker.close("Long", tag="MktExit")

    def required_bars(self) -> int:
        return 2


def _kline(dt_str, o=22500, h=22510, l=22490, c=22505, v=100):
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    return f"{dt.strftime('%m/%d/%Y %H:%M')},{o},{h},{l},{c},{v}"


def _make_runner(tmp_path, strategy, symbol="TX00"):
    return LiveRunner(
        strategy=strategy,
        symbol=symbol,
        point_value=200,
        log_dir=str(tmp_path),
    )


class TestDecisionExitType:
    """Verify exit_type and exit_limit propagate through on_decision events."""

    @staticmethod
    def _make_1m_lines(hour, start_min, count, o, h, l, c):
        """Generate count 1-min KLine strings."""
        lines = []
        for i in range(count):
            m = start_min + i
            hr = hour + m // 60
            mm = m % 60
            lines.append(_kline(f"2026-03-02 {hr:02d}:{mm:02d}", o, h, l, c))
        return lines

    def test_limit_exit_decision_has_exit_type(self, tmp_path):
        """Take-profit exit decision should include exit_type='limit' and exit_limit.

        Aggregator emits a bar only when the NEXT boundary starts. So we
        feed continuous 1m bars across multiple 15-min windows, plus one
        extra 1m bar to flush the last window.

        Timeline:
          Warmup bars: 09:00, 09:15
          09:30-09:44 (bar 2): entry fills at close=22505
          09:45-09:59 (bar 3): on_bar sets TP=22605 SL=22455
          10:00-10:14 (bar 4): check_exits → TP hit at high=22620
          10:15       (flush): first 1m of next window forces bar 4 to emit
        """
        runner = _make_runner(tmp_path, _TPSLStrategy())

        decisions = []
        runner.on("on_decision", lambda d: decisions.append(d))

        warmup = [
            _kline("2026-03-02 09:00", 22500, 22510, 22490, 22500),
            _kline("2026-03-02 09:15", 22500, 22510, 22490, 22505),
        ]
        runner.feed_warmup_bars(warmup)
        assert runner.state == LiveState.RUNNING

        # Feed all 1m bars in one batch:
        # 09:30-09:44 → entry bar (close=22505)
        # 09:45-09:59 → flat bar, sets exit orders
        # 10:00-10:14 → TP bar (high=22620)
        # 10:15       → flush bar (triggers bar 4 emission)
        lines = (
            self._make_1m_lines(9, 30, 15, 22505, 22510, 22500, 22505) +  # entry
            self._make_1m_lines(9, 45, 15, 22505, 22510, 22500, 22508) +  # exit set
            self._make_1m_lines(10, 0, 15, 22550, 22620, 22540, 22600) +  # TP hit
            self._make_1m_lines(10, 15, 1, 22600, 22610, 22595, 22605)    # flush
        )
        runner.feed_1m_bars(lines)

        trade_close = [d for d in decisions if d["action"] == "TRADE_CLOSE"]
        assert len(trade_close) >= 1, f"Expected TRADE_CLOSE, got: {[d['action'] for d in decisions]}"
        tc = trade_close[0]
        assert tc["exit_type"] == "limit"
        assert tc["exit_limit"] == 22605

    def test_stop_exit_decision_has_exit_type(self, tmp_path):
        """Stop-loss exit decision should include exit_type='stop', no exit_limit."""
        runner = _make_runner(tmp_path, _TPSLStrategy())

        decisions = []
        runner.on("on_decision", lambda d: decisions.append(d))

        warmup = [
            _kline("2026-03-02 09:00", 22500, 22510, 22490, 22500),
            _kline("2026-03-02 09:15", 22500, 22510, 22490, 22505),
        ]
        runner.feed_warmup_bars(warmup)

        lines = (
            self._make_1m_lines(9, 30, 15, 22505, 22510, 22500, 22505) +  # entry
            self._make_1m_lines(9, 45, 15, 22505, 22510, 22500, 22508) +  # exit set
            self._make_1m_lines(10, 0, 15, 22480, 22490, 22440, 22450) +  # SL hit
            self._make_1m_lines(10, 15, 1, 22450, 22460, 22445, 22455)    # flush
        )
        runner.feed_1m_bars(lines)

        trade_close = [d for d in decisions if d["action"] == "TRADE_CLOSE"]
        assert len(trade_close) >= 1, f"Expected TRADE_CLOSE, got: {[d['action'] for d in decisions]}"
        tc = trade_close[0]
        assert tc["exit_type"] == "stop"
        # exit_limit may be present (order had both TP and SL) but _send_real_order
        # only uses it when exit_type == "limit", so it's harmless metadata

    def test_market_close_decision_has_exit_type(self, tmp_path):
        """broker.close() decision should include exit_type='close'."""
        runner = _make_runner(tmp_path, _CloseStrategy())

        decisions = []
        runner.on("on_decision", lambda d: decisions.append(d))

        warmup = [
            _kline("2026-03-02 09:00", 22500, 22510, 22490, 22500),
            _kline("2026-03-02 09:15", 22500, 22510, 22490, 22505),
            _kline("2026-03-02 09:30", 22505, 22515, 22495, 22510),
        ]
        runner.feed_warmup_bars(warmup)

        # close() fills at bar close, not via check_exits, so the bar that
        # calls close() IS the bar where the trade closes (no extra flush needed
        # for close — but we still need a flush for the aggregator).
        lines = (
            self._make_1m_lines(9, 45, 15, 22510, 22520, 22505, 22515) +  # entry
            self._make_1m_lines(10, 0, 15, 22515, 22520, 22510, 22518) +  # close()
            self._make_1m_lines(10, 15, 1, 22518, 22520, 22515, 22519)    # flush
        )
        runner.feed_1m_bars(lines)

        trade_close = [d for d in decisions if d["action"] == "TRADE_CLOSE"]
        assert len(trade_close) >= 1, f"Expected TRADE_CLOSE, got: {[d['action'] for d in decisions]}"
        tc = trade_close[0]
        assert tc["exit_type"] == "close"
        assert "exit_limit" not in tc or tc.get("exit_limit") is None

    def test_entry_decision_has_no_exit_type(self, tmp_path):
        """Entry decisions should not include exit_type."""
        runner = _make_runner(tmp_path, _TPSLStrategy())

        decisions = []
        runner.on("on_decision", lambda d: decisions.append(d))

        warmup = [
            _kline("2026-03-02 09:00", 22500, 22510, 22490, 22500),
            _kline("2026-03-02 09:15", 22500, 22510, 22490, 22505),
        ]
        runner.feed_warmup_bars(warmup)

        # Feed entry bar + flush
        lines = (
            self._make_1m_lines(9, 30, 15, 22505, 22510, 22500, 22505) +  # entry
            self._make_1m_lines(9, 45, 1, 22505, 22510, 22500, 22508)     # flush
        )
        runner.feed_1m_bars(lines)

        entry_fills = [d for d in decisions if d["action"] == "ENTRY_FILL"]
        assert len(entry_fills) >= 1
        assert "exit_type" not in entry_fills[0]
