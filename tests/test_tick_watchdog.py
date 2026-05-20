"""Tests for TickWatchdog — tick health monitoring and session transitions.

Tests the actual TickWatchdog.check() method used by _check_tick_watchdog
in run_backtest.py. Covers all session transitions:
- AM → PM (13:45 gap → 15:00)
- PM → AM (05:00 gap → 08:45)
- Friday PM → Monday AM (weekend)
- Normal staleness (warn, resubscribe, reconnect)
- Near-session-close suppression
- Grace period after reconnect
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from src.live.live_runner import _TZ_TAIPEI
from src.live.tick_watchdog import TickWatchdog


def _taipei_dt(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=_TZ_TAIPEI)


def _ts(dt: datetime) -> float:
    """Convert Taipei datetime to Unix timestamp."""
    return dt.timestamp()


def _patch_now(dt):
    """Patch _taipei_now used by is_market_open and minutes_until_session_close."""
    return patch("src.live.live_runner._taipei_now", return_value=dt)


class TestSessionTransitionAMtoPM:
    """AM session closes 13:45, PM opens 15:00. Bot deployed during gap."""

    def test_last_tick_during_gap_triggers_session_resubscribe(self):
        """Tick at 14:08 (gap), check at 15:01 (PM open) → session_resubscribe."""
        wd = TickWatchdog()
        wd.active = True

        # Last tick at 14:08 (off-market gap)
        gap_dt = _taipei_dt(2026, 3, 17, 14, 8)
        wd.last_tick_time = _ts(gap_dt)

        # Check at 15:01 (PM session open)
        check_dt = _taipei_dt(2026, 3, 17, 15, 1)
        with _patch_now(check_dt):
            action = wd.check(now=_ts(check_dt))
        assert action == "session_resubscribe"

    def test_last_tick_at_am_close_triggers_session_resubscribe(self):
        """Tick at 13:44 (AM open), check at 15:01 → session_resubscribe.

        Issue #66 (bot 0422 case): even when last tick was in the prior
        session (not in the gap itself), the elapsed measurement spans the
        13:45-15:00 closed gap, so the 77-min reading is a measurement
        artifact. Prefer a soft resubscribe over a heavy reconnect; if
        ticks really don't arrive, normal escalation kicks in afterward.
        """
        wd = TickWatchdog()
        wd.active = True

        # Last tick at 13:44 (still AM session)
        am_dt = _taipei_dt(2026, 3, 17, 13, 44)
        wd.last_tick_time = _ts(am_dt)

        # Check at 15:01 — elapsed ~77min, but spans the closed gap
        check_dt = _taipei_dt(2026, 3, 17, 15, 1)
        with _patch_now(check_dt):
            action = wd.check(now=_ts(check_dt))
        assert action == "session_resubscribe"


class TestSessionTransitionPMtoAM:
    """PM session closes 05:00, AM opens 08:45 next day."""

    def test_last_tick_before_close_triggers_session_resubscribe(self):
        """Tick at 04:59 (PM open), check at 08:46 → session_resubscribe.

        Last tick was just before the 05:00 night close; check at next
        AM open. Elapsed (~3h47m) spans the 05:00-08:45 closed gap, so
        it's a session boundary not a same-session zombie.
        """
        wd = TickWatchdog()
        wd.active = True

        # Last tick at 04:59 (PM session still open)
        pm_dt = _taipei_dt(2026, 3, 18, 4, 59)
        wd.last_tick_time = _ts(pm_dt)

        # Check at 08:46 — gap spans 05:00-08:45 closed period
        check_dt = _taipei_dt(2026, 3, 18, 8, 46)
        with _patch_now(check_dt):
            action = wd.check(now=_ts(check_dt))
        assert action == "session_resubscribe"

    def test_last_tick_at_close_triggers_session_resubscribe(self):
        """Tick at 05:01 (market closed), check at 08:46 → session_resubscribe."""
        wd = TickWatchdog()
        wd.active = True

        # Last tick at 05:01 (market already closed)
        closed_dt = _taipei_dt(2026, 3, 18, 5, 1)
        wd.last_tick_time = _ts(closed_dt)

        check_dt = _taipei_dt(2026, 3, 18, 8, 46)
        with _patch_now(check_dt):
            action = wd.check(now=_ts(check_dt))
        assert action == "session_resubscribe"


class TestWeekendTransition:
    """Friday PM → Saturday 05:00 close → Monday AM 08:45 open."""

    def test_friday_night_tick_monday_morning(self):
        """Last tick Friday 23:00, check Monday 08:46 → session_resubscribe.

        Multi-day gap is still a session boundary — start with the soft
        action; if the COM session is actually dead, the resubscribe will
        fail (returning an error from RequestTicks) or no ticks will
        arrive, and the normal escalation reaches reconnect within minutes.
        """
        wd = TickWatchdog()
        wd.active = True

        fri_dt = _taipei_dt(2026, 3, 20, 23, 0)  # Friday
        wd.last_tick_time = _ts(fri_dt)

        mon_dt = _taipei_dt(2026, 3, 23, 8, 46)  # Monday
        with _patch_now(mon_dt):
            action = wd.check(now=_ts(mon_dt))
        assert action == "session_resubscribe"

    def test_saturday_morning_tick_monday(self):
        """Last tick Saturday 04:59 (Fri night carryover), check Monday 08:46."""
        wd = TickWatchdog()
        wd.active = True

        sat_dt = _taipei_dt(2026, 3, 21, 4, 59)  # Saturday
        wd.last_tick_time = _ts(sat_dt)

        mon_dt = _taipei_dt(2026, 3, 23, 8, 46)
        with _patch_now(mon_dt):
            action = wd.check(now=_ts(mon_dt))
        assert action == "session_resubscribe"

    def test_saturday_after_close_monday(self):
        """Last tick Saturday 06:00 (closed), check Monday 08:46 → session_resubscribe."""
        wd = TickWatchdog()
        wd.active = True

        # Saturday after market close
        sat_dt = _taipei_dt(2026, 3, 21, 6, 0)
        wd.last_tick_time = _ts(sat_dt)

        mon_dt = _taipei_dt(2026, 3, 23, 8, 46)
        with _patch_now(mon_dt):
            action = wd.check(now=_ts(mon_dt))
        assert action == "session_resubscribe"


class TestBot0422FalseReconnect:
    """Issue #66: bot 0422 case.

    Last real tick at 13:28 (AM session), no more ticks (settlement day or
    cash-market early close), watchdog warns 13:32-13:34, suppressed
    13:35-13:45, market closes 13:45, reopens 15:00 → watchdog measures
    90 min elapsed and force-reconnects.

    The 90-min reading is a measurement artifact across the closed gap,
    not a zombie session. The fix should treat this as a session boundary
    (soft resubscribe), not escalate straight to reconnect.
    """

    def test_first_pm_check_resubscribes_not_reconnects(self):
        """At 15:00:22 first check after PM open, action must be
        session_resubscribe (not the buggy 90m force-reconnect)."""
        wd = TickWatchdog()
        wd.active = True

        last_tick = _taipei_dt(2026, 5, 20, 13, 28)
        wd.last_tick_time = _ts(last_tick)

        check_dt = _taipei_dt(2026, 5, 20, 15, 0)
        with _patch_now(check_dt):
            action = wd.check(now=_ts(check_dt) + 22)  # 15:00:22
        assert action == "session_resubscribe"

    def test_no_reconnect_loop_after_handler(self):
        """After handler runs on_session_resubscribe, subsequent checks
        must NOT keep firing — neither session_resubscribe nor reconnect."""
        wd = TickWatchdog()
        wd.active = True

        last_tick = _taipei_dt(2026, 5, 20, 13, 28)
        wd.last_tick_time = _ts(last_tick)

        check_dt = _taipei_dt(2026, 5, 20, 15, 0)
        first = _ts(check_dt) + 22
        with _patch_now(check_dt):
            assert wd.check(now=first) == "session_resubscribe"

        # Handler runs at 15:00:22
        with patch("src.live.tick_watchdog.time.time", return_value=first):
            wd.set_grace(30)
            wd.on_session_resubscribe()

        # 30s later, grace expired — must be quiet, NOT reconnect
        second = first + 30
        second_dt = datetime.fromtimestamp(second, tz=_TZ_TAIPEI)
        with _patch_now(second_dt):
            action2 = wd.check(now=second)
        assert action2 is None

        # Another minute — still quiet
        third = first + 90
        third_dt = datetime.fromtimestamp(third, tz=_TZ_TAIPEI)
        with _patch_now(third_dt):
            assert wd.check(now=third) is None


class TestNormalStaleness:
    """Normal tick staleness during an active session."""

    def _setup(self, elapsed_seconds: int):
        """Create watchdog with last tick `elapsed_seconds` ago."""
        wd = TickWatchdog()
        wd.active = True
        now_dt = _taipei_dt(2026, 3, 17, 16, 0)  # PM session
        now = _ts(now_dt)
        wd.last_tick_time = now - elapsed_seconds
        return wd, now, now_dt

    def test_fresh_ticks_no_action(self):
        wd, now, dt = self._setup(30)
        with _patch_now(dt):
            assert wd.check(now=now) is None

    def test_2min_warn(self):
        wd, now, dt = self._setup(130)
        with _patch_now(dt):
            assert wd.check(now=now) == "warn"

    def test_5min_resubscribe(self):
        wd, now, dt = self._setup(310)
        with _patch_now(dt):
            assert wd.check(now=now) == "resubscribe"

    def test_10min_reconnect(self):
        wd, now, dt = self._setup(610)
        with _patch_now(dt):
            assert wd.check(now=now) == "reconnect"

    def test_at_threshold_no_action(self):
        """Exactly at 120s should NOT warn (must exceed)."""
        wd, now, dt = self._setup(120)
        with _patch_now(dt):
            assert wd.check(now=now) is None


class TestNearCloseSuppress:
    """Suppress warnings within 10 minutes of session close."""

    def test_am_near_close_suppressed(self):
        """13:36 = 9 min before AM close → suppressed."""
        wd = TickWatchdog()
        wd.active = True
        check_dt = _taipei_dt(2026, 3, 17, 13, 36)
        wd.last_tick_time = _ts(check_dt) - 200  # 3+ min stale
        with _patch_now(check_dt):
            assert wd.check(now=_ts(check_dt)) is None

    def test_am_before_suppress_window(self):
        """13:30 = 15 min before close → NOT suppressed."""
        wd = TickWatchdog()
        wd.active = True
        check_dt = _taipei_dt(2026, 3, 17, 13, 30)
        wd.last_tick_time = _ts(check_dt) - 200
        with _patch_now(check_dt):
            assert wd.check(now=_ts(check_dt)) == "warn"

    def test_night_near_close_suppressed(self):
        """04:52 = 8 min before night close (05:00) → suppressed."""
        wd = TickWatchdog()
        wd.active = True
        check_dt = _taipei_dt(2026, 3, 18, 4, 52)
        wd.last_tick_time = _ts(check_dt) - 200
        with _patch_now(check_dt):
            assert wd.check(now=_ts(check_dt)) is None


class TestGracePeriod:
    """Grace period after reconnect/resubscribe."""

    def test_during_grace_no_action(self):
        wd = TickWatchdog()
        wd.active = True
        check_dt = _taipei_dt(2026, 3, 17, 16, 0)
        now = _ts(check_dt)
        wd.last_tick_time = now - 300  # 5 min stale
        wd.grace_until = now + 10  # grace for 10 more seconds
        with _patch_now(check_dt):
            assert wd.check(now=now) is None

    def test_after_grace_resumes(self):
        wd = TickWatchdog()
        wd.active = True
        check_dt = _taipei_dt(2026, 3, 17, 16, 0)
        now = _ts(check_dt)
        wd.last_tick_time = now - 310  # 5+ min stale (exceeds 300s threshold)
        wd.grace_until = now - 1  # grace expired
        with _patch_now(check_dt):
            assert wd.check(now=now) == "resubscribe"


class TestInactive:
    """Watchdog should do nothing when inactive or no ticks."""

    def test_inactive(self):
        wd = TickWatchdog()
        wd.active = False
        assert wd.check() is None

    def test_no_ticks(self):
        wd = TickWatchdog()
        wd.active = True
        wd.last_tick_time = 0.0
        assert wd.check() is None

    def test_market_closed(self):
        wd = TickWatchdog()
        wd.active = True
        wd.last_tick_time = time.time() - 300
        # Sunday — market closed
        with _patch_now(_taipei_dt(2026, 3, 22, 12, 0)):
            assert wd.check() is None


class TestReset:

    def test_reset_clears_state(self):
        wd = TickWatchdog()
        wd.active = True
        wd.last_tick_time = time.time()
        wd.grace_until = time.time() + 30
        wd.last_resubscribe = time.time()
        wd.reset()
        assert wd.active is False
        assert wd.last_tick_time == 0.0
        assert wd.grace_until == 0.0
        assert wd.last_resubscribe == 0.0


class TestResubscribeCooldown:
    """Regression: zombie COM session scenario from test123 bot.

    Before the fix, _resubscribe_ticks called on_tick() which reset
    last_tick_time, so elapsed never reached RECONNECT_TIMEOUT and the
    bot loop-resubscribed every 5 minutes forever. The fix:
      1. run_backtest._resubscribe_ticks calls on_resubscribe() (not on_tick())
      2. check() suppresses repeated "resubscribe" within the cooldown window
      3. reconnect still fires unconditionally at RECONNECT_TIMEOUT
    """

    def test_on_resubscribe_does_not_reset_last_tick_time(self):
        wd = TickWatchdog()
        original = time.time() - 400  # 400s ago
        wd.last_tick_time = original
        wd.on_resubscribe()
        # last_tick_time unchanged — the quote server may not actually send
        # ticks back, so elapsed must keep climbing.
        assert wd.last_tick_time == original
        assert wd.last_resubscribe > 0

    def test_on_tick_resets_resubscribe_cooldown(self):
        """A real tick means the connection is healthy — clear cooldown."""
        wd = TickWatchdog()
        wd.last_resubscribe = time.time() - 60
        wd.on_tick()
        assert wd.last_resubscribe == 0.0

    def test_cooldown_suppresses_resubscribe_within_window(self):
        """After resubscribe, check() should NOT return resubscribe
        again within RESUBSCRIBE_COOLDOWN — must return 'warn' instead
        so elapsed keeps climbing to reconnect threshold."""
        wd = TickWatchdog()
        wd.active = True
        # Market is open — use a weekday AM time
        fake_now_dt = _taipei_dt(2026, 3, 17, 10, 30)  # Tue 10:30 AM
        # Last tick 350s ago (> RESUBSCRIBE 300 but < RECONNECT 600)
        wd.last_tick_time = _ts(fake_now_dt) - 350
        wd.last_resubscribe = _ts(fake_now_dt) - 60  # resubscribed 1 min ago (within 3-min cooldown)

        with _patch_now(fake_now_dt):
            action = wd.check(now=_ts(fake_now_dt))
        assert action == "warn"  # suppressed by cooldown

    def test_cooldown_expires_allows_new_resubscribe(self):
        """After cooldown expires, resubscribe can fire again."""
        wd = TickWatchdog()
        wd.active = True
        fake_now_dt = _taipei_dt(2026, 3, 17, 10, 30)
        wd.last_tick_time = _ts(fake_now_dt) - 400  # stale
        wd.last_resubscribe = _ts(fake_now_dt) - 200  # cooldown expired (> 180s)

        with _patch_now(fake_now_dt):
            action = wd.check(now=_ts(fake_now_dt))
        assert action == "resubscribe"

    def test_reconnect_fires_even_during_cooldown(self):
        """Reconnect (>10min elapsed) must NOT be suppressed by resubscribe
        cooldown — the whole point of the fix is to let elapsed climb to
        RECONNECT_TIMEOUT without endless resubscribes stopping it."""
        wd = TickWatchdog()
        wd.active = True
        fake_now_dt = _taipei_dt(2026, 3, 17, 10, 30)
        wd.last_tick_time = _ts(fake_now_dt) - 700  # > RECONNECT_TIMEOUT (600)
        wd.last_resubscribe = _ts(fake_now_dt) - 60  # in cooldown

        with _patch_now(fake_now_dt):
            action = wd.check(now=_ts(fake_now_dt))
        assert action == "reconnect"

    def test_on_session_resubscribe_advances_last_tick_time(self):
        """on_session_resubscribe must reset last_tick_time so the
        session_resubscribe trigger condition clears (issue #66)."""
        wd = TickWatchdog()
        # last tick from a closed-market wall-clock time
        gap_dt = _taipei_dt(2026, 5, 19, 14, 50)  # AM/PM gap
        wd.last_tick_time = _ts(gap_dt)
        before_resub = wd.last_tick_time

        wd.on_session_resubscribe()

        assert wd.last_tick_time > before_resub
        assert wd.last_resubscribe > 0
        # last_tick_time should be approximately wall-clock now
        assert abs(wd.last_tick_time - time.time()) < 1.0

    def test_session_resubscribe_does_not_loop_after_handler_call(self):
        """Issue #66 regression: subscribe during 13:45-15:00 gap, then
        session_resubscribe fires once at 15:00. After the handler runs
        on_session_resubscribe(), subsequent checks must NOT keep firing
        session_resubscribe at every 30s watchdog tick."""
        wd = TickWatchdog()
        wd.active = True

        # Subscription at 14:47:50 sets last_tick_time during the gap
        sub_dt = _taipei_dt(2026, 5, 19, 14, 47)
        wd.last_tick_time = _ts(sub_dt)

        # First check at 15:00:24 — fires session_resubscribe
        check1 = _taipei_dt(2026, 5, 19, 15, 0)
        with _patch_now(check1):
            action1 = wd.check(now=_ts(check1) + 24)
        assert action1 == "session_resubscribe"

        # Handler runs: _resubscribe_ticks (sets grace=30) then
        # on_session_resubscribe (advances last_tick_time to now)
        with patch("src.live.tick_watchdog.time.time",
                   return_value=_ts(check1) + 24):
            wd.set_grace(30)
            wd.on_session_resubscribe()

        # Next check 30s later — grace expired, but last_tick_time is now
        # fresh, so no session_resubscribe loop
        check2 = _ts(check1) + 54
        check2_dt = datetime.fromtimestamp(check2, tz=_TZ_TAIPEI)
        with _patch_now(check2_dt):
            action2 = wd.check(now=check2)
        assert action2 is None  # Loop is broken

        # Check several more cycles to confirm no loop
        for offset in (84, 114, 144):
            t = _ts(check1) + offset
            t_dt = datetime.fromtimestamp(t, tz=_TZ_TAIPEI)
            with _patch_now(t_dt):
                assert wd.check(now=t) is None

    def test_session_resubscribe_zombie_escalates_to_warn_then_reconnect(self):
        """If after a session_resubscribe ticks STILL don't arrive (true
        zombie session), the watchdog must escalate via normal staleness
        ladder rather than silently swallowing the problem."""
        wd = TickWatchdog()
        wd.active = True

        # Subscription during gap → session_resubscribe at PM open
        sub_dt = _taipei_dt(2026, 5, 19, 14, 47)
        wd.last_tick_time = _ts(sub_dt)
        check1 = _taipei_dt(2026, 5, 19, 15, 0)
        with _patch_now(check1):
            assert wd.check(now=_ts(check1) + 24) == "session_resubscribe"

        # Handler does on_session_resubscribe at 15:00:24
        handler_t = _ts(check1) + 24
        with patch("src.live.tick_watchdog.time.time",
                   return_value=handler_t):
            wd.on_session_resubscribe()
            # No grace this time — testing escalation purely on elapsed
            wd.grace_until = 0

        # 3 min later — should warn (no ticks ever arrived)
        t_warn = handler_t + 150  # > WARN_TIMEOUT (120)
        t_warn_dt = datetime.fromtimestamp(t_warn, tz=_TZ_TAIPEI)
        with _patch_now(t_warn_dt):
            assert wd.check(now=t_warn) == "warn"

        # 11 min later — should reconnect
        t_reconnect = handler_t + 700  # > RECONNECT_TIMEOUT (600)
        t_reconnect_dt = datetime.fromtimestamp(t_reconnect, tz=_TZ_TAIPEI)
        with _patch_now(t_reconnect_dt):
            assert wd.check(now=t_reconnect) == "reconnect"

    def test_session_resubscribe_recovers_when_real_ticks_arrive(self):
        """After session_resubscribe, if real ticks do arrive, the
        watchdog should remain quiet — on_tick() resets everything."""
        wd = TickWatchdog()
        wd.active = True

        sub_dt = _taipei_dt(2026, 5, 19, 14, 47)
        wd.last_tick_time = _ts(sub_dt)
        check1 = _taipei_dt(2026, 5, 19, 15, 0)
        with _patch_now(check1):
            assert wd.check(now=_ts(check1) + 24) == "session_resubscribe"

        handler_t = _ts(check1) + 24
        with patch("src.live.tick_watchdog.time.time",
                   return_value=handler_t):
            wd.on_session_resubscribe()

        # A real tick at 15:00:30
        tick_t = handler_t + 6
        with patch("src.live.tick_watchdog.time.time",
                   return_value=tick_t):
            wd.on_tick()
        assert wd.last_resubscribe == 0.0  # cleared by on_tick

        # Subsequent checks are quiet
        for offset in (30, 60, 90, 120):
            t = tick_t + offset
            t_dt = datetime.fromtimestamp(t, tz=_TZ_TAIPEI)
            with _patch_now(t_dt):
                # last_tick_time = tick_t, elapsed = offset
                # All under WARN_TIMEOUT
                assert wd.check(now=t) is None

    def test_zombie_session_eventually_escalates_to_reconnect(self):
        """End-to-end zombie scenario: tick stops at T=0, resubscribes at
        T=300 (fails silently), by T=700 we should reconnect."""
        wd = TickWatchdog()
        wd.active = True
        fake_now_dt = _taipei_dt(2026, 3, 17, 10, 30)
        base = _ts(fake_now_dt) - 700

        # Real last tick at T=0 (700s before "now")
        wd.last_tick_time = base
        # Resubscribe fired at T=300 (400s before now) — this replicates
        # the old buggy on_tick() call happening here would have reset
        # last_tick_time to T=300. With the fix, only last_resubscribe
        # moves.
        wd.last_resubscribe = base + 300

        with _patch_now(fake_now_dt):
            action = wd.check(now=_ts(fake_now_dt))
        # elapsed = 700 > RECONNECT_TIMEOUT (600) → reconnect
        assert action == "reconnect"
