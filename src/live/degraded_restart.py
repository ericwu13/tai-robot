"""One headless restart per open session when the quote link is a zombie.

A bot is restarted only while its session is open, outside the last
``TickWatchdog.NEAR_CLOSE_SUPPRESS`` minutes, ``DEGRADED``, and with no
fresh quote tick for ``restart_after_s`` (default 300s). The fresh-tick
clock is ``BacktestApp._last_fresh_tick_at`` (wall clock), never the
watchdog's ``last_tick_time``. A fill in flight defers the restart and
does not consume the once-per-session budget. ``semi_auto`` alerts once
and is not restarted.

The budget lives in ``{bot_dir}/auto_restart.json``. The relogin slot is
an OS file lock under the deploy root so a crash cannot leave a holder.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.utils.file_lock import _lock_nb, _open_lock_fd, _unlock_fd

from .host_stall import deploy_root
from .live_runner import LiveRunner, minutes_until_session_close, session_for
from .session_store import load_session, save_session
from .tick_watchdog import TickWatchdog

_TZ_TAIPEI = timezone(timedelta(hours=8))

AUTO_RESTART_FILENAME = "auto_restart.json"
RELOGIN_SLOT_NAME = ".relogin.slot"
DEFAULT_RESTART_AFTER_S = 300.0
# One login slot for the whole fleet: 13 bots, 45s each.
DEFAULT_RELOGIN_WAIT_S = 13 * 45
PID_WAIT_S = 60.0

# path -> fd this process holds. The OS drops it when the fd closes.
_HELD_SLOTS: dict[str, int] = {}


@dataclass(frozen=True)
class RestartDecision:
    session_key: str


@dataclass(frozen=True)
class AlertOnly:
    """Already restarted this session, or semi_auto. Do not spawn."""

    session_key: str
    reason: str


@dataclass(frozen=True)
class Defer:
    reason: str


@dataclass(frozen=True)
class Noop:
    reason: str = ""


def _taipei_now() -> datetime:
    return datetime.now(_TZ_TAIPEI)


def auto_restart_path(bot_dir: str) -> str:
    return os.path.join(bot_dir, AUTO_RESTART_FILENAME)


def save_auto_restart(bot_dir: str, session_key: str, *,
                      old_pid: int | None = None,
                      pending_notify: bool = True,
                      at: str | None = None) -> dict:
    """Atomically write the once-per-session marker (``os.replace``)."""
    payload = {
        "session_key": session_key,
        "at": at or _taipei_now().isoformat(timespec="seconds"),
        "old_pid": int(old_pid if old_pid is not None else os.getpid()),
        "pending_notify": bool(pending_notify),
    }
    os.makedirs(bot_dir, exist_ok=True)
    save_session(auto_restart_path(bot_dir), payload)
    return payload


def load_auto_restart(bot_dir: str) -> dict | None:
    data = load_session(auto_restart_path(bot_dir))
    if not isinstance(data, dict):
        return None
    return data


def clear_pending_notify(bot_dir: str) -> dict | None:
    """Flip ``pending_notify`` to False. ``session_key`` stays.

    Returns the record that was pending, or None when there is nothing
    to announce. A second call returns None and does not rewrite a
    record that is already clear, so the session budget stays consumed.
    """
    data = load_auto_restart(bot_dir)
    if data is None or data.get("pending_notify") is not True:
        return None
    announced = dict(data)
    data["pending_notify"] = False
    save_session(auto_restart_path(bot_dir), data)
    return announced


def relogin_slot_path(bot_dir: str) -> str | None:
    root = deploy_root(bot_dir)
    if not root:
        return None
    return os.path.join(root, RELOGIN_SLOT_NAME)


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def acquire_relogin_slot(bot_dir: str, *, wait_s: float = DEFAULT_RELOGIN_WAIT_S,
                         poll_s: float = 0.25, sleep=None,
                         monotonic=None) -> bool:
    """Take the host relogin lock. False when ``wait_s`` elapses.

    Each caller opens its own fd. A second thread in this process waits
    like any other process. Callers release with
    :func:`release_relogin_slot`.
    """
    path = relogin_slot_path(bot_dir)
    if not path:
        return False
    key = os.path.abspath(path)
    pause = time.sleep if sleep is None else sleep
    clock = time.monotonic if monotonic is None else monotonic
    deadline = clock() + max(0.0, float(wait_s))
    poll = 0.05 if poll_s is None or float(poll_s) <= 0 else float(poll_s)
    fd = _open_lock_fd(path)
    try:
        while True:
            if _lock_nb(fd):
                _HELD_SLOTS[key] = fd
                return True
            now = clock()
            if now >= deadline:
                _close_fd(fd)
                return False
            pause(min(poll, max(0.0, deadline - now)))
    except BaseException:
        if _HELD_SLOTS.get(key) != fd:
            _close_fd(fd)
        raise


def release_relogin_slot(bot_dir: str) -> None:
    """Drop the slot this process holds. A second call is a no-op."""
    path = relogin_slot_path(bot_dir)
    if not path:
        return
    key = os.path.abspath(path)
    fd = _HELD_SLOTS.pop(key, None)
    if fd is None:
        return
    try:
        _unlock_fd(fd)
    except OSError:
        pass
    _close_fd(fd)


def wait_for_old_pid(pid: int, *, timeout_s: float = PID_WAIT_S,
                     poll_s: float = 0.25, sleep=None, monotonic=None,
                     alive=None) -> bool:
    """True once ``LiveRunner._pid_alive(pid)`` is false. False on timeout."""
    pid = int(pid)
    if pid <= 0:
        return True
    check = LiveRunner._pid_alive if alive is None else alive
    pause = time.sleep if sleep is None else sleep
    clock = time.monotonic if monotonic is None else monotonic
    deadline = clock() + max(0.0, float(timeout_s))
    poll = 0.05 if poll_s is None or float(poll_s) <= 0 else float(poll_s)
    while True:
        if not check(pid):
            return True
        now = clock()
        if now >= deadline:
            return False
        pause(min(poll, max(0.0, deadline - now)))


def _snapshot_degraded(app) -> bool:
    host = getattr(app, "_link_host", None)
    snap = getattr(host, "snapshot", None)
    if not callable(snap):
        return False
    try:
        data = snap()
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return bool(data.get("degraded"))


def _fresh_tick_at(app) -> float:
    try:
        return float(getattr(app, "_last_fresh_tick_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _fill_is_pending(app) -> bool:
    guard = getattr(app, "_trading_guard", None)
    if getattr(guard, "fill_pending", False):
        return True
    poller = getattr(app, "_fill_poller", None)
    if getattr(poller, "active", False):
        return True
    return getattr(app, "_exit_fill_retry_ctx", None) is not None


class DegradedRestartPolicy:
    """Injectable clock. ``degraded_open_since`` is the only timer."""

    def __init__(self, *, restart_after_s: float = DEFAULT_RESTART_AFTER_S,
                 now=None, now_dt=None, session_for_fn=None,
                 minutes_left_fn=None):
        self.restart_after_s = float(restart_after_s)
        self.degraded_open_since: float | None = None
        self._now = now or time.time
        self._now_dt = now_dt or _taipei_now
        self._session_for = session_for_fn or session_for
        self._minutes_left = minutes_left_fn or minutes_until_session_close
        self._alerted: set[tuple[str, str]] = set()

    def observe_app(self, app, *, bot_dir: str):
        now = float(self._now())
        if not _snapshot_degraded(app):
            self.degraded_open_since = None
            return Noop("not_degraded")
        session_key = self._session_for(self._now_dt())
        order_symbol = None
        runner = getattr(app, "_live_runner", None)
        watchdog = getattr(app, "_tick_watchdog", None)
        if watchdog is not None and getattr(watchdog, "order_symbol", None):
            order_symbol = watchdog.order_symbol
        elif runner is not None:
            order_symbol = getattr(runner, "symbol", None)
        try:
            mins_left = self._minutes_left(order_symbol)
        except TypeError:
            mins_left = self._minutes_left()
        near = TickWatchdog.NEAR_CLOSE_SUPPRESS
        if session_key is None or mins_left is None or mins_left <= near:
            self.degraded_open_since = None
            return Noop("closed_or_near_close")
        fresh = _fresh_tick_at(app)
        if (self.degraded_open_since is not None
                and fresh > self.degraded_open_since):
            # A tick after the clock started means the quote is alive,
            # even if the link machine is still DEGRADED (reply-only).
            # Do not re-arm in this same call: the next poll starts a
            # new 300s wait, so the open after an overnight DEGRADED
            # cannot restart on the first tick.
            self.degraded_open_since = None
            return Noop("fresh_tick")
        if self.degraded_open_since is None:
            self.degraded_open_since = now
            return Noop("clock_started")
        if now - self.degraded_open_since < self.restart_after_s:
            return Noop("waiting")
        if _fill_is_pending(app):
            return Defer("fill_pending")
        mode = str(getattr(app, "_trading_mode", "") or "")
        if mode != "paper" and mode != "auto":
            # semi_auto, and any other mode, is not spawned. The child
            # parser only accepts paper and auto, so this cannot escalate.
            reason = "semi_auto" if mode == "semi_auto" else "not_restartable"
            return self._alert_once(session_key, reason)
        recorded = load_auto_restart(bot_dir)
        if recorded is not None and recorded.get("session_key") == session_key:
            return self._alert_once(session_key, "already_restarted")
        return RestartDecision(session_key)

    def _alert_once(self, session_key: str, reason: str):
        key = (session_key, reason)
        if key in self._alerted:
            return Noop(reason)
        self._alerted.add(key)
        return AlertOnly(session_key, reason)
