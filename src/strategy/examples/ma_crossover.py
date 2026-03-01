"""EMA crossover strategy.

Buy when fast EMA crosses above slow EMA, sell when it crosses below.
"""

from __future__ import annotations

from ...market_data.models import Bar, Direction, Signal
from ...market_data.data_store import DataStore
from ..base import AbstractStrategy
from ..indicators.ma import ema
from ..registry import register_strategy


class MACrossoverStrategy(AbstractStrategy):

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.fast_period = self.params.get("fast_period", 5)
        self.slow_period = self.params.get("slow_period", 20)
        self._prev_fast: float | None = None
        self._prev_slow: float | None = None

    def required_bars(self) -> int:
        return self.slow_period + 1

    def on_bar(self, bar: Bar, data_store: DataStore) -> Signal | None:
        closes = data_store.get_closes()
        if len(closes) < self.required_bars():
            return None

        fast_val = ema(closes, self.fast_period)
        slow_val = ema(closes, self.slow_period)
        if fast_val is None or slow_val is None:
            return None

        signal = None

        if self._prev_fast is not None and self._prev_slow is not None:
            # Crossover detection
            prev_diff = self._prev_fast - self._prev_slow
            curr_diff = fast_val - slow_val

            if prev_diff <= 0 < curr_diff:
                signal = Signal(
                    direction=Direction.BUY,
                    strength=min(abs(curr_diff) / slow_val * 100, 1.0) if slow_val else 1.0,
                    price=bar.close,
                    reason=f"EMA{self.fast_period} crossed above EMA{self.slow_period}",
                )
            elif prev_diff >= 0 > curr_diff:
                signal = Signal(
                    direction=Direction.SELL,
                    strength=min(abs(curr_diff) / slow_val * 100, 1.0) if slow_val else 1.0,
                    price=bar.close,
                    reason=f"EMA{self.fast_period} crossed below EMA{self.slow_period}",
                )

        self._prev_fast = fast_val
        self._prev_slow = slow_val
        return signal


register_strategy("ma_crossover", MACrossoverStrategy)
