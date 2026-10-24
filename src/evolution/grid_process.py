"""Run the Saturday grid sweep outside the live bot process.

``run_grid_on_bars`` is a batch of pure-Python backtests. On the evolution
worker thread that work holds the GIL and stalls the Tk loop: tick
handling, fill polling, and the STOP-file poll. ``run_grid_isolated``
runs the same function in a ``spawn`` child and waits on this thread.

Spawn is required. Linux would otherwise fork a process that already
owns Tk and COM. Windows and a PyInstaller frozen EXE only support
spawn. The child imports this module. The module import is stdlib
only, so the child does not import Tk, COM, the Capital API, or start
a bot. The grid stack is imported inside the child function.

The job is picklable (bars, strategy source, class name, grid config).
The child returns a picklable ``GridOutcome``, including trial evidence.

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


def _read_result(proc, recv_conn):
    pid = proc.pid
    try:
        proc.join(0.2)
    except Exception:
        pass
    code = proc.exitcode
    try:
        if not recv_conn.poll(1.0):
            return GridProcessFailure(
                "crash", f"grid child exited {code} with no result", pid)
        status, payload = recv_conn.recv()
    except (EOFError, OSError, ValueError) as exc:
        return GridProcessFailure(
            "crash",
            f"grid child pipe failed ({type(exc).__name__}: {exc}); "
            f"exit {code}",
            pid)
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
        entry=None):
    """Run the grid and return a ``GridOutcome`` or ``GridProcessFailure``.

    The default waits on a spawn child. ``stop_check`` is polled from
    this thread; when it returns true the child is terminated. A child
    that is still alive at ``timeout_s`` is terminated. The Tk thread
    is not this thread.

    ``in_process=True`` runs ``entry`` on the calling thread. Tests
    set it so a monkeypatched ``run_grid_on_bars`` stays in-process.
    The live worker leaves it false.
    """
    fn = grid_child_entry if entry is None else entry
    if in_process:
        if stop_check is not None and stop_check():
            return GridProcessFailure("stopped", "stop requested")
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
        try:
            send_conn.close()
            send_closed = True
        except Exception:
            pass
        deadline = time.monotonic() + float(timeout_s)
        poll = 0.05 if not poll_s or poll_s <= 0 else float(poll_s)
        while _alive(proc):
            if stop_check is not None and stop_check():
                _terminate(proc)
                return GridProcessFailure(
                    "stopped", "stop requested", proc.pid)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(proc)
                return GridProcessFailure(
                    "timeout",
                    f"grid child exceeded {float(timeout_s):g}s",
                    proc.pid)
            proc.join(min(poll, remaining))
        return _read_result(proc, recv_conn)
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
