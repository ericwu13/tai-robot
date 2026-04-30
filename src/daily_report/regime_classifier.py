"""Market regime classifier for Taiwan futures (TAIEX).

Classifies the current market state using ADX, ATR, and EMA indicators.
All raw indicator values are included in the result for downstream AI analysis.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.strategy.indicators import adx, plus_di, minus_di, atr, ema


@dataclass
class RegimeResult:
    """Market regime classification with raw indicator values."""
    label: str              # e.g. "trending-up", "range-bound", "high-volatility"
    trend_strength: str     # "trending", "range-bound", "transitional"
    volatility: str         # "high", "normal", "low"
    direction: str          # "bullish", "bearish"

    # Raw indicator values for AI diagnosis
    adx_value: float
    plus_di_value: float
    minus_di_value: float
    atr_value: float
    atr_ratio: float        # current ATR / 20-period average ATR
    ema_50: float
    last_close: float

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "trend_strength": self.trend_strength,
            "volatility": self.volatility,
            "direction": self.direction,
            "adx": round(self.adx_value, 2),
            "plus_di": round(self.plus_di_value, 2),
            "minus_di": round(self.minus_di_value, 2),
            "atr": round(self.atr_value, 2),
            "atr_ratio": round(self.atr_ratio, 4),
            "ema_50": round(self.ema_50, 2),
            "last_close": self.last_close,
        }


def _classify_trend(adx_val: float) -> str:
    if adx_val > 25:
        return "trending"
    if adx_val < 20:
        return "range-bound"
    return "transitional"


def _classify_volatility(atr_ratio: float) -> str:
    if atr_ratio > 1.3:
        return "high"
    if atr_ratio < 0.7:
        return "low"
    return "normal"


def _classify_direction(last_close: float, ema_val: float) -> str:
    return "bullish" if last_close >= ema_val else "bearish"


def _combine_label(trend: str, volatility: str, direction: str) -> str:
    """Combine sub-classifications into a single regime label."""
    if volatility == "high":
        return "high-volatility"
    if trend == "range-bound":
        if volatility == "low":
            return "low-volatility-chop"
        return "range-bound"
    if trend == "transitional":
        # Lean toward direction but mark as transitional
        return f"transitional-{direction}"
    # trending
    if direction == "bullish":
        return "trending-up"
    return "trending-down"


def classify_regime(
    highs: list[int],
    lows: list[int],
    closes: list[int],
    adx_period: int = 14,
    atr_period: int = 14,
    ema_period: int = 50,
    atr_avg_period: int = 20,
) -> RegimeResult | None:
    """Classify the current market regime from OHLC data.

    Requires enough bars for the longest lookback (at least 2*adx_period+1
    and ema_period bars). Returns None if insufficient data.

    Parameters
    ----------
    highs, lows, closes : bar data as integer lists
    adx_period : ADX calculation period (default 14)
    atr_period : ATR calculation period (default 14)
    ema_period : EMA lookback for direction (default 50)
    atr_avg_period : number of trailing ATR values to average for ratio (default 20)
    """
    adx_val = adx(highs, lows, closes, adx_period)
    if adx_val is None:
        return None

    pdi = plus_di(highs, lows, closes, adx_period)
    mdi = minus_di(highs, lows, closes, adx_period)
    if pdi is None or mdi is None:
        return None

    current_atr = atr(highs, lows, closes, atr_period)
    if current_atr is None:
        return None

    ema_val = ema([float(c) for c in closes], ema_period)
    if ema_val is None:
        return None

    # Compute ATR ratio: current vs average of trailing ATR values
    # We calculate ATR on progressively shorter slices to get a trailing series
    min_atr_len = atr_period + 1 + atr_avg_period
    if len(highs) >= min_atr_len:
        trailing_atrs = []
        for offset in range(atr_avg_period):
            end = len(highs) - offset
            val = atr(highs[:end], lows[:end], closes[:end], atr_period)
            if val is not None:
                trailing_atrs.append(val)
        avg_atr = sum(trailing_atrs) / len(trailing_atrs) if trailing_atrs else current_atr
    else:
        avg_atr = current_atr

    atr_ratio = current_atr / avg_atr if avg_atr > 0 else 1.0
    last_close = float(closes[-1])

    trend = _classify_trend(adx_val)
    vol = _classify_volatility(atr_ratio)
    direction = _classify_direction(last_close, ema_val)
    label = _combine_label(trend, vol, direction)

    return RegimeResult(
        label=label,
        trend_strength=trend,
        volatility=vol,
        direction=direction,
        adx_value=adx_val,
        plus_di_value=pdi,
        minus_di_value=mdi,
        atr_value=current_atr,
        atr_ratio=atr_ratio,
        ema_50=ema_val,
        last_close=last_close,
    )
