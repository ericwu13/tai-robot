"""Cross-platform non-blocking OS file lock.

``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows. The operating
system drops the lock when the fd is closed or the process dies, so a
crash cannot leave a stale holder. The file bytes are not the lock.

Shared by the evolution grid slot and the headless relogin slot.
``src.live`` must not import ``src.evolution`` to reach this.
"""

from __future__ import annotations

import os


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
