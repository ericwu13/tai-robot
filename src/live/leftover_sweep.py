"""`.exiting` markers. The watchdog is the only deleter.

One bot scans the deploy root every 300s. ``HangWatchdog._poll`` has no
host stamp, so the stamp file stays. Creation time is Windows-only.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager

from src.utils.file_lock import _lock_nb, _open_lock_fd, _unlock_fd
from src.utils.fileio import read_json
from .host_stall import deploy_root

logger = logging.getLogger(__name__)

EXITING_FILENAME = ".exiting"
EXITING_ALERT_AFTER_S = 60.0
EXITING_SCAN_INTERVAL_S = 300.0
_STAMP_FILENAME = "exiting_scan.json"
_LOCK_FILENAME = ".exiting_scan.lock"


def write_exiting_marker(bot_dir: str, *, pid: int | None = None,
                         created_at: float | None = None,
                         written_at: float | None = None) -> None:
    """Atomic ``.exiting``. Never raises and never deletes the marker."""
    try:
        if not bot_dir:
            return
        this_pid = int(os.getpid() if pid is None else pid)
        payload = {
            "pid": this_pid,
            "created_at": (process_created_at(this_pid)
                           if created_at is None else created_at),
            "written_at": float(time.time() if written_at is None else written_at),
            "alerted": False,
        }
        if not _write_json(os.path.join(bot_dir, EXITING_FILENAME), payload):
            logger.warning("exiting marker not written for %s", bot_dir)
    except Exception:
        logger.warning("exiting marker not written for %s", bot_dir)


def process_created_at(pid: int) -> float | None:
    """Windows start time of one pid, or None."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    return _windows_created_at(pid) if pid > 0 and os.name == "nt" else None


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
    stamp_path = os.path.join(root, _STAMP_FILENAME)
    if _stamp_fresh(read_json(stamp_path), clock):
        return
    with _held_lock(os.path.join(root, _LOCK_FILENAME)) as fd:
        if fd is None:
            return
        clock = float(now())
        if _stamp_fresh(read_json(stamp_path), clock):
            return
        if not _write_json(stamp_path, {"last_scan": clock}):
            logger.warning("exiting scan stamp not written")
        if alive is None:
            from .live_runner import LiveRunner
            alive = LiveRunner._pid_alive
        creation_of = process_created_at if creation_of is None else creation_of
        for path in _marker_paths(root):
            _review_one(path, clock, alive, creation_of, alert_fn)


def _review_one(path, now, alive, creation_of, alert_fn) -> None:
    data = _load_marker(path)
    if data is None:
        logger.warning("exiting marker unreadable, removing it: %s", path)
        _delete_marker(path)
        return
    try:
        if not alive(data["pid"]):
            _delete_marker(path)
            return
        live_created = creation_of(data["pid"])
    except Exception:
        return
    saved = data.get("created_at")
    if not _same_creation(saved, live_created):
        if saved is not None and live_created is not None:
            _delete_marker(path)
        return
    age = now - float(data["written_at"])
    if age <= EXITING_ALERT_AFTER_S or data.get("alerted") is True:
        return
    data["alerted"] = True
    if not _write_json(path, data):
        return
    message = (f"exiting process still alive pid={data['pid']} "
               f"bot_dir={os.path.dirname(path)} age={age:.0f}s")
    logger.warning("%s", message)
    if alert_fn is None:
        return
    try:
        alert_fn(message)
    except Exception:
        logger.exception("exiting marker alert failed")


def _load_marker(path):
    data = read_json(path)
    if not isinstance(data, dict):
        return None
    try:
        pid, written = int(data["pid"]), float(data["written_at"])
    except (TypeError, ValueError, KeyError):
        return None
    if pid <= 0:
        return None
    data["pid"], data["written_at"] = pid, written
    return data


def _write_json(path, data) -> bool:
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
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
    try:
        return (left is not None and right is not None
                and abs(float(left) - float(right)) < 1.0)
    except (TypeError, ValueError):
        return False


def _marker_paths(root: str) -> list[str]:
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return [os.path.join(root, name, EXITING_FILENAME) for name in names
            if os.path.isfile(os.path.join(root, name, EXITING_FILENAME))]


def _stamp_fresh(data, now) -> bool:
    try:
        return now - float(data["last_scan"]) < EXITING_SCAN_INTERVAL_S
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


@contextmanager
def _held_lock(path):
    """Non-blocking ``file_lock``. Yields None when this process did not get it."""
    fd, held = None, False
    try:
        fd = _open_lock_fd(path)
        held = _lock_nb(fd)
    except OSError:
        held = False
    try:
        yield fd if held else None
    finally:
        if held:
            try:
                _unlock_fd(fd)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


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
            times = [wintypes.FILETIME() for _ in range(4)]
            kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [
                ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel.GetProcessTimes.restype = wintypes.BOOL
            if not kernel.GetProcessTimes(
                    handle, *(ctypes.byref(item) for item in times)):
                return None
            ticks = ((int(times[0].dwHighDateTime) << 32)
                     | int(times[0].dwLowDateTime))
            return None if ticks <= 0 else ticks / 10_000_000.0 - 11644473600.0
        finally:
            kernel.CloseHandle(handle)
    except Exception:
        return None
