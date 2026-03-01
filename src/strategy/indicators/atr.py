"""Average True Range (ATR) indicator."""

from __future__ import annotations


def true_range(high: int, low: int, prev_close: int) -> int:
    """Calculate True Range for a single bar."""
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(
    highs: list[int], lows: list[int], closes: list[int], period: int = 14,
) -> float | None:
    """Calculate ATR using RMA (Wilder's smoothing), matching TradingView's ta.atr().

    Returns the current ATR value, or None if insufficient data.
    """
    if len(highs) < period + 1:
        return None

    # Calculate true ranges (need prev_close, so start from index 1)
    trs = []
    for i in range(1, len(highs)):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))

    if len(trs) < period:
        return None

    # RMA (Wilder's smoothing): first value is SMA, then exponential
    rma = sum(trs[:period]) / period
    alpha = 1.0 / period
    for i in range(period, len(trs)):
        rma = alpha * trs[i] + (1 - alpha) * rma

    return rma
