"""Relative Strength Index (RSI) indicator."""

from __future__ import annotations


def rsi(values: list[int | float], period: int = 14) -> float | None:
    """Calculate RSI using Wilder's smoothing method.

    Requires at least `period + 1` values.
    Returns a value between 0 and 100, or None if insufficient data.
    """
    if len(values) < period + 1:
        return None

    changes = [values[i] - values[i - 1] for i in range(1, len(values))]

    # Initial average gain/loss over the first `period` changes
    gains = [max(c, 0) for c in changes[:period]]
    losses = [max(-c, 0) for c in changes[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Wilder's smoothing for remaining changes
    for c in changes[period:]:
        gain = max(c, 0)
        loss = max(-c, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
