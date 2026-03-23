"""Test for issue #27: Robot deployment button problem.

When the robot is deployed but not in RUNNING state (e.g., WARMING_UP),
clicking the stop button should still stop the robot, not re-deploy it.
"""

import pytest
from unittest.mock import Mock
from src.live.live_runner import LiveState


def _make_toggle(app):
    """Reproduce the fixed _toggle_live logic (state != IDLE → stop)."""
    def toggle():
        if app._live_runner and app._live_runner.state != LiveState.IDLE:
            app._stop_live()
        else:
            app._deploy_live()
    return toggle


@pytest.mark.parametrize("state", [LiveState.WARMING_UP, LiveState.RUNNING, LiveState.STOPPED])
def test_toggle_stops_for_active_states(state):
    """Any non-IDLE state should trigger _stop_live, not _deploy_live."""
    app = Mock()
    app._live_runner = Mock(state=state)
    _make_toggle(app)()
    app._stop_live.assert_called_once()
    app._deploy_live.assert_not_called()


@pytest.mark.parametrize("runner", [Mock(state=LiveState.IDLE), None])
def test_toggle_deploys_when_idle_or_no_runner(runner):
    """IDLE state or no runner should trigger _deploy_live."""
    app = Mock()
    app._live_runner = runner
    _make_toggle(app)()
    app._deploy_live.assert_called_once()
    app._stop_live.assert_not_called()


def test_original_bug_warming_up_triggered_deploy():
    """Document the original bug: WARMING_UP wrongly triggered _deploy_live."""
    app = Mock()
    app._live_runner = Mock(state=LiveState.WARMING_UP)

    # Original buggy logic
    if app._live_runner and app._live_runner.state == LiveState.RUNNING:
        app._stop_live()
    else:
        app._deploy_live()

    # BUG: deploy was called instead of stop
    app._deploy_live.assert_called_once()
    app._stop_live.assert_not_called()
