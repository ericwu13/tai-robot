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
                path, wait_s=3, poll_s=0.001)
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
            path, wait_s=30, poll_s=0.005)
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
        path, wait_s=1, poll_s=0.05)
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
        path, wait_s=1, poll_s=0.05)
    assert owned and kind == "", (owned, kind, queue_s)
    assert queue_s < 0.5, queue_s
    release_grid_slot(path)


def _one_holder(path, hold_s, result_path):
    owned, kind, _queue = acquire_grid_slot(
        path, wait_s=10, poll_s=0.02)
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
        slot, wait_s=2, poll_s=0.05)
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
        slot, wait_s=0.4, poll_s=0.05)
    assert blocked is False and blocked_kind == "queue_timeout"
    os.kill(child, 9)
    owned, kind, queue_s = acquire_grid_slot(
        slot, wait_s=2, poll_s=0.05)
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
        path, wait_s=1, poll_s=0.05)
    assert owned and kind == ""
    assert queue_s < 0.5, queue_s
    release_grid_slot(path)


def test_parent_deadline_and_stop_keep_queue_s(tmp_path):
    """Queue deadline and STOP keep the wait in queue_s, not elapsed_s."""
    from src.evolution.grid_process import burn_child_entry, run_grid_isolated
    from tests.test_evolution_grid_process import _job

    slot = str(tmp_path / "held.slot")
    owned, _kind, _q = acquire_grid_slot(
        slot, wait_s=2, poll_s=0.05)
    assert owned
    try:
        failure = run_grid_isolated(
            _job(mode="sleep", burn_seconds=0.1),
            timeout_s=10,
            stall_s=10,
            entry=burn_child_entry,
            slot_path=slot,
            poll_s=0.05,
            slot_poll_s=0.05,
            queue_timeout_s=0.6,
        )
        assert failure.kind == "queue_timeout", failure
        assert failure.queue_s >= 0.4, failure.queue_s
        assert failure.elapsed_s < 0.2, failure.elapsed_s
    finally:
        release_grid_slot(slot)

    slot2 = str(tmp_path / "stop.slot")
    owned, _kind, _q = acquire_grid_slot(
        slot2, wait_s=2, poll_s=0.05)
    assert owned
    state = {"stop": False}

    def _stop():
        return state["stop"]

    def _later():
        time.sleep(0.4)
        state["stop"] = True

    threading.Thread(target=_later, daemon=True).start()
    try:
        failure = run_grid_isolated(
            _job(mode="sleep", burn_seconds=0.1),
            timeout_s=10,
            stall_s=10,
            entry=burn_child_entry,
            slot_path=slot2,
            poll_s=0.05,
            slot_poll_s=0.05,
            queue_timeout_s=10,
            stop_check=_stop,
        )
        assert failure.kind == "stopped", failure
        assert failure.queue_s >= 0.3, failure.queue_s
        assert failure.elapsed_s < 0.2, failure.elapsed_s
    finally:
        release_grid_slot(slot2)


def _orphan_entry(job, conn):
    """Writes a marker if the grid body runs. Module-level so spawn can pickle it."""
    open(job.evo["ran"], "w", encoding="ascii").write("ran\n")
    from src.evolution.grid_process import burn_child_entry
    burn_child_entry(job, conn)


def test_queued_orphan_exits_without_running(tmp_path):
    """A queued child exits when its parent dies, and does not take the lock."""
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    if sys.platform != "linux":
        pytest.skip(
            "queued-orphan parent kill looks up the child with ps --ppid; "
            "psutil is not a project dependency")
    repo = Path(__file__).resolve().parents[1]
    slot = str(tmp_path / "evo_grid.slot")
    marker = str(tmp_path / "ran.txt")
    owned, _kind, _q = acquire_grid_slot(
        slot, wait_s=2, poll_s=0.05)
    assert owned
    script = textwrap.dedent(f"""
        from src.evolution.grid_process import GridJob, run_grid_isolated
        from tests.test_evolution_grid_oslock import _orphan_entry
        job = GridJob("", "", (), 1, 1, {{"mode": "sleep", "burn_seconds": 0.2,
            "ran": {marker!r}}}, 14)
        run_grid_isolated(job, timeout_s=20, stall_s=20, entry=_orphan_entry,
            slot_path={slot!r}, poll_s=0.05, slot_poll_s=0.05,
            queue_timeout_s=15)
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", script], cwd=str(repo))
    deadline = time.time() + 15
    child = None
    while time.time() < deadline:
        try:
            out = subprocess.check_output(
                ["ps", "-o", "pid=,args=", "--ppid", str(proc.pid)], text=True)
        except subprocess.CalledProcessError:
            out = ""
        pids = []
        for line in out.splitlines():
            parts = line.split(None, 1)
            if not parts:
                continue
            # The resource tracker outlives the script and is not the grid child.
            if len(parts) > 1 and "resource_tracker" in parts[1]:
                continue
            pids.append(int(parts[0]))
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            alive.append(pid)
        if alive:
            time.sleep(0.3)
            try:
                os.kill(alive[0], 0)
            except OSError:
                continue
            child = alive[0]
            break
        time.sleep(0.05)
    assert child, "queued child did not start"
    os.kill(proc.pid, 9)
    proc.wait(timeout=5)
    gone = time.time() + 2
    while time.time() < gone:
        try:
            os.kill(child, 0)
        except OSError:
            break
        time.sleep(0.05)
    else:
        os.kill(child, 9)
        release_grid_slot(slot)
        raise AssertionError("queued child kept running after the parent died")
    assert not os.path.exists(marker)
    release_grid_slot(slot)
    again, kind, queue_s = acquire_grid_slot(
        slot, wait_s=1, poll_s=0.05)
    assert again and kind == "" and queue_s < 0.5
    release_grid_slot(slot)


def test_child_self_timeout_frees_the_lock(tmp_path, monkeypatch):
    """A hung backtest hits the child's own limit and drops the lock."""
    import src.evolution.grid_process as gp
    from tests.test_evolution_grid_process import _job

    monkeypatch.setattr(gp, "child_run_limit_s", lambda timeout_s: 0.6)
    slot = str(tmp_path / "evo_grid.slot")
    holder = {}

    def _run():
        holder["result"] = gp.run_grid_isolated(
            _job(mode="sleep", burn_seconds=30),
            timeout_s=30,
            stall_s=30,
            entry=gp.burn_child_entry,
            slot_path=slot,
            poll_s=0.05,
            slot_poll_s=0.05,
            queue_timeout_s=5,
        )

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join(4)
    if thread.is_alive():
        thread.join(1)
        raise AssertionError("child did not self-timeout")
    owned, kind, queue_s = acquire_grid_slot(
        slot, wait_s=1, poll_s=0.05)
    assert owned and kind == "" and queue_s < 0.5, (owned, kind, queue_s)
    release_grid_slot(slot)


def test_sidecar_is_removed_before_the_lock_drops(tmp_path, monkeypatch):
    """The sidecar is removed while this process still holds the lock."""
    import src.evolution.grid_process as gp

    path = str(tmp_path / "evo_grid.slot")
    owned, _kind, _q = acquire_grid_slot(
        path, wait_s=2, poll_s=0.05)
    assert owned
    order = []
    real_unlock = gp._unlock_fd
    real_remove = os.remove

    def _unlock(fd):
        order.append("unlock")
        return real_unlock(fd)

    def _remove(target):
        if str(target).endswith(".holder.json"):
            order.append("remove")
        return real_remove(target)

    monkeypatch.setattr(gp, "_unlock_fd", _unlock)
    monkeypatch.setattr(os, "remove", _remove)
    gp.release_grid_slot(path)
    assert "remove" in order and "unlock" in order, order
    assert order.index("remove") < order.index("unlock"), order


def test_dead_parent_process_does_not_take_the_lock(tmp_path, monkeypatch):
    """A dead multiprocessing parent does not let a queued acquire take the lock."""
    import multiprocessing

    import src.evolution.grid_process as gp

    class _Dead:
        def is_alive(self):
            return False

    monkeypatch.setattr(multiprocessing, "parent_process", lambda: _Dead())
    path = str(tmp_path / "evo_grid.slot")
    owned, kind, _queue = gp.acquire_grid_slot(
        path, wait_s=1, poll_s=0.05, parent_pid=os.getppid())
    assert owned is False, (owned, kind)
    again, again_kind, queue_s = gp.acquire_grid_slot(
        path, wait_s=1, poll_s=0.05)
    assert again and again_kind == "" and queue_s < 0.5, (again, again_kind, queue_s)
    gp.release_grid_slot(path)


def _slow_locked_child(delay, forwarded):
    """Test spawn wrapper. Sleeps before the real child tries the lock.

    Spawn reimports production modules, so a parent monkeypatch of the
    child function is invisible. The delay is an argument of this
    wrapper, which lives in the test module.
    """
    time.sleep(float(delay))
    from src.evolution.grid_process import _locked_grid_child
    return _locked_grid_child(*forwarded)


def _delay_spawn(monkeypatch, delay):
    """Make ``run_grid_isolated``'s spawn child sleep before the lock try."""
    import multiprocessing

    import src.evolution.grid_process as gp

    real_get = multiprocessing.get_context

    class _Ctx:
        def __init__(self, inner):
            self._inner = inner

        def Pipe(self, duplex=False):
            return self._inner.Pipe(duplex)

        def Process(self, target=None, args=(), **kwargs):
            if getattr(target, "__name__", "") == "_locked_grid_child":
                args = (float(delay), tuple(args))
                target = _slow_locked_child
            return self._inner.Process(target=target, args=args, **kwargs)

    def _get(method):
        return _Ctx(real_get(method))

    monkeypatch.setattr(gp.multiprocessing, "get_context", _get)


def test_silent_spawn_hits_the_pre_slot_deadline(tmp_path, monkeypatch):
    """A child that never sends a slot event is a timeout, not a hang."""
    import src.evolution.grid_process as gp
    from tests.test_evolution_grid_process import _job

    monkeypatch.setattr(gp, "pre_slot_event_deadline_s", lambda wait_s: 0.35)
    _delay_spawn(monkeypatch, 5)
    slot = str(tmp_path / "evo_grid.slot")
    result = gp.run_grid_isolated(
        _job(mode="sleep", burn_seconds=0.1),
        timeout_s=20,
        stall_s=20,
        entry=gp.burn_child_entry,
        slot_path=slot,
        poll_s=0.05,
        slot_poll_s=0.05,
        queue_timeout_s=20,
    )
    assert isinstance(result, gp.GridProcessFailure), result
    assert result.kind == "timeout", result


def test_silent_longer_than_stall_is_timeout(tmp_path, monkeypatch):
    """A spawn that stays silent past stall_s is a timeout.

    The 120 s floor is lowered to 0 so a small stall_s is the bound.
    Production still uses max(120 s, stall_s).
    """
    import src.evolution.grid_process as gp
    from tests.test_evolution_grid_process import _job

    if hasattr(gp, "PRE_SLOT_EVENT_FLOOR_S"):
        monkeypatch.setattr(gp, "PRE_SLOT_EVENT_FLOOR_S", 0.0)
    _delay_spawn(monkeypatch, 1.5)
    slot = str(tmp_path / "evo_grid.slot")
    result = gp.run_grid_isolated(
        _job(mode="sleep", burn_seconds=0.1),
        timeout_s=10,
        stall_s=0.4,
        entry=gp.burn_child_entry,
        slot_path=slot,
        poll_s=0.05,
        slot_poll_s=0.05,
        queue_timeout_s=30,
    )
    assert isinstance(result, gp.GridProcessFailure), result
    assert result.kind == "timeout", result


def test_queued_past_stall_is_not_cut_by_the_pre_slot_deadline(
        tmp_path, monkeypatch):
    """After the first slot event, stall_s does not end the queue wait."""
    import src.evolution.grid_process as gp
    from tests.test_evolution_grid_process import _job

    if hasattr(gp, "PRE_SLOT_EVENT_FLOOR_S"):
        monkeypatch.setattr(gp, "PRE_SLOT_EVENT_FLOOR_S", 0.0)
    slot = str(tmp_path / "evo_grid.slot")
    owned, _kind, _q = gp.acquire_grid_slot(slot, wait_s=2, poll_s=0.05)
    assert owned
    holder = {}

    def _release():
        time.sleep(1.2)
        gp.release_grid_slot(slot)

    threading.Thread(target=_release, daemon=True).start()
    try:
        holder["result"] = gp.run_grid_isolated(
            _job(mode="sleep", burn_seconds=0.1),
            timeout_s=10,
            stall_s=0.4,
            entry=gp.burn_child_entry,
            slot_path=slot,
            poll_s=0.05,
            slot_poll_s=0.05,
            queue_timeout_s=5,
        )
    finally:
        gp.release_grid_slot(slot)
    assert not isinstance(holder["result"], gp.GridProcessFailure), holder["result"]


def test_child_self_timeout_is_a_timeout(tmp_path, monkeypatch):
    """The child's own limit is a timeout, not a crash."""
    import src.evolution.grid_process as gp
    from tests.test_evolution_grid_process import _job

    monkeypatch.setattr(gp, "child_run_limit_s", lambda timeout_s: 0.5)
    slot = str(tmp_path / "evo_grid.slot")
    result = gp.run_grid_isolated(
        _job(mode="sleep", burn_seconds=30),
        timeout_s=30,
        stall_s=30,
        entry=gp.burn_child_entry,
        slot_path=slot,
        poll_s=0.05,
        slot_poll_s=0.05,
        queue_timeout_s=5,
    )
    assert isinstance(result, gp.GridProcessFailure), result
    assert result.kind == "timeout", result


def test_queue_clock_starts_at_the_childs_first_lock_event(tmp_path, monkeypatch):
    """Spawn time before the first lock try does not consume the queue budget."""
    import src.evolution.grid_process as gp
    from tests.test_evolution_grid_process import _job

    _delay_spawn(monkeypatch, 1.5)
    slot = str(tmp_path / "evo_grid.slot")
    result = gp.run_grid_isolated(
        _job(mode="sleep", burn_seconds=0.1),
        timeout_s=10,
        stall_s=10,
        entry=gp.burn_child_entry,
        slot_path=slot,
        poll_s=0.05,
        slot_poll_s=0.05,
        queue_timeout_s=0.8,
    )
    assert not isinstance(result, gp.GridProcessFailure), result
