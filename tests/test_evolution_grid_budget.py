"""Grid budget, host slot, and failure extras. #194 follow-up.

The slot file in these tests is always under ``tmp_path``. The autouse
fixture also points ``TAI_EVO_GRID_SLOT`` at a throwaway directory so a
caller that uses the default path does not touch the repo ``data/``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from src.evolution.grid_process import (
    burn_child_entry,
    run_grid_isolated,
)


def _plant(path, pid: int, stamp: float) -> None:
    path.write_text(f"{int(pid)}\n{float(stamp):.6f}\n", encoding="ascii")
    os.utime(path, (stamp, stamp))


def test_budget_scales_with_runs_and_the_guard_covers_the_cap():
    from src.evolution.grid_process import (
        GRID_TIMEOUT_CAP_S,
        grid_timeout_budget_s,
    )
    from src.evolution.notify import SLOT_STARTED_GUARD_S

    assert grid_timeout_budget_s(49) == max(1200, 90 * 49) == 4410
    assert grid_timeout_budget_s(1) == 1200
    assert grid_timeout_budget_s(500) == 10800
    assert GRID_TIMEOUT_CAP_S == 10800
    assert SLOT_STARTED_GUARD_S >= GRID_TIMEOUT_CAP_S + 25 * 60


def test_slot_waits_steals_stale_and_honours_stop_and_queue_bound(tmp_path):
    """One holder. A second waits, a dead pid is taken, age and STOP too."""
    from src.evolution.grid_process import acquire_grid_slot, release_grid_slot

    slot = tmp_path / "evo_grid.slot"
    path = str(slot)
    fresh = 10**9

    owned, kind, _queue = acquire_grid_slot(
        path, wait_s=2, stale_s=fresh, poll_s=0.05)
    assert owned and kind == "", (owned, kind)
    waited = {}

    def _second():
        started = time.monotonic()
        got, got_kind, _q = acquire_grid_slot(
            path, wait_s=5, stale_s=fresh, poll_s=0.05)
        waited["dt"] = time.monotonic() - started
        waited["owned"] = got
        waited["kind"] = got_kind
        if got:
            release_grid_slot(path)

    thread = threading.Thread(target=_second)
    thread.start()
    time.sleep(0.35)
    assert thread.is_alive(), "second acquirer did not wait"
    release_grid_slot(path)
    thread.join(3)
    assert not thread.is_alive()
    assert waited["owned"] is True, waited
    assert waited["kind"] == ""
    assert waited["dt"] >= 0.25, waited

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    assert dead.returncode == 0
    _plant(slot, dead.pid, time.time())
    owned, kind, _queue = acquire_grid_slot(
        path, wait_s=2, stale_s=fresh, poll_s=0.05)
    assert owned and kind == "", (owned, kind, dead.pid)
    release_grid_slot(path)

    old = time.time() - 100
    _plant(slot, os.getpid(), old)
    owned, kind, _queue = acquire_grid_slot(
        path, wait_s=2, stale_s=30, poll_s=0.05)
    assert owned and kind == "", (owned, kind)
    # Put the live hold back for the queue and STOP cases.
    release_grid_slot(path)
    owned, kind, _queue = acquire_grid_slot(
        path, wait_s=2, stale_s=fresh, poll_s=0.05)
    assert owned and kind == ""

    queued = {}

    def _queue():
        queued["result"] = acquire_grid_slot(
            path, wait_s=0.35, stale_s=fresh, poll_s=0.05)

    qthread = threading.Thread(target=_queue)
    qthread.start()
    qthread.join(3)
    assert not qthread.is_alive()
    q_owned, q_kind, q_wait = queued["result"]
    assert q_owned is False, queued
    assert q_kind == "queue_timeout", queued
    assert q_wait >= 0.3, queued
    # The original holder still owns the file.
    current = slot.read_text(encoding="ascii").split()[0]
    assert int(current) == os.getpid()

    state = {"stop": False}
    stopped = {}

    def _stop() -> bool:
        return state["stop"]

    def _waiter():
        stopped["result"] = acquire_grid_slot(
            path, stop_check=_stop, wait_s=5, stale_s=fresh, poll_s=0.05)

    wthread = threading.Thread(target=_waiter)
    wthread.start()
    time.sleep(0.2)
    assert wthread.is_alive()
    state["stop"] = True
    wthread.join(3)
    assert not wthread.is_alive()
    s_owned, s_kind, _s_wait = stopped["result"]
    assert s_owned is False, stopped
    assert s_kind == "stopped", stopped
    current = slot.read_text(encoding="ascii").split()[0]
    assert int(current) == os.getpid()
    release_grid_slot(path)
    assert not slot.exists()


def test_history_records_grid_failure_kind_and_progress(tmp_path, monkeypatch):
    """reason_kind stays grid_failed. The kind and k/n are extras."""
    import run_backtest as rb
    from tests.test_evolution_grid_process import _worker_grid_failure

    logs: list[str] = []
    real_log = rb._log

    def _cap(message):
        logs.append(str(message))
        real_log(message)

    monkeypatch.setattr(rb, "_log", _cap)

    _app, rows, seen, advanced, _chat = _worker_grid_failure(
        monkeypatch, tmp_path, mode="sleep", timeout_s=0.4, arm_stop=False)
    assert len(rows) == 1, rows
    assert rows[0]["reason_kind"] == "grid_failed"
    assert rows[0]["grid_failure_kind"] == "timeout"
    assert "/" in rows[0]["grid_progress"]
    assert isinstance(rows[0]["grid_elapsed_s"], (int, float))
    assert isinstance(rows[0]["grid_queue_s"], (int, float))
    assert "completed" in rows[0]["reason"]
    assert "settings in" in rows[0]["reason"]
    assert seen[0].kind == "timeout"
    assert advanced == []
    assert any(
        "timeout" in line and "completed" in line and "settings in" in line
        for line in logs
    ), logs

    stalled_dir = tmp_path / "stalled-bot"
    stalled_dir.mkdir()
    logs.clear()
    _app, rows, seen, _advanced, _chat = _worker_grid_failure(
        monkeypatch, stalled_dir, mode="sleep", timeout_s=6,
        arm_stop=False, stall_s=0.8)
    assert len(rows) == 1, rows
    assert rows[0]["reason_kind"] == "grid_failed"
    assert rows[0]["grid_failure_kind"] == "stalled", rows[0]
    assert "/" in rows[0]["grid_progress"]
    assert "grid_elapsed_s" in rows[0]
    assert "grid_queue_s" in rows[0]
    assert "completed" in rows[0]["reason"]
    assert "settings in" in rows[0]["reason"]
    assert seen[0].kind == "stalled", seen[0]
    assert any(
        "stalled" in line and "completed" in line and "settings in" in line
        for line in logs
    ), logs


def test_progress_callback_does_not_change_selection():
    from src.ai.code_sandbox import load_strategy_from_source
    from src.evolution.grid_search import run_grid_on_bars
    from tests.test_evolution_grid_process import _job

    job = _job()
    cls = load_strategy_from_source(job.source)
    kwargs = dict(
        strategy_cls=cls,
        source=job.source,
        bars=job.bars,
        point_value=job.point_value,
        capital_base=job.capital_base,
        evo=job.evo,
        fold_days=job.fold_days,
        max_folds=job.max_folds,
    )
    plain = run_grid_on_bars(**kwargs)
    seen: list[tuple[int, int]] = []

    def progress(i, n):
        seen.append((int(i), int(n)))

    reported = run_grid_on_bars(**kwargs, progress=progress)
    assert reported.selected == plain.selected
    assert reported.reasons == plain.reasons
    assert reported.settings == plain.settings
    assert reported.evidence == plain.evidence
    assert reported == plain
    assert seen, "progress callback was not called"
    n = seen[0][1]
    assert [i for i, _n in seen] == list(range(1, len(seen) + 1))
    assert all(item_n == n for _i, item_n in seen)


def test_three_children_serialize_on_the_slot(tmp_path, capsys):
    """Three grids launched together take the slot one at a time."""
    from tests.test_evolution_grid_process import _job

    slot = str(tmp_path / "evo_grid.slot")
    records: list[dict] = []
    lock = threading.Lock()

    def _run(index: int) -> None:
        def _started(pid):
            stamp = time.monotonic()
            with lock:
                print(
                    f"grid-slot: child {index} start {stamp:.3f} pid={pid}",
                    flush=True,
                )
                records.append({"index": index, "start": stamp, "pid": pid})

        result = run_grid_isolated(
            _job(mode="sleep", burn_seconds=0.45),
            timeout_s=20,
            stall_s=30,
            entry=burn_child_entry,
            poll_s=0.05,
            slot_path=slot,
            slot_poll_s=0.05,
            on_start=_started,
        )
        end = time.monotonic()
        with lock:
            print(f"grid-slot: child {index} end {end:.3f}", flush=True)
            for row in records:
                if row["index"] == index and "end" not in row:
                    row["end"] = end
                    row["result"] = result
                    break

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(40)
    assert all(not thread.is_alive() for thread in threads), records
    assert len(records) == 3, records
    order = sorted(records, key=lambda row: row["start"])
    for row in order:
        assert row["result"] is None, row
        assert "end" in row, row
    for prev, nxt in zip(order, order[1:]):
        assert nxt["start"] >= prev["end"] - 0.05, (prev, nxt)
        assert nxt["start"] - prev["start"] >= 0.3, (prev, nxt)
    captured = capsys.readouterr().out
    for index in range(3):
        assert f"grid-slot: child {index} start " in captured
    assert not (tmp_path / "evo_grid.slot").exists()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return int(proc.pid)


def test_release_retries_then_removes_the_slot(tmp_path, monkeypatch):
    """A Windows-style PermissionError on the first removes still clears the slot."""
    from src.evolution.grid_process import acquire_grid_slot, release_grid_slot

    path = tmp_path / "evo_grid.slot"
    owned, kind, _queue = acquire_grid_slot(
        str(path), wait_s=2, stale_s=10**9, poll_s=0.05)
    assert owned and kind == ""
    calls = {"n": 0}
    real_remove = os.remove

    def _flaky(target):
        calls["n"] += 1
        if os.path.abspath(target) == os.path.abspath(path) and calls["n"] < 3:
            raise PermissionError(13, "sharing violation")
        return real_remove(target)

    monkeypatch.setattr(os, "remove", _flaky)
    release_grid_slot(str(path))
    assert not path.exists()
    assert calls["n"] >= 3


def test_release_warns_when_remove_never_succeeds(tmp_path, monkeypatch, caplog):
    """A remove that keeps failing is a warning, not a silent leftover."""
    import logging

    from src.evolution.grid_process import acquire_grid_slot, release_grid_slot

    path = tmp_path / "evo_grid.slot"
    owned, kind, _queue = acquire_grid_slot(
        str(path), wait_s=2, stale_s=10**9, poll_s=0.05)
    assert owned and kind == ""
    now = {"t": 1000.0}

    def _mono():
        return now["t"]

    def _snooze(seconds):
        now["t"] += float(seconds)

    def _always(_target):
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(time, "monotonic", _mono)
    monkeypatch.setattr(time, "sleep", _snooze)
    monkeypatch.setattr(os, "remove", _always)
    with caplog.at_level(logging.WARNING, logger="src.evolution.grid_process"):
        release_grid_slot(str(path))
    assert path.exists()
    assert now["t"] >= 1002.0 - 0.05
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, caplog.records
    text = " ".join(r.getMessage() for r in warnings)
    assert str(path) in text
    assert str(os.getpid()) in text


def test_young_empty_slot_is_busy(tmp_path):
    """An empty or half-written young file is held, not stolen."""
    from src.evolution.grid_process import acquire_grid_slot

    path = tmp_path / "evo_grid.slot"
    for text in ("", "not-a-pid\n"):
        path.write_text(text, encoding="ascii")
        os.utime(path, None)
        owned, kind, _queue = acquire_grid_slot(
            str(path), wait_s=0.25, stale_s=10**9, poll_s=0.05)
        assert owned is False, text
        assert kind == "queue_timeout", (text, kind)
        assert path.read_text(encoding="ascii") == text


def test_steal_does_not_delete_a_fresh_slot_after_rename(tmp_path, monkeypatch):
    """B renames what it thought was stale and must put A's fresh slot back."""
    from src.evolution.grid_process import _steal_stale_slot

    path = tmp_path / "evo_grid.slot"
    _plant(path, _dead_pid(), time.time() - 100000)
    real_replace = os.replace

    def _race(src, dst):
        if os.path.abspath(src) == os.path.abspath(path):
            _plant(path, os.getpid(), time.time())
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _race)
    _steal_stale_slot(str(path), 30)
    assert path.is_file(), "fresh slot was deleted"
    assert path.read_text(encoding="ascii").split()[0] == str(os.getpid())


def test_failed_rename_leaves_the_slot_busy(tmp_path, monkeypatch):
    """A sharing violation on rename does not delete the slot."""
    from src.evolution.grid_process import _steal_stale_slot

    path = tmp_path / "evo_grid.slot"
    _plant(path, _dead_pid(), time.time() - 100000)
    before = path.read_text(encoding="ascii")

    def _boom(_src, _dst):
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(os, "replace", _boom)
    _steal_stale_slot(str(path), 30)
    assert path.read_text(encoding="ascii") == before
    from src.evolution.grid_process import acquire_grid_slot

    owned, kind, _queue = acquire_grid_slot(
        str(path), wait_s=0.25, stale_s=30, poll_s=0.05)
    assert owned is False
    assert kind == "queue_timeout"
    assert path.read_text(encoding="ascii") == before
