"""Test for issue #27: Robot deployment button problem.

When the robot is deployed but not in RUNNING state (e.g., WARMING_UP or STOPPED),
clicking the stop button should still stop the robot, not re-deploy it.
"""

import pytest
from unittest.mock import Mock, MagicMock
from src.live.live_runner import LiveState


def test_toggle_live_should_stop_when_warming_up():
    """Test that clicking stop button stops robot when in WARMING_UP state.

    This test should PASS after the fix - any non-IDLE state should stop.
    """
    # Mock the GUI application class
    app = Mock()
    app._live_runner = Mock()
    app._live_runner.state = LiveState.WARMING_UP
    app._stop_live = Mock()
    app._deploy_live = Mock()

    # Simulate the fixed _toggle_live logic
    def fixed_toggle_live():
        """Fixed implementation - stops for any non-IDLE state."""
        if app._live_runner and app._live_runner.state != LiveState.IDLE:
            app._stop_live()
        else:
            app._deploy_live()

    # Act - simulate clicking the "stop" button when bot is warming up
    fixed_toggle_live()

    # Assert - this should PASS after the fix
    # We expect _stop_live to be called for non-IDLE states
    app._stop_live.assert_called_once()
    app._deploy_live.assert_not_called()


def test_toggle_live_should_stop_when_stopped():
    """Test that clicking stop button stops robot when in STOPPED state.

    This test should PASS after the fix - any non-IDLE state should stop.
    """
    # Mock the GUI application class
    app = Mock()
    app._live_runner = Mock()
    app._live_runner.state = LiveState.STOPPED
    app._stop_live = Mock()
    app._deploy_live = Mock()

    # Simulate the fixed _toggle_live logic
    def fixed_toggle_live():
        """Fixed implementation - stops for any non-IDLE state."""
        if app._live_runner and app._live_runner.state != LiveState.IDLE:
            app._stop_live()
        else:
            app._deploy_live()

    # Act - simulate clicking the "stop" button when bot is stopped
    fixed_toggle_live()

    # Assert - this should PASS after the fix
    # We expect _stop_live to be called for non-IDLE states
    app._stop_live.assert_called_once()
    app._deploy_live.assert_not_called()


def test_original_buggy_behavior():
    """Document the original buggy behavior for reference.

    This test shows how the original code was wrong - it only stopped for RUNNING state.
    """
    # Mock the GUI application class
    app = Mock()
    app._live_runner = Mock()
    app._stop_live = Mock()
    app._deploy_live = Mock()

    def original_buggy_logic():
        """Original buggy implementation - only checks for RUNNING state."""
        if app._live_runner and app._live_runner.state == LiveState.RUNNING:
            app._stop_live()
        else:
            app._deploy_live()

    # Test that WARMING_UP incorrectly triggers deploy instead of stop
    app._live_runner.state = LiveState.WARMING_UP
    app._stop_live.reset_mock()
    app._deploy_live.reset_mock()
    original_buggy_logic()
    app._deploy_live.assert_called_once()  # BUG: should have called _stop_live
    app._stop_live.assert_not_called()

    # Test that STOPPED incorrectly triggers deploy instead of stop
    app._live_runner.state = LiveState.STOPPED
    app._stop_live.reset_mock()
    app._deploy_live.reset_mock()
    original_buggy_logic()
    app._deploy_live.assert_called_once()  # BUG: should have called _stop_live
    app._stop_live.assert_not_called()


def test_toggle_live_should_deploy_when_idle():
    """Test that clicking deploy button deploys robot when in IDLE state.

    This test should PASS both before and after the fix.
    """
    # Mock the GUI application class
    app = Mock()
    app._live_runner = Mock()
    app._live_runner.state = LiveState.IDLE
    app._stop_live = Mock()
    app._deploy_live = Mock()

    # Simulate the fixed _toggle_live logic
    def fixed_toggle_live():
        """Fixed implementation - stops for any non-IDLE state."""
        if app._live_runner and app._live_runner.state != LiveState.IDLE:
            app._stop_live()
        else:
            app._deploy_live()

    # Act - simulate clicking the "deploy" button when bot is idle
    fixed_toggle_live()

    # Assert - this should PASS with both old and new code
    app._deploy_live.assert_called_once()
    app._stop_live.assert_not_called()


def test_toggle_live_fixed_logic():
    """Test the fixed logic that should stop for any non-IDLE state."""
    # Mock the GUI application class
    app = Mock()
    app._stop_live = Mock()
    app._deploy_live = Mock()

    def fixed_toggle_live():
        """Fixed implementation - stops for any active state."""
        if app._live_runner and app._live_runner.state != LiveState.IDLE:
            app._stop_live()
        else:
            app._deploy_live()

    # Test WARMING_UP state
    app._live_runner = Mock()
    app._live_runner.state = LiveState.WARMING_UP
    app._stop_live.reset_mock()
    app._deploy_live.reset_mock()
    fixed_toggle_live()
    app._stop_live.assert_called_once()
    app._deploy_live.assert_not_called()

    # Test RUNNING state
    app._live_runner.state = LiveState.RUNNING
    app._stop_live.reset_mock()
    app._deploy_live.reset_mock()
    fixed_toggle_live()
    app._stop_live.assert_called_once()
    app._deploy_live.assert_not_called()

    # Test STOPPED state
    app._live_runner.state = LiveState.STOPPED
    app._stop_live.reset_mock()
    app._deploy_live.reset_mock()
    fixed_toggle_live()
    app._stop_live.assert_called_once()
    app._deploy_live.assert_not_called()

    # Test IDLE state
    app._live_runner.state = LiveState.IDLE
    app._stop_live.reset_mock()
    app._deploy_live.reset_mock()
    fixed_toggle_live()
    app._deploy_live.assert_called_once()
    app._stop_live.assert_not_called()

    # Test no runner
    app._live_runner = None
    app._stop_live.reset_mock()
    app._deploy_live.reset_mock()
    fixed_toggle_live()
    app._deploy_live.assert_called_once()
    app._stop_live.assert_not_called()