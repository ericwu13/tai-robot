"""Tests for COM-API close-time → open-time bar label normalization.

The Capital COM API (`SKQuoteLib_RequestKLineAMByDate`) returns N-minute
intraday bars whose timestamp is the bar CLOSE time. The rest of this
codebase (BarBuilder, BarAggregator, is_last_bar_of_session, strategies)
assumes `bar.dt` is the bar OPEN time. `parse_kline_strings()` must auto-
detect close-time labeling and shift bars by `-interval` so the rest of
the system sees a single, consistent convention.

Empirical evidence for the close-time labeling: in the user's chat log
backtest report, an `kline_minute=60` strategy entered on bars at exactly
`13:45` and `05:00` (which are the AM and night session CLOSE times) and
NEVER on `08:45` or `15:00` (the session OPEN times). That pattern is
impossible under open-time labeling.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.backtest.data_loader import (
    _detect_label_convention,
    normalize_bar_label_to_open,
    parse_kline_strings,
)
from src.market_data.models import Bar
from src.market_data.sessions import is_last_bar_of_session


def _bar(dt: datetime, interval: int = 3600) -> Bar:
    return Bar(symbol="TX00", dt=dt, open=100, high=110,
               low=90, close=105, volume=1, interval=interval)


# ── Detection ──────────────────────────────────────────────────────────


def test_detect_open_time_60min_am_session():
    """60-min open-time AM bars include exactly 08:45."""
    bars = [
        _bar(datetime(2026, 1, 7, 8, 45)),
        _bar(datetime(2026, 1, 7, 9, 45)),
        _bar(datetime(2026, 1, 7, 10, 45)),
        _bar(datetime(2026, 1, 7, 11, 45)),
        _bar(datetime(2026, 1, 7, 12, 45)),
    ]
    assert _detect_label_convention(bars, 3600) == "open"


def test_detect_close_time_60min_am_session():
    """60-min close-time AM bars include exactly 13:45 (and never 08:45)."""
    bars = [
        _bar(datetime(2026, 1, 7, 9, 45)),
        _bar(datetime(2026, 1, 7, 10, 45)),
        _bar(datetime(2026, 1, 7, 11, 45)),
        _bar(datetime(2026, 1, 7, 12, 45)),
        _bar(datetime(2026, 1, 7, 13, 45)),
    ]
    assert _detect_label_convention(bars, 3600) == "close"


def test_detect_open_time_60min_night_session():
    """60-min open-time night bars include exactly 15:00."""
    bars = [
        _bar(datetime(2026, 1, 7, 15, 0)),
        _bar(datetime(2026, 1, 7, 16, 0)),
        _bar(datetime(2026, 1, 7, 17, 0)),
    ]
    assert _detect_label_convention(bars, 3600) == "open"


def test_detect_close_time_60min_night_session():
    """60-min close-time night bars include exactly 05:00 next day."""
    bars = [
        _bar(datetime(2026, 1, 8, 3, 0)),
        _bar(datetime(2026, 1, 8, 4, 0)),
        _bar(datetime(2026, 1, 8, 5, 0)),
    ]
    assert _detect_label_convention(bars, 3600) == "close"


def test_detect_unknown_when_no_boundary_bars():
    """Bars that miss both opens and closes leave the convention undetermined."""
    bars = [
        _bar(datetime(2026, 1, 7, 10, 45)),
        _bar(datetime(2026, 1, 7, 11, 45)),
        _bar(datetime(2026, 1, 7, 12, 45)),
    ]
    assert _detect_label_convention(bars, 3600) == "unknown"


def test_detect_15min_close_time():
    """15-min close-time bars include 09:00 (close of 08:45-09:00) and 13:45."""
    bars = [
        _bar(datetime(2026, 1, 7, 9, 0), interval=900),
        _bar(datetime(2026, 1, 7, 9, 15), interval=900),
        _bar(datetime(2026, 1, 7, 13, 45), interval=900),
    ]
    assert _detect_label_convention(bars, 900) == "close"


def test_detect_15min_open_time():
    """15-min open-time bars include 08:45 (the session-open bar)."""
    bars = [
        _bar(datetime(2026, 1, 7, 8, 45), interval=900),
        _bar(datetime(2026, 1, 7, 9, 0), interval=900),
        _bar(datetime(2026, 1, 7, 13, 30), interval=900),
    ]
    assert _detect_label_convention(bars, 900) == "open"


def test_detect_daily_returns_unknown():
    """Daily bars (interval >= 86400) have no time-of-day; convention N/A."""
    bars = [
        _bar(datetime(2026, 1, 7, 0, 0), interval=86400),
        _bar(datetime(2026, 1, 8, 0, 0), interval=86400),
    ]
    assert _detect_label_convention(bars, 86400) == "unknown"


def test_detect_ambiguous_data_returns_unknown():
    """Data with bars at BOTH session opens AND closes is pathological."""
    bars = [
        _bar(datetime(2026, 1, 7, 8, 45)),   # AM open
        _bar(datetime(2026, 1, 7, 13, 45)),  # AM close
    ]
    # Should warn and refuse to guess.
    assert _detect_label_convention(bars, 3600) == "unknown"


# ── Normalization ──────────────────────────────────────────────────────


def test_normalize_close_time_to_open_time_60min():
    bars = [
        _bar(datetime(2026, 1, 7, 9, 45)),
        _bar(datetime(2026, 1, 7, 10, 45)),
        _bar(datetime(2026, 1, 7, 13, 45)),  # close-marker bar
    ]
    out = normalize_bar_label_to_open(bars, 3600)
    assert [b.dt for b in out] == [
        datetime(2026, 1, 7, 8, 45),
        datetime(2026, 1, 7, 9, 45),
        datetime(2026, 1, 7, 12, 45),
    ]
    # OHLCV preserved
    assert all(b.open == 100 and b.close == 105 for b in out)


def test_normalize_open_time_is_noop():
    bars = [
        _bar(datetime(2026, 1, 7, 8, 45)),
        _bar(datetime(2026, 1, 7, 9, 45)),
    ]
    out = normalize_bar_label_to_open(bars, 3600)
    assert [b.dt for b in out] == [b.dt for b in bars]


def test_normalize_unknown_is_noop():
    bars = [
        _bar(datetime(2026, 1, 7, 10, 45)),
        _bar(datetime(2026, 1, 7, 11, 45)),
    ]
    out = normalize_bar_label_to_open(bars, 3600)
    assert [b.dt for b in out] == [b.dt for b in bars]


def test_normalize_empty_list():
    assert normalize_bar_label_to_open([], 3600) == []


def test_normalized_close_time_passes_is_last_bar_of_session():
    """After normalization, the AM-close bar's open time satisfies the
    session-utility used by force-close logic."""
    bars = [
        _bar(datetime(2026, 1, 7, 9, 45)),
        _bar(datetime(2026, 1, 7, 13, 45)),  # AM session-close marker
    ]
    out = normalize_bar_label_to_open(bars, 3600)
    last = out[-1]
    # last.dt is now 12:45 (the bar that opens at 12:45 and covers the AM close)
    assert last.dt == datetime(2026, 1, 7, 12, 45)
    assert is_last_bar_of_session(last.dt, 60) is True


# ── End-to-end via parse_kline_strings ────────────────────────────────


def test_parse_kline_strings_normalizes_close_time_60min():
    """Mimic Capital COM old-format output for AM 60-min bars."""
    lines = [
        "01/07/2026 09:45,30000,30100,29900,30050,1000",
        "01/07/2026 10:45,30050,30200,30000,30150,1100",
        "01/07/2026 11:45,30150,30300,30100,30250,1200",
        "01/07/2026 12:45,30250,30400,30200,30350,1300",
        "01/07/2026 13:45,30350,30500,30300,30450,1400",
    ]
    bars = parse_kline_strings(lines, symbol="TX00", interval=3600)
    # After normalization, first bar should have dt = 08:45 (the open of the
    # bar that the API labeled 09:45-close).
    assert bars[0].dt == datetime(2026, 1, 7, 8, 45)
    assert bars[-1].dt == datetime(2026, 1, 7, 12, 45)
    # OHLCV preserved
    assert bars[0].open == 30000
    assert bars[0].close == 30050
    assert bars[-1].close == 30450


def test_parse_kline_strings_open_time_unchanged():
    """Open-time labeled input must pass through unchanged."""
    lines = [
        "01/07/2026 08:45,30000,30100,29900,30050,1000",
        "01/07/2026 09:45,30050,30200,30000,30150,1100",
        "01/07/2026 10:45,30150,30300,30100,30250,1200",
    ]
    bars = parse_kline_strings(lines, symbol="TX00", interval=3600)
    assert bars[0].dt == datetime(2026, 1, 7, 8, 45)
    assert bars[-1].dt == datetime(2026, 1, 7, 10, 45)


def test_parse_kline_strings_normalizes_15min_close_time():
    """15-min close-time AM bars: first close = 09:00, last close = 13:45."""
    lines = [
        "01/07/2026 09:00,30000,30100,29900,30050,500",
        "01/07/2026 09:15,30050,30100,30000,30075,400",
        "01/07/2026 13:45,30200,30300,30100,30250,600",
    ]
    bars = parse_kline_strings(lines, symbol="TX00", interval=900)
    assert bars[0].dt == datetime(2026, 1, 7, 8, 45)  # first AM open
    assert bars[-1].dt == datetime(2026, 1, 7, 13, 30)  # last AM-close bar's open


def test_parse_kline_strings_close_time_night_session():
    """60-min close-time night session: 16:00..05:00 → opens 15:00..04:00."""
    lines = [
        "01/07/2026 16:00,30000,30100,29900,30050,1000",
        "01/07/2026 17:00,30050,30100,30000,30075,500",
        "01/08/2026 05:00,30200,30300,30100,30250,800",
    ]
    bars = parse_kline_strings(lines, symbol="TX00", interval=3600)
    assert bars[0].dt == datetime(2026, 1, 7, 15, 0)
    assert bars[-1].dt == datetime(2026, 1, 8, 4, 0)
    assert is_last_bar_of_session(bars[-1].dt, 60) is True
