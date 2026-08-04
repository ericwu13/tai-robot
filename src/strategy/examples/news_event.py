"""事件強制進場策略 News-event forced-entry strategies (1分K, 全盤).

由新聞斷路器部署，永不由常規 regime selector 選用 — 進場條件就是外部訊號本身。
Deployed by the news circuit breaker, never by the regular regime selector:
the entry condition IS the external signal.

一般找型態的策略抓不到消息面的單邊行情（等不到回踩、等不到交叉），
所以這裡不判斷型態：部署後暖機完成即無條件進場一次，之後只管風險 —
ATR 停損 + 時間停損，出場後永不再進場。

Setup-hunting legs don't trade runaway news moves (the pullback or the
crossover never comes), so this strategy hunts nothing: once warmed up it
enters unconditionally, exactly once, and from then on only manages risk
— an ATR stop plus a time stop.  After the position closes it never
re-enters; the circuit breaker deploys a fresh instance for a new event.

進場 Entry:  暖機 warmup_bars 根後的下一根K棒，無條件進場（一次）
停損 Stop:   進場參考價 ∓ ATR × stop_atr_mult（ATR 不足時用 fallback_stop_points）
時間停損 Time stop: 持倉滿 time_stop_bars 根K棒仍未出場 → 市價平倉
停利 Take-profit: 無（Phase 1 從簡 — 行情要嘛跑，要嘛被停損/時間停損收掉）
"""

from __future__ import annotations

from ...market_data.models import Bar
from ...market_data.data_store import DataStore
from ...strategy.indicators.atr import atr
from ...backtest.strategy import BacktestStrategy
from ...backtest.broker import BrokerContext, OrderSide


class NewsEventStrategy(BacktestStrategy):
    """事件策略共用基底 — 子類別只換方向與標籤。

    Shared base for the long/short pair; subclasses only set ``side`` and
    the order tags.  Not deployed directly.
    """

    kline_type = 0    # 分鐘K線
    kline_minute = 1  # 1分K

    side: OrderSide = OrderSide.LONG
    entry_tag: str = "News"
    exit_tag: str = "Exit News"

    def __init__(
        self,
        atr_period: int = 14,
        stop_atr_mult: float = 2.5,
        fallback_stop_points: int = 150,
        time_stop_bars: int = 240,
        warmup_bars: int = 15,
    ):
        self.atr_period = atr_period
        self.stop_atr_mult = stop_atr_mult
        self.fallback_stop_points = fallback_stop_points
        self.time_stop_bars = time_stop_bars
        self.warmup_bars = warmup_bars
        # One-shot latch: set at the entry signal and NEVER cleared, so a
        # stopped-out event trade can't be re-opened by the next bar.
        self._entered: bool = False
        self._stop_price: int = 0
        self._stop_distance: float = 0.0
        self._entry_ref: int = 0
        self._bars_in_position: int = 0

    def required_bars(self) -> int:
        return max(self.warmup_bars, self.atr_period + 1)

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        bars = data_store.get_bars()

        # Warmup is measured by bars AVAILABLE, not by on_bar() call count:
        # live warmup / history-replay bars land in the DataStore without
        # on_bar() ever firing, and an event strategy must be able to enter
        # on its first live bar rather than sitting out another 15 minutes.
        entering = False
        if (not self._entered
                and broker.position_size == 0
                and len(bars) > self.warmup_bars):
            broker.entry(self.entry_tag, self.side)
            self._entered = True
            entering = True
            self._entry_ref = bar.close
            self._stop_distance = self._compute_stop_distance(bars)

        # Flat and not entering: either still warming up, or the one and
        # only event trade is already done. Nothing to manage.
        if broker.position_size == 0 and not entering:
            return

        if broker.position_size > 0:
            self._bars_in_position += 1
            if self._bars_in_position >= self.time_stop_bars:
                broker.close(self.entry_tag, "time_stop")
                return

        # Stop tracks the REAL fill price once the broker confirms it
        # (slippage-aware); before that, the entry bar's close.
        ref = broker.effective_entry_price() or self._entry_ref
        if self.side == OrderSide.LONG:
            self._stop_price = round(ref - self._stop_distance)
        else:
            self._stop_price = round(ref + self._stop_distance)
        broker.exit(self.exit_tag, self.entry_tag, stop=self._stop_price)

    def _compute_stop_distance(self, bars: list[Bar]) -> float:
        """Stop distance in points: ATR-scaled, or the flat fallback.

        A news deploy can land with barely any history (fresh bot, thin
        session), so an unavailable ATR must not mean an unprotected
        position — it means a fixed-point stop.
        """
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]
        atr_val = atr(highs, lows, closes, self.atr_period)
        if atr_val is None or atr_val <= 0:
            return float(self.fallback_stop_points)
        return atr_val * self.stop_atr_mult


class NewsEventShort(NewsEventStrategy):
    """事件放空 — 斷路器 deploy_short 時部署。Forced-entry short."""

    side = OrderSide.SHORT
    entry_tag = "NewsShort"
    exit_tag = "Exit NewsShort"


class NewsEventLong(NewsEventStrategy):
    """事件做多 — 斷路器 deploy_long 時部署。Forced-entry long."""

    side = OrderSide.LONG
    entry_tag = "NewsLong"
    exit_tag = "Exit NewsLong"
