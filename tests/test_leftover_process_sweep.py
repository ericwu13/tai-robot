"""`.exiting` is written when the lock is released and cleared only by the watchdog."""

from __future__ import annotations

import inspect
import json
import os

import pytest

from src.live.leftover_sweep import (
    EXITING_ALERT_AFTER_S,
    EXITING_FILENAME,
    EXITING_SCAN_INTERVAL_S,
    check_exiting_markers,
    write_exiting_marker,
)
from src.live.live_runner import LiveRunner
from tests.test_live_runner import NeverTradeStrategy

_CREATED = 100.0


def _layout(tmp_path):
    root = tmp_path / "live"
    watcher = root / "watcher"
    watcher.mkdir(parents=True)
    return root, str(watcher)


def _plant(root, name, *, pid=4242, created=_CREATED, written_at=1_000.0,
           alerted=False):
    bot = root / name
    bot.mkdir()
    path = bot / EXITING_FILENAME
    path.write_text(json.dumps({
        "pid": pid, "created_at": created, "written_at": written_at,
        "alerted": alerted,
    }), encoding="utf-8")
    return str(path)


def _check(watcher, now, *, alive, creation=None, alerts=None):
    check_exiting_markers(
        watcher, alert_fn=None if alerts is None else alerts.append,
        now=lambda: now,
        alive=alive,
        creation_of=(lambda _pid: _CREATED) if creation is None else creation,
    )


def test_dead_pid_removes_the_marker_without_an_alert(tmp_path):
    root, watcher = _layout(tmp_path)
    path = _plant(root, "TMF00_night")
    alerts = []
    _check(watcher, 2_000.0, alive=lambda _pid: False, alerts=alerts)
    assert alerts == []
    assert not os.path.isfile(path)


def test_reused_pid_removes_the_marker_without_an_alert(tmp_path):
    root, watcher = _layout(tmp_path)
    path = _plant(root, "TMF00_night", created=100.0)
    alerts = []
    _check(watcher, 2_000.0, alive=lambda _pid: True,
           creation=lambda _pid: 999.0, alerts=alerts)
    assert alerts == []
    assert not os.path.isfile(path)


def test_alive_past_60s_alerts_once(tmp_path):
    root, watcher = _layout(tmp_path)
    path = _plant(root, "TMF00_night", written_at=1_000.0)
    alerts = []
    when = 1_000.0 + EXITING_ALERT_AFTER_S + 1
    _check(watcher, when, alive=lambda _pid: True, alerts=alerts)
    assert len(alerts) == 1
    assert "pid=4242" in alerts[0]
    assert "TMF00_night" in alerts[0]
    assert "age=" in alerts[0]
    saved = json.loads(open(path, encoding="utf-8").read())
    assert saved["alerted"] is True
    _check(watcher, when + 2, alive=lambda _pid: True, alerts=alerts)
    _check(watcher, when + EXITING_SCAN_INTERVAL_S + 1,
           alive=lambda _pid: True, alerts=alerts)
    assert len(alerts) == 1
    assert os.path.isfile(path)


def test_alive_under_60s_does_not_alert(tmp_path):
    root, watcher = _layout(tmp_path)
    path = _plant(root, "TMF00_night", written_at=1_000.0)
    alerts = []
    _check(watcher, 1_000.0 + EXITING_ALERT_AFTER_S - 1,
           alive=lambda _pid: True, alerts=alerts)
    assert alerts == []
    assert os.path.isfile(path)


def test_corrupt_marker_is_deleted_with_one_log(tmp_path, caplog):
    root, watcher = _layout(tmp_path)
    path = _plant(root, "TMF00_night")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    alerts = []
    with caplog.at_level("WARNING"):
        _check(watcher, 2_000.0, alive=lambda _pid: True, alerts=alerts)
    assert alerts == []
    assert not os.path.isfile(path)
    assert sum("unreadable" in rec.message for rec in caplog.records) == 1


def test_release_lock_writes_exiting_on_the_lock_release_path(tmp_path, monkeypatch):
    runner = LiveRunner(
        NeverTradeStrategy(), "TX00", log_dir=str(tmp_path), bot_name="TestBot")
    runner.acquire_lock()
    marker = os.path.join(runner.bot_dir, EXITING_FILENAME)
    present = {}
    real_remove = os.remove

    def spy_remove(path):
        if path == runner._lock_path:
            present["before_unlock"] = os.path.isfile(marker)
        real_remove(path)

    monkeypatch.setattr(os, "remove", spy_remove)
    runner.release_lock()
    assert present["before_unlock"] is True
    assert not os.path.isfile(runner._lock_path)
    assert os.path.isfile(marker)
    data = json.loads(open(marker, encoding="utf-8").read())
    assert data["pid"] == os.getpid()
    if os.name == "nt":
        assert data["created_at"]
    else:
        assert data["created_at"] is None
    assert data["written_at"]
    assert "release_lock" in inspect.getsource(LiveRunner.stop)


def test_exiting_process_never_deletes_the_marker(tmp_path):
    runner = LiveRunner(
        NeverTradeStrategy(), "TX00", log_dir=str(tmp_path), bot_name="TestBot")
    runner.acquire_lock()
    runner.release_lock()
    marker = os.path.join(runner.bot_dir, EXITING_FILENAME)
    runner.release_lock()
    from run_backtest import note_return_from_main
    note_return_from_main()
    assert os.path.isfile(marker)
    assert "os.remove" not in inspect.getsource(write_exiting_marker)
    assert inspect.getsource(LiveRunner.release_lock).count("os.remove") == 1


def test_default_liveness_removes_a_dead_pid_without_an_alert(tmp_path):
    root, watcher = _layout(tmp_path)
    path = _plant(root, "TMF00_night", pid=2**30, written_at=1.0)
    alerts = []
    check_exiting_markers(
        watcher, alert_fn=alerts.append, now=lambda: 10_000.0)
    assert alerts == []
    assert not os.path.isfile(path)


def test_exiting_check_has_no_kill_api():
    import src.live.leftover_sweep as mod
    src = inspect.getsource(mod)
    assert "TerminateProcess" not in src
    assert "os.kill" not in src


def test_hang_watchdog_poll_calls_the_leftover_sweep():
    from src.live.main_heartbeat import HangWatchdog
    assert "_sweep_leftovers(" in inspect.getsource(HangWatchdog._poll)
