"""Data loaders: parse Capital API KLine strings and CSV files into Bar objects."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..market_data.models import Bar


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
