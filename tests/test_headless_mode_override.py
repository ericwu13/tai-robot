"""mode_override.json must not escalate a headless bot to a live mode.

A refused file is renamed to mode_override.json.refused (or removed if
that fails). ``deploy --mode auto`` assigns ``trading_mode`` directly
and is not this path. Tests use tmp dirs only.
"""

import json
import logging
import os
import time
from datetime import datetime

import pytest

from src.backtest.broker import _mode_to_source
from src.backtest.strategy import BacktestStrategy
from src.live.live_runner import LiveRunner, arm_headless_file_guard
from src.market_data.models import Bar


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
    assert not os.path.exists(path + ".refused")
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
    assert not os.path.exists(path + ".refused")
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


def test_headless_refuses_missing_trading_mode(tmp_path, caplog):
    """A dict with no trading_mode is null, not paper. Refuse it."""
    runner = _runner(tmp_path, "paper")
    runner._headless = True
    path = os.path.join(runner.bot_dir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({}, f)

    with caplog.at_level(logging.WARNING, logger="src.live.live_runner"):
        assert _poll(runner) is None

    assert runner.trading_mode == "paper"
    assert not os.path.exists(path)
    assert any("headless refused" in r.getMessage() for r in caplog.records)


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


def test_arm_one_argument_sets_the_flag_on_a_real_runner(tmp_path):
    runner = _runner(tmp_path, "auto")
    assert runner._headless is False
    arm_headless_file_guard(runner)
    assert runner._headless is True
    assert runner.trading_mode == "auto"


def test_arm_tolerates_a_bare_object():
    """object() rejects new attributes. Arming must not raise."""
    arm_headless_file_guard(object())


def _bar(minute: int) -> Bar:
    return Bar(
        symbol="TX00",
        dt=datetime(2026, 6, 11, 9, minute),
        open=20000, high=20100, low=19900, close=20050,
        volume=100, interval=60,
    )


def _write_override(runner, payload) -> str:
    path = os.path.join(runner.bot_dir, "mode_override.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


@pytest.mark.parametrize(
    "payload",
    [
        {"trading_mode": ["auto"]},
        {"trading_mode": {"mode": "auto"}},
        {"trading_mode": None},
        {"trading_mode": 1},
        ["auto"],
        "auto",
    ],
    ids=["list", "dict", "null", "number", "top_array", "top_string"],
)
def test_unusable_override_is_refused_and_bar_is_kept(tmp_path, caplog, payload):
    runner = _runner(tmp_path, "paper")
    runner._headless = True
    runner.suppress_strategy = True
    path = _write_override(runner, payload)

    with caplog.at_level(logging.WARNING, logger="src.live.live_runner"):
        runner._process_aggregated_bar(_bar(0))

    assert runner.trading_mode == "paper"
    assert runner.broker.trade_source == "paper"
    assert len(runner.data_store) == 1
    assert len(runner._aggregated_bars) == 1
    assert not os.path.exists(path)
    assert os.path.isfile(path + ".refused")
    assert any("headless refused" in r.getMessage() for r in caplog.records)
    assert not any("check failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("mode", [" paper ", "PAPER"])
def test_non_exact_paper_is_refused(tmp_path, caplog, mode):
    runner = _runner(tmp_path, "auto")
    runner._headless = True
    runner.suppress_strategy = True
    path = _drop(runner, mode)

    with caplog.at_level(logging.WARNING, logger="src.live.live_runner"):
        runner._process_aggregated_bar(_bar(0))

    assert runner.trading_mode == "auto"
    assert runner.broker.trade_source == "real"
    assert len(runner.data_store) == 1
    assert not os.path.exists(path)
    assert any("headless refused" in r.getMessage() and repr(mode) in r.getMessage()
               for r in caplog.records)


def test_refused_file_remove_oserror_is_rate_limited(tmp_path, caplog, monkeypatch):
    runner = _runner(tmp_path, "paper")
    runner._headless = True
    runner.suppress_strategy = True
    _drop(runner, "auto")

    def _busy(*_a, **_k):
        raise OSError("busy")

    monkeypatch.setattr("src.live.live_runner.os.replace", _busy)
    monkeypatch.setattr("src.live.live_runner.os.remove", _busy)

    def _remove_warnings():
        return [r for r in caplog.records if "could not remove" in r.getMessage()]

    with caplog.at_level(logging.WARNING, logger="src.live.live_runner"):
        runner._process_aggregated_bar(_bar(0))
        runner._process_aggregated_bar(_bar(1))
        assert len(_remove_warnings()) == 1
        assert len(runner._aggregated_bars) == 2
        assert runner.trading_mode == "paper"
        runner._mode_override_remove_warn_at = time.monotonic() - 61
        runner._process_aggregated_bar(_bar(2))
        assert len(_remove_warnings()) == 2
        assert len(runner.data_store) == 3
