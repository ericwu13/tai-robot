"""H4 Bollinger Bands Long-Only Strategy (H4布林型態多單策略).

從 TradingView Pine Script 翻譯。週期：H4（240分鐘），全盤。
方向：純做多，1口，不加碼。

進場型態：
  1. 突破：收盤 > 中軌，陽線，實體 >= K棒振幅的66%
  2. 回踩：最低價刺穿中軌，收盤站回中軌上方，小實體，下影線 >= 50%

停損：進場K棒最低價 - sl_offset 點（固定值）
停利：上軌 - tp_offset 點（動態，每根K棒更新）
"""

from __future__ import annotations

from ...market_data.models import Bar
from ...market_data.data_store import DataStore
from ...strategy.indicators.bollinger import bollinger_bands
from ...backtest.strategy import BacktestStrategy
from ...backtest.broker import BrokerContext, OrderSide


class H4BollingerLongStrategy(BacktestStrategy):
    """H4布林型態多單策略 — 突破/回踩中軌做多，固定點數停損停利。"""

    kline_type = 0      # 分鐘K線
    kline_minute = 240   # 4小時K

    def __init__(
        self,
        bb_period: int = 20,   # 布林帶週期
        bb_std: float = 2.0,   # 布林帶標準差倍數
        sl_offset: int = 20,   # 停損點數（固定）
        tp_offset: int = 50,   # 停利偏移點數
    ):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.sl_offset = sl_offset
        self.tp_offset = tp_offset
        self._sl_price: int = 0  # 當前停損價

    def required_bars(self) -> int:
        return self.bb_period

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        closes = data_store.get_closes()
        result = bollinger_bands(closes, self.bb_period, self.bb_std)
        if result is None:
            return

        upper, basis, lower = result  # 上軌、中軌、下軌

        body = abs(bar.close - bar.open)          # 實體長度
        k_range = max(bar.high - bar.low, 1)      # K棒振幅
        lower_shadow = min(bar.close, bar.open) - bar.low  # 下影線長度

        # 進場型態1 — 突破：收盤站上中軌，陽線，大實體
        breakout = (
            bar.close > basis
            and bar.close > bar.open
            and body >= k_range * 0.66
        )

        # 進場型態2 — 回踩：刺穿中軌後收回，小實體，長下影線
        pullback = (
            bar.low < basis
            and bar.close >= basis
            and body <= k_range * 0.34
            and lower_shadow >= k_range * 0.5
        )

        if (breakout or pullback) and broker.position_size == 0:
            broker.entry("Long", OrderSide.LONG)
            self._sl_price = bar.low - self.sl_offset  # 停損設在進場K棒低點下方

        # 每根K棒更新停利單（上軌 - 偏移）
        if broker.position_size > 0 or (breakout or pullback):
            tp_price = round(upper - self.tp_offset)
            broker.exit("Exit Long", "Long", limit=tp_price, stop=self._sl_price)
