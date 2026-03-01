"""Aggregate ticks into time-aligned OHLCV bars.

Emits completed bars as BAR events on the EventBus.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .models import Bar, Tick
from ..gateway.event_bus import Event, EventBus, EventType

logger = logging.getLogger(__name__)


class BarBuilder:
    """Builds OHLCV bars from a stream of ticks.

    Bars are time-aligned: a 60s bar starting at 09:00:00 covers
    ticks from 09:00:00.000 up to (but not including) 09:01:00.000.
    """

    def __init__(self, symbol: str, interval: int, event_bus: EventBus | None = None):
        self._symbol = symbol
        self._interval = interval  # seconds
        self._event_bus = event_bus

        self._current_bar: Bar | None = None
        self._bar_start: datetime | None = None
        self._completed_bars: list[Bar] = []

    @property
    def completed_bars(self) -> list[Bar]:
        return self._completed_bars

    @property
    def current_bar(self) -> Bar | None:
        return self._current_bar

    def _align_time(self, dt: datetime) -> datetime:
        """Align a datetime to the bar boundary."""
        epoch = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_since_midnight = (dt - epoch).total_seconds()
        aligned_seconds = (int(seconds_since_midnight) // self._interval) * self._interval
        return epoch + timedelta(seconds=aligned_seconds)

    def on_tick(self, tick: Tick) -> Bar | None:
        """Process a tick. Returns a completed bar if a bar boundary was crossed."""
        bar_time = self._align_time(tick.dt)

        # First tick ever
        if self._current_bar is None:
            self._start_new_bar(bar_time, tick)
            return None

        # Same bar period
        if bar_time == self._bar_start:
            self._update_bar(tick)
            return None

        # New bar period — finalize previous bar and start new one
        completed = self._finalize_bar()
        self._start_new_bar(bar_time, tick)
        return completed

    def _start_new_bar(self, bar_time: datetime, tick: Tick) -> None:
        self._bar_start = bar_time
        self._current_bar = Bar(
            symbol=self._symbol,
            dt=bar_time,
            open=tick.price,
            high=tick.price,
            low=tick.price,
            close=tick.price,
            volume=tick.qty,
            interval=self._interval,
        )

    def _update_bar(self, tick: Tick) -> None:
        bar = self._current_bar
        if tick.price > bar.high:
            bar.high = tick.price
        if tick.price < bar.low:
            bar.low = tick.price
        bar.close = tick.price
        bar.volume += tick.qty

    def _finalize_bar(self) -> Bar:
        bar = self._current_bar
        self._completed_bars.append(bar)

        if self._event_bus:
            self._event_bus.publish(Event(type=EventType.BAR, data=bar))

        logger.debug(
            "Bar complete: %s O=%d H=%d L=%d C=%d V=%d",
            bar.dt, bar.open, bar.high, bar.low, bar.close, bar.volume,
        )
        return bar

    def flush(self) -> Bar | None:
        """Force-finalize the current bar (e.g. at session end)."""
        if self._current_bar is None:
            return None
        return self._finalize_bar()
