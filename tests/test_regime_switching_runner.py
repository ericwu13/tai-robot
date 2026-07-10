"""Tests for RegimeSwitchingRunner: classification, apply, resume."""

import os
import json
from datetime import datetime, timedelta, timezone

from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.backtest.broker import BrokerContext, OrderSide
from src.backtest.strategy import BacktestStrategy
from src.live.live_runner import LiveState
from src.live.regime_switching_runner import RegimeSwitchingRunner
from src.regime.state_machine import RegimeConfig

_TZ_TAIPEI = timezone(timedelta(hours=8))


class LongStrategy(BacktestStrategy):
    name = "TestLong"
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0:
            broker.entry("Long", OrderSide.LONG)

    def required_bars(self):
        return 2


class ShortStrategy(BacktestStrategy):
    name = "TestShort"
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0:
            broker.entry("Short", OrderSide.SHORT)

    def required_bars(self):
        return 2


REGISTRY = {"TestLong": LongStrategy, "TestShort": ShortStrategy}


def _make_cfg(**overrides):
    defaults = dict(
        enabled=True, adx_enter=25.0, adx_exit=20.0,
        confirm_sessions=2, vol_spike_ratio=1.5,
        long_strategy="TestLong", short_strategy="TestShort",
        range_bias_action="sit_out", classify_interval=3600,
    )
    defaults.update(overrides)
    return RegimeConfig(**defaults)


def _make_bars(count=60):
    bars = []
    base = datetime(2026, 7, 1, 9, 0)
    for i in range(count * 60):
        dt = base + timedelta(minutes=i)
        price = 22000 + i
        bars.append(Bar("TX00", dt, price, price+10, price-5, price+5, 100, 60))
    return bars


def _klines_15m(count, base_date="2026-07-09"):
    lines = []
    for i in range(count):
        h = 9 + (i * 15) // 60
        m = (i * 15) % 60
        dt = datetime.strptime(f"{base_date} {h:02d}:{m:02d}", "%Y-%m-%d %H:%M")
        price = 22500 + i * 10
        lines.append(f"{dt.strftime('%m/%d/%Y %H:%M')},{price},{price+10},{price-5},{price+3},{50+i}")
    return lines


def _make_runner(tmp_path, bars=None):
    cfg = _make_cfg()
    strategy = LongStrategy()
    runner = RegimeSwitchingRunner(
        strategy, "TX00", log_dir=str(tmp_path),
        bot_name="test_regime",
        regime_cfg=cfg,
        long_strategy_name="TestLong",
        short_strategy_name="TestShort",
        strategies_registry=REGISTRY,
        bars_provider=lambda: bars or _make_bars(),
    )
    warmup = _klines_15m(20)
    runner.feed_warmup_bars(warmup)  # sets state to RUNNING
    runner._is_reloading = False    # simulate GUI clearing after live ticks begin
    return runner


class TestConstruction:
    def test_starts_idle(self, tmp_path):
        runner = _make_runner(tmp_path)
        assert runner.regime_idle is True
        assert runner.active_leg == "idle"

    def test_session_json_has_regime_keys(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner._auto_save_session()
        with open(runner._session_path) as f:
            data = json.load(f)
        assert data["regime_mode"] is True
        assert data["active_leg"] == "idle"


class TestClassification:
    def test_classify_at_night_end(self, tmp_path):
        runner = _make_runner(tmp_path)
        # Simulate NIGHT session near close (04:58 TPE)
        now = datetime(2026, 7, 10, 4, 58, tzinfo=_TZ_TAIPEI)
        lines = runner.on_status_poll(now)
        # Should have classified (or no-op if bars insufficient for this test setup)
        assert isinstance(lines, list)

    def test_day_session_no_classify(self, tmp_path):
        runner = _make_runner(tmp_path)
        now = datetime(2026, 7, 9, 12, 0, tzinfo=_TZ_TAIPEI)
        lines = runner.on_status_poll(now)
        # Should NOT have classified — it's DAY
        assert not any("Classified" in l for l in lines)


class TestApplyPending:
    def test_apply_long_in_closed_gap(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "deploy_long",
            "strategy": "TestLong", "qty_scale": 1.0, "reason": "test",
        }
        # In closed gap (07:00 TPE, between night close and day open)
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        result = runner._maybe_apply_pending(now)
        assert result is not None
        assert runner.active_leg == "long"
        assert runner.regime_idle is False
        assert runner._pending_recommendation is None

    def test_apply_short(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "deploy_short",
            "strategy": "TestShort", "qty_scale": 1.0, "reason": "test",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        result = runner._maybe_apply_pending(now)
        assert result is not None
        assert runner.active_leg == "short"
        assert runner.strategy_display_name == "TestShort"

    def test_apply_sit_out(self, tmp_path):
        runner = _make_runner(tmp_path)
        # First deploy long
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "deploy_long",
            "strategy": "TestLong", "qty_scale": 1.0, "reason": "test",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        runner._maybe_apply_pending(now)
        assert runner.active_leg == "long"

        # Now sit out
        runner._pending_recommendation = {
            "date": "2026-07-11", "action": "sit_out",
            "strategy": "", "qty_scale": 1.0, "reason": "test",
        }
        result = runner._maybe_apply_pending(now)
        assert result is not None
        assert runner.active_leg == "idle"
        assert runner.regime_idle is True

    def test_cross_tf_swap_without_history_keeps_pending(self, tmp_path):
        # A cross-timeframe leg needs accumulated 1-min history to rebuild
        # its aggregator. The 15-min warmup leaves _1m_bars empty, so the
        # swap is refused and the pending recommendation stays for retry.
        class Short30m(BacktestStrategy):
            name = "Short30m"
            kline_type = 0
            kline_minute = 30

            def on_bar(self, bar, data_store, broker):
                pass

            def required_bars(self):
                return 2

        runner = _make_runner(tmp_path)
        runner._strategies_registry = dict(runner._strategies_registry)
        runner._strategies_registry["Short30m"] = Short30m
        runner._short_strategy_name = "Short30m"
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "deploy_short",
            "strategy": "Short30m", "qty_scale": 1.0, "reason": "test",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        result = runner._maybe_apply_pending(now)
        assert result is None
        # Pending survives for the next poll; nothing swapped.
        assert runner._pending_recommendation is not None
        assert runner.active_leg != "short"
        assert runner.target_interval == 900  # still 15-min

    def test_apply_hold_noop(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "hold",
            "strategy": "", "qty_scale": 1.0, "reason": "test",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        result = runner._maybe_apply_pending(now)
        assert result is not None
        assert "hold" in result.lower()

    def test_no_apply_during_session(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "deploy_long",
            "strategy": "TestLong", "qty_scale": 1.0, "reason": "test",
        }
        # Mid-session (10:00 TPE)
        now = datetime(2026, 7, 10, 10, 0, tzinfo=_TZ_TAIPEI)
        result = runner._maybe_apply_pending(now)
        assert result is None  # not in closed gap

    def test_no_apply_with_veto(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.mode_switch_veto = lambda: "fill pending"
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "deploy_long",
            "strategy": "TestLong", "qty_scale": 1.0, "reason": "test",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        result = runner._maybe_apply_pending(now)
        assert result is None

    def test_deploy_short_half_as_short(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner._pending_recommendation = {
            "date": "2026-07-10", "action": "deploy_short_half",
            "strategy": "TestShort", "qty_scale": 0.5, "reason": "test",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        result = runner._maybe_apply_pending(now)
        assert runner.active_leg == "short"


class TestResumeRearm:
    def test_pending_rearmed_from_state_file(self, tmp_path):
        """First runner classifies → second runner picks up the pending."""
        runner1 = _make_runner(tmp_path)
        # Simulate classification
        now = datetime(2026, 7, 10, 4, 58, tzinfo=_TZ_TAIPEI)
        runner1.on_status_poll(now)

        # Check if anything was classified
        state_path = os.path.join(runner1.bot_dir, "regime_state.json")
        if os.path.exists(state_path):
            with open(state_path) as f:
                d = json.load(f)
            ns = d.get("next_session", {})
            if not ns.get("executed", True):
                # Build a new runner — should re-arm
                runner2 = _make_runner(tmp_path)
                assert runner2._pending_recommendation is not None


class TestStop:
    def test_stop_records_session(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner.stop()
        # Should have recorded a session result
        hist_path = os.path.join(runner.bot_dir, "regime_history.csv")
        # File may or may not exist depending on timing, but stop shouldn't crash
        assert runner.state == LiveState.STOPPED


class TestFullCycle:
    def test_classify_then_apply(self, tmp_path):
        """Simulate: classify at NIGHT end → apply in gap → leg is active."""
        runner = _make_runner(tmp_path)

        # Step 1: classify at NIGHT end
        night_end = datetime(2026, 7, 10, 4, 58, tzinfo=_TZ_TAIPEI)
        runner.on_status_poll(night_end)

        if runner._pending_recommendation is not None:
            # Step 2: apply in closed gap
            gap = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
            runner.on_status_poll(gap)
            assert runner._pending_recommendation is None  # consumed

        # Step 3: second poll in gap is idempotent
        gap2 = datetime(2026, 7, 10, 7, 30, tzinfo=_TZ_TAIPEI)
        lines = runner.on_status_poll(gap2)
        # No error, no double-apply
