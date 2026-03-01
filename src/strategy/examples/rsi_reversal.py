"""RSI reversal strategy.

Buy when RSI drops below oversold threshold, sell when it rises above overbought.
"""

from __future__ import annotations

from ...market_data.models import Bar, Direction, Signal
from ...market_data.data_store import DataStore
from ..base import AbstractStrategy
from ..indicators.rsi import rsi
from ..registry import register_strategy


class RSIReversalStrategy(AbstractStrategy):

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.period = self.params.get("period", 14)
        self.oversold = self.params.get("oversold", 30)
        self.overbought = self.params.get("overbought", 70)
        self._prev_rsi: float | None = None

    def required_bars(self) -> int:
        return self.period + 2

    def on_bar(self, bar: Bar, data_store: DataStore) -> Signal | None:
        closes = data_store.get_closes()
        if len(closes) < self.period + 1:
            return None

        current_rsi = rsi(closes, self.period)
        if current_rsi is None:
            return None

        signal = None

        if self._prev_rsi is not None:
            # Buy: RSI crosses above oversold from below
            if self._prev_rsi < self.oversold <= current_rsi:
                signal = Signal(
                    direction=Direction.BUY,
                    strength=1.0 - (current_rsi / 100.0),
                    price=bar.close,
                    reason=f"RSI crossed above {self.oversold} (was {self._prev_rsi:.1f}, now {current_rsi:.1f})",
                )
            # Sell: RSI crosses below overbought from above
            elif self._prev_rsi > self.overbought >= current_rsi:
                signal = Signal(
                    direction=Direction.SELL,
                    strength=current_rsi / 100.0,
                    price=bar.close,
                    reason=f"RSI crossed below {self.overbought} (was {self._prev_rsi:.1f}, now {current_rsi:.1f})",
                )

        self._prev_rsi = current_rsi
        return signal


register_strategy("rsi_reversal", RSIReversalStrategy)
