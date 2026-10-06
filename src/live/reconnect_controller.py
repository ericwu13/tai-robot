"""Guarded reconnect policy for issue #157.

Pure logic — no Tkinter, no COM. ``run_backtest._attempt_reconnect`` asks
this controller whether it is safe to call ``SKQuoteLib_LeaveMonitor`` /
``SKCenterLib_LogOut`` before a fresh login.

Calling LeaveMonitor on a half-up session (the previous attempt saw
Quote/Reply but never Ready, and ``IsConnected()==2``) faults or hangs
inside SKCOM/ntdll. Every observed crash was in the deferred pre-session
window, about two minutes before the open, when the server answers
3002/3001 and does not yet send 3003. The same teardown succeeds once the
session is open, so the guard holds only while the market is closed.
"""

from __future__ import annotations

from dataclasses import dataclass

# SKQuoteLib_IsConnected() codes used by the reconnect poll.
IS_CONNECTED = 1
IS_CONNECTING = 2

ACTION_TEARDOWN_AND_LOGIN = "teardown_and_login"
ACTION_SKIP_TEARDOWN_WAIT = "skip_teardown_wait"
ACTION_ALREADY_CONNECTED = "already_connected"


@dataclass(frozen=True)
class ReconnectGuardDecision:
    """What the GUI should do before touching the COM session."""

    action: str
    reason: str
    wait_seconds: int = 0


class ReconnectController:
    """Decides whether COM teardown is safe before a reconnect attempt.

    The half-up latch is set when an attempt's Ready wait ends without
    3003. It clears on Ready or on a clean disconnect (3021/3033), which
    starts a new cycle whose first LeaveMonitor historically returns a
    COM error instead of faulting.
    """

    READY_WAIT_S: int = 3
    # Poll interval while we are deliberately not tearing a connecting
    # session down. The GUI schedules this with root.after — it does not
    # block the Tk thread.
    CONNECTING_WAIT_S: int = 15
    # Pre-session attempts wait until the open plus this grace, so the
    # 3-second failure poll cannot schedule LeaveMonitor while the server
    # is still refusing Ready.
    PRE_SESSION_GRACE_S: int = 15
    PRE_SESSION_WAIT_CAP_S: int = 180

    def __init__(self) -> None:
        self._handshake_open: bool = False
        self._reached_ready: bool = False
        self._previous_attempt_missed_ready: bool = False

    def reset(self) -> None:
        """Drop all latch state (bot stop / new deploy)."""
        self._handshake_open = False
        self._reached_ready = False
        self._previous_attempt_missed_ready = False

    @property
    def previous_attempt_missed_ready(self) -> bool:
        return self._previous_attempt_missed_ready

    @property
    def handshake_open(self) -> bool:
        return self._handshake_open

    def on_attempt_started(self) -> None:
        """A teardown-and-login attempt has begun the handshake."""
        self._handshake_open = True
        self._reached_ready = False

    def on_ready(self) -> None:
        """Ready (3003) or IsConnected()==1 — session is up."""
        self._handshake_open = False
        self._reached_ready = True
        self._previous_attempt_missed_ready = False

    def on_attempt_failed(self) -> None:
        """The Ready wait ended without Ready.

        Latches only when a handshake was actually open (or the latch is
        already set). A stray poll must not arm the guard by itself.
        """
        if not self._reached_ready and (
            self._handshake_open or self._previous_attempt_missed_ready
        ):
            self._previous_attempt_missed_ready = True
        self._handshake_open = False

    def on_clean_disconnect(self) -> None:
        """3021/3033 — the server closed the session.

        The next attempt is the first teardown of a down session. That
        call is the one that returned 9999 in the #157 logs; it is not
        the call that faulted.
        """
        self._handshake_open = False
        self._reached_ready = False
        self._previous_attempt_missed_ready = False

    def _half_up(self) -> bool:
        """Previous or in-flight attempt has not reached Ready."""
        if self._reached_ready:
            return False
        return self._previous_attempt_missed_ready or self._handshake_open

    def decide(
        self,
        is_connected: int | None,
        *,
        market_open: bool,
    ) -> ReconnectGuardDecision:
        """Choose the next COM action.

        ``is_connected`` is ``SKQuoteLib_IsConnected()``, or None when the
        probe raised.

        Skip LeaveMonitor and LogOut only when all of these hold:

        * the previous (or in-flight) attempt never reached Ready (3003)
        * the session is still connecting (``IsConnected()==2``), or the
          probe itself failed
        * the market is not open yet

        Once the session is open, teardown is allowed again — that is the
        intra-session recovery where LeaveMonitor returns 0. ``IsConnected()==1``
        means the session is already up: do not tear it down.
        """
        if is_connected == IS_CONNECTED:
            return ReconnectGuardDecision(
                action=ACTION_ALREADY_CONNECTED,
                reason="IsConnected()==1 — session already up, skip teardown and login",
            )

        unsafe = self._half_up() and (
            is_connected == IS_CONNECTING or is_connected is None
        )
        if unsafe and not market_open:
            why = (
                "IsConnected() probe failed"
                if is_connected is None
                else "IsConnected()==2 (connecting)"
            )
            return ReconnectGuardDecision(
                action=ACTION_SKIP_TEARDOWN_WAIT,
                reason=(
                    "skip LeaveMonitor+LogOut — previous attempt never reached "
                    f"Ready (3003) and {why}, market closed"
                ),
                wait_seconds=self.CONNECTING_WAIT_S,
            )

        return ReconnectGuardDecision(
            action=ACTION_TEARDOWN_AND_LOGIN,
            reason="LeaveMonitor+LogOut then fresh login",
        )

    def ready_wait_seconds(self, *, market_open: bool, secs_until_open: int) -> int:
        """How long to wait for Ready (3003) after LoginSetQuote.

        Intra-session stays at the historical 3s poll. A pre-session
        attempt waits until the open plus a short grace (capped), so the
        failure poll cannot schedule a second LeaveMonitor while the
        server is not yet sending 3003. The wait is a ``root.after``
        delay, not a sleep on the Tk thread.
        """
        if market_open or secs_until_open <= 0:
            return self.READY_WAIT_S
        uncapped = max(self.READY_WAIT_S, int(secs_until_open) + self.PRE_SESSION_GRACE_S)
        return min(self.PRE_SESSION_WAIT_CAP_S, uncapped)
