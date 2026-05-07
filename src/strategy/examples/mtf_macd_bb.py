"""MTF MACD+BB: 30m MACD entries gated by 60m Bollinger Bands regime filter.

Demonstrates the MTF framework: a 30-minute primary strategy that consults
60-minute HTF Bollinger Bands as a directional filter.

Primary (30m): MACD bullish crossover triggers an entry signal.
HTF (60m):     Only take the entry when bar.close is above the 60m BB midline.
Exit:          ATR-based fixed TP / SL queued alongside the entry.

The HTF data is updated by the engine BEFORE on_bar() runs, and only
COMPLETED HTF bars are exposed — at primary-bar 09:30 the strategy sees the
[08:00–09:00) HTF bar, never the in-progress [09:00–10:00) one.
"""

from __future__ import annotations

from ...market_data.models import Bar
from ...market_data.data_store import DataStore
from ...strategy.indicators import macd, bollinger_bands, atr
from ...backtest.strategy import BacktestStrategy
from ...backtest.broker import BrokerContext, OrderSide


class MtfMacdBbStrategy(BacktestStrategy):
    """30m MACD entry filtered by 60m Bollinger Bands midline."""

    kline_type = 0
    kline_minute = 30
    htf_intervals = [3600]   # 60-min HTF

    def __init__(
        self,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        bb_period: int = 20,
        bb_std: float = 2.0,
        atr_period: int = 14,
        sl_mult: float = 1.5,
        tp_mult: float = 2.0,
    ):
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.atr_period = atr_period
        self.sl_mult = sl_mult
        self.tp_mult = tp_mult
        self._prev_hist: float | None = None

    def required_bars(self) -> int:
        return self.macd_slow + self.macd_signal

    def htf_required_bars(self) -> dict[int, int]:
        return {3600: self.bb_period}

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        # Primary (30m) MACD
        closes = data_store.get_closes(self.macd_slow + self.macd_signal)
        macd_result = macd(closes, self.macd_fast, self.macd_slow, self.macd_signal)
        if macd_result is None:
            return

        # HTF (60m) Bollinger Bands — only completed HTF bars
        htf_closes = data_store.htf_closes(3600, self.bb_period)
        bb = bollinger_bands(htf_closes, self.bb_period, self.bb_std)
        if bb is None:
            return

        highs = data_store.get_highs(self.atr_period + 1)
        lows = data_store.get_lows(self.atr_period + 1)
        p_closes = data_store.get_closes(self.atr_period + 1)
        atr_val = atr(highs, lows, p_closes, self.atr_period)
        if atr_val is None:
            return

        hist = macd_result.histogram

        if broker.position_size == 0 and self._prev_hist is not None:
            bullish_cross = self._prev_hist < 0 and hist >= 0
            above_midline = bar.close > bb.middle
            if bullish_cross and above_midline:
                sl = int(atr_val * self.sl_mult)
                tp = int(atr_val * self.tp_mult)
                broker.entry("MtfLong", OrderSide.LONG)
                broker.exit(
                    "MtfLongExit", "MtfLong",
                    limit=bar.close + tp,
                    stop=bar.close - sl,
                )

        self._prev_hist = hist
