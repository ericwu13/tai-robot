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

import pytest

from src.live.connection_monitor import ConnectionMonitor
from src.live.reconnect_controller import (
    ACTION_ALREADY_CONNECTED,
    ACTION_SKIP_TEARDOWN_WAIT,
    ACTION_TEARDOWN_AND_LOGIN,
    CALENDAR_DEGRADED_P1,
    CALENDAR_RECOVERY_POLLS,
    CLOCK_DISAGREE_WARN,
    CLOCK_FALLBACK_WARN,
    FORCED_TEARDOWN_LOG,
    HOLIDAY_LOOKUP_WARN,
    IS_CONNECTED,
    IS_CONNECTING,
    ReconnectController,
    ReconnectSchedule,
    arm_single_timer,
    calendar_degraded,
    in_live_session,
    pop_calendar_degraded_p1,
    pop_session_clock_warnings,
    reset_session_clock_warnings,
    seconds_since_session_open,
    seconds_until_next_session_open,
)
from src.market_data.holidays import is_taifex_holiday

# Before the open. The skip must not depend on this being negative.
_BEFORE_OPEN = -120
_OPEN_PLUS_15 = 15
_OPEN_PLUS_91 = 91
_TPE = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _reset_clock_warnings():
    reset_session_clock_warnings()
    yield
    reset_session_clock_warnings()


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

    def test_post_forced_skip_limit_is_eight(self):
        """The cap is 8 polls. A limit of 1000 must not survive this test."""
        assert ReconnectController.POST_FORCED_SKIP_LIMIT == 8
        ctrl = _incident_controller()
        ctrl.note_forced_teardown()
        for _ in range(7):
            assert ctrl.record_skip_poll() is False
        assert ctrl.record_skip_poll() is True

    def test_full_drop_resets_post_forced_skips(self):
        """IsConnected()==0 inside this disconnect restarts the skip count.

        A failed probe does not. The one forced-teardown allowance stays
        used: the next half-up stretch still skips, it just gets a fresh 8.
        """
        ctrl = _incident_controller()
        ctrl.note_forced_teardown()
        for _ in range(5):
            assert ctrl.record_skip_poll() is False
        # A failed probe leaves the count where it is: polls 6..8 still cap.
        assert ctrl.decide(None, seconds_since_open=91).action == (
            ACTION_SKIP_TEARDOWN_WAIT
        )
        for _ in range(2):
            assert ctrl.record_skip_poll() is False
        assert ctrl.record_skip_poll() is True
        dropped = ctrl.decide(0, seconds_since_open=91)
        assert dropped.action == ACTION_TEARDOWN_AND_LOGIN
        assert not dropped.forced_teardown
        assert ctrl.forced_teardown_used
        for _ in range(7):
            assert ctrl.record_skip_poll() is False
        assert ctrl.record_skip_poll() is True

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
        # 10:00 is 75 minutes after the day open. The clocks agree, so no WARN.
        ten = datetime(2026, 10, 6, 10, 0, 0, tzinfo=_TPE)
        assert seconds_since_session_open(ten) == 75 * 60
        assert pop_session_clock_warnings() == []

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


class TestSessionLookupFallback:
    """A raised lookup uses the weekend-only clock and queues a WARN.

    Reed's probe: current_session raises at 10:00 on a trading day and
    the swallowed error reports −18000 (next night open). The weekend-only
    clock is seconds since 08:45. That value is past open+90, but the
    calendar is degraded, so the automatic path keeps skipping. It does
    not consume the forced-teardown allowance.
    """

    def test_current_session_raise_at_1000_uses_weekend_clock(self, monkeypatch):
        def boom(now=None):
            raise RuntimeError("session lookup failed")

        monkeypatch.setattr("src.regime.switch_logic.current_session", boom)
        when = datetime(2026, 10, 6, 10, 0, 0, tzinfo=_TPE)
        since = seconds_since_session_open(when)
        # 75 minutes after 08:45. Not −18000, and not a hardcoded open+90.
        assert since == 75 * 60
        assert since != -18000
        assert calendar_degraded()
        assert in_live_session(when)
        assert seconds_until_next_session_open(when) == 0
        assert pop_session_clock_warnings() == [CLOCK_FALLBACK_WARN]
        ctrl = _incident_controller()
        first = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert first.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not first.forced_teardown
        assert pop_calendar_degraded_p1() == CALENDAR_DEGRADED_P1
        for _ in range(8):
            again = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
            assert again.action == ACTION_SKIP_TEARDOWN_WAIT
            assert not again.forced_teardown
            assert ctrl.record_skip_poll() is False
        assert pop_calendar_degraded_p1() is None
        assert not ctrl.forced_teardown_used

    def test_current_session_raise_on_saturday_keeps_skipping(self, monkeypatch):
        """A dead session lookup must not invent open+90 while closed."""
        def boom(now=None):
            raise RuntimeError("session lookup failed")

        monkeypatch.setattr("src.regime.switch_logic.current_session", boom)
        when = datetime(2026, 10, 10, 10, 0, 0, tzinfo=_TPE)
        assert when.weekday() == 5
        since = seconds_since_session_open(when)
        assert since < 0
        assert since < ReconnectController.HALF_UP_HOLD_S
        ctrl = _incident_controller()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not decision.forced_teardown

    def test_holiday_call_raise_keeps_skipping_when_closed(self, monkeypatch):
        """Call-time is_taifex_holiday failure uses the weekend clock and warns.

        Saturday 10:00 and Monday 03:00 are closed on that clock. The
        half-up guard keeps skipping. It does not take the live-session
        fail-open. The probe runs before current_session, which would
        swallow the raise.
        """
        def boom(d):
            raise RuntimeError("holiday lookup failed")

        monkeypatch.setattr("src.market_data.holidays.is_taifex_holiday", boom)
        saturday = datetime(2026, 10, 10, 10, 0, 0, tzinfo=_TPE)
        monday_overnight = datetime(2026, 10, 5, 3, 0, 0, tzinfo=_TPE)
        assert saturday.weekday() == 5
        assert monday_overnight.weekday() == 0
        for when in (saturday, monday_overnight):
            reset_session_clock_warnings()
            since = seconds_since_session_open(when)
            assert since < 0, when
            assert since < ReconnectController.HALF_UP_HOLD_S
            assert pop_session_clock_warnings() == [CLOCK_FALLBACK_WARN]
            ctrl = _incident_controller()
            decision = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
            assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
            assert not decision.forced_teardown
            # Closed market: no "press Reconnect" P1.
            assert pop_calendar_degraded_p1() is None

    def test_friday_session_holiday_raise_warns_before_current_session(self, monkeypatch):
        """A raise during Friday session hours must not stay silent.

        2026-10-09 10:00 is 75 minutes after the weekend clock's 08:45.
        current_session only asks is_taifex_holiday(today) and swallows
        the raise, so without the today+yesterday probe there is no WARN
        and the half-up guard sees a live session.
        """
        from datetime import date as date_cls

        seen: list[date_cls] = []

        def boom(d):
            seen.append(d)
            raise RuntimeError("holiday lookup failed")

        monkeypatch.setattr("src.market_data.holidays.is_taifex_holiday", boom)
        when = datetime(2026, 10, 9, 10, 0, 0, tzinfo=_TPE)
        assert when.weekday() == 4
        reset_session_clock_warnings()
        since = seconds_since_session_open(when)
        assert since == 75 * 60
        assert since >= ReconnectController.HALF_UP_HOLD_S
        assert set(seen) == {when.date(), when.date() - timedelta(days=1)}
        assert pop_session_clock_warnings() == [CLOCK_FALLBACK_WARN]
        assert calendar_degraded()
        ctrl = _incident_controller()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not decision.forced_teardown
        assert pop_calendar_degraded_p1() == CALENDAR_DEGRADED_P1
        # The same failure on the deferral flag is one latched WARN.
        assert in_live_session(when) is True
        assert pop_session_clock_warnings() == []
        assert ctrl.decide(
            IS_CONNECTING, seconds_since_open=since).action == (
            ACTION_SKIP_TEARDOWN_WAIT)
        assert pop_calendar_degraded_p1() is None

    def test_yesterday_holiday_raise_still_warns_on_friday_morning(self, monkeypatch):
        """Yesterday is probed even when today's call succeeds.

        Day-session current_session never asks about yesterday, so a
        raise there used to disappear.
        """
        from datetime import date as date_cls

        yesterday = date_cls(2026, 10, 8)

        def boom(d):
            if d == yesterday:
                raise RuntimeError("yesterday failed")
            return False

        monkeypatch.setattr("src.market_data.holidays.is_taifex_holiday", boom)
        when = datetime(2026, 10, 9, 10, 0, 0, tzinfo=_TPE)
        reset_session_clock_warnings()
        since = seconds_since_session_open(when)
        assert since == 75 * 60
        assert pop_session_clock_warnings() == [CLOCK_FALLBACK_WARN]
        assert calendar_degraded()


class TestDegradedCalendarDoesNotForceTeardown:
    """A raised holiday or session lookup withholds the automatic teardown."""

    _FRIDAYS = (
        datetime(2026, 10, 9, 10, 0, 0, tzinfo=_TPE),
        datetime(2027, 1, 1, 10, 0, 0, tzinfo=_TPE),
    )

    def test_full_drop_while_degraded_does_not_tear_down(self, monkeypatch):
        """A 0 reading during a degraded half-up episode is not a teardown.

        Ready (IsConnected()==1) is still already-connected. A healthy
        full drop, with the calendar up, still tears down.
        """
        def boom(d):
            raise RuntimeError("holiday lookup failed")

        monkeypatch.setattr("src.market_data.holidays.is_taifex_holiday", boom)
        when = self._FRIDAYS[0]
        since = seconds_since_session_open(when)
        assert since >= ReconnectController.HALF_UP_HOLD_S
        ctrl = _incident_controller()
        dropped = ctrl.decide(0, seconds_since_open=since)
        assert dropped.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not dropped.forced_teardown
        ready = ctrl.decide(IS_CONNECTED, seconds_since_open=since)
        assert ready.action == ACTION_ALREADY_CONNECTED
        reset_session_clock_warnings()
        healthy = _incident_controller()
        still_down = healthy.decide(0, seconds_since_open=since)
        assert still_down.action == ACTION_TEARDOWN_AND_LOGIN

    def test_manual_confirm_required_past_weekend_open_plus_90(self, monkeypatch):
        def boom(d):
            raise RuntimeError("holiday lookup failed")

        monkeypatch.setattr("src.market_data.holidays.is_taifex_holiday", boom)
        for when in self._FRIDAYS:
            assert when.weekday() == 4
            reset_session_clock_warnings()
            since = seconds_since_session_open(when)
            assert since == 75 * 60
            ctrl = _incident_controller()
            assert ctrl.manual_half_up_needs_confirm(IS_CONNECTING, since)
            assert ctrl.manual_half_up_needs_confirm(None, since)
            reset_session_clock_warnings()
            assert not calendar_degraded()
            assert not ctrl.manual_half_up_needs_confirm(IS_CONNECTING, since)

    def test_one_good_lookup_does_not_rearm_recovery_does(self, monkeypatch):
        """The P1 re-arms after the recovery streak, not after one success."""
        state = {"fail": True}

        def holiday(d):
            if state["fail"]:
                raise RuntimeError("holiday lookup failed")
            return d.weekday() >= 5

        monkeypatch.setattr("src.market_data.holidays.is_taifex_holiday", holiday)
        when = self._FRIDAYS[0]
        since = seconds_since_session_open(when)
        assert pop_session_clock_warnings() == [CLOCK_FALLBACK_WARN]
        ctrl = _incident_controller()
        first = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert first.action == ACTION_SKIP_TEARDOWN_WAIT
        assert pop_calendar_degraded_p1() == CALENDAR_DEGRADED_P1

        state["fail"] = False
        seconds_since_session_open(when)
        assert calendar_degraded()
        held = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert held.action == ACTION_SKIP_TEARDOWN_WAIT
        assert pop_calendar_degraded_p1() is None
        for _ in range(CALENDAR_RECOVERY_POLLS - 2):
            seconds_since_session_open(when)
            assert calendar_degraded()
        recovered = seconds_since_session_open(when)
        assert not calendar_degraded()
        assert recovered == 75 * 60
        assert pop_session_clock_warnings() == []

        state["fail"] = True
        degraded_since = seconds_since_session_open(when)
        again = ctrl.decide(IS_CONNECTING, seconds_since_open=degraded_since)
        assert again.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not again.forced_teardown
        assert pop_calendar_degraded_p1() == CALENDAR_DEGRADED_P1
        ctrl.decide(IS_CONNECTING, seconds_since_open=degraded_since)
        assert pop_calendar_degraded_p1() is None

    def test_ready_rearms_and_a_drop_does_not(self, monkeypatch):
        def boom(now=None):
            raise RuntimeError("session lookup failed")

        monkeypatch.setattr("src.regime.switch_logic.current_session", boom)
        when = datetime(2026, 10, 6, 10, 0, 0, tzinfo=_TPE)
        since = seconds_since_session_open(when)
        ctrl = _incident_controller()
        assert ctrl.decide(IS_CONNECTING, seconds_since_open=since).action == (
            ACTION_SKIP_TEARDOWN_WAIT)
        assert pop_calendar_degraded_p1() == CALENDAR_DEGRADED_P1

        dropped = ctrl.decide(0, seconds_since_open=since)
        assert dropped.action == ACTION_SKIP_TEARDOWN_WAIT
        assert ctrl.decide(IS_CONNECTING, seconds_since_open=since).action == (
            ACTION_SKIP_TEARDOWN_WAIT)
        assert pop_calendar_degraded_p1() is None

        ctrl.on_ready()
        ctrl.on_clean_disconnect()
        ctrl.on_attempt_started()
        ctrl.on_attempt_failed()
        assert ctrl.decide(IS_CONNECTING, seconds_since_open=since).action == (
            ACTION_SKIP_TEARDOWN_WAIT)
        assert pop_calendar_degraded_p1() == CALENDAR_DEGRADED_P1


class TestClockDisagreement:
    """A successful holiday-aware answer that fights is_market_open warns.

    The holiday-aware value stays in force, so a real holiday still skips.
    """

    def test_weekday_holiday_warns_and_still_skips(self):
        when = datetime(2026, 9, 25, 10, 0, 0, tzinfo=_TPE)
        since = seconds_since_session_open(when)
        assert since < 0
        assert not in_live_session(when)
        # in_live_session notes the same disagreement; the latch keeps one.
        assert pop_session_clock_warnings() == [CLOCK_DISAGREE_WARN]
        ctrl = _incident_controller()
        decision = ctrl.decide(IS_CONNECTING, seconds_since_open=since)
        assert decision.action == ACTION_SKIP_TEARDOWN_WAIT
        assert not decision.forced_teardown

    def test_disagreement_warns_at_most_once_per_calendar_day(self):
        """Agreeing and disagreeing again the same day does not re-warn."""
        friday = datetime(2026, 9, 25, 10, 0, 0, tzinfo=_TPE)
        seconds_since_session_open(friday)
        assert pop_session_clock_warnings() == [CLOCK_DISAGREE_WARN]
        seconds_since_session_open(friday.replace(hour=11))
        in_live_session(friday)
        assert pop_session_clock_warnings() == []
        monday = datetime(2026, 9, 28, 10, 0, 0, tzinfo=_TPE)
        seconds_since_session_open(monday)
        assert pop_session_clock_warnings() == [CLOCK_DISAGREE_WARN]


class TestHolidayWarnLatch:
    """A calendar that fails on only some dates warns once per failure stretch."""

    def test_partial_holiday_failure_warns_once_across_eight_polls(self, monkeypatch):
        from datetime import date as date_cls

        def holiday(d):
            if d >= date_cls(2026, 10, 12):
                raise RuntimeError(f"calendar failed for {d}")
            return d.weekday() >= 5

        monkeypatch.setattr("src.market_data.holidays.is_taifex_holiday", holiday)
        saturday = datetime(2026, 10, 10, 10, 0, 0, tzinfo=_TPE)
        assert saturday.weekday() == 5
        reset_session_clock_warnings()
        warned = []
        for _ in range(8):
            since = seconds_since_session_open(saturday)
            assert since < 0
            warned.extend(pop_session_clock_warnings())
        assert warned == [HOLIDAY_LOOKUP_WARN]


class TestFallbackWarnIsNotMasked:
    """Each clock entry queues its own WARN. The other must not be required."""

    def test_seconds_since_warns_without_in_live_session(self, monkeypatch):
        def boom(now=None):
            raise RuntimeError("session lookup failed")

        monkeypatch.setattr("src.regime.switch_logic.current_session", boom)
        when = datetime(2026, 10, 6, 10, 0, 0, tzinfo=_TPE)
        reset_session_clock_warnings()
        since = seconds_since_session_open(when)
        assert since == 75 * 60
        assert pop_session_clock_warnings() == [CLOCK_FALLBACK_WARN]

    def test_in_live_session_holiday_raise_warns_alone(self, monkeypatch):
        def boom(d):
            raise RuntimeError("holiday lookup failed")

        monkeypatch.setattr("src.market_data.holidays.is_taifex_holiday", boom)
        when = datetime(2026, 10, 9, 10, 0, 0, tzinfo=_TPE)
        assert when.weekday() == 4
        reset_session_clock_warnings()
        assert in_live_session(when) is True
        assert pop_session_clock_warnings() == [CLOCK_FALLBACK_WARN]


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
