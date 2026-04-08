"""Tests for FillPoller — fill confirmation via position change monitoring.

Verifies start conditions (exit-already-flat, no-COM), position update
detection (target-state checking), timeout handling with downgrade,
and TradingGuard state transitions.
"""

from __future__ import annotations

import time

import pytest

from src.live.trading_guard import TradingGuard
from src.live.fill_poller import FillPoller, FillPollAction


# ── Helpers ──

def _make_poller(timeout: float = 10.0) -> tuple[FillPoller, TradingGuard]:
    guard = TradingGuard()
    poller = FillPoller(guard, timeout=timeout)
    return poller, guard


# ── FillPoller.start() ──

class TestStart:

    def test_normal_entry(self):
        fp, g = _make_poller()
        action = fp.start("entry", 0)
        assert action.type == "start_polling"
        assert action.delay_ms == 2000
        assert action.action_type == "entry"
        assert fp.active
        assert fp.pos_before == 0

    def test_normal_exit(self):
        fp, g = _make_poller()
        action = fp.start("exit", 1)
        assert action.type == "start_polling"
        assert action.action_type == "exit"
        assert fp.pos_before == 1

    def test_exit_already_flat(self):
        """IOC exit filled before we could read position → confirm immediately."""
        fp, g = _make_poller()
        action = fp.start("exit", 0)
        assert action.type == "already_confirmed"
        assert action.action_type == "exit"

    def test_no_com(self):
        """COM unavailable → auto-confirm and update guard."""
        fp, g = _make_poller()
        g.on_fill_pending("entry")  # simulate pending state
        action = fp.start("entry", 0, com_available=False)
        assert action.type == "no_com"
        # Guard should be updated
        assert not g.fill_pending
        assert g.real_entry_confirmed  # entry sent

    def test_no_com_exit(self):
        fp, g = _make_poller()
        g.on_entry_sent()  # we had a real entry
        g.on_fill_pending("exit")
        action = fp.start("exit", 1, com_available=False)
        assert action.type == "no_com"
        assert not g.real_entry_confirmed  # exit sent


# ── FillPoller.on_position_update() ──

class TestPositionUpdate:

    def test_entry_confirmed(self):
        """Entry fill: position goes from 0 to non-zero."""
        fp, g = _make_poller()
        fp.start("entry", 0)
        result = fp.on_position_update(1)
        assert result is not None
        assert result.type == "confirmed"
        assert result.action_type == "entry"

    def test_entry_not_yet(self):
        """Entry fill: position still 0 → not confirmed."""
        fp, g = _make_poller()
        fp.start("entry", 0)
        result = fp.on_position_update(0)
        assert result is None

    def test_exit_confirmed(self):
        """Exit fill: position goes to 0 (flat)."""
        fp, g = _make_poller()
        fp.start("exit", 1)
        result = fp.on_position_update(0)
        assert result is not None
        assert result.type == "confirmed"

    def test_exit_not_yet(self):
        """Exit fill: still has position → not confirmed."""
        fp, g = _make_poller()
        fp.start("exit", 1)
        result = fp.on_position_update(1)
        assert result is None

    def test_short_entry_confirmed(self):
        """Short entry: position goes from 0 to negative."""
        fp, g = _make_poller()
        fp.start("entry", 0)
        result = fp.on_position_update(-1)
        assert result is not None
        assert result.type == "confirmed"

    def test_not_active(self):
        """No active polling → always returns None."""
        fp, g = _make_poller()
        result = fp.on_position_update(1)
        assert result is None

    def test_pos_current_updated(self):
        fp, g = _make_poller()
        fp.start("entry", 0)
        fp.on_position_update(2)
        assert fp.pos_current == 2


# ── FillPoller.check_poll() ──

class TestCheckPoll:

    def test_not_timed_out(self):
        fp, g = _make_poller(timeout=10.0)
        fp.start("entry", 0)
        # Check immediately (well before timeout)
        action = fp.check_poll(now=fp._start_time + 1.0)
        assert action.type == "poll_again"
        assert action.delay_ms == 3000

    def test_timed_out(self):
        fp, g = _make_poller(timeout=10.0)
        fp.start("entry", 0)
        action = fp.check_poll(now=fp._start_time + 11.0)
        assert action.type == "timeout"
        assert action.action_type == "entry"

    def test_exactly_at_timeout(self):
        fp, g = _make_poller(timeout=10.0)
        fp.start("entry", 0)
        action = fp.check_poll(now=fp._start_time + 10.0)
        assert action.type == "timeout"


# ── FillPoller.confirm() ──

class TestConfirm:

    def test_entry_confirm_updates_guard(self):
        fp, g = _make_poller()
        g.on_fill_pending("entry")
        fp.start("entry", 0)
        fp.on_position_update(1)
        result = fp.confirm()
        assert result.action_type == "entry"
        assert not g.fill_pending
        assert g.real_entry_confirmed
        assert not fp.active

    def test_exit_confirm_updates_guard(self):
        fp, g = _make_poller()
        g.on_entry_sent()
        g.on_fill_pending("exit")
        fp.start("exit", 1)
        fp.on_position_update(0)
        result = fp.confirm()
        assert result.action_type == "exit"
        assert not g.fill_pending
        assert not g.real_entry_confirmed  # exit clears it
        assert not fp.active

    def test_already_confirmed_exit(self):
        """Exit-already-flat path: start returns already_confirmed, then confirm()."""
        fp, g = _make_poller()
        g.on_entry_sent()
        g.on_fill_pending("exit")
        action = fp.start("exit", 0)
        assert action.type == "already_confirmed"
        result = fp.confirm()
        assert not g.fill_pending
        assert not g.real_entry_confirmed


# ── FillPoller.timeout() ──

class TestTimeout:

    def test_entry_timeout_assumes_filled(self):
        """Entry timeout: conservative — assume position exists, allow exits."""
        fp, g = _make_poller(timeout=10.0)
        g.on_fill_pending("entry")
        fp.start("entry", 0)
        result = fp.timeout()
        assert result.action_type == "entry"
        assert result.new_trading_mode == "semi_auto"
        assert result.timeout_seconds == 10.0
        assert not g.fill_pending
        assert g.real_entry_confirmed  # assumed filled
        assert not fp.active

    def test_exit_timeout_assumes_closed(self):
        """Exit timeout: conservative — assume we closed, prevent double exits."""
        fp, g = _make_poller(timeout=10.0)
        g.on_entry_sent()
        g.on_fill_pending("exit")
        fp.start("exit", 1)
        result = fp.timeout()
        assert result.action_type == "exit"
        assert not g.real_entry_confirmed  # assumed closed
        assert not fp.active

    def test_timeout_message_bilingual(self):
        fp, g = _make_poller()
        g.on_fill_pending("entry")
        fp.start("entry", 0)
        result = fp.timeout()
        assert "成交超時" in result.message
        assert "Fill timeout" in result.message
        assert "semi-auto" in result.message


# ── FillPoller.reset() ──

class TestReset:

    def test_clears_all_state(self):
        fp, g = _make_poller()
        fp.start("entry", 0)
        fp.on_position_update(1)
        fp.reset()
        assert not fp.active
        assert fp.action_type == ""
        assert fp.pos_before == 0
        assert fp.pos_current is None


# ── Integration: full flow ──

class TestFullFlow:

    def test_entry_flow(self):
        """Start → position update → confirm → guard in correct state."""
        fp, g = _make_poller()
        g.on_fill_pending("entry")

        action = fp.start("entry", 0)
        assert action.type == "start_polling"
        assert g.fill_pending

        # Simulate OI callback showing position appeared
        result = fp.on_position_update(1)
        assert result.type == "confirmed"

        # Finalize
        confirm = fp.confirm()
        assert confirm.action_type == "entry"
        assert not g.fill_pending
        assert g.real_entry_confirmed

    def test_exit_flow(self):
        """Start → position update → confirm → guard in correct state."""
        fp, g = _make_poller()
        g.on_entry_sent()
        g.on_fill_pending("exit")

        action = fp.start("exit", 1)
        assert action.type == "start_polling"

        result = fp.on_position_update(0)
        assert result.type == "confirmed"

        confirm = fp.confirm()
        assert not g.real_entry_confirmed

    def test_timeout_flow(self):
        """Start → poll → timeout → downgrade."""
        fp, g = _make_poller(timeout=5.0)
        g.on_fill_pending("entry")

        action = fp.start("entry", 0)
        assert action.type == "start_polling"

        # Simulate several polls with no position change
        fp.on_position_update(0)  # still flat
        poll_action = fp.check_poll(now=fp._start_time + 2.0)
        assert poll_action.type == "poll_again"

        fp.on_position_update(0)  # still flat
        poll_action = fp.check_poll(now=fp._start_time + 6.0)
        assert poll_action.type == "timeout"

        result = fp.timeout()
        assert result.new_trading_mode == "semi_auto"
        assert g.real_entry_confirmed  # assumed filled


# ── Regression: issue #43 — stale guard reference ──

class TestGuardIdentityIssue43:
    """Regression tests for issue #43.

    run_backtest.py used to rebind self._trading_guard on every Deploy
    (``self._trading_guard = TradingGuard(daily_loss_limit=...)``) while
    self._fill_poller was initialized once in __init__ with a reference
    to the original guard instance. After Deploy, the two diverged:
    fill confirmation cleared fill_pending on the OLD guard, but the
    order-decision path checked the NEW guard, which stayed stuck with
    fill_pending=True forever — blocking exit orders when the stop fired.

    Fix: reconfigure daily_loss_limit in place and call reset() on the
    existing guard so all holders of the reference stay in sync.
    """

    def test_rebinding_guard_leaves_fill_poller_stale(self):
        """Demonstrates the bug: rebinding guard breaks fill_poller sync.

        This is the anti-pattern — FillPoller.confirm() updates the OLD
        guard, but a caller checking a NEW guard sees stale state.
        """
        old_guard = TradingGuard(daily_loss_limit=1000)
        fp = FillPoller(old_guard)

        # Buggy deploy pattern: create a new guard and rebind the "main"
        # reference, while FillPoller still holds the old one.
        new_guard = TradingGuard(daily_loss_limit=10000)

        # Auto-mode entry flow on the NEW guard (what run_backtest.py uses)
        new_guard.on_fill_pending("entry")
        fp.start("entry", 0, com_available=True)

        # OI callback reports fill → FillPoller.confirm() runs
        result = fp.on_position_update(1)
        assert result is not None and result.type == "confirmed"
        fp.confirm()

        # Old guard is cleared, but the new guard (used by decide())
        # remains stuck — this reproduces issue #43.
        assert old_guard.fill_pending is False
        assert new_guard.fill_pending is True
        verdict, _ = new_guard.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == new_guard.BLOCK_FILL_PENDING, (
            "Expected BLOCK_FILL_PENDING on stale-reference anti-pattern")

    def test_in_place_reconfigure_keeps_fill_poller_in_sync(self):
        """The fix: update daily_loss_limit in place, keep guard identity.

        Same scenario as the previous test but using the in-place pattern.
        After fill confirmation, decide() must allow the exit to flow.
        """
        guard = TradingGuard(daily_loss_limit=1000)
        fp = FillPoller(guard)
        original_id = id(guard)

        # Correct deploy pattern: mutate in place, do not rebind.
        guard.daily_loss_limit = 10000
        guard.reset()
        fp.reset()

        assert id(guard) == original_id
        assert guard.daily_loss_limit == 10000

        # Auto-mode entry flow
        guard.on_fill_pending("entry")
        fp.start("entry", 0, com_available=True)

        # OI callback reports fill
        result = fp.on_position_update(1)
        assert result is not None and result.type == "confirmed"
        fp.confirm()

        # Same guard must be fully cleared
        assert guard.fill_pending is False
        assert guard.fill_pending_type == ""
        assert guard.real_entry_confirmed is True

        # Subsequent close signal must flow through (not blocked)
        verdict, _ = guard.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == guard.SEND_EXIT
