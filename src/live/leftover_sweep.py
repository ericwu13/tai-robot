"""Detection-only leftover sweep. One CIM query on Windows; a list error skips.

Reports a live deploy pid that owns no ``.lock``. Does not stop it or
delete a lock. At most one scan per five minutes per deploy root.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time

from src.paths import skips_gui_and_com
from src.utils.file_lock import _lock_nb, _open_lock_fd, _unlock_fd
from .host_stall import deploy_root

logger = logging.getLogger(__name__)
LEFTOVER_SWEEP_INTERVAL_S = 300.0
STAMP_FILENAME = "leftover_sweep.json"
LOCK_FILENAME = ".leftover_sweep.lock"
_LIST_TIMEOUT_S = 15
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PS = (
    "Get-CimInstance Win32_Process -Filter "
    "\"Name='tai_backtest.exe' or Name='python.exe' or Name='pythonw.exe'\" "
    "| Select ProcessId,Name,CommandLine,CreationDate | ConvertTo-Json -Compress"
)
_PYTHON = frozenset({"python.exe", "pythonw.exe"})
_SCRIPTS = ("run_bot_cli.py", "run_backtest.py")
_LIVE_DIR = re.compile(r"data[/\\]+live[/\\]+([^/\\\"'\s]+)", re.IGNORECASE)
_CIM_DATE = re.compile(r"/Date\((-?\d+)")


def sweep_leftover_processes(bot_dir, *, alert_fn=None, list_processes=None,
                             now=None, log_fn=None) -> None:
    """Alert once per leftover pid. Never raises out of the watchdog poll."""
    if list_processes is None and os.name != "nt":
        return
    try:
        _sweep(bot_dir, alert_fn=alert_fn, log_fn=log_fn,
               list_processes=list_processes or _list_windows_processes,
               now=now or time.time)
    except Exception:
        logger.exception("leftover sweep failed")


def parse_cim_process_json(raw: str) -> list:
    """One CIM object, or a list of them. Blank text is no processes."""
    text = (raw or "").strip()
    if not text:
        return []
    data = json.loads(text)
    found = []
    for row in data if isinstance(data, list) else [data]:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("ProcessId"))
        except (TypeError, ValueError):
            continue
        found.append(type("P", (), {
            "pid": pid, "image": str(row.get("Name") or ""),
            "command_line": row.get("CommandLine"),
            "created_at": _created(row.get("CreationDate"))})())
    return found


def _list_windows_processes():
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _PS],
            capture_output=True, text=True, timeout=_LIST_TIMEOUT_S,
            creationflags=_NO_WINDOW, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(str(exc)) from exc
    if done.returncode != 0:
        raise OSError(f"process list status {done.returncode}")
    return parse_cim_process_json(done.stdout)


def _created(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _CIM_DATE.search(str(value))
    return int(match.group(1)) / 1000.0 if match else str(value)


def _sweep(bot_dir, *, alert_fn, list_processes, now, log_fn) -> None:
    root = deploy_root(bot_dir)
    if not root:
        return
    path, clock = os.path.join(root, STAMP_FILENAME), float(now())
    last, keys = _load_stamp(path)
    if last is not None and clock - last < LEFTOVER_SWEEP_INTERVAL_S:
        return
    fd = _try_lock(os.path.join(root, LOCK_FILENAME))
    if fd is None:
        return
    try:
        current = float(now())
        last, keys = _load_stamp(path)
        if last is not None and current - last < LEFTOVER_SWEEP_INTERVAL_S:
            return
        try:
            lock_pids, processes = _lock_pids(root), list(list_processes())
        except (OSError, ValueError):
            logger.warning("leftover sweep process list skipped")
            _save_stamp(path, current, keys)
            return
        seen = set(keys)
        for proc in processes:
            try:
                pid = int(proc.pid)
            except (TypeError, ValueError):
                continue
            key = (pid, _created(getattr(proc, "created_at", None)))
            if pid <= 0 or pid in lock_pids or key in seen or not _is_leftover(proc):
                continue
            where = _bot_dir_for_command(getattr(proc, "command_line", None), root)
            image = os.path.basename(str(getattr(proc, "image", "") or ""))
            message = (f"leftover process pid={pid} image={image} "
                       f"bot_dir={where or 'unknown'}")
            logger.warning("%s", message)
            for fn in (log_fn, alert_fn):
                if fn is None:
                    continue
                try:
                    fn(message)
                except Exception:
                    logger.exception("leftover sweep notify failed")
            seen.add(key)
            keys.append(key)
        _save_stamp(path, current, keys)
    finally:
        _release(fd)


def _load_stamp(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None, []
    if not isinstance(data, dict):
        return None, []
    try:
        last = float(data.get("last_scan"))
    except (TypeError, ValueError):
        last = None
    keys = []
    for row in data.get("alerted") or []:
        if not isinstance(row, (list, tuple)) or not row:
            continue
        try:
            keys.append((int(row[0]), _created(row[1] if len(row) > 1 else None)))
        except (TypeError, ValueError):
            continue
    return last, keys


def _save_stamp(path, last, keys) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"last_scan": last, "alerted": [list(item) for item in keys]}, handle)
    os.replace(tmp, path)


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
    for fn in (_unlock_fd, os.close):
        try:
            fn(fd)
        except OSError:
            pass


def _lock_pids(root: str) -> set[int]:
    """Pid text in each bot ``.lock``. Does not delete a stale lock."""
    pids = set()
    for entry in os.scandir(root):
        if not entry.is_dir():
            continue
        try:
            with open(os.path.join(entry.path, ".lock"), encoding="utf-8") as handle:
                pid = int(handle.read().strip())
        except (OSError, ValueError):
            continue
        if pid > 0:
            pids.add(pid)
    return pids


def _argv(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return command.split()


def _is_leftover(proc) -> bool:
    image = os.path.basename(str(getattr(proc, "image", "") or "")).lower()
    command = getattr(proc, "command_line", None)
    argv = _argv(str(command)) if command else []
    try:
        if argv and skips_gui_and_com(argv):
            return False
    except Exception:
        pass
    if image == "tai_backtest.exe":
        return True
    text = str(command or "")
    return image in _PYTHON and any(s in text.lower() for s in _SCRIPTS) and "deploy" in argv


def _bot_dir_for_command(command, root: str):
    if not command:
        return None
    argv, symbol, bot = _argv(str(command)), None, None
    for index, token in enumerate(argv):
        nxt = argv[index + 1] if index + 1 < len(argv) else None
        if token == "--symbol" and nxt:
            symbol = nxt
        elif token == "--bot" and nxt:
            bot = nxt
        elif token.startswith("--symbol="):
            symbol = token.split("=", 1)[1]
        elif token.startswith("--bot="):
            bot = token.split("=", 1)[1]
    if symbol and bot:
        from .live_runner import LiveRunner
        return LiveRunner.bot_dir_for(root, symbol, bot)
    match = _LIVE_DIR.search(str(command))
    return os.path.join(root, match.group(1)) if match else None
