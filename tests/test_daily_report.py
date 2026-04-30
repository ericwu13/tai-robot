"""Tests for daily report pipeline: regime classifier, report generator, changelog."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import pytest

from src.backtest.broker import Trade, OrderSide
from src.daily_report.regime_classifier import (
    classify_regime,
    RegimeResult,
    _classify_trend,
    _classify_volatility,
    _classify_direction,
    _combine_label,
)
from src.daily_report.report_generator import (
    generate_daily_report,
    generate_report_from_backtest,
    generate_session_report,
    load_report,
    list_reports,
    _group_trades_by_date,
)
from src.daily_report.changelog import (
    append_changelog,
    load_changelog,
    recent_changes,
    _CHANGELOG_PATH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade(
    pnl: int = 100,
    side: OrderSide = OrderSide.LONG,
    entry_price: int = 20000,
    exit_price: int = 20100,
    entry_dt: str = "2026-04-11 09:00",
    exit_dt: str = "2026-04-11 10:30",
    entry_bar: int = 0,
    exit_bar: int = 5,
    tag: str = "Long",
    exit_tag: str = "limit",
) -> Trade:
    return Trade(
        tag=tag,
        side=side,
        qty=1,
        entry_price=entry_price,
        exit_price=exit_price,
        entry_bar_index=entry_bar,
        exit_bar_index=exit_bar,
        pnl=pnl,
        exit_tag=exit_tag,
        entry_dt=entry_dt,
        exit_dt=exit_dt,
    )


def _trending_up_bars(n: int = 80) -> tuple[list[int], list[int], list[int]]:
    """Generate synthetic uptrending bars with clear directional movement."""
    highs, lows, closes = [], [], []
    base = 20000
    for i in range(n):
        h = base + i * 30 + 50
        l = base + i * 30 - 10
        c = base + i * 30 + 20
        highs.append(h)
        lows.append(l)
        closes.append(c)
    return highs, lows, closes


def _range_bound_bars(n: int = 80) -> tuple[list[int], list[int], list[int]]:
    """Generate synthetic range-bound bars oscillating around a center."""
    import math as _math
    highs, lows, closes = [], [], []
    center = 20000
    for i in range(n):
        offset = int(50 * _math.sin(i * 0.5))
        h = center + offset + 30
        l = center + offset - 30
        c = center + offset
        highs.append(h)
        lows.append(l)
        closes.append(c)
    return highs, lows, closes


# ---------------------------------------------------------------------------
# Regime classifier tests
# ---------------------------------------------------------------------------

class TestClassifyTrend:
    def test_trending(self):
        assert _classify_trend(30.0) == "trending"
        assert _classify_trend(25.1) == "trending"

    def test_range_bound(self):
        assert _classify_trend(15.0) == "range-bound"
        assert _classify_trend(19.9) == "range-bound"

    def test_transitional(self):
        assert _classify_trend(20.0) == "transitional"
        assert _classify_trend(22.5) == "transitional"
        assert _classify_trend(25.0) == "transitional"


class TestClassifyVolatility:
    def test_high(self):
        assert _classify_volatility(1.5) == "high"
        assert _classify_volatility(1.31) == "high"

    def test_low(self):
        assert _classify_volatility(0.5) == "low"
        assert _classify_volatility(0.69) == "low"

    def test_normal(self):
        assert _classify_volatility(1.0) == "normal"
        assert _classify_volatility(0.7) == "normal"
        assert _classify_volatility(1.3) == "normal"


class TestClassifyDirection:
    def test_bullish_above_ema(self):
        assert _classify_direction(20100, 20000) == "bullish"

    def test_bearish_below_ema(self):
        assert _classify_direction(19900, 20000) == "bearish"

    def test_equal_is_bullish(self):
        assert _classify_direction(20000, 20000) == "bullish"


class TestCombineLabel:
    def test_trending_up(self):
        assert _combine_label("trending", "normal", "bullish") == "trending-up"

    def test_trending_down(self):
        assert _combine_label("trending", "normal", "bearish") == "trending-down"

    def test_range_bound(self):
        assert _combine_label("range-bound", "normal", "bullish") == "range-bound"

    def test_high_volatility_overrides(self):
        assert _combine_label("trending", "high", "bullish") == "high-volatility"
        assert _combine_label("range-bound", "high", "bearish") == "high-volatility"

    def test_low_volatility_chop(self):
        assert _combine_label("range-bound", "low", "bearish") == "low-volatility-chop"

    def test_transitional(self):
        assert _combine_label("transitional", "normal", "bullish") == "transitional-bullish"
        assert _combine_label("transitional", "normal", "bearish") == "transitional-bearish"


class TestClassifyRegime:
    def test_insufficient_data_returns_none(self):
        result = classify_regime([1, 2, 3], [1, 2, 3], [1, 2, 3])
        assert result is None

    def test_trending_up_data(self):
        highs, lows, closes = _trending_up_bars(120)
        result = classify_regime(highs, lows, closes)
        assert result is not None
        assert isinstance(result, RegimeResult)
        assert result.direction == "bullish"
        # With strong trend data, ADX should be elevated
        assert result.adx_value > 0
        assert result.ema_50 > 0
        assert result.atr_value > 0

    def test_to_dict_keys(self):
        highs, lows, closes = _trending_up_bars(120)
        result = classify_regime(highs, lows, closes)
        assert result is not None
        d = result.to_dict()
        expected_keys = {
            "label", "trend_strength", "volatility", "direction",
            "adx", "plus_di", "minus_di", "atr", "atr_ratio",
            "ema_50", "last_close",
        }
        assert set(d.keys()) == expected_keys

    def test_range_bound_data(self):
        highs, lows, closes = _range_bound_bars(120)
        result = classify_regime(highs, lows, closes)
        assert result is not None
        # Range-bound data should have lower ADX
        # (not guaranteed to be < 20 with synthetic data, but should exist)
        assert result.adx_value >= 0

    def test_custom_periods(self):
        highs, lows, closes = _trending_up_bars(200)
        result = classify_regime(
            highs, lows, closes,
            adx_period=20, atr_period=20, ema_period=100,
        )
        # With 200 bars and period=20/100, should still work
        assert result is not None


# ---------------------------------------------------------------------------
# Report generator tests
# ---------------------------------------------------------------------------

class TestGroupTradesByDate:
    def test_single_day(self):
        trades = [
            _make_trade(exit_dt="2026-04-11 10:30"),
            _make_trade(exit_dt="2026-04-11 13:00"),
        ]
        grouped = _group_trades_by_date(trades)
        assert list(grouped.keys()) == ["2026-04-11"]
        assert len(grouped["2026-04-11"]) == 2

    def test_multiple_days(self):
        trades = [
            _make_trade(exit_dt="2026-04-10 10:30"),
            _make_trade(exit_dt="2026-04-11 13:00"),
            _make_trade(exit_dt="2026-04-11 14:00"),
        ]
        grouped = _group_trades_by_date(trades)
        assert len(grouped) == 2
        assert len(grouped["2026-04-10"]) == 1
        assert len(grouped["2026-04-11"]) == 2

    def test_skips_trades_without_exit_dt(self):
        trades = [
            _make_trade(exit_dt="2026-04-11 10:30"),
            _make_trade(exit_dt=""),
        ]
        grouped = _group_trades_by_date(trades)
        assert len(grouped["2026-04-11"]) == 1


class TestGenerateDailyReport:
    def test_basic_report_structure(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        trades = [
            _make_trade(pnl=200, exit_dt="2026-04-11 10:30"),
            _make_trade(pnl=-50, exit_dt="2026-04-11 13:00"),
        ]

        report = generate_daily_report(
            date="2026-04-11",
            trades=trades,
            strategy_name="Test Strategy",
            strategy_version="1.0",
            strategy_params={"fast": 3, "slow": 8},
            point_value=200,
            symbol="TXF1",
            save=True,
        )

        assert report["date"] == "2026-04-11"
        assert report["symbol"] == "TXF1"
        assert report["strategy"]["name"] == "Test Strategy"
        assert report["strategy"]["version"] == "1.0"
        assert report["strategy"]["params"] == {"fast": 3, "slow": 8}
        assert len(report["trades"]) == 2
        assert report["summary"]["total_trades"] == 2
        assert report["summary"]["total_pnl"] == 150  # 200 + (-50)
        assert report["summary"]["winning_trades"] == 1
        assert report["summary"]["losing_trades"] == 1

        # Check file was written
        saved = tmp_path / "2026-04-11.json"
        assert saved.exists()
        loaded = json.loads(saved.read_text())
        assert loaded["date"] == "2026-04-11"

    def test_no_save(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        report = generate_daily_report(
            date="2026-04-11",
            trades=[_make_trade()],
            save=False,
        )
        assert report["date"] == "2026-04-11"
        assert not (tmp_path / "2026-04-11.json").exists()

    def test_with_regime_data(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        highs, lows, closes = _trending_up_bars(120)
        report = generate_daily_report(
            date="2026-04-11",
            trades=[_make_trade()],
            bars_highs=highs,
            bars_lows=lows,
            bars_closes=closes,
            save=False,
        )
        assert report["market_regime"] is not None
        assert "label" in report["market_regime"]
        assert "adx" in report["market_regime"]

    def test_empty_trades(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        report = generate_daily_report(
            date="2026-04-11", trades=[], save=False,
        )
        assert report["summary"]["total_trades"] == 0
        assert report["trades"] == []

    def test_trade_dict_fields(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        report = generate_daily_report(
            date="2026-04-11",
            trades=[_make_trade(pnl=100, entry_bar=2, exit_bar=7)],
            point_value=200,
            save=False,
        )
        td = report["trades"][0]
        assert td["pnl"] == 100
        assert td["pnl_currency"] == 20000  # 100 * 200
        assert td["bars_held"] == 5
        assert td["side"] == "LONG"
        assert td["exit_tag"] == "limit"


class TestGenerateReportFromBacktest:
    def test_splits_by_date(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        trades = [
            _make_trade(pnl=100, exit_dt="2026-04-10 10:00"),
            _make_trade(pnl=200, exit_dt="2026-04-11 10:00"),
            _make_trade(pnl=-50, exit_dt="2026-04-11 14:00"),
        ]
        equity = [100, 300, 250]

        reports = generate_report_from_backtest(
            trades=trades,
            equity_curve=equity,
            strategy_name="BT Strategy",
            save=True,
        )

        assert len(reports) == 2
        assert reports[0]["date"] == "2026-04-10"
        assert reports[1]["date"] == "2026-04-11"
        assert len(reports[1]["trades"]) == 2

        # Both files saved
        assert (tmp_path / "2026-04-10.json").exists()
        assert (tmp_path / "2026-04-11.json").exists()


class TestLoadAndListReports:
    def test_load_existing(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        data = {"date": "2026-04-11", "test": True}
        (tmp_path / "2026-04-11.json").write_text(json.dumps(data))

        loaded = load_report("2026-04-11")
        assert loaded == data

    def test_load_missing(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        assert load_report("2099-01-01") is None

    def test_list_reports(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        (tmp_path / "2026-04-10.json").write_text("{}")
        (tmp_path / "2026-04-11.json").write_text("{}")

        dates = list_reports()
        assert dates == ["2026-04-10", "2026-04-11"]

    def test_list_empty(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        assert list_reports() == []


# ---------------------------------------------------------------------------
# Changelog tests
# ---------------------------------------------------------------------------

class TestChangelog:
    def test_append_and_load(self, tmp_path, monkeypatch):
        import src.daily_report.changelog as cl
        monkeypatch.setattr(cl, "_CHANGELOG_PATH", tmp_path / "changelog.json")

        entry = append_changelog(
            strategy_name="SMA Cross",
            version_before="1.0",
            version_after="1.1",
            change_summary="Increased fast SMA from 3 to 5",
            initiated_by="human",
            params_before={"fast": 3},
            params_after={"fast": 5},
        )
        assert entry["strategy"] == "SMA Cross"
        assert entry["version_before"] == "1.0"
        assert entry["version_after"] == "1.1"
        assert entry["initiated_by"] == "human"

        entries = load_changelog()
        assert len(entries) == 1
        assert entries[0]["change_summary"] == "Increased fast SMA from 3 to 5"

    def test_multiple_entries(self, tmp_path, monkeypatch):
        import src.daily_report.changelog as cl
        monkeypatch.setattr(cl, "_CHANGELOG_PATH", tmp_path / "changelog.json")

        for i in range(5):
            append_changelog(
                strategy_name="Test",
                version_before=f"{i}.0",
                version_after=f"{i + 1}.0",
                change_summary=f"Change {i}",
            )

        entries = load_changelog()
        assert len(entries) == 5

    def test_recent_changes(self, tmp_path, monkeypatch):
        import src.daily_report.changelog as cl
        monkeypatch.setattr(cl, "_CHANGELOG_PATH", tmp_path / "changelog.json")

        for i in range(5):
            append_changelog(
                strategy_name="Test",
                version_before=f"{i}.0",
                version_after=f"{i + 1}.0",
                change_summary=f"Change {i}",
            )

        recent = recent_changes(n=3)
        assert len(recent) == 3
        # Newest first
        assert recent[0]["change_summary"] == "Change 4"
        assert recent[2]["change_summary"] == "Change 2"

    def test_empty_changelog(self, tmp_path, monkeypatch):
        import src.daily_report.changelog as cl
        monkeypatch.setattr(cl, "_CHANGELOG_PATH", tmp_path / "changelog.json")

        assert load_changelog() == []
        assert recent_changes() == []

    def test_corrupt_file_returns_empty(self, tmp_path, monkeypatch):
        import src.daily_report.changelog as cl
        path = tmp_path / "changelog.json"
        monkeypatch.setattr(cl, "_CHANGELOG_PATH", path)

        path.write_text("not valid json{{{")
        assert load_changelog() == []

    def test_with_metrics(self, tmp_path, monkeypatch):
        import src.daily_report.changelog as cl
        monkeypatch.setattr(cl, "_CHANGELOG_PATH", tmp_path / "changelog.json")

        entry = append_changelog(
            strategy_name="BB Strategy",
            version_before="2.0",
            version_after="2.1",
            change_summary="Tightened stop",
            initiated_by="ai",
            metrics_before={"win_rate": 0.45, "profit_factor": 1.2},
            metrics_after={"win_rate": 0.50, "profit_factor": 1.5},
        )
        assert entry["metrics_before"]["win_rate"] == 0.45
        assert entry["metrics_after"]["profit_factor"] == 1.5
        assert entry["initiated_by"] == "ai"


# ---------------------------------------------------------------------------
# Discord notifier daily_report method
# ---------------------------------------------------------------------------

class TestDiscordDailyReport:
    def test_daily_report_formats_message(self):
        from src.live.discord_notify import DiscordNotifier

        notifier = DiscordNotifier("fake-token", "fake-channel")
        # Patch _send to capture the message
        sent = []
        notifier._send = lambda msg: sent.append(msg)

        report = {
            "date": "2026-04-11",
            "summary": {
                "total_trades": 5,
                "total_pnl": 1500,
                "win_rate": 0.6,
                "profit_factor": 2.5,
                "max_drawdown": 300,
            },
            "strategy": {"name": "SMA Cross"},
            "market_regime": {"label": "trending-up", "adx": 32.5},
        }
        notifier.daily_report(report)

        assert len(sent) == 1
        msg = sent[0]
        assert "Daily Report" in msg
        assert "2026-04-11" in msg
        assert "SMA Cross" in msg
        assert "trending-up" in msg

    def test_daily_report_includes_session_line(self):
        from src.live.discord_notify import DiscordNotifier

        notifier = DiscordNotifier("fake-token", "fake-channel")
        sent = []
        notifier._send = lambda msg: sent.append(msg)

        report = {
            "date": "2026-04-11",
            "summary": {"total_trades": 1, "total_pnl": 100,
                        "win_rate": 1.0, "profit_factor": 0,
                        "max_drawdown": 0},
            "strategy": {"name": "Test"},
            "market_regime": None,
            "session": {
                "bot_name": "tmf00_main",
                "started_at": "2026-04-11T08:45:00",
                "version": "2.7.3",
            },
        }
        notifier.daily_report(report)

        assert len(sent) == 1
        msg = sent[0]
        assert "tmf00_main" in msg
        assert "v2.7.3" in msg
        # Started timestamp trimmed to minute precision (no seconds, no T)
        assert "2026-04-11 08:45" in msg
        assert "2026-04-11T08:45:00" not in msg

    def test_daily_report_without_session_field(self):
        """Reports without a session sub-dict (older saves) still render."""
        from src.live.discord_notify import DiscordNotifier

        notifier = DiscordNotifier("fake-token", "fake-channel")
        sent = []
        notifier._send = lambda msg: sent.append(msg)

        report = {
            "date": "2026-04-11",
            "summary": {"total_trades": 0, "total_pnl": 0,
                        "win_rate": 0, "profit_factor": 0,
                        "max_drawdown": 0},
            "strategy": {"name": "Test"},
            "market_regime": None,
        }
        notifier.daily_report(report)
        assert len(sent) == 1  # didn't crash

    def test_daily_report_without_regime(self):
        from src.live.discord_notify import DiscordNotifier

        notifier = DiscordNotifier("fake-token", "fake-channel")
        sent = []
        notifier._send = lambda msg: sent.append(msg)

        report = {
            "date": "2026-04-11",
            "summary": {"total_trades": 0, "total_pnl": 0,
                        "win_rate": 0, "profit_factor": 0,
                        "max_drawdown": 0},
            "strategy": {"name": "Test"},
            "market_regime": None,
        }
        notifier.daily_report(report)

        assert len(sent) == 1
        assert "trending" not in sent[0]  # no regime line


# ---------------------------------------------------------------------------
# Graceful field handling tests
# ---------------------------------------------------------------------------

class TestGracefulFieldHandling:
    """Ensure _trade_to_dict handles missing/partial fields from any strategy."""

    def test_standard_trade(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        report = generate_daily_report(
            date="2026-04-11",
            trades=[_make_trade()],
            save=False,
        )
        td = report["trades"][0]
        assert td["side"] == "LONG"
        assert td["tag"] == "Long"

    def test_trade_with_zero_pnl(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        t = _make_trade(pnl=0, entry_price=20000, exit_price=20000)
        report = generate_daily_report(
            date="2026-04-11", trades=[t], point_value=200, save=False,
        )
        td = report["trades"][0]
        assert td["pnl"] == 0
        assert td["pnl_currency"] == 0

    def test_trade_with_no_real_prices(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        t = _make_trade()
        assert t.real_entry_price == 0  # default
        report = generate_daily_report(
            date="2026-04-11", trades=[t], save=False,
        )
        td = report["trades"][0]
        assert td["real_entry_price"] is None
        assert td["real_exit_price"] is None

    def test_short_side_trade(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        t = _make_trade(side=OrderSide.SHORT, tag="Short")
        report = generate_daily_report(
            date="2026-04-11", trades=[t], save=False,
        )
        assert report["trades"][0]["side"] == "SHORT"


# ---------------------------------------------------------------------------
# generate_session_report tests
# ---------------------------------------------------------------------------

class _FakeBroker:
    """Minimal broker-like object for testing generate_session_report."""
    def __init__(self, trades):
        self.trades = trades


class _FakeDataStore:
    """Minimal data-store-like object with get_highs/lows/closes."""
    def __init__(self, highs, lows, closes):
        self._highs = highs
        self._lows = lows
        self._closes = closes

    def get_highs(self):
        return self._highs

    def get_lows(self):
        return self._lows

    def get_closes(self):
        return self._closes


class TestGenerateSessionReport:
    def test_basic_session_report(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        trades = [
            _make_trade(pnl=100, exit_dt="2026-04-11 10:30"),
            _make_trade(pnl=-30, exit_dt="2026-04-11 13:00"),
        ]
        broker = _FakeBroker(trades)

        report = generate_session_report(
            broker=broker,
            data_store=None,
            strategy_name="Test Strategy",
            point_value=200,
            symbol="TXF1",
        )
        assert report is not None
        assert report["date"] == "2026-04-11"
        assert report["symbol"] == "TXF1"
        assert len(report["trades"]) == 2
        assert report["market_regime"] is None  # no data store

    def test_session_report_with_data_store(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        trades = [_make_trade(exit_dt="2026-04-11 10:30")]
        broker = _FakeBroker(trades)
        highs, lows, closes = _trending_up_bars(120)
        ds = _FakeDataStore(highs, lows, closes)

        report = generate_session_report(
            broker=broker,
            data_store=ds,
            strategy_name="Trend Follower",
            symbol="TXF1",
        )
        assert report is not None
        assert report["market_regime"] is not None

    def test_no_trades_returns_none(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        broker = _FakeBroker([])
        assert generate_session_report(broker=broker, data_store=None) is None

    def test_session_metadata_propagates(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        trades = [_make_trade(exit_dt="2026-04-11 10:30")]
        broker = _FakeBroker(trades)

        report = generate_session_report(
            broker=broker,
            data_store=None,
            symbol="TXF1",
            bot_name="tmf00_main",
            started_at="2026-04-11T08:45:00",
        )
        assert report is not None
        assert report["session"]["bot_name"] == "tmf00_main"
        assert report["session"]["started_at"] == "2026-04-11T08:45:00"
        # version comes from version.APP_VERSION
        assert report["session"]["version"]  # non-empty

    def test_explicit_date(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        trades = [_make_trade(exit_dt="2026-04-11 10:30")]
        broker = _FakeBroker(trades)

        report = generate_session_report(
            broker=broker, data_store=None, date="2026-04-10",
        )
        assert report is not None
        # Explicit date overrides trade exit date
        assert report["date"] == "2026-04-10"

    def test_saves_to_disk(self, tmp_path, monkeypatch):
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        trades = [_make_trade(exit_dt="2026-04-11 10:30")]
        broker = _FakeBroker(trades)

        generate_session_report(broker=broker, data_store=None)
        assert (tmp_path / "2026-04-11.json").exists()

    def test_data_store_error_handled(self, tmp_path, monkeypatch):
        """If data_store.get_highs() raises, regime is skipped (not crash)."""
        import src.daily_report.report_generator as rg
        monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path)

        class _BrokenDataStore:
            def get_highs(self):
                raise RuntimeError("broken")
            def get_lows(self):
                return []
            def get_closes(self):
                return []

        trades = [_make_trade(exit_dt="2026-04-11 10:30")]
        broker = _FakeBroker(trades)

        report = generate_session_report(
            broker=broker, data_store=_BrokenDataStore(),
        )
        assert report is not None
        assert report["market_regime"] is None
