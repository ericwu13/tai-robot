"""MACD (Moving Average Convergence Divergence) indicator."""

from __future__ import annotations

from collections import namedtuple

from .ma import ema

MacdResult = namedtuple("MacdResult", ["macd_line", "signal_line", "histogram"])


def macd(
    values: list[int | float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> MacdResult | None:
    """Calculate MACD line, signal line, and histogram.

    Returns MacdResult(macd_line, signal_line, histogram) or None if
    insufficient data. Supports both attribute access (``m.signal_line``)
    and tuple unpacking (``line, signal, hist = macd(...)``).
    """
    if len(values) < slow_period + signal_period - 1:
        return None

    # Build MACD line series: fast_ema - slow_ema at each point
    macd_series = []
    for i in range(slow_period, len(values) + 1):
        window = values[:i]
        fast = ema(window, fast_period)
        slow = ema(window, slow_period)
        if fast is not None and slow is not None:
            macd_series.append(fast - slow)

    if len(macd_series) < signal_period:
        return None

    signal_line = ema(macd_series, signal_period)
    if signal_line is None:
        return None

    macd_line = macd_series[-1]
    histogram = macd_line - signal_line
    return MacdResult(macd_line, signal_line, histogram)
