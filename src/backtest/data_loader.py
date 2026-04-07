"""Data loaders: parse Capital API KLine strings and CSV files into Bar objects."""

from __future__ import annotations

import csv
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..market_data.models import Bar

_log = logging.getLogger(__name__)


_DATE_FORMATS = [
    "%Y/%m/%d %H:%M",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y%m%d %H:%M",
    "%m/%d/%Y",
    "%Y/%m/%d",
    "%Y-%m-%d",
]

_TZ_TAIPEI = timezone(timedelta(hours=8))

# TAIFEX session boundaries in minutes-from-midnight TWT
# (kept local to avoid a circular import with src.market_data.sessions)
_DAY_OPEN = 8 * 60 + 45    # 08:45 = 525
_DAY_CLOSE = 13 * 60 + 45  # 13:45 = 825
_NIGHT_OPEN = 15 * 60      # 15:00 = 900
_NIGHT_CLOSE = 5 * 60      # 05:00 = 300


def _detect_label_convention(bars: list[Bar], interval_seconds: int) -> str:
    """Detect whether intraday Bars are labeled by their open-time or close-time.

    Capital's COM KLine API (SKQuoteLib_RequestKLineAMByDate) returns N-minute
    bars whose timestamp is the bar's CLOSE time. The rest of this codebase
    (BarBuilder, BarAggregator, is_last_bar_of_session, every test) assumes
    bar.dt is the OPEN time. Without normalization, strategies that filter by
    `bar.dt.hour` or call `is_last_bar_of_session()` end up off by one
    interval on COM-API data.

    Detection rule (unambiguous on TAIFEX):
      - Any bar whose dt lands EXACTLY on a session OPEN  (08:45 or 15:00) → 'open'
      - Any bar whose dt lands EXACTLY on a session CLOSE (13:45 or 05:00) → 'close'
    Open-time labeled bars produce timestamps at session opens (08:45/15:00) and
    NEVER at session closes (13:45/05:00); close-time labeled bars produce the
    inverse. The two patterns cannot coexist for the same data source.

    Args:
        bars: parsed Bar list (any order, any count).
        interval_seconds: bar interval in seconds (only intraday matters; daily
            and above are returned as 'unknown' since they have no time-of-day).

    Returns:
        'open', 'close', or 'unknown'.
    """
    if interval_seconds <= 0 or interval_seconds >= 86400:
        return "unknown"

    saw_open = False
    saw_close = False
    for b in bars:
        if b.dt is None:
            continue
        m = b.dt.hour * 60 + b.dt.minute
        if m == _DAY_OPEN or m == _NIGHT_OPEN:
            saw_open = True
        elif m == _DAY_CLOSE or m == _NIGHT_CLOSE:
            saw_close = True
        # Early exit once both signals are found (the first one wins, but this
        # also surfaces a contradiction for the warning below).
        if saw_open and saw_close:
            break

    if saw_open and not saw_close:
        return "open"
    if saw_close and not saw_open:
        return "close"
    if saw_open and saw_close:
        # Pathological: same data set has both open- and close-aligned bars.
        # Refuse to guess; let the caller log + leave bars untouched.
        _log.warning(
            "KLine label convention is ambiguous (data has bars at BOTH "
            "session opens AND session closes). Leaving timestamps unchanged."
        )
        return "unknown"
    return "unknown"


def normalize_bar_label_to_open(bars: list[Bar], interval_seconds: int) -> list[Bar]:
    """Return *bars* with timestamps shifted to OPEN-time labeling if needed.

    This is a no-op when the data is already open-time labeled or when the
    convention can't be determined.
    """
    if not bars:
        return bars
    convention = _detect_label_convention(bars, interval_seconds)
    if convention != "close":
        return bars

    delta = timedelta(seconds=interval_seconds)
    shifted = [replace(b, dt=b.dt - delta) for b in bars]
    _log.info(
        "Normalized %d KLine bars from CLOSE-time to OPEN-time labeling "
        "(interval=%ds). The COM API labels intraday N-minute bars by their "
        "close time; this codebase expects bar.dt = bar open time.",
        len(bars), interval_seconds,
    )
    return shifted


def _detect_date_format(dt_str: str) -> str | None:
    """Try all date formats once and return the one that works."""
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(dt_str, fmt)
            return fmt
        except ValueError:
            continue
    return None


def parse_kline_strings(
    lines: list[str],
    symbol: str = "TX00",
    interval: int = 14400,
) -> list[Bar]:
    """Parse multiple KLine data strings into a sorted list of Bars.

    Auto-detects date format on first line, then reuses it for all lines.
    Auto-normalizes Capital COM-API close-time labels to open-time labels so
    that downstream code (BarAggregator, is_last_bar_of_session, strategies
    that read bar.dt.hour) sees the same convention as live BarBuilder bars.
    """
    if not lines:
        return []

    bars: list[Bar] = []
    fmt: str | None = None

    for i, line in enumerate(lines):
        parts = line.strip().split(",")
        if len(parts) < 6:
            if i < 3:
                print(f"[data_loader] FAILED to parse line {i}: {line!r}")
            continue

        try:
            dt_str = parts[0].strip()

            # Detect format on first line, reuse for all subsequent
            if fmt is None:
                fmt = _detect_date_format(dt_str)
                if fmt is None:
                    if i < 3:
                        print(f"[data_loader] FAILED to parse line {i}: {line!r}")
                    continue

            dt = datetime.strptime(dt_str, fmt)
            bars.append(Bar(
                symbol=symbol,
                dt=dt,
                open=int(float(parts[1])),
                high=int(float(parts[2])),
                low=int(float(parts[3])),
                close=int(float(parts[4])),
                volume=int(float(parts[5])),
                interval=interval,
            ))
        except (ValueError, IndexError):
            if i < 3:
                print(f"[data_loader] FAILED to parse line {i}: {line!r}")

    bars.sort(key=lambda b: b.dt)
    bars = normalize_bar_label_to_open(bars, interval)
    return bars


def load_bars_from_csv(
    path: str | Path,
    symbol: str = "TX00",
    interval: int = 14400,
) -> list[Bar]:
    """Load bars from a CSV file.

    Supports three formats:
    1. Capital API format: "MM/DD/YYYY HH:MM, Open, High, Low, Close, Volume"
    2. Standard OHLCV CSV with headers: datetime, open, high, low, close, volume
    3. TradingView export: time (Unix timestamp), open, high, low, close [, extras...]
    """
    path = Path(path)
    bars: list[Bar] = []

    with open(path, "r", encoding="utf-8") as f:
        first_line = f.readline().strip()
        f.seek(0)

        # Check if first line looks like a header
        if first_line.lower().startswith(("datetime", "date", "time", "dt")):
            reader = csv.DictReader(f)
            # Find the datetime key once from the header
            fieldnames = reader.fieldnames or []
            dt_key = next((k for k in fieldnames if k.lower() in ("datetime", "date", "dt", "time")), None)
            if dt_key is None:
                return bars

            # Build column key map once (case-insensitive lookup)
            lower_map = {k.lower(): k for k in fieldnames}
            open_key = lower_map.get("open", "")
            high_key = lower_map.get("high", "")
            low_key = lower_map.get("low", "")
            close_key = lower_map.get("close", "")
            vol_key = lower_map.get("volume", "")

            # Detect format: check first data value
            is_unix = None
            date_fmt: str | None = None

            for row in reader:
                try:
                    dt_str = row[dt_key]

                    # Auto-detect on first row
                    if is_unix is None:
                        dt_str_stripped = dt_str.strip()
                        if dt_str_stripped.isdigit():
                            is_unix = True
                        else:
                            is_unix = False
                            date_fmt = _detect_date_format(dt_str_stripped)
                            if date_fmt is None:
                                continue

                    if is_unix:
                        ts = int(dt_str)
                        dt = datetime.fromtimestamp(ts, tz=_TZ_TAIPEI).replace(tzinfo=None)
                    else:
                        dt = datetime.strptime(dt_str.strip(), date_fmt)

                    open_ = int(float(row[open_key])) if open_key else 0
                    high = int(float(row[high_key])) if high_key else 0
                    low = int(float(row[low_key])) if low_key else 0
                    close = int(float(row[close_key])) if close_key else 0
                    volume = int(float(row[vol_key])) if vol_key else 0

                    bars.append(Bar(
                        symbol=symbol, dt=dt, open=open_, high=high,
                        low=low, close=close, volume=volume, interval=interval,
                    ))
                except (KeyError, ValueError):
                    continue
        else:
            # Capital API raw format (no headers) - use fast batch parser
            lines = f.read().splitlines()
            return parse_kline_strings(lines, symbol, interval)

    bars.sort(key=lambda b: b.dt)
    return bars
