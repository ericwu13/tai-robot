"""Aggregate 1-min bars into N-min bars using session-aligned boundaries.

Uses session start (08:45 AM / 15:00 Night) as epoch instead of midnight,
so bar boundaries align naturally with TAIFEX trading sessions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..market_data.models import Bar
from ..market_data.sessions import session_align

logger = logging.getLogger(__name__)


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

        # Out-of-order bar guard (issue #78). A post-reconnect tick-history
        # replay can emit 1-min bars whose times go backwards; track the last
        # accepted bar time so a stale bar cannot corrupt the aggregation or
        # spawn a spurious extra bar. This tracks the RAW incoming ``bar.dt``,
        # NOT the session-aligned boundary, so consecutive 1-min bars within
        # the same aggregation window are both accepted.
        self._last_bar_time: datetime | None = None

    def _align_time(self, dt: datetime) -> datetime:
        """Align datetime to the target bar boundary (session-start-based)."""
        return session_align(dt, self._interval)

    def on_bar(self, bar: Bar) -> Bar | None:
        """Process an incoming bar. Returns a completed aggregated bar if boundary crossed.

        Pass-through for 1-min target: the incoming 1-min bar IS already the
        target size, so return it immediately. Without this fast path every
        1-min bar would be held as _current and only emitted on the NEXT
        bar's arrival, leaving the chart and strategy permanently 1 bar
        behind real time (issue #44).
        """
        # Drop out-of-order/duplicate bars from post-reconnect history replay
        # (issue #78). Guard on the raw incoming time BEFORE the 1-min
        # pass-through so stale bars are rejected in every timeframe.
        if self._last_bar_time is not None and bar.dt <= self._last_bar_time:
            logger.warning(
                "[AGG] Dropped out-of-order bar: %s <= last %s",
                bar.dt, self._last_bar_time,
            )
            return None
        self._last_bar_time = bar.dt

        if self._interval == 60:
            return bar

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

    def get_partial_bar(self) -> Bar | None:
        """Return a snapshot copy of the current in-progress bar, or None."""
        if self._current is None:
            return None
        c = self._current
        return Bar(
            symbol=c.symbol, dt=c.dt, open=c.open,
            high=c.high, low=c.low, close=c.close,
            volume=c.volume, interval=c.interval,
        )

    def flush(self) -> Bar | None:
        """Force-finalize the current partial bar (e.g. at session end)."""
        if self._current is None:
            return None
        return self._finalize()

    def reset(self) -> None:
        """Clear state."""
        self._current = None
        self._current_start = None
        self._last_bar_time = None

    def reset_stale_tracking(self) -> None:
        """Reset the out-of-order bar guard (issue #78) WITHOUT discarding the
        in-progress aggregation window.

        Call on reconnect / history-replay start so the first bar after the
        gap is always accepted, regardless of its timestamp relative to the
        last bar seen before the drop.
        """
        self._last_bar_time = None

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
