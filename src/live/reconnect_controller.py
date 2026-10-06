"""Guarded reconnect policy for issue #157.

Pure logic — no Tkinter, no COM. ``run_backtest._attempt_reconnect`` asks
this controller whether it is safe to call ``SKQuoteLib_LeaveMonitor``
before a fresh login.

Calling LeaveMonitor on a half-up session (the previous attempt saw
Quote/Reply but never Ready, and ``IsConnected()==2``) faults or hangs
inside SKCOM/ntdll. The skip holds until open+90s whether or not the
market is already open. After that, one forced teardown per disconnect
is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# SKQuoteLib_IsConnected() codes used by the reconnect poll.
IS_CONNECTED = 1
IS_CONNECTING = 2

ACTION_TEARDOWN_AND_LOGIN = "teardown_and_login"
ACTION_SKIP_TEARDOWN_WAIT = "skip_teardown_wait"
ACTION_ALREADY_CONNECTED = "already_connected"

# Logged when the one post-deadline teardown of a half-up session fires.
FORCED_TEARDOWN_LOG = "[RECONNECT] P1 forced teardown of half-up session"

_TZ_TAIPEI = timezone(timedelta(hours=8))
_AM_OPEN_S = 8 * 3600 + 45 * 60
_AM_CLOSE_S = 13 * 3600 + 45 * 60
_PM_OPEN_S = 15 * 3600
_NIGHT_CLOSE_S = 5 * 3600


@dataclass(frozen=True)
class ReconnectGuardDecision:
    """What the GUI should do before touching the COM session."""

    action: str
    reason: str
    wait_seconds: int = 0
    forced_teardown: bool = False


def seconds_since_session_open(now: datetime | None = None) -> int:
    """Seconds relative to the session open the reconnect clock uses.

    Negative before the next open (open−120s is −120). Zero at the open.
    Positive afterwards, including the night session after midnight.
    Weekend gaps count forward to Monday 08:45. Holidays are not special
    here — that deferral is still the connection monitor's job.
    """
    if now is None:
        from src.live.live_runner import _taipei_now
        now = _taipei_now()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_TAIPEI)
    else:
        now = now.astimezone(_TZ_TAIPEI)

    sod = now.hour * 3600 + now.minute * 60 + now.second
    weekday = now.weekday()  # Mon=0 .. Sun=6

    def _until(target_sod: int, day_shift: int = 0) -> int:
        return day_shift * 86400 + target_sod - sod

    # Saturday 00:00–05:00 is Friday's night session, open since 15:00.
    if weekday == 5 and sod < _NIGHT_CLOSE_S:
        return sod + 86400 - _PM_OPEN_S

    # Sunday, or Saturday after 05:00: next open is Monday 08:45.
    if weekday == 6:
        return -_until(_AM_OPEN_S, day_shift=1)
    if weekday == 5:
        return -_until(_AM_OPEN_S, day_shift=2)

    # Monday 00:00–05:00 has no Sunday carryover.
    if weekday == 0 and sod < _NIGHT_CLOSE_S:
        return -_until(_AM_OPEN_S)

    if _AM_OPEN_S <= sod < _AM_CLOSE_S:
        return sod - _AM_OPEN_S
    if sod >= _PM_OPEN_S:
        return sod - _PM_OPEN_S
    if sod < _NIGHT_CLOSE_S:
        return sod + 86400 - _PM_OPEN_S

    # 05:00–08:45 → today's 08:45. 13:45–15:00 → today's 15:00.
    if sod < _AM_OPEN_S:
        return -_until(_AM_OPEN_S)
    return -_until(_PM_OPEN_S)


def arm_single_timer(current_id, delay_ms, callback, *, after, after_cancel):
    """Schedule ``callback``, cancelling ``current_id`` first.

    Repeated calls leave exactly one pending timer. ``_on_disconnected``
    reaches this through ``_execute_reconnect_action``.
    """
    if current_id is not None:
        try:
            after_cancel(current_id)
        except Exception:
            pass
    return after(delay_ms, callback)


class ReconnectController:
    """Decides whether COM teardown is safe before a reconnect attempt.

    The half-up latch is set when an attempt's Ready wait ends without
    3003. While that latch is set and ``IsConnected()==2``, LeaveMonitor
    is skipped until open+90s — the market-open flag does not lift the
    skip. After open+90s, one forced teardown is allowed per disconnect.
    The caller records it with ``note_forced_teardown`` when it actually
    enters the LeaveMonitor path, so a poll that only inspects the
    decision does not consume the allowance.
    """

    READY_WAIT_S: int = 3
    # Half-up LeaveMonitor stays refused until this many seconds after
    # the session open, whether or not ``is_market_open()`` is already true.
    HALF_UP_HOLD_S: int = 90
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
        self._forced_teardown_used: bool = False

    def reset(self) -> None:
        """Drop all latch state (bot stop / new deploy)."""
        self._handshake_open = False
        self._reached_ready = False
        self._previous_attempt_missed_ready = False
        self._forced_teardown_used = False

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
        self._forced_teardown_used = False

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
        self._forced_teardown_used = False

    def note_forced_teardown(self) -> None:
        """The GUI is about to LeaveMonitor a half-up session past open+90s.

        A second forced teardown in this same disconnect is then refused.
        """
        self._forced_teardown_used = True

    @property
    def forced_teardown_used(self) -> bool:
        return self._forced_teardown_used

    def _half_up(self) -> bool:
        """Previous or in-flight attempt has not reached Ready."""
        if self._reached_ready:
            return False
        return self._previous_attempt_missed_ready or self._handshake_open

    def decide(
        self,
        is_connected: int | None,
        *,
        seconds_since_open: float,
    ) -> ReconnectGuardDecision:
        """Choose the next COM action.

        ``is_connected`` is ``SKQuoteLib_IsConnected()``, or None when the
        probe raised. ``seconds_since_open`` is negative before the open
        and positive after it (see ``seconds_since_session_open``).

        Skip LeaveMonitor while the handshake is half-up
        (no Ready 3003, ``IsConnected()==2`` or a failed probe) and
        ``seconds_since_open`` is still under open+90s. That does not
        depend on whether the market is open. After open+90s, one forced
        teardown per disconnect is allowed. A down session
        (``IsConnected()==0``) and the first attempt after a clean 3033
        still tear down — those are not the half-up state.
        """
        if is_connected == IS_CONNECTED:
            return ReconnectGuardDecision(
                action=ACTION_ALREADY_CONNECTED,
                reason="IsConnected()==1 — session already up, skip teardown and login",
            )

        unsafe = self._half_up() and (
            is_connected == IS_CONNECTING or is_connected is None
        )
        if unsafe and seconds_since_open < self.HALF_UP_HOLD_S:
            why = (
                "IsConnected() probe failed"
                if is_connected is None
                else "IsConnected()==2 (connecting)"
            )
            return ReconnectGuardDecision(
                action=ACTION_SKIP_TEARDOWN_WAIT,
                reason=(
                    "skip LeaveMonitor — half-up, no Ready (3003), "
                    f"{why}, seconds_since_open={seconds_since_open} "
                    f"< open+{self.HALF_UP_HOLD_S}s"
                ),
                wait_seconds=self.CONNECTING_WAIT_S,
            )
        if unsafe and self._forced_teardown_used:
            return ReconnectGuardDecision(
                action=ACTION_SKIP_TEARDOWN_WAIT,
                reason=(
                    "skip LeaveMonitor — forced half-up teardown already "
                    "used for this disconnect"
                ),
                wait_seconds=self.CONNECTING_WAIT_S,
            )
        if unsafe:
            return ReconnectGuardDecision(
                action=ACTION_TEARDOWN_AND_LOGIN,
                reason=FORCED_TEARDOWN_LOG,
                forced_teardown=True,
            )

        return ReconnectGuardDecision(
            action=ACTION_TEARDOWN_AND_LOGIN,
            reason="LeaveMonitor then fresh login",
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
