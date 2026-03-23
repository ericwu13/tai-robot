"""Tests for BacktestEngine bar replay."""

from datetime import datetime

import pytest

from src.market_data.models import Bar
from src.backtest.engine import BacktestEngine
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext, OrderSide
from src.market_data.data_store import DataStore


def make_bar(i, open_, high, low, close, volume=100):
    from datetime import timedelta
    base = datetime(2025, 1, 1, 8, 45)
    return Bar(
        symbol="TX00",
        dt=base + timedelta(hours=4 * i),
        open=open_, high=high, low=low, close=close,
        volume=volume, interval=14400,
    )


class AlwaysLongStrategy(BacktestStrategy):
    """Test strategy: enter long if flat, always queue exit with fixed TP/SL.

    Mirrors TradingView pattern where strategy.exit() runs every bar.
    Exit queued on bar N is checked on bar N+1.
    """

    def __init__(self, tp_offset=100, sl_offset=50):
        self._tp_offset = tp_offset
        self._sl_offset = sl_offset
        self._entry_close = 0

    def required_bars(self) -> int:
        return 1

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        if broker.position_size == 0:
            broker.entry("Long", OrderSide.LONG)
            self._entry_close = bar.close
        # Always queue exit (like TradingView strategy.exit every bar)
        broker.exit("Exit", "Long",
                    limit=self._entry_close + self._tp_offset,
                    stop=self._entry_close - self._sl_offset)


class NeverTradeStrategy(BacktestStrategy):
    def required_bars(self) -> int:
        return 1

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        pass


class TestBacktestEngine:
    def test_basic_winning_trade(self):
        """Entry on bar 0, exit queued on bar 0, TP checked on bar 1."""
        bars = [
            make_bar(0, 20000, 20050, 19980, 20010),  # entry at 20010, queue exit TP=20110 SL=19960
            make_bar(1, 20020, 20200, 20000, 20150),   # check_exits: TP 20110 hit (high=20200 >= 20110)
        ]
        engine = BacktestEngine(AlwaysLongStrategy(), point_value=200)
        result = engine.run(bars)

        # First trade should be the TP hit
        assert result.bars_processed == 2
        first_trade = result.trades[0]
        assert first_trade.entry_price == 20010
        assert first_trade.exit_price == 20110
        assert first_trade.pnl == 100 * 200

    def test_basic_losing_trade(self):
        """Entry on bar 0, SL hit on bar 1."""
        bars = [
            make_bar(0, 20000, 20050, 19980, 20010),  # entry at 20010, TP=20110 SL=19960
            make_bar(1, 19990, 20020, 19900, 19950),   # SL hit: low=19900 <= 19960
        ]
        engine = BacktestEngine(AlwaysLongStrategy(), point_value=200)
        result = engine.run(bars)

        first_trade = result.trades[0]
        assert first_trade.exit_price == 19960
        assert first_trade.pnl == (19960 - 20010) * 200

    def test_force_close_at_end(self):
        """When neither TP nor SL is hit, position is force-closed at last bar's close."""
        bars = [
            make_bar(0, 20000, 20050, 19980, 20010),  # entry at 20010, TP=20110 SL=19960
            make_bar(1, 20000, 20050, 19970, 20000),   # TP not hit, SL not hit (low=19970 > 19960)
        ]
        engine = BacktestEngine(AlwaysLongStrategy(), point_value=1)
        result = engine.run(bars)

        assert len(result.trades) == 1
        assert result.trades[0].exit_price == 20000
        assert result.trades[0].exit_tag == "force_close"

    def test_no_trades_strategy(self):
        bars = [
            make_bar(0, 20000, 20050, 19980, 20010),
            make_bar(1, 20010, 20060, 19990, 20020),
            make_bar(2, 20020, 20070, 20000, 20030),
        ]
        engine = BacktestEngine(NeverTradeStrategy())
        result = engine.run(bars)

        assert result.bars_processed == 3
        assert len(result.trades) == 0

    def test_empty_bars(self):
        engine = BacktestEngine(NeverTradeStrategy())
        result = engine.run([])
        assert result.bars_processed == 0
        assert len(result.trades) == 0

    def test_multiple_trades(self):
        """Three trades: same-bar re-entry allowed (matches TradingView).

        Exit fills intra-bar at TP/SL price, entry fills at bar close.
        In live, tick-level exit detection separates these naturally.
        - Bar 0: enter at 20010
        - Bar 1: TP hit at 20110 (exit intra-bar), re-enter at 20150 (close)
        - Bar 2: SL hit at 20100 (exit intra-bar), re-enter at 20080 (close)
        - Bar 3: force close at 20070
        """
        bars = [
            make_bar(0, 20000, 20050, 19980, 20010),   # enter at 20010, TP=20110 SL=19960
            make_bar(1, 20020, 20200, 20000, 20150),    # TP hit at 20110, re-enter at 20150
            make_bar(2, 20140, 20160, 20050, 20080),    # SL hit at 20100, re-enter at 20080
            make_bar(3, 20090, 20100, 20050, 20070),    # last bar, force close at 20070
        ]
        engine = BacktestEngine(AlwaysLongStrategy(), point_value=1)
        result = engine.run(bars)

        assert len(result.trades) == 3
        assert result.trades[0].entry_price == 20010
        assert result.trades[0].exit_price == 20110  # TP
        assert result.trades[0].pnl == 100
        # Same-bar re-entry at bar 1 close
        assert result.trades[1].entry_price == 20150
        assert result.trades[1].exit_price == 20100  # SL on bar 2
        assert result.trades[1].pnl == -50
        # Same-bar re-entry at bar 2 close
        assert result.trades[2].entry_price == 20080
        assert result.trades[2].exit_price == 20070  # force close
        assert result.trades[2].pnl == -10

    def test_result_has_metrics(self):
        bars = [
            make_bar(0, 20000, 20050, 19980, 20010),
            make_bar(1, 20020, 20200, 20000, 20150),
        ]
        engine = BacktestEngine(AlwaysLongStrategy(), point_value=200)
        result = engine.run(bars)

        assert result.metrics is not None
        assert result.metrics.total_trades >= 1
        assert result.strategy_name == "AlwaysLongStrategy"

    def test_strategy_waits_for_required_bars(self):
        """Strategy with required_bars=3 should not trade on first 2 bars."""

        class NeedThreeBars(BacktestStrategy):
            def required_bars(self):
                return 3
            def on_bar(self, bar, data_store, broker):
                if broker.position_size == 0:
                    broker.entry("Long", OrderSide.LONG)

        bars = [make_bar(i, 20000, 20050, 19980, 20010) for i in range(5)]
        engine = BacktestEngine(NeedThreeBars(), point_value=1)
        result = engine.run(bars)

        # Entry should happen on bar 2 (first bar with 3 bars in data_store)
        assert result.trades[0].entry_bar_index == 2
