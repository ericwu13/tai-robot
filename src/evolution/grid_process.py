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

One grid runs per install folder. The slot is
``project_root()/data/evo_grid.slot`` (or ``TAI_EVO_GRID_SLOT``).
Two installs side by side do not share it. It is not a machine-wide
path and it is not under ``%PROGRAMDATA%``. The parent acquires that
file before the clocks start, queues on this thread (never on Tk),
and releases the slot when the run ends. The child does not start a
worker pool.

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
GRID_SLOT_STALE_SLACK_S = 10 * 60
GRID_QUEUE_FACTOR = 3
GRID_SLOT_FILENAME = "evo_grid.slot"


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


def grid_slot_stale_s() -> int:
    """A slot older than the cap plus 10 min is taken over."""
    return GRID_TIMEOUT_CAP_S + GRID_SLOT_STALE_SLACK_S


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


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` is still a process. Never raises.

    ``os.kill(pid, 0)`` is not an existence check on Windows (signal 0
    is ``CTRL_C_EVENT``), so that platform uses ``OpenProcess``. A pid
    we cannot verify stays alive so a live holder is not stolen.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_win(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_alive_win(pid: int) -> bool:
    try:
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        still_active = 259
        handle = kernel.OpenProcess(access, False, pid)
        if not handle:
            # Access denied: the pid exists and is not ours.
            if ctypes.get_last_error() == 5:
                return True
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            if not ok:
                return True
            return int(code.value) == still_active
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return True


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


def _read_slot(path: str):
    """``(pid or None, timestamp or None)``. The timestamp is the older
    of the embedded clock and the file mtime.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    mtime = float(st.st_mtime)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read(256)
    except OSError:
        return None, mtime
    parts = text.split()
    pid = None
    stamp = mtime
    if parts:
        try:
            pid = int(parts[0])
        except ValueError:
            pid = None
    if len(parts) >= 2:
        try:
            stamp = float(parts[1])
        except ValueError:
            stamp = mtime
    if mtime < stamp:
        stamp = mtime
    return pid, stamp


def _slot_is_stale(pid, stamp, stale_s: float, now: float | None = None) -> bool:
    """True when the slot may be taken over.

    A readable dead pid is stale at any age. A live pid is stale only
    once it is older than ``stale_s`` (the cap plus 10 min). An empty
    or half-written file has no pid: a young one is busy (the holder
    is between create and write), and only a file older than that same
    guard is stale. No separate age constant.
    """
    current = time.time() if now is None else float(now)
    try:
        age = None if stamp is None else current - float(stamp)
    except (TypeError, ValueError):
        age = None
    if pid is None:
        if age is None:
            return False
        return age > float(stale_s)
    if not _pid_alive(pid):
        return True
    if age is None:
        return True
    return age > float(stale_s)


def _try_create_slot(path: str) -> bool:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        payload = f"{os.getpid()}\n{time.time():.6f}\n".encode("ascii")
        os.write(fd, payload)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(path)
        except OSError:
            pass
        return False
    try:
        os.close(fd)
    except OSError:
        pass
    return True


def _restore_slot(renamed: str, path: str) -> None:
    """Put a non-stale slot back without clobbering a new holder.

    Restoring is safer than deleting: the file we moved aside may be
    a live holder's slot. ``os.link`` fails when ``path`` already
    exists, so a slot created after the rename is left alone. If the
    link is not available and ``path`` is empty, ``os.replace`` moves
    it back. A path that reappeared is left busy, and the renamed copy
    of the non-stale slot is kept rather than deleted.
    """
    try:
        os.link(renamed, path)
    except FileExistsError:
        return
    except OSError:
        if os.path.exists(path):
            return
        try:
            os.replace(renamed, path)
        except OSError:
            return
        return
    try:
        os.remove(renamed)
    except OSError:
        pass


def _steal_stale_slot(path: str, stale_s: float) -> None:
    """Take over ``path`` only when the renamed copy is still stale.

    The file is renamed to ``<path>.<pid>.<random>`` before any delete.
    The pid and stamp are read again from that copy. Only then is the
    copy removed. A fresh slot that landed at ``path`` is never the
    file we delete. If the copy is no longer stale, it is restored
    when ``path`` is empty. A rename that fails (a Windows open handle)
    leaves ``path`` in place and does not delete it.
    """
    pid, stamp = _read_slot(path)
    if not _slot_is_stale(pid, stamp, stale_s):
        return
    holder = str(pid) if isinstance(pid, int) else "na"
    renamed = f"{path}.{holder}.{os.urandom(4).hex()}"
    try:
        os.replace(path, renamed)
    except OSError:
        return
    pid2, stamp2 = _read_slot(renamed)
    if not _slot_is_stale(pid2, stamp2, stale_s):
        _restore_slot(renamed, path)
        return
    try:
        os.remove(renamed)
    except OSError:
        pass


def acquire_grid_slot(
        path: str,
        *,
        stop_check=None,
        poll_s: float = GRID_SLOT_POLL_S,
        wait_s: float,
        stale_s: float,
        sleep=None):
    """Take the host slot. ``(owned, kind, queue_s)``.

    ``kind`` is ``""`` when this process holds the file, otherwise
    ``stopped`` or ``queue_timeout``. A free slot is taken even when
    stop is already set, so the caller can spawn and then honour STOP
    the way a grid with no queue does. Stop is noticed while waiting
    on a busy slot. A dead pid, or a file older than ``stale_s``, is
    taken over.
    """
    pause = time.sleep if sleep is None else sleep
    start = time.monotonic()
    deadline = start + max(0.0, float(wait_s))
    poll = 0.05 if poll_s is None or poll_s <= 0 else float(poll_s)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
    while True:
        if _try_create_slot(path):
            return True, "", time.monotonic() - start
        if stop_check is not None and stop_check():
            return False, "stopped", time.monotonic() - start
        pid, stamp = _read_slot(path)
        if _slot_is_stale(pid, stamp, stale_s):
            _steal_stale_slot(path, stale_s)
            if _try_create_slot(path):
                return True, "", time.monotonic() - start
        now = time.monotonic()
        if now >= deadline:
            return False, "queue_timeout", now - start
        pause(min(poll, max(0.0, deadline - now)))


def release_grid_slot(path: str, pid: int | None = None) -> None:
    """Remove the slot only when it still names ``pid``.

    Windows can refuse the delete while a waiter has the file open.
    Retry for about 2 s, re-checking the pid before each attempt. A
    remove that still fails is logged. It is not ignored.
    """
    import logging

    owner = os.getpid() if pid is None else int(pid)
    deadline = time.monotonic() + 2.0
    delay = 0.05
    last_exc: BaseException | None = None
    while True:
        current, _stamp = _read_slot(path)
        if current != owner:
            return
        try:
            os.remove(path)
            return
        except OSError as exc:
            last_exc = exc
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(delay, max(0.0, deadline - now)))
        delay = min(delay * 2, 0.5)
    logging.getLogger(__name__).warning(
        "grid slot release failed path=%s pid=%s [%s] %s",
        path,
        owner,
        type(last_exc).__name__ if last_exc is not None else "OSError",
        last_exc,
    )


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
        slot_stale_s: float | None = None):
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
    stale_s = float(grid_slot_stale_s() if slot_stale_s is None
                    else slot_stale_s)
    owned, qkind, queue_s = acquire_grid_slot(
        path,
        stop_check=stop_check,
        poll_s=slot_poll_s,
        wait_s=wait_s,
        stale_s=stale_s,
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
    run_started = time.monotonic()
    # Spawn, not fork: the parent already owns Tk and COM.
    ctx = multiprocessing.get_context("spawn")
    try:
        return _run_grid_held(
            job,
            fn=fn,
            timeout_s=float(timeout_s),
            stall_s=float(stall_s),
            stop_check=stop_check,
            in_process=in_process,
            poll_s=poll_s,
            on_start=on_start,
            on_progress=on_progress,
            queue_s=queue_s,
            run_started=run_started,
            ctx=ctx,
        )
    finally:
        release_grid_slot(path, os.getpid())


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
        ctx):
    """The sweep after this process holds the slot. Clocks already run."""
    done = 0
    total = 0
    last_progress_at = run_started

    def _progress() -> str:
        return f"{done}/{total}"

    def _elapsed() -> float:
        return max(0.0, time.monotonic() - run_started)

    def _stamp(result):
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
                if _is_progress(message):
                    _note(message)
                    continue
                return _decode_message(proc, message)
            if kind == "err":
                return _pipe_failed(proc, message)
            return None
        return None

    try:
        try:
            proc.start()
        except Exception as exc:
            return _stamp(GridProcessFailure(
                "crash",
                f"could not start grid child: {type(exc).__name__}: {exc}",
                proc.pid))
        if on_start is not None:
            on_start(proc.pid)
        try:
            send_conn.close()
            send_closed = True
        except Exception:
            pass
        # Clocks started at acquire, before spawn, so a slow start
        # counts against the stall and the overall cap.
        deadline = run_started + float(timeout_s)
        poll = 0.05 if not poll_s or poll_s <= 0 else float(poll_s)
        while True:
            # STOP wins over a payload that has not been read yet, same
            # as killing a child that has not exited.
            if stop_check is not None and stop_check():
                _terminate(proc)
                return _stamp(GridProcessFailure(
                    "stopped", "stop requested", proc.pid))
            now = time.monotonic()
            if now >= deadline:
                # A finished ok that is already buffered still counts.
                # Progress does not extend the cap. Anything not yet a
                # full result is discarded.
                found = _drain_buffered()
                if found is not None:
                    return _stamp(found)
                _terminate(proc)
                return _stamp(GridProcessFailure(
                    "timeout",
                    f"grid child exceeded {float(timeout_s):g}s; "
                    f"progress {_progress()}",
                    proc.pid,
                    progress=_progress()))
            if stall_s > 0 and (now - last_progress_at) >= float(stall_s):
                kind, message = _poll_message(recv_conn, 0.0)
                if kind == "msg":
                    if _is_progress(message):
                        _note(message)
                        continue
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
            remaining = deadline - now
            until_stall = (
                float(stall_s) - (now - last_progress_at)
                if stall_s > 0 else remaining)
            slice_s = min(poll, remaining, max(0.0, until_stall))
            kind, message = _poll_message(recv_conn, slice_s)
            if kind == "msg":
                if _is_progress(message):
                    _note(message)
                    continue
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
