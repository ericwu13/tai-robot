"""OS-lock host slot. Multiprocess storms, no sleeps-for-luck races.

The slot file under ``tmp_path`` is only a lock file. Ownership is the
OS lock, not the bytes.
"""
from __future__ import annotations

import multiprocessing
import os
import threading
import time

import pytest

from src.evolution.grid_process import (
    acquire_grid_slot,
    release_grid_slot,
)


def _ctx():
    name = "fork" if os.name != "nt" else "spawn"
    return multiprocessing.get_context(name)


def _overlap(lines: list[str]) -> int:
    """How many times two ENTER intervals are open together."""
    events = []
    for line in lines:
        kind, _pid, ts = line.split()
        events.append((float(ts), 0 if kind == "ENTER" else 1, kind))
    events.sort()
    depth = 0
    hits = 0
    for _ts, _order, kind in events:
        if kind == "ENTER":
            depth += 1
            if depth > 1:
                hits += 1
        else:
            depth = max(0, depth - 1)
    return hits


def _plant_stale(path: str) -> None:
    """A dead-pid slot file. The old steal path races on this; an OS lock ignores it."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    stamp = time.time() - 20000.0
    with open(path, "w", encoding="ascii") as handle:
        handle.write(f"1\n{stamp:.6f}\n")
    os.utime(path, (stamp, stamp))


def _thread_storm(path: str, waiters: int, rounds: int) -> int:
    """Reed's unhooked storm. Returns rounds where two holders overlapped."""
    doubles = 0
    for _ in range(int(rounds)):
        _plant_stale(path)
        state = {"n": 0, "max": 0}
        gate = threading.Lock()

        def _run():
            owned, _kind, _queue = acquire_grid_slot(
                path, wait_s=3, stale_s=12300, poll_s=0.001)
            if not owned:
                return
            with gate:
                state["n"] += 1
                state["max"] = max(state["max"], state["n"])
            time.sleep(0.005)
            with gate:
                state["n"] -= 1
            release_grid_slot(path)

        threads = [threading.Thread(target=_run) for _ in range(waiters)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
        if state["max"] >= 2:
            doubles += 1
        release_grid_slot(path)
    return doubles


def _storm_worker(path, rounds, log_path, start_bar, done_bar):
    for _ in range(int(rounds)):
        start_bar.wait()
        owned, kind, _queue = acquire_grid_slot(
            path, wait_s=30, stale_s=12300, poll_s=0.005)
        if not owned:
            fd = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
            try:
                os.write(fd, f"NOLOCK {os.getpid()} {kind}\n".encode())
            finally:
                os.close(fd)
            done_bar.wait()
            continue
        fd = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, f"ENTER {os.getpid()} {time.time():.6f}\n".encode())
            time.sleep(0.002)
            os.write(fd, f"EXIT {os.getpid()} {time.time():.6f}\n".encode())
        finally:
            os.close(fd)
        release_grid_slot(path)
        done_bar.wait()


def _run_storm(path: str, waiters: int, rounds: int) -> tuple[int, list[str]]:
    ctx = _ctx()
    log_path = path + ".overlap"
    start_bar = ctx.Barrier(waiters + 1)
    done_bar = ctx.Barrier(waiters + 1)
    procs = [
        ctx.Process(
            target=_storm_worker,
            args=(path, rounds, log_path, start_bar, done_bar),
        )
        for _ in range(waiters)
    ]
    for proc in procs:
        proc.start()
    for _ in range(int(rounds)):
        _plant_stale(path)
        start_bar.wait(30)
        done_bar.wait(30)
    for proc in procs:
        proc.join(30)
    alive = [proc.pid for proc in procs if proc.is_alive()]
    for proc in procs:
        if proc.is_alive():
            proc.terminate()
        proc.join(5)
        proc.close()
    assert not alive, alive
    lines = []
    if os.path.exists(log_path):
        lines = open(log_path, encoding="ascii").read().splitlines()
    return _overlap(lines), lines


def test_multiprocess_storm_one_holder_no_overlap(tmp_path):
    """Six waiters, 200 rounds. Enter/exit intervals do not overlap."""
    path = str(tmp_path / "evo_grid.slot")
    hits, lines = _run_storm(path, 6, 200)
    assert not any(line.startswith("NOLOCK") for line in lines), lines[:8]
    assert hits == 0, (hits, lines[:12])
    owned, kind, queue_s = acquire_grid_slot(
        path, wait_s=1, stale_s=10**9, poll_s=0.05)
    assert owned and kind == "", (owned, kind, queue_s)
    assert queue_s < 0.5, queue_s
    release_grid_slot(path)


@pytest.mark.parametrize("waiters", [4, 5])
def test_unhooked_waiter_storm_has_no_double_owner(tmp_path, waiters):
    """Reed's unhooked 4- and 5-waiter storms. No double owner, no leftover."""
    path = str(tmp_path / "evo_grid.slot")
    doubles = _thread_storm(path, waiters, 200)
    assert doubles == 0, (waiters, doubles)
    owned, kind, queue_s = acquire_grid_slot(
        path, wait_s=1, stale_s=10**9, poll_s=0.05)
    assert owned and kind == "", (owned, kind, queue_s)
    assert queue_s < 0.5, queue_s
    release_grid_slot(path)


def _one_holder(path, hold_s, result_path):
    owned, kind, _queue = acquire_grid_slot(
        path, wait_s=10, stale_s=10**9, poll_s=0.02)
    with open(result_path, "a", encoding="ascii") as handle:
        handle.write(f"{os.getpid()} {int(bool(owned))} {kind}\n")
    if not owned:
        return
    time.sleep(hold_s)
    release_grid_slot(path)


def test_killed_holder_child_frees_the_lock(tmp_path):
    """SIGKILL of the grid child drops the OS lock promptly."""
    import json

    from src.evolution.grid_process import burn_child_entry, run_grid_isolated
    from tests.test_evolution_grid_process import _job

    slot = str(tmp_path / "evo_grid.slot")
    ctx = _ctx()

    def _parent():
        run_grid_isolated(
            _job(mode="sleep", burn_seconds=30),
            timeout_s=40,
            stall_s=40,
            entry=burn_child_entry,
            slot_path=slot,
            poll_s=0.05,
            slot_poll_s=0.05,
            queue_timeout_s=5,
        )

    proc = ctx.Process(target=_parent)
    proc.start()
    sidecar = slot + ".holder.json"
    child = None
    deadline = time.time() + 15
    while time.time() < deadline:
        if os.path.exists(sidecar):
            data = json.loads(open(sidecar, encoding="utf-8").read())
            child = int(data["child_pid"])
            break
        time.sleep(0.05)
    assert child, "child never wrote the sidecar"
    os.kill(child, 9)
    owned, kind, queue_s = acquire_grid_slot(
        slot, wait_s=2, stale_s=10**9, poll_s=0.05)
    assert owned and kind == "", (owned, kind, queue_s)
    assert queue_s < 1.5, queue_s
    release_grid_slot(slot)
    proc.terminate()
    proc.join(5)


def test_killed_parent_live_child_keeps_the_lock(tmp_path):
    """A dead parent does not free the lock while its grid child lives."""
    import json

    from src.evolution.grid_process import burn_child_entry, run_grid_isolated
    from tests.test_evolution_grid_process import _job

    slot = str(tmp_path / "evo_grid.slot")
    ctx = _ctx()

    def _parent():
        run_grid_isolated(
            _job(mode="sleep", burn_seconds=30),
            timeout_s=40,
            stall_s=40,
            entry=burn_child_entry,
            slot_path=slot,
            poll_s=0.05,
            slot_poll_s=0.05,
            queue_timeout_s=5,
        )

    proc = ctx.Process(target=_parent)
    proc.start()
    sidecar = slot + ".holder.json"
    child = None
    deadline = time.time() + 15
    while time.time() < deadline:
        if os.path.exists(sidecar):
            data = json.loads(open(sidecar, encoding="utf-8").read())
            child = int(data["child_pid"])
            break
        time.sleep(0.05)
    assert child
    os.kill(proc.pid, 9)
    proc.join(5)
    os.kill(child, 0)  # child still alive
    blocked, blocked_kind, _q = acquire_grid_slot(
        slot, wait_s=0.4, stale_s=10**9, poll_s=0.05)
    assert blocked is False and blocked_kind == "queue_timeout"
    os.kill(child, 9)
    owned, kind, queue_s = acquire_grid_slot(
        slot, wait_s=2, stale_s=10**9, poll_s=0.05)
    assert owned and kind == ""
    assert queue_s < 1.5
    release_grid_slot(slot)


def test_three_waiters_one_owner_then_prompt_reacquire(tmp_path):
    """Public-API port of the 3-waiter probe. One owner, then a prompt next."""
    path = str(tmp_path / "evo_grid.slot")
    _plant_stale(path)
    result = str(tmp_path / "owners.txt")
    threads = [
        threading.Thread(target=_one_holder, args=(path, 0.25, result))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.08)
    early = ""
    if os.path.exists(result):
        early = open(result, encoding="ascii").read()
    early_n = sum(
        1 for line in early.splitlines()
        if len(line.split()) >= 2 and line.split()[1] == "1"
    )
    assert early_n <= 1, early
    for thread in threads:
        thread.join(15)
    text = open(result, encoding="ascii").read().splitlines()
    assert len(text) == 3, text
    assert all(line.split()[1] == "1" for line in text), text
    owned, kind, queue_s = acquire_grid_slot(
        path, wait_s=1, stale_s=10**9, poll_s=0.05)
    assert owned and kind == ""
    assert queue_s < 0.5, queue_s
    release_grid_slot(path)
