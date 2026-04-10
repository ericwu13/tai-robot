"""Bollinger Bands indicator."""

from __future__ import annotations

import math
from collections import namedtuple

from .ma import sma

BollingerResult = namedtuple("BollingerResult", ["upper", "middle", "lower"])


def bollinger_bands(
    values: list[int | float],
    period: int = 20,
    num_std: float = 2.0,
) -> BollingerResult | None:
    """Calculate Bollinger Bands.

    Returns BollingerResult(upper, middle, lower) or None if insufficient
    data. Supports both attribute access (``bb.middle``) and tuple
    unpacking (``upper, mid, lower = bollinger_bands(...)``).
    """
    if len(values) < period:
        return None

    window = values[-period:]
    middle = sum(window) / period

    variance = sum((v - middle) ** 2 for v in window) / period
    std_dev = math.sqrt(variance)

    upper = middle + num_std * std_dev
    lower = middle - num_std * std_dev
    return BollingerResult(upper, middle, lower)
