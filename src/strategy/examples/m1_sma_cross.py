"""1-Min SMA Crossover Strategy (1分K均線交叉策略).

簡單測試策略，用於1分K頻繁交易。
當快線 SMA(3) 向上穿越慢線 SMA(8) 時做多。
當快線 SMA(3) 向下穿越慢線 SMA(8) 時平倉。

Simple test strategy that trades frequently on 1-min bars.
Enter long when SMA(3) crosses above SMA(8).
Exit via close when SMA(3) crosses below SMA(8).
"""

from __future__ import annotations

from ...market_data.models import Bar
from ...market_data.data_store import DataStore
from ...strategy.indicators.ma import sma
from ...backtest.strategy import BacktestStrategy
from ...backtest.broker import BrokerContext, OrderSide


class M1SmaCrossStrategy(BacktestStrategy):
    """1分K均線交叉策略 — 快慢均線黃金/死亡交叉做多放空。"""

    kline_type = 0   # 分鐘K線
    kline_minute = 1  # 1分K

    def __init__(self, fast: int = 3, slow: int = 8):
        self.fast = fast    # 快線週期
        self.slow = slow    # 慢線週期

    def required_bars(self) -> int:
        return self.slow + 1

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        closes = [b.close for b in data_store.get_bars()]

        # 計算當前與前一根K棒的快慢均線值
        fast_now = sma(closes, self.fast)
        fast_prev = sma(closes[:-1], self.fast)
        slow_now = sma(closes, self.slow)
        slow_prev = sma(closes[:-1], self.slow)

        if None in (fast_now, fast_prev, slow_now, slow_prev):
            return

        # 黃金交叉（快線上穿慢線）→ 做多進場
        if fast_prev <= slow_prev and fast_now > slow_now and broker.position_size == 0:
            broker.entry("Long", OrderSide.LONG)

        # 死亡交叉（快線下穿慢線）→ 平倉出場
        if fast_prev >= slow_prev and fast_now < slow_now and broker.position_size > 0:
            broker.close("Long", "Exit")
