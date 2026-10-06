"""Guarded reconnect policy for issue #157.

Pure logic — no Tkinter, no COM. ``run_backtest._attempt_reconnect`` asks
this controller whether it is safe to call ``SKQuoteLib_LeaveMonitor``
before a fresh login.

Calling LeaveMonitor on a half-up session (the previous attempt saw
Quote/Reply but never Ready, and ``IsConnected()==2``) faults or hangs
inside SKCOM/ntdll. That skip keys off the connection state. It lasts
until 90s after a real TAIFEX session open — ``current_session`` /
``is_taifex_holiday``, not the weekend-only ``is_market_open()`` clock.
A weekday holiday has no open, so a half-up session keeps skipping.
After a real open+90s, one forced teardown per disconnect is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# SKQuoteLib_IsConnected() codes used by the reconnect poll.
IS_CONNECTED = 1
IS_CONNECTING = 2

ACTION_TEARDOWN_AND_LOGIN = "teardown_and_login"
ACTION_SKIP_TEARDOWN_WAIT = "skip_teardown_wait"
ACTION_ALREADY_CONNECTED = "already_connected"

# Logged when the one post-deadline teardown of a half-up session fires.
FORCED_TEARDOWN_LOG = "[RECONNECT] P1 forced teardown of half-up session"
# Logged when the 15s skip loop after that teardown is stopped.
ESCALATION_LOG = (
    "[RECONNECT] P1 half-up skip loop stopped after forced teardown"
)

_TZ_TAIPEI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ReconnectGuardDecision:
    """What the GUI should do before touching the COM session."""

    action: str
    reason: str
    wait_seconds: int = 0
    forced_teardown: bool = False


def _as_taipei(now: datetime | None) -> datetime:
    if now is None:
        from src.live.live_runner import _taipei_now
        return _taipei_now()
    if now.tzinfo is None:
        return now.replace(tzinfo=_TZ_TAIPEI)
    return now.astimezone(_TZ_TAIPEI)


def _is_closed_day(d: date) -> bool:
    """True when TAIFEX has no session opening on ``d``.

    Uses ``is_taifex_holiday`` (weekends, TW public holidays, overrides).
    A missing holiday calendar degrades to weekends, matching
    ``current_session``.
    """
    try:
        from src.market_data.holidays import is_taifex_holiday
        return is_taifex_holiday(d)
    except Exception:
        return d.weekday() >= 5


def _next_session_open(now: datetime) -> datetime:
    """Next DAY 08:45 or NIGHT 15:00 on a day that actually trades."""
    for ahead in range(16):
        day = now.date() + timedelta(days=ahead)
        if _is_closed_day(day):
            continue
        for hour, minute in ((8, 45), (15, 0)):
            open_dt = datetime(
                day.year, day.month, day.day, hour, minute, tzinfo=_TZ_TAIPEI)
            if open_dt > now:
                return open_dt
    day = now.date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, 8, 45, tzinfo=_TZ_TAIPEI)


def _live_session(now: datetime):
    """``current_session`` for ``now``, or None if it cannot be resolved."""
    try:
        from src.regime.switch_logic import current_session
        return current_session(now)
    except Exception:
        return None


def seconds_since_session_open(now: datetime | None = None) -> int:
    """Seconds relative to the real session open the reconnect clock uses.

    Inside a session this is seconds since that session's open (night
    included, keyed by ``current_session``). Outside a session it is
    negative, counting to the next DAY 08:45 or NIGHT 15:00 on a day
    ``is_taifex_holiday`` says actually trades. A weekday holiday is not
    an open: the weekend-only clock would already be past open+90s at
    10:00, and this one is still negative.
    """
    now = _as_taipei(now)
    session = _live_session(now)
    if session is not None:
        return int((now - session.open_dt).total_seconds())
    return int((now - _next_session_open(now)).total_seconds())


def in_live_session(now: datetime | None = None) -> bool:
    """True when a real TAIFEX session contains ``now``.

    The reconnect deferral uses this. It does not call the weekend-only
    market-open helper, which treats a weekday holiday as a normal session.
    """
    return _live_session(_as_taipei(now)) is not None


def seconds_until_next_session_open(now: datetime | None = None) -> int:
    """Seconds until the next real session open, or 0 when one is in progress."""
    since = seconds_since_session_open(now)
    if since >= 0:
        return 0
    return -since


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


class ReconnectSchedule:
    """The reconnect, Ready-wait, and skip-poll callbacks share one slot.

    ``_stop_live`` and ``_manual_reconnect`` call ``cancel_all``. A second
    ``_on_disconnected`` arms this again and still leaves one callback.
    """

    def __init__(self) -> None:
        self._id = None
        self._kind: str | None = None

    @property
    def timer_id(self):
        return self._id

    @property
    def kind(self) -> str | None:
        return self._kind

    def note_fired(self) -> None:
        """The armed callback is running; the toolkit has already dropped it."""
        self._id = None
        self._kind = None

    def arm(self, kind: str, delay_ms: int, callback, *, after, after_cancel):
        self._id = arm_single_timer(
            self._id, delay_ms, callback, after=after, after_cancel=after_cancel)
        self._kind = kind
        return self._id

    def cancel_all(self, after_cancel) -> None:
        if self._id is not None:
            try:
                after_cancel(self._id)
            except Exception:
                pass
        self._id = None
        self._kind = None


class ReconnectController:
    """Decides whether COM teardown is safe before a reconnect attempt.

    The half-up latch is set when an attempt's Ready wait ends without
    3003. While that latch is set and ``IsConnected()==2``, LeaveMonitor
    is skipped. The connection state is what blocks the teardown. The
    clock only ends the hold, and only at 90s after a real session open.
    After that, one forced teardown is allowed per disconnect.
    The caller records it with ``note_forced_teardown`` when it actually
    enters the LeaveMonitor path, so a poll that only inspects the
    decision does not consume the allowance.
    """

    READY_WAIT_S: int = 3
    # Half-up LeaveMonitor stays refused until this many seconds after a
    # real session open. A weekday holiday never reaches this deadline.
    HALF_UP_HOLD_S: int = 90
    # Poll interval while we are deliberately not tearing a connecting
    # session down. The GUI schedules this with root.after — it does not
    # block the Tk thread. This loop, not a long Ready timer, holds the
    # pre-open gap (including multi-day holidays).
    CONNECTING_WAIT_S: int = 15
    # After the one forced teardown, this many 15s skips then the loop
    # stops and the GUI raises an operator alert. 8 × 15s = 2 minutes.
    POST_FORCED_SKIP_LIMIT: int = 8

    def __init__(self) -> None:
        self._handshake_open: bool = False
        self._reached_ready: bool = False
        self._previous_attempt_missed_ready: bool = False
        self._forced_teardown_used: bool = False
        self._post_forced_skips: int = 0

    def reset(self) -> None:
        """Drop all latch state (bot stop / new deploy)."""
        self._handshake_open = False
        self._reached_ready = False
        self._previous_attempt_missed_ready = False
        self._forced_teardown_used = False
        self._post_forced_skips = 0

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
        self._post_forced_skips = 0

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
        self._post_forced_skips = 0

    def on_operator_reconnect(self) -> None:
        """The operator pressed Reconnect.

        Clears the one-teardown allowance and the post-forced skip count.
        ``decide(..., manual=True)`` then tears down even while the
        session is still half-up, including before open+90s.
        """
        self._forced_teardown_used = False
        self._post_forced_skips = 0

    def note_forced_teardown(self) -> None:
        """The GUI is about to LeaveMonitor a half-up session past open+90s.

        A second forced teardown in this same disconnect is then refused.
        """
        self._forced_teardown_used = True
        self._post_forced_skips = 0

    @property
    def forced_teardown_used(self) -> bool:
        return self._forced_teardown_used

    def _half_up(self) -> bool:
        """Previous or in-flight attempt has not reached Ready."""
        if self._reached_ready:
            return False
        return self._previous_attempt_missed_ready or self._handshake_open

    def record_skip_poll(self) -> bool:
        """Count one skip after the forced teardown was used.

        Returns True when the loop has reached ``POST_FORCED_SKIP_LIMIT``
        and must not be scheduled again. Skips before that teardown
        (the open+90 hold) are not counted.
        """
        if not self._forced_teardown_used:
            return False
        self._post_forced_skips += 1
        return self._post_forced_skips >= self.POST_FORCED_SKIP_LIMIT

    def decide(
        self,
        is_connected: int | None,
        *,
        seconds_since_open: float,
        manual: bool = False,
    ) -> ReconnectGuardDecision:
        """Choose the next COM action.

        ``is_connected`` is ``SKQuoteLib_IsConnected()``, or None when the
        probe raised. ``seconds_since_open`` is negative before the next
        real open and positive after it (see ``seconds_since_session_open``).
        It must come from that holiday-aware clock. This method does not
        consult a weekend-only market-open flag.

        Skip LeaveMonitor while the handshake is half-up
        (no Ready 3003, ``IsConnected()==2`` or a failed probe) and the
        real session is still under open+90s. On a weekday holiday that
        clock stays negative, so the skip holds. After a real open+90s,
        one forced teardown per disconnect is allowed. A down session
        (``IsConnected()==0``) and the first attempt after a clean 3033
        still tear down — those are not the half-up state.
        """
        if is_connected == IS_CONNECTED:
            return ReconnectGuardDecision(
                action=ACTION_ALREADY_CONNECTED,
                reason="IsConnected()==1 — session already up, skip teardown and login",
            )

        if manual:
            return ReconnectGuardDecision(
                action=ACTION_TEARDOWN_AND_LOGIN,
                reason=(
                    "operator reconnect — LeaveMonitor even if the "
                    "session is half-up"
                ),
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

    def ready_wait_seconds(self) -> int:
        """How long to wait for Ready (3003) after LoginSetQuote.

        Always the short poll. The pre-open hold is the skip loop in
        ``decide`` (every ``CONNECTING_WAIT_S``), not one timer of
        ``secs_until_open + 90``. A holiday gap can last days; a single
        long ``root.after`` would not re-check ``IsConnected()==2``.
        The wait is a ``root.after`` delay, not a sleep on the Tk thread.
        """
        return self.READY_WAIT_S
