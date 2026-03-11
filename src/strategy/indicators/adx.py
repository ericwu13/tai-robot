"""Average Directional Index (ADX) indicator."""

from __future__ import annotations

from .atr import true_range


def _smooth_series(values: list[float], period: int) -> list[float]:
    """Apply RMA (Wilder's smoothing) and return all smoothed values.

    First value is SMA of the first *period* elements, then exponential.
    """
    rma = sum(values[:period]) / period
    result = [rma]
    alpha = 1.0 / period
    for i in range(period, len(values)):
        rma = alpha * values[i] + (1 - alpha) * rma
        result.append(rma)
    return result


def _compute_di(
    highs: list[int], lows: list[int], closes: list[int], period: int,
) -> tuple[list[float], list[float]] | None:
    """Compute +DI and -DI series.

    Returns (plus_di_series, minus_di_series) or None if insufficient data.
    Each series has length = len(trs) - period + 1.
    """
    n = len(highs)
    if n < 2 * period + 1:
        return None

    # Calculate +DM, -DM, and TR from index 1 onward
    plus_dms: list[float] = []
    minus_dms: list[float] = []
    trs: list[float] = []

    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]

        plus_dm = float(up) if up > down and up > 0 else 0.0
        minus_dm = float(down) if down > up and down > 0 else 0.0

        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)
        trs.append(float(true_range(highs[i], lows[i], closes[i - 1])))

    if len(trs) < period:
        return None

    # Smooth each series using RMA
    smoothed_plus_dm = _smooth_series(plus_dms, period)
    smoothed_minus_dm = _smooth_series(minus_dms, period)
    smoothed_tr = _smooth_series(trs, period)

    # Calculate +DI and -DI
    plus_di_series: list[float] = []
    minus_di_series: list[float] = []
    for i in range(len(smoothed_tr)):
        tr_val = smoothed_tr[i]
        if tr_val == 0:
            plus_di_series.append(0.0)
            minus_di_series.append(0.0)
        else:
            plus_di_series.append(100.0 * smoothed_plus_dm[i] / tr_val)
            minus_di_series.append(100.0 * smoothed_minus_dm[i] / tr_val)

    return plus_di_series, minus_di_series


def adx(
    highs: list[int], lows: list[int], closes: list[int], period: int = 14,
) -> float | None:
    """Calculate ADX using RMA (Wilder's smoothing), matching TradingView's ta.adx().

    Requires at least 2 * period + 1 data points.
    Returns the current ADX value (0-100), or None if insufficient data.
    """
    result = _compute_di(highs, lows, closes, period)
    if result is None:
        return None

    plus_di_series, minus_di_series = result

    # Calculate DX series
    dx_series: list[float] = []
    for i in range(len(plus_di_series)):
        di_sum = plus_di_series[i] + minus_di_series[i]
        if di_sum == 0:
            dx_series.append(0.0)
        else:
            dx_series.append(100.0 * abs(plus_di_series[i] - minus_di_series[i]) / di_sum)

    if len(dx_series) < period:
        return None

    # Smooth DX with RMA to get ADX
    smoothed_dx = _smooth_series(dx_series, period)
    return smoothed_dx[-1]


def plus_di(
    highs: list[int], lows: list[int], closes: list[int], period: int = 14,
) -> float | None:
    """Calculate +DI (Plus Directional Indicator).

    Returns the current +DI value (0-100), or None if insufficient data.
    """
    result = _compute_di(highs, lows, closes, period)
    if result is None:
        return None
    return result[0][-1]


def minus_di(
    highs: list[int], lows: list[int], closes: list[int], period: int = 14,
) -> float | None:
    """Calculate -DI (Minus Directional Indicator).

    Returns the current -DI value (0-100), or None if insufficient data.
    """
    result = _compute_di(highs, lows, closes, period)
    if result is None:
        return None
    return result[1][-1]
