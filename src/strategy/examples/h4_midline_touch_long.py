"""H4 中線戰法多單 (絕對觸碰濾網版) — Midline Touch Long Strategy.

Translated from TradingView Pine Script. Timeframe: H4 (240-min), full session.
Direction: Long only, 1 contract, no pyramiding.

Key difference from H4BollingerLongStrategy: adds a **touch filter** (low <= basis)
to ensure entry only near the middle band (no chasing highs), and expands to 4 entry
conditions.

Touch filter: low <= basis (bar must touch or pierce the middle band)

Entry patterns:
  1. Body above middle: open > basis AND close > basis
  2. Pierce middle: low < basis, close >= basis, has lower shadow
  3. Strong breakout: close > basis, body >= 2/3 of range
  4. Pullback test: body <= 1/3 of range, lower shadow >= 1/2, close >= basis

Stop loss: entry bar's low minus sl_offset points (static)
Take profit: upper band minus tp_offset points (dynamic, updates each bar)
"""

from __future__ import annotations

from ...market_data.models import Bar
from ...market_data.data_store import DataStore
from ...strategy.indicators.bollinger import bollinger_bands
from ...backtest.strategy import BacktestStrategy
from ...backtest.broker import BrokerContext, OrderSide


class H4MidlineTouchLongStrategy(BacktestStrategy):

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

        # Touch filter: bar must touch or pierce the middle band (no chasing highs)
        touch_filter = bar.low <= basis

        # Condition 1: body fully above middle band (open > basis AND close > basis)
        cond1 = bar.open > basis and bar.close > basis

        # Condition 2: pierces middle, close >= middle, has lower shadow
        cond2 = bar.low < basis and bar.close >= basis and lower_shadow > 0

        # Condition 3: close > middle, strong breakout (body >= 2/3 of range)
        cond3 = bar.close > basis and body >= k_range * (2.0 / 3.0)

        # Condition 4: small body <= 1/3, long lower shadow >= 1/2, close >= middle
        cond4 = (
            bar.close >= basis
            and body <= k_range * (1.0 / 3.0)
            and lower_shadow >= k_range * 0.5
        )

        long_cond = touch_filter and (cond1 or cond2 or cond3 or cond4)

        if long_cond and broker.position_size == 0:
            broker.entry("Long", OrderSide.LONG)
            self._sl_price = bar.low - self.sl_offset

        if broker.position_size > 0 or (long_cond and broker.position_size == 0):
            tp_price = round(upper - self.tp_offset)
            broker.exit("Exit Long", "Long", limit=tp_price, stop=self._sl_price)
