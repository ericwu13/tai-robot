"""`.exiting` is written at process shutdown for the last deployed bot."""

from __future__ import annotations

import inspect
import json
import os
import time

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


def test_release_lock_does_not_write_exiting(tmp_path):
    runner = LiveRunner(
        NeverTradeStrategy(), "TX00", log_dir=str(tmp_path), bot_name="TestBot")
    runner.acquire_lock()
    marker = os.path.join(runner.bot_dir, EXITING_FILENAME)
    runner.release_lock()
    assert not os.path.isfile(runner._lock_path)
    assert not os.path.isfile(marker)
    assert "release_lock" in inspect.getsource(LiveRunner.stop)
    assert "write_exiting_marker" not in inspect.getsource(LiveRunner.release_lock)
    assert inspect.getsource(LiveRunner.release_lock).count("os.remove") == 1
    write_exiting_marker(runner.bot_dir, pid=os.getpid(), created_at=1.0,
                         written_at=1.0)
    runner.acquire_lock()
    assert not os.path.isfile(marker)
    write_exiting_marker(runner.bot_dir, pid=os.getpid() + 1, created_at=1.0,
                         written_at=1.0)
    runner.acquire_lock()
    assert os.path.isfile(marker)


def test_exiting_process_never_deletes_the_marker(tmp_path):
    runner = LiveRunner(
        NeverTradeStrategy(), "TX00", log_dir=str(tmp_path), bot_name="TestBot")
    marker = os.path.join(runner.bot_dir, EXITING_FILENAME)
    write_exiting_marker(runner.bot_dir, pid=os.getpid(), created_at=1.0,
                         written_at=1.0)
    runner.release_lock()
    runner.release_lock()
    from run_backtest import note_return_from_main
    note_return_from_main()
    assert os.path.isfile(marker)
    assert "os.remove" not in inspect.getsource(write_exiting_marker)
    assert inspect.getsource(LiveRunner.release_lock).count("os.remove") == 1


def _quiet_shutdown(monkeypatch):
    import run_backtest as rb
    monkeypatch.setattr(rb, "arm_process_exit_diagnostics", lambda: None)
    monkeypatch.setattr(rb, "release_com_connections", lambda: None)


def _shutdown_app():
    import run_backtest as rb
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._exit_marker_dir = None
    app._link_host = None
    return app


def test_gui_stop_does_not_alert_while_the_process_stays_up(tmp_path, monkeypatch):
    from tests.test_deploy_abort_keeps_position import _app, _runner
    monkeypatch.setattr(
        "src.live.leftover_sweep.process_created_at", lambda _pid: 100.0)
    runner = _runner(tmp_path, "paper")
    runner.acquire_lock()
    app, _discord = _app(monkeypatch, runner, "paper", confirmed=False)
    app._exit_marker_dir = runner.bot_dir
    app._stop_live()
    assert app._live_runner is None
    alerts = []
    check_exiting_markers(
        runner.bot_dir, alert_fn=alerts.append,
        now=lambda: time.time() + 120,
        alive=lambda _pid: True, creation_of=lambda _pid: 100.0)
    assert alerts == []
    assert not os.path.isfile(os.path.join(runner.bot_dir, EXITING_FILENAME))


def test_redeploy_marks_the_second_bot_on_exit(tmp_path, monkeypatch):
    import run_backtest as rb
    monkeypatch.setattr(
        "src.live.leftover_sweep.process_created_at", lambda _pid: 100.0)
    _quiet_shutdown(monkeypatch)
    first = LiveRunner(
        NeverTradeStrategy(), "TX00", log_dir=str(tmp_path), bot_name="First")
    second = LiveRunner(
        NeverTradeStrategy(), "TX00", log_dir=str(tmp_path), bot_name="Second")
    app = _shutdown_app()
    deploy = inspect.getsource(rb.BacktestApp._deploy_live_continue)
    lock = deploy.index("self._live_runner.acquire_lock()")
    assert ("self._exit_marker_dir = self._live_runner.bot_dir"
            in deploy[lock:lock + 200])
    shutdown = inspect.getsource(rb.BacktestApp._shutdown_link)
    assert ('write_exiting_marker(getattr(self, "_exit_marker_dir", None))'
            in shutdown)
    assert shutdown.index("write_exiting_marker") < shutdown.index("host.shutdown")
    init = inspect.getsource(rb.BacktestApp.__init__)
    assert "_exit_marker_dir = None" in init
    assert "_exit_marker_dir" not in inspect.getsource(rb.BacktestApp._stop_live)
    first.acquire_lock()
    app._exit_marker_dir = first.bot_dir
    first.release_lock()
    second.acquire_lock()
    app._exit_marker_dir = second.bot_dir
    alerts = []
    check_exiting_markers(
        first.bot_dir, alert_fn=alerts.append,
        now=lambda: time.time() + 120,
        alive=lambda _pid: True, creation_of=lambda _pid: 100.0)
    assert alerts == []
    app._live_runner = None
    app._shutdown_link()
    assert os.path.isfile(os.path.join(second.bot_dir, EXITING_FILENAME))
    assert not os.path.isfile(os.path.join(first.bot_dir, EXITING_FILENAME))
    saved = json.loads(open(
        os.path.join(second.bot_dir, EXITING_FILENAME), encoding="utf-8").read())
    assert saved["pid"] == os.getpid()


def test_headless_stop_writes_the_marker_after_the_runner_is_gone(
        tmp_path, monkeypatch):
    from tests.test_deploy_abort_keeps_position import _app, _runner
    _quiet_shutdown(monkeypatch)
    runner = _runner(tmp_path, "paper")
    runner.acquire_lock()
    app, _discord = _app(monkeypatch, runner, "paper", confirmed=False)
    app._exit_marker_dir = runner.bot_dir
    app._link_host = None
    app._stop_live()
    marker = os.path.join(runner.bot_dir, EXITING_FILENAME)
    assert app._live_runner is None
    assert not os.path.isfile(marker)
    app._shutdown_link()
    assert os.path.isfile(marker)
    saved = json.loads(open(marker, encoding="utf-8").read())
    assert saved["pid"] == os.getpid()


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
