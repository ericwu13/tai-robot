"""Call-site tests for issue #62: the semi-auto/auto order handler must
surface every risk-gate block as a SIGNAL_BLOCKED decision.

The unit tests in test_live_runner.py exercise ``log_blocked_signal`` in
isolation — they pass even if every call site were deleted. These tests drive
``BacktestApp._handle_semi_auto_order`` with a lightweight fake ``self`` (no
Tkinter / COM) and assert the handler actually invokes ``log_blocked_signal``
with the correct reason code for each block branch.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import run_backtest as rb
from src.live.trading_guard import TradingGuard


def _fake_app(guard, trading_mode="semi_auto"):
    """Minimal stand-in for BacktestApp with just the attributes the
    _handle_semi_auto_order block branches touch."""
    live_runner = SimpleNamespace(
        symbol="TX00",
        log_blocked_signal=MagicMock(),
    )
    return SimpleNamespace(
        _live_runner=live_runner,
        _trading_guard=guard,
        _trading_mode=trading_mode,
        _live_log_msg=lambda *a, **k: None,
        _log_order_decision=lambda *a, **k: None,
        _is_settlement_no_entry_window=lambda *a, **k: False,
        _SETTLEMENT_NO_ENTRY_MINUTES=60,
    )


def test_daily_loss_limit_block_logs_signal():
    """BLOCK_ENTRY (daily loss limit) must emit a DAILY_LOSS_LIMIT signal."""
    guard = TradingGuard(daily_loss_limit=10000)
    guard.paused = True  # daily loss limit already tripped
    app = _fake_app(guard)

    decision = {"action": "ENTRY_FILL", "side": "LONG", "price": 20000}
    rb.BacktestApp._handle_semi_auto_order(app, decision)

    app._live_runner.log_blocked_signal.assert_called_once()
    args = app._live_runner.log_blocked_signal.call_args.args
    assert args[0] == "ENTRY_FILL"       # action
    assert args[1] == "LONG"             # side
    assert args[2] == 20000              # price
    assert args[3] == "DAILY_LOSS_LIMIT"  # reason code


def test_skip_exit_block_logs_no_real_position():
    """SKIP_EXIT (no confirmed real entry) must emit NO_REAL_POSITION."""
    guard = TradingGuard(daily_loss_limit=10000)
    # real_entry_confirmed stays False → any exit is skipped.
    app = _fake_app(guard)

    decision = {"action": "TRADE_CLOSE", "side": "LONG", "price": 20100}
    rb.BacktestApp._handle_semi_auto_order(app, decision)

    app._live_runner.log_blocked_signal.assert_called_once()
    args = app._live_runner.log_blocked_signal.call_args.args
    assert args[0] == "TRADE_CLOSE"
    assert args[3] == "NO_REAL_POSITION"


def test_fill_pending_block_logs_signal():
    """BLOCK_FILL_PENDING must emit a FILL_PENDING signal."""
    guard = TradingGuard(daily_loss_limit=10000)
    guard.fill_pending = True
    guard.fill_pending_type = "entry"
    app = _fake_app(guard, trading_mode="auto")

    decision = {"action": "ENTRY_FILL", "side": "SHORT", "price": 20200}
    rb.BacktestApp._handle_semi_auto_order(app, decision)

    app._live_runner.log_blocked_signal.assert_called_once()
    args = app._live_runner.log_blocked_signal.call_args.args
    assert args[3] == "FILL_PENDING"


def test_allowed_entry_does_not_log_blocked_signal():
    """A clean entry (auto mode, no blocks) must NOT emit a blocked signal."""
    guard = TradingGuard(daily_loss_limit=10000)
    app = _fake_app(guard, trading_mode="auto")
    # SEND_ENTRY path calls _send_real_order — stub it out.
    app._send_real_order = MagicMock(return_value=True)

    decision = {"action": "ENTRY_FILL", "side": "LONG", "price": 20000}
    rb.BacktestApp._handle_semi_auto_order(app, decision)

    app._live_runner.log_blocked_signal.assert_not_called()
    app._send_real_order.assert_called_once()
