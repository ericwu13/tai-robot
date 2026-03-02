"""1-Min Bollinger Bands ATR Long-Only Strategy (1分K布林ATR多單策略).

從 H4 布林ATR多單改編。週期：1分鐘，全盤。
方向：純做多，1口，不加碼。

進場型態（與H4版本相同邏輯）：
  1. 突破：收盤 > 中軌，陽線，實體 >= K棒振幅的66%
  2. 回踩：最低價刺穿中軌，收盤站回中軌上方，小實體，下影線 >= 50%

停損：進場K棒最低價 - ATR × sl_mult（自適應）
停利：上軌 - ATR × tp_mult（自適應，動態更新）
冷卻：出場後 cooldown_bars 根K棒內不再進場
"""

from __future__ import annotations

from ...market_data.models import Bar
from ...market_data.data_store import DataStore
from ...strategy.indicators.bollinger import bollinger_bands
from ...strategy.indicators.atr import atr
from ...backtest.strategy import BacktestStrategy
from ...backtest.broker import BrokerContext, OrderSide


class M1BollingerAtrLongStrategy(BacktestStrategy):
    """1分K布林ATR多單策略 — ATR自適應停損停利 + 冷卻期。"""

    kline_type = 0   # 分鐘K線
    kline_minute = 1  # 1分K

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
