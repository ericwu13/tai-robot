"""A bot that has released ``.lock`` and is still alive.

``release_lock`` writes ``bot_dir/.exiting`` and never removes it. The
hang watchdog is the only deleter. Once per five minutes one bot globs
every ``.exiting`` under the deploy root.

Dead pid, or a live pid with a different creation time: delete the
marker, no alert. The same process still alive more than 60s after
``written_at``: alert once and record that on the marker.
"""

from __future__ import annotations

import json
import logging
import os
import time

from src.utils.file_lock import _lock_nb, _open_lock_fd, _unlock_fd
from .host_stall import deploy_root

logger = logging.getLogger(__name__)

EXITING_FILENAME = ".exiting"
EXITING_ALERT_AFTER_S = 60.0
EXITING_SCAN_INTERVAL_S = 300.0
STAMP_FILENAME = "exiting_scan.json"
LOCK_FILENAME = ".exiting_scan.lock"


def write_exiting_marker(bot_dir: str, *, pid: int | None = None,
                         created_at: float | None = None,
                         written_at: float | None = None) -> None:
    """Atomic ``.exiting``. Never raises and never deletes the marker."""
    try:
        if not bot_dir:
            return
        os.makedirs(bot_dir, exist_ok=True)
        this_pid = int(os.getpid() if pid is None else pid)
        payload = {
            "pid": this_pid,
            "created_at": (process_created_at(this_pid)
                           if created_at is None else created_at),
            "written_at": float(time.time() if written_at is None else written_at),
            "alerted": False,
        }
        path = os.path.join(bot_dir, EXITING_FILENAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        logger.warning("exiting marker not written for %s", bot_dir)


def process_created_at(pid: int) -> float | None:
    """Unix start time of one pid, or None when it cannot be read."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_created_at(pid)
    return _linux_created_at(pid)


def check_exiting_markers(bot_dir: str, *, alert_fn=None, now=None,
                          alive=None, creation_of=None) -> None:
    """Review every ``.exiting`` under the deploy root. Never raises."""
    try:
        _check(bot_dir, alert_fn=alert_fn, now=time.time if now is None else now,
               alive=alive, creation_of=creation_of)
    except Exception:
        logger.exception("exiting marker check failed")


def _check(bot_dir, *, alert_fn, now, alive, creation_of) -> None:
    root = deploy_root(bot_dir)
    if not root or not os.path.isdir(root):
        return
    clock = float(now())
    stamp_path = os.path.join(root, STAMP_FILENAME)
    if _stamp_fresh(_load_stamp(stamp_path), clock):
        return
    fd = _try_lock(os.path.join(root, LOCK_FILENAME))
    if fd is None:
        return
    try:
        clock = float(now())
        if _stamp_fresh(_load_stamp(stamp_path), clock):
            return
        _save_stamp(stamp_path, clock)
        if alive is None:
            alive = _process_alive
        if creation_of is None:
            creation_of = process_created_at
        for path in _marker_paths(root):
            _review_one(path, clock, alive, creation_of, alert_fn)
    finally:
        _release(fd)


def _review_one(path, now, alive, creation_of, alert_fn) -> None:
    data = _load_marker(path)
    if data is None:
        logger.warning("exiting marker unreadable, removing it: %s", path)
        _delete_marker(path)
        return
    pid = data["pid"]
    try:
        still_alive = bool(alive(pid))
    except Exception:
        return
    if not still_alive:
        _delete_marker(path)
        return
    live_created = None
    try:
        live_created = creation_of(pid)
    except Exception:
        live_created = None
    if not _same_creation(data.get("created_at"), live_created):
        if data.get("created_at") is not None and live_created is not None:
            _delete_marker(path)
        return
    age = now - float(data["written_at"])
    if age <= EXITING_ALERT_AFTER_S or data.get("alerted") is True:
        return
    data["alerted"] = True
    if not _store_marker(path, data):
        return
    message = (f"exiting process still alive pid={pid} "
               f"bot_dir={os.path.dirname(path)} age={age:.0f}s")
    logger.warning("%s", message)
    if alert_fn is None:
        return
    try:
        alert_fn(message)
    except Exception:
        logger.exception("exiting marker alert failed")


def _load_marker(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        pid = int(data["pid"])
        written = float(data["written_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not isinstance(data, dict) or pid <= 0:
        return None
    data["pid"] = pid
    data["written_at"] = written
    return data


def _store_marker(path, data) -> bool:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _delete_marker(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _same_creation(left, right) -> bool:
    if left is None or right is None:
        return False
    try:
        return abs(float(left) - float(right)) < 1.0
    except (TypeError, ValueError):
        return False


def _marker_paths(root: str) -> list[str]:
    found = []
    try:
        names = os.listdir(root)
    except OSError:
        return found
    for name in names:
        path = os.path.join(root, name, EXITING_FILENAME)
        if os.path.isfile(path):
            found.append(path)
    return found


def _stamp_fresh(last, now) -> bool:
    return last is not None and now - last < EXITING_SCAN_INTERVAL_S


def _load_stamp(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return float(data["last_scan"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _save_stamp(path, when) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"last_scan": when}, handle)
        os.replace(tmp, path)
    except OSError:
        logger.warning("exiting scan stamp not written")


def _try_lock(path):
    fd = None
    try:
        fd = _open_lock_fd(path)
        if _lock_nb(fd):
            return fd
    except OSError:
        pass
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    return None


def _release(fd) -> None:
    try:
        _unlock_fd(fd)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _process_alive(pid: int) -> bool:
    """Whether ``pid`` exists. No signal and no terminate.

    Linux reads ``/proc``. Windows reuses ``LiveRunner._pid_alive``
    (open one process, read its exit code).
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        from .live_runner import LiveRunner
        return bool(LiveRunner._pid_alive(pid))
    return os.path.isdir(f"/proc/{pid}")


def _linux_created_at(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            rest = handle.read().rsplit(")", 1)[1].split()
        ticks = int(rest[19])
        btime = None
        with open("/proc/stat", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("btime "):
                    btime = int(line.split()[1])
                    break
        if btime is None:
            return None
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def _windows_created_at(pid: int) -> float | None:
    """Start time of one known pid. Not a process list."""
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [
                ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.GetProcessTimes.restype = wintypes.BOOL
            if not kernel.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exit_time),
                    ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            if ticks <= 0:
                return None
            return ticks / 10_000_000.0 - 11644473600.0
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return None
