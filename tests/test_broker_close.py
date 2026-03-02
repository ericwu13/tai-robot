"""Tests for broker.close() market exit and exit() edge cases."""

import pytest

from src.backtest.broker import SimulatedBroker, BrokerContext, OrderSide, Order


@pytest.fixture
def broker():
    return SimulatedBroker(point_value=200)


@pytest.fixture
def ctx(broker):
    return BrokerContext(broker)


class TestMarketClose:
    """Tests for broker.close() — market exit at bar close."""

    def test_close_fills_at_bar_close(self, broker, ctx):
        """close() should exit at the current bar's close price."""
        ctx.entry("Long", OrderSide.LONG)
        broker.on_bar_close(0, 20000)
        assert broker.position_size == 1

        ctx.close("Long", tag="Exit_TP")
        broker.on_bar_close(1, 20150)

        assert broker.position_size == 0
        assert len(broker.trades) == 1
        assert broker.trades[0].exit_price == 20150
        assert broker.trades[0].pnl == (20150 - 20000) * 200
        assert broker.trades[0].exit_tag == "Exit_TP"

    def test_close_losing_trade(self, broker, ctx):
        """close() on a losing position records negative P&L."""
        ctx.entry("Long", OrderSide.LONG)
        broker.on_bar_close(0, 20000)

        ctx.close("Long", tag="Exit_SL")
        broker.on_bar_close(1, 19850)

        assert broker.position_size == 0
        assert broker.trades[0].pnl == (19850 - 20000) * 200  # -30000

    def test_close_allows_re_entry_next_bar(self, broker, ctx):
        """After close(), a new entry can fill on the next bar."""
        # Trade 1
        ctx.entry("L1", OrderSide.LONG)
        broker.on_bar_close(0, 20000)

        ctx.close("L1")
        broker.on_bar_close(1, 20100)
        assert broker.position_size == 0

        # Trade 2 — new entry on next bar
        ctx.entry("L2", OrderSide.LONG)
        broker.on_bar_close(2, 20050)
        assert broker.position_size == 1
        assert broker.entry_price == 20050

    def test_close_no_entry_on_same_bar(self, broker, ctx):
        """Entry on same bar as close should be skipped (exit priority)."""
        ctx.entry("L1", OrderSide.LONG)
        broker.on_bar_close(0, 20000)

        # Close and re-enter on same bar
        ctx.close("L1")
        ctx.entry("L2", OrderSide.LONG)
        broker.on_bar_close(1, 20100)

        # Close should have happened, entry skipped (same bar as exit)
        assert broker.position_size == 0
        assert len(broker.trades) == 1

    def test_close_wrong_from_entry_is_ignored(self, broker, ctx):
        """close() with non-matching from_entry should not close the position."""
        ctx.entry("Long", OrderSide.LONG)
        broker.on_bar_close(0, 20000)

        ctx.close("WrongTag")
        broker.on_bar_close(1, 20100)

        assert broker.position_size == 1  # still open
        assert len(broker.trades) == 0

    def test_close_no_position_is_noop(self, broker, ctx):
        """close() when flat should do nothing."""
        ctx.close("Long")
        broker.on_bar_close(0, 20000)

        assert broker.position_size == 0
        assert len(broker.trades) == 0

    def test_close_and_exit_coexist(self, broker, ctx):
        """close() should work even when limit/stop exits are pending."""
        ctx.entry("Long", OrderSide.LONG)
        broker.on_bar_close(0, 20000)

        # Queue both a limit/stop exit and a market close
        ctx.exit("Exit_LS", "Long", limit=20200, stop=19800)
        ctx.close("Long", tag="Exit_Manual")
        broker.on_bar_close(1, 20050)

        # Market close should fill first (at bar close)
        assert broker.position_size == 0
        assert broker.trades[0].exit_tag == "Exit_Manual"
        assert broker.trades[0].exit_price == 20050

    def test_multiple_trades_via_close(self, broker, ctx):
        """Multiple round-trips using close() should all record correctly."""
        for i in range(3):
            ctx.entry(f"L{i}", OrderSide.LONG)
            broker.on_bar_close(i * 3, 20000 + i * 100)

            ctx.close(f"L{i}")
            broker.on_bar_close(i * 3 + 1, 20050 + i * 100)

            # Skip a bar to avoid same-bar entry block
            broker.on_bar_close(i * 3 + 2, 20060 + i * 100)

        assert len(broker.trades) == 3
        assert all(t.pnl == 50 * 200 for t in broker.trades)


class TestExitWithoutPrices:
    """Verify that exit() with no limit/stop is still a no-op (documented behavior)."""

    def test_exit_no_limit_no_stop_does_nothing(self, broker, ctx):
        """exit() without limit or stop should never fill — use close() instead."""
        ctx.entry("Long", OrderSide.LONG)
        broker.on_bar_close(0, 20000)

        ctx.exit("Exit", "Long")  # no limit, no stop
        broker.check_exits(1, open_=20050, high=20200, low=19800, close=20100)

        assert broker.position_size == 1  # still open
        assert len(broker.trades) == 0
