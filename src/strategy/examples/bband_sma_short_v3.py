"""BbandSmaShortV3 — BB mean-reversion short with trailing stop, break-even, and bounce exit.

Fixes issue #75: the original V3 had only three exit paths (BB lower TP,
fixed SL, EOD) with no mechanism to lock in profits once price moved
favorably.  This version adds:

  - **Trailing stop**: activated after price moves ``trail_activate_pts``
    in favor; trails at ``trail_atr_mult * ATR`` above the lowest low.
  - **Break-even move**: after price moves ``breakeven_pts`` in favor,
    the stop is moved to the entry price (zero-risk).
  - **Bounce exit**: if price bounces ``bounce_pts`` from the extreme
    favorable price, close immediately.

All three are parameterised and default-off-safe (large values effectively
disable a mechanism without changing the logic).
"""

from __future__ import annotations

from ...backtest.strategy import BacktestStrategy
from ...backtest.broker import BrokerContext, OrderSide
from ...market_data.models import Bar
from ...market_data.data_store import DataStore
from ...market_data.sessions import is_last_bar_of_session
from ..indicators import sma, bollinger_bands, atr


class BbandSmaShortV3(BacktestStrategy):
    """BB mean-reversion short strategy with dynamic exit management."""

    kline_type = 0
    kline_minute = 60

    def __init__(self, **kwargs):
        self.bb_period = int(kwargs.get("bb_period", 20))
        self.bb_std = float(kwargs.get("bb_std", 2.0))
        self.sma_period = int(kwargs.get("sma_period", 20))
        self.atr_period = int(kwargs.get("atr_period", 14))

        # Entry: SL buffer above prev_high
        self.atr_sl_mult = float(kwargs.get("atr_sl_mult", 0.1))

        # --- New exit parameters (issue #75) ---
        # Trailing stop: activates after price drops trail_activate_pts from entry
        self.trail_activate_pts = int(kwargs.get("trail_activate_pts", 150))
        self.trail_atr_mult = float(kwargs.get("trail_atr_mult", 1.5))

        # Break-even: move SL to entry after price drops breakeven_pts
        self.breakeven_pts = int(kwargs.get("breakeven_pts", 100))

        # Bounce exit: close if price bounces bounce_pts from the lowest low
        self.bounce_pts = int(kwargs.get("bounce_pts", 200))

        self._reset_state()

    def _reset_state(self):
        self.stop_loss_price = 0
        self.lowest_low = 0
        self.trail_active = False
        self.breakeven_applied = False

    def required_bars(self) -> int:
        return max(self.bb_period, self.sma_period, self.atr_period) + 2

    def exit_levels(self) -> dict:
        if self.stop_loss_price:
            return {"stop": self.stop_loss_price}
        return {}

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        closes = data_store.get_closes()
        highs = data_store.get_highs()
        lows = data_store.get_lows()

        if len(closes) < self.required_bars():
            return

        bb = bollinger_bands(closes, self.bb_period, self.bb_std)
        sma_val = sma(closes, self.sma_period)
        atr_val = atr(highs, lows, closes, self.atr_period)

        if bb is None or sma_val is None or atr_val is None:
            return

        if broker.position_size > 0:
            self._manage_exit(bar, broker, bb, atr_val)
        else:
            self._reset_state()
            self._check_entry(bar, data_store, broker, bb, sma_val, atr_val)

    def _check_entry(self, bar, data_store, broker, bb, sma_val, atr_val):
        price_above_upper = bar.close > bb.upper
        price_above_sma = bar.close > sma_val

        if price_above_upper and price_above_sma:
            prev_highs = data_store.get_highs(2)
            if len(prev_highs) < 2:
                return
            prev_high = prev_highs[0]
            self.stop_loss_price = int(prev_high + (self.atr_sl_mult * atr_val))
            self.lowest_low = bar.low
            broker.entry("Short", OrderSide.SHORT)

    def _manage_exit(self, bar, broker, bb, atr_val):
        entry = broker.effective_entry_price()

        # Track the lowest low since entry (for trailing and bounce)
        if bar.low < self.lowest_low:
            self.lowest_low = bar.low

        favorable_move = entry - self.lowest_low  # positive when profitable

        # --- Break-even move (one-time) ---
        if not self.breakeven_applied and favorable_move >= self.breakeven_pts:
            self.stop_loss_price = entry
            self.breakeven_applied = True

        # --- Trailing stop activation and update ---
        if favorable_move >= self.trail_activate_pts:
            self.trail_active = True

        if self.trail_active:
            trail_stop = int(self.lowest_low + (self.trail_atr_mult * atr_val))
            # Only ratchet DOWN (tighter) for shorts — trail_stop should
            # decrease as lowest_low drops
            if trail_stop < self.stop_loss_price:
                self.stop_loss_price = trail_stop

        # --- Exit checks (priority order) ---

        # 1. TP: BB lower band touch
        if bar.low <= bb.lower:
            broker.close("Short", tag="TP")
            return

        # 2. Bounce exit: price bounced too far from the extreme
        if self.bounce_pts > 0 and (bar.high - self.lowest_low) >= self.bounce_pts:
            broker.close("Short", tag="Bounce")
            return

        # 3. SL / Trailing stop hit
        if bar.high >= self.stop_loss_price:
            tag = "TrailSL" if self.trail_active else "SL"
            broker.close("Short", tag=tag)
            return

        # 4. EOD: session last bar
        if is_last_bar_of_session(bar.dt, self.kline_minute):
            broker.close("Short", tag="EOD")
            return

        # 5. Place pending stop order for intra-bar fill by the broker engine
        broker.exit("SL_order", "Short", stop=self.stop_loss_price)
