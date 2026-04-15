"""Tests for session persistence: save, load, broker serialization, LiveRunner resume."""

import os
import json
from datetime import datetime

import pytest

from src.backtest.broker import SimulatedBroker, OrderSide, Trade
from src.live.session_store import save_session, load_session, session_summary


class TestBrokerSerialization:
    def test_roundtrip_empty(self):
        broker = SimulatedBroker(point_value=200)
        data = broker.to_dict()
        restored = SimulatedBroker.from_dict(data)

        assert restored.point_value == 200
        assert restored.position_size == 0
        assert restored.trades == []
        assert restored.equity_curve == []

    def test_roundtrip_with_trades(self):
        broker = SimulatedBroker(point_value=200)
        # Simulate a completed trade
        broker.queue_entry(
            __import__("src.backtest.broker", fromlist=["Order"]).Order(
                tag="BB_Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 22500)
        broker.check_exits(1, 22600, 22700, 22550, 22650)
        # Force close
        broker.force_close(2, 22600)

        data = broker.to_dict()
        restored = SimulatedBroker.from_dict(data)

        assert len(restored.trades) == 1
        assert restored.trades[0].tag == "BB_Long"
        assert restored.trades[0].side == OrderSide.LONG
        assert restored.trades[0].entry_price == 22500
        assert restored.trades[0].exit_price == 22600
        assert restored.trades[0].pnl == 20000  # (22600-22500)*1*200
        assert restored._cumulative_pnl == 20000
        assert restored.equity_curve == [20000]
        assert restored.position_size == 0

    def test_roundtrip_with_open_position(self):
        broker = SimulatedBroker(point_value=50)
        broker.queue_entry(
            __import__("src.backtest.broker", fromlist=["Order"]).Order(
                tag="entry1", side=OrderSide.SHORT, qty=1))
        broker.on_bar_close(5, 18000, "2026-04-12 10:30")

        data = broker.to_dict()
        restored = SimulatedBroker.from_dict(data)

        assert restored.position_size == 1
        assert restored.position_side == OrderSide.SHORT
        assert restored.entry_price == 18000
        assert restored.entry_tag == "entry1"
        assert restored.entry_bar_index == 5
        # _entry_dt must survive roundtrip — otherwise a restart mid-trade
        # produces an empty entry_dt on the closing Trade.
        assert restored._entry_dt == "2026-04-12 10:30"

    def test_from_dict_missing_entry_dt_defaults_to_empty(self):
        """Backward compat: old session files (before _entry_dt persistence)
        should load without error and default _entry_dt to empty string."""
        data = {
            "point_value": 50, "fill_mode": "on_close",
            "position_size": 1, "position_side": "LONG",
            "entry_price": 18000, "entry_tag": "entry1",
            "entry_bar_index": 5,
            # _entry_dt intentionally absent
            "trades": [], "equity_curve": [],
            "_cumulative_pnl": 0, "_bar_index": 5,
        }
        restored = SimulatedBroker.from_dict(data)
        assert restored._entry_dt == ""
        assert restored.entry_price == 18000  # other fields still load


class TestSessionStore:
    def test_save_and_load(self, tmp_path):
        path = str(tmp_path / "session.json")
        data = {"strategy": "TestStrat", "broker": {"trades": [], "point_value": 200}}
        save_session(path, data)

        loaded = load_session(path)
        assert loaded is not None
        assert loaded["strategy"] == "TestStrat"

    def test_load_nonexistent(self, tmp_path):
        assert load_session(str(tmp_path / "nope.json")) is None

    def test_load_corrupt(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            f.write("{invalid json")
        assert load_session(path) is None

    def test_atomic_overwrite(self, tmp_path):
        path = str(tmp_path / "session.json")
        save_session(path, {"v": 1})
        save_session(path, {"v": 2})

        loaded = load_session(path)
        assert loaded["v"] == 2
        # No .tmp file left behind
        assert not os.path.exists(path + ".tmp")

    def test_session_summary(self):
        data = {
            "started_at": "2026-03-04T09:00:00",
            "saved_at": "2026-03-04T13:00:00",
            "broker": {
                "trades": [{"pnl": 5000}, {"pnl": -2000}],
                "_cumulative_pnl": 3000,
                "position_size": 1,
                "position_side": "LONG",
                "entry_price": 22500,
            },
        }
        s = session_summary(data)
        assert "Trades: 2" in s
        assert "+3,000" in s
        assert "LONG" in s
        assert "22,500" in s


class TestLiveRunnerSession:
    def test_save_on_stop_with_trades(self, tmp_path):
        """Session file is created on stop and contains broker state."""
        from src.live.live_runner import LiveRunner
        from tests.test_live_runner import AlwaysLongStrategy, _kline, _klines_1m

        strategy = AlwaysLongStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path), bot_name="SessionTest")

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)

        # Feed enough bars to trigger entry
        lines = _klines_1m("2026-03-01", 540, 60)
        runner.feed_1m_bars(lines)
        runner.stop()

        # Session file should exist after stop
        assert os.path.isfile(runner.session_path)

        data = load_session(runner.session_path)
        assert data is not None
        assert data["strategy"] == strategy.name
        assert data["symbol"] == "TX00"
        assert data["bot_name"] == "SessionTest"
        assert "broker" in data
        assert data["broker"]["point_value"] == 200

    def test_restore_session(self, tmp_path):
        from src.live.live_runner import LiveRunner
        from tests.test_live_runner import NeverTradeStrategy, _kline

        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path), bot_name="ResumeTest")

        # Create fake session data
        session_data = {
            "started_at": "2026-03-04T09:00:00",
            "bar_index": 50,
            "broker": {
                "point_value": 200,
                "position_size": 0,
                "position_side": None,
                "entry_price": 0,
                "entry_tag": "",
                "entry_bar_index": 0,
                "trades": [
                    {
                        "tag": "BB_Long", "side": "LONG", "qty": 1,
                        "entry_price": 22500, "exit_price": 22700,
                        "entry_bar_index": 10, "exit_bar_index": 20,
                        "pnl": 40000, "exit_tag": "TP",
                    },
                ],
                "equity_curve": [40000],
                "_cumulative_pnl": 40000,
                "_bar_index": 50,
                "_exit_bar_index": 20,
            },
        }

        n = runner.restore_session(session_data)
        assert n == 1
        assert len(runner.broker.trades) == 1
        assert runner.broker.trades[0].pnl == 40000
        assert runner.broker._cumulative_pnl == 40000
        assert runner._bar_index == 50

    def test_save_on_stop(self, tmp_path):
        from src.live.live_runner import LiveRunner
        from tests.test_live_runner import NeverTradeStrategy, _kline

        strategy = NeverTradeStrategy()
        runner = LiveRunner(strategy, "TX00", point_value=200,
                            log_dir=str(tmp_path), bot_name="StopSave")

        warmup = [_kline(f"2026-02-{d:02d} 09:00") for d in range(20, 25)]
        runner.feed_warmup_bars(warmup)
        runner.stop()

        assert os.path.isfile(runner.session_path)
