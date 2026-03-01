"""Tests for SimulatedBroker order matching."""

import pytest

from src.backtest.broker import SimulatedBroker, OrderSide, Order


@pytest.fixture
def broker():
    return SimulatedBroker(point_value=200)


class TestEntryFills:
    def test_entry_fills_at_bar_close(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(bar_index=0, close=20000)

        assert broker.position_size == 1
        assert broker.entry_price == 20000
        assert broker.position_side == OrderSide.LONG

    def test_no_duplicate_entry_while_in_position(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_entry(Order(tag="Long2", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(1, 20100)

        assert broker.position_size == 1
        assert broker.entry_price == 20000  # still original entry

    def test_entry_clears_pending(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        assert len(broker._pending_entries) == 0


class TestStopLossExit:
    def test_stop_loss_fills_at_stop_price(self, broker):
        # Enter long at 20000
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        # Queue exit with stop at 19900
        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long",
            stop=19900,
        ))

        # Bar hits stop: low=19850
        broker.check_exits(1, open_=19980, high=20050, low=19850, close=19870)

        assert broker.position_size == 0
        assert len(broker.trades) == 1
        assert broker.trades[0].exit_price == 19900
        assert broker.trades[0].pnl == (19900 - 20000) * 1 * 200  # -20000

    def test_stop_loss_gap_down_fills_at_open(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long",
            stop=19900,
        ))

        # Gap down below stop: open=19850
        broker.check_exits(1, open_=19850, high=19900, low=19800, close=19860)

        assert broker.trades[0].exit_price == 19850  # fills at open (slippage)


class TestTakeProfitExit:
    def test_take_profit_fills_at_limit_price(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long",
            limit=20200,
        ))

        broker.check_exits(1, open_=20050, high=20300, low=20000, close=20250)

        assert broker.position_size == 0
        assert broker.trades[0].exit_price == 20200
        assert broker.trades[0].pnl == (20200 - 20000) * 1 * 200  # +40000

    def test_take_profit_gap_up_fills_at_open(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long",
            limit=20200,
        ))

        # Gap up above limit: open=20250
        broker.check_exits(1, open_=20250, high=20300, low=20200, close=20280)

        assert broker.trades[0].exit_price == 20250  # fills at open


class TestAmbiguousBar:
    def test_both_hit_open_below_stop_fills_stop(self, broker):
        """When bar hits both SL and TP, and open <= stop, SL fills first."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long",
            limit=20200, stop=19900,
        ))

        # Both hit: open gaps below stop
        broker.check_exits(1, open_=19880, high=20300, low=19800, close=20100)

        assert broker.trades[0].exit_price == 19880  # stop fills at open (gap)

    def test_both_hit_open_above_stop_fills_limit(self, broker):
        """When bar hits both SL and TP, and open > stop, TP fills first."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long",
            limit=20200, stop=19900,
        ))

        # Both hit but open > stop
        broker.check_exits(1, open_=19950, high=20300, low=19800, close=20100)

        assert broker.trades[0].exit_price == 20200  # limit fills


class TestForceClose:
    def test_force_close_at_end_of_data(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        broker.force_close(10, 20150)

        assert broker.position_size == 0
        assert broker.trades[0].exit_price == 20150
        assert broker.trades[0].pnl == (20150 - 20000) * 1 * 200

    def test_force_close_no_position_is_noop(self, broker):
        broker.force_close(10, 20000)
        assert len(broker.trades) == 0


class TestEquityCurve:
    def test_equity_tracks_cumulative_pnl(self, broker):
        # Trade 1: +100 points
        broker.queue_entry(Order(tag="L1", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.queue_exit(Order(tag="X1", side=OrderSide.LONG, from_entry="L1", limit=20100))
        broker.check_exits(1, open_=20050, high=20200, low=20000, close=20150)

        # Trade 2: -50 points
        broker.queue_entry(Order(tag="L2", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(2, 20200)
        broker.queue_exit(Order(tag="X2", side=OrderSide.LONG, from_entry="L2", stop=20150))
        broker.check_exits(3, open_=20180, high=20200, low=20100, close=20120)

        assert len(broker.equity_curve) == 2
        assert broker.equity_curve[0] == 100 * 200   # +20000
        assert broker.equity_curve[1] == 100 * 200 + (-50) * 200  # +10000


class TestPointValue:
    def test_default_point_value(self):
        b = SimulatedBroker(point_value=1)
        b.queue_entry(Order(tag="L", side=OrderSide.LONG, qty=1))
        b.on_bar_close(0, 100)
        b.force_close(1, 110)
        assert b.trades[0].pnl == 10  # raw points

    def test_tx_point_value(self):
        b = SimulatedBroker(point_value=200)
        b.queue_entry(Order(tag="L", side=OrderSide.LONG, qty=1))
        b.on_bar_close(0, 20000)
        b.force_close(1, 20100)
        assert b.trades[0].pnl == 100 * 200  # TWD 20000
