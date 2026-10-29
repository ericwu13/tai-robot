"""One recovery attempt per session while quote-stuck DEGRADED.

The retry key is ``session_for``'s open-date key, held on
``QuoteStuckRecovery`` for the process. LeaveMonitor / login are not
the recovery. Manual Reconnect is the existing machine path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from src.live.link_host import LinkHost
from src.live.link_state_machine import LinkMachineConfig, LinkPhase
from src.live.live_runner import session_for
from src.live.quote_stuck_recovery import QuoteStuckRecovery
from tests.test_link_state_machine import at, boot_until_entered

TPE = timezone(timedelta(hours=8))


def _t(y, mo, d, hh, mm=0) -> datetime:
    return datetime(y, mo, d, hh, mm, tzinfo=TPE)


def _consider(recovery, now, *, quote_state=1, reason="stuck", degraded=True):
    return recovery.consider(
        degraded=degraded,
        reason=reason,
        quote_state=quote_state,
        now=now,
    )


def _stuck_quote():
    """Quote EnterMonitor never reaches 3003, so IsConnected stays 2."""
    cfg = LinkMachineConfig(stuck_limit_s=5, probe_interval_s=60)
    machine, fake, t0 = boot_until_entered(cfg=cfg)
    at(machine, fake, t0)
    at(machine, fake, t0 + timedelta(seconds=5))
    assert machine.degraded and machine.degraded_reason == "stuck"
    assert machine.quote == LinkPhase.DOWN
    assert machine.reply == LinkPhase.READY and machine.reply_reconciled
    return machine, fake, t0


def test_one_retry_per_session_key_and_the_next_open_retries():
    recovery = QuoteStuckRecovery()
    day = _t(2026, 10, 27, 10)
    later = _t(2026, 10, 27, 11, 30)
    night = _t(2026, 10, 27, 15)
    assert session_for(day) == "2026-10-27|DAY"
    assert session_for(later) == "2026-10-27|DAY"
    assert session_for(night) == "2026-10-27|NIGHT"

    assert _consider(recovery, day) == "resubscribe"
    assert recovery.attempted_key == "2026-10-27|DAY"
    # Ticks came back, then the feed stuck again in the same session.
    assert recovery.on_live_tick() is True
    assert _consider(recovery, later) == "skip"
    assert recovery.attempted_key == "2026-10-27|DAY"

    assert _consider(recovery, night) == "resubscribe"
    assert recovery.attempted_key == "2026-10-27|NIGHT"


def test_second_stuck_episode_without_ticks_does_not_retry():
    recovery = QuoteStuckRecovery()
    first = _t(2026, 10, 27, 9)
    second = _t(2026, 10, 27, 12)
    assert _consider(recovery, first) == "resubscribe"
    assert _consider(recovery, second) == "skip"
    assert recovery.on_live_tick() is True
    assert _consider(recovery, second) == "skip"


@pytest.mark.parametrize("quote_state", [1, 2])
def test_link_up_states_resubscribe_once(quote_state):
    recovery = QuoteStuckRecovery()
    now = _t(2026, 10, 27, 10)
    assert _consider(recovery, now, quote_state=quote_state) == "resubscribe"
    assert _consider(recovery, now, quote_state=quote_state) == "skip"


def test_down_link_and_other_reasons_do_not_retry():
    recovery = QuoteStuckRecovery()
    now = _t(2026, 10, 27, 10)
    assert _consider(recovery, now, quote_state=0) == "skip"
    assert _consider(recovery, now, quote_state=None) == "skip"
    assert _consider(recovery, now, reason="login_failures") == "skip"
    assert _consider(recovery, now, degraded=False) == "skip"
    assert recovery.attempted_key is None
    assert _consider(recovery, now) == "resubscribe"


def test_night_tail_is_one_session_key():
    recovery = QuoteStuckRecovery()
    opened = _t(2026, 10, 23, 16)
    after_midnight = _t(2026, 10, 24, 2)
    assert session_for(opened) == session_for(after_midnight) == "2026-10-23|NIGHT"
    assert _consider(recovery, opened) == "resubscribe"
    assert _consider(recovery, after_midnight) == "skip"
    assert recovery.attempted_key == "2026-10-23|NIGHT"


def test_no_retry_on_a_holiday_and_the_next_open_still_retries():
    recovery = QuoteStuckRecovery()
    holiday = _t(2026, 10, 26, 10)
    nxt = _t(2026, 10, 27, 9)
    assert session_for(holiday) is None
    assert _consider(recovery, holiday) == "skip"
    assert recovery.attempted_key is None
    assert _consider(recovery, nxt) == "resubscribe"
    assert recovery.attempted_key == "2026-10-27|DAY"


def test_no_retry_on_a_closures_file_date(tmp_path, monkeypatch):
    import src.market_data.holidays as hol

    path = tmp_path / "taifex_closures.yaml"
    path.write_text("2026-04-16\n", encoding="utf-8")
    monkeypatch.setattr(hol, "closures_override_path", lambda: path)
    hol._closures_cache = None
    hol._logged_closure_dates.clear()
    try:
        closed = _t(2026, 4, 16, 10)
        nxt = _t(2026, 4, 17, 10)
        assert session_for(closed) is None
        assert session_for(nxt) == "2026-04-17|DAY"
        recovery = QuoteStuckRecovery()
        assert _consider(recovery, closed) == "skip"
        assert recovery.attempted_key is None
        assert _consider(recovery, nxt) == "resubscribe"
    finally:
        hol._closures_cache = None
        hol._logged_closure_dates.clear()


def test_attempt_and_tick_outcome_are_logged(caplog):
    recovery = QuoteStuckRecovery()
    now = _t(2026, 10, 27, 10)
    with caplog.at_level(logging.INFO, logger="src.live.quote_stuck_recovery"):
        assert _consider(recovery, now, quote_state=2) == "resubscribe"
        assert recovery.on_live_tick() is True
    assert "resubscribe once for 2026-10-27|DAY" in caplog.text
    assert "IsConnected=2" in caplog.text
    assert "no reconnect" in caplog.text
    assert "ticks resumed for 2026-10-27|DAY" in caplog.text
    assert "clearing DEGRADED" in caplog.text


def test_successful_resubscribe_clears_degraded_without_reconnect():
    machine, fake, t0 = _stuck_quote()
    recovery = QuoteStuckRecovery()
    assert _consider(recovery, _t(2026, 10, 27, 10), quote_state=2) == "resubscribe"
    before = list(fake.calls)
    assert recovery.on_live_tick() is True
    alert = machine.clear_stuck_after_ticks()
    assert alert == "RECOVERED: ticks resumed after quote resubscribe"
    assert not machine.degraded
    assert machine.degraded_reason == ""
    assert machine.quote == LinkPhase.READY
    assert not machine.entries_blocked
    assert not machine.orders_paused
    assert fake.calls == before
    fake.force_quote_state = 2
    at(machine, fake, t0 + timedelta(seconds=6))
    assert not machine.degraded
    assert fake.calls.count("EnterMonitorLONG") == 1
    assert fake.calls.count("LoginSetQuote") == 1
    assert "LeaveMonitor" not in fake.calls
    assert "LogOut" not in fake.calls
    assert "RestartProcess" not in fake.calls
    assert "SubscribeQuotes" not in fake.calls


def test_manual_reconnect_path_is_unchanged():
    """Manual Reconnect still leaves DEGRADED without a resubscribe."""
    machine, fake, t0 = _stuck_quote()
    enters = fake.calls.count("EnterMonitorLONG")
    logins = fake.calls.count("LoginSetQuote")
    connects = fake.calls.count("ConnectByID")
    press = t0 + timedelta(seconds=6)
    machine.manual_reconnect(press)
    at(machine, fake, press)
    assert not machine.degraded
    assert machine.entries_blocked
    assert machine.quote == LinkPhase.DOWN
    assert fake.calls.count("EnterMonitorLONG") == enters
    assert fake.calls.count("LoginSetQuote") == logins
    assert fake.calls.count("ConnectByID") == connects
    assert "LeaveMonitor" not in fake.calls
    assert "LogOut" not in fake.calls
    assert "RestartProcess" not in fake.calls
    assert "SubscribeQuotes" not in fake.calls
    assert not any(a.startswith("RECOVERED") for a in machine.alerts)


def test_host_sends_request_ticks_once_per_session(monkeypatch):
    from run_backtest import BacktestApp

    app = BacktestApp.__new__(BacktestApp)
    app._quote_stuck = QuoteStuckRecovery()
    app._live_runner = object()
    sent = []
    app._resubscribe_ticks = lambda *a, **k: sent.append("ticks")
    app._link_host = type("H", (), {})()
    app._link_host.machine = type("M", (), {})()
    app._link_host.machine.degraded = True
    app._link_host.machine.degraded_reason = "stuck"
    app._link_host.machine.last_quote_is_connected = 1

    def at_time(stamp):
        monkeypatch.setattr("run_backtest._taipei_now", lambda s=stamp: s)

    at_time(_t(2026, 10, 27, 10))
    app._maybe_quote_stuck_resubscribe()
    app._maybe_quote_stuck_resubscribe()
    assert sent == ["ticks"]

    at_time(_t(2026, 10, 26, 10))  # holiday, does not consume the next key
    app._link_host.machine.last_quote_is_connected = 2
    app._maybe_quote_stuck_resubscribe()
    assert sent == ["ticks"]

    at_time(_t(2026, 10, 27, 15))
    app._maybe_quote_stuck_resubscribe()
    assert sent == ["ticks", "ticks"]


def test_clear_stuck_ignores_other_degrade_reasons():
    from tests.test_link_state_machine import pair

    machine, _fake = pair()
    machine.degraded = True
    machine.degraded_reason = "login_failures"
    machine.entries_blocked = True
    machine.orders_paused = True
    machine.quote = LinkPhase.READY
    assert machine.clear_stuck_after_ticks() is None
    assert machine.degraded
    assert machine.degraded_reason == "login_failures"
    assert machine.entries_blocked
    assert machine.orders_paused
    assert machine.quote == LinkPhase.READY


def test_clear_stuck_does_not_unblock_until_reply_reconciled():
    from tests.test_link_state_machine import pair

    machine, _fake = pair()
    machine.degraded = True
    machine.degraded_reason = "stuck"
    machine.entries_blocked = True
    machine.orders_paused = True
    machine.quote = LinkPhase.DOWN
    machine.reply = LinkPhase.READY
    machine.reply_reconciled = False
    alert = machine.clear_stuck_after_ticks()
    assert alert == "RECOVERED: ticks resumed after quote resubscribe"
    assert not machine.degraded
    assert machine.quote == LinkPhase.READY
    assert machine.entries_blocked
    assert machine.orders_paused


def test_live_tick_consumes_the_awaiting_flag():
    recovery = QuoteStuckRecovery()
    assert _consider(recovery, _t(2026, 10, 27, 10)) == "resubscribe"
    assert recovery.on_live_tick() is True
    assert recovery.on_live_tick() is False


def test_entering_degraded_clears_the_previous_quote_probe():
    from tests.test_link_state_machine import pair

    machine, _fake = pair()
    machine.last_quote_is_connected = 2
    machine._degrade(_t(2026, 10, 27, 10), "stuck")
    assert machine.degraded
    assert machine.degraded_reason == "stuck"
    assert machine.last_quote_is_connected is None


def test_host_live_tick_clears_degraded():
    from run_backtest import BacktestApp

    machine, fake, _t0 = _stuck_quote()
    app = BacktestApp.__new__(BacktestApp)
    app._quote_stuck = QuoteStuckRecovery()
    app._link_gate_latched = False
    app._link_host = LinkHost(machine)
    assert _consider(app._quote_stuck, _t(2026, 10, 27, 10), quote_state=2) == "resubscribe"
    before = list(fake.calls)
    app._clear_quote_stuck_if_ticks_resumed()
    assert not machine.degraded
    assert machine.quote == LinkPhase.READY
    assert fake.calls == before
    app._clear_quote_stuck_if_ticks_resumed()
    assert fake.calls == before
    assert not machine.degraded
