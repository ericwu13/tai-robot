"""Stochastic Oscillator (%K / %D) indicator."""

from __future__ import annotations

from collections import namedtuple

StochasticResult = namedtuple("StochasticResult", ["k", "d"])


def stochastic(
    highs: list[int | float],
    lows: list[int | float],
    closes: list[int | float],
    k_period: int = 14,
    d_period: int = 3,
) -> StochasticResult | None:
    """Calculate Stochastic Oscillator matching TradingView's ta.stoch().

    %K = 100 * (close - lowest_low(k_period)) / (highest_high(k_period) - lowest_low(k_period))
    %D = SMA(%K, d_period)

    Requires at least ``k_period + d_period - 1`` bars.
    Returns ``StochasticResult(k, d)`` or ``None`` if insufficient data.
    Supports both attribute access (``s.k``) and tuple unpacking
    (``k_val, d_val = stochastic(...)``).
    If highest_high equals lowest_low (zero range), returns ``(50.0, 50.0)``.
    """
    min_length = k_period + d_period - 1
    if len(highs) < min_length or len(lows) < min_length or len(closes) < min_length:
        return None

    # Compute %K for the last d_period bars so we can average them for %D
    k_values: list[float] = []
    for i in range(d_period):
        # Index into the tail of the data
        end = len(closes) - (d_period - 1 - i)
        start = end - k_period

        highest_high = max(highs[start:end])
        lowest_low = min(lows[start:end])

        if highest_high == lowest_low:
            k_values.append(50.0)
        else:
            k_values.append(
                100.0 * (closes[end - 1] - lowest_low) / (highest_high - lowest_low)
            )

    k_value = k_values[-1]
    d_value = sum(k_values) / d_period
    return StochasticResult(k_value, d_value)
