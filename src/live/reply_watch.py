"""Login-time reply-channel diagnostics (issue #219).

Logging and one Discord alert. This does not connect, reconnect, gate
orders, or start a thread. The caller supplies the scheduler: GUI and
headless both pass Tk ``root.after``, which the headless main loop
already pumps.

``mark_reply_arrived`` is the only function a COM callback may call.
It does a queue put, the same primitive the other COM handlers use.
"""

from __future__ import annotations

import queue
from collections.abc import Callable

from src.live.reply_link_poller import SILENT_ALERT

REPLY_SILENT_MS = 60_000
REPLY_PROBE_MS = 30_000

# COM thread → main thread. Separate from the UI queue so the watchdog
# can see an arrival without dispatching other events.
_arrived: queue.Queue[str] = queue.Queue()


def mark_reply_arrived(kind: str) -> None:
    """Record OnConnect / OnComplete. Safe from a COM callback.

    Failures are swallowed. A diagnostic must not raise into the
    callback that delivers the reply event.
    """
    try:
        _arrived.put_nowait(str(kind))
    except Exception:
        pass


def _take_arrived() -> list[str]:
    found: list[str] = []
    while True:
        try:
            found.append(_arrived.get_nowait())
        except queue.Empty:
            return found


def clear_reply_arrived() -> None:
    """Drop queued arrivals. Tests use this so cases stay independent."""
    _take_arrived()


def describe_return_code(code, lookup) -> str:
    """SKCOM message when ``lookup`` works, otherwise the raw code.

    ``lookup`` is ``SKCenterLib_GetReturnCodeMessage`` or None. A missing
    lookup, an empty message, or a raised error all fall back to ``code``.
    """
    if not callable(lookup):
        return str(code)
    try:
        msg = lookup(code)
    except Exception:
        return str(code)
    if msg is None:
        return str(code)
    text = str(msg).strip()
    if not text:
        return str(code)
    return f"{code} {text}"


class ReplyChannelWatch:
    """One silent-reply watchdog and one IsConnectedByID read per login.

    ``schedule(ms, fn) -> token`` and ``cancel(token)`` are Tk after /
    after_cancel on both the GUI and the headless root. ``probe(user_id)``
    performs the COM read. Its result is only logged.
    """

    def __init__(
        self,
        *,
        schedule: Callable,
        cancel: Callable,
        log_error: Callable[[str], None],
        log_info: Callable[[str], None],
        notify: Callable[[str], None],
        probe: Callable,
        on_silent: Callable[[], None] | None = None,
    ) -> None:
        self._schedule = schedule
        self._cancel = cancel
        self._log_error = log_error
        self._log_info = log_info
        self._notify = notify
        self._probe = probe
        self._on_silent_hook = on_silent
        self._generation = 0
        self._seen = False
        self._silent_fired = False
        self._probed = False
        self._silent_token = None
        self._probe_token = None

    def arm(self, user_id: str) -> None:
        """Arm the 60s watchdog and the 30s read for this login.

        An OnConnect / OnComplete already queued (ConnectByID can deliver
        one before this returns to the Tk loop) suppresses the watchdog.
        Scheduler failures are logged and swallowed so login continues.
        """
        try:
            self._cancel_silent()
            self._cancel_probe()
            self._generation += 1
            generation = self._generation
            self._silent_fired = False
            self._probed = False
            self._seen = bool(_take_arrived())
            if self._seen:
                self._silent_token = None
            else:
                self._silent_token = self._schedule(
                    REPLY_SILENT_MS,
                    lambda g=generation: self._on_silent(g),
                )
            self._probe_token = self._schedule(
                REPLY_PROBE_MS,
                lambda g=generation, u=user_id: self._on_probe(g, u),
            )
        except Exception as exc:
            self._safe_error(
                "[CONN] reply-channel watch arm failed: "
                f"[{type(exc).__name__}] {exc}")

    def rearm_silent(self) -> None:
        """New 60s silent episode. The 30s IsConnectedByID read stays put."""
        try:
            self._cancel_silent()
            self._generation += 1
            generation = self._generation
            self._silent_fired = False
            self._seen = bool(_take_arrived())
            if self._seen:
                self._silent_token = None
                return
            self._silent_token = self._schedule(
                REPLY_SILENT_MS,
                lambda g=generation: self._on_silent(g),
            )
        except Exception as exc:
            self._safe_error(
                "[CONN] reply silent rearm failed: "
                f"[{type(exc).__name__}] {exc}")

    def note_reply(self, kind: str) -> None:
        """Cancel the silent watchdog. The 30s read stays armed.

        Main thread only (Tk after_cancel). COM callbacks use
        ``mark_reply_arrived`` and the UI queue, which lands here.
        ``kind`` is OnConnect or OnComplete; it is named in the failure
        line and does not change what is cancelled.
        """
        try:
            _take_arrived()
            self._seen = True
            self._cancel_silent()
        except Exception as exc:
            self._seen = True
            self._safe_error(
                f"[CONN] reply watch cancel failed on {kind}: "
                f"[{type(exc).__name__}] {exc}")

    def _cancel_silent(self) -> None:
        token = self._silent_token
        self._silent_token = None
        self._cancel_token(token)

    def _cancel_probe(self) -> None:
        token = self._probe_token
        self._probe_token = None
        self._cancel_token(token)

    def _cancel_token(self, token) -> None:
        if token is None:
            return
        try:
            self._cancel(token)
        except Exception:
            pass

    def _on_silent(self, generation: int) -> None:
        self._silent_token = None
        try:
            if _take_arrived():
                self._seen = True
            if (generation != self._generation
                    or self._seen
                    or self._silent_fired):
                return
            self._silent_fired = True
            if self._on_silent_hook is not None:
                try:
                    self._on_silent_hook()
                except Exception as exc:
                    self._safe_error(
                        "[CONN] reply silent watch failed: "
                        f"[{type(exc).__name__}] {exc}")
                return
            try:
                self._log_error(SILENT_ALERT)
            except Exception:
                pass
            try:
                self._notify(SILENT_ALERT)
            except Exception as exc:
                self._safe_error(
                    "[CONN] reply silent discord failed: "
                    f"[{type(exc).__name__}] {exc}")
        except Exception as exc:
            self._safe_error(
                "[CONN] reply silent watch failed: "
                f"[{type(exc).__name__}] {exc}")

    def _on_probe(self, generation: int, user_id: str) -> None:
        self._probe_token = None
        try:
            if generation != self._generation or self._probed:
                return
            self._probed = True
            try:
                value = self._probe(user_id)
            except Exception as exc:
                self._safe_error(
                    "[CONN] IsConnectedByID raised: "
                    f"[{type(exc).__name__}] {exc}")
                return
            try:
                self._log_info(
                    f"[CONN] IsConnectedByID={value} at 30s after login")
            except Exception:
                pass
        except Exception as exc:
            self._safe_error(
                "[CONN] IsConnectedByID probe failed: "
                f"[{type(exc).__name__}] {exc}")

    def _safe_error(self, msg: str) -> None:
        try:
            self._log_error(msg)
        except Exception:
            pass
