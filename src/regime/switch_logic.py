"""Pure functions for regime switching logic.

Session-boundary detection, classification triggers, swap decisions —
all testable without COM, Tk, or live infrastructure.

Session identity convention: a trading session is identified by its OPEN
date. The midnight-straddling night session (15:00 → 05:00 next day)
keeps a single stable identity — e.g. the night opening Tue 15:00 and
closing Wed 05:00 is ``("<Tue date>", "NIGHT")`` throughout. This is the
key used for classification dedup, P&L recording, and history rows.
(``session_slot`` below predates this and returns the CALENDAR date at
the queried moment; it is kept for the daily-report session key where
calendar-date semantics are fine.)
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

logger = logging.getLogger(__name__)

_TZ_TAIPEI = timezone(timedelta(hours=8))

# Taiwan futures session boundaries (minutes since midnight, TPE)
_DAY_OPEN = 8 * 60 + 45     # 08:45 = 525
_DAY_CLOSE = 13 * 60 + 45   # 13:45 = 825
_DAY_SLOT_END = 13 * 60 + 46  # 13:46 = 826 (1 min grace for poll)
_NIGHT_OPEN = 15 * 60        # 15:00 = 900
_NIGHT_CLOSE = 5 * 60        # 05:00 = 300


def session_slot(now: datetime | None = None) -> tuple[str, str]:
    """Return ``(YYYY-MM-DD, "DAY"|"NIGHT")`` for a Taipei-time moment.

    DAY covers 08:45 <= hh:mm < 13:46 (one extra minute past the 13:45
    close so a poll at 13:45:30 still classifies as DAY).  Everything
    else is NIGHT — the night session straddles midnight (15:00-05:00
    TPE), so "not DAY" is the correct partition.

    When *now* is None, uses the current Taipei wall-clock time.
    """
    if now is None:
        now = datetime.now(_TZ_TAIPEI)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_TAIPEI)
    minutes = now.hour * 60 + now.minute
    slot = "DAY" if _DAY_OPEN <= minutes < _DAY_SLOT_END else "NIGHT"
    return (now.strftime("%Y-%m-%d"), slot)


def in_closed_gap(now: datetime | None = None) -> bool:
    """True when the market is in a closed gap between sessions.

    Gaps:
    - DAY close  → NIGHT open: 13:46 .. 14:59  (74 min)
    - NIGHT close → DAY open:  05:01 .. 08:44  (223 min)
    - Weekend: Sat 05:01 → Mon 08:44

    The swap application window — no ticks flow, bot is flat.
    """
    if now is None:
        now = datetime.now(_TZ_TAIPEI)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_TAIPEI)

    weekday = now.weekday()  # Mon=0, Sun=6
    minutes = now.hour * 60 + now.minute

    # Sunday: fully closed all day
    if weekday == 6:
        return True

    # Saturday: closed after 05:00
    if weekday == 5:
        return minutes >= _NIGHT_CLOSE

    # Monday: pre-open gap (00:00-08:44) — no carryover from Sunday
    if weekday == 0 and minutes < _DAY_OPEN:
        return True

    # Weekday intraday gaps
    # After DAY close (13:46) and before NIGHT open (15:00)
    if _DAY_SLOT_END <= minutes < _NIGHT_OPEN:
        return True
    # After NIGHT close (05:00) and before DAY open (08:45)
    if _NIGHT_CLOSE <= minutes < _DAY_OPEN:
        return True

    return False


class SessionInfo(NamedTuple):
    """A concrete trading session, identified by its OPEN date."""
    open_date: str        # YYYY-MM-DD of the session OPEN
    slot: str             # "DAY" | "NIGHT"
    open_dt: datetime     # tz-aware Taipei
    close_dt: datetime    # tz-aware Taipei

    @property
    def key(self) -> str:
        return f"{self.open_date}|{self.slot}"


def _norm_tpe(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(_TZ_TAIPEI)
    if now.tzinfo is None:
        return now.replace(tzinfo=_TZ_TAIPEI)
    return now


def _is_closed_day(d: date) -> bool:
    """True when TAIFEX has no sessions opening on ``d``.

    Delegates to the market-data holiday calendar (weekends + TW public
    holidays + overrides); degrades to a weekend-only check if the
    holidays module is unavailable (mis-bundled frozen EXE, issue #58).
    """
    try:
        from src.market_data.holidays import is_taifex_holiday
        return is_taifex_holiday(d)
    except Exception:
        return d.weekday() >= 5


def current_session(now: datetime | None = None) -> SessionInfo | None:
    """The trading session containing ``now``, or None when the market
    is closed (intraday gap, weekend, or holiday).

    Unlike ``session_slot``, this returns None outside real sessions —
    so a Saturday 13:43 poll can no longer masquerade as a DAY session,
    and the night session is keyed by its OPEN date on both sides of
    midnight.
    """
    now = _norm_tpe(now)
    minutes = now.hour * 60 + now.minute
    today = now.date()

    if _DAY_OPEN <= minutes < _DAY_CLOSE:
        if _is_closed_day(today):
            return None
        open_dt = now.replace(hour=8, minute=45, second=0, microsecond=0)
        close_dt = now.replace(hour=13, minute=45, second=0, microsecond=0)
        return SessionInfo(today.isoformat(), "DAY", open_dt, close_dt)

    if minutes >= _NIGHT_OPEN:
        if _is_closed_day(today):
            return None
        open_dt = now.replace(hour=15, minute=0, second=0, microsecond=0)
        return SessionInfo(today.isoformat(), "NIGHT", open_dt,
                           open_dt + timedelta(hours=14))

    if minutes < _NIGHT_CLOSE:
        # 00:00-05:00 — carryover of the night that opened yesterday
        yesterday = today - timedelta(days=1)
        if _is_closed_day(yesterday):
            return None
        open_dt = (now - timedelta(days=1)).replace(
            hour=15, minute=0, second=0, microsecond=0)
        return SessionInfo(yesterday.isoformat(), "NIGHT", open_dt,
                           open_dt + timedelta(hours=14))

    return None


def latest_night_session(now: datetime | None = None) -> SessionInfo | None:
    """The most recently OPENED night session at ``now`` — in progress
    or already closed. Looks back up to 14 days (long holiday runs).
    """
    now = _norm_tpe(now)
    for back in range(15):
        d = now.date() - timedelta(days=back)
        if _is_closed_day(d):
            continue
        open_dt = datetime(d.year, d.month, d.day, 15, 0, tzinfo=_TZ_TAIPEI)
        if open_dt <= now:
            return SessionInfo(d.isoformat(), "NIGHT", open_dt,
                               open_dt + timedelta(hours=14))
    return None


def last_completed_night(now: datetime | None = None) -> SessionInfo | None:
    """The most recent night session that has already CLOSED at ``now``."""
    now = _norm_tpe(now)
    sess = latest_night_session(now)
    if sess is None:
        return None
    if now >= sess.close_dt:
        return sess
    return latest_night_session(sess.open_dt - timedelta(minutes=1))


def classification_due(
    now: datetime,
    last_assessed_key: str,
    window_minutes: float = 2.0,
) -> SessionInfo | None:
    """The night session whose classification should fire now, or None.

    Fires in the session's last ``window_minutes`` (the normal 04:58
    trigger) — or any time AFTER it closed while still unassessed
    (catch-up: an app hang/sleep across the close window no longer
    silently skips the day; a catch-up run classifies "as of now", so
    its features may include bars from the following DAY session).

    Weekend/holiday polls are naturally inert: the latest night session
    doesn't advance on closed days, so once its key is assessed the
    check stays None until a new night actually opens.
    """
    sess = latest_night_session(now)
    if sess is None:
        return None
    if sess.key == last_assessed_key:
        return None
    now = _norm_tpe(now)
    if now >= sess.close_dt:
        return sess  # catch-up
    if (sess.close_dt - now) <= timedelta(minutes=window_minutes):
        return sess
    return None


def validate_leg_strategies(
    long_name: str,
    short_name: str,
    registry: dict,
) -> list[str]:
    """Validate that both leg strategies exist in the registry.

    Mixed timeframes are ALLOWED: the switching runner rebuilds its bar
    aggregator from accumulated 1-min history when it swaps to a leg on a
    different timeframe (see ``LiveRunner._rebuild_timeframe``). A mismatch
    is therefore only logged as a soft warning, not returned as an error.

    Returns a list of error strings (empty = valid).
    """
    errors = []
    if not long_name or long_name not in registry:
        errors.append(f"Long strategy '{long_name}' not found in registry")
    if not short_name or short_name not in registry:
        errors.append(f"Short strategy '{short_name}' not found in registry")
    if errors:
        return errors

    long_cls = registry[long_name]
    short_cls = registry[short_name]
    l_kt = getattr(long_cls, "kline_type", 0)
    l_km = getattr(long_cls, "kline_minute", 1)
    s_kt = getattr(short_cls, "kline_type", 0)
    s_km = getattr(short_cls, "kline_minute", 1)
    if (l_kt, l_km) != (s_kt, s_km):
        logger.warning(
            "Mixed timeframes detected (long=(%s,%s), short=(%s,%s)): the "
            "first cross-timeframe swap requires accumulated 1-min history "
            "to rebuild the new interval's bars before it can apply.",
            l_kt, l_km, s_kt, s_km,
        )
    return errors
