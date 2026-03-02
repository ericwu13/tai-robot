"""H4 中線戰法多單 (絕對觸碰濾網版) — Midline Touch Long Strategy.

從 TradingView Pine Script 翻譯。週期：H4（240分鐘），全盤。
方向：純做多，1口，不加碼。

與 H4BollingerLong 的關鍵差異：加入**觸碰濾網**（最低價 <= 中軌），
確保只在中軌附近進場（避免追高），並擴展至4種進場條件。

觸碰濾網：最低價 <= 中軌（K棒必須觸碰或刺穿中軌）

進場型態：
  1. 實體在中軌上方：開盤 > 中軌 且 收盤 > 中軌
  2. 刺穿中軌：最低價 < 中軌，收盤 >= 中軌，有下影線
  3. 強勢突破：收盤 > 中軌，實體 >= 振幅的 2/3
  4. 回踩測試：實體 <= 振幅的 1/3，下影線 >= 1/2，收盤 >= 中軌

停損：進場K棒最低價 - sl_offset 點（固定值）
停利：上軌 - tp_offset 點（動態，每根K棒更新）
"""

from __future__ import annotations

from ...market_data.models import Bar
from ...market_data.data_store import DataStore
from ...strategy.indicators.bollinger import bollinger_bands
from ...backtest.strategy import BacktestStrategy
from ...backtest.broker import BrokerContext, OrderSide


class H4MidlineTouchLongStrategy(BacktestStrategy):
    """H4中線戰法多單 — 觸碰中軌濾網 + 4種型態進場。"""

    kline_type = 0      # 分鐘K線
    kline_minute = 240   # 4小時K

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

        # 觸碰濾網：K棒必須觸碰或刺穿中軌（避免追高）
        touch_filter = bar.low <= basis

        # 條件1：實體完全在中軌上方
        cond1 = bar.open > basis and bar.close > basis

        # 條件2：刺穿中軌後收回，有下影線
        cond2 = bar.low < basis and bar.close >= basis and lower_shadow > 0

        # 條件3：強勢突破，大實體（>= 振幅 2/3）
        cond3 = bar.close > basis and body >= k_range * (2.0 / 3.0)

        # 條件4：回踩測試，小實體 + 長下影線
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
