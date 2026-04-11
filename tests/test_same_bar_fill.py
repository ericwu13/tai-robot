"""Tests for fill_mode='next_open' (same-bar entry+exit allowed).

Covers the TradingView process_orders_on_close=false default semantics:
- Entry queued on bar N fills at bar N+1's OPEN
- Exit limit/stop checked against bar N+1's OHLC against the just-filled
  position — so a TP/SL hit on bar N+1 closes the trade on the same bar
  it opened (same-bar entry+exit)
- broker.entry_price is the open of the fill bar, not the close of the
  signal bar
- entry_bar_index points at the fill bar (bar N+1), not the signal bar (N)

Compared with the legacy fill_mode='on_close' tests in test_broker_close.py
and test_backtest_engine.py, these tests pin the new behavior so future
changes don't silently regress the same-bar capability.
"""

from datetime import datetime, timedelta

import pytest

from src.market_data.models import Bar
from src.backtest.broker import SimulatedBroker, BrokerContext, OrderSide, Order
from src.backtest.engine import BacktestEngine
from src.backtest.strategy import BacktestStrategy
from src.market_data.data_store import DataStore


def make_bar(i, open_, high, low, close, volume=100):
    base = datetime(2025, 1, 1, 8, 45)
    return Bar(
        symbol="TX00",
        dt=base + timedelta(minutes=i),
        open=open_, high=high, low=low, close=close,
        volume=volume, interval=60,
    )


# ---------- Direct broker tests ---------- #

class TestBrokerNextOpenMode:
    """Unit tests for SimulatedBroker(fill_mode='next_open')."""

    def test_default_fill_mode_is_on_close(self):
        broker = SimulatedBroker()
        assert broker.fill_mode == "on_close"

    def test_on_bar_close_does_not_fill_entry_in_next_open_mode(self):
        """In next_open mode, on_bar_close MUST NOT fill pending entries."""
        broker = SimulatedBroker(point_value=200, fill_mode="next_open")
        ctx = BrokerContext(broker)
        ctx.entry("Long", OrderSide.LONG)
        broker.on_bar_close(0, 20000)
        assert broker.position_size == 0  # entry NOT filled at close

    def test_on_bar_open_fills_entry_at_open_price(self):
        """on_bar_open in next_open mode fills at the open price."""
        broker = SimulatedBroker(point_value=200, fill_mode="next_open")
        ctx = BrokerContext(broker)
        ctx.entry("Long", OrderSide.LONG)
        broker.on_bar_close(0, 20000)        # bar 0 close — no fill
        broker.on_bar_open(1, 20050)         # bar 1 open — fill here
        assert broker.position_size == 1
        assert broker.entry_price == 20050   # open price, not close
        assert broker.entry_bar_index == 1   # FILL bar, not signal bar

    def test_on_bar_open_is_noop_in_on_close_mode(self):
        """In on_close mode, on_bar_open does nothing."""
        broker = SimulatedBroker(point_value=200, fill_mode="on_close")
        ctx = BrokerContext(broker)
        ctx.entry("Long", OrderSide.LONG)
        broker.on_bar_open(0, 20050)         # no-op in on_close mode
        assert broker.position_size == 0
        broker.on_bar_close(0, 20000)        # legacy fill at close
        assert broker.position_size == 1
        assert broker.entry_price == 20000

    def test_market_close_still_fills_at_close_in_next_open_mode(self):
        """broker.close() is market-on-close in BOTH modes."""
        broker = SimulatedBroker(point_value=200, fill_mode="next_open")
        ctx = BrokerContext(broker)
        ctx.entry("Long", OrderSide.LONG)
        broker.on_bar_open(1, 20050)         # entry fills bar 1 open
        ctx.close("Long", tag="manual")
        broker.on_bar_close(1, 20100)        # market close at bar 1 close
        assert broker.position_size == 0
        assert broker.trades[-1].exit_price == 20100
        assert broker.trades[-1].exit_tag == "manual"

    def test_serialization_round_trip_preserves_fill_mode(self):
        broker = SimulatedBroker(point_value=200, fill_mode="next_open")
        data = broker.to_dict()
        assert data["fill_mode"] == "next_open"
        restored = SimulatedBroker.from_dict(data)
        assert restored.fill_mode == "next_open"

    def test_legacy_session_load_defaults_to_on_close(self):
        """Old session JSONs without fill_mode load as on_close."""
        legacy_data = {
            "point_value": 200,
            "position_size": 0,
            "position_side": None,
            "entry_price": 0,
            "entry_tag": "",
            "entry_bar_index": 0,
            "trades": [],
            "equity_curve": [],
        }
        broker = SimulatedBroker.from_dict(legacy_data)
        assert broker.fill_mode == "on_close"


# ---------- Engine integration tests ---------- #

class _SignalOnceStrategy(BacktestStrategy):
    """Queues a single long entry on the first qualifying bar with TP/SL."""

    def __init__(self, tp_offset: int, sl_offset: int):
        self._tp = tp_offset
        self._sl = sl_offset
        self._signaled = False
        self._signal_close = 0

    def required_bars(self) -> int:
        return 1

    def on_bar(self, bar, data_store, broker):
        if not self._signaled and broker.position_size == 0:
            broker.entry("Long", OrderSide.LONG)
            self._signal_close = bar.close
            self._signaled = True
        # Re-queue exit every bar so it stays live (TV pattern)
        if self._signal_close > 0:
            broker.exit(
                "Exit", "Long",
                limit=self._signal_close + self._tp,
                stop=self._signal_close - self._sl,
            )


class TestEngineNextOpenMode:
    """End-to-end engine tests for fill_mode='next_open'."""

    def test_same_bar_tp_hit(self):
        """Entry signals on bar 0; entry fills bar 1 open; TP hit on bar 1 same bar."""
        bars = [
            # bar 0: signal bar; close=20000 → TP=20100, SL=19950
            make_bar(0, 19990, 20010, 19980, 20000),
            # bar 1: open=20020 → entry fills here; high=20150 ≥ 20100 → TP hit
            make_bar(1, 20020, 20150, 20015, 20100),
        ]
        engine = BacktestEngine(
            _SignalOnceStrategy(tp_offset=100, sl_offset=50),
            point_value=200,
            fill_mode="next_open",
        )
        result = engine.run(bars)

        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.entry_price == 20020          # bar 1 open, not bar 0 close
        assert t.exit_price == 20100           # TP price
        assert t.entry_bar_index == 1          # fill bar
        assert t.exit_bar_index == 1           # SAME bar — same-bar enter+exit
        assert t.pnl == (20100 - 20020) * 200

    def test_same_bar_sl_hit(self):
        """Entry fills bar 1 open; SL hit intra-bar 1 same bar."""
        bars = [
            # bar 0: close=20000 → TP=20100, SL=19950
            make_bar(0, 19990, 20010, 19980, 20000),
            # bar 1: open=19980 → fill; low=19940 ≤ 19950 → SL hit
            make_bar(1, 19980, 19990, 19940, 19960),
        ]
        engine = BacktestEngine(
            _SignalOnceStrategy(tp_offset=100, sl_offset=50),
            point_value=200,
            fill_mode="next_open",
        )
        result = engine.run(bars)

        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.entry_price == 19980
        assert t.exit_price == 19950
        assert t.entry_bar_index == 1
        assert t.exit_bar_index == 1           # same-bar exit
        assert t.pnl == (19950 - 19980) * 200  # -6000

    def test_no_same_bar_exit_in_on_close_mode(self):
        """Same data + same strategy under on_close mode: trade lifetime >= 1 bar."""
        bars = [
            make_bar(0, 19990, 20010, 19980, 20000),
            make_bar(1, 20020, 20150, 20015, 20100),
            make_bar(2, 20100, 20200, 20050, 20120),
        ]
        engine = BacktestEngine(
            _SignalOnceStrategy(tp_offset=100, sl_offset=50),
            point_value=200,
            fill_mode="on_close",
        )
        result = engine.run(bars)

        # In on_close mode the entry fills at bar 0 close (20000), and the
        # exit cannot fire on bar 0 — it can fire at the earliest on bar 1.
        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.entry_price == 20000          # bar 0 close, NOT bar 1 open
        assert t.entry_bar_index == 0
        assert t.exit_bar_index >= 1           # at least 1 bar later

    def test_entry_on_last_bar_never_fills(self):
        """Strategy signals on the final bar — no bar to fill at, no trade."""
        bars = [
            make_bar(0, 19990, 20010, 19980, 20000),
        ]
        engine = BacktestEngine(
            _SignalOnceStrategy(tp_offset=100, sl_offset=50),
            point_value=200,
            fill_mode="next_open",
        )
        result = engine.run(bars)

        # Pending entry from bar 0 has no bar 1 to fill at; force_close
        # is a no-op because position_size == 0.
        assert len(result.trades) == 0
        assert result.broker.position_size == 0

    def test_force_close_at_end_of_data(self):
        """Position open with no TP/SL hit gets force-closed at last bar's close."""
        bars = [
            # bar 0: close=20000 → TP=20500, SL=19500 (very wide)
            make_bar(0, 19990, 20010, 19980, 20000),
            # bar 1: open=20020, entry fills, no TP/SL hit
            make_bar(1, 20020, 20100, 19900, 20050),
            # bar 2: still no hit; last bar — force close at 20030
            make_bar(2, 20060, 20080, 19950, 20030),
        ]
        engine = BacktestEngine(
            _SignalOnceStrategy(tp_offset=500, sl_offset=500),
            point_value=200,
            fill_mode="next_open",
        )
        result = engine.run(bars)

        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.entry_price == 20020
        assert t.exit_price == 20030
        assert t.exit_tag == "force_close"

    def test_strategy_required_bars_respected(self):
        """required_bars=3 → strategy first runs on bar 2 → fill on bar 3."""

        class NeedThreeBars(BacktestStrategy):
            def required_bars(self):
                return 3
            def on_bar(self, bar, data_store, broker):
                if broker.position_size == 0:
                    broker.entry("Long", OrderSide.LONG)

        bars = [make_bar(i, 20000, 20050, 19980, 20010) for i in range(5)]
        engine = BacktestEngine(NeedThreeBars(), point_value=1, fill_mode="next_open")
        result = engine.run(bars)

        # Strategy first runs on bar 2 (3 bars in data_store), queues entry.
        # Bar 3 on_bar_open fills it → entry_bar_index=3 (FILL bar, not signal
        # bar 2). No exit ever queued, so the position is force-closed at the
        # last bar (bar 4) close.
        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.entry_bar_index == 3          # fill bar, not signal bar 2
        assert t.exit_bar_index == 4           # force_close bar
        assert t.exit_tag == "force_close"

    def test_ambiguous_bar_stop_first_when_open_below_stop(self):
        """When bar 1 open <= stop AND high >= TP, the SL fires first."""
        bars = [
            # bar 0: close=20000 → TP=20100, SL=19950
            make_bar(0, 19990, 20010, 19980, 20000),
            # bar 1: open=19940 (below SL), high=20120 (above TP), low=19930
            make_bar(1, 19940, 20120, 19930, 20050),
        ]
        engine = BacktestEngine(
            _SignalOnceStrategy(tp_offset=100, sl_offset=50),
            point_value=200,
            fill_mode="next_open",
        )
        result = engine.run(bars)

        assert len(result.trades) == 1
        t = result.trades[0]
        # Entry fills at gap-down open=19940. Stop=19950, open=19940 < stop,
        # so SL fires first at min(open, stop) = 19940 (gap-down fill).
        assert t.entry_price == 19940
        assert t.exit_price == 19940           # gap-down stop fill
        assert t.exit_bar_index == 1
