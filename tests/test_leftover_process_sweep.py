"""Leftover deploy processes are reported, never stopped."""

from __future__ import annotations

import inspect
import os
from types import SimpleNamespace

import pytest

from src.live.leftover_sweep import (
    LEFTOVER_SWEEP_INTERVAL_S,
    _bot_dir_for_command,
    sweep_leftover_processes,
)


def _proc(pid, image, command, created):
    return SimpleNamespace(
        pid=pid, image=image, command_line=command, created_at=created)


def test_sweep_alerts_the_unlocked_pid_once(tmp_path):
    assert LEFTOVER_SWEEP_INTERVAL_S == 300
    root = tmp_path / "live"
    owned = root / "TMF00_owned"
    owned.mkdir(parents=True)
    (owned / ".lock").write_text("4101\n", encoding="utf-8")
    watcher = root / "TMF00_watcher"
    watcher.mkdir()
    procs = [
        _proc(4101, "python.exe",
              "python.exe run_bot_cli.py deploy --symbol TMF00 --bot owned",
              10.0),
        _proc(4102, "python.exe",
              "python.exe run_bot_cli.py deploy --symbol MXF00 --bot night2",
              20.0),
        _proc(4103, "python.exe",
              "python.exe run_backtest.py deploy --grid-subprocess-smoke",
              30.0),
    ]
    calls = {"n": 0}

    def list_processes():
        calls["n"] += 1
        return list(procs)

    alerts = []
    clock = {"t": 1000.0}

    def sweep():
        sweep_leftover_processes(
            str(watcher),
            alert_fn=alerts.append,
            list_processes=list_processes,
            now=lambda: clock["t"],
        )

    sweep()
    assert calls["n"] == 1
    assert len(alerts) == 1
    assert "leftover process" in alerts[0]
    assert "pid=4102" in alerts[0]
    assert "python.exe" in alerts[0]
    assert "MXF00_night2" in alerts[0]
    assert "4101" not in alerts[0]
    assert "4103" not in alerts[0]
    assert (owned / ".lock").read_text(encoding="utf-8").strip() == "4101"

    clock["t"] = 1000.0 + LEFTOVER_SWEEP_INTERVAL_S - 1
    sweep()
    assert calls["n"] == 1
    assert len(alerts) == 1

    clock["t"] = 1000.0 + LEFTOVER_SWEEP_INTERVAL_S
    sweep()
    assert calls["n"] == 2
    assert len(alerts) == 1

    procs[1].created_at = 99.0
    clock["t"] += LEFTOVER_SWEEP_INTERVAL_S
    sweep()
    assert calls["n"] == 3
    assert len(alerts) == 2
    assert "pid=4102" in alerts[1]


def test_unknown_command_says_unknown_and_a_live_path_is_named(tmp_path):
    root = str(tmp_path)
    assert _bot_dir_for_command(
        "python run_bot_cli.py deploy --symbol TX00 --bot night", root,
    ).endswith("TX00_night")
    assert _bot_dir_for_command(
        r"C:\apps\data\live\MXF00_night2\cli_stdout.log", root,
    ).endswith("MXF00_night2")
    assert _bot_dir_for_command("python run_backtest.py deploy", root) is None

    bot = tmp_path / "live" / "watcher"
    bot.mkdir(parents=True)
    alerts = []
    sweep_leftover_processes(
        str(bot),
        alert_fn=alerts.append,
        list_processes=lambda: [_proc(
            7, "python.exe", "python.exe run_bot_cli.py deploy", 1.0)],
        now=lambda: 50.0,
    )
    assert alerts == ["leftover process pid=7 image=python.exe bot_dir=unknown"]


def test_process_list_failure_does_not_alert_or_retry_immediately(tmp_path):
    bot = tmp_path / "live" / "watcher"
    bot.mkdir(parents=True)
    alerts = []

    def boom():
        raise OSError("no snapshot")

    sweep_leftover_processes(
        str(bot), alert_fn=alerts.append, list_processes=boom, now=lambda: 1.0)
    assert alerts == []
    calls = {"n": 0}

    def listing():
        calls["n"] += 1
        return []

    sweep_leftover_processes(
        str(bot), alert_fn=alerts.append, list_processes=listing, now=lambda: 2.0)
    assert calls["n"] == 0
    assert alerts == []


def test_held_stamp_lock_skips_the_scan(tmp_path):
    from src.live.host_stall import deploy_root
    from src.live.leftover_sweep import LOCK_FILENAME
    from src.utils.file_lock import _lock_nb, _open_lock_fd, _unlock_fd

    bot = tmp_path / "live" / "watcher"
    bot.mkdir(parents=True)
    root = deploy_root(str(bot))
    fd = _open_lock_fd(os.path.join(root, LOCK_FILENAME))
    assert _lock_nb(fd)
    alerts = []
    calls = {"n": 0}
    try:
        sweep_leftover_processes(
            str(bot),
            alert_fn=alerts.append,
            list_processes=lambda: calls.__setitem__("n", calls["n"] + 1) or [],
            now=lambda: 10.0,
        )
    finally:
        _unlock_fd(fd)
        os.close(fd)
    assert calls["n"] == 0
    assert alerts == []


def test_sweep_without_a_lister_is_a_no_op_off_windows(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows lists real processes")
    alerts = []
    sweep_leftover_processes(str(tmp_path / "bot"), alert_fn=alerts.append)
    assert alerts == []
    assert list(tmp_path.glob("leftover_sweep.json")) == []


def test_parse_cim_process_json_accepts_one_object_or_a_list():
    from src.live.leftover_sweep import parse_cim_process_json

    one = (
        '{"ProcessId":7,"Name":"python.exe",'
        '"CommandLine":"python.exe run_bot_cli.py deploy --symbol MXF00 --bot night2",'
        '"CreationDate":"/Date(1700000000000)/"}'
    )
    parsed = parse_cim_process_json(one)
    assert len(parsed) == 1
    assert parsed[0].pid == 7 and parsed[0].image == "python.exe"
    assert parsed[0].created_at == 1700000000.0
    assert "deploy" in parsed[0].command_line
    both = "[" + one + "," + one.replace('"ProcessId":7', '"ProcessId":8') + "]"
    assert [proc.pid for proc in parse_cim_process_json(both)] == [7, 8]
    assert parse_cim_process_json("  ") == []


def test_leftover_sweep_source_has_no_stop_api():
    import src.live.leftover_sweep as mod

    src = inspect.getsource(mod)
    assert "TerminateProcess" not in src
    assert "os.kill" not in src
    assert "kill" not in inspect.signature(sweep_leftover_processes).parameters
