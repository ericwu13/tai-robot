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

The child sends ``("progress", i, n)`` after the baseline and after
each setting, then ``("ok", outcome)``. Progress is liveness only.
``stall_s`` without a progress message fails as ``stalled``.
``timeout_s`` is the overall cap and does not move when progress
arrives. Partial trial results are discarded: only a finished ``ok``
is returned.

One grid runs per install folder. The lock file is
``project_root()/data/evo_grid.slot`` (or ``TAI_EVO_GRID_SLOT``).
Two installs side by side do not share it. It is not a machine-wide
path and it is not under ``%PROGRAMDATA%``. The grid child takes an
OS lock on that file (``fcntl.flock`` or ``msvcrt.locking``) and holds
it until it exits. The operating system drops the lock when the child
dies, so there is no stale file and no steal. A JSON sidecar next to
the lock names the holder for log lines only. The child does not start
a worker pool. There is no Job Object.

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

# Floor for one backtest. A larger pre-registered sweep scales at
# 90 s per run (baseline + settings) and never exceeds the 3 h cap.
# A silent child still dies at the stall bound, which sits in front
# of that cap. The Tk loop is not blocked while the worker waits.
DEFAULT_GRID_TIMEOUT_S = 20 * 60
GRID_TIMEOUT_PER_RUN_S = 90
GRID_TIMEOUT_CAP_S = 3 * 60 * 60
DEFAULT_GRID_STALL_S = 10 * 60
GRID_SLOT_POLL_S = 5.0
GRID_QUEUE_FACTOR = 3
GRID_SLOT_FILENAME = "evo_grid.slot"
# The child frees the lock if its own backtest does not return. The
# parent still enforces timeout_s. Slack covers a kill that is in flight.
GRID_CHILD_TIMEOUT_SLACK_S = 60.0


def grid_timeout_budget_s(n_runs: int) -> int:
    """Overall cap: ``max(20 min, 90 s × runs)``, never above 3 h.

    ``n_runs`` is the baseline plus the pre-registered settings. A
    count below 1 still gets the floor.
    """
    try:
        runs = int(n_runs)
    except (TypeError, ValueError):
        runs = 1
    if runs < 1:
        runs = 1
    scaled = GRID_TIMEOUT_PER_RUN_S * runs
    return min(GRID_TIMEOUT_CAP_S, max(DEFAULT_GRID_TIMEOUT_S, scaled))


def grid_queue_wait_s() -> int:
    """How long a bot waits for the host slot before ``queue_timeout``."""
    return GRID_QUEUE_FACTOR * GRID_TIMEOUT_CAP_S


# Spawn plus import only. The queue clock is a separate clock that
# starts at the first slot event. Default stall_s is 600, so the
# production bound is 600 s.
PRE_SLOT_EVENT_FLOOR_S = 120.0


def pre_slot_event_deadline_s(stall_s: float) -> float:
    """How long a silent spawn may run before its first slot event.

    ``max(120 s, stall_s)``. This covers spawn plus import only. The
    queue clock starts at the first slot event and is not this bound.
    The clock starts at ``proc.start`` and the first slot message
    clears it. A child that dies with no slot event is still an eof/crash.
    """
    try:
        stall = float(stall_s)
    except (TypeError, ValueError):
        stall = float(DEFAULT_GRID_STALL_S)
    if stall < 0:
        stall = 0.0
    return max(PRE_SLOT_EVENT_FLOOR_S, stall)


def child_run_limit_s(timeout_s: float) -> float:
    """How long the child may hold the lock: the run budget plus slack.

    ``timeout_s`` is the parent budget (``max(1200, 90 s × n)``, capped
    at 3 h). The child uses this so a hung backtest still drops the
    lock when the parent is not around to kill it.
    """
    try:
        budget = float(timeout_s)
    except (TypeError, ValueError):
        budget = float(DEFAULT_GRID_TIMEOUT_S)
    if budget < 0:
        budget = 0.0
    return budget + GRID_CHILD_TIMEOUT_SLACK_S


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

    ``kind`` is ``timeout``, ``stalled``, ``crash``, ``stopped``, or
    ``queue_timeout``. The worker treats every kind as a grid that
    selected nothing: no model pick, watermark unchanged, one history
    line, guard released. ``progress`` is ``k/n`` completed runs.
    Partial trial results are not kept.
    """

    kind: str
    detail: str
    pid: int | None = None
    progress: str = ""
    elapsed_s: float = 0.0
    queue_s: float = 0.0


def _is_progress(message) -> bool:
    """True for a ``("progress", i, n)`` liveness message."""
    return (
        isinstance(message, tuple)
        and len(message) == 3
        and message[0] == "progress"
    )


class _Capture:
    """Stand-in for the child end of the pipe. In-process tests only."""

    def __init__(self, on_progress=None) -> None:
        self.item = None
        self.on_progress = on_progress
        self.progress = "0/0"

    def send(self, item) -> None:
        if _is_progress(item):
            try:
                done = int(item[1])
                total = int(item[2])
            except (TypeError, ValueError):
                return
            self.progress = f"{done}/{total}"
            if self.on_progress is not None:
                try:
                    self.on_progress(done, total)
                except Exception:
                    pass
            return
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


def _execute_grid_job(job: GridJob, progress=None):
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
        progress=progress,
    )


def _set_below_normal_priority() -> None:
    """Ask Windows for ``BELOW_NORMAL``. No-op elsewhere. Never raises.

    The ctypes import stays inside this function and behind the
    ``os.name != "nt"`` guard so a Linux child does not load it.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        below_normal = 0x00004000
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel.GetCurrentProcess()
        kernel.SetPriorityClass(handle, below_normal)
    except Exception:
        return


def grid_child_entry(job: GridJob, conn) -> None:
    """Spawn target. Sends progress, then ``("ok", outcome)`` or an error.

    One backtest at a time. This process does not start a worker pool.
    """
    _set_below_normal_priority()

    def progress(i, n) -> None:
        conn.send(("progress", int(i), int(n)))

    try:
        conn.send(("ok", _execute_grid_job(job, progress=progress)))
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
    elif kind == "stalled":
        what_zh = "網格子行程停滯"
        what_en = "Grid subprocess stalled"
    elif kind == "queue_timeout":
        what_zh = "網格排隊逾時"
        what_en = "Grid queue timed out"
    else:
        what_zh = "網格子行程崩潰"
        what_en = "Grid subprocess crashed"
    brief = _brief(failure.detail)
    progress = failure.progress or "0/0"
    try:
        elapsed = float(failure.elapsed_s)
    except (TypeError, ValueError):
        elapsed = 0.0
    done_line = f"completed {progress} settings in {elapsed:.0f} s"
    return (
        f"🧬 EVO: {what_zh}，沒有試驗證據。沒有選出設定，"
        f"本次未消耗任何交易資料（watermark 未前進），模型不挑選設定。"
        f"{brief}\n"
        f"{what_en} — no trial evidence. Nothing was selected. "
        f"Nothing was consumed: the watermark did NOT advance. "
        f"The model is not asked to pick. Missing evidence is not a "
        f"relative pass."
        f"{brief}\n"
        f"{done_line}"
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


def _run_in_process(fn, job, on_progress=None):
    cap = _Capture(on_progress)
    try:
        fn(job, cap)
    except Exception:
        return GridProcessFailure(
            "crash", traceback.format_exc(), progress=cap.progress)
    if cap.item is None:
        return GridProcessFailure(
            "crash", "grid child returned no result", progress=cap.progress)
    try:
        status, payload = cap.item
    except (TypeError, ValueError):
        return GridProcessFailure(
            "crash", f"grid child sent {cap.item!r}", progress=cap.progress)
    if status == "ok":
        return payload
    text = payload if isinstance(payload, str) else repr(payload)
    return GridProcessFailure("crash", text, progress=cap.progress)


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


def default_slot_path() -> str:
    """This install's grid slot. Not shared with another install folder.

    ``project_root()/data/evo_grid.slot`` is the writable install
    directory (the EXE folder when frozen, the repo root from source).
    Two folders running at once — source tree and EXE, or old and new
    install — each have their own slot. That is intentional. Do not
    move the file to ``%PROGRAMDATA%`` or any other machine-wide path.
    ``TAI_EVO_GRID_SLOT`` redirects tests off ``data/``.
    """
    override = os.environ.get("TAI_EVO_GRID_SLOT", "").strip()
    if override:
        return override
    from src.paths import project_root
    return os.path.join(project_root(), "data", GRID_SLOT_FILENAME)


def grid_progress_should_log(
        last_i: int,
        last_t: float,
        i: int,
        now: float,
        *,
        every_n: int = 10,
        every_s: float = 60.0) -> bool:
    """True when a progress line is due.

    Log every ``every_n`` backtests or every ``every_s`` seconds,
    whichever comes first. ``i`` must have moved forward.
    """
    try:
        done = int(i)
        prev = int(last_i)
    except (TypeError, ValueError):
        return False
    if done <= prev:
        return False
    if (done - prev) >= int(every_n):
        return True
    try:
        return (float(now) - float(last_t)) >= float(every_s)
    except (TypeError, ValueError):
        return False



def _pid_alive(pid: int) -> bool:
    """True when ``pid`` is a live process.

    Windows ``os.kill(pid, 0)`` delivers CTRL_C_EVENT. A non-null
    ``OpenProcess`` handle is not enough: the process may already have
    exited. ``GetExitCodeProcess`` must still report ``STILL_ACTIVE``.
    A permission error on POSIX counts as alive.
    """
    try:
        target = int(pid)
    except (TypeError, ValueError):
        return False
    if target <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel32.OpenProcess(0x1000, False, target)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(target, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _parent_gone(parent_pid: int) -> bool:
    """True when the process that spawned this child has exited.

    ``parent_process().is_alive()`` waits on the spawn sentinel. That
    is the check that works on Windows, where ``getppid`` does not
    change and ``OpenProcess`` can still succeed while the sentinel
    handle is open. When this is not a spawn child, POSIX falls back
    to a ppid change or a gone pid.
    """
    parent = None
    try:
        parent = multiprocessing.parent_process()
    except Exception:
        parent = None
    if parent is not None:
        try:
            return not bool(parent.is_alive())
        except Exception:
            pass
    if os.name == "nt":
        # No spawn sentinel. Do not treat a bare OpenProcess handle as alive.
        return not _pid_alive(parent_pid)
    try:
        if os.getppid() != int(parent_pid):
            return True
    except (OSError, TypeError, ValueError):
        return True
    return not _pid_alive(parent_pid)


def _sidecar_path(path: str) -> str:
    """Diagnostic file next to the lock. Never decides who owns the slot."""
    return path + ".holder.json"


def _read_holder_pid(path: str):
    """Child pid from the sidecar, for a log line only."""
    try:
        with open(_sidecar_path(path), "r", encoding="utf-8") as handle:
            text = handle.read(512)
    except OSError:
        return None
    try:
        import json
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("child_pid", "pid"):
        try:
            pid = int(data.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            return pid
    return None


def _sidecar_child_pid(path: str):
    """Holder pid written in the sidecar, or None. The parent pid is not a holder."""
    try:
        with open(_sidecar_path(path), "r", encoding="utf-8") as handle:
            text = handle.read(512)
    except OSError:
        return None
    try:
        import json
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        pid = int(data.get("child_pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _write_sidecar(path: str, parent_pid: int, on_warning=None) -> None:
    import json

    payload = {
        "pid": int(parent_pid) if parent_pid else os.getpid(),
        "child_pid": os.getpid(),
        "started": time.time(),
    }
    target = _sidecar_path(path)
    try:
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError as exc:
        _warn_slot(
            f"grid slot sidecar write failed path={target} "
            f"pid={os.getpid()} [{type(exc).__name__}] {exc}",
            on_warning,
        )


def _warn_slot(message: str, on_warning) -> None:
    import logging

    logging.getLogger(__name__).warning("%s", message)
    if on_warning is None:
        return
    try:
        on_warning(message)
    except Exception:
        pass


def _open_lock_fd(path: str) -> int:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags, 0o644)
    try:
        os.set_inheritable(fd, False)
    except OSError:
        pass
    try:
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"\0")
    except OSError:
        pass
    return fd


def _lock_busy(exc: OSError) -> bool:
    import errno
    err = getattr(exc, "errno", None)
    return err in (
        errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK,
    )


def _lock_nb(fd: int) -> bool:
    """Non-blocking exclusive lock. False when another holder has it."""
    if os.name == "nt":
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if _lock_busy(exc):
                return False
            raise
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if _lock_busy(exc):
            return False
        raise


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


# path -> fd this process holds. The OS drops the lock when we close it
# or when this process dies. Nothing else inherits the fd.
_HELD_LOCKS: dict[str, int] = {}


def _emit_queue(on_queue, kind: str, **fields) -> None:
    if on_queue is None:
        return
    try:
        on_queue(kind, **fields)
    except Exception:
        pass


def acquire_grid_slot(
        path: str,
        *,
        stop_check=None,
        poll_s: float = GRID_SLOT_POLL_S,
        wait_s: float,
        sleep=None,
        on_queue=None,
        notice_s: float = 600.0,
        on_warning=None,
        parent_pid: int | None = None):
    """Take the host lock. ``(owned, kind, queue_s)``.

    The lock is ``fcntl.flock`` on POSIX and ``msvcrt.locking`` on
    Windows. The operating system releases it when this process dies.
    There is no stale file and no steal.

    A free lock is taken on the first try and does not emit ``wait``.
    ``wait`` is emitted only after that try fails. While waiting, a
    ``waiting`` line is emitted every ``notice_s`` (10 min in
    production) with the sidecar's holder pid when one was written.
    The sidecar is diagnostic only.

    When ``parent_pid`` is set, each poll also checks that the parent
    is still alive. A reparent or a gone parent returns not owned and
    does not take the lock. In-process callers leave it unset.
    """
    pause = time.sleep if sleep is None else sleep
    start = time.monotonic()
    deadline = start + max(0.0, float(wait_s))
    # Production polls every 5 s. Tests pass a shorter poll.
    poll = 5.0 if poll_s is None or float(poll_s) <= 0 else float(poll_s)
    notice = 600.0 if notice_s is None or float(notice_s) <= 0 else float(notice_s)
    key = os.path.abspath(path)
    fd = _open_lock_fd(path)
    announced = False
    last_notice = start
    try:
        while True:
            if parent_pid and _parent_gone(int(parent_pid)):
                try:
                    os.close(fd)
                except OSError:
                    pass
                return False, "stopped", time.monotonic() - start
            if _lock_nb(fd):
                _HELD_LOCKS[key] = fd
                _write_sidecar(
                    path,
                    os.getpid() if parent_pid is None else int(parent_pid),
                    on_warning,
                )
                queue_s = time.monotonic() - start
                _emit_queue(
                    on_queue, "acquired",
                    queue_s=queue_s, holder=os.getpid())
                return True, "", queue_s
            now = time.monotonic()
            if not announced:
                announced = True
                _emit_queue(
                    on_queue, "wait", holder=_read_holder_pid(path))
                last_notice = now
            if stop_check is not None and stop_check():
                os.close(fd)
                return False, "stopped", now - start
            if now - last_notice >= notice:
                _emit_queue(
                    on_queue, "waiting",
                    holder=_read_holder_pid(path), waited_s=now - start)
                last_notice = now
            if now >= deadline:
                os.close(fd)
                return False, "queue_timeout", now - start
            pause(min(poll, max(0.0, deadline - now)))
    except BaseException:
        if key not in _HELD_LOCKS:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def release_grid_slot(
        path: str, pid: int | None = None, *, on_warning=None) -> None:
    """Drop the OS lock this process holds. A missing hold is a no-op.

    ``pid`` is ignored: the lock is this process's fd, not a pid written
    into the file. A failed unlock or a leftover sidecar is logged.
    """
    del pid
    key = os.path.abspath(path)
    fd = _HELD_LOCKS.pop(key, None)
    if fd is None:
        return
    # Remove the sidecar before the unlock, and only when it still
    # names this process. A waiter can take the lock the moment we
    # unlock and write its own sidecar.
    sidecar = _sidecar_path(path)
    if _sidecar_child_pid(path) == os.getpid():
        try:
            os.remove(sidecar)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _warn_slot(
                f"grid slot sidecar cleanup failed path={sidecar} "
                f"pid={os.getpid()} [{type(exc).__name__}] {exc}",
                on_warning,
            )
    try:
        _unlock_fd(fd)
    except OSError as exc:
        _warn_slot(
            f"grid slot release failed path={path} pid={os.getpid()} "
            f"[{type(exc).__name__}] {exc}",
            on_warning,
        )
    try:
        os.close(fd)
    except OSError as exc:
        _warn_slot(
            f"grid slot release failed path={path} pid={os.getpid()} "
            f"[{type(exc).__name__}] {exc}",
            on_warning,
        )


def _child_hold_watch(slot_path, deadline, on_warning, conn) -> None:
    """Drop the lock if this backtest does not return by ``deadline``.

    The parent normally kills the child at ``timeout_s``. This thread
    covers a hung backtest whose parent is gone, so the OS lock does
    not stay held. The ``self_timeout`` message makes the parent record
    ``timeout`` instead of a crash. ``os._exit`` skips the main
    thread's finally; the release above has already closed the fd.
    """
    while time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        conn.send(("self_timeout",))
    except Exception:
        pass
    release_grid_slot(slot_path, on_warning=on_warning)
    os._exit(0)


def _locked_grid_child(
        fn, job, conn, slot_path, wait_s, poll_s, notice_s, parent_pid,
        run_limit_s):
    """Child target. This process, not the parent, holds the host lock."""

    def _on_queue(kind, **fields):
        try:
            conn.send(("slot", kind, dict(fields)))
        except Exception:
            pass

    def _on_warning(message):
        try:
            conn.send(("slot_warn", str(message)))
        except Exception:
            pass

    owned, qkind, queue_s = acquire_grid_slot(
        slot_path,
        wait_s=float(wait_s),
        poll_s=float(poll_s),
        notice_s=float(notice_s),
        on_queue=_on_queue,
        on_warning=_on_warning,
        parent_pid=int(parent_pid) if parent_pid else None,
    )
    if not owned:
        try:
            conn.send((
                "slot_done",
                qkind or "queue_timeout",
                float(queue_s),
            ))
        except Exception:
            pass
        return
    import threading
    try:
        limit = float(run_limit_s)
    except (TypeError, ValueError):
        limit = child_run_limit_s(DEFAULT_GRID_TIMEOUT_S)
    threading.Thread(
        target=_child_hold_watch,
        args=(
            slot_path, time.monotonic() + max(0.0, limit), _on_warning, conn,
        ),
        daemon=True,
        name="evo-grid-hold",
    ).start()
    try:
        fn(job, conn)
    finally:
        release_grid_slot(slot_path, on_warning=_on_warning)

def _stamp_failure(result, *, progress: str, elapsed_s: float, queue_s: float):
    if not isinstance(result, GridProcessFailure):
        return result
    shown = result.progress or progress
    return GridProcessFailure(
        result.kind,
        result.detail,
        result.pid,
        progress=shown,
        elapsed_s=float(elapsed_s),
        queue_s=float(queue_s),
    )


def run_grid_isolated(
        job: GridJob,
        *,
        timeout_s: float = DEFAULT_GRID_TIMEOUT_S,
        stop_check=None,
        in_process: bool = False,
        poll_s: float = 0.2,
        entry=None,
        on_start=None,
        stall_s: float = DEFAULT_GRID_STALL_S,
        on_progress=None,
        slot_path: str | None = None,
        slot_poll_s: float = GRID_SLOT_POLL_S,
        queue_timeout_s: float | None = None,
        on_queue=None,
        queue_notice_s: float = 600.0,
        on_release_warning=None):
    """Run the grid and return a ``GridOutcome`` or ``GridProcessFailure``.

    The default waits on a spawn child. Each pass checks ``stop_check``,
    polls the result pipe for a short slice, then checks the deadline
    and whether the child is still alive. A ``("progress", i, n)``
    message is liveness: it records ``i/n`` and does not finish the
    run. The result is received as soon as it is readable, which drains
    the pipe so a payload larger than the OS buffer cannot stick the
    child in ``send``. Messages already buffered behind a progress
    line are drained too, including the final non-blocking read after
    the child has exited. The child is joined after that receipt.
    ``stop_check`` true, or a still-running child with an empty pipe at
    ``timeout_s``, terminates the child. No progress for ``stall_s``
    fails as ``stalled`` even when the overall cap is still in the
    future. A child that exits without a message is a crash with no
    trial evidence. Partial results are not kept.

    The timeout and stall clocks start when the host slot is acquired.
    Waiting for that slot is capped at ``queue_timeout_s`` (default
    three times the 3 h cap). The Tk thread is not this thread.

    ``in_process=True`` runs ``entry`` on the calling thread. The live
    worker leaves it false. That path still rebuilds the strategy
    class from source before ``run_grid_on_bars``, so a parent
    monkeypatch of ``run_grid_on_bars`` is not a substitute for
    patching this function. ``on_start`` is called with the child pid
    after spawn, or with None on the in-process path.
    """
    fn = grid_child_entry if entry is None else entry
    path = default_slot_path() if slot_path is None else str(slot_path)
    wait_s = float(grid_queue_wait_s() if queue_timeout_s is None
                   else queue_timeout_s)
    # Spawn, not fork: the parent already owns Tk and COM.
    ctx = multiprocessing.get_context("spawn")
    if in_process:
        # No child exists on this seam, so the caller holds the lock.
        owned, qkind, queue_s = acquire_grid_slot(
            path,
            stop_check=stop_check,
            poll_s=slot_poll_s,
            wait_s=wait_s,
            on_queue=on_queue,
            notice_s=queue_notice_s,
            on_warning=on_release_warning,
        )
        if not owned:
            detail = ("stop requested" if qkind == "stopped"
                      else f"grid queue exceeded {wait_s:g}s; progress 0/0")
            return GridProcessFailure(
                qkind or "queue_timeout",
                detail,
                None,
                progress="0/0",
                elapsed_s=0.0,
                queue_s=queue_s,
            )
        try:
            return _run_grid_held(
                job,
                fn=fn,
                timeout_s=float(timeout_s),
                stall_s=float(stall_s),
                stop_check=stop_check,
                in_process=True,
                poll_s=poll_s,
                on_start=on_start,
                on_progress=on_progress,
                queue_s=queue_s,
                run_started=time.monotonic(),
                ctx=ctx,
                slot_path=path,
                on_warning=on_release_warning,
                on_queue=on_queue,
                child_locks=False,
                wait_s=wait_s,
                slot_poll_s=slot_poll_s,
                notice_s=queue_notice_s,
            )
        finally:
            release_grid_slot(path, on_warning=on_release_warning)
    return _run_grid_held(
        job,
        fn=fn,
        timeout_s=float(timeout_s),
        stall_s=float(stall_s),
        stop_check=stop_check,
        in_process=False,
        poll_s=poll_s,
        on_start=on_start,
        on_progress=on_progress,
        queue_s=0.0,
        run_started=time.monotonic(),
        ctx=ctx,
        slot_path=path,
        on_warning=on_release_warning,
        on_queue=on_queue,
        child_locks=True,
        wait_s=wait_s,
        slot_poll_s=slot_poll_s,
        notice_s=queue_notice_s,
    )


def _run_grid_held(
        job,
        *,
        fn,
        timeout_s: float,
        stall_s: float,
        stop_check,
        in_process: bool,
        poll_s: float,
        on_start,
        on_progress,
        queue_s: float,
        run_started: float,
        ctx,
        slot_path: str,
        on_warning=None,
        on_queue=None,
        child_locks: bool = False,
        wait_s: float = 0.0,
        slot_poll_s: float = GRID_SLOT_POLL_S,
        notice_s: float = 600.0):
    """Run the sweep. The child holds the OS lock unless in-process."""
    done = 0
    total = 0
    acquired = not child_locks
    last_progress_at = run_started

    def _progress() -> str:
        return f"{done}/{total}"

    def _elapsed() -> float:
        return max(0.0, time.monotonic() - run_started)

    def _stamp(result):
        # Before the lock is taken, the wait belongs in queue_s. After
        # it is taken, elapsed_s is the run and queue_s is the wait
        # recorded on the acquired message.
        if not acquired and isinstance(result, GridProcessFailure):
            waited = _elapsed()
            kept = float(result.queue_s or 0.0)
            return _stamp_failure(
                result,
                progress=_progress(),
                elapsed_s=0.0,
                queue_s=kept if kept > 0.0 else waited,
            )
        return _stamp_failure(
            result,
            progress=_progress(),
            elapsed_s=_elapsed(),
            queue_s=queue_s,
        )

    def _note(message) -> None:
        nonlocal done, total, last_progress_at
        try:
            done_i = int(message[1])
            total_i = int(message[2])
        except (TypeError, ValueError, IndexError):
            return
        done = done_i
        total = total_i
        last_progress_at = time.monotonic()
        if on_progress is not None:
            try:
                on_progress(done, total)
            except Exception:
                pass

    if in_process:
        if stop_check is not None and stop_check():
            return _stamp(GridProcessFailure("stopped", "stop requested"))
        if on_start is not None:
            on_start(None)
        result = _run_in_process(fn, job, on_progress)
        return _stamp(result)

    recv_conn, send_conn = ctx.Pipe(duplex=False)
    if child_locks:
        proc = ctx.Process(
            target=_locked_grid_child,
            args=(
                fn, job, send_conn, slot_path, float(wait_s),
                float(slot_poll_s), float(notice_s), os.getpid(),
                float(child_run_limit_s(timeout_s)),
            ),
            daemon=True,
            name="evo-grid",
        )
    else:
        proc = ctx.Process(
            target=fn, args=(job, send_conn), daemon=True, name="evo-grid")
    send_closed = False

    def _drain_buffered():
        """Return a finished payload already in the pipe, or None.

        Progress lines are consumed and do not hide a following ``ok``.
        A progress flood cannot extend the deadline: after a bounded
        number of progress messages the caller times out and discards
        whatever is not a finished result.
        """
        for _ in range(10000):
            kind, message = _poll_message(recv_conn, 0.0)
            if kind == "msg":
                action = _apply(message)
                if action == "continue":
                    continue
                if isinstance(action, GridProcessFailure):
                    return action
                return _decode_message(proc, message)
            if kind == "err":
                return _pipe_failed(proc, message)
            return None
        return None

    def _arm_queue() -> None:
        nonlocal queue_deadline, pre_event_deadline
        pre_event_deadline = None
        if queue_deadline is None:
            queue_deadline = time.monotonic() + float(wait_s)

    def _apply(message):
        """Consume progress and slot messages. ``result`` means finish."""
        nonlocal queue_s, run_started, last_progress_at, acquired
        if _is_progress(message):
            _note(message)
            return "continue"
        if not isinstance(message, tuple) or not message:
            return "result"
        tag = message[0]
        if tag == "self_timeout":
            return GridProcessFailure(
                "timeout",
                f"grid child exceeded its own limit; progress {_progress()}",
                proc.pid,
                progress=_progress(),
            )
        if tag == "slot_warn":
            if on_warning is not None and len(message) > 1:
                try:
                    on_warning(message[1])
                except Exception:
                    pass
            return "continue"
        if tag == "slot_done":
            _arm_queue()
            qkind = message[1] if len(message) > 1 else "queue_timeout"
            try:
                q_s = float(message[2]) if len(message) > 2 else 0.0
            except (TypeError, ValueError):
                q_s = 0.0
            queue_s = q_s
            detail = ("stop requested" if qkind == "stopped"
                      else f"grid queue exceeded {float(wait_s):g}s; "
                           "progress 0/0")
            return GridProcessFailure(
                qkind or "queue_timeout", detail, proc.pid,
                progress="0/0", elapsed_s=0.0, queue_s=q_s)
        if tag == "slot":
            _arm_queue()
            kind_name = message[1] if len(message) > 1 else ""
            fields = message[2] if len(message) > 2 and isinstance(
                message[2], dict) else {}
            if on_queue is not None:
                try:
                    on_queue(kind_name, **fields)
                except Exception:
                    pass
            if kind_name == "acquired" and not acquired:
                acquired = True
                try:
                    queue_s = float(fields.get("queue_s") or 0.0)
                except (TypeError, ValueError):
                    queue_s = 0.0
                run_started = time.monotonic()
                last_progress_at = run_started
                if on_start is not None:
                    on_start(proc.pid)
            return "continue"
        return "result"

    try:
        try:
            proc.start()
        except Exception as exc:
            return _stamp(GridProcessFailure(
                "crash",
                f"could not start grid child: {type(exc).__name__}: {exc}",
                proc.pid))
        try:
            send_conn.close()
            send_closed = True
        except Exception:
            pass
        # Stall and the grid cap start when the child takes the lock.
        # The queue cap starts at the child's first slot event, not at
        # proc.start(), so a slow spawn does not consume the wait.
        # A separate pre-event cap, max(120 s, stall_s), starts at
        # spawn and is cleared by that first event. It covers spawn
        # plus import only. A dead child with no slot event is still
        # an eof/crash.
        queue_deadline = None
        pre_event_limit = float(pre_slot_event_deadline_s(stall_s))
        pre_event_deadline = time.monotonic() + pre_event_limit
        poll = 0.05 if not poll_s or poll_s <= 0 else float(poll_s)
        while True:
            # STOP wins over a payload that has not been read yet, same
            # as killing a child that has not exited.
            if stop_check is not None and stop_check():
                _terminate(proc)
                return _stamp(GridProcessFailure(
                    "stopped", "stop requested", proc.pid))
            now = time.monotonic()
            if (not acquired and pre_event_deadline is not None
                    and now >= pre_event_deadline):
                found = _drain_buffered()
                if found is not None:
                    return _stamp(found)
                if not _alive(proc):
                    return _stamp(_no_result(proc))
                _terminate(proc)
                return _stamp(GridProcessFailure(
                    "timeout",
                    f"grid child sent no slot event within "
                    f"{pre_event_limit:g}s",
                    proc.pid,
                    progress="0/0",
                ))
            if acquired:
                deadline = run_started + float(timeout_s)
            elif queue_deadline is not None:
                deadline = queue_deadline
            else:
                deadline = None
            if deadline is not None and now >= deadline:
                # A finished ok that is already buffered still counts.
                # Progress does not extend the cap. Anything not yet a
                # full result is discarded.
                found = _drain_buffered()
                if found is not None:
                    return _stamp(found)
                _terminate(proc)
                if not acquired:
                    return _stamp(GridProcessFailure(
                        "queue_timeout",
                        f"grid queue exceeded {float(wait_s):g}s; "
                        "progress 0/0",
                        proc.pid,
                        progress="0/0",
                        queue_s=max(0.0, now - (queue_deadline - float(wait_s))),
                    ))
                return _stamp(GridProcessFailure(
                    "timeout",
                    f"grid child exceeded {float(timeout_s):g}s; "
                    f"progress {_progress()}",
                    proc.pid,
                    progress=_progress()))
            if (acquired and stall_s > 0
                    and (now - last_progress_at) >= float(stall_s)):
                kind, message = _poll_message(recv_conn, 0.0)
                if kind == "msg":
                    action = _apply(message)
                    if action == "continue":
                        continue
                    if isinstance(action, GridProcessFailure):
                        return _stamp(action)
                    return _stamp(_decode_message(proc, message))
                if kind == "err":
                    return _stamp(_pipe_failed(proc, message))
                if kind == "eof" or not _alive(proc):
                    found = _drain_buffered()
                    if found is not None:
                        return _stamp(found)
                    return _stamp(_no_result(proc))
                _terminate(proc)
                return _stamp(GridProcessFailure(
                    "stalled",
                    f"grid child stalled for {float(stall_s):g}s "
                    f"without progress; progress {_progress()}",
                    proc.pid,
                    progress=_progress()))
            if deadline is None:
                remaining = poll
            else:
                remaining = deadline - now
            if acquired and stall_s > 0:
                until_stall = float(stall_s) - (now - last_progress_at)
            else:
                until_stall = remaining
            slice_s = min(poll, remaining, max(0.0, until_stall))
            kind, message = _poll_message(recv_conn, slice_s)
            if kind == "msg":
                action = _apply(message)
                if action == "continue":
                    continue
                if isinstance(action, GridProcessFailure):
                    return _stamp(action)
                return _stamp(_decode_message(proc, message))
            if kind == "err":
                return _stamp(_pipe_failed(proc, message))
            if kind == "eof" or not _alive(proc):
                # The write end may have closed between the empty poll
                # and the liveness check. Drain what is already buffered
                # before calling the pipe empty. Progress lines in that
                # buffer must not hide the ok that follows them.
                if kind != "eof":
                    found = _drain_buffered()
                    if isinstance(found, GridProcessFailure):
                        return _stamp(found)
                    if found is not None:
                        return _stamp(found)
                return _stamp(_no_result(proc))
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
