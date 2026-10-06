"""Acceptance tests for the issue #157 reconnect guard.

The 2026-10-06 NIGHT incident: attempt #1 reached Quote (3002) but never
Ready (3003), ``IsConnected()==2``, and attempt #2 called
``SKQuoteLib_LeaveMonitor`` on that half-up session. The process AVed in
ntdll or hung on the Tk thread. A half-up handshake (IsConnected()==2, no 3003) keeps skipping until
open+90s, including after the market has opened. One forced teardown
is then allowed per disconnect.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.live.reconnect_controller import (
    ACTION_ALREADY_CONNECTED,
    ACTION_SKIP_TEARDOWN_WAIT,
    ACTION_TEARDOWN_AND_LOGIN,
    FORCED_TEARDOWN_LOG,
    IS_CONNECTED,
    IS_CONNECTING,
    ReconnectController,
    arm_single_timer,
    seconds_since_session_open,
)

# Before the open. The skip must not depend on this being negative.
_BEFORE_OPEN = -120
_OPEN_PLUS_15 = 15
_OPEN_PLUS_91 = 91
_TPE = timezone(timedelta(hours=8))


def _incident_controller() -> ReconnectController:
    """Attempt #1 tore down a down session, then timed out without Ready."""
    ctrl = ReconnectController()
    ctrl.on_clean_disconnect()
    first = ctrl.decide(0, seconds_since_open=_BEFORE_OPEN)
    assert first.action == ACTION_TEARDOWN_AND_LOGIN
    ctrl.on_attempt_started()
    ctrl.on_attempt_failed()
    return ctrl


class TestIssue157PreSessionGuard:
    """Acceptance (1): do not LeaveMonitor a connecting pre-session handshake."""

    def test_attempt_two_skips_teardown_while_market_closed(self):
        ctrl = _incident_controller()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=_BEFORE_OPEN)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
        assert decision.wait_seconds == ReconnectController.CONNECTING_WAIT_S
        assert "LeaveMonitor" in decision.reason
        assert ctrl.previous_attempt_missed_ready

    def test_skip_holds_across_repeated_polls(self):
        """A 15s poll must not clear the latch and fall through to teardown."""
        ctrl = _incident_controller()
        for _ in range(4):
            decision = ctrl.decide(IS_CONNECTING, seconds_since_open=_BEFORE_OPEN)
            assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
            ctrl.on_attempt_failed()

    def test_probe_failure_is_treated_as_unsafe_before_the_open(self):
        ctrl = _incident_controller()
        decision = ctrl.decide(None, seconds_since_open=_BEFORE_OPEN)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT

    def test_down_session_still_tears_down_before_the_open(self):
        """IsConnected()==0 is not the half-up state. Bug B cleanup stays."""
        ctrl = _incident_controller()
        decision = ctrl.decide(0, seconds_since_open=_BEFORE_OPEN)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN

    def test_first_attempt_after_clean_disconnect_still_tears_down(self):
        """Attempt #1 LeaveMonitor returned 9999 in the incident. The
        guard must not swallow that call — only the follow-up while the
        handshake is still connecting."""
        ctrl = ReconnectController()
        ctrl.on_clean_disconnect()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=_OPEN_PLUS_15)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN

    def test_stray_failure_does_not_arm_the_guard(self):
        ctrl = ReconnectController()
        ctrl.on_attempt_failed()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=_BEFORE_OPEN)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN
        assert not ctrl.previous_attempt_missed_ready


class TestIssue157HalfUpHold:
    """Half-up skip lasts until open+90s, then one forced teardown."""

    def test_half_up_at_open_plus_15_skips(self):
        """The market is open here. The clock must not lift the skip."""
        ctrl = _incident_controller()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=_OPEN_PLUS_15)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not decision.forced_teardown

    def test_half_up_at_open_plus_91_tears_down_once(self):
        ctrl = _incident_controller()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=_OPEN_PLUS_91)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN
        assert decision.forced_teardown
        assert decision.reason == FORCED_TEARDOWN_LOG
        ctrl.note_forced_teardown()
        assert ctrl.forced_teardown_used

    def test_second_forced_teardown_same_disconnect_is_refused(self):
        ctrl = _incident_controller()
        first = ctrl.decide(IS_CONNECTING, seconds_since_open=_OPEN_PLUS_91)
        assert first.forced_teardown
        ctrl.note_forced_teardown()
        second = ctrl.decide(IS_CONNECTING, seconds_since_open=_OPEN_PLUS_91)
        assert second.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not second.forced_teardown

    def test_new_disconnect_allows_another_forced_teardown(self):
        ctrl = _incident_controller()
        ctrl.note_forced_teardown()
        ctrl.on_clean_disconnect()
        ctrl.on_attempt_started()
        ctrl.on_attempt_failed()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=_OPEN_PLUS_91)
        assert decision.forced_teardown
        assert decision.reason == FORCED_TEARDOWN_LOG

    def test_already_connected_never_tears_down(self):
        ctrl = _incident_controller()
        for seconds_since_open in (_BEFORE_OPEN, _OPEN_PLUS_15, _OPEN_PLUS_91):
            decision = ctrl.decide(
                IS_CONNECTED, seconds_since_open=seconds_since_open)
            assert decision.action == ACTION_ALREADY_CONNECTED

    def test_ready_clears_the_latch(self):
        ctrl = _incident_controller()
        assert ctrl.decide(
            IS_CONNECTING, seconds_since_open=_OPEN_PLUS_15).action == (
            ACTION_SKIP_TEARDOWN_WAIT
        )
        ctrl.on_ready()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=_OPEN_PLUS_15)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN
        assert not decision.forced_teardown
        assert not ctrl.previous_attempt_missed_ready

    def test_clean_disconnect_clears_the_latch(self):
        ctrl = _incident_controller()
        ctrl.on_clean_disconnect()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=_OPEN_PLUS_15)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN
        assert not decision.forced_teardown

    def test_in_flight_handshake_is_half_up_before_the_failure_latch(self):
        """The Ready poll can run while the handshake is still open."""
        ctrl = ReconnectController()
        ctrl.on_clean_disconnect()
        ctrl.on_attempt_started()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=_OPEN_PLUS_15)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
        assert ctrl.handshake_open


class TestIssue157ReadyWait:
    """Acceptance (3): pre-session Ready wait covers the open; intra-session stays 3s."""

    def test_pre_session_wait_covers_until_open_plus_grace(self):
        ctrl = ReconnectController()
        # Deferred reconnect fires ~120s before the open (secs_until_open - 120).
        wait = ctrl.ready_wait_seconds(market_open=False, secs_until_open=120)
        assert wait >= 120 + ReconnectController.PRE_SESSION_GRACE_S
        assert wait <= ReconnectController.PRE_SESSION_WAIT_CAP_S

    def test_pre_session_wait_is_capped(self):
        ctrl = ReconnectController()
        wait = ctrl.ready_wait_seconds(market_open=False, secs_until_open=10_000)
        assert wait == ReconnectController.PRE_SESSION_WAIT_CAP_S

    def test_intra_session_wait_stays_three_seconds(self):
        ctrl = ReconnectController()
        assert ctrl.ready_wait_seconds(market_open=True, secs_until_open=0) == (
            ReconnectController.READY_WAIT_S
        )
        assert ctrl.ready_wait_seconds(market_open=False, secs_until_open=0) == (
            ReconnectController.READY_WAIT_S
        )

    def test_short_gap_still_waits_past_the_open(self):
        ctrl = ReconnectController()
        wait = ctrl.ready_wait_seconds(market_open=False, secs_until_open=30)
        assert wait == 30 + ReconnectController.PRE_SESSION_GRACE_S


class TestReset:
    def test_reset_clears_latch(self):
        ctrl = _incident_controller()
        ctrl.note_forced_teardown()
        ctrl.reset()
        assert not ctrl.previous_attempt_missed_ready
        assert not ctrl.handshake_open
        assert not ctrl.forced_teardown_used
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=_BEFORE_OPEN)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN


class TestSessionOpenClock:
    """open+15s and open+91s are what the guard compares against."""

    def test_open_plus_15_and_91_on_a_weekday_morning(self):
        # 2026-10-06 is a Tuesday, not a weekend.
        open_15 = datetime(2026, 10, 6, 8, 45, 15, tzinfo=_TPE)
        open_91 = datetime(2026, 10, 6, 8, 46, 31, tzinfo=_TPE)
        before = datetime(2026, 10, 6, 8, 43, 0, tzinfo=_TPE)
        assert seconds_since_session_open(open_15) == 15
        assert seconds_since_session_open(open_91) == 91
        assert seconds_since_session_open(before) == -120

    def test_night_open_and_weekend_gap(self):
        night = datetime(2026, 10, 6, 15, 1, 30, tzinfo=_TPE)
        assert seconds_since_session_open(night) == 90
        # Saturday 10:00 is closed until Monday 08:45.
        saturday = datetime(2026, 10, 10, 10, 0, 0, tzinfo=_TPE)
        assert seconds_since_session_open(saturday) < 0


class TestSingleReconnectTimer:
    def test_repeated_arm_leaves_exactly_one_pending_timer(self):
        """``_on_disconnected`` schedules through ``arm_single_timer``.

        Two disconnects must not leave two ``root.after`` callbacks.
        """
        pending: dict[int, object] = {}
        seq = {"n": 0}

        def after(delay_ms, callback):
            seq["n"] += 1
            token = seq["n"]
            pending[token] = (delay_ms, callback)
            return token

        def after_cancel(token):
            pending.pop(token, None)

        current = None
        current = arm_single_timer(
            current, 28_000, lambda: None, after=after, after_cancel=after_cancel)
        current = arm_single_timer(
            current, 28_000, lambda: None, after=after, after_cancel=after_cancel)
        assert list(pending) == [current]
        assert len(pending) == 1
