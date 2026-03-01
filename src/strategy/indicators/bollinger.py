"""Bollinger Bands indicator."""

from __future__ import annotations

import math

from .ma import sma


def bollinger_bands(
    values: list[int | float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[float, float, float] | None:
    """Calculate Bollinger Bands.

    Returns (upper, middle, lower) or None if insufficient data.
    """
    if len(values) < period:
        return None

    window = values[-period:]
    middle = sum(window) / period

    variance = sum((v - middle) ** 2 for v in window) / period
    std_dev = math.sqrt(variance)

    upper = middle + num_std * std_dev
    lower = middle - num_std * std_dev
    return upper, middle, lower
