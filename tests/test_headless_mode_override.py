"""mode_override.json must not escalate a headless bot to a live mode.

The file is consumed (removed) either way, matching the GUI hot-swap.
``deploy --mode auto`` assigns ``trading_mode`` directly and is not this
path. Tests use tmp dirs only.
"""

import json
import logging
import os

import pytest

from src.backtest.broker import _mode_to_source
from src.backtest.strategy import BacktestStrategy
from src.live.live_runner import LiveRunner, arm_headless_file_guard


class _NeverTrade(BacktestStrategy):
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        pass

    def required_bars(self):
        return 2


def _runner(tmp_path, mode="paper"):
    runner = LiveRunner(_NeverTrade(), "TX00", log_dir=str(tmp_path))
    runner.trading_mode = mode
    runner.broker.trade_source = _mode_to_source(mode)
    return runner


def _drop(runner, mode):
    path = os.path.join(runner.bot_dir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"trading_mode": mode}, f)
    return path


def _poll(runner):
    """Same two calls as LiveRunner._process_aggregated_bar."""
    new_mode = runner._check_mode_override()
    if new_mode:
        runner._apply_mode_switch(new_mode)
    return new_mode


@pytest.mark.parametrize("mode", ["auto", "real", "semi_auto", "full_auto"])
@pytest.mark.parametrize("allow_override", [False, True])
def test_headless_refuses_live_file_mode(tmp_path, caplog, mode, allow_override):
    runner = _runner(tmp_path, "paper")
    runner._headless = True
    runner._allow_live_override = allow_override
    statuses = []
    runner.on("on_status", lambda msg: statuses.append(msg))
    path = _drop(runner, mode)

    with caplog.at_level(logging.WARNING, logger="src.live.live_runner"):
        assert _poll(runner) is None
        warned = len(caplog.records)
        assert _poll(runner) is None

    assert runner.trading_mode == "paper"
    assert runner.broker.trade_source == "paper"
    assert not os.path.exists(path)
    assert warned == 1
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "headless refused" in message
    assert repr(mode) in message
    assert any("headless refused" in s and repr(mode) in s for s in statuses)


def test_headless_paper_file_still_applies(tmp_path, caplog):
    runner = _runner(tmp_path, "auto")
    arm_headless_file_guard(runner, app_headless=True, origin="cli")
    assert runner._headless is True
    assert runner.trading_mode == "auto"
    assert runner.broker.trade_source == "real"
    path = _drop(runner, "paper")

    with caplog.at_level(logging.WARNING, logger="src.live.live_runner"):
        assert _poll(runner) == "paper"

    assert runner.trading_mode == "paper"
    assert runner.broker.trade_source == "paper"
    assert not os.path.exists(path)
    assert caplog.records == []


def test_gui_file_override_still_allows_auto(tmp_path, caplog):
    runner = _runner(tmp_path, "paper")
    arm_headless_file_guard(runner, app_headless=False, origin="gui")
    assert runner._headless is False
    runner._allow_live_override = True
    path = _drop(runner, "auto")

    with caplog.at_level(logging.WARNING, logger="src.live.live_runner"):
        assert _poll(runner) == "auto"

    assert runner.trading_mode == "auto"
    assert runner.broker.trade_source == "real"
    assert not os.path.exists(path)
    assert caplog.records == []


def test_gui_file_auto_still_needs_allow_live_override(tmp_path):
    runner = _runner(tmp_path, "paper")
    arm_headless_file_guard(runner, app_headless=False, origin="gui")
    runner._allow_live_override = False
    statuses = []
    runner.on("on_status", lambda msg: statuses.append(msg))
    _drop(runner, "auto")

    assert _poll(runner) == "auto"
    assert runner.trading_mode == "paper"
    assert any("allow_live_override" in s for s in statuses)


def test_headless_ignores_file_with_no_mode(tmp_path, caplog):
    runner = _runner(tmp_path, "paper")
    runner._headless = True
    path = os.path.join(runner.bot_dir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({}, f)

    with caplog.at_level(logging.WARNING, logger="src.live.live_runner"):
        assert _poll(runner) is None

    assert runner.trading_mode == "paper"
    assert not os.path.exists(path)
    assert caplog.records == []


@pytest.mark.parametrize(
    "app_headless,origin",
    [
        (True, "gui"),
        (True, "cli"),
        (False, "cli"),
        (False, None),
        (False, ""),
        (False, "headless"),
    ],
)
def test_arm_fails_closed_without_changing_deploy_mode(
        tmp_path, app_headless, origin):
    runner = _runner(tmp_path, "auto")
    arm_headless_file_guard(
        runner, app_headless=app_headless, origin=origin)
    assert runner._headless is True
    assert runner.trading_mode == "auto"
    assert runner.broker.trade_source == "real"


def test_arm_leaves_an_attended_gui_unguarded(tmp_path):
    runner = _runner(tmp_path, "auto")
    arm_headless_file_guard(runner, app_headless=False, origin="gui")
    assert runner._headless is False
    assert runner.trading_mode == "auto"
