"""H4 Bollinger Bands ATR Long-Only Strategy (布林ATR型態多單策略).

Translated from TradingView Pine Script. Timeframe: H4 (240-min), full session.
Direction: Long only, 1 contract, no pyramiding.

Improvement over h4_bollinger_long: uses ATR-based SL/TP instead of fixed
point offsets, so the strategy adapts to changing volatility.
Includes cooldown period after exit to prevent immediate re-entry.

Entry patterns (same as original):
  1. Breakout: close > middle band, bullish candle, body >= 66% of bar range
  2. Pullback: low pierces middle band, closes above it, small body, long lower shadow >= 50%

Stop loss: entry bar's low minus ATR * sl_mult (adaptive)
Take profit: upper band minus ATR * tp_mult (adaptive, dynamic)
Cooldown: no re-entry for cooldown_bars bars after an exit
"""

from __future__ import annotations

from ...market_data.models import Bar
from ...market_data.data_store import DataStore
from ...strategy.indicators.bollinger import bollinger_bands
from ...strategy.indicators.atr import atr
from ...backtest.strategy import BacktestStrategy
from ...backtest.broker import BrokerContext, OrderSide


class H4BollingerAtrLongStrategy(BacktestStrategy):

    kline_type = 0
    kline_minute = 240

    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        atr_period: int = 14,
        sl_mult: float = 1.0,
        tp_mult: float = 0.5,
        cooldown_bars: int = 6,
    ):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.atr_period = atr_period
        self.sl_mult = sl_mult
        self.tp_mult = tp_mult
        self.cooldown_bars = cooldown_bars
        self._sl_price: int = 0
        self._was_in_position: bool = False
        self._bars_since_exit: int = 999

    def required_bars(self) -> int:
        return max(self.bb_period, self.atr_period + 1)

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        bars = data_store.get_bars()
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]

        bb = bollinger_bands(closes, self.bb_period, self.bb_std)
        if bb is None:
            return
        upper, basis, lower = bb

        atr_val = atr(highs, lows, closes, self.atr_period)
        if atr_val is None:
            return

        # Track cooldown: detect when position was just closed
        if self._was_in_position and broker.position_size == 0:
            self._bars_since_exit = 0
        elif broker.position_size == 0:
            self._bars_since_exit += 1
        self._was_in_position = broker.position_size > 0

        body = abs(bar.close - bar.open)
        k_range = max(bar.high - bar.low, 1)
        lower_shadow = min(bar.close, bar.open) - bar.low

        # Entry pattern 1 - Breakout
        breakout = (
            bar.close > basis
            and bar.close > bar.open
            and body >= k_range * 0.66
        )

        # Entry pattern 2 - Pullback
        pullback = (
            bar.low < basis
            and bar.close >= basis
            and body <= k_range * 0.34
            and lower_shadow >= k_range * 0.5
        )

        can_enter = (
            (breakout or pullback)
            and broker.position_size == 0
            and self._bars_since_exit >= self.cooldown_bars
        )

        if can_enter:
            broker.entry("Long", OrderSide.LONG)
            self._sl_price = round(bar.low - atr_val * self.sl_mult)

        if broker.position_size > 0 or can_enter:
            tp_price = round(upper - atr_val * self.tp_mult)
            broker.exit("Exit Long", "Long", limit=tp_price, stop=self._sl_price)
