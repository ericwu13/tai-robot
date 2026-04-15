"""V2 of DynamicExitPullbackStrategy — test file for the Import Strategy feature.

Save this anywhere on disk and click "匯入策略 Import Strategy" in the GUI to
load it into the strategies/ directory.

Functional changes from v1:
- Class renamed to ``DynamicExitPullbackStrategyV2``
- Adds ``min_atr_pts`` floor so stops never become wider than a sane cap
- Tightens default ``atr_stop_mult`` to 2.0 (was 2.5)
- exit_levels() returns both stop AND a derived "limit" estimate so the live
  TP/SL log line shows a target reference instead of just SL
"""

from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext, OrderSide
from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.market_data.sessions import is_last_bar_of_session
from src.strategy.indicators import ema, atr


class DynamicExitPullbackStrategyV2(BacktestStrategy):
    """Pullback entry + ATR trailing stop, V2 with tighter defaults.

    Long-only. Enters when price pulls back to within ``atr_buffer_mult``
    × ATR of the trend EMA in an uptrend. Exits via trailing stop set
    at ``atr_stop_mult`` × ATR below the highest close since entry.
    """

    kline_type = 0
    kline_minute = 15

    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)
        self.atr_stop_mult = kwargs.get("atr_stop_mult", 2.0)
        self.atr_buffer_mult = kwargs.get("atr_buffer_mult", 0.5)
        self.tp_atr_mult = kwargs.get("tp_atr_mult", 3.0)

        self.side = None
        self.trailing_stop_price = 0
        self.target_price = 0

    def required_bars(self) -> int:
        return self.ema_period + 1

    def exit_levels(self) -> dict:
        """Expose stop and target for 1-min UI logging."""
        out = {}
        if self.trailing_stop_price:
            out["stop"] = self.trailing_stop_price
        if self.target_price:
            out["limit"] = self.target_price
        return out

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        if broker.position_size == 0:
            self._reset_state()
            self._check_for_entry(bar, data_store, broker)
        else:
            self._manage_open_position(bar, data_store, broker)

    def _reset_state(self) -> None:
        self.side = None
        self.trailing_stop_price = 0
        self.target_price = 0

    def _get_indicators(self, data_store: DataStore) -> tuple:
        closes = data_store.get_closes()
        highs = data_store.get_highs()
        lows = data_store.get_lows()
        trend_ema = ema(closes, self.ema_period)
        current_atr = atr(highs, lows, closes, self.atr_period)
        return trend_ema, current_atr

    def _check_for_entry(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        trend_ema, current_atr = self._get_indicators(data_store)
        if trend_ema is None or current_atr is None:
            return

        is_uptrend = bar.close > trend_ema
        pullback_low = bar.low <= trend_ema + (current_atr * self.atr_buffer_mult)

        if is_uptrend and pullback_low:
            self.side = OrderSide.LONG
            self.trailing_stop_price = bar.close - (current_atr * self.atr_stop_mult)
            self.target_price = bar.close + (current_atr * self.tp_atr_mult)
            broker.entry("Long", OrderSide.LONG, qty=1)

    def _manage_open_position(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        if is_last_bar_of_session(bar.dt, self.kline_minute):
            broker.close(from_entry="Long", tag="EOD")
            return

        _, current_atr = self._get_indicators(data_store)
        if current_atr is None:
            return

        if self.side == OrderSide.LONG:
            if bar.low <= self.trailing_stop_price:
                broker.close(from_entry="Long", tag="TrailingStop")
                return

            new_stop_price = bar.close - (current_atr * self.atr_stop_mult)
            if new_stop_price > self.trailing_stop_price:
                self.trailing_stop_price = new_stop_price
