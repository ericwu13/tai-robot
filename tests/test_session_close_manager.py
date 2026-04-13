"""Tests for SessionCloseManager -- force-close state machine.

Safety-critical code: comprehensive tests for every state transition,
retry path, edge case, and both AM/Night sessions.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.live.session_close_manager import (
    SessionCloseManager,
    CloseState,
    EVT_FORCE_CLOSE_STARTED,
    EVT_FORCE_CLOSE_RETRIED,
    EVT_FORCE_CLOSE_FAILED,
    EVT_EMERGENCY_SWEEP,
    EVT_SESSION_END_PENDING_SET,
    EVT_SESSION_END_PENDING_CLEARED,
    APPROACHING_MINUTES,
    EMERGENCY_MINUTES,
    POLL_NORMAL,
    POLL_APPROACHING,
    POLL_FORCE_CLOSE,
    POLL_EMERGENCY,
    ATTEMPT_TIMEOUTS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockDeps:
    """Collects mock dependencies for SessionCloseManager."""

    def __init__(self, *, minutes=None, force_close_result=True):
        self.events: list[tuple[str, dict]] = []
        self.alarms: list[str] = []
        self.deferred_cleared: int = 0
        self.force_close_calls: list[int] = []
        self._minutes = minutes  # int, None, or callable(dt)->int|None
        self._fc_result = force_close_result  # bool or callable(attempt)->bool

    def minutes_fn(self, dt):
        if callable(self._minutes):
            return self._minutes(dt)
        return self._minutes

    def force_close_fn(self, attempt):
        self.force_close_calls.append(attempt)
        if callable(self._fc_result):
            return self._fc_result(attempt)
        return self._fc_result

    def on_event(self, event, detail):
        self.events.append((event, detail))

    def alarm_fn(self, msg):
        self.alarms.append(msg)

    def clear_deferred_fn(self):
        self.deferred_cleared += 1

    def create(self, **overrides):
        kwargs = dict(
            force_close_fn=self.force_close_fn,
            minutes_until_close_fn=self.minutes_fn,
            clear_deferred_close_fn=self.clear_deferred_fn,
            on_event=self.on_event,
            alarm_fn=self.alarm_fn,
        )
        kwargs.update(overrides)
        return SessionCloseManager(**kwargs)

    def event_names(self):
        return [e for e, _ in self.events]


# Base times for tests
_DAY_BASE = datetime(2026, 3, 17)   # Tuesday
_NIGHT_BASE = datetime(2026, 3, 18)  # Wednesday (after midnight portion)


def _dt(h, m, s=0, base=None):
    """Create a datetime at h:m:s on base date."""
    base = base or _DAY_BASE
    return base.replace(hour=h, minute=m, second=s)


# Convenience: create a manager at a given state by feeding ticks
def _manager_at_approaching(deps, mins=4, pos=0):
    """Return a manager in APPROACHING_CLOSE state."""
    deps._minutes = mins
    mgr = deps.create()
    mgr.tick(_dt(13, 41), pos)
    assert mgr.state == CloseState.APPROACHING_CLOSE
    return mgr


def _manager_at_force_close_1(deps, mins=3):
    """Return a manager in FORCE_CLOSE_ATTEMPT_1 with position open."""
    deps._minutes = mins
    mgr = deps.create()
    mgr.tick(_dt(13, 42), 1)  # pos=1, triggers force close
    assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_1
    return mgr


# ---------------------------------------------------------------------------
# 1. State machine transitions
# ---------------------------------------------------------------------------

class TestStateTransitions:

    def test_normal_to_approaching_at_threshold(self):
        deps = MockDeps(minutes=APPROACHING_MINUTES)
        mgr = deps.create()
        assert mgr.state == CloseState.NORMAL
        mgr.tick(_dt(13, 40), 0)
        assert mgr.state == CloseState.APPROACHING_CLOSE

    def test_normal_stays_above_threshold(self):
        deps = MockDeps(minutes=60)
        mgr = deps.create()
        poll = mgr.tick(_dt(12, 45), 0)
        assert mgr.state == CloseState.NORMAL
        assert poll == POLL_NORMAL

    def test_normal_stays_when_market_closed(self):
        deps = MockDeps(minutes=None)
        mgr = deps.create()
        poll = mgr.tick(_dt(14, 0), 0)
        assert mgr.state == CloseState.NORMAL
        assert poll == POLL_NORMAL

    def test_approaching_to_force_close_with_position(self):
        deps = MockDeps(minutes=4)
        mgr = deps.create()
        mgr.tick(_dt(13, 41), 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_1

    def test_approaching_stays_when_flat(self):
        deps = MockDeps(minutes=4)
        mgr = deps.create()
        poll = mgr.tick(_dt(13, 41), 0)
        assert mgr.state == CloseState.APPROACHING_CLOSE
        assert poll == POLL_APPROACHING

    def test_approaching_to_emergency_under_1min(self):
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        mgr.tick(_dt(13, 44), 1)
        assert mgr.state == CloseState.EMERGENCY_TICK_SWEEP

    def test_approaching_back_to_normal_on_new_session(self):
        mins_val = [3]
        deps = MockDeps(minutes=lambda dt: mins_val[0])
        mgr = deps.create()
        mgr.tick(_dt(13, 42), 0)
        assert mgr.state == CloseState.APPROACHING_CLOSE

        mins_val[0] = 300  # new session
        mgr.tick(_dt(15, 0), 0)
        assert mgr.state == CloseState.NORMAL

    def test_approaching_to_session_end_on_market_close(self):
        mins_val = [3]
        deps = MockDeps(minutes=lambda dt: mins_val[0])
        mgr = deps.create()
        mgr.tick(_dt(13, 42), 0)
        assert mgr.state == CloseState.APPROACHING_CLOSE

        mins_val[0] = None
        mgr.tick(_dt(13, 46), 0)
        assert mgr.state == CloseState.SESSION_END_PENDING

    def test_force_close_to_session_end_when_flat(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        mgr.tick(_dt(13, 42, 10), 0)  # position now flat
        assert mgr.state == CloseState.SESSION_END_PENDING

    def test_force_close_1_to_2_on_timeout(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        timeout = ATTEMPT_TIMEOUTS[CloseState.FORCE_CLOSE_ATTEMPT_1]
        later = _dt(13, 42) + timedelta(seconds=timeout)
        deps._minutes = 2
        mgr.tick(later, 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_2
        assert len(deps.force_close_calls) == 2  # initial + retry

    def test_force_close_2_to_3_on_timeout(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)

        # Advance past attempt 1 timeout
        t1 = ATTEMPT_TIMEOUTS[CloseState.FORCE_CLOSE_ATTEMPT_1]
        now = _dt(13, 42) + timedelta(seconds=t1)
        deps._minutes = 2
        mgr.tick(now, 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_2

        # Advance past attempt 2 timeout
        t2 = ATTEMPT_TIMEOUTS[CloseState.FORCE_CLOSE_ATTEMPT_2]
        now2 = now + timedelta(seconds=t2)
        mgr.tick(now2, 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_3
        assert len(deps.force_close_calls) == 3

    def test_force_close_to_emergency_on_1min(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        deps._minutes = EMERGENCY_MINUTES
        mgr.tick(_dt(13, 44), 1)
        assert mgr.state == CloseState.EMERGENCY_TICK_SWEEP

    def test_force_close_to_session_end_on_market_close(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        deps._minutes = None
        mgr.tick(_dt(13, 46), 1)
        assert mgr.state == CloseState.SESSION_END_PENDING
        assert len(deps.alarms) == 1
        assert "CRITICAL" in deps.alarms[0]

    def test_emergency_to_session_end_when_flat(self):
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        mgr.tick(_dt(13, 44), 1)
        assert mgr.state == CloseState.EMERGENCY_TICK_SWEEP

        mgr.tick(_dt(13, 44, 5), 0)
        assert mgr.state == CloseState.SESSION_END_PENDING

    def test_emergency_to_session_end_on_market_close(self):
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        mgr.tick(_dt(13, 44), 1)
        assert mgr.state == CloseState.EMERGENCY_TICK_SWEEP

        deps._minutes = None
        mgr.tick(_dt(13, 46), 1)
        assert mgr.state == CloseState.SESSION_END_PENDING
        assert len(deps.alarms) == 1

    def test_session_end_to_normal_on_new_session(self):
        deps = MockDeps(minutes=None)
        mgr = deps.create()
        mgr.state = CloseState.SESSION_END_PENDING
        mgr.session_end_pending = True

        deps._minutes = 300
        mgr.tick(_dt(15, 0), 0)
        assert mgr.state == CloseState.NORMAL
        assert mgr.session_end_pending is False
        assert EVT_SESSION_END_PENDING_CLEARED in deps.event_names()


# ---------------------------------------------------------------------------
# 2. Polling frequency changes
# ---------------------------------------------------------------------------

class TestPollingFrequency:

    def test_normal_returns_30s(self):
        deps = MockDeps(minutes=60)
        mgr = deps.create()
        assert mgr.tick(_dt(12, 0), 0) == POLL_NORMAL

    def test_approaching_flat_returns_5s(self):
        deps = MockDeps(minutes=4)
        mgr = deps.create()
        assert mgr.tick(_dt(13, 41), 0) == POLL_APPROACHING

    def test_force_close_returns_2s(self):
        deps = MockDeps(minutes=3)
        mgr = deps.create()
        assert mgr.tick(_dt(13, 42), 1) == POLL_FORCE_CLOSE

    def test_emergency_returns_1s(self):
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        assert mgr.tick(_dt(13, 44), 1) == POLL_EMERGENCY

    def test_session_end_returns_30s(self):
        deps = MockDeps(minutes=None)
        mgr = deps.create()
        mgr.state = CloseState.SESSION_END_PENDING
        assert mgr.tick(_dt(14, 0), 0) == POLL_NORMAL


# ---------------------------------------------------------------------------
# 3. Retry logic
# ---------------------------------------------------------------------------

class TestRetryLogic:

    def test_first_attempt_sends_order(self):
        deps = MockDeps(minutes=3)
        mgr = deps.create()
        mgr.tick(_dt(13, 42), 1)
        assert deps.force_close_calls == [1]
        assert EVT_FORCE_CLOSE_STARTED in deps.event_names()

    def test_retry_after_timeout_sends_order(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        timeout = ATTEMPT_TIMEOUTS[CloseState.FORCE_CLOSE_ATTEMPT_1]
        later = _dt(13, 42) + timedelta(seconds=timeout)
        deps._minutes = 2
        mgr.tick(later, 1)
        assert len(deps.force_close_calls) == 2
        assert EVT_FORCE_CLOSE_RETRIED in deps.event_names()

    def test_all_three_fail_triggers_alarm(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)

        # Exhaust all attempts
        t = _dt(13, 42)
        for state in [CloseState.FORCE_CLOSE_ATTEMPT_1,
                      CloseState.FORCE_CLOSE_ATTEMPT_2,
                      CloseState.FORCE_CLOSE_ATTEMPT_3]:
            timeout = ATTEMPT_TIMEOUTS[state]
            t = t + timedelta(seconds=timeout)
            deps._minutes = 2
            mgr.tick(t, 1)

        assert EVT_FORCE_CLOSE_FAILED in deps.event_names()
        assert any("all retry attempts" in d.get("reason", "")
                    for e, d in deps.events if e == EVT_FORCE_CLOSE_FAILED)
        assert len(deps.alarms) == 1

    def test_second_attempt_succeeds_goes_session_end(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        timeout = ATTEMPT_TIMEOUTS[CloseState.FORCE_CLOSE_ATTEMPT_1]
        later = _dt(13, 42) + timedelta(seconds=timeout)
        deps._minutes = 2
        mgr.tick(later, 1)  # attempt 2 sent
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_2

        # Position now flat
        mgr.tick(later + timedelta(seconds=1), 0)
        assert mgr.state == CloseState.SESSION_END_PENDING

    def test_force_close_fn_called_with_attempt_number(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        assert deps.force_close_calls == [1]

        timeout = ATTEMPT_TIMEOUTS[CloseState.FORCE_CLOSE_ATTEMPT_1]
        deps._minutes = 2
        mgr.tick(_dt(13, 42) + timedelta(seconds=timeout), 1)
        assert deps.force_close_calls == [1, 2]


# ---------------------------------------------------------------------------
# 4. Fill verification (position check after send)
# ---------------------------------------------------------------------------

class TestFillVerification:

    def test_position_open_after_send_stays_in_force_close(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        # Position still open, but not enough time for retry
        mgr.tick(_dt(13, 42, 5), 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_1

    def test_position_flat_after_send_goes_session_end(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        mgr.tick(_dt(13, 42, 5), 0)
        assert mgr.state == CloseState.SESSION_END_PENDING


# ---------------------------------------------------------------------------
# 5. Backoff timing
# ---------------------------------------------------------------------------

class TestBackoffTiming:

    def test_attempt_1_waits_15s(self):
        assert ATTEMPT_TIMEOUTS[CloseState.FORCE_CLOSE_ATTEMPT_1] == 15

    def test_attempt_2_waits_10s(self):
        assert ATTEMPT_TIMEOUTS[CloseState.FORCE_CLOSE_ATTEMPT_2] == 10

    def test_attempt_3_waits_10s(self):
        assert ATTEMPT_TIMEOUTS[CloseState.FORCE_CLOSE_ATTEMPT_3] == 10

    def test_no_retry_before_timeout(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        # Tick 5 seconds later -- should NOT retry yet
        mgr.tick(_dt(13, 42, 5), 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_1
        assert len(deps.force_close_calls) == 1  # only the initial send

    def test_retry_fires_exactly_at_timeout(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        timeout = ATTEMPT_TIMEOUTS[CloseState.FORCE_CLOSE_ATTEMPT_1]
        deps._minutes = 2
        mgr.tick(_dt(13, 42) + timedelta(seconds=timeout), 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_2
        assert len(deps.force_close_calls) == 2


# ---------------------------------------------------------------------------
# 6. Emergency tick sweep
# ---------------------------------------------------------------------------

class TestEmergencyTickSweep:

    def test_per_tick_force_close(self):
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        mgr.tick(_dt(13, 44), 1)
        assert mgr.state == CloseState.EMERGENCY_TICK_SWEEP
        deps.force_close_calls.clear()

        # Each market tick should attempt close
        for i in range(5):
            result = mgr.on_market_tick(_dt(13, 44, i + 1), 1)
            assert result is True
        assert len(deps.force_close_calls) == 5

    def test_stops_when_flat(self):
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        mgr.tick(_dt(13, 44), 1)
        assert mgr.state == CloseState.EMERGENCY_TICK_SWEEP

        result = mgr.on_market_tick(_dt(13, 44, 1), 0)
        assert result is False
        assert mgr.state == CloseState.SESSION_END_PENDING

    def test_increments_count(self):
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        mgr.tick(_dt(13, 44), 1)

        mgr.on_market_tick(_dt(13, 44, 1), 1)
        mgr.on_market_tick(_dt(13, 44, 2), 1)
        assert mgr._emergency_count == 2
        assert len([e for e, _ in deps.events if e == EVT_EMERGENCY_SWEEP]) == 2

    def test_market_close_during_sweep_alarms(self):
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        mgr.tick(_dt(13, 44), 1)

        deps._minutes = None
        mgr.tick(_dt(13, 46), 1)
        assert mgr.state == CloseState.SESSION_END_PENDING
        assert len(deps.alarms) == 1
        assert "emergency sweep" in deps.alarms[0].lower()

    def test_noop_outside_emergency_state(self):
        deps = MockDeps(minutes=60)
        mgr = deps.create()
        result = mgr.on_market_tick(_dt(12, 0), 1)
        assert result is False
        assert deps.force_close_calls == []


# ---------------------------------------------------------------------------
# 7. Session-end pending flag
# ---------------------------------------------------------------------------

class TestSessionEndPending:

    def test_set_on_approaching(self):
        deps = MockDeps(minutes=4)
        mgr = deps.create()
        mgr.tick(_dt(13, 41), 0)
        assert mgr.session_end_pending is True
        assert EVT_SESSION_END_PENDING_SET in deps.event_names()

    def test_not_set_when_normal(self):
        deps = MockDeps(minutes=60)
        mgr = deps.create()
        mgr.tick(_dt(12, 0), 0)
        assert mgr.session_end_pending is False

    def test_cleared_on_new_session(self):
        deps = MockDeps(minutes=4)
        mgr = deps.create()
        mgr.tick(_dt(13, 41), 0)
        assert mgr.session_end_pending is True

        deps._minutes = 300
        mgr.tick(_dt(15, 0), 0)
        assert mgr.session_end_pending is False
        assert EVT_SESSION_END_PENDING_CLEARED in deps.event_names()

    def test_set_event_has_minutes(self):
        deps = MockDeps(minutes=3)
        mgr = deps.create()
        mgr.tick(_dt(13, 42), 0)
        set_events = [(e, d) for e, d in deps.events
                      if e == EVT_SESSION_END_PENDING_SET]
        assert len(set_events) == 1
        assert set_events[0][1]["minutes"] == 3

    def test_set_only_once(self):
        deps = MockDeps(minutes=4)
        mgr = deps.create()
        mgr.tick(_dt(13, 41), 0)
        mgr.tick(_dt(13, 41, 5), 0)
        set_events = [e for e, _ in deps.events
                      if e == EVT_SESSION_END_PENDING_SET]
        assert len(set_events) == 1


# ---------------------------------------------------------------------------
# 8. Clock drift scenarios
# ---------------------------------------------------------------------------

class TestClockDrift:

    def test_clock_jumps_forward_past_close(self):
        """Clock jumps from 13:42 to 13:46 (market closed)."""
        mins_val = [3]
        deps = MockDeps(minutes=lambda dt: mins_val[0])
        mgr = deps.create()
        mgr.tick(_dt(13, 42), 0)
        assert mgr.state == CloseState.APPROACHING_CLOSE

        mins_val[0] = None  # market closed after jump
        mgr.tick(_dt(13, 46), 0)
        assert mgr.state == CloseState.SESSION_END_PENDING

    def test_clock_jumps_back_reenters_normal(self):
        """Clock jumps back from near-close to far-from-close."""
        mins_val = [3]
        deps = MockDeps(minutes=lambda dt: mins_val[0])
        mgr = deps.create()
        mgr.tick(_dt(13, 42), 0)
        assert mgr.state == CloseState.APPROACHING_CLOSE

        mins_val[0] = 60  # clock jumped back
        mgr.tick(_dt(12, 45), 0)
        assert mgr.state == CloseState.NORMAL

    def test_clock_jump_during_force_close_to_emergency(self):
        """Clock jumps forward during force close attempt."""
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)

        deps._minutes = EMERGENCY_MINUTES
        mgr.tick(_dt(13, 44), 1)
        assert mgr.state == CloseState.EMERGENCY_TICK_SWEEP


# ---------------------------------------------------------------------------
# 9. Both day (13:45) and night (05:00) session ends
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("session,base,close_h,close_m", [
    ("day", _DAY_BASE, 13, 45),
    ("night", _NIGHT_BASE, 5, 0),
], ids=["day_session", "night_session"])
class TestBothSessions:

    def test_full_force_close_sequence(self, session, base, close_h, close_m):
        """Walk through NORMAL -> APPROACHING -> FORCE_CLOSE -> SESSION_END."""
        mins_val = [4]
        deps = MockDeps(minutes=lambda dt: mins_val[0])
        mgr = deps.create()

        # Approaching close
        t = _dt(close_h, close_m, base=base) - timedelta(minutes=4)
        mgr.tick(t, 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_1
        assert mgr.session_end_pending is True

        # Position goes flat after force close
        mins_val[0] = 3
        mgr.tick(t + timedelta(seconds=5), 0)
        assert mgr.state == CloseState.SESSION_END_PENDING

    def test_emergency_sweep(self, session, base, close_h, close_m):
        """Emergency sweep triggers at 1 min before close."""
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        t = _dt(close_h, close_m, base=base) - timedelta(minutes=1)
        mgr.tick(t, 1)
        assert mgr.state == CloseState.EMERGENCY_TICK_SWEEP

        # Sweep closes position
        mgr.on_market_tick(t + timedelta(seconds=1), 0)
        assert mgr.state == CloseState.SESSION_END_PENDING

    def test_no_action_when_flat(self, session, base, close_h, close_m):
        """No force close when position is already flat."""
        deps = MockDeps(minutes=3)
        mgr = deps.create()
        t = _dt(close_h, close_m, base=base) - timedelta(minutes=3)
        mgr.tick(t, 0)
        assert mgr.state == CloseState.APPROACHING_CLOSE
        assert deps.force_close_calls == []


# ---------------------------------------------------------------------------
# 10. Deferred close cleared when force-close starts
# ---------------------------------------------------------------------------

class TestDeferredCloseClearedOnForceClose:

    def test_cleared_on_first_attempt(self):
        deps = MockDeps(minutes=3)
        mgr = deps.create()
        mgr.tick(_dt(13, 42), 1)
        assert deps.deferred_cleared == 1

    def test_cleared_on_retry(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        initial_clears = deps.deferred_cleared
        timeout = ATTEMPT_TIMEOUTS[CloseState.FORCE_CLOSE_ATTEMPT_1]
        deps._minutes = 2
        mgr.tick(_dt(13, 42) + timedelta(seconds=timeout), 1)
        assert deps.deferred_cleared == initial_clears + 1

    def test_cleared_on_emergency_from_approaching(self):
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        mgr.tick(_dt(13, 44), 1)
        assert deps.deferred_cleared == 1


# ---------------------------------------------------------------------------
# 11. No-op when already flat
# ---------------------------------------------------------------------------

class TestNoOpWhenFlat:

    def test_approaching_flat_no_force_close(self):
        deps = MockDeps(minutes=3)
        mgr = deps.create()
        mgr.tick(_dt(13, 42), 0)
        assert mgr.state == CloseState.APPROACHING_CLOSE
        assert deps.force_close_calls == []

    def test_normal_flat_no_action(self):
        deps = MockDeps(minutes=60)
        mgr = deps.create()
        mgr.tick(_dt(12, 0), 0)
        assert deps.force_close_calls == []
        assert deps.events == []

    def test_emergency_flat_goes_session_end(self):
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        mgr.tick(_dt(13, 44), 0)
        # Should be approaching (flat), not emergency
        assert mgr.state == CloseState.APPROACHING_CLOSE


# ---------------------------------------------------------------------------
# 12. Order sender failures
# ---------------------------------------------------------------------------

class TestOrderSenderFailures:

    def test_false_return_still_transitions(self):
        deps = MockDeps(minutes=3, force_close_result=False)
        mgr = deps.create()
        mgr.tick(_dt(13, 42), 1)
        # Still enters force close state (will retry via timeout)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_1
        assert deps.force_close_calls == [1]

    def test_exception_propagates(self):
        """Exceptions in force_close_fn should propagate to caller."""
        def bad_fn(attempt):
            raise RuntimeError("COM crashed")

        deps = MockDeps(minutes=3)
        mgr = deps.create(force_close_fn=bad_fn)
        with pytest.raises(RuntimeError, match="COM crashed"):
            mgr.tick(_dt(13, 42), 1)


# ---------------------------------------------------------------------------
# 13. Concurrent position changes
# ---------------------------------------------------------------------------

class TestConcurrentPositionChanges:

    def test_position_closes_via_strategy_mid_sequence(self):
        """Strategy closes position while force-close state machine is active."""
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        # Strategy closed the position between ticks
        mgr.tick(_dt(13, 42, 10), 0)
        assert mgr.state == CloseState.SESSION_END_PENDING
        # No additional force close sent (only initial attempt)
        assert len(deps.force_close_calls) == 1

    def test_position_opens_during_approaching(self):
        """New position opens while in approaching-close state."""
        deps = MockDeps(minutes=4)
        mgr = deps.create()
        mgr.tick(_dt(13, 41), 0)
        assert mgr.state == CloseState.APPROACHING_CLOSE

        # Position opens on next tick
        mgr.tick(_dt(13, 41, 30), 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_1

    def test_emergency_tick_position_flat_between_ticks(self):
        """Position closes between on_market_tick calls."""
        deps = MockDeps(minutes=EMERGENCY_MINUTES)
        mgr = deps.create()
        mgr.tick(_dt(13, 44), 1)
        assert mgr.state == CloseState.EMERGENCY_TICK_SWEEP

        # First tick: still open
        mgr.on_market_tick(_dt(13, 44, 1), 1)
        assert mgr.state == CloseState.EMERGENCY_TICK_SWEEP

        # Second tick: now flat (strategy closed it)
        mgr.on_market_tick(_dt(13, 44, 2), 0)
        assert mgr.state == CloseState.SESSION_END_PENDING


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

class TestReset:

    def test_reset_clears_all_state(self):
        deps = MockDeps(minutes=3)
        mgr = _manager_at_force_close_1(deps)
        mgr.session_end_pending = True
        mgr._emergency_count = 5

        mgr.reset()
        assert mgr.state == CloseState.NORMAL
        assert mgr.session_end_pending is False
        assert mgr._attempt == 0
        assert mgr._attempt_sent_at is None
        assert mgr._emergency_count == 0

    def test_reset_emits_cleared_when_was_pending(self):
        deps = MockDeps(minutes=3)
        mgr = deps.create()
        mgr.session_end_pending = True
        mgr.reset()
        assert EVT_SESSION_END_PENDING_CLEARED in deps.event_names()

    def test_reset_no_event_when_not_pending(self):
        deps = MockDeps(minutes=60)
        mgr = deps.create()
        mgr.reset()
        assert EVT_SESSION_END_PENDING_CLEARED not in deps.event_names()


# ---------------------------------------------------------------------------
# Integration: full lifecycle
# ---------------------------------------------------------------------------

class TestFullLifecycle:

    def test_normal_through_session_end_and_back(self):
        """Full lifecycle: NORMAL -> APPROACHING -> FORCE_CLOSE -> SESSION_END -> NORMAL."""
        mins_seq = iter([
            60,   # normal
            4,    # approaching + force close (position open)
            4,    # waiting for fill (within 15s timeout)
            4,    # still waiting
            None, # market closed (position went flat)
            None, # still closed
            300,  # new session
        ])
        deps = MockDeps(minutes=lambda dt: next(mins_seq))
        mgr = deps.create()

        # Normal
        mgr.tick(_dt(12, 0), 0)
        assert mgr.state == CloseState.NORMAL

        # Approaching + force close (position open)
        t0 = _dt(13, 41, 0)
        mgr.tick(t0, 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_1

        # Waiting (5s later, within 15s timeout — no retry yet)
        mgr.tick(t0 + timedelta(seconds=5), 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_1

        # Still waiting (10s later, still within timeout)
        mgr.tick(t0 + timedelta(seconds=10), 1)
        assert mgr.state == CloseState.FORCE_CLOSE_ATTEMPT_1

        # Market closes, position was closed by strategy
        mgr.tick(t0 + timedelta(seconds=20), 0)
        assert mgr.state == CloseState.SESSION_END_PENDING

        # Still closed
        mgr.tick(_dt(14, 0), 0)
        assert mgr.state == CloseState.SESSION_END_PENDING

        # New session
        mgr.tick(_dt(15, 0), 0)
        assert mgr.state == CloseState.NORMAL
        assert mgr.session_end_pending is False
