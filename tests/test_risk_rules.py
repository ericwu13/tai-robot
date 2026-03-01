"""Tests for risk management rules."""

import time

from src.execution.position_tracker import Fill, PositionTracker
from src.market_data.models import Direction, Signal
from src.risk.rules import (
    MaxDailyLossRule,
    MaxDrawdownRule,
    MaxPositionRule,
    OrderRateLimitRule,
)


def _signal(direction: Direction) -> Signal:
    return Signal(direction=direction, price=20000)


class TestMaxPositionRule:
    def test_within_limit(self, position_tracker):
        rule = MaxPositionRule(max_position=2)
        result = rule.check(_signal(Direction.BUY), "TXFD0", 1, position_tracker)
        assert result is None

    def test_exceeds_limit(self, position_tracker):
        # Set existing position to 2
        position_tracker.on_fill(Fill("TXFD0", 0, 20000, 2))
        rule = MaxPositionRule(max_position=2)
        result = rule.check(_signal(Direction.BUY), "TXFD0", 1, position_tracker)
        assert result is not None
        assert "exceeds" in result

    def test_sell_reduces_position(self, position_tracker):
        position_tracker.on_fill(Fill("TXFD0", 0, 20000, 2))
        rule = MaxPositionRule(max_position=2)
        # Selling should reduce position, should pass
        result = rule.check(_signal(Direction.SELL), "TXFD0", 1, position_tracker)
        assert result is None

    def test_short_position_limit(self, position_tracker):
        position_tracker.on_fill(Fill("TXFD0", 1, 20000, 2))  # short 2
        rule = MaxPositionRule(max_position=2)
        result = rule.check(_signal(Direction.SELL), "TXFD0", 1, position_tracker)
        assert result is not None

    def test_flat_signal_passes(self, position_tracker):
        rule = MaxPositionRule(max_position=1)
        result = rule.check(_signal(Direction.FLAT), "TXFD0", 1, position_tracker)
        assert result is None


class TestMaxDailyLossRule:
    def test_no_loss(self, position_tracker):
        rule = MaxDailyLossRule(max_daily_loss=20000)
        result = rule.check(_signal(Direction.BUY), "TXFD0", 1, position_tracker)
        assert result is None

    def test_loss_exceeded(self, position_tracker):
        # Simulate losses: buy at 20100, sell at 20000 -> loss of 100 per contract x 201 = -20100
        position_tracker.on_fill(Fill("TXFD0", 0, 20100, 201))  # buy 201 @ 20100
        position_tracker.on_fill(Fill("TXFD0", 1, 20000, 201))  # sell 201 @ 20000 -> -20100

        rule = MaxDailyLossRule(max_daily_loss=20000)
        result = rule.check(_signal(Direction.BUY), "TXFD0", 1, position_tracker)
        assert result is not None
        assert "Daily loss" in result


class TestOrderRateLimitRule:
    def test_within_limit(self):
        rule = OrderRateLimitRule(max_per_minute=5)
        tracker = PositionTracker()
        for _ in range(4):
            result = rule.check(_signal(Direction.BUY), "TXFD0", 1, tracker)
            assert result is None

    def test_exceeds_limit(self):
        rule = OrderRateLimitRule(max_per_minute=3)
        tracker = PositionTracker()
        for _ in range(3):
            rule.check(_signal(Direction.BUY), "TXFD0", 1, tracker)
        result = rule.check(_signal(Direction.BUY), "TXFD0", 1, tracker)
        assert result is not None
        assert "rate limit" in result


class TestMaxDrawdownRule:
    def test_no_drawdown(self, position_tracker):
        rule = MaxDrawdownRule(max_drawdown_pct=5.0, starting_equity=1_000_000)
        result = rule.check(_signal(Direction.BUY), "TXFD0", 1, position_tracker)
        assert result is None

    def test_drawdown_exceeded(self, position_tracker):
        # Simulate big loss: -500 per contract x 110 = -55000 (5.5% of 1M)
        position_tracker.on_fill(Fill("TXFD0", 0, 20000, 110))
        position_tracker.on_fill(Fill("TXFD0", 1, 19500, 110))  # -55000 loss

        rule = MaxDrawdownRule(max_drawdown_pct=5.0, starting_equity=1_000_000)
        result = rule.check(_signal(Direction.BUY), "TXFD0", 1, position_tracker)
        assert result is not None
        assert "Drawdown" in result

    def test_no_equity_tracking(self, position_tracker):
        # If starting_equity is 0, rule passes
        rule = MaxDrawdownRule(max_drawdown_pct=5.0, starting_equity=0)
        result = rule.check(_signal(Direction.BUY), "TXFD0", 1, position_tracker)
        assert result is None
