"""CSV cache for downloaded data (TAIFEX, etc.)."""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from ..market_data.models import Bar

_CACHE_ROOT = Path(__file__).resolve().parent.parent.parent / "data"


def get_cache_path(source: str, symbol: str, suffix: str) -> Path:
    """Return cache file path, e.g. data/taifex/TX_daily.csv."""
    return _CACHE_ROOT / source / f"{symbol}{suffix}"


def save_bars_csv(bars: list[Bar], path: Path) -> None:
    """Save bars as standard OHLCV CSV with headers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["datetime", "open", "high", "low", "close", "volume"])
        for b in bars:
            writer.writerow([
                b.dt.strftime("%Y/%m/%d"),
                b.open, b.high, b.low, b.close, b.volume,
            ])


def load_bars_csv(path: Path, symbol: str, interval: int) -> list[Bar] | None:
    """Load cached bars from CSV. Returns None if file doesn't exist."""
    if not path.exists():
        return None
    bars: list[Bar] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                dt = datetime.strptime(row["datetime"], "%Y/%m/%d")
                bars.append(Bar(
                    symbol=symbol,
                    dt=dt,
                    open=int(row["open"]),
                    high=int(row["high"]),
                    low=int(row["low"]),
                    close=int(row["close"]),
                    volume=int(row["volume"]),
                    interval=interval,
                ))
            except (KeyError, ValueError):
                continue
    bars.sort(key=lambda b: b.dt)
    return bars


def cache_covers_range(path: Path, start: date, end: date) -> bool:
    """Check if the cached CSV covers the requested date range."""
    if not path.exists():
        return False
    first_dt = None
    last_dt = None
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                dt = datetime.strptime(row["datetime"], "%Y/%m/%d").date()
                if first_dt is None or dt < first_dt:
                    first_dt = dt
                if last_dt is None or dt > last_dt:
                    last_dt = dt
            except (KeyError, ValueError):
                continue
    if first_dt is None or last_dt is None:
        return False
    return first_dt <= start and last_dt >= end
