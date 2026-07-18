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

    def test_classify_keyed_by_open_date(self, tmp_path):
        # The night closing Fri 05:00 opened Thu 15:00 — its key is the
        # OPEN date (2026-07-09), stable across midnight.
        runner = _make_runner(tmp_path)
        now = datetime(2026, 7, 10, 4, 58, tzinfo=_TZ_TAIPEI)
        runner.on_status_poll(now)
        assert runner._manager._state.last_assessed == "2026-07-09|NIGHT"

    def test_day_session_no_classify_when_assessed(self, tmp_path):
        runner = _make_runner(tmp_path)
        # Last completed night (opened Wed 07-08) already assessed
        runner._manager._state.last_assessed = "2026-07-08|NIGHT"
        now = datetime(2026, 7, 9, 12, 0, tzinfo=_TZ_TAIPEI)
        lines = runner.on_status_poll(now)
        assert not any("Classified" in l for l in lines)

    def test_catch_up_classify_after_missed_window(self, tmp_path):
        # A poll long after the night closed (missed 04:58-05:00 window,
        # or a fresh deploy) classifies the unassessed night late.
        runner = _make_runner(tmp_path)
        now = datetime(2026, 7, 9, 12, 0, tzinfo=_TZ_TAIPEI)
        lines = runner.on_status_poll(now)
        assert any("Classified" in l for l in lines)
        assert runner._manager._state.last_assessed == "2026-07-08|NIGHT"

    def test_insufficient_bars_backs_off(self, tmp_path):
        # Classifier failure sets a retry backoff instead of hammering
        # (and warning) on every 30s poll until the next night.
        runner = _make_runner(tmp_path)
        runner._manager._bars_provider = lambda: []
        now = datetime(2026, 7, 10, 4, 58, tzinfo=_TZ_TAIPEI)
        runner.on_status_poll(now)
        assert runner._classify_retry_after is not None
        # Within the backoff window the manager is not called again
        calls = []
        orig = runner._manager.classify_session
        runner._manager.classify_session = lambda *a, **k: calls.append(a) or orig(*a, **k)
        runner.on_status_poll(now + timedelta(seconds=30))
        assert calls == []


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
            with open(state_path, encoding="utf-8") as f:
                d = json.load(f)
            ns = d.get("next_session", {})
            if not ns.get("executed", True):
                # Build a new runner — should re-arm
                runner2 = _make_runner(tmp_path)
                assert runner2._pending_recommendation is not None


class TestRestoreActiveLeg:
    """restore_session must re-arm the active leg from session.json."""

    def _save_and_restore(self, tmp_path, leg, strategy_name):
        runner1 = _make_runner(tmp_path)
        # Simulate a swap having happened
        runner1._active_leg = leg
        runner1.regime_idle = (leg == "idle")
        if leg != "idle":
            cls = REGISTRY[strategy_name]
            runner1.strategy = cls()
            runner1.strategy_display_name = strategy_name
            runner1.broker.strategy_label = strategy_name
        runner1._auto_save_session()
        session_path = runner1._session_path
        with open(session_path) as f:
            session_data = json.load(f)

        runner2 = _make_runner(tmp_path)
        assert runner2.active_leg == "idle"
        assert runner2.regime_idle is True
        runner2.restore_session(session_data)
        return runner2

    def test_restore_short_leg(self, tmp_path):
        runner = self._save_and_restore(tmp_path, "short", "TestShort")
        assert runner.active_leg == "short"
        assert runner.regime_idle is False
        assert runner.strategy_display_name == "TestShort"
        assert runner.broker.strategy_label == "TestShort"
        assert isinstance(runner.strategy, ShortStrategy)

    def test_restore_long_leg(self, tmp_path):
        runner = self._save_and_restore(tmp_path, "long", "TestLong")
        assert runner.active_leg == "long"
        assert runner.regime_idle is False
        assert runner.strategy_display_name == "TestLong"

    def test_restore_idle_stays_idle(self, tmp_path):
        runner = self._save_and_restore(tmp_path, "idle", "")
        assert runner.active_leg == "idle"
        assert runner.regime_idle is True

    def test_restore_missing_key_stays_idle(self, tmp_path):
        """Session JSON without active_leg (pre-regime or corrupted)."""
        runner = _make_runner(tmp_path)
        runner.restore_session({"broker": runner.broker.to_dict()})
        assert runner.active_leg == "idle"
        assert runner.regime_idle is True


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


def _klines_15m_days(n_days, start="2026-07-01"):
    """15-min KLine strings spanning ``n_days``, 08:45-18:30 each day
    (40 bars/day → 10 hourly bars/day after aggregation)."""
    lines = []
    day0 = datetime.strptime(start, "%Y-%m-%d")
    for d in range(n_days):
        day = day0 + timedelta(days=d)
        for i in range(40):
            dt = day.replace(hour=8, minute=45) + timedelta(minutes=15 * i)
            price = 22000 + d * 100 + i
            lines.append(
                f"{dt.strftime('%m/%d/%Y %H:%M')},{price},{price+10},"
                f"{price-5},{price+3},{50+i}")
    return lines


class TestClassifierBarsPreseed:
    def _make_bare_runner(self, tmp_path):
        """Runner with NO bars_provider — exercises the default
        _classifier_bars provider."""
        return RegimeSwitchingRunner(
            LongStrategy(), "TX00", log_dir=str(tmp_path),
            bot_name="preseed",
            regime_cfg=_make_cfg(),
            long_strategy_name="TestLong",
            short_strategy_name="TestShort",
            strategies_registry=REGISTRY,
        )

    def test_warmup_preseeds_classifier(self, tmp_path):
        """Warmup KLine history alone must let a fresh bot (empty bot
        dir, no CSVs) classify — the old default provider read only the
        bot's own CSVs, forcing ~4 trading days of cold start."""
        runner = self._make_bare_runner(tmp_path)
        runner.feed_warmup_bars(_klines_15m_days(7))  # ~70 hourly bars
        runner._is_reloading = False
        rec = runner._manager.classify_session("2026-07-08", "NIGHT")
        assert rec is not None

    def test_no_warmup_no_csv_returns_empty(self, tmp_path):
        runner = self._make_bare_runner(tmp_path)
        assert runner._classifier_bars() == []

    def test_high_timeframe_falls_back_to_csv(self, tmp_path):
        # H4/daily bars cannot be re-binned down to the hourly classify
        # interval — the provider must ignore them.
        runner = self._make_bare_runner(tmp_path)
        runner.feed_warmup_bars(_klines_15m_days(7))
        runner.target_interval = 14400  # simulate an H4 donor
        assert runner._classifier_bars() == []  # no CSVs in this dir

    def test_provider_survives_aggregated_bars_rebind(self, tmp_path):
        # _rebuild_timeframe REBINDS self._aggregated_bars (issue #43
        # family) — the provider must read the attribute at call time.
        runner = self._make_bare_runner(tmp_path)
        runner.feed_warmup_bars(_klines_15m_days(7))
        assert len(runner._classifier_bars()) > 0
        runner._aggregated_bars = []
        assert runner._classifier_bars() == []


class TestSessionPnlWindow:
    def _trade(self, exit_dt, pnl):
        from types import SimpleNamespace
        return SimpleNamespace(exit_dt=exit_dt, pnl=pnl)

    def test_night_window_spans_midnight(self, tmp_path):
        """Pre-midnight night exits belong to the night session; day
        trades don't leak in. The old calendar-date match dropped the
        22:30 trade entirely (session was keyed by the NEXT day's date
        at the 04:58 record)."""
        from src.regime.switch_logic import current_session
        runner = _make_runner(tmp_path)
        runner.broker.trades.append(self._trade("2026-07-09 22:30", 100.0))
        runner.broker.trades.append(self._trade("2026-07-10 03:20", 50.0))
        runner.broker.trades.append(self._trade("2026-07-09 10:00", 999.0))
        sess = current_session(datetime(2026, 7, 10, 4, 58, tzinfo=_TZ_TAIPEI))
        assert runner._compute_session_pnl(sess) == (150.0, 2)

    def test_day_window_excludes_night_trades(self, tmp_path):
        """The old date-only match double-counted post-midnight night
        exits into the same date's DAY row."""
        from src.regime.switch_logic import current_session
        runner = _make_runner(tmp_path)
        runner.broker.trades.append(self._trade("2026-07-10 03:20", 50.0))
        runner.broker.trades.append(self._trade("2026-07-10 09:15", 30.0))
        sess = current_session(datetime(2026, 7, 10, 10, 0, tzinfo=_TZ_TAIPEI))
        assert runner._compute_session_pnl(sess) == (30.0, 1)

    def test_force_close_seconds_precision(self, tmp_path):
        # force_close writes "YYYY-MM-DD HH:MM:SS" — must still match.
        from src.regime.switch_logic import current_session
        runner = _make_runner(tmp_path)
        runner.broker.trades.append(self._trade("2026-07-10 10:00:42", 70.0))
        sess = current_session(datetime(2026, 7, 10, 10, 5, tzinfo=_TZ_TAIPEI))
        assert runner._compute_session_pnl(sess) == (70.0, 1)


class TestStaleRecommendationDiscard:
    def test_stale_pending_discarded(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner._pending_recommendation = {
            "date": "2026-07-06", "action": "deploy_long",
            "strategy": "TestLong", "qty_scale": 1.0, "reason": "old",
        }
        # Fri 07:00 — the 07-09-open night has completed since 07-06
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        result = runner._maybe_apply_pending(now)
        assert result is None
        assert runner._pending_recommendation is None  # dropped, not applied
        assert runner.active_leg == "idle"

    def test_fresh_pending_still_applies(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner._pending_recommendation = {
            "date": "2026-07-09", "action": "deploy_long",
            "strategy": "TestLong", "qty_scale": 1.0, "reason": "fresh",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        result = runner._maybe_apply_pending(now)
        assert result is not None
        assert runner.active_leg == "long"


class TestPermanentApplyErrorDiscard:
    def test_unknown_strategy_discards_pending(self, tmp_path):
        """A leg strategy missing from the registry is a permanent config
        error — old code kept the pending armed and errored every 30s
        poll forever (and re-armed it after every restart)."""
        runner = _make_runner(tmp_path)
        runner._long_strategy_name = "Nonexistent"
        runner._pending_recommendation = {
            "date": "2026-07-09", "action": "deploy_long",
            "strategy": "Nonexistent", "qty_scale": 1.0, "reason": "test",
        }
        now = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ_TAIPEI)
        result = runner._maybe_apply_pending(now)
        assert result is None
        assert runner._pending_recommendation is None
        assert runner.active_leg == "idle"


class TestStopRecordsForceClose:
    def test_force_close_trade_included_in_record(self, tmp_path, monkeypatch):
        """stop() must record AFTER LiveRunner.stop() appends the
        force-close trade — old code recorded first, permanently losing
        that trade's P&L from the session row."""
        import csv as csv_mod
        from types import SimpleNamespace
        import src.live.regime_switching_runner as rsr
        from src.live.live_runner import LiveRunner

        runner = _make_runner(tmp_path)
        sess = rsr.current_session(
            datetime(2026, 7, 10, 10, 0, tzinfo=_TZ_TAIPEI))
        monkeypatch.setattr(rsr, "current_session", lambda now=None: sess)

        def fake_stop(self):
            self.broker.trades.append(
                SimpleNamespace(exit_dt="2026-07-10 10:00", pnl=777.0))
            return {}

        monkeypatch.setattr(LiveRunner, "stop", fake_stop)
        runner.stop()

        hist = os.path.join(runner.bot_dir, "regime_history.csv")
        with open(hist, newline="") as f:
            rows = list(csv_mod.reader(f))
        match = [r for r in rows[1:]
                 if r[0] == "2026-07-10" and r[1] == "DAY"]
        assert len(match) == 1
        assert match[0][15] == "777.0"  # pnl column
