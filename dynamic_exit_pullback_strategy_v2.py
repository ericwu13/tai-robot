"""V2 of DynamicExitPullbackStrategy — test file for the Import Strategy feature.

Save this anywhere on disk and click "匯入策略 Import Strategy" in the GUI to
load it into the strategies/ directory.

Functional difference from v1: **none**. V2 is the same trailing-stop-only
strategy as v1 with a different class name. Exposed as V2 purely so the
Import Strategy flow has a distinct entry in index.json.
"""

import math
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext, OrderSide
from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.market_data.sessions import is_last_bar_of_session
from src.strategy.indicators import ema, atr


class DynamicExitPullbackStrategyV2(BacktestStrategy):
    """
    Pullback entry on an EMA trend with ATR trailing stop.

    This is V2 of DynamicExitPullbackStrategy with identical behavior —
    same default parameters, same entry/exit logic. Exit is trailing-stop
    only; there is no take-profit.
    """
    kline_type = 0
    kline_minute = 15

    def __init__(self, **kwargs):
        """Initializes strategy parameters and state."""
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)
        self.atr_stop_mult = kwargs.get("atr_stop_mult", 2.5)
        self.atr_buffer_mult = kwargs.get("atr_buffer_mult", 0.5)

        # State tracking for position direction and stop price
        self.side = None
        self.trailing_stop_price = 0

    def required_bars(self) -> int:
        """Minimum number of bars required for indicators."""
        return self.ema_period + 1

    def exit_levels(self) -> dict:
        """Expose trailing stop for 1m bar logging."""
        if self.trailing_stop_price:
            return {"stop": self.trailing_stop_price}
        return {}

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        """Main strategy logic executed on each bar."""
        if broker.position_size == 0:
            self._reset_state()
            self._check_for_entry(bar, data_store, broker)
        else:
            self._manage_open_position(bar, data_store, broker)

    def _reset_state(self) -> None:
        """Resets position-specific state when flat."""
        self.side = None
        self.trailing_stop_price = 0

    def _get_indicators(self, data_store: DataStore) -> tuple:
        """Helper to calculate and return all required indicators."""
        closes = data_store.get_closes()
        highs = data_store.get_highs()
        lows = data_store.get_lows()

        trend_ema = ema(closes, self.ema_period)
        current_atr = atr(highs, lows, closes, self.atr_period)

        return trend_ema, current_atr

    def _check_for_entry(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        """Checks for and executes a long entry signal."""
        trend_ema, current_atr = self._get_indicators(data_store)
        if trend_ema is None or current_atr is None:
            return

        is_uptrend = bar.close > trend_ema
        pullback_low = bar.low <= trend_ema + (current_atr * self.atr_buffer_mult)

        # Long entry: price pulls back towards EMA during an uptrend
        if is_uptrend and pullback_low:
            self.side = OrderSide.LONG
            self.trailing_stop_price = bar.close - (current_atr * self.atr_stop_mult)
            broker.entry("Long", OrderSide.LONG, qty=1)

    def _manage_open_position(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        """Manages the trailing stop and session-end exit for an open position."""
        if is_last_bar_of_session(bar.dt, self.kline_minute):
            broker.close(from_entry="Long", tag="EOD")
            return

        _, current_atr = self._get_indicators(data_store)
        if current_atr is None:
            return

        if self.side == OrderSide.LONG:
            # 1. Check if the current bar hits the trailing stop
            if bar.low <= self.trailing_stop_price:
                broker.close(from_entry="Long", tag="TrailingStop")
                return

            # 2. Update the trailing stop if price moves favorably
            new_stop_price = bar.close - (current_atr * self.atr_stop_mult)
            if new_stop_price > self.trailing_stop_price:
                self.trailing_stop_price = new_stop_price
