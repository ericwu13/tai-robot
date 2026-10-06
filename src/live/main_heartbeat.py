"""Main-thread heartbeat and hung-but-alive watchdog (issue #157 phase 2).

The Tk main thread is also the broker message pump. A COM call that never
returns (LeaveMonitor in the 0422 incident) leaves the process alive, so a
PID check still says ALIVE. This module:

- writes ``heartbeat.json`` atomically, and only from the main thread
- stamps ``inflight_com_call`` around broker/COM calls
- runs a daemon watchdog that dumps stacks, writes ``hang.json``, and
  sends one Discord P1 per hang episode

The watchdog never exits, kills, or restarts the process. Re-arm happens
only after the heartbeat recovers.
"""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager

logger = logging.getLogger(__name__)

HEARTBEAT_FILENAME = "heartbeat.json"
HANG_FILENAME = "hang.json"
STACKS_FILENAME = "hang_stacks.txt"

# In-process defaults. check_bots has its own (slower) external thresholds.
# Tests inject smaller values so a blocked call is detected quickly.
DEFAULT_HUNG_S = 90.0
DEFAULT_INFLIGHT_HUNG_S = 60.0
DEFAULT_POLL_S = 2.0
HEARTBEAT_INTERVAL_MS = 5000

# Park an idle watchdog for this long so a finished test does not spin.
# The thread stays alive — it does not exit.
_IDLE_PARK_S = 3600.0


class MainThreadHeartbeatError(RuntimeError):
    """Raised when a background thread tries to write heartbeat.json."""


def _assert_main_thread() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise MainThreadHeartbeatError(
            "heartbeat.json can only be written on the Tk main thread "
            f"(caller={threading.current_thread().name!r})")


def atomic_write_json(path: str, payload: dict) -> None:
    """Write JSON via a temp file in the same directory plus os.replace."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hb-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def dump_thread_stacks(path: str) -> None:
    """Dump every thread stack into ``path``.

    ``faulthandler.dump_traceback`` is the primary dump. ``sys._current_frames``
    is written as well so a thread faulthandler skipped is still captured.
    """
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"pid={os.getpid()} ts={time.time():.3f}\n")
        try:
            faulthandler.dump_traceback(handle, all_threads=True)
        except Exception as exc:  # noqa: BLE001
            handle.write(f"faulthandler.dump_traceback failed: {exc}\n")
        frames = sys._current_frames()
        for tid, frame in frames.items():
            handle.write(f"\n--- thread {tid} ---\n")
            handle.write("".join(traceback.format_stack(frame)))


class HeartbeatWriter:
    """Owns one bot directory's heartbeat.json. Writes are main-thread only."""

    def __init__(self, bot_dir: str):
        self.bot_dir = bot_dir
        self._inflight: dict | None = None

    @property
    def path(self) -> str:
        return os.path.join(self.bot_dir, HEARTBEAT_FILENAME)

    def write(self, now: float | None = None) -> None:
        """Atomically publish ts, pid, and the current inflight COM call.

        ``now`` overrides the timestamp (tests). A background thread raises
        ``MainThreadHeartbeatError`` and does not create the file.
        """
        _assert_main_thread()
        payload = {
            "ts": time.time() if now is None else float(now),
            "pid": os.getpid(),
            "inflight_com_call": self._inflight,
        }
        atomic_write_json(self.path, payload)

    def begin_inflight(self, name: str) -> None:
        """Persist the in-flight call BEFORE the COM call can block."""
        self._inflight = {"name": str(name), "start_ts": time.time()}
        self.write()

    def end_inflight(self) -> None:
        self._inflight = None
        self.write()

    def delete(self) -> None:
        """Remove heartbeat.json. Main-thread only; missing file is fine."""
        _assert_main_thread()
        try:
            os.remove(self.path)
        except OSError:
            pass

    @contextmanager
    def inflight(self, name: str):
        self.begin_inflight(name)
        try:
            yield
        finally:
            self.end_inflight()


def read_heartbeat(path: str) -> dict | None:
    """Return the heartbeat object, or None if it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _inflight_age(hb: dict, now: float) -> tuple[dict | None, float | None]:
    raw = hb.get("inflight_com_call")
    if not isinstance(raw, dict):
        return None, None
    try:
        start = float(raw.get("start_ts"))
    except (TypeError, ValueError):
        return raw, None
    return raw, now - start


def _heartbeat_age(hb: dict, now: float) -> float | None:
    try:
        return now - float(hb.get("ts"))
    except (TypeError, ValueError):
        return None


class HangWatchdog:
    """Daemon thread. Reports a hang. Never exits, kills, or restarts."""

    def __init__(self, bot_dir: str, *, hung_s: float = DEFAULT_HUNG_S,
                 inflight_hung_s: float = DEFAULT_INFLIGHT_HUNG_S,
                 poll_s: float = DEFAULT_POLL_S, alert_fn=None):
        self.bot_dir = bot_dir
        self.hung_s = float(hung_s)
        self.inflight_hung_s = float(inflight_hung_s)
        self.poll_s = float(poll_s)
        self.alert_fn = alert_fn
        self._latched = False
        self._enabled = True
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._enabled = True
            self._wake.set()
            return
        self._enabled = True
        self._thread = threading.Thread(
            target=self._loop, name="hang-watchdog", daemon=True)
        self._thread.start()

    def disable(self) -> None:
        """Stop checking, but do not let the thread return."""
        self._enabled = False
        self._wake.set()

    def configure(self, bot_dir: str, *, hung_s: float | None = None,
                  inflight_hung_s: float | None = None,
                  poll_s: float | None = None, alert_fn=None) -> None:
        self.bot_dir = bot_dir
        if hung_s is not None:
            self.hung_s = float(hung_s)
        if inflight_hung_s is not None:
            self.inflight_hung_s = float(inflight_hung_s)
        if poll_s is not None:
            self.poll_s = float(poll_s)
        if alert_fn is not None:
            self.alert_fn = alert_fn
        self._enabled = True
        self._latched = False
        self._wake.set()

    def _loop(self) -> None:
        # Intentional infinite loop. A hang must not take the process down,
        # and stopping the bot must not kill this thread either.
        while True:
            enabled = self._enabled and bool(self.bot_dir)
            if enabled:
                try:
                    self._check_once()
                except Exception:
                    logger.exception("hang watchdog check failed")
            self._wake.wait(self.poll_s if enabled else _IDLE_PARK_S)
            self._wake.clear()

    def _check_once(self) -> None:
        path = os.path.join(self.bot_dir, HEARTBEAT_FILENAME)
        hb = read_heartbeat(path)
        if hb is None:
            # Missing file = not armed (clean stop, or not deployed yet).
            self._latched = False
            return
        now = time.time()
        age = _heartbeat_age(hb, now)
        inflight, inflight_age = _inflight_age(hb, now)
        hung_heartbeat = age is not None and age > self.hung_s
        hung_inflight = (inflight_age is not None
                         and inflight_age > self.inflight_hung_s)
        if not hung_heartbeat and not hung_inflight:
            self._latched = False
            return
        if self._latched:
            return
        # Latch BEFORE the alert so a slow send cannot double-fire, and a
        # second poll during the dump still counts as the same episode.
        self._latched = True
        reason = "inflight_com_call" if hung_inflight else "heartbeat"
        stacks_path = os.path.join(self.bot_dir, STACKS_FILENAME)
        dump_thread_stacks(stacks_path)
        hang = {
            "ts": now,
            "pid": os.getpid(),
            "reason": reason,
            "heartbeat_age_s": age,
            "inflight_com_call": inflight,
            "stacks_file": STACKS_FILENAME,
        }
        atomic_write_json(os.path.join(self.bot_dir, HANG_FILENAME), hang)
        if self.alert_fn is None:
            return
        name = ""
        if isinstance(inflight, dict):
            name = str(inflight.get("name") or "")
        age_txt = f"{age:.0f}" if age is not None else "?"
        message = (
            f"🚨 **P1 HUNG** `{os.path.basename(self.bot_dir)}` "
            f"main thread heartbeat age={age_txt}s "
            f"inflight_com_call={name or 'none'} ({reason}). "
            f"Process was NOT killed or restarted."
        )
        try:
            self.alert_fn(message)
        except Exception:
            logger.exception("hang watchdog alert failed")


# Process-wide install used by the Tk app. Tests may bind their own pair.
_writer: HeartbeatWriter | None = None
_watchdog: HangWatchdog | None = None
_guard = threading.Lock()


def start(bot_dir: str, alert_fn=None, *, hung_s: float = DEFAULT_HUNG_S,
          inflight_hung_s: float = DEFAULT_INFLIGHT_HUNG_S,
          poll_s: float = DEFAULT_POLL_S) -> HeartbeatWriter:
    """Install the writer, publish one heartbeat, and start the watchdog.

    Must run on the Tk main thread (the first write asserts it). Calling
    ``start`` again retargets the same watchdog thread — it does not spawn
    a second one and it does not restart the process.
    """
    global _writer, _watchdog
    writer = HeartbeatWriter(bot_dir)
    writer.write()
    with _guard:
        _writer = writer
        if _watchdog is None:
            _watchdog = HangWatchdog(
                bot_dir, hung_s=hung_s, inflight_hung_s=inflight_hung_s,
                poll_s=poll_s, alert_fn=alert_fn)
            _watchdog.start()
        else:
            _watchdog.configure(
                bot_dir, hung_s=hung_s, inflight_hung_s=inflight_hung_s,
                poll_s=poll_s, alert_fn=alert_fn)
    return writer


def tick() -> None:
    """One main-loop heartbeat. No-op when the bot is not deployed."""
    writer = _writer
    if writer is not None:
        writer.write()


def stop() -> None:
    """Drop the writer and delete heartbeat.json. The watchdog keeps running.

    A missing file is not a hang: the watchdog re-arms and stays quiet.
    """
    global _writer
    with _guard:
        writer = _writer
        _writer = None
    if writer is not None:
        writer.delete()


def call_com(name: str, fn, *args, **kwargs):
    """Run a broker/COM call, stamping inflight_com_call around it.

    The stamp is written only on the main thread, and only when a writer
    is installed. Otherwise ``fn`` runs unchanged — login before deploy,
    and any background caller, keep their existing behavior. The heartbeat
    writer itself still rejects a background-thread write.
    """
    writer = _writer
    on_main = threading.current_thread() is threading.main_thread()
    if writer is None or not on_main:
        return fn(*args, **kwargs)
    writer.begin_inflight(name)
    try:
        return fn(*args, **kwargs)
    finally:
        writer.end_inflight()
