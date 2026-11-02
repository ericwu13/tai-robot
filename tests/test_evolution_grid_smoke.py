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


def test_smoke_non_grid_result_is_not_ok():
    from src.evolution.grid_smoke import _summarize

    outcome, detail = _summarize("hang-finished")
    assert outcome != "ok"
    assert outcome == "crash"
    assert "not a grid outcome" in detail
    assert "str" in detail


def test_smoke_payload_in_process_exits_the_ok_path(tmp_path):
    report = run_grid_smoke(
        bars=4,
        timeout_s=10,
        payload_bytes=128,
        in_process=True,
        stop_dir=str(tmp_path),
        poll_s=0.05,
    )
    assert report.outcome != "ok", report
    assert report.outcome == "crash"
    assert "not a grid outcome" in report.detail
    assert report.result_bytes >= 128
    assert report.child_pid is None


def test_smoke_payload_over_the_pipe_buffer_returns(tmp_path):
    """``--payload-bytes`` larger than the OS pipe must not deadlock.

    128 KB is past the Windows 8 KB buffer and the Linux 64 KB buffer.
    The child builds the blob. The pickled job stays small.
    """
    import pickle

    import src.evolution.grid_smoke as smoke

    seen = {}
    real = smoke.run_grid_isolated

    def _wrap(job, **kwargs):
        seen["job_bytes"] = len(pickle.dumps(job))
        seen["nbytes"] = job.evo.get("nbytes")
        assert b"Q" * 1000 not in pickle.dumps(job)
        return real(job, **kwargs)

    # Patch the name run_grid_smoke looks up.
    orig = smoke.run_grid_isolated
    smoke.run_grid_isolated = _wrap
    try:
        report = run_grid_smoke(
            bars=4,
            timeout_s=20,
            payload_bytes=128 * 1024,
            in_process=False,
            stop_dir=str(tmp_path),
            poll_s=0.05,
        )
    finally:
        smoke.run_grid_isolated = orig
    assert seen["nbytes"] == 128 * 1024
    assert seen["job_bytes"] < 8192, seen
    assert report.outcome == "crash", report
    assert "not a grid outcome" in report.detail
    assert report.result_bytes >= 128 * 1024
    assert report.child_pid and report.child_pid > 0
    try:
        os.kill(report.child_pid, 0)
    except OSError:
        return
    raise AssertionError(f"payload child {report.child_pid} still alive")


def test_smoke_cli_exits_nonzero_for_a_non_grid_outcome(
        tmp_path, monkeypatch, capsys):
    import src.evolution.grid_smoke as smoke

    def _fake(job, **kwargs):
        on_start = kwargs.get("on_start")
        if on_start is not None:
            on_start(4321)
        return "hang-finished"

    monkeypatch.setattr(smoke, "run_grid_isolated", _fake)
    monkeypatch.setattr(
        "tempfile.mkdtemp", lambda prefix="": str(tmp_path / "smoke"))
    code = main([
        "--grid-subprocess-smoke",
        "--bars", "4",
        "--timeout-s", "30",
    ])
    out = capsys.readouterr().out
    assert code != 0, out
    assert code == 1
    assert "grid-smoke: outcome=ok" not in out
    assert "not a grid outcome" in out


def test_smoke_cli_payload_bytes_flag(tmp_path, monkeypatch, capsys):
    import src.evolution.grid_smoke as smoke

    seen = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return smoke.SmokeReport(
            child_pid=9,
            elapsed_s=0.1,
            result_bytes=int(kwargs["payload_bytes"]) + 20,
            outcome="crash",
            detail="not a grid outcome: bytes",
            stop_file=str(tmp_path / "STOP"),
        )

    monkeypatch.setattr(smoke, "run_grid_smoke", _fake)
    code = main([
        "--grid-subprocess-smoke",
        "--payload-bytes", "9000",
        "--bars", "4",
        "--timeout-s", "30",
    ])
    out = capsys.readouterr().out
    assert seen["payload_bytes"] == 9000
    assert seen["hang"] is False
    assert seen["in_process"] is False
    assert code == 1, out
    assert "grid-smoke: mode=payload" in out
    assert "grid-smoke: payload_bytes=9000" in out
    assert "grid-smoke: outcome=ok" not in out
    assert "result_bytes=" in out


def test_smoke_cli_rejects_hang_with_payload(capsys):
    code = main([
        "--grid-subprocess-smoke",
        "--hang",
        "--payload-bytes", "10",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "outcome=usage" in out
    assert "--hang and --payload-bytes" in out


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
