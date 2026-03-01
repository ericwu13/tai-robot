"""Bollinger Band breakout strategy.

Buy when price breaks above upper band, sell when it breaks below lower band.
"""

from __future__ import annotations

from ...market_data.models import Bar, Direction, Signal
from ...market_data.data_store import DataStore
from ..base import AbstractStrategy
from ..indicators.bollinger import bollinger_bands
from ..registry import register_strategy


class BollingerBreakoutStrategy(AbstractStrategy):

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.period = self.params.get("period", 20)
        self.num_std = self.params.get("num_std", 2.0)
        self._prev_close: int | None = None

    def required_bars(self) -> int:
        return self.period + 1

    def on_bar(self, bar: Bar, data_store: DataStore) -> Signal | None:
        closes = data_store.get_closes()
        if len(closes) < self.period:
            return None

        bands = bollinger_bands(closes, self.period, self.num_std)
        if bands is None:
            return None

        upper, middle, lower = bands
        signal = None

        if self._prev_close is not None:
            # Breakout above upper band
            if self._prev_close <= upper < bar.close:
                signal = Signal(
                    direction=Direction.BUY,
                    strength=min((bar.close - upper) / (upper - middle), 1.0) if upper != middle else 1.0,
                    price=bar.close,
                    reason=f"Price broke above upper BB ({upper:.0f})",
                )
            # Breakout below lower band
            elif self._prev_close >= lower > bar.close:
                signal = Signal(
                    direction=Direction.SELL,
                    strength=min((lower - bar.close) / (middle - lower), 1.0) if middle != lower else 1.0,
                    price=bar.close,
                    reason=f"Price broke below lower BB ({lower:.0f})",
                )

        self._prev_close = bar.close
        return signal


register_strategy("bollinger_breakout", BollingerBreakoutStrategy)
