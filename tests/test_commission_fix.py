"""Test for commission handling fix that resolves Issue #20.

This test validates that the APP TV backtest engine now properly accounts
for commission like TradingView, reducing the P&L discrepancy.
"""

import pytest
from datetime import datetime, timedelta

from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext, OrderSide
from src.backtest.engine import BacktestEngine
from src.market_data.models import Bar
from src.market_data.data_store import DataStore


class SimpleCommissionTestStrategy(BacktestStrategy):
    """Simple strategy to test commission handling."""

    def required_bars(self) -> int:
        return 1

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        if broker.position_size == 0:
            # Enter on first bar
            broker.entry("Long", OrderSide.LONG, qty=1)
        else:
            # Exit immediately on next bar for simple test
            broker.close("Long", tag="TestExit")


def test_commission_is_deducted_from_pnl():
    """Test that commission is properly deducted from P&L calculations."""
    # Create simple 2-bar scenario
    bars = [
        Bar(
            symbol="TX00",
            dt=datetime(2026, 2, 4, 8, 45),
            open=32000, high=32020, low=31980, close=32000,
            volume=1000, interval=3600
        ),
        Bar(
            symbol="TX00",
            dt=datetime(2026, 2, 4, 9, 45),
            open=32000, high=32020, low=31980, close=32200,  # 200 point gain
            volume=1000, interval=3600
        ),
    ]

    strategy = SimpleCommissionTestStrategy()

    # Test without commission
    engine_no_commission = BacktestEngine(strategy, point_value=200, commission_per_contract=0)
    result_no_commission = engine_no_commission.run(bars)

    # Test with commission (182 TWD per contract per side)
    engine_with_commission = BacktestEngine(strategy, point_value=200, commission_per_contract=182)
    result_with_commission = engine_with_commission.run(bars)

    assert len(result_no_commission.trades) == 1
    assert len(result_with_commission.trades) == 1

    trade_no_commission = result_no_commission.trades[0]
    trade_with_commission = result_with_commission.trades[0]

    # Both should have same entry/exit prices
    assert trade_no_commission.entry_price == trade_with_commission.entry_price
    assert trade_no_commission.exit_price == trade_with_commission.exit_price

    # Calculate expected values
    raw_pnl = (32200 - 32000) * 200  # 40000 TWD
    commission = 2 * 182  # Entry + Exit commission = 364 TWD
    expected_net_pnl = raw_pnl - commission  # 39636 TWD

    # Verify P&L calculations
    assert trade_no_commission.pnl == raw_pnl, f"Without commission: {trade_no_commission.pnl} != {raw_pnl}"
    assert trade_with_commission.pnl == expected_net_pnl, f"With commission: {trade_with_commission.pnl} != {expected_net_pnl}"

    # Commission should be exactly the difference
    commission_deducted = trade_no_commission.pnl - trade_with_commission.pnl
    assert commission_deducted == commission, f"Commission deducted: {commission_deducted} != {commission}"


def test_tv_pnl_discrepancy_explained():
    """Test that explains the P&L discrepancy from Issue #20."""

    # Scenario based on first trade from issue:
    # Entry: 32252, Exit: 32423 (TV official)
    # TV reported P&L: 33836 TWD
    # Raw calculation: (32423 - 32252) * 200 = 34200 TWD
    # Difference: 34200 - 33836 = 364 TWD = commission

    bars = [
        Bar(
            symbol="TX00",
            dt=datetime(2026, 2, 4, 8, 45),
            open=32250, high=32260, low=32240, close=32252,  # Entry
            volume=1000, interval=3600
        ),
        Bar(
            symbol="TX00",
            dt=datetime(2026, 2, 4, 12, 45),
            open=32420, high=32450, low=32400, close=32423,  # TV exit
            volume=1000, interval=3600
        ),
    ]

    strategy = SimpleCommissionTestStrategy()

    # Test with TradingView-style commission (182 per side)
    engine = BacktestEngine(strategy, point_value=200, commission_per_contract=182)
    result = engine.run(bars)

    assert len(result.trades) == 1
    trade = result.trades[0]

    # Entry and exit should match scenario
    assert trade.entry_price == 32252
    assert trade.exit_price == 32423

    # P&L should match TradingView official result
    tv_official_pnl = 33836
    raw_pnl = (32423 - 32252) * 200  # 34200
    commission = 2 * 182  # 364
    expected_pnl = raw_pnl - commission  # 33836

    assert trade.pnl == expected_pnl, f"P&L {trade.pnl} != expected {expected_pnl}"
    assert trade.pnl == tv_official_pnl, f"P&L {trade.pnl} != TV official {tv_official_pnl}"

    print(f"Raw P&L: {raw_pnl}")
    print(f"Commission: {commission}")
    print(f"Net P&L: {trade.pnl}")
    print(f"TV Official P&L: {tv_official_pnl}")
    print(f"Match: {trade.pnl == tv_official_pnl}")


def test_multiple_contracts_commission_scaling():
    """Test that commission scales properly with contract quantity."""
    bars = [
        Bar(symbol="TX00", dt=datetime(2026, 2, 4, 8, 45),
             open=32000, high=32020, low=31980, close=32000,
             volume=1000, interval=3600),
        Bar(symbol="TX00", dt=datetime(2026, 2, 4, 9, 45),
             open=32000, high=32020, low=31980, close=32100,
             volume=1000, interval=3600),
    ]

    class MultiContractStrategy(BacktestStrategy):
        def required_bars(self) -> int:
            return 1

        def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
            if broker.position_size == 0:
                broker.entry("Long", OrderSide.LONG, qty=3)  # 3 contracts
            else:
                broker.close("Long", tag="TestExit")

    strategy = MultiContractStrategy()
    engine = BacktestEngine(strategy, point_value=200, commission_per_contract=182)
    result = engine.run(bars)

    trade = result.trades[0]
    assert trade.qty == 3

    # Commission should be 3 contracts * 2 sides * 182 = 1092
    expected_commission = 3 * 2 * 182
    raw_pnl = (32100 - 32000) * 3 * 200  # 60000
    expected_net_pnl = raw_pnl - expected_commission  # 58908

    assert trade.pnl == expected_net_pnl


def test_commission_serialization():
    """Test that commission setting is preserved in broker serialization."""
    from src.backtest.broker import SimulatedBroker

    broker = SimulatedBroker(point_value=200, commission_per_contract=182)
    serialized = broker.to_dict()

    assert serialized["commission_per_contract"] == 182

    restored_broker = SimulatedBroker.from_dict(serialized)
    assert restored_broker.commission_per_contract == 182
    assert restored_broker.point_value == 200


if __name__ == "__main__":
    test_commission_is_deducted_from_pnl()
    test_tv_pnl_discrepancy_explained()
    test_multiple_contracts_commission_scaling()
    test_commission_serialization()
    print("All commission tests passed!")