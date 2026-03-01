"""Moving average indicators: SMA and EMA.

All functions are pure — no state, easy to test.
"""

from __future__ import annotations


def sma(values: list[int | float], period: int) -> float | None:
    """Simple Moving Average over the last `period` values.

    Returns None if not enough data.
    """
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: list[int | float], period: int) -> float | None:
    """Exponential Moving Average over all values using the given period.

    Uses the standard multiplier: 2 / (period + 1).
    Returns None if not enough data.
    """
    if len(values) < period:
        return None
    multiplier = 2.0 / (period + 1)
    # Seed with SMA of the first `period` values
    result = sum(values[:period]) / period
    for v in values[period:]:
        result = (v - result) * multiplier + result
    return result
