"""Tests for Issue #23: Resubscribe doesn't work over weekends.

Tests the tick watchdog session detection and reconnect scheduling logic
that are unit-testable without COM.
"""

import time
from unittest.mock import patch

import pytest

from src.live.tick_watchdog import TickWatchdog


class TestTickWatchdogSessionResubscribe:
    """Watchdog should detect session transitions and trigger resubscription."""

    def test_session_resubscribe_when_last_tick_during_closed_market(self):
        """If last tick was during closed market (e.g. Saturday), resubscribe immediately."""
        wd = TickWatchdog()
        wd.active = True
        # Simulate a tick received during market hours, then market closes
        wd.last_tick_time = time.time() - 7200  # 2 hours ago

        # Mock: market is now open, but last tick was during closed hours
        with patch("src.live.tick_watchdog.is_market_open") as mock_open, \
             patch("src.live.tick_watchdog.minutes_until_session_close", return_value=120):
            # Current time: market is open; last tick time: market was closed
            mock_open.side_effect = lambda dt=None: dt is None
            action = wd.check()

        assert action == "session_resubscribe"

    def test_no_action_during_grace_period(self):
        """During grace period after reconnect, watchdog should not trigger."""
        wd = TickWatchdog()
        wd.active = True
        wd.last_tick_time = time.time() - 600  # 10 min ago (stale)
        wd.set_grace(60)  # 60s grace

        with patch("src.live.tick_watchdog.is_market_open", return_value=True), \
             patch("src.live.tick_watchdog.minutes_until_session_close", return_value=120):
            action = wd.check()

        assert action is None

    def test_reconnect_after_long_staleness(self):
        """After 10+ minutes of no ticks during market hours, force reconnect."""
        wd = TickWatchdog()
        wd.active = True
        now = time.time()
        wd.last_tick_time = now - 700  # 11+ minutes ago

        with patch("src.live.tick_watchdog.is_market_open", return_value=True), \
             patch("src.live.tick_watchdog.minutes_until_session_close", return_value=120):
            action = wd.check(now=now)

        assert action == "reconnect"

    def test_warn_then_resubscribe_escalation(self):
        """Watchdog escalates: warn at 2min, resubscribe at 5min, reconnect at 10min."""
        wd = TickWatchdog()
        wd.active = True
        now = time.time()

        with patch("src.live.tick_watchdog.is_market_open", return_value=True), \
             patch("src.live.tick_watchdog.minutes_until_session_close", return_value=120):
            # 2.5 min stale -> warn
            wd.last_tick_time = now - 150
            assert wd.check(now=now) == "warn"

            # 6 min stale -> resubscribe
            wd.last_tick_time = now - 360
            assert wd.check(now=now) == "resubscribe"

            # 11 min stale -> reconnect
            wd.last_tick_time = now - 660
            assert wd.check(now=now) == "reconnect"

    def test_no_action_when_inactive(self):
        """Inactive watchdog should never trigger actions."""
        wd = TickWatchdog()
        wd.active = False
        wd.last_tick_time = time.time() - 3600  # 1 hour stale

        assert wd.check() is None

    def test_suppress_near_session_close(self):
        """Suppress warnings when near session close (thin volume is normal)."""
        wd = TickWatchdog()
        wd.active = True
        now = time.time()
        wd.last_tick_time = now - 400  # stale enough to trigger

        with patch("src.live.tick_watchdog.is_market_open", return_value=True), \
             patch("src.live.tick_watchdog.minutes_until_session_close", return_value=5):
            assert wd.check(now=now) is None
