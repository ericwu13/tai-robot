"""Tests for the read-only monitoring checks (scripts/monitor/).

Every case injects ``now`` (and ``pid_alive_fn`` where liveness matters)
so nothing depends on the wall clock, on live processes, or on
settings.yaml.

Anchor time: Tuesday 2026-08-25 06:00 TPE — inside the post-night closed
gap, so ``last_completed_night`` is ``2026-08-24|NIGHT`` and the next
classification targets ``2026-08-25|NIGHT``.  The tests are written
against those known-bad shapes (a stale ``last_assessed``, a latched
suppression key from 08-20, a naive ``issued_at``) so they fail if the
checks stop noticing them.
"""

import json
from datetime import datetime, timedelta

import pytest

from scripts.monitor.check_bots import check_bots
from scripts.monitor.check_bridge import check_bridge
from scripts.monitor.check_regime import check_regime
from scripts.monitor.common import TZ_TPE

NOW = datetime(2026, 8, 25, 6, 0, tzinfo=TZ_TPE)           # closed gap
NOW_IN_SESSION = datetime(2026, 8, 25, 16, 0, tzinfo=TZ_TPE)  # NIGHT open
LAST_NIGHT = "2026-08-24|NIGHT"
TONIGHT = "2026-08-25|NIGHT"


def levels(findings):
    return [f.level for f in findings]


def messages(findings, level=None):
    return [f.message for f in findings if level is None or f.level == level]


def has_level(findings, level):
    return any(f.level == level for f in findings)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ── check_regime ────────────────────────────────────────────────────────

HEALTHY_STATE = {
    "effective_regime": "trending-down",
    "effective_since": "2026-08-14",
    "raw_regime": "trending-down",
    "pending_label": "",
    "pending_count": 0,
    "flip_history": ["2026-08-14"],
    "flip_sessions": [9],
    "paused_until_session": 10,
    "session_count": 16,
    "manual_override": "auto",
    "last_assessed": LAST_NIGHT,
    "last_features": {"label": "trending-down", "_vote_sources": ["W3:trending-down"]},
    "key_format": "open-date",
    "next_session": {
        "date": "2026-08-24",
        "action": "deploy_short",
        "strategy": "AI: BbandSmaShortV3",
        "reason": "trending-down confirmed",
        "executed": True,
        "executed_at": "2026-08-25 05:00:10",
    },
}


def make_regime_bot(tmp_path, state, name="TMF00_bot"):
    """A directory whose CURRENT deploy is a regime bot.

    session.json's ``regime_mode`` is what separates a live regime bot
    from a plain deploy that inherited a leftover regime_state.json — a
    fixture without it would be graded as archaeology (everything
    demoted to P3), so the regime assertions below would pass vacuously.
    """
    bot = tmp_path / "live" / name
    write_json(bot / "regime_state.json", state)
    write_json(bot / "session.json", {
        "regime_mode": True, "strategy": "S", "trading_mode": "paper"})
    return bot


def test_regime_healthy_state_has_no_p1_or_p2(tmp_path):
    make_regime_bot(tmp_path, HEALTHY_STATE)
    findings, lines = check_regime(NOW, str(tmp_path / "live"))
    assert not has_level(findings, "P1"), messages(findings, "P1")
    assert not has_level(findings, "P2"), messages(findings, "P2")
    assert any("W3:trending-down" in ln for ln in lines)


def test_regime_stale_last_assessed_is_p1(tmp_path):
    state = dict(HEALTHY_STATE, last_assessed="2026-08-21|NIGHT")
    make_regime_bot(tmp_path, state)
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    p1 = messages(findings, "P1")
    assert p1, levels(findings)
    assert any(LAST_NIGHT in m and "classification missed" in m for m in p1)


def test_regime_deploy_placeholder_is_p2(tmp_path):
    make_regime_bot(tmp_path, {"regime": "unknown", "classified_at": None})
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert not has_level(findings, "P1")
    assert any("never classified" in m for m in messages(findings, "P2"))


def test_regime_pre_v216_key_format_is_p2(tmp_path):
    state = dict(HEALTHY_STATE)
    state.pop("key_format")
    make_regime_bot(tmp_path, state)
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert any("key format" in m for m in messages(findings, "P2"))


def test_regime_flip_pause_active_is_p2(tmp_path):
    state = dict(HEALTHY_STATE, paused_until_session=20, session_count=16)
    make_regime_bot(tmp_path, state)
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert any("flip-counter pause" in m for m in messages(findings, "P2"))


def test_regime_latched_news_suppression_is_p1(tmp_path):
    """A suppression key from a night that closed days ago = expiry is dead."""
    bot = make_regime_bot(tmp_path, HEALTHY_STATE)
    write_json(bot / "session.json", {
        "strategy": "AI: BbandSmaShortV3",
        "trading_mode": "semi_auto",
        "regime_mode": True,
        "news": {
            "signal_suppressed": True,
            "event_suppressed": False,
            "suppressed_reason": "risk_off: SOXX -2.6%",
            "signal_session_key": "2026-08-20|NIGHT",
        },
    })
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    p1 = messages(findings, "P1")
    assert any("LATCHED" in m and "2026-08-20|NIGHT" in m for m in p1), p1


def test_regime_current_suppression_is_only_informational(tmp_path):
    bot = make_regime_bot(tmp_path, HEALTHY_STATE)
    write_json(bot / "session.json", {
        "regime_mode": True,
        "news": {
            "signal_suppressed": True,
            "suppressed_reason": "risk_off: SOXX -2.6%",
            "signal_session_key": TONIGHT,
        },
    })
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert not has_level(findings, "P1"), messages(findings, "P1")
    assert any("by design" in m for m in messages(findings, "P3"))


def test_regime_news_suppressed_flood_is_p2(tmp_path):
    bot = make_regime_bot(tmp_path, HEALTHY_STATE)
    rows = ["datetime,bar_dt,strategy,action,side,tag,price,reason"]
    for i in range(30):
        stamp = (NOW - timedelta(hours=2, minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(f"{stamp},2026-08-25 03:00,S,NEWS_SUPPRESSED,,,0,news gate")
    (bot / "decisions.csv").write_text("\n".join(rows), encoding="utf-8")
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert any("swallowed 30 bars" in m for m in messages(findings, "P2"))


def test_regime_decision_window_uses_bar_dt_not_local_clock(tmp_path):
    """decisions.csv column 0 is stamped with the MACHINE clock (UTC-7 on
    this box), 15h behind the TPE bar_dt.  A flood 10h ago is inside the
    24h window by bar_dt, but its column-0 stamp — misread as TPE — looks
    25h old.  The count must key on bar_dt (column 1)."""
    bot = make_regime_bot(tmp_path, HEALTHY_STATE)
    rows = ["datetime,bar_dt,strategy,action,side,tag,price,reason"]
    bar_dt = (NOW - timedelta(hours=10)).strftime("%Y-%m-%d %H:%M")
    local_stamp = (NOW - timedelta(hours=10 + 15)).strftime("%Y-%m-%d %H:%M:%S")
    for _ in range(30):
        rows.append(f"{local_stamp},{bar_dt},S,NEWS_SUPPRESSED,,,0,news gate")
    (bot / "decisions.csv").write_text("\n".join(rows), encoding="utf-8")
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert any("swallowed 30 bars" in m for m in messages(findings, "P2"))


def test_regime_old_news_suppressed_rows_are_ignored(tmp_path):
    """Only the last 24h counts — a week-old flood is not today's problem."""
    bot = make_regime_bot(tmp_path, HEALTHY_STATE)
    rows = ["datetime,bar_dt,strategy,action,side,tag,price,reason"]
    for i in range(30):
        stamp = (NOW - timedelta(days=5, minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(f"{stamp},2026-08-20 03:00,S,NEWS_SUPPRESSED,,,0,news gate")
    (bot / "decisions.csv").write_text("\n".join(rows), encoding="utf-8")
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert not any("swallowed" in m for m in messages(findings, "P2"))


def test_regime_corrupt_state_is_p1(tmp_path):
    bot = tmp_path / "live" / "TMF00_bot"
    bot.mkdir(parents=True)
    (bot / "regime_state.json").write_text("{not json", encoding="utf-8")
    write_json(bot / "session.json", {"regime_mode": True})
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert any("corrupt" in m for m in messages(findings, "P1"))


def test_regime_plain_deploy_with_leftover_state_is_only_p3(tmp_path):
    """A LIVE plain-deploy bot redeployed over an old regime bot's
    directory: regime_state.json is a leftover, so its stale
    last_assessed is archaeology, not a missed classification."""
    state = dict(HEALTHY_STATE, last_assessed="2026-08-07|NIGHT")
    state.pop("key_format")
    bot = make_regime_bot(tmp_path, state)
    write_json(bot / "session.json", {          # plain deploy: no regime_mode
        "strategy": "AI: BbandSmaShortV3", "trading_mode": "semi_auto",
        "saved_at": "2026-08-25T05:55:00",
    })
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert not has_level(findings, "P1"), messages(findings, "P1")
    assert not has_level(findings, "P2"), messages(findings, "P2")
    assert any("[non-regime deploy]" in m for m in messages(findings, "P3")), \
        messages(findings, "P3")


def test_regime_stale_state_on_a_real_regime_deploy_is_still_p1(tmp_path):
    """Regression guard for the demotion above: the SAME stale state on a
    bot whose session.json says regime_mode must still raise P1."""
    make_regime_bot(tmp_path, dict(HEALTHY_STATE, last_assessed="2026-08-07|NIGHT"))
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert any("classification missed" in m for m in messages(findings, "P1")), \
        levels(findings)


def test_regime_state_without_session_json_is_demoted(tmp_path):
    """No session.json at all = nothing is deployed here now."""
    bot = tmp_path / "live" / "TMF00_bot"
    write_json(bot / "regime_state.json",
               dict(HEALTHY_STATE, last_assessed="2026-08-07|NIGHT"))
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert not has_level(findings, "P1"), messages(findings, "P1")
    assert any("[non-regime deploy]" in m for m in messages(findings, "P3"))


def test_regime_missing_session_pnl_is_p2(tmp_path):
    """A classification row whose session closed hours ago must carry P&L.

    The 2026-08-24 night closed at 2026-08-25 05:00, so this asserts at
    08:00 — past the 2h recording grace, still inside the pre-DAY gap.
    """
    now = datetime(2026, 8, 25, 8, 0, tzinfo=TZ_TPE)
    bot = make_regime_bot(tmp_path, HEALTHY_STATE)
    header = ("date,session,adx,plus_di,minus_di,atr_ratio,ema_slope,close,"
              "raw_regime,effective_regime,confirm_count,decision,"
              "strategy_deployed,dry_run,override,pnl,trades,strategy_active,"
              "applied,applied_at,trading_mode,votes")
    row = ("2026-08-24,NIGHT,19.2,15.7,24.0,1.03,-73.0,44512.0,range-bound,"
           "trending-down,1,deploy_short,AI: BbandSmaShortV3,,auto,,,"
           "AI: BbandSmaShortV3,false,,semi_auto,W3:trending-down")
    (bot / "regime_history.csv").write_text(f"{header}\n{row}\n", encoding="utf-8")
    findings, _ = check_regime(now, str(tmp_path / "live"))
    assert any("P&L never recorded" in m for m in messages(findings, "P2"))


def test_regime_history_pnl_present_is_clean(tmp_path):
    bot = make_regime_bot(tmp_path, HEALTHY_STATE)
    header = ("date,session,adx,plus_di,minus_di,atr_ratio,ema_slope,close,"
              "raw_regime,effective_regime,confirm_count,decision,"
              "strategy_deployed,dry_run,override,pnl,trades,strategy_active,"
              "applied,applied_at,trading_mode,votes")
    row = ("2026-08-24,NIGHT,19.2,15.7,24.0,1.03,-73.0,44512.0,range-bound,"
           "trending-down,1,deploy_short,AI: BbandSmaShortV3,,auto,1250.0,2,"
           "AI: BbandSmaShortV3,false,,semi_auto,W3:trending-down")
    (bot / "regime_history.csv").write_text(f"{header}\n{row}\n", encoding="utf-8")
    findings, lines = check_regime(NOW, str(tmp_path / "live"))
    assert not has_level(findings, "P2"), messages(findings, "P2")
    assert any("W3:trending-down" in ln for ln in lines)


# ── check_bots ──────────────────────────────────────────────────────────

def make_live_bot(tmp_path, pid=4242, log_date="20260825", log_time="15:55:00",
                  name="TMF00_bot"):
    bot = tmp_path / "live" / name
    bot.mkdir(parents=True)
    (bot / ".lock").write_text(str(pid), encoding="utf-8")
    (bot / f"debug_{log_date}.log").write_text(
        f"[{log_date[:4]}-{log_date[4:6]}-{log_date[6:]} {log_time}.100 TPE / "
        f"2026-08-25 00:55:00.100 local] tick\n", encoding="utf-8")
    write_json(bot / "session.json", {
        "strategy": "AI: BbandSmaShortV3", "trading_mode": "semi_auto",
        "saved_at": "2026-08-25T15:55:00",
        "broker": {"_cumulative_pnl": -390, "position_size": 0,
                   "position_side": None, "trades": []},
    })
    return bot


def test_bots_dead_pid_lock_is_p1(tmp_path):
    make_live_bot(tmp_path, pid=99999)
    findings, _ = check_bots(NOW_IN_SESSION, str(tmp_path / "live"),
                             pid_alive_fn=lambda pid: False)
    p1 = messages(findings, "P1")
    assert any("dead PID" in m and "crashed" in m for m in p1), p1


def test_bots_dead_pid_lock_is_never_deleted(tmp_path):
    bot = make_live_bot(tmp_path, pid=99999)
    check_bots(NOW_IN_SESSION, str(tmp_path / "live"),
               pid_alive_fn=lambda pid: False)
    assert (bot / ".lock").exists(), "monitoring must never delete a lock"


def test_bots_alive_pid_with_fresh_log_in_session_is_clean(tmp_path):
    make_live_bot(tmp_path, pid=4242, log_time="15:55:00")
    findings, _ = check_bots(NOW_IN_SESSION, str(tmp_path / "live"),
                             pid_alive_fn=lambda pid: True)
    assert not has_level(findings, "P1"), messages(findings, "P1")


def test_bots_frozen_log_during_session_is_p1(tmp_path):
    make_live_bot(tmp_path, pid=4242, log_time="14:00:00")  # 2h before now
    findings, _ = check_bots(NOW_IN_SESSION, str(tmp_path / "live"),
                             pid_alive_fn=lambda pid: True)
    assert any("frozen mid-session" in m for m in messages(findings, "P1"))


def test_bots_frozen_log_in_closed_gap_is_not_p1(tmp_path):
    """Silence between sessions is normal — no ticks flow."""
    make_live_bot(tmp_path, pid=4242, log_date="20260824", log_time="23:00:00")
    findings, _ = check_bots(NOW, str(tmp_path / "live"),
                             pid_alive_fn=lambda pid: True)
    assert not has_level(findings, "P1"), messages(findings, "P1")


def test_bots_unparsable_lock_is_p2(tmp_path):
    bot = tmp_path / "live" / "TMF00_bot"
    bot.mkdir(parents=True)
    (bot / ".lock").write_text("garbage", encoding="utf-8")
    findings, _ = check_bots(NOW_IN_SESSION, str(tmp_path / "live"),
                             pid_alive_fn=lambda pid: True)
    assert any("unparsable lock" in m for m in messages(findings, "P2"))


def _timeout_rows(bar_dts):
    rows = ["datetime,bar_dt,strategy,action,side,tag,price,reason"]
    for bd in bar_dts:
        rows.append(f"2026-08-24 14:55:00,{bd},S,REAL_ORDER_TIMEOUT,,,0,timeout 10s")
    return "\n".join(rows)


def test_bots_recent_order_timeouts_are_p2_even_on_plain_deploys(tmp_path):
    """REAL_ORDER_TIMEOUT = the semi_auto confirm dialog auto-skipped after
    10s and the signal ran PAPER-only (the order was never sent — see
    run_backtest._dismiss_order_dialog and the trade-source downgrade in
    test_trade_source_and_hotswap).  It is deployment-agnostic, so it must
    live in check_bots: check_regime demotes every finding for plain
    deploys (the TMF00_0422 case) and no longer scans it at all."""
    bot = make_live_bot(tmp_path)  # plain deploy: session has no regime_mode
    recent = (NOW - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    (bot / "decisions.csv").write_text(_timeout_rows([recent, recent]),
                                       encoding="utf-8")
    findings, _ = check_bots(NOW, str(tmp_path / "live"),
                             pid_alive_fn=lambda pid: True)
    assert any("timed out 2x" in m and "PAPER-only" in m
               for m in messages(findings, "P2")), messages(findings)


def test_bots_old_order_timeouts_are_ignored(tmp_path):
    """Only the last 24h (keyed on bar_dt, TPE) counts."""
    bot = make_live_bot(tmp_path)
    old = (NOW - timedelta(days=4)).strftime("%Y-%m-%d %H:%M")
    (bot / "decisions.csv").write_text(_timeout_rows([old, old]),
                                       encoding="utf-8")
    findings, _ = check_bots(NOW, str(tmp_path / "live"),
                             pid_alive_fn=lambda pid: True)
    assert not any("timed out" in m for m in messages(findings, "P2")), \
        messages(findings, "P2")


def test_regime_no_longer_flags_order_timeouts(tmp_path):
    """Moved to check_bots — check_regime must not double-report it, and
    its NEWS_SUPPRESSED scan must be unaffected."""
    bot = make_regime_bot(tmp_path, HEALTHY_STATE)
    recent = (NOW - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    (bot / "decisions.csv").write_text(_timeout_rows([recent]),
                                       encoding="utf-8")
    findings, _ = check_regime(NOW, str(tmp_path / "live"))
    assert not any("REAL_ORDER_TIMEOUT" in m or "timed out" in m
                   for m in messages(findings)), messages(findings)


def test_bots_corrupt_session_json_is_p2(tmp_path):
    bot = make_live_bot(tmp_path, pid=4242)
    (bot / "session.json").write_text("{broken", encoding="utf-8")
    findings, _ = check_bots(NOW_IN_SESSION, str(tmp_path / "live"),
                             pid_alive_fn=lambda pid: True)
    assert any("session.json unreadable" in m for m in messages(findings, "P2"))


# ── check_bridge ────────────────────────────────────────────────────────

def iso(dt):
    return dt.isoformat()


@pytest.fixture
def bridge(tmp_path):
    """A healthy bridge directory + the settings that point at it."""
    d = tmp_path / "n8n-bridge"
    d.mkdir()
    write_json(d / "monitor_state.json",
               {"last_check": iso(NOW - timedelta(minutes=10)),
                "last_result": "fresh 5/11 | fired: none"})
    write_json(d / "rss_scorer_state.json",
               {"last_check": iso(NOW - timedelta(minutes=20)),
                "session_net": 1.09, "session_peak": 2.29})
    write_json(d / ".chips_state.json",
               {"last_check": iso(NOW - timedelta(hours=14)),
                "last_result": "vote trending-up"})
    write_json(d / "regime_vote_w3.json",
               {"source": "W3", "direction": "trending-up",
                "expires_after_session": TONIGHT})
    write_json(d / "events.json",
               {"version": 1, "updated_at": iso(NOW - timedelta(hours=6)),
                "events": [{"date": "2026-08-27", "name": "NVDA earnings",
                            "severity": "high", "sessions": ["NIGHT"]}]})
    (d / "monitor.log").write_text(
        f"{(NOW - timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')} TPE | "
        f"fresh 5/11 | fired: none | vote: -\n", encoding="utf-8")
    (d / "chips_monitor.log").write_text(
        "2026-08-24 16:15:04 TPE | vote trending-up (外資 +6,200)\n"
        "2026-08-24 18:15:04 TPE | no vote — hysteresis band\n",
        encoding="utf-8")
    return d


def settings_for(bridge_dir):
    return {"news": {
        "signal_path": str(bridge_dir / "signal.json"),
        "regime_vote_path": str(bridge_dir / "regime_vote.json"),
        "events_path": str(bridge_dir / "events.json"),
        "rss_state_file": str(bridge_dir / "rss_scorer_state.json"),
    }}


def test_bridge_healthy_has_no_p1_or_p2(tmp_path, bridge):
    findings, lines = check_bridge(NOW, settings_for(bridge),
                                   str(tmp_path / "live"))
    assert not has_level(findings, "P1"), messages(findings, "P1")
    assert not has_level(findings, "P2"), messages(findings, "P2")
    assert any("trending-up" in ln for ln in lines)
    assert any(TONIGHT in ln for ln in lines)


def test_bridge_missing_vote_file_is_not_a_finding(tmp_path, bridge):
    """Vote ABSENCE is the normal resting state (consumed and deleted)."""
    (bridge / "regime_vote_w3.json").unlink()
    findings, _ = check_bridge(NOW, settings_for(bridge), str(tmp_path / "live"))
    assert not has_level(findings, "P1"), messages(findings, "P1")
    assert not has_level(findings, "P2"), messages(findings, "P2")


def test_bridge_stale_w3_sidecar_is_p2(tmp_path, bridge):
    """W3's threshold is 2h — a 3h-old last_check means the cron died."""
    write_json(bridge / "rss_scorer_state.json",
               {"last_check": iso(NOW - timedelta(hours=3)), "session_net": 0.0})
    findings, _ = check_bridge(NOW, settings_for(bridge), str(tmp_path / "live"))
    assert any(m.startswith("W3 bridge stale") for m in messages(findings, "P2"))


def test_bridge_naive_issued_at_is_p2(tmp_path, bridge):
    """A naive stamp is a WRITER bug — the reader rejects it outright."""
    write_json(bridge / "signal.json", {
        "version": 1, "signal_id": "abc-123",
        "issued_at": (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S"),
        "action": "risk_off", "severity": "high", "source": "crossmarket",
    })
    findings, _ = check_bridge(NOW, settings_for(bridge), str(tmp_path / "live"))
    assert any("no timezone" in m for m in messages(findings, "P2"))


def test_bridge_stale_signal_file_is_healthy(tmp_path, bridge):
    write_json(bridge / "signal.json", {
        "version": 1, "signal_id": "abc-123",
        "issued_at": iso(NOW - timedelta(hours=40)),
        "action": "risk_off", "severity": "high", "source": "crossmarket",
    })
    findings, lines = check_bridge(NOW, settings_for(bridge),
                                   str(tmp_path / "live"))
    assert not has_level(findings, "P1"), messages(findings, "P1")
    assert not has_level(findings, "P2"), messages(findings, "P2")
    assert any("healthy resting state" in ln for ln in lines)


def _write_fresh_signal(bridge, signal_id="sig-fresh-1"):
    write_json(bridge / "signal.json", {
        "version": 1, "signal_id": signal_id,
        "issued_at": iso(NOW - timedelta(minutes=12)),
        "action": "risk_off", "severity": "high",
        "source": "crossmarket-monitor", "reason": "SOXX -2.6%",
        "direction": "bearish",
    })


def test_bridge_fresh_signal_consumed_by_nobody_is_p1(tmp_path, bridge):
    _write_fresh_signal(bridge)
    (tmp_path / "live" / "TMF00_bot").mkdir(parents=True)
    write_json(tmp_path / "live" / "TMF00_bot" / "session.json", {})
    findings, _ = check_bridge(NOW, settings_for(bridge), str(tmp_path / "live"))
    assert any("consumed by NO bot" in m for m in messages(findings, "P1"))


def test_bridge_fresh_signal_in_a_ledger_is_not_p1(tmp_path, bridge):
    _write_fresh_signal(bridge)
    bot = tmp_path / "live" / "TMF00_bot"
    write_json(bot / "session.json", {})
    write_json(bot / "news_ledger.json",
               {"version": 1, "consumed": ["older-id", "sig-fresh-1"]})
    findings, lines = check_bridge(NOW, settings_for(bridge),
                                   str(tmp_path / "live"))
    assert not has_level(findings, "P1"), messages(findings, "P1")
    assert any("TMF00_bot" in ln for ln in lines)


def test_bridge_chips_voteless_streak_is_p2(tmp_path, bridge):
    (bridge / "chips_monitor.log").write_text(
        "2026-08-20 16:15:04 TPE | vote trending-up\n"
        "2026-08-21 18:15:04 TPE | no TAIFEX data — no vote (weekend/holiday/outage)\n"
        "2026-08-22 16:15:04 TPE | no TAIFEX data — no vote (weekend/holiday/outage)\n"
        "2026-08-24 16:15:04 TPE | no TAIFEX data — no vote (weekend/holiday/outage)\n",
        encoding="utf-8")
    findings, _ = check_bridge(NOW, settings_for(bridge), str(tmp_path / "live"))
    assert any("voteless for 3" in m for m in messages(findings, "P2"))


def test_bridge_chips_only_primary_slot_flags_missing_retries(tmp_path, bridge):
    (bridge / "chips_monitor.log").write_text(
        "2026-08-20 16:15:04 TPE | vote trending-up\n"
        "2026-08-21 16:15:04 TPE | no vote — hysteresis band\n"
        "2026-08-24 16:15:04 TPE | vote trending-down\n",
        encoding="utf-8")
    findings, _ = check_bridge(NOW, settings_for(bridge), str(tmp_path / "live"))
    assert any("retry crons leave no trace" in m for m in messages(findings, "P2"))


def test_bridge_stale_event_calendar_is_p2(tmp_path, bridge):
    write_json(bridge / "events.json",
               {"version": 1, "updated_at": iso(NOW - timedelta(days=30)),
                "events": []})
    findings, _ = check_bridge(NOW, settings_for(bridge), str(tmp_path / "live"))
    assert any("event calendar stale" in m for m in messages(findings, "P2"))


def test_bridge_silent_w2_monitor_is_p2(tmp_path, bridge):
    (bridge / "monitor.log").write_text(
        f"{(NOW - timedelta(hours=5)).strftime('%Y-%m-%d %H:%M:%S')} TPE | "
        f"fresh 5/11 | fired: none | vote: -\n", encoding="utf-8")
    findings, _ = check_bridge(NOW, settings_for(bridge), str(tmp_path / "live"))
    assert any("W2 monitor silent" in m for m in messages(findings, "P2"))


def test_bridge_empty_regime_vote_path_does_not_crash(tmp_path, bridge):
    """The historical miswiring: votes are written but never consumed."""
    cfg = settings_for(bridge)
    cfg["news"]["regime_vote_path"] = ""
    findings, lines = check_bridge(NOW, cfg, str(tmp_path / "live"))
    assert lines
    assert isinstance(findings, list)
