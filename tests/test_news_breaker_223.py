"""Issue #223 — shared NewsBreaker, entry-only suppression, W2 heartbeat.

These tests are the failing-first pins. Against the pre-fix tree they
fail because:

- a plain LiveRunner has no breaker and ``--news`` is refused
- ``news_idle`` returns from ``_process_aggregated_bar`` before the
  strategy, so a stop / market close never becomes TRADE_CLOSE
- nothing reads a W2 heartbeat, so a real bot cannot fail closed
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.backtest.broker import OrderSide
from src.backtest.strategy import BacktestStrategy
from src.config.settings import NewsConfig
from src.live.headless import build_deploy_request, build_parser
from src.live.live_runner import LiveRunner
from src.live.regime_switching_runner import RegimeSwitchingRunner
from src.live.trading_guard import TradingGuard
from src.news.circuit_breaker import BreakerState
from src.news.heartbeat import (
    DEFAULT_MAX_HEARTBEAT_AGE_SEC,
    read_heartbeat,
    write_heartbeat,
)
from src.news.news_breaker import NewsBreaker
from src.regime.state_machine import RegimeConfig
from scripts.dashboard import snapshot
from scripts.news_bridge.crossmarket_monitor import write_heartbeat_file

_TZ = timezone(timedelta(hours=8))
IN_SESSION = datetime(2026, 7, 10, 10, 0, tzinfo=_TZ)
IN_GAP = datetime(2026, 7, 10, 14, 0, tzinfo=_TZ)


class _Strat(BacktestStrategy):
    name = "Plain1m"
    kline_type = 0
    kline_minute = 1

    def __init__(self, fn=None):
        self.fn = fn
        self.calls = 0

    def on_bar(self, bar, data_store, broker):
        self.calls += 1
        if self.fn is not None:
            self.fn(bar, broker)

    def required_bars(self):
        return 1


def _klines(count=30, base_date="2026-07-10"):
    lines = []
    base = datetime.strptime(f"{base_date} 08:45", "%Y-%m-%d %H:%M")
    for i in range(count):
        dt = base + timedelta(minutes=i)
        price = 22500 + i
        lines.append(
            f"{dt.strftime('%m/%d/%Y %H:%M')},{price},{price+10},"
            f"{price-5},{price+3},{50+i}")
    return lines


def _fresh(runner):
    from dataclasses import replace
    last = runner._aggregated_bars[-1]
    return replace(last, dt=last.dt + timedelta(minutes=1))


def _open(runner, side=OrderSide.LONG, tag="Long"):
    runner.broker.position_size = 1
    runner.broker.position_side = side
    runner.broker.entry_price = 22500
    runner.broker.entry_tag = tag
    runner.broker.entry_bar_index = 0
    runner.broker._entry_dt = "2026-07-10 09:00"


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
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


def _cfg(tmp, name, **overrides):
    base = dict(
        enabled=True,
        signal_path=str(tmp / "signal.json"),
        events_path=str(tmp / "events.json"),
        ledger_path=str(tmp / name / "ledger.json"),
        heartbeat_path=str(tmp / name / "w2_heartbeat.json"),
        regime_vote_path=str(tmp / "regime_vote.json"),
        max_signal_age_sec=900,
        tier2_enabled=False,
        suppress_scope="both",
    )
    base.update(overrides)
    (tmp / name).mkdir(parents=True, exist_ok=True)
    return NewsConfig(**base)


def _plain_named(tmp, cfg, bot_name, **kw):
    runner = LiveRunner(
        _Strat(), "TX00", log_dir=str(tmp), bot_name=bot_name, news_cfg=cfg)
    runner.feed_warmup_bars(_klines())
    runner._is_reloading = False
    runner.trading_mode = kw.get("mode", "paper")
    runner._news_leg = kw.get("leg", "")
    return runner


def _regime(tmp, cfg, *, leg="idle"):
    runner = RegimeSwitchingRunner(
        _Strat(), "TX00", log_dir=str(tmp), bot_name="regime_n",
        regime_cfg=RegimeConfig(
            enabled=True, long_strategy="L", short_strategy="S",
            classify_interval=60),
        long_strategy_name="L", short_strategy_name="S",
        strategies_registry={},
        bars_provider=lambda: [],
        news_cfg=cfg,
    )
    runner.feed_warmup_bars(_klines())
    runner._is_reloading = False
    runner._maybe_classify = lambda now: None
    runner._maybe_record_session = lambda now: None
    runner._maybe_apply_pending = lambda now: None
    runner._active_leg = leg
    runner.regime_idle = False
    return runner


def _args(*extra):
    return build_parser().parse_args(
        ["deploy", "--symbol", "TMF00", "--bot", "b1", *extra])


# ── R1: opt-in, persisted, shared ──

def test_news_off_by_default_and_old_session_resumes_off():
    fresh = build_deploy_request(_args("--strategy", "x"), None)
    assert fresh.news_enabled is False
    assert fresh.news_tier2_enabled is False
    assert fresh.news_directional is False
    old = build_deploy_request(
        _args("--strategy", "x"), {"strategy": "Saved", "trading_mode": "paper"})
    assert old.news_enabled is False
    assert old.news_tier2_enabled is False
    assert old.news_directional is False


def test_resume_restores_news_flags_without_repeating_them():
    session = {
        "strategy": "Saved",
        "news_enabled": True,
        "news_tier2_enabled": True,
        "news_suppress_scope": "conflicting_leg",
    }
    req = build_deploy_request(_args(), session)
    assert req.news_enabled is True
    assert req.news_tier2_enabled is True
    assert req.news_directional is True
    assert req.regime_enabled is False


def test_plain_runner_without_news_does_not_touch_files(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "src.news.news_breaker.read_signal",
        lambda *a, **k: seen.append("signal") or (None, ""))
    monkeypatch.setattr(
        "src.news.news_breaker.read_heartbeat",
        lambda *a, **k: seen.append("hb") or (None, "missing"))
    runner = _plain_named(tmp_path, None, "off")
    assert runner._check_news(IN_SESSION) == []
    assert seen == []
    assert runner.news_idle is False
    assert runner.heartbeat_entry_block == ""
    runner._auto_save_session()
    data = json.loads(Path(runner._session_path).read_text(encoding="utf-8"))
    assert data["news_enabled"] is False
    runner2 = _plain_named(tmp_path, None, "off2")
    runner2.restore_session(data)
    assert runner2._news_enabled is False
    assert runner2.news_idle is False


def test_both_runners_share_one_breaker_class(tmp_path):
    """A copy-pasted poll on either runner fails this."""
    plain = _plain_named(tmp_path, _cfg(tmp_path, "p"), "p")
    regime = _regime(tmp_path, _cfg(tmp_path, "r"))
    assert type(plain._news) is NewsBreaker
    assert type(regime._news) is NewsBreaker
    assert plain._check_news.__func__ is regime._check_news.__func__
    assert regime.reads_regime_votes is True
    assert plain.reads_regime_votes is False


def test_plain_bot_does_not_stat_the_vote_file(tmp_path, monkeypatch):
    vote = tmp_path / "regime_vote.json"
    vote.write_text("{}", encoding="utf-8")
    touched = []
    real_exists = __import__("os").path.exists

    def spy(path):
        if str(path) == str(vote):
            touched.append(path)
        return real_exists(path)

    monkeypatch.setattr("src.news.news_breaker.os.path.exists", spy)
    runner = _plain_named(tmp_path, _cfg(tmp_path, "p"), "p")
    runner._check_news(IN_SESSION)
    assert touched == []
    assert runner._news.health.votes_age_sec is None


@pytest.mark.parametrize("action,side,leg,expect_flat,expect_idle", [
    ("risk_off", OrderSide.LONG, "long", True, True),
    ("clear", OrderSide.LONG, "long", False, False),
])
def test_plain_and_regime_gate_decisions_match(
        tmp_path, action, side, leg, expect_flat, expect_idle):
    """Same signal, same position, same leg → identical gate state."""
    _write_signal(tmp_path / "signal.json", action=action, signal_id=f"id-{action}")
    # A fresh heartbeat so the source-health gate is not the difference.
    write_heartbeat(tmp_path / "p" / "w2_heartbeat.json", IN_SESSION)
    write_heartbeat(tmp_path / "r" / "w2_heartbeat.json", IN_SESSION)
    plain = _plain_named(tmp_path, _cfg(tmp_path, "p"), "p", leg=leg)
    regime = _regime(tmp_path, _cfg(tmp_path, "r"), leg=leg)
    if action != "clear":
        _open(plain, side)
        _open(regime, side)
    plain._check_news(IN_SESSION)
    regime._check_news(IN_SESSION)
    assert plain.news_idle is expect_idle
    assert regime.news_idle is expect_idle
    assert plain.breaker_state.to_dict() == regime.breaker_state.to_dict()
    assert (len(plain.broker.trades) == 1) is expect_flat
    assert (len(regime.broker.trades) == 1) is expect_flat


def test_session_key_change_releases_on_both_runners(tmp_path):
    _write_signal(tmp_path / "signal.json")
    write_heartbeat(tmp_path / "p" / "w2_heartbeat.json", IN_SESSION)
    write_heartbeat(tmp_path / "r" / "w2_heartbeat.json", IN_SESSION)
    plain = _plain_named(tmp_path, _cfg(tmp_path, "p"), "p")
    regime = _regime(tmp_path, _cfg(tmp_path, "r"), leg="long")
    plain._check_news(IN_SESSION)
    regime._check_news(IN_SESSION)
    assert plain.news_idle and regime.news_idle
    plain._check_news(IN_GAP)
    regime._check_news(IN_GAP)
    assert plain.news_idle is False
    assert regime.news_idle is False
    assert plain.breaker_state.signal_suppressed is False
    assert regime.breaker_state.signal_suppressed is False


def test_stale_signal_does_not_act_and_ledger_acts_once(tmp_path):
    runner = _plain_named(tmp_path, _cfg(tmp_path, "p"), "p")
    write_heartbeat(tmp_path / "p" / "w2_heartbeat.json", IN_SESSION)
    _open(runner)
    _write_signal(tmp_path / "signal.json",
                  issued_at=IN_SESSION - timedelta(seconds=900))
    runner._check_news(IN_SESSION)
    assert runner.broker.trades == []
    assert runner.news_idle is False

    _write_signal(tmp_path / "signal.json", signal_id="fresh")
    runner._check_news(IN_SESSION)
    runner._check_news(IN_SESSION + timedelta(seconds=30))
    assert len(runner.broker.trades) == 1
    assert runner._ledger.is_consumed("fresh")


def test_directional_keeps_same_side_position(tmp_path):
    cfg = _cfg(tmp_path, "p", suppress_scope="conflicting_leg")
    write_heartbeat(tmp_path / "p" / "w2_heartbeat.json", IN_SESSION)
    _write_signal(tmp_path / "signal.json", direction="bearish")
    runner = _plain_named(tmp_path, cfg, "p", leg="short")
    _open(runner, OrderSide.SHORT, tag="Short")
    runner._check_news(IN_SESSION)
    assert runner.broker.position_size == 1          # same-side kept
    assert runner.news_idle is False                 # short leg not gated
    # The kept position's stop still exits (R2 — and here the leg is open).
    runner.strategy = _Strat(lambda bar, b: b.exit("sl", "Short", stop=1))
    runner._process_aggregated_bar(_fresh(runner))
    assert runner.broker.position_size == 0
    assert runner.broker.last_exit_type == "stop"


def test_scope_default_stays_both():
    assert NewsConfig().suppress_scope == "both"


def test_plain_bot_tier2_does_not_swap_legs(tmp_path):
    """Tier2 is accepted and persisted. A plain bot has no leg swap."""
    cfg = _cfg(tmp_path, "p", tier2_enabled=True)
    write_heartbeat(tmp_path / "p" / "w2_heartbeat.json", IN_SESSION)
    _write_signal(tmp_path / "signal.json", action="deploy_short", signal_id="dep")
    runner = _plain_named(tmp_path, cfg, "p")
    original = type(runner.strategy)
    runner._check_news(IN_SESSION)
    assert type(runner.strategy) is original
    assert runner.breaker_state.news_strategy_active is False
    assert runner._ledger.is_consumed("dep") is True


def test_enabled_flags_round_trip_through_session(tmp_path):
    cfg = _cfg(tmp_path, "p", tier2_enabled=True, suppress_scope="conflicting_leg")
    runner = _plain_named(tmp_path, cfg, "p")
    runner._auto_save_session()
    data = json.loads(Path(runner._session_path).read_text(encoding="utf-8"))
    assert data["news_enabled"] is True
    assert data["news_tier2_enabled"] is True
    assert data["news_suppress_scope"] == "conflicting_leg"
    assert "news_health" in data
    # A deploy that opts out ignores the saved gate.
    runner._check_news  # bound
    _write_signal(tmp_path / "signal.json")
    write_heartbeat(tmp_path / "p" / "w2_heartbeat.json", IN_SESSION)
    runner._check_news(IN_SESSION)
    data = json.loads(Path(runner._session_path).read_text(encoding="utf-8"))
    opted_out = _plain_named(tmp_path, None, "off")
    opted_out.restore_session(data)
    assert opted_out.news_idle is False
    assert opted_out.breaker_state == BreakerState()


# ── R2: exits pass, entries do not ──

def _gated(tmp, *, mode="paper"):
    cfg = _cfg(tmp, "p")
    write_heartbeat(tmp / "p" / "w2_heartbeat.json", IN_SESSION)
    runner = _plain_named(tmp, cfg, "p", mode=mode)
    _write_signal(tmp / "signal.json", signal_id="gate")
    # Flat risk_off: suppress without a flatten, then open a position the
    # gate must not freeze.
    runner._check_news(IN_SESSION)
    assert runner.news_idle is True
    _open(runner)
    return runner


def test_stop_close_and_force_close_pass_while_entries_drop(tmp_path):
    runner = _gated(tmp_path)
    decisions = []
    runner.on("on_decision", decisions.append)

    def trade(bar, broker):
        broker.exit("sl", "Long", stop=10 ** 9)
        broker.entry("Again", OrderSide.LONG)

    runner.strategy = _Strat(trade)
    runner._process_aggregated_bar(_fresh(runner))
    assert runner.strategy.calls == 1          # mutant early-return fails
    assert runner.broker.last_exit_type == "stop"
    assert any(d["action"] == "TRADE_CLOSE" for d in decisions)
    assert runner.broker.position_size == 0
    assert not any(d["action"] == "ENTRY_FILL" for d in decisions)
    assert any(d["action"] == "NEWS_SUPPRESSED" for d in decisions)


def test_market_close_passes_while_suppressed(tmp_path):
    runner = _gated(tmp_path)
    decisions = []
    runner.on("on_decision", decisions.append)

    def trade(bar, broker):
        broker.close("Long", "flat")
        broker.entry("Again", OrderSide.LONG)

    runner.strategy = _Strat(trade)
    runner._process_aggregated_bar(_fresh(runner))
    assert any(d["action"] == "TRADE_CLOSE" for d in decisions)
    assert runner.broker.position_size == 0
    assert not any(d["action"] == "ENTRY_FILL" for d in decisions)


def test_force_close_passes_while_suppressed(tmp_path):
    runner = _gated(tmp_path)
    decisions = []
    runner.on("on_decision", decisions.append)
    price = runner.force_close_position("manual_flat", "operator")
    assert price > 0
    assert decisions[-1]["action"] == "FORCE_CLOSE"
    assert runner.broker.position_size == 0


def test_entry_filter_does_not_touch_trading_guard(tmp_path):
    """#222 link gate and #204-style ENTRY_FILL blocks stay on the guard.

    The news filter drops the sim entry. It must not arm fill_pending
    (that would also block exits) or clear link_entries_blocked.
    """
    guard = TradingGuard()
    guard.link_entries_blocked = True
    guard.real_entry_confirmed = True
    runner = _gated(tmp_path)
    # Flat, so a filled entry would be visible. The broker only drops an
    # entry when a position is already open — this has to be the filter.
    runner.broker.position_size = 0
    runner.broker.position_side = None
    runner.strategy = _Strat(lambda bar, b: b.entry("L", OrderSide.LONG))
    runner._process_aggregated_bar(_fresh(runner))
    assert runner.broker.position_size == 0
    assert guard.link_entries_blocked is True
    assert guard.fill_pending is False
    assert guard.decide("auto", "ENTRY_FILL", "LONG")[0] == TradingGuard.BLOCK_LINK
    assert guard.decide("auto", "TRADE_CLOSE", "LONG")[0] == TradingGuard.SEND_EXIT
    assert guard.decide("auto", "FORCE_CLOSE", "LONG")[0] == TradingGuard.SEND_EXIT


# ── R3: heartbeat ──

def test_heartbeat_age_constant_is_the_only_definition():
    """Provisional default lives in one assignment. Callers reference it."""
    assert DEFAULT_MAX_HEARTBEAT_AGE_SEC == 900
    assert NewsConfig().max_heartbeat_age_sec == DEFAULT_MAX_HEARTBEAT_AGE_SEC
    root = Path(__file__).resolve().parents[1]
    paths = list((root / "src").rglob("*.py"))
    paths.append(root / "run_backtest.py")
    paths += list((root / "scripts").rglob("*.py"))
    hits = []
    for path in paths:
        if "__pycache__" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            numbered = "900" in line and (
                "max_heartbeat_age_sec" in line
                or line.strip().startswith("DEFAULT_MAX_HEARTBEAT_AGE_SEC"))
            if numbered:
                hits.append(f"{path.relative_to(root)}:{n}:{line.strip()}")
    assert len(hits) == 1, hits
    assert hits[0].startswith("src/news/heartbeat.py:"), hits


def test_stale_or_missing_heartbeat_blocks_real_entries_only(tmp_path, caplog):
    cfg = _cfg(tmp_path, "p")
    runner = _plain_named(tmp_path, cfg, "p", mode="auto")
    caplog.set_level(logging.WARNING)
    runner._check_news(IN_SESSION)
    assert runner.heartbeat_entry_block
    assert "missing" in runner.heartbeat_entry_block
    runner.strategy = _Strat(lambda bar, b: b.entry("L", OrderSide.LONG))
    runner._process_aggregated_bar(_fresh(runner))
    assert runner.broker.position_size == 0

    # Same bot, paper: warning only, entry fills.
    runner.trading_mode = "paper"
    runner.heartbeat_entry_block = "stale-until-next-poll"
    runner._check_news(IN_SESSION + timedelta(seconds=30))
    assert runner.heartbeat_entry_block == ""
    warnings = [r for r in caplog.records if "heartbeat" in r.getMessage().lower()]
    assert len(warnings) == 1
    runner.strategy = _Strat(lambda bar, b: b.entry("L", OrderSide.LONG))
    runner._process_aggregated_bar(_fresh(runner))
    assert runner.broker.position_size == 1


def test_stale_heartbeat_still_lets_a_real_exit_through(tmp_path):
    cfg = _cfg(tmp_path, "p")
    runner = _plain_named(tmp_path, cfg, "p", mode="auto")
    _open(runner)
    decisions = []
    runner.on("on_decision", decisions.append)
    runner._check_news(IN_SESSION)
    assert runner.heartbeat_entry_block

    def trade(bar, broker):
        broker.exit("sl", "Long", stop=10 ** 9)
        broker.entry("Again", OrderSide.LONG)

    runner.strategy = _Strat(trade)
    runner._process_aggregated_bar(_fresh(runner))
    assert any(d["action"] == "TRADE_CLOSE" for d in decisions)
    assert runner.broker.last_exit_type == "stop"
    assert runner.broker.position_size == 0


def test_heartbeat_alert_is_debounced_and_recovery_clears_the_block(tmp_path):
    cfg = _cfg(tmp_path, "p")
    runner = _plain_named(tmp_path, cfg, "p", mode="auto")
    notes = []
    runner.on_news_notify_cb = notes.append
    runner._check_news(IN_SESSION)
    runner._check_news(IN_SESSION + timedelta(seconds=30))
    stale = [n for n in notes if "heartbeat STALE" in n]
    assert len(stale) == 1
    assert runner.heartbeat_entry_block

    when = IN_SESSION + timedelta(seconds=60)
    write_heartbeat(cfg.heartbeat_path, when)
    runner._check_news(when)
    assert runner.heartbeat_entry_block == ""
    assert len([n for n in notes if "heartbeat RECOVERED" in n]) == 1
    runner._check_news(when + timedelta(seconds=30))
    assert len([n for n in notes if "heartbeat RECOVERED" in n]) == 1
    assert len([n for n in notes if "heartbeat STALE" in n]) == 1

    # Age exactly at the limit is stale (same rule as the signal file).
    old = when + timedelta(seconds=120)
    write_heartbeat(cfg.heartbeat_path, old - timedelta(seconds=DEFAULT_MAX_HEARTBEAT_AGE_SEC))
    age, status = read_heartbeat(cfg.heartbeat_path, old, DEFAULT_MAX_HEARTBEAT_AGE_SEC)
    assert status == "stale"
    assert age >= DEFAULT_MAX_HEARTBEAT_AGE_SEC


def test_monitor_heartbeat_file_matches_the_reader(tmp_path):
    path = tmp_path / "w2_heartbeat.json"
    write_heartbeat_file(path, IN_SESSION)
    age, status = read_heartbeat(path, IN_SESSION, DEFAULT_MAX_HEARTBEAT_AGE_SEC)
    assert status == "ok"
    assert age is not None and abs(age) < 1


# ── Dashboard field ──

def test_dashboard_risk_off_off_when_the_field_is_absent(tmp_path):
    bot = tmp_path / "TMF00_old"
    bot.mkdir()
    (bot / "session.json").write_text(json.dumps({
        "strategy": "Demo", "symbol": "TMF00", "bot_name": "old",
        "trading_mode": "paper", "broker": {"trades": []},
    }), encoding="utf-8")
    view = snapshot.bot_summary(
        IN_SESSION, str(bot), pid_alive_fn=lambda pid: False)
    assert view["risk_off"]["enabled"] is False
    assert view["risk_off"]["label"] == "risk-off: off"
    assert view["risk_off"]["gate"] == "off"
    assert view["risk_off"]["heartbeat_age_sec"] is None


def test_dashboard_risk_off_shows_gate_and_heartbeat_age(tmp_path):
    bot = tmp_path / "TMF00_news"
    bot.mkdir()
    (bot / "session.json").write_text(json.dumps({
        "strategy": "Demo", "symbol": "TMF00", "bot_name": "news",
        "trading_mode": "auto", "broker": {"trades": []},
        "news_enabled": True,
        "news_tier2_enabled": True,
        "news_suppress_scope": "conflicting_leg",
        "news": {"signal_suppressed": True, "suppressed_reason": "cpi"},
        "news_health": {
            "gate": "suppressed",
            "gate_reason": "cpi",
            "entries_blocked": True,
            "entries_blocked_reason": "W2 heartbeat stale (1000s)",
            "heartbeat_age_sec": 1000,
            "heartbeat_status": "stale",
            "signal_age_sec": 12,
            "events_age_sec": 40,
            "votes_age_sec": None,
        },
    }), encoding="utf-8")
    view = snapshot.bot_summary(
        IN_SESSION, str(bot), pid_alive_fn=lambda pid: False)["risk_off"]
    assert view["label"] == "risk-off: on"
    assert view["enabled"] is True
    assert view["directional"] is True
    assert view["tier2"] is True
    assert view["gate"] == "suppressed"
    assert view["gate_reason"] == "cpi"
    assert view["blocks_entries"] is True
    assert view["heartbeat_age_sec"] == 1000
    assert view["heartbeat_status"] == "stale"
    assert view["sources"]["signal_age_sec"] == 12
    assert view["sources"]["events_age_sec"] == 40
    assert view["sources"]["votes_age_sec"] is None
    assert "stale" in view["entries_blocked_reason"]
