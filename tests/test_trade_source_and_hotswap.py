"""Tests for trade source tagging, hot-swap mode switching, and report source split."""

import json
import os
import tempfile
from datetime import datetime

import pytest

from src.backtest.broker import (
    SimulatedBroker, Trade, OrderSide, Order, _mode_to_source,
)
from src.evolution.fitness import SOURCE_BACKTEST, SOURCE_PAPER, SOURCE_REAL, VALID_SOURCES
from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.backtest.strategy import BacktestStrategy
from src.live.live_runner import LiveRunner, LiveState


# ── Helpers ──

class NeverTradeStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        pass

    def required_bars(self):
        return 2


def _make_bar(dt=None, open_=20000, high=20100, low=19900, close=20050, volume=100, interval=900):
    return Bar(
        dt=dt or datetime(2026, 6, 11, 9, 0),
        open=open_, high=high, low=low, close=close,
        volume=volume, interval=interval,
    )


# ---------------------------------------------------------------------------
# Feature 1: Trade Source Tagging
# ---------------------------------------------------------------------------

class TestModeToSource:
    def test_semi_auto_is_real(self):
        assert _mode_to_source("semi_auto") == "real"

    def test_auto_is_real(self):
        assert _mode_to_source("auto") == "real"

    def test_paper_is_paper(self):
        assert _mode_to_source("paper") == "paper"

    def test_default_is_backtest(self):
        assert _mode_to_source("backtest") == "backtest"
        assert _mode_to_source("") == "backtest"
        assert _mode_to_source("unknown") == "backtest"


class TestSourceConstants:
    def test_source_real_exists(self):
        assert SOURCE_REAL == "real"

    def test_valid_sources_includes_real(self):
        assert SOURCE_REAL in VALID_SOURCES
        assert SOURCE_BACKTEST in VALID_SOURCES
        assert SOURCE_PAPER in VALID_SOURCES


class TestTradeSourceField:
    def test_trade_default_source_empty(self):
        t = Trade(
            tag="L", side=OrderSide.LONG, qty=1,
            entry_price=20000, exit_price=20100,
            entry_bar_index=0, exit_bar_index=1,
        )
        assert t.source == ""

    def test_trade_with_source(self):
        t = Trade(
            tag="L", side=OrderSide.LONG, qty=1,
            entry_price=20000, exit_price=20100,
            entry_bar_index=0, exit_bar_index=1,
            source="real",
        )
        assert t.source == "real"


class TestBrokerTradeSource:
    def test_trade_source_real_with_confirmed_fill(self):
        broker = SimulatedBroker(point_value=200)
        broker.trade_source = "real"
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        # Real broker confirmed the entry fill
        broker.real_entry_price = 20001
        broker.real_entry_dt = "2026-06-11 21:01:05"
        broker.queue_exit(Order(tag="Exit", side=OrderSide.LONG, from_entry="Long", limit=20100))
        broker.check_exits(1, 20050, 20150, 19950, 20100)
        assert len(broker.trades) == 1
        assert broker.trades[0].source == "real"

    def test_trade_source_real_without_fill_downgrades_to_paper(self):
        # semi_auto confirm declined/timed out, or auto order failed:
        # the trade never touched the real account → paper, not real.
        # (Regression: trade #65 of bot 0422 — REAL_ORDER_TIMEOUT on
        # entry, real_entry_price=0, yet tagged "real" by mode.)
        broker = SimulatedBroker(point_value=200)
        broker.trade_source = "real"
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.queue_exit(Order(tag="Exit", side=OrderSide.LONG, from_entry="Long", limit=20100))
        broker.check_exits(1, 20050, 20150, 19950, 20100)
        assert len(broker.trades) == 1
        assert broker.trades[0].source == "paper"
        assert broker.trades[0].real_entry_price == 0

    def test_trade_source_backtest_mode_not_downgraded(self):
        # backtest has real_entry_price=0 by definition — must NOT be
        # rewritten; the downgrade only applies to the "real" tag.
        broker = SimulatedBroker(point_value=200)
        broker.trade_source = "backtest"
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.queue_exit(Order(tag="Exit", side=OrderSide.LONG, from_entry="Long", limit=20100))
        broker.check_exits(1, 20050, 20150, 19950, 20100)
        assert broker.trades[0].source == "backtest"

    def test_trade_source_defaults_empty(self):
        broker = SimulatedBroker(point_value=200)
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.queue_exit(Order(tag="Exit", side=OrderSide.LONG, from_entry="Long", limit=20100))
        broker.check_exits(1, 20050, 20150, 19950, 20100)
        assert broker.trades[0].source == ""

    def test_has_open_position(self):
        broker = SimulatedBroker(point_value=200)
        assert not broker.has_open_position()
        broker.queue_entry(Order(tag="L", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        assert broker.has_open_position()


class TestTradeSerializationRoundTrip:
    def test_to_dict_includes_source(self):
        broker = SimulatedBroker(point_value=200)
        broker.trade_source = "paper"
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.queue_exit(Order(tag="Exit", side=OrderSide.LONG, from_entry="Long", limit=20100))
        broker.check_exits(1, 20050, 20150, 19950, 20100)

        data = broker.to_dict()
        assert data["trades"][0]["source"] == "paper"

    def test_from_dict_preserves_source(self):
        broker = SimulatedBroker(point_value=200)
        broker.trade_source = "real"
        broker.queue_entry(Order(tag="Long", side=OrderSide.LONG, qty=1))
        broker.on_bar_close(0, 20000)
        broker.real_entry_price = 20001  # confirmed fill → tag stays "real"
        broker.queue_exit(Order(tag="Exit", side=OrderSide.LONG, from_entry="Long", limit=20100))
        broker.check_exits(1, 20050, 20150, 19950, 20100)

        data = broker.to_dict()
        restored = SimulatedBroker.from_dict(data)
        assert restored.trades[0].source == "real"

    def test_legacy_session_without_source(self):
        data = {
            "point_value": 200,
            "trades": [{
                "tag": "L", "side": "LONG", "qty": 1,
                "entry_price": 20000, "exit_price": 20100,
                "entry_bar_index": 0, "exit_bar_index": 1,
                "pnl": 20000,
            }],
        }
        restored = SimulatedBroker.from_dict(data)
        assert restored.trades[0].source == ""


# ---------------------------------------------------------------------------
# Feature 2: Hot-Swap Mode Switching
# ---------------------------------------------------------------------------

class TestCheckModeOverride:
    def test_returns_none_when_file_absent(self, tmp_path):
        runner = LiveRunner(NeverTradeStrategy(), "TX00", log_dir=str(tmp_path))
        assert runner._check_mode_override() is None

    def test_reads_and_deletes_file(self, tmp_path):
        runner = LiveRunner(NeverTradeStrategy(), "TX00", log_dir=str(tmp_path))
        override_path = os.path.join(runner.bot_dir, "mode_override.json")
        os.makedirs(os.path.dirname(override_path), exist_ok=True)
        with open(override_path, "w") as f:
            json.dump({"trading_mode": "semi_auto"}, f)

        result = runner._check_mode_override()
        assert result == "semi_auto"
        assert not os.path.exists(override_path)

    def test_returns_none_on_invalid_json(self, tmp_path):
        runner = LiveRunner(NeverTradeStrategy(), "TX00", log_dir=str(tmp_path))
        override_path = os.path.join(runner.bot_dir, "mode_override.json")
        os.makedirs(os.path.dirname(override_path), exist_ok=True)
        with open(override_path, "w") as f:
            f.write("not json{{{")
        assert runner._check_mode_override() is None


class TestApplyModeSwitch:
    def _make_runner(self, tmp_path, mode="paper", allow_override=False):
        runner = LiveRunner(NeverTradeStrategy(), "TX00", log_dir=str(tmp_path))
        runner.trading_mode = mode
        runner.broker.trade_source = _mode_to_source(mode)
        runner._allow_live_override = allow_override
        return runner

    def test_noop_when_same_mode(self, tmp_path):
        runner = self._make_runner(tmp_path, "paper")
        runner._apply_mode_switch("paper")
        assert runner.trading_mode == "paper"

    def test_rejects_invalid_mode(self, tmp_path):
        runner = self._make_runner(tmp_path, "paper")
        statuses = []
        runner.on("on_status", lambda msg: statuses.append(msg))
        runner._apply_mode_switch("invalid_mode")
        assert runner.trading_mode == "paper"
        assert any("rejected invalid" in s for s in statuses)

    def test_rejects_auto_without_override(self, tmp_path):
        runner = self._make_runner(tmp_path, "paper", allow_override=False)
        statuses = []
        runner.on("on_status", lambda msg: statuses.append(msg))
        runner._apply_mode_switch("auto")
        assert runner.trading_mode == "paper"
        assert any("allow_live_override" in s for s in statuses)

    def test_allows_auto_with_override(self, tmp_path):
        runner = self._make_runner(tmp_path, "paper", allow_override=True)
        runner._apply_mode_switch("auto")
        assert runner.trading_mode == "auto"
        assert runner.broker.trade_source == "real"

    def test_allows_auto_with_user_confirmation(self, tmp_path):
        # GUI confirmation dialog bypasses the allow_live_override gate
        runner = self._make_runner(tmp_path, "semi_auto", allow_override=False)
        runner._apply_mode_switch("auto", user_confirmed=True)
        assert runner.trading_mode == "auto"
        assert runner.broker.trade_source == "real"

    def test_user_confirmed_false_still_gated(self, tmp_path):
        runner = self._make_runner(tmp_path, "semi_auto", allow_override=False)
        runner._apply_mode_switch("auto", user_confirmed=False)
        assert runner.trading_mode == "semi_auto"

    def test_rejects_when_open_position(self, tmp_path):
        runner = self._make_runner(tmp_path, "paper")
        runner.broker.queue_entry(Order(tag="L", side=OrderSide.LONG, qty=1))
        runner.broker.on_bar_close(0, 20000)
        statuses = []
        runner.on("on_status", lambda msg: statuses.append(msg))
        runner._apply_mode_switch("semi_auto")
        assert runner.trading_mode == "paper"
        assert any("open position" in s for s in statuses)

    def test_updates_broker_trade_source(self, tmp_path):
        runner = self._make_runner(tmp_path, "paper")
        runner._apply_mode_switch("semi_auto")
        assert runner.trading_mode == "semi_auto"
        assert runner.broker.trade_source == "real"

    def test_fires_on_mode_changed_callback(self, tmp_path):
        runner = self._make_runner(tmp_path, "paper")
        changes = []
        runner.on_mode_changed = lambda old, new: changes.append((old, new))
        runner._apply_mode_switch("semi_auto")
        assert changes == [("paper", "semi_auto")]

    def test_paper_to_semi_auto_switch(self, tmp_path):
        runner = self._make_runner(tmp_path, "paper")
        runner._apply_mode_switch("semi_auto")
        assert runner.trading_mode == "semi_auto"
        assert runner.broker.trade_source == "real"

    def test_semi_auto_to_paper_switch(self, tmp_path):
        runner = self._make_runner(tmp_path, "semi_auto")
        runner._apply_mode_switch("paper")
        assert runner.trading_mode == "paper"
        assert runner.broker.trade_source == "paper"

    def test_auto_to_semi_auto_switch(self, tmp_path):
        runner = self._make_runner(tmp_path, "auto")
        runner._apply_mode_switch("semi_auto")
        assert runner.trading_mode == "semi_auto"
        assert runner.broker.trade_source == "real"

    def test_auto_to_paper_switch(self, tmp_path):
        runner = self._make_runner(tmp_path, "auto")
        runner._apply_mode_switch("paper")
        assert runner.trading_mode == "paper"
        assert runner.broker.trade_source == "paper"

    def test_veto_blocks_switch(self, tmp_path):
        # Simulates the GUI's fill_pending guard: an exit order in
        # flight leaves position_size==0, so only the veto can block.
        runner = self._make_runner(tmp_path, "auto")
        runner.mode_switch_veto = lambda: "real order fill pending"
        statuses = []
        runner.on("on_status", lambda msg: statuses.append(msg))
        runner._apply_mode_switch("semi_auto")
        assert runner.trading_mode == "auto"
        assert runner.broker.trade_source == "real"
        assert any("fill pending" in s for s in statuses)

    def test_veto_none_allows_switch(self, tmp_path):
        runner = self._make_runner(tmp_path, "auto")
        runner.mode_switch_veto = lambda: None
        runner._apply_mode_switch("semi_auto")
        assert runner.trading_mode == "semi_auto"

    def test_veto_applies_to_file_override_path(self, tmp_path):
        runner = self._make_runner(tmp_path, "auto")
        runner.mode_switch_veto = lambda: "real order fill pending"
        override_path = os.path.join(runner.bot_dir, "mode_override.json")
        with open(override_path, "w") as f:
            json.dump({"trading_mode": "paper"}, f)
        new_mode = runner._check_mode_override()
        assert new_mode == "paper"
        runner._apply_mode_switch(new_mode)
        assert runner.trading_mode == "auto"  # vetoed


# ---------------------------------------------------------------------------
# Feature 3: Report Split by Source
# ---------------------------------------------------------------------------

class TestReportSourceSplit:
    def test_mixed_source_trades(self, tmp_path):
        from src.daily_report.report_generator import generate_daily_report
        trades = [
            Trade(tag="L1", side=OrderSide.LONG, qty=1,
                  entry_price=20000, exit_price=20100, entry_bar_index=0,
                  exit_bar_index=1, pnl=20000, exit_dt="2026-06-11 10:00",
                  source="paper"),
            Trade(tag="L2", side=OrderSide.LONG, qty=1,
                  entry_price=20000, exit_price=20200, entry_bar_index=2,
                  exit_bar_index=3, pnl=40000, exit_dt="2026-06-11 11:00",
                  source="real"),
            Trade(tag="L3", side=OrderSide.LONG, qty=1,
                  entry_price=20000, exit_price=19900, entry_bar_index=4,
                  exit_bar_index=5, pnl=-20000, exit_dt="2026-06-11 12:00",
                  source="paper"),
        ]
        report = generate_daily_report("2026-06-11", trades, save=False)
        # "paper" = full simulated view (ALL trades, including real-mirrored
        # ones); "real" = only the subset executed at the broker.
        assert len(report["paper_trades"]) == 3
        assert len(report["real_trades"]) == 1
        assert report["paper_summary"] is not None
        assert report["real_summary"] is not None
        # Original keys preserved
        assert len(report["trades"]) == 3
        assert report["summary"] is not None
        # paper view == full simulated view
        assert report["paper_summary"]["total_pnl"] == report["summary"]["total_pnl"]

    def test_all_paper_trades(self, tmp_path):
        from src.daily_report.report_generator import generate_daily_report
        trades = [
            Trade(tag="L1", side=OrderSide.LONG, qty=1,
                  entry_price=20000, exit_price=20100, entry_bar_index=0,
                  exit_bar_index=1, pnl=20000, exit_dt="2026-06-11 10:00",
                  source="paper"),
        ]
        report = generate_daily_report("2026-06-11", trades, save=False)
        assert len(report["paper_trades"]) == 1
        assert len(report["real_trades"]) == 0
        assert report["real_summary"] is None

    def test_backward_compat_no_source(self, tmp_path):
        from src.daily_report.report_generator import generate_daily_report
        trades = [
            Trade(tag="L1", side=OrderSide.LONG, qty=1,
                  entry_price=20000, exit_price=20100, entry_bar_index=0,
                  exit_bar_index=1, pnl=20000, exit_dt="2026-06-11 10:00"),
        ]
        report = generate_daily_report("2026-06-11", trades, save=False)
        assert len(report["paper_trades"]) == 1
        assert len(report["real_trades"]) == 0

    def test_all_real_session_paper_includes_everything(self, tmp_path):
        # Pure semi_auto/auto session: every trade tagged "real". The paper
        # (simulated) view must still cover all of them, not be empty.
        from src.daily_report.report_generator import generate_daily_report
        trades = [
            Trade(tag="L1", side=OrderSide.LONG, qty=1,
                  entry_price=20000, exit_price=20100, entry_bar_index=0,
                  exit_bar_index=1, pnl=20000, exit_dt="2026-06-11 10:00",
                  source="real"),
            Trade(tag="L2", side=OrderSide.LONG, qty=1,
                  entry_price=20100, exit_price=20050, entry_bar_index=2,
                  exit_bar_index=3, pnl=-10000, exit_dt="2026-06-11 11:00",
                  source="real"),
        ]
        report = generate_daily_report("2026-06-11", trades, save=False)
        assert len(report["paper_trades"]) == 2
        assert report["paper_summary"]["total_pnl"] == 10000
        assert len(report["real_trades"]) == 2
        assert report["real_summary"]["total_pnl"] == 10000

    def test_trade_to_dict_includes_source(self):
        from src.daily_report.report_generator import _trade_to_dict
        t = Trade(tag="L", side=OrderSide.LONG, qty=1,
                  entry_price=20000, exit_price=20100,
                  entry_bar_index=0, exit_bar_index=1, pnl=20000,
                  source="real")
        d = _trade_to_dict(t, point_value=200)
        assert d["source"] == "real"


class TestLiveRunnerSummary:
    def test_summary_splits_by_source(self, tmp_path):
        runner = LiveRunner(NeverTradeStrategy(), "TX00", log_dir=str(tmp_path))
        runner.broker.trade_source = "paper"
        runner.broker.trades.append(
            Trade(tag="L1", side=OrderSide.LONG, qty=1, entry_price=20000,
                  exit_price=20100, entry_bar_index=0, exit_bar_index=1,
                  pnl=20000, source="paper"))
        runner.broker.trades.append(
            Trade(tag="L2", side=OrderSide.LONG, qty=1, entry_price=20000,
                  exit_price=20200, entry_bar_index=2, exit_bar_index=3,
                  pnl=40000, source="real"))
        summary = runner._summary()
        assert summary["trades"] == 2
        # paper = full simulated view (all trades); real = real subset
        assert summary["paper_trades"] == 2
        assert summary["paper_pnl"] == 60000
        assert summary["real_trades"] == 1
        assert summary["real_pnl"] == 40000
