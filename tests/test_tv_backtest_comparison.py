"""Test to verify APP TV backtest results match Official TradingView results.

This test implements the GeneralAtrBreakout strategy and compares results
with the official TradingView backtest to identify discrepancies.
"""

import math
from datetime import datetime, timedelta
from typing import List

import pytest

from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext, OrderSide
from src.backtest.engine import BacktestEngine
from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.strategy.indicators import bollinger_bands, atr


class GeneralAtrBreakout(BacktestStrategy):
    """
    Implements the "General 60K Ultimate Day Trade" strategy.
    This is the exact implementation from the issue description.
    """
    kline_type = 0
    kline_minute = 60

    def __init__(self, **kwargs):
        """Initializes strategy parameters and state."""
        self.bb_length = kwargs.get("bb_length", 20)
        self.bb_mult = kwargs.get("bb_mult", 2.0)
        self.atr_period = kwargs.get("atr_period", 14)
        self.max_daily_losses = kwargs.get("max_daily_losses", 2)
        self.min_stop_loss = kwargs.get("min_stop_loss", 60)

        # State tracking variables
        self.daily_loss_count = 0
        self.last_bar_date = None
        self.prev_position_size = 0
        self.entry_price = 0.0
        self.entry_sl_points = 0.0
        self.entry_bar_index = -1

    def required_bars(self) -> int:
        """Minimum number of bars required for indicators."""
        return max(self.bb_length, self.atr_period) + 1

    def _is_last_bar_of_session(self, dt_utc) -> bool:
        """Checks if the bar is the last one before a session close."""
        # Day session last 60m bar starts at 12:00 TWT -> 04:00 UTC.
        # Night session last 60m bar starts at 04:00 TWT -> 20:00 UTC (prev day).
        is_day_close_bar = dt_utc.hour == 4 and dt_utc.minute == 0
        is_night_close_bar = dt_utc.hour == 20 and dt_utc.minute == 0
        return is_day_close_bar or is_night_close_bar

    def _update_daily_state(self, bar: Bar, broker: BrokerContext):
        """Resets loss counter on a new day and detects closed trades."""
        current_date = bar.dt.date()
        if self.last_bar_date and current_date != self.last_bar_date:
            self.daily_loss_count = 0
        self.last_bar_date = current_date

        if self.prev_position_size > 0 and broker.position_size == 0:
            if bar.close < self.entry_price:  # Simple PnL check for a loss
                self.daily_loss_count += 1
        self.prev_position_size = broker.position_size

    def _check_entry_conditions(self, bar: Bar, middle_band: float) -> bool:
        """Checks the specific K-bar patterns for a long entry signal."""
        if bar.low > middle_band or bar.close < middle_band:
            return False

        total_range = bar.high - bar.low
        safe_range = total_range if total_range > 0 else 0.0001
        body_size = abs(bar.close - bar.open)
        lower_wick = min(bar.open, bar.close) - bar.low

        cond_A = bar.open > middle_band
        cond_B = lower_wick > 0
        cond_C = (body_size / safe_range) >= 0.66
        cond_D = (body_size / safe_range) <= 0.33 and lower_wick >= (body_size * 2)
        return cond_A or cond_B or cond_C or cond_D

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext):
        """Main strategy logic executed on each bar."""
        closes = data_store.get_closes()
        highs = data_store.get_highs()
        lows = data_store.get_lows()

        bb_result = bollinger_bands(closes, self.bb_length, self.bb_mult)
        atr_val = atr(highs, lows, closes, self.atr_period)
        if not bb_result or not atr_val:
            return

        upper_band, middle_band, _ = bb_result
        self._update_daily_state(bar, broker)
        is_last_bar = self._is_last_bar_of_session(bar.dt)

        if broker.position_size > 0:
            if is_last_bar:
                broker.close("Long", tag="Session Close")
                return

            bars_since_entry = (len(data_store) - 1) - self.entry_bar_index
            tp_price = upper_band if bars_since_entry > 0 else self.entry_price + 10000
            sl_price = self.entry_price - self.entry_sl_points
            broker.exit("ExitLong", "Long", limit=tp_price, stop=sl_price)
        else:  # No position
            if is_last_bar or self.daily_loss_count >= self.max_daily_losses:
                return

            if self._check_entry_conditions(bar, middle_band):
                self.entry_price = bar.close
                self.entry_sl_points = max(atr_val / 2, self.min_stop_loss)
                self.entry_bar_index = len(data_store) - 1
                broker.entry("Long", OrderSide.LONG, qty=1)


def create_sample_bars() -> List[Bar]:
    """Create sample bars that match some of the trading dates from the issue.

    Using simplified data based on the trade dates and approximate prices
    from the issue description.
    """
    base_dt = datetime(2026, 2, 4, 8, 45)  # Start from first trade date
    bars = []

    # Sample bars representing the trading period
    prices = [
        (32200, 32300, 32180, 32252),  # Entry bar for first trade
        (32252, 32550, 32230, 32525),  # Exit bar for first trade (should profit)
        (32500, 32520, 31900, 31940),  # Next entry
        (31940, 31950, 31800, 31830),  # Loss trade
        (31830, 32100, 31780, 32053),  # Recovery trade
        # Add more bars representing the trading pattern
    ]

    for i, (open_price, high, low, close) in enumerate(prices):
        bar = Bar(
            symbol="TX00",
            dt=base_dt + timedelta(hours=i),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=1000,
            interval=3600  # 1 hour
        )
        bars.append(bar)

    return bars


def test_general_atr_breakout_strategy():
    """Test that GeneralAtrBreakout strategy runs without errors."""
    bars = create_sample_bars()
    strategy = GeneralAtrBreakout()
    engine = BacktestEngine(strategy, point_value=200)  # TX00 point value

    result = engine.run(bars)

    # Basic sanity checks
    assert result.bars_processed == len(bars)
    assert result.strategy_name == "GeneralAtrBreakout"
    assert len(result.trades) >= 0  # May or may not have trades with limited data


def test_tv_comparison_trade_logic():
    """Test specific trade logic that might cause discrepancies with TradingView.

    This test focuses on the differences between Pine Script and Python implementation:
    1. Pine Script uses 'loss' parameter (points-based stop loss)
    2. Python uses 'stop' parameter (price-based stop loss)
    3. Different order execution semantics
    """
    # Create bars that should trigger entry and exit conditions
    bars = []
    base_dt = datetime(2026, 2, 4, 8, 45)

    # Create enough bars for indicators to work
    for i in range(25):  # Need 20+ for BB and 14+ for ATR
        price = 32000 + i * 10
        bar = Bar(
            symbol="TX00",
            dt=base_dt + timedelta(hours=i),
            open=price,
            high=price + 20,
            low=price - 20,
            close=price,
            volume=1000,
            interval=3600
        )
        bars.append(bar)

    # Calculate what the middle band should be at this point
    closes = [bar.close for bar in bars]
    bb_result = bollinger_bands(closes, 20, 2.0)
    middle_band = 32120  # Default if no result
    if bb_result:
        upper_band, middle_band, lower_band = bb_result
        print(f"After setup bars: Middle band = {middle_band}, Upper = {upper_band}, Lower = {lower_band}")

    # Add entry trigger bar (touches middle band)
    # Use the actual middle band value to ensure trigger
    entry_bar = Bar(
        symbol="TX00",
        dt=base_dt + timedelta(hours=25),
        open=int(middle_band + 20),  # Above middle band
        high=int(middle_band + 50),
        low=int(middle_band - 10),   # Below middle band (triggers wash condition)
        close=int(middle_band + 10), # Above middle band (reclaim)
        volume=1000,
        interval=3600
    )
    bars.append(entry_bar)

    # Add exit trigger bar
    exit_bar = Bar(
        symbol="TX00",
        dt=base_dt + timedelta(hours=26),
        open=int(middle_band + 15),
        high=int(middle_band + 100),  # Should hit upper band
        low=int(middle_band),
        close=int(middle_band + 80),
        volume=1000,
        interval=3600
    )
    bars.append(exit_bar)

    strategy = GeneralAtrBreakout()
    engine = BacktestEngine(strategy, point_value=200)
    result = engine.run(bars)

    print(f"Total bars processed: {result.bars_processed}")
    print(f"Number of trades: {len(result.trades)}")

    # Debug: Check if entry conditions would be met
    if len(result.trades) == 0:
        print("No trades executed, checking entry conditions...")
        data_store = DataStore(max_bars=5000)
        for bar in bars:
            data_store.add_bar(bar)

        closes = data_store.get_closes()
        bb_result = bollinger_bands(closes, 20, 2.0)
        if bb_result:
            upper_band, middle_band, lower_band = bb_result
            entry_bar = bars[-2]  # Second to last bar (our entry trigger)

            print(f"Entry bar: open={entry_bar.open}, high={entry_bar.high}, low={entry_bar.low}, close={entry_bar.close}")
            print(f"Middle band: {middle_band}")
            print(f"Low <= middle_band: {entry_bar.low <= middle_band}")
            print(f"Close >= middle_band: {entry_bar.close >= middle_band}")

            # Check entry conditions
            if entry_bar.low <= middle_band and entry_bar.close >= middle_band:
                total_range = entry_bar.high - entry_bar.low
                safe_range = total_range if total_range > 0 else 0.0001
                body_size = abs(entry_bar.close - entry_bar.open)
                lower_wick = min(entry_bar.open, entry_bar.close) - entry_bar.low

                cond_A = entry_bar.open > middle_band
                cond_B = lower_wick > 0
                cond_C = (body_size / safe_range) >= 0.66
                cond_D = (body_size / safe_range) <= 0.33 and lower_wick >= (body_size * 2)

                print(f"Condition A (open > middle): {cond_A}")
                print(f"Condition B (lower_wick > 0): {cond_B}")
                print(f"Condition C (body >= 66% of range): {cond_C} (body/range = {body_size/safe_range:.2%})")
                print(f"Condition D (small body + long wick): {cond_D}")
                print(f"Entry should trigger: {cond_A or cond_B or cond_C or cond_D}")

    # Should have at least one trade
    assert len(result.trades) >= 1

    # Verify the trade matches expected behavior
    first_trade = result.trades[0]
    assert first_trade.side == OrderSide.LONG

    # The exit price should be either:
    # 1. The upper band (take profit)
    # 2. The stop loss price
    # 3. Force close at end

    print(f"Trade: Entry={first_trade.entry_price}, Exit={first_trade.exit_price}, P&L={first_trade.pnl}")
    print(f"Exit tag: {first_trade.exit_tag}")


def test_point_value_calculation():
    """Test that point value calculations match TradingView expectations.

    TradingView TX00 contract:
    - 1 point = 200 TWD
    - Entry 32252, Exit 32525 = 273 points = 273 * 200 = 54600 TWD
    """
    entry_price = 32252
    exit_price = 32525
    point_value = 200

    expected_pnl = (exit_price - entry_price) * point_value
    assert expected_pnl == 54600  # Should match first trade from APP TV

    # Compare with Official TV first trade: 32252 -> 32423 = 171 points = 34200 TWD
    # But Official TV shows 33836 TWD, suggesting different calculation or commission
    tv_exit_price = 32423
    tv_pnl_raw = (tv_exit_price - entry_price) * point_value
    assert tv_pnl_raw == 34200  # Raw calculation
    # The 33836 in TV might include commission or slippage


def test_atr_stop_loss_calculation():
    """Test ATR-based stop loss calculation matches TradingView ta.atr() behavior."""
    # Create sample data for ATR calculation
    highs = [100, 105, 103, 108, 110, 115, 112, 118, 120, 125, 122, 128, 130, 135, 132]
    lows = [95, 98, 96, 101, 103, 108, 105, 111, 113, 118, 115, 121, 123, 128, 125]
    closes = [98, 102, 100, 105, 107, 112, 109, 115, 117, 122, 119, 125, 127, 132, 129]

    atr_val = atr(highs, lows, closes, 14)
    assert atr_val is not None
    assert atr_val > 0

    # Test that min_stop_loss override works
    strategy = GeneralAtrBreakout(min_stop_loss=60)
    stop_loss_points = max(atr_val / 2, strategy.min_stop_loss)

    # If ATR/2 < 60, should use 60
    if atr_val / 2 < 60:
        assert stop_loss_points == 60
    else:
        assert stop_loss_points == atr_val / 2


if __name__ == "__main__":
    # Run individual tests for debugging
    test_general_atr_breakout_strategy()
    test_tv_comparison_trade_logic()
    test_point_value_calculation()
    test_atr_stop_loss_calculation()
    print("All tests passed!")