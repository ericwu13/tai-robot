"""Per-trade strategy attribution (regime mode).

In regime mode one bot's session spans several strategies (the user
redeploys on each regime switch; session resume keeps the combined trade
history), so every Trade must record which strategy opened it. The label
is snapshotted at ENTRY time — a strategy switch while a position is open
must not relabel the open trade.
"""

from src.backtest.broker import Order, OrderSide, SimulatedBroker, Trade
from src.backtest.report import export_trades_csv


def _open_long(broker: SimulatedBroker, bar_index: int = 0,
               price: int = 20000, dt: str = "2026-07-01 09:00") -> None:
    broker.queue_entry(Order(tag="L", side=OrderSide.LONG, qty=1))
    broker.on_bar_close(bar_index, price, bar_dt=dt)


def _close(broker: SimulatedBroker, bar_index: int = 1,
           price: int = 20100, dt: str = "2026-07-01 10:00") -> None:
    broker.force_close(bar_index, price, bar_dt=dt)


class TestStrategyAttribution:
    def test_trade_records_strategy_active_at_entry(self):
        broker = SimulatedBroker(point_value=10)
        broker.strategy_label = "BbandMonthShortV5"
        _open_long(broker)
        _close(broker)
        assert broker.trades[0].strategy == "BbandMonthShortV5"

    def test_switch_mid_position_keeps_entry_strategy(self):
        # Regime switch while a position is open: the open trade was
        # decided by the OLD strategy and must stay attributed to it.
        broker = SimulatedBroker(point_value=10)
        broker.strategy_label = "TrendFollowerV1"
        _open_long(broker)
        broker.strategy_label = "MeanRevertV2"  # switch mid-position
        _close(broker)
        assert broker.trades[0].strategy == "TrendFollowerV1"

    def test_next_trade_after_switch_gets_new_strategy(self):
        broker = SimulatedBroker(point_value=10)
        broker.strategy_label = "TrendFollowerV1"
        _open_long(broker, bar_index=0)
        _close(broker, bar_index=1)
        broker.strategy_label = "MeanRevertV2"
        _open_long(broker, bar_index=2, dt="2026-07-01 11:00")
        _close(broker, bar_index=3, dt="2026-07-01 12:00")
        assert [t.strategy for t in broker.trades] == [
            "TrendFollowerV1", "MeanRevertV2"]

    def test_entry_strategy_cleared_after_close(self):
        broker = SimulatedBroker(point_value=10)
        broker.strategy_label = "A"
        _open_long(broker)
        _close(broker)
        assert broker.entry_strategy == ""


class TestStrategyPersistence:
    def test_round_trip_preserves_trade_strategy(self):
        broker = SimulatedBroker(point_value=10)
        broker.strategy_label = "OldStrat"
        _open_long(broker, bar_index=0)
        _close(broker, bar_index=1)
        restored = SimulatedBroker.from_dict(broker.to_dict())
        assert restored.trades[0].strategy == "OldStrat"

    def test_resume_open_position_closes_with_original_strategy(self):
        # The regime-mode scenario: OldStrat opened a position, the bot
        # was redeployed with NewStrat (session resume), then the
        # position closes — attribution must stay with OldStrat.
        broker = SimulatedBroker(point_value=10)
        broker.strategy_label = "OldStrat"
        _open_long(broker)

        restored = SimulatedBroker.from_dict(broker.to_dict())
        restored.strategy_label = "NewStrat"  # as restore_session() does
        assert restored.entry_strategy == "OldStrat"

        _close(restored, bar_index=5, dt="2026-07-02 09:00")
        assert restored.trades[0].strategy == "OldStrat"

        # ...and a brand-new trade belongs to the new strategy.
        _open_long(restored, bar_index=6, dt="2026-07-02 10:00")
        _close(restored, bar_index=7, dt="2026-07-02 11:00")
        assert restored.trades[1].strategy == "NewStrat"

    def test_legacy_session_file_defaults_empty(self):
        broker = SimulatedBroker(point_value=10)
        broker.strategy_label = "A"
        _open_long(broker, bar_index=0)
        _close(broker, bar_index=1)
        data = broker.to_dict()
        # Simulate a pre-attribution session.json
        del data["entry_strategy"]
        for t in data["trades"]:
            del t["strategy"]
        restored = SimulatedBroker.from_dict(data)
        assert restored.entry_strategy == ""
        assert restored.trades[0].strategy == ""


class TestStrategyCsvExport:
    def test_export_includes_strategy_column(self, tmp_path):
        trade = Trade(
            tag="L", side=OrderSide.LONG, qty=1,
            entry_price=20000, exit_price=20100,
            entry_bar_index=0, exit_bar_index=1,
            strategy="BbandMonthShortV5",
        )
        path = tmp_path / "trades.csv"
        export_trades_csv([trade], path)
        content = path.read_text(encoding="utf-8")
        assert "Strategy" in content.splitlines()[0]
        assert "BbandMonthShortV5" in content
