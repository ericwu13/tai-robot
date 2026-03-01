"""Daily Bollinger Bands Long-Only Strategy (日線布林型態多單策略).

Same logic as H4 version but on daily (1D) bars.
"""

from __future__ import annotations

from .h4_bollinger_long import H4BollingerLongStrategy


class DailyBollingerLongStrategy(H4BollingerLongStrategy):

    kline_type = 4
    kline_minute = 1
