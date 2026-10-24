"""Key-free grid smoke: in-process and spawn, no chat key and no broker."""

from __future__ import annotations

import ast
import os
import threading
import time
from pathlib import Path

from src.evolution.grid_smoke import main, run_grid_smoke

ROOT = Path(__file__).resolve().parents[1]


def test_smoke_module_does_not_import_tk_or_com():
    tree = ast.parse((ROOT / "src/evolution/grid_smoke.py").read_text(
        encoding="utf-8"))
    found = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    assert "tkinter" not in found
    assert "comtypes" not in found
    assert "run_backtest" not in found


def test_smoke_in_process_runs_the_real_grid(tmp_path):
    report = run_grid_smoke(
        bars=80,
        timeout_s=60,
        hang=False,
        in_process=True,
        stop_dir=str(tmp_path),
        poll_s=0.05,
    )
    assert report.outcome == "ok", report
    assert report.child_pid is None
    assert report.result_bytes > 0
    assert report.stop_file == os.path.join(str(tmp_path), "STOP")
    assert not os.path.isfile(report.stop_file)
    assert "settings=" in report.detail
    # Nothing outside the temp dir. The smoke does not write the STOP file.
    assert list(tmp_path.iterdir()) == []


def test_smoke_spawn_matches_the_in_process_grid(tmp_path):
    common = dict(bars=80, timeout_s=60, hang=False, poll_s=0.05)
    in_proc = run_grid_smoke(
        in_process=True, stop_dir=str(tmp_path / "in"), **common)
    lines = []
    spawned = run_grid_smoke(
        in_process=False,
        stop_dir=str(tmp_path / "sub"),
        echo=lines.append,
        **common,
    )
    assert in_proc.outcome == spawned.outcome == "ok", (in_proc, spawned)
    assert in_proc.detail == spawned.detail
    assert spawned.child_pid and spawned.child_pid > 0
    assert lines == [f"grid-smoke: child_pid={spawned.child_pid}"]
    assert spawned.result_bytes > 0
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.kill(spawned.child_pid, 0)
        except OSError:
            break
        time.sleep(0.02)
    else:
        raise AssertionError(f"smoke child {spawned.child_pid} still alive")


def test_smoke_hang_timeout_kills_the_spawn_child(tmp_path):
    started = time.monotonic()
    report = run_grid_smoke(
        bars=4,
        timeout_s=0.6,
        hang=True,
        in_process=False,
        stop_dir=str(tmp_path),
        poll_s=0.05,
    )
    elapsed = time.monotonic() - started
    assert report.outcome == "timeout", report
    assert "exceeded" in report.detail
    assert report.child_pid and report.child_pid > 0
    assert elapsed < 5, elapsed
    try:
        os.kill(report.child_pid, 0)
    except OSError:
        return
    raise AssertionError(f"hung child {report.child_pid} still alive")


def test_smoke_hang_stop_file_kills_the_spawn_child(tmp_path):
    holder = {}

    def _run():
        holder["report"] = run_grid_smoke(
            bars=4,
            timeout_s=20,
            hang=True,
            in_process=False,
            stop_dir=str(tmp_path),
            poll_s=0.05,
        )

    thread = threading.Thread(target=_run)
    thread.start()
    stop_file = tmp_path / "STOP"
    deadline = time.time() + 5
    while time.time() < deadline and not thread.is_alive():
        time.sleep(0.02)
    time.sleep(0.3)
    stop_file.write_text("stop\n", encoding="utf-8")
    thread.join(10)
    assert not thread.is_alive()
    report = holder["report"]
    assert report.outcome == "stopped", report
    assert report.stop_file == str(stop_file)
    try:
        os.kill(report.child_pid, 0)
    except OSError:
        return
    raise AssertionError(f"stopped child {report.child_pid} still alive")


def test_smoke_cli_prints_the_contract(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "tempfile.mkdtemp", lambda prefix="": str(tmp_path / "smoke"))
    code = main([
        "run_backtest.py",
        "--grid-subprocess-smoke",
        "--bars", "40",
        "--timeout-s", "60",
    ])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "grid-smoke: mode=grid" in out
    assert "grid-smoke: stop_file=" in out
    assert "tai-grid-smoke" not in out or "stop_file=" in out
    assert "grid-smoke: note=timeout is this process only" in out
    assert "grid-smoke: child_pid=" in out
    assert "grid-smoke: outcome=ok" in out
    assert "grid-smoke: result_bytes=" in out
    assert "not a settings key" in out
