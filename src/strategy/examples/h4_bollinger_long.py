"""H4 Bollinger Bands Long-Only Strategy (布林型態多單策略).

Translated from TradingView Pine Script. Timeframe: H4 (240-min), full session.
Direction: Long only, 1 contract, no pyramiding.

Entry patterns:
  1. Breakout: close > middle band, bullish candle, body >= 66% of bar range
  2. Pullback: low pierces middle band, closes above it, small body, long lower shadow >= 50%

Stop loss: entry bar's low minus sl_offset points (static)
Take profit: upper band minus tp_offset points (dynamic, updates each bar)
"""

from __future__ import annotations

from ...market_data.models import Bar
from ...market_data.data_store import DataStore
from ...strategy.indicators.bollinger import bollinger_bands
from ...backtest.strategy import BacktestStrategy
from ...backtest.broker import BrokerContext, OrderSide


class H4BollingerLongStrategy(BacktestStrategy):

    kline_type = 0
    kline_minute = 240

    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        sl_offset: int = 20,
        tp_offset: int = 50,
    ):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.sl_offset = sl_offset
        self.tp_offset = tp_offset
        self._sl_price: int = 0

    def required_bars(self) -> int:
        return self.bb_period

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        closes = data_store.get_closes()
        result = bollinger_bands(closes, self.bb_period, self.bb_std)
        if result is None:
            return

        upper, basis, lower = result

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

        if (breakout or pullback) and broker.position_size == 0:
            broker.entry("Long", OrderSide.LONG)
            self._sl_price = bar.low - self.sl_offset

        # TradingView Pine Script calls strategy.exit() every bar (unconditionally
        # after the entry block). The TP uses float upper - offset, rounded to tick.
        if broker.position_size > 0 or (breakout or pullback):
            tp_price = round(upper - self.tp_offset)
            broker.exit("Exit Long", "Long", limit=tp_price, stop=self._sl_price)
