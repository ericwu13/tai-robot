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

from src.live.connection_monitor import ConnectionMonitor
from src.live.reconnect_controller import (
    ACTION_ALREADY_CONNECTED,
    ACTION_SKIP_TEARDOWN_WAIT,
    ACTION_TEARDOWN_AND_LOGIN,
    FORCED_TEARDOWN_LOG,
    IS_CONNECTED,
    IS_CONNECTING,
    ReconnectController,
    ReconnectSchedule,
    arm_single_timer,
    in_live_session,
    seconds_since_session_open,
    seconds_until_next_session_open,
)
from src.market_data.holidays import is_taifex_holiday

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

    def test_half_up_at_open_plus_89_skips(self):
        """One second before the deadline is still a skip, not a teardown."""
        ctrl = _incident_controller()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=89)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not decision.forced_teardown

    def test_open_plus_89_skips_and_90_is_the_first_forced_teardown(self):
        """89s still skips. 90s is the first instant a forced teardown is allowed, and 91s is the same."""
        ctrl = _incident_controller()
        at_89 = ctrl.decide(IS_CONNECTING, seconds_since_open=89)
        assert at_89.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not at_89.forced_teardown
        at_90 = ctrl.decide(IS_CONNECTING, seconds_since_open=90)
        assert at_90.action == ACTION_TEARDOWN_AND_LOGIN
        assert at_90.forced_teardown
        assert at_90.reason == FORCED_TEARDOWN_LOG
        at_91 = ctrl.decide(IS_CONNECTING, seconds_since_open=91)
        assert at_91.action == ACTION_TEARDOWN_AND_LOGIN
        assert at_91.forced_teardown
        assert at_91.reason == FORCED_TEARDOWN_LOG

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
    """The long pre-open Ready wait is dropped in favor of the skip polls.

    ``secs_until_open + 90`` as one timer would not re-check
    ``IsConnected()==2``, and a holiday gap can last days. After
    LoginSetQuote the Ready poll is 3s. If the session is still half-up,
    ``decide`` skips and the 15s poll repeats until a real open+90s.
    """

    def test_ready_wait_is_the_short_poll(self):
        ctrl = ReconnectController()
        assert ctrl.ready_wait_seconds() == ReconnectController.READY_WAIT_S
        assert ctrl.READY_WAIT_S == 3

    def test_short_poll_still_skips_half_up_before_open_plus_90(self):
        """A 3s Ready poll must not become a teardown while the hold remains."""
        ctrl = _incident_controller()
        for since in (-120, _OPEN_PLUS_15, 89):
            decision = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
            assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
            assert decision.wait_seconds == ReconnectController.CONNECTING_WAIT_S


class TestOperatorReconnectAndSkipCap:
    """The Reconnect button always tears down. The post-forced loop is finite."""

    def test_manual_reconnect_resets_allowance_and_is_not_refused(self):
        ctrl = _incident_controller()
        ctrl.note_forced_teardown()
        assert ctrl.forced_teardown_used
        # Before open+90 the automatic path still skips. The button must not.
        blocked = ctrl.decide(IS_CONNECTING, seconds_since_open=15)
        assert blocked.action == ACTION_SKIP_TEARDOWN_WAIT
        ctrl.on_operator_reconnect()
        assert not ctrl.forced_teardown_used
        for since in (15, 89, 90, 91):
            decision = ctrl.decide(
                IS_CONNECTING, seconds_since_open=since, manual=True)
            assert decision.action == ACTION_TEARDOWN_AND_LOGIN
            assert not decision.forced_teardown

    def test_post_forced_skip_loop_stops_at_the_limit(self):
        ctrl = _incident_controller()
        # The open+90 hold is not the loop being bounded.
        assert not ctrl.record_skip_poll()
        ctrl.note_forced_teardown()
        for _ in range(ctrl.POST_FORCED_SKIP_LIMIT - 1):
            decision = ctrl.decide(IS_CONNECTING, seconds_since_open=91)
            assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
            assert not decision.forced_teardown
            assert not ctrl.record_skip_poll()
        assert ctrl.record_skip_poll()
        # Stopping the loop does not call LeaveMonitor again.
        again = ctrl.decide(IS_CONNECTING, seconds_since_open=91)
        assert again.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not again.forced_teardown

    def test_operator_reconnect_clears_the_skip_count(self):
        ctrl = _incident_controller()
        ctrl.note_forced_teardown()
        for _ in range(ctrl.POST_FORCED_SKIP_LIMIT - 1):
            assert not ctrl.record_skip_poll()
        ctrl.on_operator_reconnect()
        ctrl.note_forced_teardown()
        for _ in range(ctrl.POST_FORCED_SKIP_LIMIT - 1):
            assert not ctrl.record_skip_poll()
        assert ctrl.record_skip_poll()


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


class TestWeekdayHolidayHalfUp:
    """A weekday TAIFEX holiday is not an open.

    ``is_market_open()`` only knows weekends, so 2026-09-25 10:00 looks
    like a day session hours past open+90s. The half-up guard must still
    skip, and the deferral must aim at the next real open.
    """

    def test_half_up_on_2026_09_25_skips_teardown(self):
        when = datetime(2026, 9, 25, 10, 0, 0, tzinfo=_TPE)
        assert when.weekday() < 5
        assert is_taifex_holiday(when.date())
        assert not in_live_session(when)
        # Next session is Tue 2026-09-29 08:45 (Mon 09-28 is also closed).
        expected_open = datetime(2026, 9, 29, 8, 45, tzinfo=_TPE)
        since = seconds_since_session_open(when)
        assert since == int((when - expected_open).total_seconds())
        assert since < 0
        ctrl = _incident_controller()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not decision.forced_teardown

    def test_holiday_open_plus_91_wall_clock_still_skips(self):
        """08:46:31 is open+91s on the weekend-only clock. Not on this day."""
        when = datetime(2026, 9, 25, 8, 46, 31, tzinfo=_TPE)
        since = seconds_since_session_open(when)
        assert since < ReconnectController.HALF_UP_HOLD_S
        ctrl = _incident_controller()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT

    def test_holiday_deferral_targets_the_next_real_open(self):
        when = datetime(2026, 9, 25, 10, 0, 0, tzinfo=_TPE)
        secs = seconds_until_next_session_open(when)
        assert secs == -seconds_since_session_open(when)
        assert secs > 120
        monitor = ConnectionMonitor()
        monitor.on_disconnected()
        action = monitor.schedule_next(
            has_live_runner=True,
            market_open=in_live_session(when),
            secs_until_open=secs,
        )
        assert action.type == "defer_to_market"
        assert action.delay_seconds == max(secs - 120, 60)


class TestSessionLookupFailOpen:
    """A raised session or holiday lookup must not skip for days.

    Fail open: the clock reports open+90s, so a half-up session gets one
    forced teardown and the second attempt is refused.
    """

    def test_current_session_raise_allows_one_forced_teardown(self, monkeypatch):
        def boom(now=None):
            raise RuntimeError("session lookup failed")

        monkeypatch.setattr("src.regime.switch_logic.current_session", boom)
        # Tuesday 10:00 is a real day session when the lookup works.
        when = datetime(2026, 10, 6, 10, 0, 0, tzinfo=_TPE)
        since = seconds_since_session_open(when)
        assert since == ReconnectController.HALF_UP_HOLD_S
        assert in_live_session(when)
        assert seconds_until_next_session_open(when) == 0
        ctrl = _incident_controller()
        first = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert first.action == ACTION_TEARDOWN_AND_LOGIN
        assert first.forced_teardown
        assert first.reason == FORCED_TEARDOWN_LOG
        ctrl.note_forced_teardown()
        second = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert second.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not second.forced_teardown

    def test_holiday_lookup_raise_allows_one_forced_teardown(self, monkeypatch):
        def boom(d):
            raise RuntimeError("holiday lookup failed")

        monkeypatch.setattr("src.market_data.holidays.is_taifex_holiday", boom)
        # 14:00 is the day-to-night gap: current_session is None, so the
        # next-open scan is what calls the holiday calendar.
        when = datetime(2026, 10, 6, 14, 0, 0, tzinfo=_TPE)
        since = seconds_since_session_open(when)
        assert since == ReconnectController.HALF_UP_HOLD_S
        ctrl = _incident_controller()
        first = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert first.forced_teardown
        ctrl.note_forced_teardown()
        second = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert second.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not second.forced_teardown


class TestSingleReconnectTimer:
    def _clock(self):
        pending: dict[int, object] = {}
        seq = {"n": 0}

        def after(delay_ms, callback):
            seq["n"] += 1
            token = seq["n"]
            pending[token] = (delay_ms, callback)
            return token

        def after_cancel(token):
            pending.pop(token, None)

        return pending, after, after_cancel

    def test_repeated_on_disconnected_leaves_exactly_one_timer(self):
        """Two disconnect arms must not leave two ``root.after`` callbacks."""
        pending, after, after_cancel = self._clock()
        schedule = ReconnectSchedule()
        schedule.arm(
            "reconnect", 28_000, lambda: None, after=after, after_cancel=after_cancel)
        schedule.arm(
            "reconnect", 28_000, lambda: None, after=after, after_cancel=after_cancel)
        assert list(pending) == [schedule.timer_id]
        assert len(pending) == 1
        assert schedule.kind == "reconnect"

    def test_stop_or_manual_reconnect_clears_ready_and_skip(self):
        """``_stop_live`` and ``_manual_reconnect`` both call ``cancel_all``."""
        pending, after, after_cancel = self._clock()
        schedule = ReconnectSchedule()
        schedule.arm(
            "reconnect", 5_000, lambda: None, after=after, after_cancel=after_cancel)
        schedule.arm(
            "ready", 210_000, lambda: None, after=after, after_cancel=after_cancel)
        schedule.arm(
            "skip", 15_000, lambda: None, after=after, after_cancel=after_cancel)
        assert len(pending) == 1
        assert schedule.kind == "skip"
        schedule.cancel_all(after_cancel)
        assert pending == {}
        assert schedule.timer_id is None
        assert schedule.kind is None

    def test_arm_single_timer_still_cancels_the_previous_id(self):
        pending, after, after_cancel = self._clock()
        current = None
        current = arm_single_timer(
            current, 1_000, lambda: None, after=after, after_cancel=after_cancel)
        current = arm_single_timer(
            current, 1_000, lambda: None, after=after, after_cancel=after_cancel)
        assert list(pending) == [current]
