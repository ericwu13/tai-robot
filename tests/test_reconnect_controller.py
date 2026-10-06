"""Acceptance tests for the issue #157 reconnect guard.

The 2026-10-06 NIGHT incident: attempt #1 reached Quote (3002) but never
Ready (3003), ``IsConnected()==2``, and attempt #2 called
``SKQuoteLib_LeaveMonitor`` on that half-up session. The process AVed in
ntdll or hung on the Tk thread. The same teardown succeeds once the
market is open, so these tests pin both sides.
"""

from __future__ import annotations

from src.live.reconnect_controller import (
    ACTION_ALREADY_CONNECTED,
    ACTION_SKIP_TEARDOWN_WAIT,
    ACTION_TEARDOWN_AND_LOGIN,
    IS_CONNECTED,
    IS_CONNECTING,
    ReconnectController,
)


def _incident_controller() -> ReconnectController:
    """Attempt #1 tore down a down session, then timed out without Ready."""
    ctrl = ReconnectController()
    ctrl.on_clean_disconnect()
    first = ctrl.decide(0, market_open=False)
    assert first.action == ACTION_TEARDOWN_AND_LOGIN
    ctrl.on_attempt_started()
    ctrl.on_attempt_failed()
    return ctrl


class TestIssue157PreSessionGuard:
    """Acceptance (1): do not LeaveMonitor a connecting pre-session handshake."""

    def test_attempt_two_skips_teardown_while_market_closed(self):
        ctrl = _incident_controller()
        decision = ctrl.decide(IS_CONNECTING, market_open=False)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
        assert decision.wait_seconds == ReconnectController.CONNECTING_WAIT_S
        assert "LeaveMonitor" in decision.reason
        assert ctrl.previous_attempt_missed_ready

    def test_skip_holds_across_repeated_polls(self):
        """A 15s poll must not clear the latch and fall through to teardown."""
        ctrl = _incident_controller()
        for _ in range(4):
            decision = ctrl.decide(IS_CONNECTING, market_open=False)
            assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
            ctrl.on_attempt_failed()

    def test_probe_failure_is_treated_as_unsafe_before_the_open(self):
        ctrl = _incident_controller()
        decision = ctrl.decide(None, market_open=False)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT

    def test_down_session_still_tears_down_before_the_open(self):
        """IsConnected()==0 is not the half-up state. Bug B cleanup stays."""
        ctrl = _incident_controller()
        decision = ctrl.decide(0, market_open=False)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN

    def test_first_attempt_after_clean_disconnect_still_tears_down(self):
        """Attempt #1 LeaveMonitor returned 9999 in the incident. The
        guard must not swallow that call — only the follow-up while the
        handshake is still connecting."""
        ctrl = ReconnectController()
        ctrl.on_clean_disconnect()
        decision = ctrl.decide(IS_CONNECTING, market_open=False)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN

    def test_stray_failure_does_not_arm_the_guard(self):
        ctrl = ReconnectController()
        ctrl.on_attempt_failed()
        decision = ctrl.decide(IS_CONNECTING, market_open=False)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN
        assert not ctrl.previous_attempt_missed_ready


class TestIssue157MarketOpenRecovery:
    """Acceptance (2): once the session is open, teardown is allowed again."""

    def test_connecting_after_the_open_tears_down(self):
        ctrl = _incident_controller()
        decision = ctrl.decide(IS_CONNECTING, market_open=True)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN

    def test_already_connected_never_tears_down(self):
        ctrl = _incident_controller()
        for market_open in (False, True):
            decision = ctrl.decide(IS_CONNECTED, market_open=market_open)
            assert decision.action == ACTION_ALREADY_CONNECTED

    def test_ready_clears_the_latch(self):
        ctrl = _incident_controller()
        assert ctrl.decide(IS_CONNECTING, market_open=False).action == (
            ACTION_SKIP_TEARDOWN_WAIT
        )
        ctrl.on_ready()
        decision = ctrl.decide(IS_CONNECTING, market_open=False)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN
        assert not ctrl.previous_attempt_missed_ready

    def test_clean_disconnect_clears_the_latch(self):
        ctrl = _incident_controller()
        ctrl.on_clean_disconnect()
        decision = ctrl.decide(IS_CONNECTING, market_open=False)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN

    def test_in_flight_handshake_is_half_up_before_the_failure_latch(self):
        """The Ready poll can run while the handshake is still open."""
        ctrl = ReconnectController()
        ctrl.on_clean_disconnect()
        ctrl.on_attempt_started()
        decision = ctrl.decide(IS_CONNECTING, market_open=False)
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
        ctrl.reset()
        assert not ctrl.previous_attempt_missed_ready
        assert not ctrl.handshake_open
        decision = ctrl.decide(IS_CONNECTING, market_open=False)
        assert decision.action == ACTION_TEARDOWN_AND_LOGIN
