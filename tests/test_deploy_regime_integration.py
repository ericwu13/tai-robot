"""Tests for deploy dialog regime integration (Commit 5).

Verifies the duck-typing gates, resume detection, and evolution skip
that were added to run_backtest.py without requiring the Tk GUI.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from src.market_data.models import Bar
from src.backtest.broker import OrderSide
from src.backtest.strategy import BacktestStrategy
from src.live.live_runner import LiveRunner, LiveState
from src.live.regime_switching_runner import RegimeSwitchingRunner
from src.regime.state_machine import RegimeConfig


_TZ_TAIPEI = timezone(timedelta(hours=8))


class StrategyA(BacktestStrategy):
    name = "StrategyA"
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0:
            broker.entry("A_long", OrderSide.LONG)

    def required_bars(self):
        return 2


class StrategyB(BacktestStrategy):
    name = "StrategyB"
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        pass

    def required_bars(self):
        return 2


REGISTRY = {"StrategyA": StrategyA, "StrategyB": StrategyB}


def _make_cfg(**overrides):
    defaults = dict(
        enabled=True, adx_enter=25.0, adx_exit=20.0,
        confirm_sessions=2, vol_spike_ratio=1.5,
        long_strategy="StrategyA", short_strategy="StrategyB",
        range_bias_action="sit_out", classify_interval=3600,
    )
    defaults.update(overrides)
    return RegimeConfig(**defaults)


def _klines_15m(count, base_date="2026-07-09"):
    lines = []
    for i in range(count):
        h = 9 + (i * 15) // 60
        m = (i * 15) % 60
        dt = datetime.strptime(f"{base_date} {h:02d}:{m:02d}", "%Y-%m-%d %H:%M")
        price = 22500 + i * 10
        lines.append(f"{dt.strftime('%m/%d/%Y %H:%M')},{price},{price+10},{price-5},{price+3},{50+i}")
    return lines


def _make_bars(count=60):
    bars = []
    base = datetime(2026, 7, 1, 9, 0)
    for i in range(count * 60):
        dt = base + timedelta(minutes=i)
        price = 22000 + i
        bars.append(Bar("TX00", dt, price, price+10, price-5, price+5, 100, 60))
    return bars


def _make_regime_runner(tmp_path):
    cfg = _make_cfg()
    runner = RegimeSwitchingRunner(
        StrategyA(), "TX00", log_dir=str(tmp_path),
        bot_name="test_regime",
        regime_cfg=cfg,
        long_strategy_name="StrategyA",
        short_strategy_name="StrategyB",
        strategies_registry=REGISTRY,
        bars_provider=lambda: _make_bars(),
    )
    warmup = _klines_15m(20)
    runner.feed_warmup_bars(warmup)
    runner._is_reloading = False
    return runner


def _make_plain_runner(tmp_path):
    runner = LiveRunner(StrategyA(), "TX00", log_dir=str(tmp_path))
    warmup = _klines_15m(20)
    runner.feed_warmup_bars(warmup)
    runner._is_reloading = False
    return runner


# ── Duck-typing gates ──

class TestDuckTypingGates:
    """run_backtest.py uses hasattr(runner, 'on_status_poll') to detect regime."""

    def test_regime_runner_has_on_status_poll(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        assert hasattr(runner, 'on_status_poll')

    def test_plain_runner_lacks_on_status_poll(self, tmp_path):
        runner = _make_plain_runner(tmp_path)
        assert not hasattr(runner, 'on_status_poll')

    def test_regime_runner_has_get_regime_status(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        assert hasattr(runner, 'get_regime_status')

    def test_plain_runner_lacks_get_regime_status(self, tmp_path):
        runner = _make_plain_runner(tmp_path)
        assert not hasattr(runner, 'get_regime_status')


# ── Regime manager ownership ──

class TestRegimeManagerOwnership:
    """RegimeSwitchingRunner owns its RegimeManager; plain runner does not."""

    def test_regime_runner_owns_manager(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        assert runner.regime_manager is not None

    def test_regime_manager_accessible_via_property(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        mgr = runner.regime_manager
        assert hasattr(mgr, 'classify_session')
        assert hasattr(mgr, 'get_pending_recommendation')


# ── Session persistence and resume ──

class TestRegimeResume:
    """session.json with regime_mode=True triggers regime construction."""

    def test_session_json_has_regime_mode(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        runner._auto_save_session()
        with open(runner._session_path) as f:
            data = json.load(f)
        assert data["regime_mode"] is True
        assert data["active_leg"] == "idle"

    def test_plain_session_lacks_regime_mode(self, tmp_path):
        runner = _make_plain_runner(tmp_path)
        runner._auto_save_session()
        with open(runner._session_path) as f:
            data = json.load(f)
        assert "regime_mode" not in data

    def test_regime_mode_flag_survives_leg_change(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "deploy_long",
            "strategy": "StrategyA", "qty_scale": 1.0, "reason": "test",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        runner._maybe_apply_pending(now)
        runner._auto_save_session()
        with open(runner._session_path) as f:
            data = json.load(f)
        assert data["regime_mode"] is True
        assert data["active_leg"] == "long"


# ── Evolution skip ──

class TestEvolutionSkip:
    """Evolution should be skipped for regime switching runners."""

    def test_regime_runner_is_detected_by_hasattr(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        assert hasattr(runner, 'on_status_poll')

    def test_plain_runner_allows_evolution(self, tmp_path):
        runner = _make_plain_runner(tmp_path)
        assert not hasattr(runner, 'on_status_poll')


# ── Status poll ──

class TestStatusPoll:
    """on_status_poll returns list of status lines."""

    def test_poll_returns_list(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        now = datetime(2026, 7, 9, 12, 0, tzinfo=_TZ_TAIPEI)
        lines = runner.on_status_poll(now)
        assert isinstance(lines, list)

    def test_poll_in_day_session(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        now = datetime(2026, 7, 9, 10, 0, tzinfo=_TZ_TAIPEI)
        lines = runner.on_status_poll(now)
        assert not any("Classified" in l for l in lines)

    def test_poll_in_gap_with_pending(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "deploy_long",
            "strategy": "StrategyA", "qty_scale": 1.0, "reason": "test",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        lines = runner.on_status_poll(now)
        assert any("Applied" in l for l in lines)
        assert runner._pending_recommendation is None


# ── Regime status for GUI ──

class TestGetRegimeStatus:
    """get_regime_status provides data for the GUI status line."""

    def test_idle_status(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        status = runner.get_regime_status()
        assert status["active_leg"] == "idle"
        assert status["regime_idle"] is True
        assert status["long_strategy"] == "StrategyA"
        assert status["short_strategy"] == "StrategyB"

    def test_active_status_after_apply(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "deploy_long",
            "strategy": "StrategyA", "qty_scale": 1.0, "reason": "test",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        runner._maybe_apply_pending(now)
        status = runner.get_regime_status()
        assert status["active_leg"] == "long"
        assert status["regime_idle"] is False
        assert status["pending_recommendation"] is None


# ── Strategy var display ──

class TestDeployDisplayName:
    """Regime runner uses 'Regime 切換 Switching' as display name."""

    def test_regime_runner_display_name(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        assert runner.strategy_display_name == "Regime Switching"

    def test_regime_runner_custom_display_name(self, tmp_path):
        cfg = _make_cfg()
        runner = RegimeSwitchingRunner(
            StrategyA(), "TX00", log_dir=str(tmp_path),
            bot_name="test_regime",
            strategy_display_name="Regime 切換 Switching",
            regime_cfg=cfg,
            long_strategy_name="StrategyA",
            short_strategy_name="StrategyB",
            strategies_registry=REGISTRY,
            bars_provider=lambda: _make_bars(),
        )
        assert runner.strategy_display_name == "Regime 切換 Switching"

    def test_regime_runner_display_changes_on_swap(self, tmp_path):
        runner = _make_regime_runner(tmp_path)
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "deploy_long",
            "strategy": "StrategyA", "qty_scale": 1.0, "reason": "test",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        runner._maybe_apply_pending(now)
        assert runner.strategy_display_name == "StrategyA"
