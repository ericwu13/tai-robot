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

If that holiday-aware lookup raises, the clock still falls back to the
weekend-only session edges and leaves a WARN for the GUI to send
through ``_log`` and Discord. That degraded calendar does not allow
the automatic forced teardown: a half-up session keeps skipping, and
one P1 asks the operator to press Reconnect. Swallowing the error and
treating it as "no session" makes 10:00 on a trading day look 18000s
before the night open.
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
# current_session itself raised while the weekend-only clock says the
# market is open. The GUI sends this through _log and Discord.
CLOCK_FALLBACK_WARN = (
    "[RECONNECT] WARN holiday-aware session lookup failed — "
    "using the weekend-only clock"
)
# is_taifex_holiday raised at call time. Weekday-only closed check.
# Import failure stays silent; this one is a live lookup error.
HOLIDAY_LOOKUP_WARN = (
    "[RECONNECT] WARN is_taifex_holiday failed — "
    "using the weekday-only closed check"
)
# is_market_open() and current_session disagree. Holiday-aware still wins.
CLOCK_DISAGREE_WARN = (
    "[RECONNECT] WARN weekend-only is_market_open() and the "
    "holiday-aware session clock disagree"
)
# Operator Reconnect on a half-up session before open+90. Default answer is No.
HALF_UP_TEARDOWN_CONFIRM = (
    "Session is still connecting (no Ready). Tearing it down is the "
    "call that crashed/froze bots in #157. Proceed?"
)
OPERATOR_DECLINED_HALF_UP_LOG = "[RECONNECT] operator declined half-up teardown"
# No dialog was shown. Distinct from an operator No.
HALF_UP_CONFIRM_UNAVAILABLE_LOG = (
    "[RECONNECT] half-up confirm unavailable (TclError); refused"
)
OPERATOR_FORCED_HALF_UP_P1 = "operator forced half-up teardown"
HEADLESS_HALF_UP_REFUSED_P1 = "manual reconnect refused: half-up before open+90"
# Automatic path withheld LeaveMonitor because the holiday/session
# lookup raised. One per degraded episode, via _log and Discord.
CALENDAR_DEGRADED_P1 = (
    "[RECONNECT] P1 calendar degraded; half-up session not torn down "
    "automatically — press Reconnect to confirm"
)
# Yes, but the handshake moved while the dialog was open.
HALF_UP_YES_STALE_LOG = (
    "[RECONNECT] operator Yes dropped — handshake changed during confirm"
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
    A missing holiday module degrades to weekends, matching
    ``current_session``.     A call that raises does the same and queues
    ``HOLIDAY_LOOKUP_WARN``. The latch stays until a full next-open
    walk finishes with no failures, so one bad date cannot re-alert on
    every 15s poll. A Saturday or a closed overnight stays on the
    negative clock and keeps skipping; the raise does not force
    LeaveMonitor there.
    """
    global _holiday_scan_failures
    try:
        from src.market_data.holidays import is_taifex_holiday
    except Exception:
        return d.weekday() >= 5
    try:
        return is_taifex_holiday(d)
    except Exception:
        _holiday_scan_failures += 1
        note_calendar_degraded()
        _note_clock_warning("holiday", HOLIDAY_LOOKUP_WARN)
        return d.weekday() >= 5


def _next_session_open(now: datetime) -> datetime:
    """Next DAY 08:45 or NIGHT 15:00 on a day that actually trades."""
    global _holiday_scan_failures
    _holiday_scan_failures = 0
    try:
        for ahead in range(16):
            day = now.date() + timedelta(days=ahead)
            if _is_closed_day(day):
                continue
            for hour, minute in ((8, 45), (15, 0)):
                open_dt = datetime(
                    day.year, day.month, day.day, hour, minute,
                    tzinfo=_TZ_TAIPEI)
                if open_dt > now:
                    return open_dt
        day = now.date() + timedelta(days=1)
        return datetime(day.year, day.month, day.day, 8, 45, tzinfo=_TZ_TAIPEI)
    finally:
        if _holiday_scan_failures == 0:
            _clear_clock_warning("holiday")


def _live_session(now: datetime):
    """``current_session`` for ``now``.

    Raises when the session lookup raises. Callers fall back to the
    weekend-only clock. Swallowing the error and treating it as "no
    session" is the −18000s probe at 10:00 on a trading day.
    """
    from src.regime.switch_logic import current_session
    return current_session(now)


def _weekend_session_open(now: datetime) -> datetime:
    """Open instant the weekend-only clock uses for ``now``.

    Same edges as ``live_runner.is_market_open``: weekdays 08:45–13:45
    and 15:00–05:00, Saturday until 05:00, Sunday closed, Monday with no
    night carryover. A weekday holiday is an ordinary weekday here.
    When the market is closed this is the next open, so the delta is
    negative.
    """
    from src.live.live_runner import is_market_open

    weekday = now.weekday()
    minutes = now.hour * 60 + now.minute

    def at(day, hour: int, minute: int) -> datetime:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_TZ_TAIPEI)

    if is_market_open(now):
        if minutes >= 15 * 60:
            return at(now.date(), 15, 0)
        if minutes < 5 * 60:
            return at(now.date() - timedelta(days=1), 15, 0)
        return at(now.date(), 8, 45)
    if weekday == 6:
        return at(now.date() + timedelta(days=1), 8, 45)
    if weekday == 5:
        return at(now.date() + timedelta(days=2), 8, 45)
    if weekday == 0 and minutes < 5 * 60:
        return at(now.date(), 8, 45)
    if 5 * 60 <= minutes < 8 * 60 + 45:
        return at(now.date(), 8, 45)
    if 13 * 60 + 45 <= minutes < 15 * 60:
        return at(now.date(), 15, 0)
    return at(now.date() + timedelta(days=1), 8, 45)


def _weekend_seconds_since_open(now: datetime) -> int:
    return int((now - _weekend_session_open(now)).total_seconds())


_pending_clock_warnings: list[str] = []
_latched_clock_warnings: set[str] = set()
# Calendar disagreement is noisy if the two clocks flicker. One WARN per
# Taipei date, even when they agree again and then disagree the same day.
_clock_disagree_day: date | None = None
# Failures inside the current _next_session_open walk. The holiday WARN
# latch clears only when a whole walk finishes with zero failures.
_holiday_scan_failures: int = 0


def _note_clock_warning(kind: str, message: str) -> None:
    """Queue ``message`` once until ``kind`` clears."""
    if kind in _latched_clock_warnings:
        return
    _latched_clock_warnings.add(kind)
    _pending_clock_warnings.append(message)


def _clear_clock_warning(kind: str) -> None:
    _latched_clock_warnings.discard(kind)


def _note_clock_disagreement(now: datetime, in_session: bool) -> None:
    """WARN when the weekend-only flag and the holiday-aware answer differ.

    The holiday-aware answer stays in force. The WARN is what stops a
    trading day mislabeled as closed from skipping with no operator signal.
    It fires at most once per Taipei calendar day: agreeing and then
    disagreeing again the same day does not send a second alert.
    """
    global _clock_disagree_day
    try:
        from src.live.live_runner import is_market_open
        weekend_open = bool(is_market_open(now))
    except Exception:
        return
    if weekend_open == in_session:
        return
    day = now.date()
    if _clock_disagree_day == day:
        return
    _clock_disagree_day = day
    # The kind latch would otherwise hold into the next date.
    _latched_clock_warnings.discard("disagree")
    _note_clock_warning("disagree", CLOCK_DISAGREE_WARN)


def pop_session_clock_warnings() -> list[str]:
    """Warnings the GUI should send through ``_log`` and Discord."""
    global _pending_clock_warnings
    pending = _pending_clock_warnings
    _pending_clock_warnings = []
    return pending


def reset_session_clock_warnings() -> None:
    """Drop queued warnings and their once-only latches. Tests use this."""
    global _pending_clock_warnings, _clock_disagree_day, _holiday_scan_failures
    global _calendar_degraded, _calendar_degraded_p1_sent
    global _pending_calendar_degraded_p1
    _pending_clock_warnings = []
    _latched_clock_warnings.clear()
    _clock_disagree_day = None
    _holiday_scan_failures = 0
    _calendar_degraded = False
    _calendar_degraded_p1_sent = False
    _pending_calendar_degraded_p1 = None


# A holiday or session lookup raised during the reconnect decision.
# Stays set until a later lookup succeeds. The automatic half-up path
# does not force a teardown while this is set, whatever the weekend
# clock says about open+90.
_calendar_degraded: bool = False
# One P1 per degraded episode. Re-armed when the calendar recovers or
# the episode ends (Ready, a full drop, or a new disconnect).
_calendar_degraded_p1_sent: bool = False
_pending_calendar_degraded_p1: str | None = None


def calendar_degraded() -> bool:
    """True after ``is_taifex_holiday`` or ``current_session`` raised."""
    return _calendar_degraded


def note_calendar_degraded() -> None:
    global _calendar_degraded
    _calendar_degraded = True


def note_calendar_recovered() -> None:
    """A lookup succeeded. The next degraded stretch may alert again."""
    global _calendar_degraded, _calendar_degraded_p1_sent
    _calendar_degraded = False
    _calendar_degraded_p1_sent = False


def rearm_calendar_degraded_p1() -> None:
    """The disconnect episode ended, or a new one started."""
    global _calendar_degraded_p1_sent
    _calendar_degraded_p1_sent = False


def queue_calendar_degraded_p1() -> None:
    """Queue the operator P1 once until the latch is re-armed."""
    global _calendar_degraded_p1_sent, _pending_calendar_degraded_p1
    if not _calendar_degraded or _calendar_degraded_p1_sent:
        return
    _calendar_degraded_p1_sent = True
    _pending_calendar_degraded_p1 = CALENDAR_DEGRADED_P1


def pop_calendar_degraded_p1() -> str | None:
    """The P1 the GUI should send, or None when this poll stays quiet."""
    global _pending_calendar_degraded_p1
    message = _pending_calendar_degraded_p1
    _pending_calendar_degraded_p1 = None
    return message


def _holiday_calls_failed(now: datetime) -> bool:
    """True when ``is_taifex_holiday`` raises for today or yesterday.

    ``current_session`` calls ``switch_logic._is_closed_day``, which
    swallows that raise and treats a weekday as open. During the day
    session that looks like a normal Friday: no WARN, and a half-up
    session is past open+90s, so one LeaveMonitor is allowed. Probe both
    days before that lookup. An import failure is not a call-time raise;
    ``current_session`` still degrades to weekends on its own.
    """
    try:
        from src.market_data.holidays import is_taifex_holiday
    except Exception:
        return False
    failed = False
    for day in (now.date(), now.date() - timedelta(days=1)):
        try:
            is_taifex_holiday(day)
        except Exception:
            failed = True
    return failed


def seconds_since_session_open(now: datetime | None = None) -> int:
    """Seconds relative to the real session open the reconnect clock uses.

    Inside a session this is seconds since that session's open (night
    included, keyed by ``current_session``). Outside a session it is
    negative, counting to the next DAY 08:45 or NIGHT 15:00 on a day
    ``is_taifex_holiday`` says actually trades. A weekday holiday is not
    an open: the weekend-only clock would already be past open+90s at
    10:00, and this one is still negative.

    If ``current_session`` itself raises, or ``is_taifex_holiday`` raises
    for today or yesterday, this returns the weekend-only clock and queues
    ``CLOCK_FALLBACK_WARN``. The calendar is then degraded: ``decide``
    keeps skipping a half-up session instead of the forced teardown,
    even when this fallback value is past open+90s (Friday 10:00 is
    75 minutes after 08:45). A Saturday or a closed overnight is still
    negative. It is not the next night open (−18000s) at 10:00 on a
    trading day. The holiday probe runs first: ``current_session``
    would swallow the raise and report a live Friday session with no WARN.
    """
    global _holiday_scan_failures
    now = _as_taipei(now)
    if _holiday_calls_failed(now):
        note_calendar_degraded()
        _clear_clock_warning("disagree")
        _note_clock_warning("fallback", CLOCK_FALLBACK_WARN)
        return _weekend_seconds_since_open(now)
    # A previous closed-day walk must not keep this call degraded.
    _holiday_scan_failures = 0
    try:
        session = _live_session(now)
        if session is not None:
            since = int((now - session.open_dt).total_seconds())
            in_session = True
        else:
            since = int((now - _next_session_open(now)).total_seconds())
            in_session = False
    except Exception:
        note_calendar_degraded()
        _clear_clock_warning("disagree")
        weekend_since = _weekend_seconds_since_open(now)
        _note_clock_warning("fallback", CLOCK_FALLBACK_WARN)
        return weekend_since
    if _holiday_scan_failures:
        note_calendar_degraded()
    else:
        note_calendar_recovered()
    _clear_clock_warning("fallback")
    _note_clock_disagreement(now, in_session)
    return since


def in_live_session(now: datetime | None = None) -> bool:
    """True when a real TAIFEX session contains ``now``.

    The reconnect deferral uses this. It does not call the weekend-only
    market-open helper, which treats a weekday holiday as a normal session.
    A lookup that raises, including ``is_taifex_holiday`` for today or
    yesterday, falls back to that helper and queues the same WARN as
    ``seconds_since_session_open``.
    """
    now = _as_taipei(now)
    if _holiday_calls_failed(now):
        note_calendar_degraded()
        _note_clock_warning("fallback", CLOCK_FALLBACK_WARN)
        from src.live.live_runner import is_market_open
        return bool(is_market_open(now))
    try:
        live = _live_session(now) is not None
    except Exception:
        note_calendar_degraded()
        _note_clock_warning("fallback", CLOCK_FALLBACK_WARN)
        from src.live.live_runner import is_market_open
        return bool(is_market_open(now))
    # A future-date failure is recorded on the scan counter. Do not
    # clear that degraded stretch from this flag alone.
    if _holiday_scan_failures == 0:
        note_calendar_recovered()
    _note_clock_disagreement(now, live)
    return live


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
        self._delay_ms: int | None = None
        self._callback = None
        self._suspended = False

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
        self._delay_ms = None
        self._callback = None
        self._suspended = False

    def arm(self, kind: str, delay_ms: int, callback, *, after, after_cancel):
        self._delay_ms = delay_ms
        self._callback = callback
        self._suspended = False
        self._id = arm_single_timer(
            self._id, delay_ms, callback, after=after, after_cancel=after_cancel)
        self._kind = kind
        return self._id

    def suspend(self, after_cancel) -> None:
        """Cancel the pending id and keep the callback for ``resume``.

        The half-up confirm dialog uses this so a skip poll cannot fire
        LeaveMonitor while the modal is up. The after id lives in this
        one slot; nothing else is armed beside it.
        """
        if self._id is None or self._callback is None:
            return
        try:
            after_cancel(self._id)
        except Exception:
            pass
        self._id = None
        self._suspended = True

    def resume(self, *, after, after_cancel):
        """Re-arm the callback ``suspend`` held, if it is still pending.

        The full delay is re-armed on purpose (the safe direction). A
        leftover shorter wait could fire LeaveMonitor as soon as the
        dialog returns; starting the interval over only postpones the
        next probe.
        """
        if not self._suspended or self._callback is None:
            self._suspended = False
            return None
        kind = self._kind or "skip"
        # Full delay on purpose (the safe direction), not the time left.
        delay_ms = 0 if self._delay_ms is None else self._delay_ms
        callback = self._callback
        self._suspended = False
        return self.arm(
            kind, delay_ms, callback, after=after, after_cancel=after_cancel)

    def cancel_all(self, after_cancel) -> None:
        if self._id is not None:
            try:
                after_cancel(self._id)
            except Exception:
                pass
        self._id = None
        self._kind = None
        self._delay_ms = None
        self._callback = None
        self._suspended = False


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
        rearm_calendar_degraded_p1()

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
        rearm_calendar_degraded_p1()

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
        rearm_calendar_degraded_p1()

    def on_operator_reconnect(self) -> None:
        """The operator pressed Reconnect and a teardown will run.

        Clears the one-teardown allowance and the post-forced skip count.
        ``decide(..., manual=True)`` then tears down even while the
        session is still half-up. Before open+90 the GUI asks first
        (default No) and headless refuses; this method runs only when
        the teardown is actually going ahead.
        """
        self._forced_teardown_used = False
        self._post_forced_skips = 0

    def is_half_up(self) -> bool:
        """No Ready (3003) since the last EnterMonitor."""
        return self._half_up()

    def manual_half_up_needs_confirm(
        self,
        is_connected: int | None,
        seconds_since_open: float,
    ) -> bool:
        """Operator Reconnect must ask before tearing this session down.

        The trigger is manual (the caller), no Ready since the last
        EnterMonitor, and the holiday-aware clock still before open+90s.
        ``IsConnected()==2`` and a failed probe (None) both count.
        A degraded calendar counts as before open+90: that deadline
        cannot be trusted once the lookup has raised. Fully up, fully
        down, and a healthy clock at open+90s or later do not.
        """
        before_deadline = (
            seconds_since_open < self.HALF_UP_HOLD_S or calendar_degraded()
        )
        return (
            (is_connected == IS_CONNECTING or is_connected is None)
            and self._half_up()
            and before_deadline
        )

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

        ``IsConnected()==0`` also clears the post-forced skip count. A
        session that fully drops inside this disconnect is not the
        half-up loop those skips were counting. A failed probe (None)
        does not clear it.
        """
        if is_connected == 0:
            self._post_forced_skips = 0
            # A full drop ends the half-up episode. The next one may alert.
            rearm_calendar_degraded_p1()

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
        # Degraded calendar: never the automatic forced teardown, even
        # when the weekend-only fallback is already past open+90s.
        if unsafe and calendar_degraded():
            queue_calendar_degraded_p1()
            why = (
                "IsConnected() probe failed"
                if is_connected is None
                else "IsConnected()==2 (connecting)"
            )
            return ReconnectGuardDecision(
                action=ACTION_SKIP_TEARDOWN_WAIT,
                reason=(
                    "skip LeaveMonitor — calendar degraded, half-up, "
                    f"no Ready (3003), {why}; automatic forced teardown "
                    "withheld"
                ),
                wait_seconds=self.CONNECTING_WAIT_S,
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
