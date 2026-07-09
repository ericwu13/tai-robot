"""Pure functions for regime switching logic.

Session-boundary detection, classification triggers, swap decisions —
all testable without COM, Tk, or live infrastructure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def should_classify(
    now: datetime,
    session_slot_result: tuple[str, str],
    last_assessed_key: str,
    minutes_to_close: float,
) -> bool:
    """Whether the runner should fire classification right now.

    Only fires on NIGHT sessions, within 2 minutes of close, and only
    once per on-disk dedup key.
    """
    _date, slot = session_slot_result
    if slot != "NIGHT":
        return False
    if minutes_to_close > 2:
        return False
    dedup_key = f"{_date}|NIGHT"
    if dedup_key == last_assessed_key:
        return False
    return True


def validate_leg_strategies(
    long_name: str,
    short_name: str,
    registry: dict,
) -> list[str]:
    """Validate that both leg strategies exist and share the same timeframe.

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
        errors.append(
            f"Timeframe mismatch: long=({l_kt},{l_km}) vs short=({s_kt},{s_km})"
        )
    return errors
