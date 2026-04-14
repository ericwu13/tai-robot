"""Regression tests for session-end force-close direction bug.

The bug: _check_session_end_close read `broker.trades[-1].side` (the PREVIOUS
completed trade) instead of `broker.position_side` (the CURRENT open position).
This caused force-close to compute the wrong buy_sell direction, sending an
order that OPENS a second position instead of closing the existing one.

Incident: 2026-04-14 04:58:26 night session close — error 980 (insufficient
margin) because the close order went in the same direction as the position.
"""

import pytest

from src.backtest.broker import SimulatedBroker, BrokerContext, OrderSide
from src.live.trading_guard import TradingGuard


# ── Helpers ──

def _make_broker_with_position(side: OrderSide, prior_trades: list[OrderSide] | None = None):
    """Create a broker with an open position, optionally preceded by closed trades.

    Args:
        side: the side of the CURRENT open position (LONG or SHORT).
        prior_trades: list of sides for prior completed trades (closed before
            the current position was opened).

    Returns:
        (broker, ctx) with the open position active.
    """
    broker = SimulatedBroker(point_value=200)
    ctx = BrokerContext(broker)
    bar = 0

    # Fill prior trades (each: entry -> fill -> close)
    for prior_side in (prior_trades or []):
        ctx.entry(f"prior_{bar}", prior_side)
        broker.on_bar_close(bar, 20000)
        bar += 1
        # Close via force_close to keep it simple
        broker.force_close(bar, 20050, f"2026-01-01 09:{bar:02d}")
        bar += 1

    # Open the current position
    ctx.entry("current", side)
    broker.on_bar_close(bar, 20100)
    bar += 1

    assert broker.position_size == 1, "Position must be open"
    assert broker.position_side == side, f"Expected {side}, got {broker.position_side}"
    return broker, ctx


# ── Tests: position_side gives the correct direction ──

class TestPositionSideDirection:
    """Verify that broker.position_side reflects the CURRENT open position,
    not the last completed trade."""

    def test_long_position_no_prior_trades(self):
        """LONG position, no prior trades: side should be LONG (not empty)."""
        broker, _ = _make_broker_with_position(OrderSide.LONG)
        side = broker.position_side.value if broker.position_side else ""
        assert side == "LONG"

    def test_long_position_with_prior_short(self):
        """LONG position after a completed SHORT trade: side should be LONG (not SHORT).

        This is the exact scenario that caused the incident — trades[-1].side
        was SHORT (the previous trade), but the open position is LONG.
        """
        broker, _ = _make_broker_with_position(
            OrderSide.LONG, prior_trades=[OrderSide.SHORT])
        # Bug: trades[-1].side.value would return "SHORT" (wrong!)
        assert broker.trades[-1].side == OrderSide.SHORT, "Prior trade was SHORT"
        # Fix: position_side returns the CURRENT position
        side = broker.position_side.value if broker.position_side else ""
        assert side == "LONG"

    def test_short_position_no_prior_trades(self):
        """SHORT position, no prior trades: side should be SHORT."""
        broker, _ = _make_broker_with_position(OrderSide.SHORT)
        side = broker.position_side.value if broker.position_side else ""
        assert side == "SHORT"

    def test_short_position_with_prior_long(self):
        """SHORT position after a completed LONG trade: side should be SHORT (not LONG)."""
        broker, _ = _make_broker_with_position(
            OrderSide.SHORT, prior_trades=[OrderSide.LONG])
        assert broker.trades[-1].side == OrderSide.LONG, "Prior trade was LONG"
        side = broker.position_side.value if broker.position_side else ""
        assert side == "SHORT"

    def test_flat_position(self):
        """No open position: side should be empty string."""
        broker = SimulatedBroker(point_value=200)
        side = broker.position_side.value if broker.position_side else ""
        assert side == ""

    def test_flat_after_close_with_trades(self):
        """After closing a LONG, position_side is None even though trades exist."""
        broker, _ = _make_broker_with_position(OrderSide.LONG)
        broker.force_close(10, 20200, "2026-01-01 10:00")
        assert broker.position_size == 0
        assert broker.position_side is None
        # trades[-1] still has the old side — this is what the bug read
        assert broker.trades[-1].side == OrderSide.LONG


# ── Tests: TradingGuard.decide computes correct buy_sell ──

class TestForceCloseDecisionDirection:
    """Verify that TradingGuard.decide() produces the correct buy_sell
    for force-close given the position side."""

    def test_long_force_close_sends_sell(self):
        """LONG position force-close: buy_sell should be 1 (SELL)."""
        guard = TradingGuard()
        guard.on_entry_sent()  # mark real position exists
        _, details = guard.decide("auto", "FORCE_CLOSE", "LONG")
        assert details["buy_sell"] == 1, "LONG force-close must SELL (1)"
        assert details["action_type"] == "exit"

    def test_short_force_close_sends_buy(self):
        """SHORT position force-close: buy_sell should be 0 (BUY)."""
        guard = TradingGuard()
        guard.on_entry_sent()
        _, details = guard.decide("auto", "FORCE_CLOSE", "SHORT")
        assert details["buy_sell"] == 0, "SHORT force-close must BUY (0)"
        assert details["action_type"] == "exit"

    def test_wrong_side_produces_wrong_direction(self):
        """Demonstrate the bug: if side="" (from empty trades), buy_sell=0 (BUY).

        For a LONG position, the correct close is SELL (1). If the buggy code
        reads side="" because trades is empty, decide() gets "" which falls
        into the `else` branch and produces buy_sell=0 (BUY) — opening a
        second position instead of closing.
        """
        guard = TradingGuard()
        guard.on_entry_sent()
        _, details = guard.decide("auto", "FORCE_CLOSE", "")
        # With side="", the code does: buy_sell = 1 if "" == "LONG" else 0 → 0 (BUY)
        assert details["buy_sell"] == 0, "Empty side falls through to BUY"
        # This is WRONG for a LONG position — it should be SELL (1)

    def test_swapped_side_produces_wrong_direction(self):
        """Demonstrate the bug: prior SHORT trade + current LONG position.

        If buggy code reads trades[-1].side = "SHORT", decide() computes
        buy_sell = 1 if "SHORT" == "LONG" else 0 → 0 (BUY).
        For a LONG position, correct close is SELL (1). BUY opens a second
        position → error 980 (insufficient margin).
        """
        guard = TradingGuard()
        guard.on_entry_sent()
        _, details = guard.decide("auto", "FORCE_CLOSE", "SHORT")
        assert details["buy_sell"] == 0, "SHORT close = BUY (0)"
        # If the position is actually LONG, this BUY is WRONG


# ── Tests: force-close uses sNewClose=1 (explicit close) ──

class TestForceCloseNewClose:
    """Verify that FORCE_CLOSE uses sNewClose=1, not 2 (auto)."""

    def test_force_close_uses_explicit_close(self):
        guard = TradingGuard()
        guard.on_entry_sent()
        _, details = guard.decide("auto", "FORCE_CLOSE", "LONG")
        assert details["new_close"] == 1, "Force-close must use sNewClose=1 (explicit close)"

    def test_normal_exit_uses_auto(self):
        guard = TradingGuard()
        guard.on_entry_sent()
        _, details = guard.decide("auto", "TRADE_CLOSE", "LONG")
        assert details["new_close"] == 2, "Normal exit uses sNewClose=2 (auto)"

    def test_entry_uses_new(self):
        guard = TradingGuard()
        _, details = guard.decide("auto", "ENTRY_FILL", "LONG")
        assert details["new_close"] == 0, "Entry uses sNewClose=0 (new)"


# ── Integration: full flow from broker state to decide() ──

class TestEndToEndForceCloseDirection:
    """Full integration: create broker state, extract side, pass to decide()."""

    @pytest.mark.parametrize("current_side,prior_sides,expected_buy_sell", [
        (OrderSide.LONG, [], 1),              # LONG → SELL
        (OrderSide.LONG, [OrderSide.SHORT], 1),  # prior SHORT, current LONG → SELL
        (OrderSide.SHORT, [], 0),             # SHORT → BUY
        (OrderSide.SHORT, [OrderSide.LONG], 0),  # prior LONG, current SHORT → BUY
        (OrderSide.LONG, [OrderSide.LONG, OrderSide.SHORT], 1),  # multiple priors
        (OrderSide.SHORT, [OrderSide.SHORT, OrderSide.LONG], 0),
    ])
    def test_position_side_to_decide(self, current_side, prior_sides, expected_buy_sell):
        """End-to-end: broker.position_side → decide() → correct buy_sell."""
        broker, _ = _make_broker_with_position(current_side, prior_trades=prior_sides)

        # This is the FIXED code path:
        side = broker.position_side.value if broker.position_side else ""

        guard = TradingGuard()
        guard.on_entry_sent()
        _, details = guard.decide("auto", "FORCE_CLOSE", side)
        assert details["buy_sell"] == expected_buy_sell, (
            f"current={current_side.value} priors={[s.value for s in prior_sides]} "
            f"→ expected buy_sell={expected_buy_sell}, got {details['buy_sell']}")

    @pytest.mark.parametrize("current_side,prior_sides", [
        (OrderSide.LONG, [OrderSide.SHORT]),
        (OrderSide.SHORT, [OrderSide.LONG]),
    ])
    def test_buggy_code_would_fail(self, current_side, prior_sides):
        """Prove that the OLD buggy code (trades[-1].side) gives WRONG direction."""
        broker, _ = _make_broker_with_position(current_side, prior_trades=prior_sides)

        # BUGGY path: reads last completed trade's side
        buggy_side = broker.trades[-1].side.value if broker.trades else ""
        # FIXED path: reads current open position's side
        fixed_side = broker.position_side.value if broker.position_side else ""

        assert buggy_side != fixed_side, (
            f"Bug scenario: trades[-1].side={buggy_side} != position_side={fixed_side}")

        guard = TradingGuard()
        guard.on_entry_sent()
        _, buggy_details = guard.decide("auto", "FORCE_CLOSE", buggy_side)
        _, fixed_details = guard.decide("auto", "FORCE_CLOSE", fixed_side)
        assert buggy_details["buy_sell"] != fixed_details["buy_sell"], (
            "Buggy and fixed code should produce DIFFERENT buy_sell in this scenario")
