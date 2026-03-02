"""Aggregate 1-min bars into N-min bars using time-aligned boundaries.

Same midnight-based alignment as BarBuilder, but bar-to-bar instead of tick-to-bar.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..market_data.models import Bar


class BarAggregator:
    """Aggregates smaller bars into larger timeframe bars.

    Example: feed 1-min bars, get completed H4 bars when boundary is crossed.
    """

    def __init__(self, symbol: str, target_interval: int):
        """
        Args:
            symbol: Instrument symbol.
            target_interval: Target bar interval in seconds (e.g. 14400 for H4).
        """
        self._symbol = symbol
        self._interval = target_interval
        self._current: Bar | None = None
        self._current_start: datetime | None = None

    def _align_time(self, dt: datetime) -> datetime:
        """Align datetime to the target bar boundary (midnight-based)."""
        epoch = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds = int((dt - epoch).total_seconds())
        aligned = (seconds // self._interval) * self._interval
        return epoch + timedelta(seconds=aligned)

    def on_bar(self, bar: Bar) -> Bar | None:
        """Process an incoming bar. Returns a completed aggregated bar if boundary crossed."""
        bar_time = self._align_time(bar.dt)

        if self._current is None:
            self._start(bar_time, bar)
            return None

        if bar_time == self._current_start:
            self._update(bar)
            return None

        # Boundary crossed — finalize previous and start new
        completed = self._finalize()
        self._start(bar_time, bar)
        return completed

    def flush(self) -> Bar | None:
        """Force-finalize the current partial bar (e.g. at session end)."""
        if self._current is None:
            return None
        return self._finalize()

    def reset(self) -> None:
        """Clear state."""
        self._current = None
        self._current_start = None

    def _start(self, aligned_dt: datetime, bar: Bar) -> None:
        self._current_start = aligned_dt
        self._current = Bar(
            symbol=self._symbol,
            dt=aligned_dt,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            interval=self._interval,
        )

    def _update(self, bar: Bar) -> None:
        c = self._current
        if bar.high > c.high:
            c.high = bar.high
        if bar.low < c.low:
            c.low = bar.low
        c.close = bar.close
        c.volume += bar.volume

    def _finalize(self) -> Bar:
        completed = self._current
        self._current = None
        self._current_start = None
        return completed


def aggregate_bars(bars_1m: list[Bar], target_interval: int) -> list[Bar]:
    """Re-aggregate 1-min bars into any target interval.

    Pure function — creates a fresh BarAggregator, replays all bars,
    and flushes the partial bar at the end.

    Args:
        bars_1m: List of 1-minute Bar objects.
        target_interval: Target interval in seconds (e.g. 900 for 15m).

    Returns:
        List of aggregated Bar objects.
    """
    if not bars_1m:
        return []

    if target_interval == 60:
        return list(bars_1m)

    symbol = bars_1m[0].symbol
    agg = BarAggregator(symbol, target_interval)
    result: list[Bar] = []

    for bar in bars_1m:
        completed = agg.on_bar(bar)
        if completed is not None:
            result.append(completed)

    # Flush partial bar at the end
    partial = agg.flush()
    if partial is not None:
        result.append(partial)

    return result
