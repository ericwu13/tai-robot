"""Run the Saturday grid sweep outside the live bot process.

``run_grid_on_bars`` is a batch of pure-Python backtests. On the evolution
worker thread that work holds the GIL and stalls the Tk loop: tick
handling, fill polling, and the STOP-file poll. ``run_grid_isolated``
runs the same function in a ``spawn`` child and waits on this thread.

Spawn is required. Linux would otherwise fork a process that already
owns Tk and COM. Windows and a PyInstaller frozen EXE only support
spawn.

Importing this module loads only the stdlib (multiprocessing, os,
time, traceback, dataclasses). That does not keep the child off every
other import. ``grid_child_entry`` imports the evolution grid stack,
and that stack imports the backtest code it needs. The child must not
import Tk, COM/comtypes, the Capital API, or the live bot
(``run_backtest``, ``src.live``, ``src.ui``). Those are the imports
``audit_child_imports`` treats as forbidden. The grid stack itself is
expected.

The job is picklable (bars, strategy source, class name, grid config).
The child returns a picklable ``GridOutcome``, including trial evidence.
The parent reads that result while the child is still running. Reading
only after the child exits deadlocks: ``send`` blocks once the payload
fills the OS pipe (8 KB on Windows, 64 KB on Linux) and the child never
reaches exit.

``in_process=True`` is the test seam. It runs the same function on the
calling thread and holds the GIL. Production leaves it false. A timeout
cannot interrupt the in-process path; the subprocess path kills the
child.
"""

from __future__ import annotations

import multiprocessing
import os
import time
import traceback
from dataclasses import dataclass

# A full pre-registered sweep (at most 48 pure-Python backtests) on a
# long 1-min history can take minutes. The bound exists so a stuck
# child cannot hold the evolution guard open. The Tk loop is not
# blocked while the worker waits.
DEFAULT_GRID_TIMEOUT_S = 20 * 60


@dataclass(frozen=True)
class GridJob:
    """Everything the child needs. No live objects, no Tk, no COM."""

    source: str
    class_name: str
    bars: tuple
    point_value: int
    capital_base: int
    evo: dict
    fold_days: int = 14
    max_folds: int = 10


@dataclass(frozen=True)
class GridProcessFailure:
    """The sweep produced no trial evidence.

    ``kind`` is ``timeout``, ``crash``, or ``stopped``. The worker
    treats every kind as a grid that selected nothing: no model pick,
    watermark unchanged, one history line, guard released.
    """

    kind: str
    detail: str
    pid: int | None = None


class _Capture:
    """Stand-in for the child end of the pipe. In-process tests only."""

    def __init__(self) -> None:
        self.item = None

    def send(self, item) -> None:
        if self.item is None:
            self.item = item

    def close(self) -> None:
        return None


def _load_strategy_class(source: str, class_name: str):
    """Rebuild the baseline class from source inside the child.

    The live class object is not pickled. ``load_strategy_from_source``
    returns the first strategy class; a file with more than one class
    is resolved by ``class_name``.
    """
    from src.ai.code_sandbox import (
        CodeExecutionError,
        CodeValidationError,
        validate_code,
    )
    from src.backtest.strategy import BacktestStrategy

    errors = validate_code(source or "")
    if errors:
        raise CodeValidationError(
            "Code validation failed:\n" + "\n".join(f"  - {e}" for e in errors))
    namespace: dict = {"__name__": "src.evolution._grid_child_strategy"}
    try:
        exec(source, namespace)  # noqa: S102 — same boundary as the sandbox
    except Exception as exc:
        raise CodeExecutionError(f"Code execution failed: {exc}") from exc
    found = []
    for obj in namespace.values():
        if (isinstance(obj, type)
                and issubclass(obj, BacktestStrategy)
                and obj is not BacktestStrategy):
            found.append(obj)
    for obj in found:
        if obj.__name__ == class_name:
            return obj
    if len(found) == 1:
        return found[0]
    names = ", ".join(obj.__name__ for obj in found) or "(none)"
    raise CodeExecutionError(
        f"strategy class {class_name!r} not found in grid source "
        f"(found {names})")


def _execute_grid_job(job: GridJob):
    """Run ``run_grid_on_bars`` on a class rebuilt from ``job``."""
    from src.evolution.grid_search import run_grid_on_bars

    strategy_cls = _load_strategy_class(job.source, job.class_name)
    evo = job.evo if isinstance(job.evo, dict) else {}
    return run_grid_on_bars(
        strategy_cls=strategy_cls,
        source=job.source,
        bars=job.bars,
        point_value=int(job.point_value),
        capital_base=int(job.capital_base),
        evo=evo,
        fold_days=int(job.fold_days),
        max_folds=int(job.max_folds),
    )


def grid_child_entry(job: GridJob, conn) -> None:
    """Spawn target. Sends ``("ok", outcome)`` or ``("error", traceback)``."""
    try:
        conn.send(("ok", _execute_grid_job(job)))
    except Exception:
        conn.send(("error", traceback.format_exc()))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def payload_child_entry(job: GridJob, conn) -> None:
    """Send ``b"Q" * nbytes``. Tests the pipe drain. Not used by the bot."""
    evo = job.evo if isinstance(job.evo, dict) else {}
    blob = b"Q" * int(evo.get("nbytes", 0))
    try:
        conn.send(("ok", blob))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def silent_child_entry(job: GridJob, conn) -> None:
    """Exit without a result. Tests the empty-pipe crash. Not used by the bot."""
    return


def drain_race_child_entry(job: GridJob, conn) -> None:
    """Send a small payload after a go-file appears, then exit.

    Tests the parent's final non-blocking read. Not used by the bot.
    The parent creates the go-file from inside an empty poll and does
    not read until this process has exited, so the payload is waiting
    in the pipe when the liveness check first sees a dead child. The
    blob is built here. It is not part of the pickled job.
    """
    evo = job.evo if isinstance(job.evo, dict) else {}
    path = str(evo.get("go_file") or "")
    deadline = time.monotonic() + float(evo.get("go_timeout_s", 8))
    while time.monotonic() < deadline:
        if path and os.path.isfile(path):
            break
        time.sleep(0.01)
    else:
        return
    blob = b"Q" * int(evo.get("nbytes", 32))
    try:
        conn.send(("ok", blob))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def hang_child_entry(job: GridJob, conn) -> None:
    """Sleep until the parent kills this process. Smoke only.

    ``--grid-subprocess-smoke --hang`` uses this so a STOP file or a
    short ``--timeout-s`` can be observed. It does not run the grid.
    """
    evo = job.evo if isinstance(job.evo, dict) else {}
    seconds = float(evo.get("hang_seconds", 3600))
    time.sleep(max(0.0, seconds))
    try:
        conn.send(("ok", "hang-finished"))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def burn_child_entry(job: GridJob, conn) -> None:
    """CPU-bound or failing stand-in. Tests only. Not used by the bot.

    ``job.evo["mode"]`` is ``burn`` (default), ``sleep``, ``crash``,
    or ``raise``. ``burn`` holds the GIL for ``burn_seconds`` so an
    in-process run stalls the caller and a subprocess run does not.
    """
    evo = job.evo if isinstance(job.evo, dict) else {}
    mode = str(evo.get("mode", "burn"))
    seconds = float(evo.get("burn_seconds", 1.0))
    if mode == "crash":
        os._exit(3)
    if mode == "raise":
        raise RuntimeError("grid child raised")
    if mode == "sleep":
        time.sleep(max(0.0, seconds))
        conn.send(("ok", None))
        return
    deadline = time.perf_counter() + max(0.0, seconds)
    acc = 0
    while time.perf_counter() < deadline:
        for i in range(20000):
            acc += i * i
    conn.send(("ok", acc))


def audit_child_imports(conn) -> None:
    """Send the forbidden modules loaded by the real grid stack.

    Spawn target for tests. The list is empty when the child did not
    import Tk, COM, the Capital API, or the bot.
    """
    import sys

    from src.evolution.grid_search import run_grid_on_bars  # noqa: F401

    bad = []
    for name in sys.modules:
        if (name in ("run_backtest", "tkinter")
                or name.startswith("tkinter.")
                or name.startswith("comtypes")
                or "SKCOM" in name
                or name.startswith("CapitalAPI")
                or name.startswith("src.live.live_runner")
                or name.startswith("src.live.headless")
                or name.startswith("src.live.com_broker")
                or name.startswith("src.ui")):
            bad.append(name)
    conn.send(sorted(bad))
    try:
        conn.close()
    except Exception:
        pass


def _brief(detail: str) -> str:
    lines = [ln.strip() for ln in (detail or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    chosen = lines[-1] if lines[0].startswith("Traceback") else lines[0]
    if len(chosen) > 300:
        chosen = chosen[:300] + "…"
    return "\n" + chosen


def grid_failure_message(failure: GridProcessFailure) -> str:
    """Operator text for a sweep that produced no trial evidence.

    Same exit as a grid that selected nothing: nothing is applied, the
    model is not asked to pick, and the watermark does not advance.
    Missing evidence is not a relative pass. The absolute PF fallback
    is a different path (the grid was not the selector).
    """
    kind = failure.kind
    if kind == "timeout":
        what_zh = "網格子行程逾時"
        what_en = "Grid subprocess timed out"
    elif kind == "stopped":
        what_zh = "已要求停止，網格子行程已終止"
        what_en = "Stop requested — the grid subprocess was terminated"
    else:
        what_zh = "網格子行程崩潰"
        what_en = "Grid subprocess crashed"
    brief = _brief(failure.detail)
    return (
        f"🧬 EVO: {what_zh}，沒有試驗證據。沒有選出設定，"
        f"本次未消耗任何交易資料（watermark 未前進），模型不挑選設定。"
        f"{brief}\n"
        f"{what_en} — no trial evidence. Nothing was selected. "
        f"Nothing was consumed: the watermark did NOT advance. "
        f"The model is not asked to pick. Missing evidence is not a "
        f"relative pass."
        f"{brief}"
    )


def _alive(proc) -> bool:
    try:
        return bool(proc.is_alive())
    except (ValueError, AssertionError):
        return False


def _terminate(proc) -> None:
    """SIGTERM, then SIGKILL. Both are TerminateProcess on Windows."""
    if not _alive(proc):
        return
    proc.terminate()
    proc.join(2.0)
    if _alive(proc):
        proc.kill()
        proc.join(2.0)


def _run_in_process(fn, job):
    cap = _Capture()
    try:
        fn(job, cap)
    except Exception:
        return GridProcessFailure("crash", traceback.format_exc())
    if cap.item is None:
        return GridProcessFailure("crash", "grid child returned no result")
    status, payload = cap.item
    if status == "ok":
        return payload
    text = payload if isinstance(payload, str) else repr(payload)
    return GridProcessFailure("crash", text)


def _join_after_receipt(proc) -> None:
    """The payload is already in hand. Reap the child, then return it."""
    try:
        proc.join(2.0)
    except Exception:
        pass
    if _alive(proc):
        _terminate(proc)


def _exit_code(proc):
    try:
        proc.join(0.2)
    except Exception:
        pass
    return proc.exitcode


def _no_result(proc) -> GridProcessFailure:
    pid = proc.pid
    # A clean exit closes the pipe just before the process is reaped.
    # Join before terminate so that exit code survives.
    code = _exit_code(proc)
    if _alive(proc):
        _terminate(proc)
        code = proc.exitcode
    return GridProcessFailure(
        "crash", f"grid child exited {code} with no result", pid)


def _pipe_failed(proc, exc) -> GridProcessFailure:
    pid = proc.pid
    code = _exit_code(proc)
    return GridProcessFailure(
        "crash",
        f"grid child pipe failed ({type(exc).__name__}: {exc}); "
        f"exit {code}",
        pid)


def _poll_message(recv_conn, timeout):
    """``("msg", item)``, ``("eof", None)``, ``("empty", None)``, or ``("err", exc)``.

    ``poll`` uses a short timeout so the caller can still notice STOP,
    the deadline, and a dead child. A positive poll then ``recv``s.
    That read is what lets a large ``send`` finish.
    """
    try:
        ready = recv_conn.poll(0.0 if timeout <= 0 else timeout)
    except EOFError:
        return "eof", None
    except (OSError, ValueError) as exc:
        return "err", exc
    if not ready:
        return "empty", None
    try:
        return "msg", recv_conn.recv()
    except EOFError:
        return "eof", None
    except (OSError, ValueError) as exc:
        return "err", exc


def _decode_message(proc, message):
    pid = proc.pid
    _join_after_receipt(proc)
    try:
        status, payload = message
    except (TypeError, ValueError):
        return GridProcessFailure(
            "crash", f"grid child sent {message!r}", pid)
    if status == "ok":
        return payload
    text = payload if isinstance(payload, str) else repr(payload)
    return GridProcessFailure("crash", text, pid)


def run_grid_isolated(
        job: GridJob,
        *,
        timeout_s: float = DEFAULT_GRID_TIMEOUT_S,
        stop_check=None,
        in_process: bool = False,
        poll_s: float = 0.2,
        entry=None,
        on_start=None):
    """Run the grid and return a ``GridOutcome`` or ``GridProcessFailure``.

    The default waits on a spawn child. Each pass checks ``stop_check``,
    polls the result pipe for a short slice, then checks the deadline
    and whether the child is still alive. The result is received as soon as
    it is readable, which drains the pipe so a payload larger than the
    OS buffer cannot stick the child in ``send``. The child is joined
    after that receipt. ``stop_check`` true, or a still-running child
    with an empty pipe at ``timeout_s``, terminates the child. A child
    that exits without a message is a crash with no trial evidence.
    The Tk thread is not this thread.

    ``in_process=True`` runs ``entry`` on the calling thread. The live
    worker leaves it false. That path still rebuilds the strategy
    class from source before ``run_grid_on_bars``, so a parent
    monkeypatch of ``run_grid_on_bars`` is not a substitute for
    patching this function. ``on_start`` is called with the child pid
    after spawn, or with None on the in-process path.
    """
    fn = grid_child_entry if entry is None else entry
    if in_process:
        if stop_check is not None and stop_check():
            return GridProcessFailure("stopped", "stop requested")
        if on_start is not None:
            on_start(None)
        return _run_in_process(fn, job)

    ctx = multiprocessing.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=fn, args=(job, send_conn), daemon=True, name="evo-grid")
    send_closed = False
    try:
        try:
            proc.start()
        except Exception as exc:
            return GridProcessFailure(
                "crash",
                f"could not start grid child: {type(exc).__name__}: {exc}",
                proc.pid)
        if on_start is not None:
            on_start(proc.pid)
        try:
            send_conn.close()
            send_closed = True
        except Exception:
            pass
        deadline = time.monotonic() + float(timeout_s)
        poll = 0.05 if not poll_s or poll_s <= 0 else float(poll_s)
        while True:
            # STOP wins over a payload that has not been read yet, same
            # as killing a child that has not exited.
            if stop_check is not None and stop_check():
                _terminate(proc)
                return GridProcessFailure(
                    "stopped", "stop requested", proc.pid)
            remaining = deadline - time.monotonic()
            slice_s = 0.0 if remaining <= 0 else min(poll, remaining)
            kind, message = _poll_message(recv_conn, slice_s)
            if kind == "msg":
                return _decode_message(proc, message)
            if kind == "err":
                return _pipe_failed(proc, message)
            if kind == "eof" or not _alive(proc):
                # The write end may have closed between the empty poll
                # and the liveness check. One more non-blocking read
                # before calling the pipe empty.
                if kind != "eof":
                    kind, message = _poll_message(recv_conn, 0.0)
                    if kind == "msg":
                        return _decode_message(proc, message)
                    if kind == "err":
                        return _pipe_failed(proc, message)
                return _no_result(proc)
            if remaining <= 0:
                _terminate(proc)
                return GridProcessFailure(
                    "timeout",
                    f"grid child exceeded {float(timeout_s):g}s",
                    proc.pid)
    finally:
        if not send_closed:
            try:
                send_conn.close()
            except Exception:
                pass
        try:
            recv_conn.close()
        except Exception:
            pass
        try:
            if _alive(proc):
                _terminate(proc)
        except Exception:
            pass
        try:
            proc.join(0.2)
        except Exception:
            pass
        try:
            proc.close()
        except Exception:
            pass
