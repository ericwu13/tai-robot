"""The Saturday grid sweep runs outside the live process.

The worker thread waits on a spawn child. The Tk thread is not that
thread, so a pure-Python sweep cannot hold the GIL under the main
loop. Timeout, crash, and STOP kill the child and leave the evolution
guard free with one history line.
"""

from __future__ import annotations

import ast
import inspect
import os
import threading
import time

import pytest
from datetime import datetime, timedelta
from pathlib import Path

from src.evolution.grid_process import (
    GridJob,
    GridProcessFailure,
    audit_child_imports,
    burn_child_entry,
    drain_race_child_entry,
    payload_child_entry,
    run_grid_isolated,
    silent_child_entry,
)
from src.market_data.models import Bar

ROOT = Path(__file__).resolve().parents[1]

_PARITY_SOURCE = """
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import OrderSide


class DynamicExitPullbackParity(BacktestStrategy):
    kline_type = 0
    kline_minute = 15

    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)

    def required_bars(self):
        return 1

    def on_bar(self, bar, data_store, broker):
        n = len(data_store.get_bars())
        slot = int(self.ema_period) % 5
        if broker.position_size == 0 and n % 6 == slot:
            broker.entry("L", OrderSide.LONG)
        elif broker.position_size > 0 and n % 6 == (slot + 3) % 6:
            broker.close("L", "x")
"""


def _parity_bars():
    start = datetime(2025, 1, 2, 9, 0)
    return tuple(
        Bar(
            symbol="TXF",
            dt=start + timedelta(days=i),
            open=20000,
            high=20020,
            low=19980,
            close=20000 + (i % 7),
            volume=1,
            interval=900,
        )
        for i in range(180)
    )


def _job(**evo) -> GridJob:
    return GridJob(
        source=_PARITY_SOURCE,
        class_name="DynamicExitPullbackParity",
        bars=_parity_bars(),
        point_value=200,
        capital_base=100000,
        evo=dict(evo),
        fold_days=14,
        max_folds=10,
    )


def _assert_dead(pid: int) -> None:
    assert pid and pid > 0
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.02)
    raise AssertionError(f"grid child pid {pid} is still alive")


def _count_beats(seconds: float) -> int:
    """Main-thread work that needs the GIL. Sleep would not show a stall."""
    end = time.perf_counter() + seconds
    n = 0
    while time.perf_counter() < end:
        acc = 0
        for i in range(4000):
            acc += i
        n += 1
    return n


def test_isolated_runner_defaults_to_spawn_subprocess():
    sig = inspect.signature(run_grid_isolated)
    assert sig.parameters["in_process"].default is False
    src = inspect.getsource(run_grid_isolated)
    assert 'get_context("spawn")' in src


def test_grid_process_module_import_is_stdlib_only():
    tree = ast.parse((ROOT / "src/evolution/grid_process.py").read_text(
        encoding="utf-8"))
    allowed = {
        "__future__", "multiprocessing", "os", "time", "traceback",
        "dataclasses",
    }
    found = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    assert found <= allowed, found


def test_freeze_support_runs_before_tk_and_before_the_headless_bot():
    gui = (ROOT / "run_backtest.py").read_text(encoding="utf-8")
    assert gui.index("multiprocessing.freeze_support()") < gui.index(
        "--grid-subprocess-smoke") < gui.index("import tkinter")
    cli = (ROOT / "run_bot_cli.py").read_text(encoding="utf-8")
    assert cli.index("multiprocessing.freeze_support()") < cli.index(
        "from src.live.headless")
    assert cli.index("multiprocessing.freeze_support()") < cli.index(
        "import run_backtest") if "import run_backtest" in cli else True


def test_frozen_hook_skips_comtypes_for_the_grid_child():
    """Same argv cases as the old hook locals, through one shared helper.

    ``runtime_hook_comtypes._is_multiprocessing_child`` and
    ``_is_grid_smoke`` were replaced by ``src.paths`` so the EXE entry
    and the hook cannot disagree. Fork is still only argv[1]. The smoke
    flag is still anywhere. A plain EXE still installs the gen cache.
    """
    import runtime_hook_comtypes as hook
    from src.paths import is_grid_smoke, is_multiprocessing_child

    fork = ["tai_backtest.exe", "--multiprocessing-fork", "parent_pid=1"]
    smoke = ["tai_backtest.exe", "--grid-subprocess-smoke", "--hang"]
    assert is_multiprocessing_child(fork)
    assert not is_multiprocessing_child(["tai_backtest.exe"])
    assert is_grid_smoke(smoke)
    assert not is_grid_smoke(fork)
    assert not hook.should_install_comtypes(fork)
    assert not hook.should_install_comtypes(smoke)
    assert hook.should_install_comtypes(["tai_backtest.exe"])
    src = (ROOT / "runtime_hook_comtypes.py").read_text(encoding="utf-8")
    assert "should_install_comtypes" in src
    assert "skips_gui_and_com" in src
    assert "import comtypes.gen" in src
    assert "def _is_multiprocessing_child" not in src
    assert "def _is_grid_smoke" not in src


def test_trial_evidence_matches_the_in_process_sweep():
    """Subprocess evidence equals the in-process sweep and a direct call."""
    from src.ai.code_sandbox import load_strategy_from_source
    from src.evolution.grid_search import GridOutcome, run_grid_on_bars

    job = _job()
    cls = load_strategy_from_source(job.source)
    direct = run_grid_on_bars(
        strategy_cls=cls,
        source=job.source,
        bars=job.bars,
        point_value=job.point_value,
        capital_base=job.capital_base,
        evo=job.evo,
        fold_days=job.fold_days,
        max_folds=job.max_folds,
    )
    in_proc = run_grid_isolated(job, in_process=True)
    sub = run_grid_isolated(job, timeout_s=120)
    assert isinstance(direct, GridOutcome)
    assert isinstance(in_proc, GridOutcome)
    assert isinstance(sub, GridOutcome), sub
    assert sub.reasons, sub
    assert sub == in_proc == direct
    assert sub.evidence == direct.evidence


def test_child_import_chain_does_not_pull_tk_com_or_the_bot():
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=audit_child_imports, args=(send_conn,))
    proc.start()
    send_conn.close()
    assert recv_conn.poll(30), "audit child produced no report"
    bad = recv_conn.recv()
    proc.join(10)
    recv_conn.close()
    assert bad == [], bad
    assert proc.exitcode == 0


def test_main_thread_keeps_ticking_during_a_heavy_sweep():
    """A GIL-bound in-process sweep starves this counter. Spawn does not.

    The counter runs on the calling thread. The sweep runs on a worker
    thread, which is where the live bot runs it. Sleep is not the
    counter: sleep releases the GIL and would pass either way.
    """
    job = _job(mode="burn", burn_seconds=1.6)

    def _beats_while(*, in_process: bool) -> int:
        holder = {}

        def _runner():
            holder["result"] = run_grid_isolated(
                job,
                in_process=in_process,
                timeout_s=15,
                entry=burn_child_entry,
                poll_s=0.05,
            )

        thread = threading.Thread(target=_runner)
        thread.start()
        time.sleep(0.2)
        beats = _count_beats(0.45)
        thread.join(15)
        assert not thread.is_alive()
        assert "result" in holder
        return beats

    solo = _count_beats(0.45)
    spawned = _beats_while(in_process=False)
    in_proc = _beats_while(in_process=True)
    assert spawned > solo * 0.55, (spawned, solo, in_proc)
    assert spawned > in_proc * 1.3, (spawned, in_proc, solo)


def test_timeout_kills_the_child():
    job = _job(mode="sleep", burn_seconds=30)
    failure = run_grid_isolated(
        job, timeout_s=0.4, entry=burn_child_entry, poll_s=0.05)
    assert isinstance(failure, GridProcessFailure)
    assert failure.kind == "timeout"
    _assert_dead(failure.pid)


def test_child_crash_is_no_grid_evidence():
    job = _job(mode="crash")
    failure = run_grid_isolated(
        job, timeout_s=15, entry=burn_child_entry, poll_s=0.05)
    assert isinstance(failure, GridProcessFailure)
    assert failure.kind == "crash"
    assert "3" in failure.detail
    _assert_dead(failure.pid)


def test_child_exit_without_a_result_is_a_crash():
    """K10: a clean exit that never sent a message is no trial evidence."""
    failure = run_grid_isolated(
        _job(), timeout_s=15, entry=silent_child_entry, poll_s=0.05)
    assert isinstance(failure, GridProcessFailure), failure
    assert failure.kind == "crash"
    assert "no result" in failure.detail
    assert "exited 0" in failure.detail
    _assert_dead(failure.pid)


def test_large_subprocess_result_is_read_before_the_child_exits():
    """A payload bigger than the OS pipe buffer must come back.

    The parent used to recv only after the child exited. ``send`` blocks
    once the pipe is full (8 KB on Windows, 64 KB on Linux), the child
    never exits, and the bounded timeout below turns that deadlock into
    a timeout failure. Spawn and the real result pipe are the production
    path (``in_process`` stays false).
    """
    import pickle

    n = 128 * 1024
    assert n > 64 * 1024
    assert len(pickle.dumps(("ok", b"Q" * n))) > 64 * 1024
    started = time.monotonic()
    result = run_grid_isolated(
        _job(nbytes=n),
        timeout_s=20,
        entry=payload_child_entry,
        poll_s=0.05,
    )
    elapsed = time.monotonic() - started
    assert isinstance(result, bytes), result
    assert result == b"Q" * n
    assert elapsed < 20, elapsed


def test_child_exception_is_a_crash_result():
    job = GridJob(
        source="this is not python",
        class_name="Nope",
        bars=(),
        point_value=1,
        capital_base=1,
        evo={},
    )
    failure = run_grid_isolated(job, timeout_s=60)
    assert isinstance(failure, GridProcessFailure), failure
    assert failure.kind == "crash"
    assert failure.detail.strip()
    _assert_dead(failure.pid)


def test_stop_terminates_the_child():
    job = _job(mode="sleep", burn_seconds=30)
    state = {"stop": False}

    def _stop() -> bool:
        return state["stop"]

    holder = {}

    def _runner():
        holder["result"] = run_grid_isolated(
            job,
            timeout_s=15,
            stop_check=_stop,
            entry=burn_child_entry,
            poll_s=0.05,
        )

    thread = threading.Thread(target=_runner)
    thread.start()
    time.sleep(0.3)
    state["stop"] = True
    thread.join(10)
    assert not thread.is_alive()
    failure = holder["result"]
    assert isinstance(failure, GridProcessFailure)
    assert failure.kind == "stopped"
    _assert_dead(failure.pid)


def _worker_grid_failure(monkeypatch, tmp_path, *, mode, timeout_s, arm_stop):
    """Drive the live worker until the sweep child fails."""
    from unittest.mock import MagicMock

    import run_backtest as rb
    import src.evolution.grid_process as gp
    from src.evolution.history import (
        evolution_history_path, load_evolution_history,
    )
    from src.live.headless import clear_stop_file, request_stop
    from tests.test_evolution_history import _validation_app
    from tests.test_evolution_min_design_trades import _InlineThread

    real = gp.run_grid_isolated
    seen = []

    def _wrapped(job, **kwargs):
        evo = dict(job.evo)
        evo["mode"] = mode
        evo["burn_seconds"] = 60
        job = gp.GridJob(
            source=job.source,
            class_name=job.class_name,
            bars=tuple(job.bars),
            point_value=job.point_value,
            capital_base=job.capital_base,
            evo=evo,
            fold_days=job.fold_days,
            max_folds=job.max_folds,
        )
        kwargs["entry"] = gp.burn_child_entry
        kwargs["timeout_s"] = timeout_s
        kwargs["in_process"] = False
        kwargs["poll_s"] = 0.05
        result = real(job, **kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(gp, "run_grid_isolated", _wrapped)
    monkeypatch.setattr(
        "src.evolution.grid_search.grid_search_applies",
        lambda *args, **kwargs: True)
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    advanced = []
    monkeypatch.setattr(
        "src.evolution.notify.maybe_advance_watermark",
        lambda *args, **kwargs: advanced.append(args))
    chat = MagicMock()
    chat.max_tokens = 1024
    chat.conversation = []
    chat.send_message.return_value = "plan"
    chat.one_shot.return_value = ""
    rb._evolution_guard_clear()
    app = _validation_app(tmp_path, chat, paper_gate="relative")
    if arm_stop:
        request_stop(str(tmp_path))
    try:
        rb.BacktestApp._start_evolution_pipeline(
            app, "preamble", ["design trade"], "", app._result,
            auto_run=False, holdout_days=14, bot_dir=str(tmp_path),
            design_cut_idx=1)
    finally:
        clear_stop_file(str(tmp_path))
    rows = load_evolution_history(evolution_history_path(tmp_path))
    return app, rows, seen, advanced, chat


def _assert_guard_released_with_one_failure_line(tmp_path, app, rows, seen):
    import run_backtest as rb

    try:
        assert app.errors == [], app.chat
        assert len(rows) == 1, rows
        assert rows[0]["final_status"] == "fail"
        assert rows[0]["reason_kind"] == "grid_failed"
        assert "watermark" in rows[0]["reason"].lower()
        assert len(seen) == 1
        assert isinstance(seen[0], GridProcessFailure)
        _assert_dead(seen[0].pid)
        # The pipeline's finally already dropped the guard. Clearing it
        # here would hide a leak. A second begin has to succeed or the
        # next Saturday is wedged.
        key = rb._evolution_run_key(app, str(tmp_path))
        assert rb._evolution_try_begin(key) is True
        rb._evolution_end(key)
    finally:
        rb._evolution_guard_clear()


def test_timeout_writes_history_and_releases_the_guard(tmp_path, monkeypatch):
    app, rows, seen, advanced, chat = _worker_grid_failure(
        monkeypatch, tmp_path, mode="sleep", timeout_s=0.4, arm_stop=False)
    _assert_guard_released_with_one_failure_line(tmp_path, app, rows, seen)
    assert seen[0].kind == "timeout"
    assert "timed out" in rows[0]["reason"].lower()
    assert advanced == []
    assert chat.send_message.call_count == 0


def test_crash_writes_history_and_releases_the_guard(tmp_path, monkeypatch):
    app, rows, seen, advanced, chat = _worker_grid_failure(
        monkeypatch, tmp_path, mode="crash", timeout_s=15, arm_stop=False)
    _assert_guard_released_with_one_failure_line(tmp_path, app, rows, seen)
    assert seen[0].kind == "crash"
    assert "crashed" in rows[0]["reason"].lower()
    assert advanced == []
    assert chat.send_message.call_count == 0


def test_stop_file_terminates_the_sweep_and_releases_the_guard(
        tmp_path, monkeypatch):
    app, rows, seen, advanced, chat = _worker_grid_failure(
        monkeypatch, tmp_path, mode="sleep", timeout_s=15, arm_stop=True)
    _assert_guard_released_with_one_failure_line(tmp_path, app, rows, seen)
    assert seen[0].kind == "stopped"
    assert "stop" in rows[0]["reason"].lower()
    assert advanced == []
    assert chat.send_message.call_count == 0


def test_final_drain_reads_a_payload_sent_after_the_empty_poll(
        tmp_path, monkeypatch):
    """A payload that lands after an empty poll must not be dropped.

    The first poll tells the child to send and exit, waits until that
    process is dead, then reports an empty pipe without reading. The
    production final drain is the next poll, with timeout 0. Removing
    that drain reports "no result" and this assertion fails. A poll
    that already sees the bytes returns from the main loop and never
    reaches the drain, so it would not kill that mutant.
    """
    import src.evolution.grid_process as gp

    go = tmp_path / "GO"
    real_poll = gp._poll_message
    holder: dict = {}
    reads: list[float] = []

    def _poll(recv_conn, timeout):
        if not reads and "first" not in holder:
            holder["first"] = True
            go.write_text("go\n", encoding="utf-8")
            deadline = time.time() + 5
            while time.time() < deadline:
                proc = holder.get("proc")
                if proc is not None and proc.pid and not proc.is_alive():
                    break
                time.sleep(0.01)
            else:
                raise AssertionError(
                    "child was still alive when the empty poll returned")
            return "empty", None
        kind, message = real_poll(recv_conn, timeout)
        if kind == "msg":
            reads.append(float(timeout))
        return kind, message

    real_get_context = gp.multiprocessing.get_context

    def _get_context(method=None):
        ctx = real_get_context(method)
        real_process = ctx.Process

        def _process(*args, **kwargs):
            proc = real_process(*args, **kwargs)
            holder["proc"] = proc
            return proc

        ctx.Process = _process
        return ctx

    monkeypatch.setattr(gp, "_poll_message", _poll)
    monkeypatch.setattr(gp.multiprocessing, "get_context", _get_context)
    job = GridJob(
        source="",
        class_name="",
        bars=(),
        point_value=1,
        capital_base=1,
        evo={
            "go_file": str(go),
            "nbytes": 64,
            "go_timeout_s": 8,
        },
    )
    result = run_grid_isolated(
        job,
        timeout_s=10,
        entry=drain_race_child_entry,
        poll_s=0.05,
    )
    assert result == b"Q" * 64, result
    assert reads == [0.0], reads


def test_gui_stop_sets_evolution_cancel():
    """K6: GUI stop raises _evolution_cancel before the rest of teardown.

    The worker test below sets the flag itself. This one calls
    ``_stop_live``. The flag starts false. Later teardown has nothing
    to tear down, so it raises, and the flag is already true.
    """
    import types

    import run_backtest as rb

    app = types.SimpleNamespace(_evolution_cancel=False)
    with pytest.raises(AttributeError):
        rb.BacktestApp._stop_live(app)
    assert app._evolution_cancel is True


def test_gui_stop_flag_terminates_the_sweep(tmp_path, monkeypatch):
    """_stop_live sets _evolution_cancel. The worker must kill the child."""
    from unittest.mock import MagicMock

    import run_backtest as rb
    import src.evolution.grid_process as gp
    from src.evolution.history import (
        evolution_history_path, load_evolution_history,
    )
    from tests.test_evolution_history import _validation_app

    real = gp.run_grid_isolated
    seen = []

    def _wrapped(job, **kwargs):
        evo = dict(job.evo)
        evo["mode"] = "sleep"
        evo["burn_seconds"] = 60
        job = gp.GridJob(
            source=job.source,
            class_name=job.class_name,
            bars=tuple(job.bars),
            point_value=job.point_value,
            capital_base=job.capital_base,
            evo=evo,
            fold_days=job.fold_days,
            max_folds=job.max_folds,
        )
        kwargs["entry"] = gp.burn_child_entry
        kwargs["timeout_s"] = 15
        kwargs["in_process"] = False
        kwargs["poll_s"] = 0.05
        result = real(job, **kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(gp, "run_grid_isolated", _wrapped)
    monkeypatch.setattr(
        "src.evolution.grid_search.grid_search_applies",
        lambda *args, **kwargs: True)
    # Real thread: the inline runner cannot set the flag mid-wait.
    app_box = {}

    def _arm():
        deadline = time.time() + 5
        while time.time() < deadline:
            app = app_box.get("app")
            if app is not None and getattr(app, "_evolution_cancel", True) is False:
                time.sleep(0.25)
                app._evolution_cancel = True
                return
            time.sleep(0.02)

    threading.Thread(target=_arm, daemon=True).start()
    chat = MagicMock()
    chat.max_tokens = 1024
    chat.conversation = []
    rb._evolution_guard_clear()
    app = _validation_app(tmp_path, chat, paper_gate="relative")
    app_box["app"] = app
    try:
        rb.BacktestApp._start_evolution_pipeline(
            app, "preamble", ["design trade"], "", app._result,
            auto_run=False, holdout_days=14, bot_dir=str(tmp_path),
            design_cut_idx=1)
        deadline = time.time() + 20
        while time.time() < deadline and not seen:
            time.sleep(0.05)
        assert seen and seen[0].kind == "stopped", (seen, app.chat, app.errors)
        _assert_dead(seen[0].pid)
        rows = []
        end = time.time() + 5
        while time.time() < end:
            rows = load_evolution_history(evolution_history_path(tmp_path))
            if rows:
                break
            time.sleep(0.05)
        assert len(rows) == 1, rows
        assert rows[0]["reason_kind"] == "grid_failed"
        key = rb._evolution_run_key(app, str(tmp_path))
        acquired = False
        end = time.time() + 5
        while time.time() < end:
            acquired = rb._evolution_try_begin(key)
            if acquired:
                break
            time.sleep(0.05)
        assert acquired, "evolution guard was not released after stop"
        rb._evolution_end(key)
    finally:
        rb._evolution_guard_clear()
