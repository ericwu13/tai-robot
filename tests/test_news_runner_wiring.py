"""Phase 2 wiring: RegimeSwitchingRunner × the news circuit breaker.

The planner is exhaustively tested in test_news_circuit_breaker.py; this
file covers the glue that the planner cannot see — that the poll reads
the right files, executes the plan against the real broker/swap paths,
persists across a restart, and stays completely inert when the feature
is off.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import src.live.regime_switching_runner as rsr
from src.backtest.broker import OrderSide
from src.backtest.strategy import BacktestStrategy
from src.config.settings import NewsConfig
from src.live.regime_switching_runner import RegimeSwitchingRunner
from src.news.circuit_breaker import BreakerState
from src.regime.state_machine import RegimeConfig
from src.strategy.examples.news_event import (
    NEWS_LONG_DISPLAY, NEWS_SHORT_DISPLAY, NewsEventLong, NewsEventShort,
)

_TZ = timezone(timedelta(hours=8))

# Fri 2026-07-10: 10:00 = DAY session, 14:00 = post-DAY closed gap,
# 07:00 = pre-DAY closed gap.
IN_SESSION = datetime(2026, 7, 10, 10, 0, tzinfo=_TZ)
IN_GAP_AFTER_DAY = datetime(2026, 7, 10, 14, 0, tzinfo=_TZ)
IN_GAP_BEFORE_DAY = datetime(2026, 7, 10, 7, 0, tzinfo=_TZ)


class Long1m(BacktestStrategy):
    name = "TestLong1m"
    kline_type = 0
    kline_minute = 1

    def on_bar(self, bar, data_store, broker):
        pass

    def required_bars(self):
        return 2


class Short1m(BacktestStrategy):
    name = "TestShort1m"
    kline_type = 0
    kline_minute = 1

    def on_bar(self, bar, data_store, broker):
        pass

    def required_bars(self):
        return 2


REGISTRY = {"TestLong1m": Long1m, "TestShort1m": Short1m,
            NEWS_SHORT_DISPLAY: NewsEventShort, NEWS_LONG_DISPLAY: NewsEventLong}


def _klines_1m(count, base_date="2026-07-10"):
    lines = []
    base = datetime.strptime(f"{base_date} 08:45", "%Y-%m-%d %H:%M")
    for i in range(count):
        dt = base + timedelta(minutes=i)
        price = 22500 + i
        lines.append(f"{dt.strftime('%m/%d/%Y %H:%M')},{price},{price+10},"
                     f"{price-5},{price+3},{50+i}")
    return lines


def _make_cfg():
    return RegimeConfig(
        enabled=True, long_strategy="TestLong1m", short_strategy="TestShort1m",
        classify_interval=3600)


def _news_cfg(tmp_path, **overrides):
    defaults = dict(
        enabled=True,
        signal_path=str(tmp_path / "signal.json"),
        events_path=str(tmp_path / "events.json"),
        ledger_path=str(tmp_path / "ledger.json"),
        max_signal_age_sec=900,
        tier2_enabled=False,
        calendar_min_severity="high",
    )
    defaults.update(overrides)
    return NewsConfig(**defaults)


def _make_runner(tmp_path, news_cfg=None, bot_name="news_bot", warmup=60):
    runner = RegimeSwitchingRunner(
        Long1m(), "TX00", log_dir=str(tmp_path), bot_name=bot_name,
        regime_cfg=_make_cfg(),
        long_strategy_name="TestLong1m", short_strategy_name="TestShort1m",
        strategies_registry=REGISTRY,
        bars_provider=lambda: [],
        news_cfg=news_cfg,
    )
    runner.feed_warmup_bars(_klines_1m(warmup))
    runner._is_reloading = False
    # Isolate the news step: the regime steps have their own test file and
    # would otherwise classify/record on these synthetic timestamps.
    runner._maybe_classify = lambda now: None
    runner._maybe_record_session = lambda now: None
    runner._maybe_apply_pending = lambda now: None
    return runner


def _write_signal(path, action="risk_off", signal_id="sig-1",
                  issued_at=IN_SESSION, **extra):
    payload = {
        "version": 1,
        "signal_id": signal_id,
        "action": action,
        "issued_at": issued_at.isoformat(),
        "severity": "high",
        "source": "unit-test",
        "reason": "unit test signal",
    }
    payload.update(extra)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _write_events(path, events, updated_at=IN_SESSION):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "updated_at": updated_at.isoformat(),
                   "events": events}, f)


def _open_position(runner, price=22500):
    runner.broker.position_size = 1
    runner.broker.position_side = OrderSide.LONG
    runner.broker.entry_price = price
    runner.broker.entry_tag = "Long"
    runner.broker.entry_bar_index = 0
    runner.broker._entry_dt = "2026-07-10 09:00"


# ── Disabled = inert ──

class TestDisabled:
    def test_no_news_cfg_reads_nothing(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path)
        calls = []
        monkeypatch.setattr(rsr, "read_signal",
                            lambda *a, **k: calls.append("signal") or (None, ""))
        monkeypatch.setattr(rsr, "load_events",
                            lambda *a, **k: calls.append("events") or ([], ""))
        assert runner._check_news(IN_SESSION) == []
        assert runner.on_status_poll(IN_SESSION) == []
        assert calls == []
        assert runner.news_idle is False

    def test_disabled_flag_reads_nothing(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path, enabled=False))
        calls = []
        monkeypatch.setattr(rsr, "read_signal",
                            lambda *a, **k: calls.append("signal") or (None, ""))
        _write_signal(tmp_path / "signal.json")
        runner._check_news(IN_SESSION)
        assert calls == []

    def test_disabled_writes_no_news_block_change(self, tmp_path):
        """session.json still carries a clean (default) news block."""
        runner = _make_runner(tmp_path)
        runner._auto_save_session()
        with open(runner._session_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["news"] == BreakerState().to_dict()


# ── Tier 1: risk_off ──

class TestRiskOff:
    def test_flattens_and_suppresses_once_across_ten_polls(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        decisions = []
        runner.on("on_decision", decisions.append)
        _open_position(runner)
        _write_signal(tmp_path / "signal.json")

        for i in range(10):
            runner._check_news(IN_SESSION + timedelta(seconds=30 * i))

        assert len(runner.broker.trades) == 1
        forced = [d for d in decisions if d["action"] == "FORCE_CLOSE"]
        assert len(forced) == 1
        assert forced[0]["tag"] == "news_risk_off"
        assert runner.news_idle is True
        assert runner.breaker_state.signal_suppressed is True
        assert runner.breaker_state.event_suppressed is False

    def test_flat_risk_off_suppresses_without_a_trade(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_signal(tmp_path / "signal.json")
        runner._check_news(IN_SESSION)
        assert runner.broker.trades == []
        assert runner.news_idle is True

    def test_suppression_blocks_strategy_but_keeps_bars(self, tmp_path):
        """news_idle must behave like regime_idle: data flows, trading stops."""
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_signal(tmp_path / "signal.json")
        runner._check_news(IN_SESSION)
        assert runner.news_idle is True

        ran = []
        runner.strategy.on_bar = lambda *a, **k: ran.append(1)
        bar = runner._aggregated_bars[-1]
        runner.regime_idle = False   # isolate news_idle as the only gate

        before = len(runner.data_store)
        runner._process_aggregated_bar(bar)
        assert ran == []                       # no trading
        assert len(runner.data_store) == before + 1   # bars still flow

        # Positive control: the same call trades once suppression lifts.
        runner.news_idle = False
        runner._process_aggregated_bar(bar)
        assert ran == [1]

    def test_clear_releases(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_signal(tmp_path / "signal.json")
        runner._check_news(IN_SESSION)
        assert runner.news_idle is True

        _write_signal(tmp_path / "signal.json", action="clear",
                      signal_id="sig-2")
        runner._check_news(IN_SESSION + timedelta(seconds=60))
        assert runner.news_idle is False
        assert runner.breaker_state.signal_suppressed is False

    def test_clear_does_not_cancel_event_suppression(self, tmp_path):
        """Two sources: the tap clears its own, the calendar keeps gating."""
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_events(tmp_path / "events.json",
                      [{"date": "2026-07-10", "name": "US CPI",
                        "severity": "high", "sessions": ["DAY", "NIGHT"]}])
        _write_signal(tmp_path / "signal.json")
        runner._check_news(IN_SESSION)
        assert runner.breaker_state.signal_suppressed is True
        assert runner.breaker_state.event_suppressed is True

        _write_signal(tmp_path / "signal.json", action="clear",
                      signal_id="sig-2")
        runner._check_news(IN_SESSION + timedelta(seconds=60))
        assert runner.breaker_state.signal_suppressed is False
        assert runner.breaker_state.event_suppressed is True
        assert runner.news_idle is True   # still gated by the event

    def test_stale_signal_is_ignored(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _open_position(runner)
        _write_signal(tmp_path / "signal.json",
                      issued_at=IN_SESSION - timedelta(hours=2))
        runner._check_news(IN_SESSION)
        assert runner.broker.trades == []
        assert runner.news_idle is False


# ── Tier 2: emergency deploy ──

class TestTier2Deploy:
    def test_deploy_bypasses_gap_timing(self, tmp_path):
        """Mid-session — a regime apply would refuse, a news deploy must not."""
        runner = _make_runner(tmp_path, _news_cfg(tmp_path, tier2_enabled=True))
        _open_position(runner)
        _write_signal(tmp_path / "signal.json", action="deploy_short")

        assert rsr.in_closed_gap(IN_SESSION) is False
        runner._check_news(IN_SESSION)

        assert isinstance(runner.strategy, NewsEventShort)
        assert runner.strategy_display_name == NEWS_SHORT_DISPLAY
        assert runner.breaker_state.news_strategy_active is True
        assert runner.breaker_state.news_deployed_session_key == "2026-07-10|DAY"
        assert runner.regime_idle is False
        assert runner.news_idle is False
        assert len(runner.broker.trades) == 1   # flattened first

    def test_deploy_long_in_gap_keys_next_session(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path, tier2_enabled=True))
        _write_signal(tmp_path / "signal.json", action="deploy_long",
                      issued_at=IN_GAP_BEFORE_DAY)
        runner._check_news(IN_GAP_BEFORE_DAY)
        assert isinstance(runner.strategy, NewsEventLong)
        assert runner.breaker_state.news_deployed_session_key == "2026-07-10|DAY"

    def test_tier2_disabled_does_not_deploy(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path, tier2_enabled=False))
        _write_signal(tmp_path / "signal.json", action="deploy_short")
        runner._check_news(IN_SESSION)
        assert isinstance(runner.strategy, Long1m)
        assert runner.breaker_state.news_strategy_active is False
        # ...but the signal is consumed, so it does not retry forever.
        assert runner._ledger.is_consumed("sig-1") is True

    def test_deploy_refused_in_replay(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path, tier2_enabled=True))
        _open_position(runner)
        runner.suppress_strategy = True
        _write_signal(tmp_path / "signal.json", action="deploy_short")
        runner._check_news(IN_SESSION)
        assert isinstance(runner.strategy, Long1m)
        assert runner.broker.trades == []          # no flatten either
        assert runner.breaker_state.news_strategy_active is False
        assert runner._ledger.is_consumed("sig-1") is True

    def test_risk_off_refused_in_replay(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _open_position(runner)
        runner._is_reloading = True
        _write_signal(tmp_path / "signal.json")
        runner._check_news(IN_SESSION)
        assert runner.broker.trades == []
        assert runner.news_idle is False
        assert runner._ledger.is_consumed("sig-1") is True


# ── Revert at the session boundary ──

class TestRevertAtBoundary:
    def _deploy(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path, tier2_enabled=True))
        _write_signal(tmp_path / "signal.json", action="deploy_short")
        runner._check_news(IN_SESSION)
        assert runner.breaker_state.news_strategy_active is True
        return runner

    def test_same_session_does_not_revert(self, tmp_path):
        runner = self._deploy(tmp_path)
        runner._check_news(IN_SESSION + timedelta(minutes=30))
        assert isinstance(runner.strategy, NewsEventShort)
        assert runner.breaker_state.news_strategy_active is True

    def test_reverts_once_the_session_ends(self, tmp_path):
        runner = self._deploy(tmp_path)
        lines = runner._check_news(IN_GAP_AFTER_DAY)
        assert any("Reverted to regime" in l for l in lines)
        assert runner.breaker_state.news_strategy_active is False
        assert runner.breaker_state.news_deployed_session_key == ""
        assert runner.active_leg == "idle"
        assert runner.regime_idle is True

    def test_revert_follows_the_selector(self, tmp_path, monkeypatch):
        from src.regime.selector import Recommendation
        runner = self._deploy(tmp_path)
        monkeypatch.setattr(
            runner._manager, "current_recommendation",
            lambda: Recommendation("deploy_short", "TestShort1m",
                                   reason="trending-down"))
        runner._check_news(IN_GAP_AFTER_DAY)
        assert runner.active_leg == "short"
        assert isinstance(runner.strategy, Short1m)
        assert runner.breaker_state.news_strategy_active is False

    def test_failed_leg_revert_falls_back_to_sit_out(self, tmp_path, monkeypatch):
        """A refused swap must never strand the bot on the event strategy."""
        from src.regime.selector import Recommendation
        runner = self._deploy(tmp_path)
        monkeypatch.setattr(
            runner._manager, "current_recommendation",
            lambda: Recommendation("deploy_long", "Nonexistent", reason="x"))
        runner._long_strategy_name = "Nonexistent"
        runner._check_news(IN_GAP_AFTER_DAY)
        assert runner.breaker_state.news_strategy_active is False
        assert runner.active_leg == "idle"
        assert runner.regime_idle is True

    def test_no_revert_without_a_news_strategy(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        assert runner._check_news_revert(IN_GAP_AFTER_DAY) == []


# ── Scheduled-event calendar gate ──

class TestCalendarGate:
    def test_active_event_stamps_and_suppresses(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_events(tmp_path / "events.json",
                      [{"date": "2026-07-10", "name": "US CPI",
                        "severity": "high", "sessions": ["DAY"]}])
        runner._check_news(IN_SESSION)
        assert runner.news_idle is True
        assert runner.breaker_state.event_name == "US CPI"
        assert (runner._manager._state.last_features.get("_event_risk")
                == "US CPI")

    def test_stamp_persists_to_regime_state(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_events(tmp_path / "events.json",
                      [{"date": "2026-07-10", "name": "FOMC",
                        "severity": "high", "sessions": ["DAY"]}])
        runner._check_news(IN_SESSION)
        state_path = os.path.join(runner.bot_dir, "regime_state.json")
        with open(state_path, encoding="utf-8") as f:
            assert json.load(f)["last_features"]["_event_risk"] == "FOMC"

    def test_gate_clears_when_the_window_passes(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_events(tmp_path / "events.json",
                      [{"date": "2026-07-10", "name": "US CPI",
                        "severity": "high", "sessions": ["DAY"]}])
        runner._check_news(IN_SESSION)
        assert runner.news_idle is True

        _write_events(tmp_path / "events.json", [])
        runner._events_mtime = -1.0   # force a re-read (same-second mtime)
        runner._check_news(IN_SESSION + timedelta(minutes=10))
        assert runner.news_idle is False
        assert runner.breaker_state.event_name == ""
        assert "_event_risk" not in runner._manager._state.last_features

    def test_low_severity_below_floor_does_not_gate(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_events(tmp_path / "events.json",
                      [{"date": "2026-07-10", "name": "Minor print",
                        "severity": "low", "sessions": ["DAY"]}])
        runner._check_news(IN_SESSION)
        assert runner.news_idle is False

    def test_stale_calendar_fails_open(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_events(tmp_path / "events.json",
                      [{"date": "2026-07-10", "name": "US CPI",
                        "severity": "high", "sessions": ["DAY"]}],
                      updated_at=IN_SESSION - timedelta(days=30))
        runner._check_news(IN_SESSION)
        assert runner.news_idle is False
        assert runner._calendar_stale_warned is True

    def test_gate_deferred_while_a_position_is_open(self, tmp_path):
        """An events.json refreshed mid-trade must not freeze the open
        position's stop management — news_idle short-circuits
        _process_aggregated_bar before the strategy ever runs."""
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _open_position(runner)
        _write_events(tmp_path / "events.json",
                      [{"date": "2026-07-10", "name": "US CPI",
                        "severity": "high", "sessions": ["DAY"]}])

        runner._check_news(IN_SESSION)
        assert runner.news_idle is False
        assert runner.breaker_state.event_suppressed is False
        assert runner.breaker_state.event_name == ""
        assert "_event_risk" not in runner._manager._state.last_features

        # Position exits by its own management → the gate engages on the
        # very next poll, no new file write needed.
        runner.broker.position_size = 0
        runner.broker.position_side = None
        runner._check_news(IN_SESSION + timedelta(seconds=30))
        assert runner.news_idle is True
        assert runner.breaker_state.event_name == "US CPI"
        assert (runner._manager._state.last_features.get("_event_risk")
                == "US CPI")

    def test_deferred_gate_still_lets_the_strategy_manage_the_trade(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _open_position(runner)
        _write_events(tmp_path / "events.json",
                      [{"date": "2026-07-10", "name": "US CPI",
                        "severity": "high", "sessions": ["DAY"]}])
        runner._check_news(IN_SESSION)

        ran = []
        runner.strategy.on_bar = lambda *a, **k: ran.append(1)
        runner.regime_idle = False   # isolate news_idle as the only gate
        runner._process_aggregated_bar(runner._aggregated_bars[-1])
        assert ran == [1]

    def test_missing_calendar_does_not_gate(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        runner._check_news(IN_SESSION)
        assert runner.news_idle is False

    def test_file_is_parsed_at_most_every_five_minutes(self, tmp_path,
                                                       monkeypatch):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_events(tmp_path / "events.json", [])
        parses = []
        real = rsr.load_events
        monkeypatch.setattr(rsr, "load_events",
                            lambda p: parses.append(p) or real(p))
        for i in range(10):
            runner._check_news(IN_SESSION + timedelta(seconds=30 * i))
        assert len(parses) == 1

    def test_expired_cache_is_reparsed(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_events(tmp_path / "events.json", [])
        parses = []
        real = rsr.load_events
        monkeypatch.setattr(rsr, "load_events",
                            lambda p: parses.append(p) or real(p))
        runner._check_news(IN_SESSION)
        runner._check_news(IN_SESSION + timedelta(seconds=301))
        assert len(parses) == 2


# ── Restart: suppression + ledger survive ──

class TestRestart:
    def test_suppression_and_consumed_survive_a_restart(self, tmp_path):
        runner1 = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_signal(tmp_path / "signal.json")
        runner1._check_news(IN_SESSION)
        assert runner1.news_idle is True
        with open(runner1._session_path, encoding="utf-8") as f:
            session_data = json.load(f)

        runner2 = _make_runner(tmp_path, _news_cfg(tmp_path))
        assert runner2.news_idle is False        # clean until restored
        runner2.restore_session(session_data)
        assert runner2.news_idle is True
        assert runner2.breaker_state.signal_suppressed is True
        # The same signal file is still on disk — it must not re-fire.
        _open_position(runner2)
        runner2._check_news(IN_SESSION + timedelta(minutes=1))
        assert runner2.broker.trades == []

    def test_event_gate_survives_a_restart(self, tmp_path):
        runner1 = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_events(tmp_path / "events.json",
                      [{"date": "2026-07-10", "name": "US CPI",
                        "severity": "high", "sessions": ["DAY"]}])
        runner1._check_news(IN_SESSION)
        with open(runner1._session_path, encoding="utf-8") as f:
            session_data = json.load(f)

        runner2 = _make_runner(tmp_path, _news_cfg(tmp_path))
        runner2.restore_session(session_data)
        assert runner2.news_idle is True
        assert runner2.breaker_state.event_name == "US CPI"

    def test_session_without_news_block_restores_clean(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        runner.restore_session({"broker": runner.broker.to_dict()})
        assert runner.breaker_state == BreakerState()
        assert runner.news_idle is False

    def test_missing_ledger_path_defaults_into_the_bot_dir(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path, ledger_path=""))
        _write_signal(tmp_path / "signal.json")
        runner._check_news(IN_SESSION)
        assert os.path.exists(os.path.join(runner.bot_dir, "news_ledger.json"))
        assert runner._ledger.is_consumed("sig-1") is True


# ── Failure containment ──

class TestFailureContainment:
    def test_action_exception_still_consumes(self, tmp_path):
        """A broken action must not re-run every 30s forever."""
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _open_position(runner)
        boom = []

        def explode(tag, reason):
            boom.append(tag)
            raise RuntimeError("broker exploded")

        runner.force_close_position = explode
        notes = []
        runner.on_news_notify_cb = notes.append
        _write_signal(tmp_path / "signal.json")

        runner._check_news(IN_SESSION)
        assert runner._ledger.is_consumed("sig-1") is True
        assert any("failed" in n for n in notes)

        runner._check_news(IN_SESSION + timedelta(seconds=30))
        assert len(boom) == 1   # not retried

    def test_broken_calendar_does_not_stop_the_signal(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        with open(tmp_path / "events.json", "w", encoding="utf-8") as f:
            f.write("{ not json")
        _write_signal(tmp_path / "signal.json")
        runner._check_news(IN_SESSION)
        assert runner.breaker_state.signal_suppressed is True

    def test_discord_failure_is_swallowed(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))

        def bad(msg):
            raise RuntimeError("discord down")

        runner.on_news_notify_cb = bad
        _write_signal(tmp_path / "signal.json")
        runner._check_news(IN_SESSION)
        assert runner.news_idle is True


# ── Poll integration ──

class TestStatusPollIntegration:
    def test_on_status_poll_runs_the_news_step(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_signal(tmp_path / "signal.json")
        lines = runner.on_status_poll(IN_SESSION)
        assert any("[NEWS]" in l for l in lines)
        assert runner.news_idle is True

    def test_news_status_exposed_to_the_gui(self, tmp_path):
        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        _write_signal(tmp_path / "signal.json")
        runner._check_news(IN_SESSION)
        status = runner.get_regime_status()
        assert status["news_enabled"] is True
        assert status["news"]["signal_suppressed"] is True

    def test_stop_still_records_the_session(self, tmp_path, monkeypatch):
        """Existing behaviour preserved with the news step in place."""
        import csv as csv_mod
        from types import SimpleNamespace
        from src.live.live_runner import LiveRunner

        runner = _make_runner(tmp_path, _news_cfg(tmp_path))
        sess = rsr.current_session(IN_SESSION)
        monkeypatch.setattr(rsr, "current_session", lambda now=None: sess)

        def fake_stop(self):
            self.broker.trades.append(
                SimpleNamespace(exit_dt="2026-07-10 10:00", pnl=321.0))
            return {}

        monkeypatch.setattr(LiveRunner, "stop", fake_stop)
        runner.stop()

        hist = os.path.join(runner.bot_dir, "regime_history.csv")
        with open(hist, newline="") as f:
            rows = list(csv_mod.reader(f))
        match = [r for r in rows[1:] if r[0] == "2026-07-10" and r[1] == "DAY"]
        assert len(match) == 1
        assert match[0][15] == "321.0"


# ── LiveRunner.force_close_position (shared emergency-exit path) ──

class TestForceClosePath:
    def test_no_position_is_a_noop(self, tmp_path):
        runner = _make_runner(tmp_path)
        assert runner.force_close_position("news_risk_off", "x") == 0
        assert runner.broker.trades == []

    def test_emits_force_close_with_the_given_tag(self, tmp_path):
        runner = _make_runner(tmp_path)
        decisions = []
        runner.on("on_decision", decisions.append)
        _open_position(runner)
        price = runner.force_close_position("news_risk_off", "news flatten")
        assert price > 0
        assert len(runner.broker.trades) == 1
        assert decisions[-1]["action"] == "FORCE_CLOSE"
        assert decisions[-1]["tag"] == "news_risk_off"
        assert decisions[-1]["reason"] == "news flatten"


@pytest.mark.parametrize("action,expect_suppressed", [
    ("risk_off", True),
    ("clear", False),
])
def test_signal_actions_end_to_end(tmp_path, action, expect_suppressed):
    runner = _make_runner(tmp_path, _news_cfg(tmp_path))
    _write_signal(tmp_path / "signal.json", action=action)
    runner._check_news(IN_SESSION)
    assert runner.news_idle is expect_suppressed
