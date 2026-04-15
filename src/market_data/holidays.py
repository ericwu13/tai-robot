"""TAIFEX trading-day and settlement-day detection.

Combines the ``holidays`` package (Taiwan public holidays) with a small
override set for TAIFEX-specific deviations:
- ``OVERRIDE_HOLIDAYS``: extra non-trading days TAIFEX observes
- ``OVERRIDE_TRADING``: government public holidays where TAIFEX still trades
  (rare — occasional makeup days when a holiday is work-shifted)

Update the override sets at the start of each year if TAIFEX publishes
adjustments not yet reflected in the ``holidays`` package.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

import holidays as _holidays


# Manual overrides — only add dates that disagree with the `holidays` package.
# Keep small; they should be rare exceptions.
OVERRIDE_HOLIDAYS: frozenset[date] = frozenset({
    # Example: date(2026, 12, 31),  # year-end early close (if TAIFEX adds one)
})

OVERRIDE_TRADING: frozenset[date] = frozenset({
    # Example: date(2026, X, Y),  # government holiday but TAIFEX trades anyway
})

_MONTH_CODES = "ABCDEFGHIJKL"  # A=Jan .. L=Dec


def is_taifex_holiday(d: date) -> bool:
    """True if TAIFEX is closed on ``d`` (weekend, public holiday, or override)."""
    if d in OVERRIDE_TRADING:
        return False
    if d in OVERRIDE_HOLIDAYS:
        return True
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return True
    return d in _holidays.country_holidays("TW", years=d.year)


def next_trading_day(d: date) -> date:
    """Return the first non-holiday day >= ``d``."""
    while is_taifex_holiday(d):
        d += timedelta(days=1)
    return d


def third_wednesday(year: int, month: int) -> date:
    """Date of the 3rd Wednesday of (year, month)."""
    cal = calendar.monthcalendar(year, month)
    wednesdays = [week[calendar.WEDNESDAY] for week in cal if week[calendar.WEDNESDAY] != 0]
    return date(year, month, wednesdays[2])


def settlement_day(year: int, month: int) -> date:
    """TAIFEX monthly settlement day for (year, month).

    Normally the 3rd Wednesday.  If that Wednesday is a holiday, settlement
    moves to the next trading day.
    """
    return next_trading_day(third_wednesday(year, month))


def is_settlement_day(d: date | datetime | None = None) -> bool:
    """True if ``d`` is the TAIFEX settlement day for its calendar month."""
    if d is None:
        from src.live.live_runner import _taipei_now
        d = _taipei_now()
    if isinstance(d, datetime):
        d = d.date()
    return d == settlement_day(d.year, d.month)


def is_front_month_contract(order_symbol: str, d: date | datetime | None = None) -> bool:
    """True if ``order_symbol`` matches the current month's expiry letter.

    Front-month means the contract that settles in this calendar month
    (e.g., on 2026-04-15 the front month is the April contract whose
    symbol ends with ``D6`` since D=April, 6=2026).

    Back-month contracts (May, June, ...) keep trading until 13:45 even
    on settlement day; only the front-month is force-settled at 13:30.
    """
    if not order_symbol or len(order_symbol) < 2:
        return False
    if d is None:
        from src.live.live_runner import _taipei_now
        d = _taipei_now()
    if isinstance(d, datetime):
        d = d.date()
    expected_letter = _MONTH_CODES[d.month - 1]
    expected_year_digit = str(d.year % 10)
    # order_symbol ends with {month_letter}{year_digit}, e.g. "TXFD6"
    return order_symbol[-2] == expected_letter and order_symbol[-1] == expected_year_digit
