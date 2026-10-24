"""TAIFEX trading-day and settlement-day detection.

Combines the ``holidays`` package (Taiwan public holidays) with a small
override set for TAIFEX-specific deviations:
- ``OVERRIDE_HOLIDAYS``: extra non-trading days TAIFEX observes
- ``OVERRIDE_TRADING``: government public holidays where TAIFEX still trades
  (rare — occasional makeup days when a holiday is work-shifted)

Update the override sets at the start of each year if TAIFEX publishes
adjustments not yet reflected in the ``holidays`` package.

Emergency closures (typhoon and similar) are not in that calendar. An
operator lists them in ``taifex_closures.yaml`` next to ``settings.yaml``
(see ``taifex_closures.example.yaml``). A missing file changes nothing.
This module does not scrape TAIFEX notices.
"""

from __future__ import annotations

import calendar
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import holidays as _holidays
import yaml


_logger = logging.getLogger(__name__)

# Operator file. Gitignored. The example file is never read from here.
CLOSURES_OVERRIDE_FILENAME = "taifex_closures.yaml"

_CLOSURE_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# (path, mtime_ns, dates). A missing file is not cached so creating one
# is picked up on the next call.
_closures_cache: tuple[str, int, frozenset[date]] | None = None
_logged_closure_dates: set[date] = set()

# Manual overrides — only add dates that disagree with the `holidays` package.
# Keep small; they should be rare exceptions.
OVERRIDE_HOLIDAYS: frozenset[date] = frozenset({
    # Example: date(2026, 12, 31),  # year-end early close (if TAIFEX adds one)
})

OVERRIDE_TRADING: frozenset[date] = frozenset({
    # Example: date(2026, X, Y),  # government holiday but TAIFEX trades anyway
})

_MONTH_CODES = "ABCDEFGHIJKL"  # A=Jan .. L=Dec

# Warn-once flag for a broken `holidays` package in the frozen EXE.
_tw_holidays_broken: bool = False


def _tw_public_holidays(year: int) -> frozenset[date]:
    """Return TW public holidays for ``year`` as a set of dates.

    Falls back to an empty set (weekends-only detection) if the
    ``holidays`` package cannot resolve the TW country module — this
    happens in poorly-bundled frozen EXEs where
    ``holidays.countries.taiwan`` wasn't collected (issue #58).  A
    single warning is logged so the operator sees the degradation.
    """
    global _tw_holidays_broken
    try:
        return frozenset(_holidays.country_holidays("TW", years=year))
    except Exception as e:
        if not _tw_holidays_broken:
            _tw_holidays_broken = True
            _logger.warning(
                "TW holiday detection unavailable — falling back to "
                "weekend-only check. Settlement days that coincide with "
                "public holidays may be mis-dated. Cause: %s", e,
            )
        return frozenset()


def holiday_calendar_health(year: int | None = None) -> tuple[bool, str]:
    """Whether TW public holidays resolved for ``year``.

    ``(True, detail)`` when ``holidays.country_holidays("TW")`` returns a
    non-empty set. ``(False, detail)`` when the package raises or resolves
    empty — the same weekend-only answer ``is_taifex_holiday`` then gives
    (issue #58). The result is this call only; the warn-once latch does
    not stick a later good year as degraded.

    Monitor probes that escalate a missed night must treat False as
    "do not invent weekday TAIFEX holidays" (issue #155).
    """
    y = date.today().year if year is None else int(year)
    try:
        days = frozenset(_holidays.country_holidays("TW", years=y))
    except Exception as e:
        # Same warn-once log the trading-day check emits on this failure.
        _tw_public_holidays(y)
        return False, f"{type(e).__name__}: {e}"
    if not days:
        return False, f"TW public holidays for {y} resolved empty"
    return True, f"{len(days)} TW public holidays for {y}"


def closures_override_path() -> Path:
    """Path of the local emergency-closure file. It may not exist.

    Frozen EXE: next to the executable, same place as ``settings.yaml``.
    Source checkout: repository root.
    """
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parents[2]
    return root / CLOSURES_OVERRIDE_FILENAME


def _parse_closure_dates(text: str) -> frozenset[date]:
    """ISO dates on non-comment lines. A bad token is skipped.

    ``YYYY-MM-DD`` and ``YYYY-MM-DD DAY`` both close the whole date.
    A listed date is a full-day holiday; the optional session word does
    not narrow it.
    """
    found: set[date] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _CLOSURE_DATE_RE.search(stripped)
        if match is None:
            continue
        try:
            found.add(date.fromisoformat(match.group(1)))
        except ValueError:
            continue
    return frozenset(found)


def _dates_from_yaml_node(node) -> set[date]:
    """Dates yaml.safe_load already resolved, plus ISO dates inside strings.

    ``datetime`` is a ``date`` subclass, so it is handled first. A bad
    token inside one string is skipped by ``_parse_closure_dates``.
    """
    found: set[date] = set()
    if node is None or isinstance(node, (bool, int, float)):
        return found
    if isinstance(node, datetime):
        found.add(node.date())
    elif isinstance(node, date):
        found.add(node)
    elif isinstance(node, str):
        found.update(_parse_closure_dates(node))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.update(_dates_from_yaml_node(item))
    elif isinstance(node, dict):
        for key, value in node.items():
            found.update(_dates_from_yaml_node(key))
            found.update(_dates_from_yaml_node(value))
    return found


def load_closure_override_dates() -> frozenset[date]:
    """Dates in the closures file. Empty when the file is missing or unreadable.

    A missing file is silent. Bad bytes, a ``ValueError``, or a YAML
    parse error log one warning and yield no dates — the line regex does
    not run on a document YAML refused.
    """
    global _closures_cache
    path = closures_override_path()
    key = str(path)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return frozenset()
    if (_closures_cache is not None
            and _closures_cache[0] == key
            and _closures_cache[1] == mtime_ns):
        return _closures_cache[2]
    try:
        text = path.read_text(encoding="utf-8-sig")
        loaded = yaml.safe_load(text)
        dates = _parse_closure_dates(text) | _dates_from_yaml_node(loaded)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        _logger.warning("closures override unreadable, ignoring it: %s", exc)
        dates = frozenset()
    _closures_cache = (key, mtime_ns, dates)
    return dates


def _log_closure_holiday(d: date) -> None:
    """Log once per date. The message contains ``holiday`` (issue #179)."""
    if d in _logged_closure_dates:
        return
    _logged_closure_dates.add(d)
    _logger.warning("holiday %s", d.isoformat())


def is_taifex_holiday(d: date) -> bool:
    """True if TAIFEX is closed on ``d`` (weekend, public holiday, or override).

    A date listed in the closures file is closed even on a normal weekday.
    A missing file leaves this function on the static calendar only.
    """
    if d in load_closure_override_dates():
        _log_closure_holiday(d)
        return True
    if d in OVERRIDE_TRADING:
        return False
    if d in OVERRIDE_HOLIDAYS:
        return True
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return True
    return d in _tw_public_holidays(d.year)


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

    Also recognizes the static "near-month" placeholder codes used by
    Capital API continuous-quote symbols (TX00 → "TXFD0", MTX00 →
    "MTXFD0", TMF00 → "TM0000").  These don't match the standard
    {letter}{digit} suffix but the broker auto-routes them to the
    current front-month contract, so for settlement-day purposes they
    behave as front-month.
    """
    if not order_symbol:
        return False
    # Near-month quote-symbol placeholders (issue #66): without this
    # branch, is_front_month_contract("TM0000") returns False on
    # settlement day and minutes_until_session_close keeps using 13:45
    # instead of the actual 13:30 settlement close → false-positive
    # watchdog warnings between 13:30 and 13:45.
    from .kline_config import SYMBOL_CONFIG
    for cfg in SYMBOL_CONFIG.values():
        if cfg.get("near_month") and cfg.get("order_symbol") == order_symbol:
            return True
    if len(order_symbol) < 2:
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
