"""Issue #107: semi-auto confirm skip/timeout must unwind the sim entry.

CONFIRMED bug (v2.17.21 TMF00 DynamicExitPullbackStrategyV2):
  1. Strategy ENTRY_FILL fills SimulatedBroker LONG immediately.
  2. TradingGuard.decide() returns CONFIRM_ENTRY → 10s dialog.
  3. Timeout/skip logs REAL_ORDER_TIMEOUT / REAL_ORDER_SKIPPED but does
     NOT flatten the broker. session.json is saved with position_size=1,
     real_entry_price=0.
  4. Resume restores the ghost LONG. OI is flat so the guard is NOT
     reconciled, but the sim position is left in place — UI shows 持倉
     after reboot.

These tests are designed to FAIL against pre-fix code:
  * test_abandon_unconfirmed_entry_*          (method missing / no-op)
  * test_on_entry_skipped_unwinds_sim_position (on_entry_skipped is `pass`)
  * test_dismiss_timeout_unwinds_sim_entry     (dismiss only logs)
  * test_dismiss_skip_unwinds_sim_entry        (same)
  * test_abandoned_entry_session_roundtrip_is_flat
  * test_runner_abandon_persists_flat_session
  * test_resume_flat_oi_clears_unconfirmed_ghost
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.backtest.broker import Order, OrderSide, SimulatedBroker
from src.live.session_store import load_session
from src.live.trading_guard import TradingGuard

import run_backtest as rb


def _long_unconfirmed(price: int = 44774) -> SimulatedBroker:
    """SimulatedBroker after a semi-auto ENTRY_FILL, before real confirm."""
    broker = SimulatedBroker(point_value=50)
    broker.trade_source = "real"
    broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
    broker.on_bar_close(0, price, "2026-09-04 13:01:00")
    assert broker.position_size == 1
    assert broker.real_entry_price == 0
    return broker


# ── SimulatedBroker.abandon_unconfirmed_entry ──

class TestAbandonUnconfirmedEntry:
    """FAILS pre-fix: abandon_unconfirmed_entry does not exist."""

    def test_abandon_unconfirmed_entry_clears_position_without_trade(self):
        broker = _long_unconfirmed()
        abandoned = broker.abandon_unconfirmed_entry()

        assert abandoned is True
        assert broker.position_size == 0
        assert broker.position_side is None
        assert broker.entry_price == 0
        assert broker.entry_tag == ""
        assert broker.entry_bar_index == 0
        assert broker.real_entry_price == 0
        assert broker.trades == []  # not a paper close — user declined

    def test_abandon_is_noop_when_flat(self):
        broker = SimulatedBroker(point_value=50)
        assert broker.abandon_unconfirmed_entry() is False
        assert broker.position_size == 0

    def test_abandon_refuses_confirmed_real_fill(self):
        """A real OpenInterest fill must be closed via the normal exit path."""
        broker = _long_unconfirmed()
        broker.real_entry_price = 44780
        broker.real_entry_dt = "2026-09-04 13:01:05"

        assert broker.abandon_unconfirmed_entry() is False
        assert broker.position_size == 1
        assert broker.real_entry_price == 44780

    def test_abandon_clears_pending_exits(self):
        broker = _long_unconfirmed()
        broker.queue_exit(Order(
            tag="Exit", side=OrderSide.LONG, from_entry="Long", stop=44700,
        ))
        broker.abandon_unconfirmed_entry()
        assert broker._pending_exits == []
        assert broker.position_size == 0


# ── TradingGuard.on_entry_skipped ──

class TestOnEntrySkippedUnwindsBroker:
    """FAILS pre-fix: on_entry_skipped is `pass` and never touches the broker."""

    def test_on_entry_skipped_unwinds_sim_position(self):
        broker = _long_unconfirmed()
        g = TradingGuard()
        g.on_entry_skipped(broker)

        assert broker.position_size == 0
        assert g.real_entry_confirmed is False

    def test_on_entry_skipped_without_broker_still_blocks_exits(self):
        """Issue #17: skip still means no real-exit send, even with no broker."""
        g = TradingGuard()
        g.on_entry_skipped()
        verdict, _ = g.decide("semi_auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SKIP_EXIT


# ── run_backtest._dismiss_order_dialog ──

def _fake_dismiss_app(broker):
    """Minimal BacktestApp stand-in for _dismiss_order_dialog (no Tkinter)."""
    runner = SimpleNamespace(
        broker=broker,
        _auto_save_session=MagicMock(),
    )
    app = SimpleNamespace(
        _order_confirm_timer_id=None,
        _order_confirm_dlg=None,
        _live_runner=runner,
        _trading_guard=TradingGuard(),
        _live_log_msg=lambda *a, **k: None,
        _log_order_decision=lambda *a, **k: None,
    )
    app._unwind_skipped_sim_entry = (
        lambda: rb.BacktestApp._unwind_skipped_sim_entry(app))
    return app


class TestDismissOrderDialogUnwinds:
    """FAILS pre-fix: timeout/skip only log; sim LONG stays open."""

    def test_dismiss_timeout_unwinds_sim_entry(self):
        broker = _long_unconfirmed()
        app = _fake_dismiss_app(broker)

        rb.BacktestApp._dismiss_order_dialog(app, "timeout")

        assert broker.position_size == 0
        assert broker.position_side is None
        app._live_runner._auto_save_session.assert_called()

    def test_dismiss_skip_unwinds_sim_entry(self):
        broker = _long_unconfirmed()
        app = _fake_dismiss_app(broker)

        rb.BacktestApp._dismiss_order_dialog(app, "skipped")

        assert broker.position_size == 0
        app._live_runner._auto_save_session.assert_called()

    def test_dismiss_confirmed_does_not_unwind(self):
        broker = _long_unconfirmed()
        app = _fake_dismiss_app(broker)

        rb.BacktestApp._dismiss_order_dialog(app, "confirmed")

        assert broker.position_size == 1
        app._live_runner._auto_save_session.assert_not_called()

    def test_dismiss_replaced_does_not_unwind(self):
        """A replacement dialog is still about the same pending entry."""
        broker = _long_unconfirmed()
        app = _fake_dismiss_app(broker)

        rb.BacktestApp._dismiss_order_dialog(app, "replaced")

        assert broker.position_size == 1


# ── session.json must not restore the ghost ──

class TestGhostNotRestored:
    """FAILS pre-fix: skip leaves position_size=1 in to_dict/from_dict."""

    def test_abandoned_entry_session_roundtrip_is_flat(self):
        broker = _long_unconfirmed()
        broker.abandon_unconfirmed_entry()
        restored = SimulatedBroker.from_dict(broker.to_dict())
        assert restored.position_size == 0
        assert restored.position_side is None
        assert restored.real_entry_price == 0
        assert restored.trades == []

    def test_from_dict_still_restores_unconfirmed_open_if_not_abandoned(self):
        """from_dict itself must NOT drop paper / fill-pending positions.
        Issue #107 abandons at skip/resume time, then persists the flat state.
        """
        broker = _long_unconfirmed()
        restored = SimulatedBroker.from_dict(broker.to_dict())
        assert restored.position_size == 1
        assert restored.position_side == OrderSide.LONG
        assert restored.real_entry_price == 0


class TestLiveRunnerAbandonPersists:
    """FAILS pre-fix: LiveRunner.abandon_unconfirmed_entry does not exist."""

    def test_runner_abandon_persists_flat_session(self, tmp_path):
        from src.live.live_runner import LiveRunner
        from tests.test_live_runner import AlwaysLongStrategy

        runner = LiveRunner(
            AlwaysLongStrategy(), "TMF00", point_value=50,
            log_dir=str(tmp_path), bot_name="issue107",
        )
        runner.trading_mode = "semi_auto"
        runner.broker.trade_source = "real"
        runner.broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        runner.broker.on_bar_close(0, 44774, "2026-09-04 13:01:00")
        runner._auto_save_session()

        pre = load_session(runner._session_path)
        assert pre["broker"]["position_size"] == 1
        assert pre["broker"].get("real_entry_price", 0) == 0

        ok = runner.abandon_unconfirmed_entry()
        assert ok is True
        assert runner.broker.position_size == 0

        post = load_session(runner._session_path)
        assert post["broker"]["position_size"] == 0
        restored = SimulatedBroker.from_dict(post["broker"])
        assert restored.position_size == 0

    def test_resume_flat_oi_clears_unconfirmed_ghost(self, tmp_path):
        """Resume: restore_session loads the ghost, OI-flat must then abandon.

        FAILS pre-fix: restore_session leaves position_size=1 and there is
        no reconcile_unconfirmed_on_resume helper.
        """
        from src.live.live_runner import LiveRunner
        from tests.test_live_runner import AlwaysLongStrategy

        runner = LiveRunner(
            AlwaysLongStrategy(), "TMF00", point_value=50,
            log_dir=str(tmp_path), bot_name="issue107-resume",
        )
        runner.trading_mode = "semi_auto"
        ghost = _long_unconfirmed()
        n = runner.restore_session({"broker": ghost.to_dict(), "bar_index": 3})
        assert n == 0
        assert runner.broker.position_size == 1

        cleared = runner.reconcile_unconfirmed_on_resume("flat")
        assert cleared is True
        assert runner.broker.position_size == 0
        post = load_session(runner._session_path)
        assert post["broker"]["position_size"] == 0

    def test_resume_unknown_oi_does_not_clear(self, tmp_path):
        from src.live.live_runner import LiveRunner
        from tests.test_live_runner import AlwaysLongStrategy

        runner = LiveRunner(
            AlwaysLongStrategy(), "TMF00", point_value=50,
            log_dir=str(tmp_path), bot_name="issue107-unknown",
        )
        runner.restore_session({"broker": _long_unconfirmed().to_dict()})
        assert runner.reconcile_unconfirmed_on_resume("unknown") is False
        assert runner.broker.position_size == 1

    def test_resume_match_does_not_clear(self, tmp_path):
        from src.live.live_runner import LiveRunner
        from tests.test_live_runner import AlwaysLongStrategy

        runner = LiveRunner(
            AlwaysLongStrategy(), "TMF00", point_value=50,
            log_dir=str(tmp_path), bot_name="issue107-match",
        )
        runner.restore_session({"broker": _long_unconfirmed().to_dict()})
        assert runner.reconcile_unconfirmed_on_resume("match") is False
        assert runner.broker.position_size == 1
