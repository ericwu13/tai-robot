"""``src/news/bridge_runs.py`` — one bounded run log per news-bridge workflow.

The dashboard's 工作流程 view needs "did W<n> run, when, did it succeed,
on which host" without parsing four different log formats.  The recorder
is bookkeeping around a bridge pass, so it must NEVER raise into it: a
bridge that completed its vote write must not exit non-zero because the
run log could not be written.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.news import bridge_runs as br

TPE = timezone(timedelta(hours=8))
T0 = datetime(2026, 11, 7, 16, 15, tzinfo=TPE)


def lines(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x]


def test_record_run_appends_one_json_line(tmp_path):
    assert br.record_run(tmp_path, "W4", ok=True, result="vote: -",
                         started_at=T0, finished_at=T0 + timedelta(seconds=3),
                         exit_code=0) is True
    rows = lines(br.runs_path(tmp_path, "W4"))
    assert br.runs_path(tmp_path, "W4") == tmp_path / "runs" / "W4.jsonl"
    assert rows == [{
        "v": 1, "workflow": "W4", "started_at": "2026-11-07T16:15:00+08:00",
        "finished_at": "2026-11-07T16:15:03+08:00", "duration_s": 3.0,
        "ok": True, "exit_code": 0, "result": "vote: -", "error": "",
        "host": socket.gethostname()}]


def test_result_and_error_are_bounded(tmp_path):
    br.record_run(tmp_path, "W2", ok=False, result="x" * 5000, error="e" * 5000)
    row = lines(br.runs_path(tmp_path, "W2"))[0]
    assert len(row["result"]) <= br.MAX_TEXT and len(row["error"]) <= br.MAX_TEXT


def test_log_is_trimmed_to_keep_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(br, "MAX_BYTES", 2_000)
    monkeypatch.setattr(br, "KEEP_LINES", 5)
    for i in range(40):
        br.record_run(tmp_path, "W2", ok=True, result=f"pass {i}")
    rows = lines(br.runs_path(tmp_path, "W2"))
    assert len(rows) <= 40 and rows[-1]["result"] == "pass 39"
    assert Path(br.runs_path(tmp_path, "W2")).stat().st_size <= 2_000 + 400


def test_unknown_workflow_is_refused_without_raising(tmp_path):
    assert br.record_run(tmp_path, "../evil", ok=True) is False
    assert not (tmp_path / "runs").exists()


def test_unwritable_dir_returns_false(tmp_path):
    blocker = tmp_path / "runs"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    assert br.record_run(tmp_path, "W1", ok=True) is False


def test_recorder_records_success_and_result(tmp_path):
    with br.run_recorder(tmp_path, "W1") as run:
        run.result = "wrote 3 event(s)"
        run.exit_code = 0
    row = lines(br.runs_path(tmp_path, "W1"))[0]
    assert row["ok"] is True and row["result"] == "wrote 3 event(s)"
    assert row["duration_s"] >= 0


def test_recorder_nonzero_exit_is_not_ok(tmp_path):
    with br.run_recorder(tmp_path, "W3") as run:
        run.exit_code = 1
    assert lines(br.runs_path(tmp_path, "W3"))[0]["ok"] is False


def test_recorder_explicit_failure_wins(tmp_path):
    with br.run_recorder(tmp_path, "W3") as run:
        run.exit_code = 0
        run.fail("Gemini 429")
    row = lines(br.runs_path(tmp_path, "W3"))[0]
    assert row["ok"] is False and row["error"] == "Gemini 429"


def test_recorder_records_and_reraises_exceptions(tmp_path):
    with pytest.raises(ValueError):
        with br.run_recorder(tmp_path, "W2"):
            raise ValueError("boom")
    row = lines(br.runs_path(tmp_path, "W2"))[0]
    assert row["ok"] is False and row["error"] == "ValueError: boom"


def test_recorder_never_raises_on_its_own_failure(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(br, "_append", broken)
    with br.run_recorder(tmp_path, "W4") as run:
        run.result = "fine"
    # reaching here is the assertion


def test_parse_runs_tolerates_garbage_and_old_rows():
    text = "\n".join([
        json.dumps({"v": 1, "workflow": "W2", "ok": True, "finished_at": "2026-11-07T16:00:00+08:00"}),
        "{not json",
        json.dumps(["list"]),
        json.dumps({"v": 1, "workflow": "W2", "ok": False, "finished_at": "2026-11-07T16:02:00+08:00",
                    "error": "x"}),
        "",
    ])
    rows = br.parse_runs(text)
    assert [r["ok"] for r in rows] == [True, False]


def test_module_is_stdlib_only_and_loadable_by_path(tmp_path):
    """Bridges load it by file path (src/news/__init__ pulls a wider dep
    chain): it must import with nothing but the stdlib."""
    src = Path(br.__file__)
    code = ("import importlib.util,sys;"
            f"spec=importlib.util.spec_from_file_location('bridge_runs', r'{src}');"
            "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
            "bad=[k for k in sys.modules if k.startswith(('src','yaml'))];"
            "print('BAD' if bad else 'OK')")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                         text=True, check=True, cwd=str(tmp_path))
    assert out.stdout.strip() == "OK"
