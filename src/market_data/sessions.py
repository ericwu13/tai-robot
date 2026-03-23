"""Taiwan futures session utilities.

Bar datetimes in this codebase are in Taiwan time (TWT / UTC+8).

Taiwan Futures Exchange sessions:
- Day session:   08:45 ~ 13:45 TWT
- Night session: 15:00 ~ 05:00+1 TWT
"""

from __future__ import annotations

from datetime import datetime

# Session boundaries in minutes from midnight (TWT)
DAY_OPEN = 8 * 60 + 45    # 08:45 = 525
DAY_CLOSE = 13 * 60 + 45  # 13:45 = 825
NIGHT_OPEN = 15 * 60       # 15:00 = 900
NIGHT_CLOSE = 5 * 60       # 05:00 = 300


def is_last_bar_of_session(dt: datetime, kline_minute: int = 60) -> bool:
    """Check if bar at *dt* is the last bar of a Taiwan futures session.

    The last bar is the one whose ``[open, open + interval)`` range
    covers the session close time.

    Args:
        dt: Bar open time in **Taiwan time (TWT/UTC+8)**.
        kline_minute: Bar interval in minutes.

    Returns:
        True if this bar is the last of its session.

    Examples (60-min bars):
        >>> is_last_bar_of_session(datetime(2026, 2, 4, 12, 45), 60)  # day close
        True
        >>> is_last_bar_of_session(datetime(2026, 2, 5,  4,  0), 60)  # night close
        True
        >>> is_last_bar_of_session(datetime(2026, 2, 4, 11, 45), 60)
        False
        >>> is_last_bar_of_session(datetime(2026, 2, 4, 20,  0), 60)
        False

    Examples (15-min bars):
        >>> is_last_bar_of_session(datetime(2026, 2, 4, 13, 30), 15)  # day close
        True
        >>> is_last_bar_of_session(datetime(2026, 2, 5,  4, 45), 15)  # night close
        True
    """
    bar_start = dt.hour * 60 + dt.minute
    bar_end = bar_start + kline_minute

    # Day session: bar opens in [08:45, 13:45) and bar covers 13:45
    if DAY_OPEN <= bar_start < DAY_CLOSE:
        return bar_end >= DAY_CLOSE

    # Night session (after-midnight portion): bar opens in [00:00, 05:00)
    if bar_start < NIGHT_CLOSE:
        return bar_end >= NIGHT_CLOSE

    # Night session (before-midnight portion 15:00-23:59): never the last bar
    # because the session continues past midnight until 05:00.
    return False
