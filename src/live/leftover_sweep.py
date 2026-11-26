"""Detection-only sweep for a live deploy process that no longer owns a ``.lock``.

``LiveRunner.stop`` releases the lock before LeaveMonitor, so a process
stuck on the way out is invisible to ``status`` / ``list``. The hang
watchdog already runs in every live bot, so the scan lives on its poll.
One bot under ``deploy_root`` scans, at most once per
``LEFTOVER_SWEEP_INTERVAL_S``, behind a non-blocking lock. The sweep
reports the pid. It does not stop the process and it does not remove a
lock file.

Windows lists ``tai_backtest.exe``, ``python.exe``, and ``pythonw.exe``.
Other ``os.name`` values no-op unless a test passes ``list_processes``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time

from src.paths import skips_gui_and_com
from src.utils.file_lock import _lock_nb, _open_lock_fd, _unlock_fd

from .host_stall import deploy_root

logger = logging.getLogger(__name__)

LEFTOVER_SWEEP_INTERVAL_S = 300.0
STAMP_FILENAME = "leftover_sweep.json"
LOCK_FILENAME = ".leftover_sweep.lock"

_PYTHON_IMAGES = frozenset({"python.exe", "pythonw.exe"})
_EXE_IMAGE = "tai_backtest.exe"
_DEPLOY_SCRIPTS = ("run_bot_cli.py", "run_backtest.py")
_LIVE_DIR = re.compile(r"data[/\\]+live[/\\]+([^/\\\"'\s]+)", re.IGNORECASE)


def sweep_leftover_processes(
    bot_dir: str,
    *,
    alert_fn=None,
    list_processes=None,
    now=None,
    log_fn=None,
) -> None:
    """Alert once per leftover pid. Never raises out of the watchdog poll.

    ``list_processes`` injects a fake process list. Without it, non-Windows
    hosts return before any disk or process work.
    """
    if list_processes is None and os.name != "nt":
        return
    try:
        _sweep(
            bot_dir,
            alert_fn=alert_fn,
            list_processes=list_processes,
            now=time.time if now is None else now,
            log_fn=log_fn,
        )
    except Exception:
        logger.exception("leftover sweep failed")


def _sweep(bot_dir, *, alert_fn, list_processes, now, log_fn) -> None:
    root = deploy_root(bot_dir)
    if not root:
        return
    lister = list_processes if list_processes is not None else _list_windows_processes
    stamp_path = os.path.join(root, STAMP_FILENAME)
    if _stamp_is_fresh(_read_json(stamp_path), float(now())):
        return
    fd = _try_lock(os.path.join(root, LOCK_FILENAME))
    if fd is None:
        return
    try:
        current = float(now())
        data = _read_json(stamp_path)
        if _stamp_is_fresh(data, current):
            return
        try:
            lock_pids = _lock_pids(root)
        except OSError:
            logger.warning("leftover sweep could not read lock files")
            _write_json(stamp_path, {
                "scanned_at": current,
                "alerted": _alerted_rows(data),
            })
            return
        try:
            processes = list(lister())
        except Exception:
            logger.warning("leftover sweep process list failed")
            # Stamp anyway so a broken listing is not retried every poll.
            _write_json(stamp_path, {
                "scanned_at": current,
                "alerted": _alerted_rows(data),
            })
            return
        seen = _alerted_keys(data)
        rows = _alerted_rows(data)
        for proc in processes:
            pid = _pid_of(proc)
            if pid is None or pid in lock_pids:
                continue
            if not _is_leftover(proc):
                continue
            created = _created_at(proc)
            key = (pid, created)
            if key in seen:
                continue
            where = _bot_dir_for_command(
                getattr(proc, "command_line", None), root)
            message = (
                f"leftover process pid={pid} "
                f"image={_image_name(proc)} "
                f"bot_dir={where or 'unknown'}"
            )
            _emit(message, alert_fn, log_fn)
            seen.add(key)
            rows.append({"pid": pid, "created_at": created})
        _write_json(stamp_path, {"scanned_at": current, "alerted": rows})
    finally:
        _release(fd)


def _emit(message: str, alert_fn, log_fn) -> None:
    logger.warning("%s", message)
    if log_fn is not None:
        try:
            log_fn(message)
        except Exception:
            logger.exception("leftover sweep log failed")
    if alert_fn is None:
        return
    try:
        alert_fn(message)
    except Exception:
        logger.exception("leftover sweep alert failed")


def _stamp_is_fresh(data, now: float) -> bool:
    if not isinstance(data, dict):
        return False
    try:
        scanned = float(data.get("scanned_at"))
    except (TypeError, ValueError):
        return False
    return now - scanned < LEFTOVER_SWEEP_INTERVAL_S


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))
        handle.write("\n")
    os.replace(tmp, path)


def _alerted_rows(data) -> list:
    rows = data.get("alerted") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _alerted_keys(data) -> set:
    keys = set()
    for row in _alerted_rows(data):
        try:
            pid = int(row.get("pid"))
        except (TypeError, ValueError):
            continue
        created = row.get("created_at")
        if created is None:
            keys.add((pid, None))
            continue
        try:
            keys.add((pid, float(created)))
        except (TypeError, ValueError):
            keys.add((pid, None))
    return keys


def _try_lock(path: str):
    try:
        fd = _open_lock_fd(path)
    except OSError:
        return None
    try:
        if _lock_nb(fd):
            return fd
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    return None


def _release(fd: int) -> None:
    try:
        _unlock_fd(fd)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _lock_pids(root: str) -> set[int]:
    """Pid text in each bot ``.lock``. Does not delete a stale lock."""
    pids: set[int] = set()
    for entry in os.scandir(root):
        if not entry.is_dir():
            continue
        lock_path = os.path.join(entry.path, ".lock")
        try:
            with open(lock_path, encoding="utf-8") as handle:
                text = handle.read().strip()
            pid = int(text)
        except (OSError, ValueError):
            continue
        if pid > 0:
            pids.add(pid)
    return pids


def _pid_of(proc):
    try:
        pid = int(getattr(proc, "pid"))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _created_at(proc):
    created = getattr(proc, "created_at", None)
    if created is None:
        return None
    try:
        return float(created)
    except (TypeError, ValueError):
        return None


def _image_name(proc) -> str:
    return os.path.basename(str(getattr(proc, "image", "") or ""))


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return command.split()


def _is_leftover(proc) -> bool:
    image = _image_name(proc).lower()
    command = getattr(proc, "command_line", None)
    if command:
        try:
            skip_child = skips_gui_and_com(_split_command(str(command)))
        except Exception:
            skip_child = False
        if skip_child:
            return False
    if image == _EXE_IMAGE:
        return True
    if image not in _PYTHON_IMAGES:
        return False
    if not command:
        return False
    text = str(command)
    lowered = text.lower()
    if not any(script in lowered for script in _DEPLOY_SCRIPTS):
        return False
    return "deploy" in _split_command(text)


def _bot_dir_for_command(command, root: str) -> str | None:
    if not command:
        return None
    text = str(command)
    argv = _split_command(text)
    symbol = _option(argv, "--symbol")
    bot = _option(argv, "--bot")
    if symbol and bot:
        from .live_runner import LiveRunner
        return LiveRunner.bot_dir_for(root, symbol, bot)
    match = _LIVE_DIR.search(text)
    if match:
        return os.path.join(root, match.group(1))
    return None


def _option(argv: list[str], name: str) -> str | None:
    for index, token in enumerate(argv):
        if token == name and index + 1 < len(argv):
            return argv[index + 1]
        prefix = name + "="
        if token.startswith(prefix):
            return token[len(prefix):]
    return None


def _list_windows_processes():
    """Image, command line, and creation time for the three deploy images.

    A snapshot or walk failure raises so the caller skips the scan.
    One process whose command line cannot be read is kept with an empty
    command line; Python rows then fail the deploy filter and are not
    alerted.
    """
    if os.name != "nt":
        raise OSError("process listing is only available on Windows")
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        # Default alignment inserts the pad before ULONG_PTR.
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.Process32FirstW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]

    snap = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snap or snap == invalid:
        raise OSError("process snapshot failed")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel.Process32FirstW(snap, ctypes.byref(entry)):
            err = ctypes.get_last_error()
            raise OSError(f"process walk failed ({err})")
        wanted = _PYTHON_IMAGES | {_EXE_IMAGE}
        found = []
        while True:
            image = str(entry.szExeFile or "")
            if os.path.basename(image).lower() in wanted:
                pid = int(entry.th32ProcessID)
                command, created = _windows_command_and_created(kernel, pid)
                found.append(_Process(pid, image, command, created))
            if not kernel.Process32NextW(snap, ctypes.byref(entry)):
                break
        return found
    finally:
        kernel.CloseHandle(snap)


class _Process:
    def __init__(self, pid, image, command_line, created_at):
        self.pid = pid
        self.image = image
        self.command_line = command_line
        self.created_at = created_at


def _windows_command_and_created(kernel, pid: int):
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None, None
    try:
        return _command_line(handle), _creation_unix(kernel, handle)
    except Exception:
        return None, None
    finally:
        kernel.CloseHandle(handle)


def _creation_unix(kernel, handle):
    import ctypes
    from ctypes import wintypes

    created = wintypes.FILETIME()
    exited = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()
    kernel.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    if not kernel.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel_time), ctypes.byref(user_time)):
        return None
    ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    if ticks <= 0:
        return None
    return ticks / 10_000_000.0 - 11644473600.0


def _command_line(handle):
    """Best-effort command line. ``None`` when the query does not fit the buffer."""
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtQueryInformationProcess.restype = ctypes.c_ulong
    # ProcessCommandLineInformation = 60. The string must sit inside the
    # buffer we allocated; anything else is ignored.
    process_command_line = 60
    mismatch = 0xC0000004
    size = 4096
    for _attempt in range(2):
        buf = ctypes.create_string_buffer(size)
        needed = wintypes.ULONG(0)
        status = ntdll.NtQueryInformationProcess(
            handle, process_command_line, buf, size, ctypes.byref(needed))
        if status == mismatch and needed.value > size:
            size = int(needed.value) + 2
            continue
        if status != 0:
            return None
        raw = bytes(buf)
        length = int.from_bytes(raw[:2], "little")
        if length <= 0:
            return None
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            ptr = int.from_bytes(raw[8:16], "little")
        else:
            ptr = int.from_bytes(raw[4:8], "little")
        base = ctypes.addressof(buf)
        if ptr < base or ptr + length > base + size:
            return None
        return ctypes.wstring_at(ptr, length // 2)
    return None
