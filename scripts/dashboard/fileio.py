"""File reads that stay out of a live bot's way.

On Windows a plain ``open()`` shares read and write access but NOT delete
access. While such a handle is open the bot's ``os.remove`` fails with a
sharing violation: the ``.lock`` removal on a clean stop, and
``save_session``'s remove-then-rename fallback (its first ``os.replace``
fails against ANY open handle on the target). So a plain-``open`` reader
caught at the wrong moment makes a session save raise.

``open_shared`` adds FILE_SHARE_DELETE. With the POSIX delete semantics
NTFS uses on Windows 10 1809+ (Steven: 19045) the remove unlinks the name
at once, the fallback rename lands, and we keep reading the old contents.
``os.replace`` itself still fails while we hold the handle, which is why
handles are held only for one read: the heartbeat writer retries the
replace for 35 ms. Off Windows it is plain ``open``.
"""

from __future__ import annotations

import json
import os

_GENERIC_READ = 0x80000000
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4          # READ | WRITE | DELETE
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32


def _open_shared_nt(path: str):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                       wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(path, _GENERIC_READ, _FILE_SHARE_ALL, None,
                    _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        err = ctypes.get_last_error()
        if err in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
            raise FileNotFoundError(err, "not found", path)
        if err in (_ERROR_ACCESS_DENIED, _ERROR_SHARING_VIOLATION):
            raise PermissionError(err, "access denied", path)
        raise OSError(err, "CreateFileW failed", path)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def open_shared(path: str):
    """Binary read handle that does not block the bot's replace/delete."""
    if os.name == "nt":
        return _open_shared_nt(os.fspath(path))
    return open(path, "rb")


def read_bytes(path: str, max_bytes: int | None = None) -> bytes | None:
    """Whole file, or its last ``max_bytes``. None when unreadable."""
    try:
        with open_shared(path) as f:
            if max_bytes is not None:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
            return f.read()
    except OSError:
        return None


def read_text(path: str, max_bytes: int | None = None) -> str | None:
    raw = read_bytes(path, max_bytes)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def read_json(path: str):
    """Parsed JSON, or None when missing, unreadable, or mid-write garbage."""
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def tail_lines(path: str, n: int, max_bytes: int = 256_000) -> list[str]:
    """Last ``n`` complete lines. A partial first line from the seek is dropped."""
    raw = read_bytes(path, max_bytes)
    if not raw:
        return []
    truncated = False
    try:
        truncated = os.path.getsize(path) > len(raw)
    except OSError:
        pass
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if truncated and lines:
        lines = lines[1:]
    return lines[-n:] if n else []
