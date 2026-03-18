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

    def test_last_tick_at_am_close_triggers_reconnect(self):
        """Tick at 13:44 (AM open), check at 15:01 → reconnect (>10min elapsed)."""
        wd = TickWatchdog()
        wd.active = True

        # Last tick at 13:44 (still AM session)
        am_dt = _taipei_dt(2026, 3, 17, 13, 44)
        wd.last_tick_time = _ts(am_dt)

        # Check at 15:01 — elapsed ~77min, last tick was during open market
        check_dt = _taipei_dt(2026, 3, 17, 15, 1)
        with _patch_now(check_dt):
            action = wd.check(now=_ts(check_dt))
        assert action == "reconnect"  # >10min elapsed


class TestSessionTransitionPMtoAM:
    """PM session closes 05:00, AM opens 08:45 next day."""

    def test_last_tick_before_close_triggers_reconnect(self):
        """Tick at 04:59 (PM open), check at 08:46 → reconnect (>3h elapsed)."""
        wd = TickWatchdog()
        wd.active = True

        # Last tick at 04:59 (PM session still open)
        pm_dt = _taipei_dt(2026, 3, 18, 4, 59)
        wd.last_tick_time = _ts(pm_dt)

        # Check at 08:46 — elapsed ~3h47m, last tick during open market
        check_dt = _taipei_dt(2026, 3, 18, 8, 46)
        with _patch_now(check_dt):
            action = wd.check(now=_ts(check_dt))
        assert action == "reconnect"

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
        """Last tick Friday 23:00, check Monday 08:46 → reconnect."""
        wd = TickWatchdog()
        wd.active = True

        # Friday night tick (market open)
        fri_dt = _taipei_dt(2026, 3, 20, 23, 0)  # Friday
        wd.last_tick_time = _ts(fri_dt)

        # Monday morning (AM open)
        mon_dt = _taipei_dt(2026, 3, 23, 8, 46)  # Monday
        with _patch_now(mon_dt):
            action = wd.check(now=_ts(mon_dt))
        assert action == "reconnect"  # >2 days elapsed, last tick was open market

    def test_saturday_morning_tick_monday(self):
        """Last tick Saturday 04:59 (Fri night carryover), check Monday 08:46."""
        wd = TickWatchdog()
        wd.active = True

        # Saturday 04:59 (market still open from Friday night)
        sat_dt = _taipei_dt(2026, 3, 21, 4, 59)  # Saturday
        wd.last_tick_time = _ts(sat_dt)

        mon_dt = _taipei_dt(2026, 3, 23, 8, 46)
        with _patch_now(mon_dt):
            action = wd.check(now=_ts(mon_dt))
        assert action == "reconnect"

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
        wd.reset()
        assert wd.active is False
        assert wd.last_tick_time == 0.0
        assert wd.grace_until == 0.0
