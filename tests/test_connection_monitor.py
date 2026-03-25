"""Tests for ConnectionMonitor — reconnection state machine.

Verifies backoff sequences, max attempt exhaustion, market-hours deferral,
manual reconnect reset, and resubscribe retry logic.
"""

from __future__ import annotations

import pytest

from src.live.connection_monitor import ConnectionMonitor, ReconnectAction


class TestBackoffSequence:
    """Verify exponential backoff delays [5, 10, 20, 30, 60, 60, ...]."""

    def test_first_five_delays(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        expected = [5, 10, 20, 30, 60]
        for i, exp_delay in enumerate(expected):
            action = cm.schedule_next(
                has_live_runner=False, market_open=True, secs_until_open=0)
            assert action.type == "attempt", f"attempt {i}"
            assert action.delay_seconds == exp_delay, f"attempt {i}"
            assert action.attempt == i + 1

    def test_delay_caps_at_60(self):
        """After index 4, delay stays at 60s."""
        cm = ConnectionMonitor()
        cm.on_disconnected()
        # Burn through first 5
        for _ in range(5):
            cm.schedule_next(has_live_runner=False, market_open=True, secs_until_open=0)
        # 6th through 10th should all be 60s
        for i in range(5):
            action = cm.schedule_next(
                has_live_runner=False, market_open=True, secs_until_open=0)
            assert action.delay_seconds == 60, f"attempt {i + 6}"


class TestMaxAttempts:

    def test_give_up_after_10_without_live_runner(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        for _ in range(10):
            cm.schedule_next(has_live_runner=False, market_open=True, secs_until_open=0)
        action = cm.schedule_next(
            has_live_runner=False, market_open=True, secs_until_open=0)
        assert action.type == "give_up"
        assert not cm.is_active

    def test_defer_after_10_with_live_runner(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        for _ in range(10):
            cm.schedule_next(has_live_runner=True, market_open=True, secs_until_open=0)
        # With live runner and secs_until_open > 0, defers to market
        action = cm.schedule_next(
            has_live_runner=True, market_open=False, secs_until_open=7200)
        assert action.type == "defer_to_market"
        # Counter should reset for fresh cycle
        assert cm.attempt == 0

    def test_give_up_at_max_no_secs_until_open(self):
        """Max attempts, live runner, but secs_until_open=0 → give up."""
        cm = ConnectionMonitor()
        cm.on_disconnected()
        for _ in range(10):
            cm.schedule_next(has_live_runner=True, market_open=True, secs_until_open=0)
        action = cm.schedule_next(
            has_live_runner=True, market_open=True, secs_until_open=0)
        assert action.type == "give_up"


class TestMarketHoursDeferral:

    def test_defer_during_off_market_with_live_runner(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        action = cm.schedule_next(
            has_live_runner=True, market_open=False, secs_until_open=3600)
        assert action.type == "defer_to_market"
        # Delay = secs_until_open - 120 (2 min before), min 60
        assert action.delay_seconds == 3600 - 120

    def test_defer_minimum_60s(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        action = cm.schedule_next(
            has_live_runner=True, market_open=False, secs_until_open=150)
        # secs - 120 = 30, but min is 60
        assert action.delay_seconds == 60

    def test_no_defer_within_2min_of_open(self):
        """If secs_until_open <= 120, use normal backoff instead of deferring."""
        cm = ConnectionMonitor()
        cm.on_disconnected()
        action = cm.schedule_next(
            has_live_runner=True, market_open=False, secs_until_open=100)
        assert action.type == "attempt"  # normal backoff, not defer

    def test_no_defer_without_live_runner(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        action = cm.schedule_next(
            has_live_runner=False, market_open=False, secs_until_open=7200)
        assert action.type == "attempt"  # normal backoff

    def test_no_defer_during_market_hours(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        action = cm.schedule_next(
            has_live_runner=True, market_open=True, secs_until_open=0)
        assert action.type == "attempt"


class TestManualReconnect:

    def test_resets_attempt_counter(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        # Burn 5 attempts
        for _ in range(5):
            cm.schedule_next(has_live_runner=False, market_open=True, secs_until_open=0)
        assert cm.attempt == 5
        # Manual reconnect resets
        action = cm.on_manual_reconnect()
        assert action.type == "attempt_now"
        assert cm.attempt == 0
        assert cm.is_active

    def test_message(self):
        cm = ConnectionMonitor()
        action = cm.on_manual_reconnect()
        assert "手動" in action.message


class TestOnConnected:

    def test_resets_state(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        for _ in range(3):
            cm.schedule_next(has_live_runner=False, market_open=True, secs_until_open=0)
        assert cm.attempt == 3
        action = cm.on_connected()
        assert action.type == "connected"
        assert cm.attempt == 0
        assert not cm.is_active


class TestOnDisconnected:

    def test_sets_active(self):
        cm = ConnectionMonitor()
        action = cm.on_disconnected()
        assert action.type == "start_reconnect"
        assert cm.is_active
        assert cm.attempt == 0


class TestReset:

    def test_clears_state(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        cm.schedule_next(has_live_runner=False, market_open=True, secs_until_open=0)
        cm.reset()
        assert cm.attempt == 0
        assert not cm.is_active


class TestResubscribeRetry:

    def test_first_retry(self):
        cm = ConnectionMonitor()
        action = cm.should_retry_resubscribe(0)
        assert action is not None
        assert action.type == "resubscribe_retry"
        assert action.delay_seconds == 5
        assert action.attempt == 1

    def test_second_retry(self):
        cm = ConnectionMonitor()
        action = cm.should_retry_resubscribe(1)
        assert action is not None
        assert action.attempt == 2

    def test_last_retry(self):
        cm = ConnectionMonitor()
        action = cm.should_retry_resubscribe(2)
        assert action is not None
        assert action.attempt == 3

    def test_max_retries_exceeded(self):
        cm = ConnectionMonitor()
        action = cm.should_retry_resubscribe(3)
        assert action is None

    def test_message_contains_retry_info(self):
        cm = ConnectionMonitor()
        action = cm.should_retry_resubscribe(0)
        assert "1/3" in action.message


class TestMessageFormat:
    """Verify messages are bilingual (Chinese + English)."""

    def test_backoff_message(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        action = cm.schedule_next(
            has_live_runner=False, market_open=True, secs_until_open=0)
        assert "重連中" in action.message
        assert "Reconnecting" in action.message
        assert "5s" in action.message
        assert "1/10" in action.message

    def test_give_up_message(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        for _ in range(10):
            cm.schedule_next(has_live_runner=False, market_open=True, secs_until_open=0)
        action = cm.schedule_next(
            has_live_runner=False, market_open=True, secs_until_open=0)
        assert "失敗" in action.message

    def test_defer_message(self):
        cm = ConnectionMonitor()
        cm.on_disconnected()
        action = cm.schedule_next(
            has_live_runner=True, market_open=False, secs_until_open=7200)
        assert "休市中" in action.message
        assert "Market closed" in action.message
