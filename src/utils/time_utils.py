"""Taiwan market hours and session detection utilities."""

from datetime import datetime, time, timezone, timedelta

# Taiwan Standard Time (UTC+8)
TST = timezone(timedelta(hours=8))

# Regular session: 08:45 - 13:45
REGULAR_OPEN = time(8, 45)
REGULAR_CLOSE = time(13, 45)

# After-hours session: 15:00 - 05:00 (next day)
AFTER_HOURS_OPEN = time(15, 0)
AFTER_HOURS_CLOSE = time(5, 0)


def now_tst() -> datetime:
    """Current time in Taiwan Standard Time."""
    return datetime.now(TST)


def is_regular_session(dt: datetime | None = None) -> bool:
    """Check if the given time falls within the regular trading session."""
    if dt is None:
        dt = now_tst()
    t = dt.time()
    return REGULAR_OPEN <= t <= REGULAR_CLOSE


def is_after_hours_session(dt: datetime | None = None) -> bool:
    """Check if the given time falls within the after-hours session."""
    if dt is None:
        dt = now_tst()
    t = dt.time()
    return t >= AFTER_HOURS_OPEN or t <= AFTER_HOURS_CLOSE


def is_market_open(dt: datetime | None = None) -> bool:
    """Check if any trading session is active."""
    return is_regular_session(dt) or is_after_hours_session(dt)


def parse_sk_date(date_int: int) -> datetime:
    """Parse SK date integer (YYYYMMDD) to datetime."""
    year = date_int // 10000
    month = (date_int % 10000) // 100
    day = date_int % 100
    return datetime(year, month, day, tzinfo=TST)


def parse_sk_time(time_hms: int, time_millismicros: int = 0) -> time:
    """Parse SK time integers to time object.

    time_hms: HHMMSS (e.g. 134500)
    time_millismicros: milliseconds * 1000 + microseconds
    """
    h = time_hms // 10000
    m = (time_hms % 10000) // 100
    s = time_hms % 100
    ms = time_millismicros // 1000
    us = time_millismicros % 1000
    return time(h, m, s, ms * 1000 + us)


def combine_sk_datetime(date_int: int, time_hms: int, time_millismicros: int = 0) -> datetime:
    """Combine SK date and time integers into a full datetime."""
    d = parse_sk_date(date_int)
    t = parse_sk_time(time_hms, time_millismicros)
    return d.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=t.microsecond)
