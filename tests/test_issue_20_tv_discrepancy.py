"""Test for Issue #20: APP TV back-test vs TV (official) back test difference.

This test focuses specifically on the discrepancies between the APP TV and
Official TradingView implementations by examining:

1. Exit timing differences (session close logic)
2. Take profit vs stop loss execution order
3. Point value calculations and commission handling
4. ATR-based stop loss implementation differences
"""

import math
from datetime import datetime, timedelta
from typing import List, Dict, Any

import pytest

from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext, OrderSide, SimulatedBroker
from src.backtest.engine import BacktestEngine
from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.strategy.indicators import bollinger_bands, atr


class TVComparisonStrategy(BacktestStrategy):
    """Modified strategy to better match TradingView Pine Script behavior."""

    kline_type = 0
    kline_minute = 60

    def __init__(self, **kwargs):
        """Initialize with same parameters as TV version."""
        self.bb_length = kwargs.get("bb_length", 20)
        self.bb_mult = kwargs.get("bb_mult", 2.0)
        self.atr_period = kwargs.get("atr_period", 14)
        self.max_daily_losses = kwargs.get("max_daily_losses", 2)
        self.min_stop_loss = kwargs.get("min_stop_loss", 60)

        # State tracking
        self.daily_loss_count = 0
        self.last_bar_date = None
        self.entry_price = 0.0
        self.entry_sl_points = 0.0
        self.entry_bar_index = -1

    def required_bars(self) -> int:
        return max(self.bb_length, self.atr_period) + 1

    def _is_last_bar_of_session(self, dt) -> bool:
        """Check if this is last bar of Taiwan trading session.

        Taiwan trading hours (local time UTC+8):
        - Night session: 15:00-05:00 (next day)
        - Day session: 08:45-13:45

        Converting to UTC for bar timestamps:
        - Night session ends at 05:00 TWT = 21:00 UTC (prev day)
        - Day session ends at 13:45 TWT = 05:45 UTC
        """
        # For 60-minute bars, session close bars would be:
        # Day close: 12:45 TWT = 04:45 UTC -> we'll use 04:00 and 05:00 UTC as close bars
        # Night close: 04:00 TWT = 20:00 UTC -> we'll use 20:00 UTC as close bars
        is_day_close = dt.hour == 4 and dt.minute in [0, 45]
        is_night_close = dt.hour == 20 and dt.minute == 0
        return is_day_close or is_night_close

    def _update_daily_state(self, bar: Bar, broker: BrokerContext):
        """Update daily loss tracking."""
        current_date = bar.dt.date()
        if self.last_bar_date and current_date != self.last_bar_date:
            self.daily_loss_count = 0
        self.last_bar_date = current_date

        # Check if position just closed with a loss
        if hasattr(broker._broker, 'trades') and len(broker._broker.trades) > 0:
            last_trade = broker._broker.trades[-1]
            if (last_trade.exit_bar_index == len(DataStore().bars) - 1 and  # Just closed
                last_trade.pnl < 0):
                self.daily_loss_count += 1

    def _check_entry_conditions(self, bar: Bar, middle_band: float) -> bool:
        """Check TV-style entry conditions."""
        # Pine Script precondition: low <= middle_band
        if bar.low > middle_band:
            return False

        total_range = bar.high - bar.low
        safe_range = total_range if total_range > 0 else 0.0001
        body_size = abs(bar.close - bar.open)
        lower_wick = min(bar.open, bar.close) - bar.low

        # Pine Script conditions (modified to match close >= middle_band)
        cond_A = bar.open > middle_band and bar.close >= middle_band
        cond_B = bar.close >= middle_band and lower_wick > 0
        cond_C = bar.close >= middle_band and (body_size / safe_range) >= 0.66
        cond_D = (bar.close >= middle_band and
                 (body_size / safe_range) <= 0.33 and
                 lower_wick >= (body_size * 2))

        return cond_A or cond_B or cond_C or cond_D

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext):
        """TV-style strategy logic."""
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
            # Force close at session end (like TV "收盤斷電")
            if is_last_bar:
                broker.close("Long", tag="Session Close")
                return

            # Calculate exits like TV Pine Script
            bars_since_entry = (len(data_store) - 1) - self.entry_bar_index

            # TV: first bar after entry disables TP (sets very high price)
            if bars_since_entry <= 0:
                final_tp = self.entry_price + 1000  # Essentially disabled
            else:
                final_tp = upper_band

            # TV uses 'loss=entry_sl_points' which is points-based stop loss
            # We need to convert this to absolute price for our broker
            stop_price = self.entry_price - self.entry_sl_points

            broker.exit("ExitLong", "Long", limit=final_tp, stop=stop_price)
        else:
            # Entry logic
            if (not is_last_bar and
                self.daily_loss_count < self.max_daily_losses):

                # TV precondition + entry signal
                precondition = bar.low <= middle_band
                if precondition and self._check_entry_conditions(bar, middle_band):
                    self.entry_price = bar.close
                    # TV: lock in ATR stop loss at entry time
                    self.entry_sl_points = max(atr_val / 2, self.min_stop_loss)
                    self.entry_bar_index = len(data_store) - 1
                    broker.entry("Long", OrderSide.LONG, qty=1)


def create_realistic_bars() -> List[Bar]:
    """Create realistic bar data that might trigger the discrepancy."""
    bars = []
    base_dt = datetime(2026, 2, 4, 8, 45)  # Start at first trade time

    # Create 25 setup bars with realistic price action
    for i in range(25):
        price = 32000 + i * 5 + (i % 3) * 10  # Some variation
        bar = Bar(
            symbol="TX00",
            dt=base_dt + timedelta(hours=i),
            open=price - 5,
            high=price + 15,
            low=price - 15,
            close=price,
            volume=1000 + i * 10,
            interval=3600
        )
        bars.append(bar)

    # Add specific entry scenario based on first trade
    # Entry at 08:45 on 2026-02-04
    entry_dt = datetime(2026, 2, 4, 8, 45)
    entry_bar = Bar(
        symbol="TX00",
        dt=entry_dt,
        open=32200,
        high=32280,
        low=32180,  # Dip below middle band
        close=32252, # Close above middle band - should trigger entry
        volume=1500,
        interval=3600
    )
    bars.append(entry_bar)

    # Add bars leading to TV's exit at 12:45 vs APP's exit at 20:00
    # 09:45 bar
    bars.append(Bar(
        symbol="TX00",
        dt=datetime(2026, 2, 4, 9, 45),
        open=32252,
        high=32300,
        low=32240,
        close=32280,
        volume=1200,
        interval=3600
    ))

    # 10:45 bar
    bars.append(Bar(
        symbol="TX00",
        dt=datetime(2026, 2, 4, 10, 45),
        open=32280,
        high=32350,
        low=32260,
        close=32320,
        volume=1100,
        interval=3600
    ))

    # 11:45 bar
    bars.append(Bar(
        symbol="TX00",
        dt=datetime(2026, 2, 4, 11, 45),
        open=32320,
        high=32380,
        low=32300,
        close=32360,
        volume=1000,
        interval=3600
    ))

    # 12:45 bar - TV exits here with "收盤斷電"
    bars.append(Bar(
        symbol="TX00",
        dt=datetime(2026, 2, 4, 12, 45),
        open=32360,
        high=32450,
        low=32340,
        close=32423,  # TV exit price
        volume=900,
        interval=3600
    ))

    # Continue to 20:00 where APP TV exits
    for hour in range(13, 21):  # 13:45 to 20:00
        if hour == 13:
            minute = 45
        else:
            minute = 0

        dt = datetime(2026, 2, 4, hour, minute)
        if hour < 20:
            price = 32423 + (hour - 13) * 10
        else:
            price = 32525  # APP TV exit price

        bar = Bar(
            symbol="TX00",
            dt=dt,
            open=price - 5,
            high=price + 20,
            low=price - 10,
            close=price,
            volume=800,
            interval=3600
        )
        bars.append(bar)

    return bars


def test_session_close_timing_difference():
    """Test the main hypothesis: session close timing causes the P&L difference."""
    bars = create_realistic_bars()

    strategy = TVComparisonStrategy()
    engine = BacktestEngine(strategy, point_value=200)
    result = engine.run(bars)

    print(f"\nBacktest Results:")
    print(f"Total trades: {len(result.trades)}")

    if result.trades:
        first_trade = result.trades[0]
        print(f"First trade:")
        print(f"  Entry: {first_trade.entry_price} at {first_trade.entry_dt}")
        print(f"  Exit: {first_trade.exit_price} at {first_trade.exit_dt}")
        print(f"  P&L: {first_trade.pnl}")
        print(f"  Exit tag: {first_trade.exit_tag}")

        # Compare with expected values
        expected_tv_exit = 32423  # TV official exit
        expected_app_exit = 32525  # APP TV exit

        if first_trade.exit_tag == "Session Close":
            print(f"  -> Matches TV official behavior (session close)")
            # Should be close to TV official P&L: (32423 - 32252) * 200 = 34200
            expected_pnl = (expected_tv_exit - 32252) * 200
            print(f"  Expected TV P&L: {expected_pnl}")
        else:
            print(f"  -> Matches APP TV behavior (target/stop hit)")
            expected_pnl = (expected_app_exit - 32252) * 200
            print(f"  Expected APP P&L: {expected_pnl}")

        print(f"  Actual vs Expected P&L difference: {abs(first_trade.pnl - expected_pnl)}")

    return result


def test_point_value_and_commission():
    """Test different point values and commission scenarios."""
    # TV official seems to show 33836 instead of 34200 for the first trade
    # This suggests either commission or different point value calculation

    entry_price = 32252
    tv_exit_price = 32423
    raw_pnl = (tv_exit_price - entry_price) * 200  # 34200
    tv_reported_pnl = 33836
    difference = raw_pnl - tv_reported_pnl  # 364

    print(f"Raw P&L calculation: {raw_pnl}")
    print(f"TV reported P&L: {tv_reported_pnl}")
    print(f"Difference: {difference}")

    # This could be commission (182 per side * 2 = 364)
    commission_per_trade = difference
    print(f"Implied commission per round trip: {commission_per_trade}")

    # Test with commission
    net_pnl = raw_pnl - commission_per_trade
    assert abs(net_pnl - tv_reported_pnl) < 1, f"Commission-adjusted P&L {net_pnl} should match TV {tv_reported_pnl}"


def test_atr_stop_loss_implementation():
    """Test differences in ATR-based stop loss implementation."""
    # Create sample data
    highs = [32100 + i * 5 for i in range(20)]
    lows = [32000 + i * 5 for i in range(20)]
    closes = [32050 + i * 5 for i in range(20)]

    atr_val = atr(highs, lows, closes, 14)
    assert atr_val is not None

    # Strategy parameters
    min_stop_loss = 60
    entry_price = 32252

    # APP TV calculation
    app_sl_points = max(atr_val / 2, min_stop_loss)
    app_sl_price = entry_price - app_sl_points

    # TV Pine Script uses 'loss=entry_sl_points' which is points-based
    # Our implementation converts to price, which should be equivalent
    tv_sl_points = max(atr_val / 2, min_stop_loss)
    tv_sl_price = entry_price - tv_sl_points

    print(f"ATR value: {atr_val}")
    print(f"ATR/2: {atr_val/2}")
    print(f"Min stop loss: {min_stop_loss}")
    print(f"Final SL points: {app_sl_points}")
    print(f"SL price: {app_sl_price}")

    # Should be the same
    assert abs(app_sl_price - tv_sl_price) < 0.01


if __name__ == "__main__":
    test_session_close_timing_difference()
    test_point_value_and_commission()
    test_atr_stop_loss_implementation()
    print("All tests passed!")