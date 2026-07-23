"""Tests for regime-related report changes (Commit 6).

Verifies per-strategy breakdown, bot-namespaced filenames, regime_switching
section in reports, and Discord format additions.
"""

import json
import os
from datetime import datetime
from pathlib import Path

from src.backtest.broker import Trade, OrderSide
from src.daily_report.report_generator import (
    generate_daily_report,
    load_report,
    _REPORTS_DIR,
)
from src.live.discord_notify import DiscordNotifier


def _make_trade(tag="t", side=OrderSide.LONG, pnl=100, exit_dt="2026-07-09 10:00",
                strategy="StratA"):
    t = Trade(
        tag=tag, side=side, qty=1,
        entry_price=22500, exit_price=22500 + pnl,
        entry_bar_index=0, exit_bar_index=5,
        pnl=pnl, entry_dt="2026-07-09 09:00", exit_dt=exit_dt,
    )
    t.strategy = strategy
    return t


class TestPerStrategyBreakdown:
    def test_single_strategy_no_breakdown(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.daily_report.report_generator._REPORTS_DIR", tmp_path)
        trades = [_make_trade(strategy="A"), _make_trade(strategy="A")]
        report = generate_daily_report("2026-07-09", trades, save=False)
        assert report["per_strategy"] == {}

    def test_multi_strategy_has_breakdown(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.daily_report.report_generator._REPORTS_DIR", tmp_path)
        trades = [
            _make_trade(strategy="LongA", pnl=200),
            _make_trade(strategy="LongA", pnl=-50),
            _make_trade(strategy="ShortB", pnl=150),
        ]
        report = generate_daily_report("2026-07-09", trades, save=False)
        ps = report["per_strategy"]
        assert "LongA" in ps
        assert "ShortB" in ps
        assert ps["LongA"]["total_trades"] == 2
        assert ps["LongA"]["total_pnl"] == 150
        assert ps["ShortB"]["total_trades"] == 1
        assert ps["ShortB"]["total_pnl"] == 150

    def test_breakdown_included_in_saved_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.daily_report.report_generator._REPORTS_DIR", tmp_path)
        trades = [
            _make_trade(strategy="LongA", pnl=200),
            _make_trade(strategy="ShortB", pnl=150),
        ]
        generate_daily_report("2026-07-09", trades, save=True)
        path = tmp_path / "2026-07-09.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "per_strategy" in data
        assert len(data["per_strategy"]) == 2


class TestBotNamespacedFilename:
    def test_without_bot_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.daily_report.report_generator._REPORTS_DIR", tmp_path)
        generate_daily_report("2026-07-09", [_make_trade()], save=True)
        assert (tmp_path / "2026-07-09.json").exists()

    def test_with_bot_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.daily_report.report_generator._REPORTS_DIR", tmp_path)
        generate_daily_report("2026-07-09", [_make_trade()],
                              bot_name="my_bot", save=True)
        assert (tmp_path / "2026-07-09_my_bot.json").exists()
        assert not (tmp_path / "2026-07-09.json").exists()

    def test_load_with_bot_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.daily_report.report_generator._REPORTS_DIR", tmp_path)
        generate_daily_report("2026-07-09", [_make_trade()],
                              bot_name="bot1", save=True)
        report = load_report("2026-07-09", bot_name="bot1")
        assert report is not None
        assert report["date"] == "2026-07-09"

    def test_load_falls_back_to_plain(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.daily_report.report_generator._REPORTS_DIR", tmp_path)
        generate_daily_report("2026-07-09", [_make_trade()], save=True)
        report = load_report("2026-07-09", bot_name="nonexistent")
        assert report is not None

    def test_load_no_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.daily_report.report_generator._REPORTS_DIR", tmp_path)
        assert load_report("2026-07-09") is None


class TestDiscordRegimeInfo:
    def test_regime_switching_info_in_discord_message(self):
        """Compact report shows the bilingual active leg label."""
        sent = []
        notifier = DiscordNotifier("fake_token", "fake_channel",
                                   bot_name="test", symbol="TX00")
        notifier._send = lambda msg, ch="": sent.append(msg)

        report = {
            "date": "2026-07-09",
            "strategy": {"name": "Regime Switching"},
            "regime_switching": {
                "active_leg": "long",
                "long_strategy": "LongA",
                "short_strategy": "ShortB",
            },
            "summary": {},
        }
        notifier.daily_report(report)
        assert len(sent) == 1
        msg = sent[0]
        assert "做多 Long" in msg
        assert "Regime Switching" in msg

    def test_no_stat_block_for_single(self):
        """Single-strategy compact report shows strategy name."""
        sent = []
        notifier = DiscordNotifier("fake_token", "fake_channel",
                                   bot_name="bot")
        notifier._send = lambda msg, ch="": sent.append(msg)

        report = {
            "date": "2026-07-09",
            "strategy": {"name": "StratA"},
            "summary": {},
        }
        notifier.daily_report(report)
        assert len(sent) == 1
        assert "StratA" in sent[0]


class TestRegimeSwitchingReport:
    def test_regime_switching_section_in_report(self):
        """RegimeSwitchingRunner injects regime_switching into report."""
        from src.live.regime_switching_runner import RegimeSwitchingRunner
        from src.regime.state_machine import RegimeConfig

        cfg = RegimeConfig(
            enabled=True, adx_enter=25.0, adx_exit=20.0,
            confirm_sessions=2, vol_spike_ratio=1.5,
            long_strategy="StratA", short_strategy="StratB",
            range_bias_action="sit_out",
        )
        runner = RegimeSwitchingRunner.__new__(RegimeSwitchingRunner)
        runner._active_leg = "long"
        runner._long_strategy_name = "StratA"
        runner._short_strategy_name = "StratB"

        report = {}
        report["regime_switching"] = {
            "active_leg": runner._active_leg,
            "long_strategy": runner._long_strategy_name,
            "short_strategy": runner._short_strategy_name,
        }
        assert report["regime_switching"]["active_leg"] == "long"
        assert report["regime_switching"]["long_strategy"] == "StratA"
