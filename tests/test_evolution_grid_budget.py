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
    # The original holder still owns the lock. The file bytes are not the lock.
    still, still_kind, _still_q = acquire_grid_slot(
        path, wait_s=0.2, stale_s=fresh, poll_s=0.05)
    assert still is False and still_kind == "queue_timeout"

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
    release_grid_slot(path)
    again, again_kind, again_q = acquire_grid_slot(
        path, wait_s=1, stale_s=fresh, poll_s=0.05)
    assert again and again_kind == ""
    assert again_q < 0.5
    release_grid_slot(path)


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
    # The lock file may remain. It does not block the next grid.
    from src.evolution.grid_process import acquire_grid_slot, release_grid_slot
    owned, kind, queue_s = acquire_grid_slot(
        slot, wait_s=1, stale_s=10**9, poll_s=0.05)
    assert owned and kind == "" and queue_s < 0.5
    release_grid_slot(slot)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return int(proc.pid)


def test_release_drops_the_os_lock(tmp_path):
    """Releasing the OS lock lets the next acquire succeed promptly."""
    from src.evolution.grid_process import acquire_grid_slot, release_grid_slot

    path = str(tmp_path / "evo_grid.slot")
    owned, kind, _queue = acquire_grid_slot(
        path, wait_s=2, stale_s=10**9, poll_s=0.05)
    assert owned and kind == ""
    release_grid_slot(path)
    again, again_kind, again_q = acquire_grid_slot(
        path, wait_s=1, stale_s=10**9, poll_s=0.05)
    assert again and again_kind == ""
    assert again_q < 0.5
    release_grid_slot(path)


def test_release_warns_when_unlock_fails(tmp_path, monkeypatch, caplog):
    """A failed unlock is a warning. The bytes of the file are not the lock."""
    import logging

    import src.evolution.grid_process as gp

    path = str(tmp_path / "evo_grid.slot")
    owned, kind, _queue = gp.acquire_grid_slot(
        path, wait_s=2, stale_s=10**9, poll_s=0.05)
    assert owned and kind == ""

    def _boom(_fd):
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(gp, "_unlock_fd", _boom)
    with caplog.at_level(logging.WARNING, logger="src.evolution.grid_process"):
        gp.release_grid_slot(path)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, caplog.records
    text = " ".join(r.getMessage() for r in warnings)
    assert path in text
    assert str(os.getpid()) in text
    assert "PermissionError" in text


def test_leftover_file_does_not_block(tmp_path):
    """An empty or stale pid file is not a holder. The OS lock is."""
    from src.evolution.grid_process import acquire_grid_slot, release_grid_slot

    path = tmp_path / "evo_grid.slot"
    for text in ("", "not-a-pid\n", f"{_dead_pid()}\n1.000000\n"):
        path.write_text(text, encoding="ascii")
        owned, kind, queue_s = acquire_grid_slot(
            str(path), wait_s=1, stale_s=10**9, poll_s=0.05)
        assert owned and kind == "", text
        assert queue_s < 0.5
        release_grid_slot(str(path))


def _progress_then_ok(job, conn) -> None:
    """25 progress lines, then a finished grid. In-process only."""
    from src.evolution.grid_search import GridOutcome

    for i in range(1, 26):
        conn.send(("progress", i, 25))
    conn.send(("ok", GridOutcome(
        None, (), (), None, "done", evaluated=False)))


def test_grid_progress_throttle_is_10_backtests_or_60s():
    """Whichever bound comes first. Missing on faecbc90."""
    from src.evolution.grid_process import grid_progress_should_log

    assert not grid_progress_should_log(0, 0.0, 9, 5.0)
    assert grid_progress_should_log(0, 0.0, 10, 5.0)
    assert not grid_progress_should_log(10, 5.0, 19, 20.0)
    assert grid_progress_should_log(10, 5.0, 11, 70.0)


def test_success_grid_logs_queue_progress_and_mean(tmp_path, monkeypatch):
    """The bot log shows the queue, throttled progress, and the success line."""
    from unittest.mock import MagicMock

    import run_backtest as rb
    import src.evolution.grid_process as gp
    from tests.test_evolution_history import _validation_app
    from tests.test_evolution_min_design_trades import _InlineThread

    logs: list[str] = []
    # Patching rb.threading.Thread replaces the class on the shared
    # threading module. The releaser has to stay a real thread, or it
    # runs inline and drops the slot before the pipeline starts waiting.
    real_thread = threading.Thread
    monkeypatch.setattr(rb, "_log", lambda message: logs.append(str(message)))
    monkeypatch.setattr(gp, "grid_child_entry", _progress_then_ok)
    monkeypatch.setattr(gp, "grid_queue_wait_s", lambda: 8.0)
    monkeypatch.setattr(
        "src.evolution.grid_search.grid_search_applies",
        lambda *args, **kwargs: True)
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    monkeypatch.setattr(
        "src.evolution.notify.maybe_advance_watermark",
        lambda *args, **kwargs: None)
    chat = MagicMock()
    chat.max_tokens = 1024
    chat.conversation = []
    chat.send_message.return_value = "plan"
    chat.one_shot.return_value = ""
    rb._evolution_guard_clear()
    app = _validation_app(tmp_path, chat, paper_gate="relative")
    app._grid_in_process = True
    app._grid_queue_notice_s = 0.05
    app._grid_slot_poll_s = 0.05
    slot = os.environ["TAI_EVO_GRID_SLOT"]
    from src.evolution.grid_process import acquire_grid_slot, release_grid_slot

    held, _kind, _queue = acquire_grid_slot(
        slot, wait_s=2, stale_s=10**9, poll_s=0.05)
    assert held

    def _drop():
        deadline = time.time() + 8
        while time.time() < deadline and not any(
                line == "EVO grid waiting for host slot" for line in logs):
            time.sleep(0.02)
        # Long enough for one periodic notice (queue_notice is 0.05 s)
        # and for the logged wait to round above 0 s.
        time.sleep(1.2)
        release_grid_slot(slot)

    real_thread(target=_drop, daemon=True).start()
    try:
        rb.BacktestApp._start_evolution_pipeline(
            app, "preamble", ["design trade"], "", app._result,
            auto_run=False, holdout_days=14, bot_dir=str(tmp_path),
            design_cut_idx=1)
    finally:
        rb._evolution_guard_clear()
        release_grid_slot(slot)
    text = "\n".join(logs)
    assert "EVO grid waiting for host slot" in text
    assert f"holder pid={os.getpid()}" in text
    assert "EVO grid still waiting for host slot" in text
    assert "EVO grid acquired host slot queue" in text
    assert "EVO grid progress 10/25" in text
    assert "EVO grid progress 20/25" in text
    assert "EVO grid progress 1/25" not in text
    assert "EVO grid progress 9/25" not in text
    assert "EVO grid progress 11/25" not in text
    finished = [line for line in logs if line.startswith("EVO grid finished ")]
    assert len(finished) == 1, logs
    summary = finished[0]
    assert "queue " in summary and "elapsed " in summary
    assert "n=25" in summary
    assert "mean " in summary and "s/backtest" in summary


def _stat_ino(st, ino: int):
    return os.stat_result((
        st.st_mode, int(ino), st.st_dev, st.st_nlink, st.st_uid,
        st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime,
    ))


def _zero_ino(st):
    return _stat_ino(st, 0)


def test_free_lock_logs_acquired_without_waiting(tmp_path, monkeypatch):
    """A free lock logs acquired and the finish line, and not 'waiting'."""
    from unittest.mock import MagicMock

    import run_backtest as rb
    import src.evolution.grid_process as gp
    from tests.test_evolution_history import _validation_app
    from tests.test_evolution_min_design_trades import _InlineThread

    logs: list[str] = []
    monkeypatch.setattr(rb, "_log", lambda message: logs.append(str(message)))
    monkeypatch.setattr(gp, "grid_child_entry", _progress_then_ok)
    monkeypatch.setattr(
        "src.evolution.grid_search.grid_search_applies",
        lambda *args, **kwargs: True)
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    monkeypatch.setattr(
        "src.evolution.notify.maybe_advance_watermark",
        lambda *args, **kwargs: None)
    chat = MagicMock()
    chat.max_tokens = 1024
    chat.conversation = []
    chat.send_message.return_value = "plan"
    chat.one_shot.return_value = ""
    rb._evolution_guard_clear()
    app = _validation_app(tmp_path, chat, paper_gate="relative")
    app._grid_in_process = True
    app._grid_queue_notice_s = 0.05
    app._grid_slot_poll_s = 0.05
    try:
        rb.BacktestApp._start_evolution_pipeline(
            app, "preamble", ["design trade"], "", app._result,
            auto_run=False, holdout_days=14, bot_dir=str(tmp_path),
            design_cut_idx=1)
    finally:
        rb._evolution_guard_clear()
    text = "\n".join(logs)
    assert "EVO grid waiting for host slot" not in text
    assert "EVO grid still waiting" not in text
    assert "EVO grid acquired host slot queue 0 s" in text
    finished = [line for line in logs if line.startswith("EVO grid finished ")]
    assert len(finished) == 1, logs
    assert "queue 0 s" in finished[0]
    assert "n=" in finished[0] and "mean " in finished[0]


def test_slot_release_warning_reaches_the_bot_log(tmp_path, monkeypatch):
    """A failed unlock is on the bot log, not only the Python logger."""
    from unittest.mock import MagicMock

    import run_backtest as rb
    import src.evolution.grid_process as gp
    from tests.test_evolution_history import _validation_app
    from tests.test_evolution_min_design_trades import _InlineThread

    logs: list[str] = []
    monkeypatch.setattr(rb, "_log", lambda message: logs.append(str(message)))
    monkeypatch.setattr(gp, "grid_child_entry", _progress_then_ok)
    monkeypatch.setattr(
        "src.evolution.grid_search.grid_search_applies",
        lambda *args, **kwargs: True)
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    monkeypatch.setattr(
        "src.evolution.notify.maybe_advance_watermark",
        lambda *args, **kwargs: None)
    def _stuck(_fd):
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(gp, "_unlock_fd", _stuck)
    chat = MagicMock()
    chat.max_tokens = 1024
    chat.conversation = []
    chat.send_message.return_value = "plan"
    chat.one_shot.return_value = ""
    rb._evolution_guard_clear()
    app = _validation_app(tmp_path, chat, paper_gate="relative")
    app._grid_in_process = True
    app._grid_queue_notice_s = 600
    app._grid_slot_poll_s = 0.05
    try:
        rb.BacktestApp._start_evolution_pipeline(
            app, "preamble", ["design trade"], "", app._result,
            auto_run=False, holdout_days=14, bot_dir=str(tmp_path),
            design_cut_idx=1)
    finally:
        rb._evolution_guard_clear()
    text = "\n".join(logs)
    assert "EVO grid slot release failed path=" in text
    assert f"pid={os.getpid()}" in text
    assert "PermissionError" in text
