"""Tests for SimulatedBroker order matching."""

import pytest

from src.backtest.broker import SimulatedBroker, OrderSide, Order, Trade


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
        assert b.trades[0].pnl == (20100 - 20000) * 1 * 200


# ── Regression: issue #45 — real entry price capture ──

class TestRealEntryPriceLifecycle:
    """Regression tests for issue #45.

    The SimulatedBroker gains two new fields — ``real_entry_price`` and
    ``real_entry_dt`` — which the GUI's ``_on_fill_confirmed`` writes
    when the real broker confirms a fill. These must:

    1. Start at 0/""
    2. Be copied into the Trade on _close_position and then reset
    3. Be reset at new-entry time as belt-and-braces (in case a late
       fill-confirmation callback slipped past the guard)
    4. Survive to_dict/from_dict round-trips (with backward-compat
       defaults for old session files missing the keys)
    5. Be accessible via ``effective_entry_price()`` which falls back
       to the simulated entry price when real is unset
    """

    def test_initial_state_is_zero(self, broker):
        assert broker.real_entry_price == 0
        assert broker.real_entry_dt == ""

    def test_real_entry_survives_stop_loss_close(self, broker):
        """Stop-loss close path copies real_entry_price into the Trade."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        # Simulate what _on_fill_confirmed would do after the real fill
        broker.real_entry_price = 20003
        broker.real_entry_dt = "2026-04-09T00:18:33"

        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long", stop=19900))
        broker.check_exits(1, open_=19980, high=20050, low=19850, close=19870)

        assert len(broker.trades) == 1
        t = broker.trades[0]
        assert t.real_entry_price == 20003
        assert t.real_entry_dt == "2026-04-09T00:18:33"
        # Broker field reset after _close_position
        assert broker.real_entry_price == 0
        assert broker.real_entry_dt == ""

    def test_real_entry_survives_take_profit_close(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 19998
        broker.real_entry_dt = "2026-04-09T00:18:33"

        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long", limit=20200))
        broker.check_exits(1, open_=20050, high=20300, low=20000, close=20250)

        assert broker.trades[0].real_entry_price == 19998
        assert broker.real_entry_price == 0

    def test_real_entry_survives_force_close(self, broker):
        """force_close (session end / bot stop) preserves real_entry_price."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 20005
        broker.real_entry_dt = "2026-04-09T00:18:33"

        broker.force_close(1, 20050, bar_dt="2026-04-09 05:00")

        assert broker.trades[0].real_entry_price == 20005
        assert broker.trades[0].real_entry_dt == "2026-04-09T00:18:33"
        assert broker.real_entry_price == 0

    def test_real_entry_survives_market_close(self, broker):
        """broker.close() → market close via on_bar_close preserves real_entry_price."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 19999
        broker.real_entry_dt = "2026-04-09T00:18:33"

        broker.queue_market_close("MktExit", "Long")
        broker.on_bar_close(1, 20100)

        assert broker.trades[0].real_entry_price == 19999
        assert broker.real_entry_price == 0

    def test_real_entry_zero_in_paper_mode(self, broker):
        """Pure sim flow never touches real_entry — Trade has 0/""."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long", stop=19900))
        broker.check_exits(1, open_=19980, high=20050, low=19850, close=19870)

        assert broker.trades[0].real_entry_price == 0
        assert broker.trades[0].real_entry_dt == ""

    def test_new_entry_clears_stale_real_entry_price(self, broker):
        """Belt-and-braces: on_bar_close resets real_entry_price for a new entry.

        Protects against the race where a late fill confirmation from a
        previously-closed trade slips past the _on_fill_confirmed guard
        and plants a stale value on the broker just before a new entry
        is processed.
        """
        # Simulate stale leak (as if a late callback planted this on flat broker)
        broker.real_entry_price = 99999
        broker.real_entry_dt = "stale"
        # Now a new entry comes in
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)

        # Belt-and-braces reset clears the stale value
        assert broker.real_entry_price == 0
        assert broker.real_entry_dt == ""
        assert broker.entry_price == 20000  # new sim entry

    def test_effective_entry_price_returns_real_when_set(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 20003

        assert broker.effective_entry_price() == 20003

    def test_effective_entry_price_falls_back_to_sim(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        # real_entry_price never set

        assert broker.effective_entry_price() == 20000

    def test_effective_entry_price_when_flat(self, broker):
        assert broker.effective_entry_price() == 0

    def test_effective_entry_price_ignores_zero_real(self, broker):
        """0 is the sentinel for 'not confirmed' — always fall back."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 0  # explicitly zero
        assert broker.effective_entry_price() == 20000


class TestRealEntryPricePersistence:
    """to_dict/from_dict round-trip + backward compat for issue #45."""

    def test_to_dict_includes_real_entry_fields(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 20003
        broker.real_entry_dt = "2026-04-09T00:18:33"

        data = broker.to_dict()

        assert data["real_entry_price"] == 20003
        assert data["real_entry_dt"] == "2026-04-09T00:18:33"

    def test_to_dict_includes_per_trade_real_entry(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 20003
        broker.real_entry_dt = "2026-04-09T00:18:33"
        broker.force_close(1, 20050)

        data = broker.to_dict()

        assert len(data["trades"]) == 1
        assert data["trades"][0]["real_entry_price"] == 20003
        assert data["trades"][0]["real_entry_dt"] == "2026-04-09T00:18:33"

    def test_from_dict_roundtrip(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 20003
        broker.real_entry_dt = "2026-04-09T00:18:33"

        data = broker.to_dict()
        restored = SimulatedBroker.from_dict(data)

        assert restored.real_entry_price == 20003
        assert restored.real_entry_dt == "2026-04-09T00:18:33"

    def test_from_dict_roundtrip_preserves_trade_real_entry(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 20003
        broker.real_entry_dt = "2026-04-09T00:18:33"
        broker.force_close(1, 20050)

        data = broker.to_dict()
        restored = SimulatedBroker.from_dict(data)

        assert restored.trades[0].real_entry_price == 20003
        assert restored.trades[0].real_entry_dt == "2026-04-09T00:18:33"

    def test_from_dict_backward_compat_missing_keys(self):
        """Old session files without real_entry_* keys must still load."""
        old_data = {
            "point_value": 200,
            "position_size": 0,
            "position_side": None,
            "entry_price": 0,
            "entry_tag": "",
            "entry_bar_index": 0,
            "trades": [
                {
                    "tag": "Long", "side": "LONG", "qty": 1,
                    "entry_price": 20000, "exit_price": 20050,
                    "entry_bar_index": 0, "exit_bar_index": 1,
                    "pnl": 10000, "exit_tag": "force_close",
                    # No real_entry_price, real_entry_dt, etc.
                },
            ],
            "equity_curve": [10000],
            "_cumulative_pnl": 10000,
            "_bar_index": 1,
            "_exit_bar_index": 1,
        }

        restored = SimulatedBroker.from_dict(old_data)

        assert restored.real_entry_price == 0
        assert restored.real_entry_dt == ""
        assert restored.trades[0].real_entry_price == 0
        assert restored.trades[0].real_entry_dt == ""
        assert restored.trades[0].entry_price == 20000  # sim still works


class TestTrySetRealEntryPriceRaceGuard:
    """Regression tests for the race guard in issue #45.

    ``try_set_real_entry_price`` must reject late/stale fill
    confirmations to prevent a previous trade's real fill price from
    leaking onto the next trade's broker state.
    """

    def test_accepts_valid_write(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(5, 20000)
        assert broker.entry_bar_index == 5

        ok = broker.try_set_real_entry_price(
            20003, entry_bar_index=5, fill_dt="2026-04-09T00:18:33")

        assert ok is True
        assert broker.real_entry_price == 20003
        assert broker.real_entry_dt == "2026-04-09T00:18:33"

    def test_rejects_when_flat(self, broker):
        """Late callback after sim position already closed → no write."""
        assert broker.position_size == 0
        ok = broker.try_set_real_entry_price(20003, entry_bar_index=5)
        assert ok is False
        assert broker.real_entry_price == 0

    def test_rejects_when_already_set(self, broker):
        """Second confirmation (double-fire) must not overwrite."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(5, 20000)
        broker.try_set_real_entry_price(20003, entry_bar_index=5)

        ok = broker.try_set_real_entry_price(99999, entry_bar_index=5)

        assert ok is False
        assert broker.real_entry_price == 20003  # unchanged

    def test_rejects_mismatched_entry_bar_index(self, broker):
        """Late callback from previous trade (bar_index mismatch) → dropped."""
        # Previous trade had bar_index=3, closed, new trade at bar_index=7
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(7, 20000)
        assert broker.entry_bar_index == 7

        # Late callback from the previous trade (bar_index=3)
        ok = broker.try_set_real_entry_price(
            99999, entry_bar_index=3, fill_dt="stale")

        assert ok is False
        assert broker.real_entry_price == 0  # stale write dropped
        assert broker.real_entry_dt == ""

    def test_rejects_zero_price(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(5, 20000)

        ok = broker.try_set_real_entry_price(0, entry_bar_index=5)

        assert ok is False
        assert broker.real_entry_price == 0

    def test_rejects_negative_price(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(5, 20000)

        ok = broker.try_set_real_entry_price(-100, entry_bar_index=5)

        assert ok is False
        assert broker.real_entry_price == 0

    def test_late_callback_after_close_and_reopen_does_not_leak(self, broker):
        """Critical race: previous trade's late fill must not land on new trade.

        This is the scenario that issue #45's race guard protects against:
          1. Trade A entry sent (bar_index=5), fill_pending, confirmation not yet back
          2. Trade A simulated stop fires, position closes
          3. Trade B enters (bar_index=8)
          4. Trade A's late entry-fill confirmation arrives with bar_index=5
             → MUST NOT write onto Trade B's broker state
          5. Trade B's own confirmation arrives with bar_index=8
             → SHOULD write normally
        """
        # --- Trade A ---
        broker.queue_entry(Order(tag="LongA", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(5, 20000)
        # Trade A's fill hasn't confirmed yet — real_entry_price still 0
        broker.queue_exit(Order(
            tag="Stop", side=OrderSide.LONG, from_entry="LongA", stop=19900))
        # Simulated stop fires before Trade A confirmation arrives
        broker.check_exits(6, open_=19950, high=19960, low=19850, close=19870)
        assert broker.position_size == 0
        assert len(broker.trades) == 1
        assert broker.trades[0].real_entry_price == 0  # never confirmed

        # --- Trade B enters ---
        broker.queue_entry(Order(tag="LongB", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(8, 19900)
        assert broker.position_size == 1
        assert broker.entry_bar_index == 8

        # --- Late fill confirmation for Trade A arrives ---
        ok = broker.try_set_real_entry_price(
            20003,              # would-have-been Trade A's real fill
            entry_bar_index=5,  # Trade A's bar_index (now stale)
            fill_dt="late",
        )
        assert ok is False
        # Trade B's broker state is untouched
        assert broker.real_entry_price == 0
        assert broker.real_entry_dt == ""

        # --- Trade B's own confirmation arrives and writes normally ---
        ok = broker.try_set_real_entry_price(
            19905, entry_bar_index=8, fill_dt="2026-04-09T00:19:00")
        assert ok is True
        assert broker.real_entry_price == 19905

        # Close Trade B; its Trade should carry the correct real fill
        broker.force_close(9, 19950)
        assert broker.trades[1].real_entry_price == 19905
        assert broker.trades[0].real_entry_price == 0


class TestTrySetRealExitPriceRaceGuard:
    """Phase 2: real exit price tracking, mirror of issue #45 entry guard.

    ``try_set_real_exit_price`` updates the most recent trade's
    ``real_exit_price``/``real_exit_dt`` after the simulated exit has
    already created the Trade record.  Race guards reject stale or
    invalid writes so a late callback for a previous trade cannot
    leak onto the current one.
    """

    def _open_and_close(self, broker, entry_bar=5, exit_bar=6,
                        entry_price=20000, exit_price=20100):
        """Helper: open a long position and close it via stop exit."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(entry_bar, entry_price)
        broker.queue_exit(Order(
            tag="TP", side=OrderSide.LONG, from_entry="Long", limit=exit_price))
        broker.check_exits(exit_bar, open_=entry_price + 50,
                           high=exit_price + 10, low=entry_price - 10,
                           close=exit_price)

    def test_accepts_valid_write(self, broker):
        self._open_and_close(broker, entry_bar=5, exit_bar=6)
        assert broker.trades[-1].exit_bar_index == 6

        ok = broker.try_set_real_exit_price(
            20098, exit_bar_index=6, fill_dt="2026-04-14T11:57:01")

        assert ok is True
        assert broker.trades[-1].real_exit_price == 20098
        assert broker.trades[-1].real_exit_dt == "2026-04-14T11:57:01"

    def test_rejects_when_no_trades(self, broker):
        """Late callback before any trade exists → no write."""
        assert not broker.trades
        ok = broker.try_set_real_exit_price(20003, exit_bar_index=6)
        assert ok is False

    def test_rejects_when_already_set(self, broker):
        """Second confirmation (double-fire) must not overwrite."""
        self._open_and_close(broker, entry_bar=5, exit_bar=6)
        broker.try_set_real_exit_price(20098, exit_bar_index=6, fill_dt="first")

        ok = broker.try_set_real_exit_price(99999, exit_bar_index=6, fill_dt="second")

        assert ok is False
        assert broker.trades[-1].real_exit_price == 20098
        assert broker.trades[-1].real_exit_dt == "first"

    def test_rejects_mismatched_exit_bar_index(self, broker):
        """Late callback for previous trade (exit_bar_index mismatch) → dropped."""
        self._open_and_close(broker, entry_bar=5, exit_bar=6)
        # Open + close a second trade
        self._open_and_close(broker, entry_bar=8, exit_bar=10,
                             entry_price=21000, exit_price=21100)
        assert broker.trades[-1].exit_bar_index == 10

        # Late callback from the first trade (exit_bar_index=6)
        ok = broker.try_set_real_exit_price(
            99999, exit_bar_index=6, fill_dt="stale")

        assert ok is False
        assert broker.trades[-1].real_exit_price == 0  # stale write dropped
        assert broker.trades[-1].real_exit_dt == ""
        # First trade's record is also untouched (we only update last)
        assert broker.trades[0].real_exit_price == 0

    def test_rejects_zero_or_negative_price(self, broker):
        self._open_and_close(broker, entry_bar=5, exit_bar=6)

        assert broker.try_set_real_exit_price(0, exit_bar_index=6) is False
        assert broker.try_set_real_exit_price(-100, exit_bar_index=6) is False
        assert broker.trades[-1].real_exit_price == 0

    def test_does_not_overwrite_via_force_close(self, broker):
        """Force close sets up the trade; later real fill should write."""
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(5, 20000)
        broker.force_close(7, 20100)
        assert len(broker.trades) == 1
        assert broker.trades[0].exit_bar_index == 7

        ok = broker.try_set_real_exit_price(
            20105, exit_bar_index=7, fill_dt="2026-04-14T11:58:00")

        assert ok is True
        assert broker.trades[0].real_exit_price == 20105


class TestBrokerContextRealEntryAPI:
    """BrokerContext exposes entry_price, real_entry_price, effective_entry_price."""

    def test_entry_price_property(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        ctx = broker.context

        assert ctx.entry_price == 20000

    def test_real_entry_price_property(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 20003
        ctx = broker.context

        assert ctx.real_entry_price == 20003

    def test_effective_entry_price_method(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 20003
        ctx = broker.context

        assert ctx.effective_entry_price() == 20003

    def test_effective_entry_price_fallback_via_context(self, broker):
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        ctx = broker.context

        assert ctx.effective_entry_price() == 20000  # sim fallback

    def test_entry_price_read_only(self, broker):
        """Properties are read-only — writing should raise AttributeError."""
        ctx = broker.context
        with pytest.raises(AttributeError):
            ctx.entry_price = 99999
