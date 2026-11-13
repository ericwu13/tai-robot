"""3002 without a following 3003 must not look ready.

Live 2026-11-12 07:30 TPE: the reply server dropped (OnSolaceReplyDisconnect
code 3002), OnConnection(3002, 0) arrived, the reply link came back, and
no 3003 followed. ``_quote_connected`` was still True from the earlier
3003, so the 15s timeout returned without a log. The link machine keeps
3002 as a no-op while quote is already READY, the heartbeat kept
reporting quote ready, and the closed-gap watchdog does not poll
IsConnected. The quote sat "ready" until the day open.

These tests drive that sequence on a real BacktestApp drain plus the
link machine. They fail on the pre-fix code.
"""

from __future__ import annotations

import inspect
import json
import queue
import time
from datetime import datetime, timedelta, timezone

import pytest

import run_backtest as rb
import src.live.main_heartbeat as hb
from src.live.link_state_machine import LinkPhase
from src.live.live_runner import LiveState
from src.live.trading_guard import TradingGuard
from tests.test_link_host import boot_ready

TPE = timezone(timedelta(hours=8))
_FORBIDDEN = ("LeaveMonitor", "LoginSetQuote", "LogOut", "RestartProcess")


def tpe(y, mo, d, hh, mm, ss=0) -> datetime:
    return datetime(y, mo, d, hh, mm, ss, tzinfo=TPE)


class _Btn:
    def config(self, **kwargs):
        self.kwargs = kwargs


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Root:
    def __init__(self):
        self.pending = {}
        self._next = 1

    def after(self, ms, fn, *args):
        token = self._next
        self._next += 1
        self.pending[token] = (ms, fn, args)
        return token

    def after_cancel(self, token):
        self.pending.pop(token, None)


def _empty_ui_queue() -> None:
    while True:
        try:
            rb._ui_queue.get_nowait()
        except queue.Empty:
            return


def _app(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(rb, "_log", lambda msg: logged.append(str(msg)))
    monkeypatch.setattr(rb, "_log_debug", lambda msg: logged.append(str(msg)))
    _empty_ui_queue()
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = _Root()
    app._quote_connected = False
    app._quote_ready_timer_id = None
    app._quote_intermediate_seen_at = 0.0
    app._link_flags_pending = []
    app._link_gate_latched = False
    app._quote_stuck = None
    app._reply_watch = None
    app._reply_poller = None
    app._trading_mode = "paper"
    app._trading_guard = TradingGuard()
    app._trading_guard.real_entry_confirmed = True
    app._warn_disconnect_count = 0
    app._ticks_requested_at = 0.0
    app._first_tick_after_request_logged = False
    app._live_tick_active = False
    app._live_runner = None
    app._live_bar_builder = None
    app._reconnect_timer_id = None
    app.btn_api = _Btn()
    app.btn_deploy = _Btn()
    app.btn_login = _Btn()
    app.btn_reconnect = _Btn()
    app.login_status_var = _Var("")
    app._statuses: list[tuple] = []
    app._dots: list[str] = []
    app.set_status = lambda msg, level="info": app._statuses.append((msg, level))
    app._set_conn_dot = lambda state: app._dots.append(state)
    app._live_log_msg = lambda *a, **k: None
    return app, logged


def _bind_clock(monkeypatch, fake, start):
    clock = {"now": start}
    monkeypatch.setattr(rb, "_taipei_now", lambda: clock["now"])
    fake.set_now(start)
    return clock


def _tick(app, fake, clock, when):
    clock["now"] = when
    fake.set_now(when)
    app._link_host_tick(force=True)


def _running(app) -> None:
    """A RUNNING runner keeps the tick-plus-IsConnected timeout rule."""
    runner = _Runner()
    runner.state = LiveState.RUNNING
    app._live_runner = runner


def _ready(monkeypatch, when):
    """Adopted machine, quote READY, ``_quote_connected`` True."""
    host, fake, _t0 = boot_ready(t0=when)
    app, logged = _app(monkeypatch)
    clock = _bind_clock(monkeypatch, fake, when)
    app._link_host = host
    app._quote_connected = True
    return app, fake, clock, logged


def _replay_drop(app, fake, clock, drop):
    """Reply drop, quote 3002, reply back, OnComplete, orders released.

    Returns the Quote→Ready timer token. Reply events must not cancel it.
    """
    clock["now"] = drop
    fake.set_now(drop)
    fake.note_solace_reply_disconnect(drop)
    rb.SKReplyLibEvent().OnSolaceReplyDisconnect("USER", 3002)
    rb.SKQuoteLibEvents().OnConnection(3002, 0)
    rb.SKReplyLibEvent().OnSolaceReplyConnection("USER", 0)
    app._process_ui_queue()
    token = app._quote_ready_timer_id
    assert token is not None
    assert token in app.root.pending
    assert app.root.pending[token][0] == 15000
    # Stale flag from the earlier 3003. 3002 must not be treated as Ready.
    assert app._quote_connected is True
    _tick(app, fake, clock, drop)
    assert app._quote_ready_timer_id == token
    settled = drop + timedelta(seconds=10)
    _tick(app, fake, clock, settled)
    assert fake.calls.count("ConnectByID") >= 1
    fake.note_on_complete()
    rb.SKReplyLibEvent().OnComplete("USER")
    app._process_ui_queue()
    assert app._quote_ready_timer_id == token, (
        "OnComplete / OnSolaceReplyConnection cancelled the Quote→Ready timer"
    )
    _tick(app, fake, clock, settled)
    assert fake.orders_released
    assert app._link_host.machine.orders_paused is False
    assert app._link_host.machine.quote == LinkPhase.READY
    assert app._quote_ready_timer_id == token
    return token


def _fire_timeout(app, fake, clock, when):
    clock["now"] = when
    fake.set_now(when)
    app._quote_intermediate_seen_at = time.time() - 15.2
    token = app._quote_ready_timer_id
    assert token is not None
    _ms, fn, args = app.root.pending.pop(token)
    assert _ms == 15000
    fn(*args)


def _no_forbidden(fake, base: int) -> None:
    fresh = fake.calls[base:]
    for name in _FORBIDDEN:
        assert name not in fresh, name


def _recover_enter(app, fake, clock, drop):
    """One #178 EnterMonitorLONG while the session is open.

    Returns ``(entered_at, enter_count)``. IsConnected stays 0 until
    the call, which is what lets EnterMonitorLONG through the gate.
    """
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    fired = drop + timedelta(seconds=15)
    _fire_timeout(app, fake, clock, fired)
    entered = fired + timedelta(seconds=5)
    _tick(app, fake, clock, entered)
    assert app._link_host.machine.quote == LinkPhase.CONNECTING
    return entered, fake.calls.count("EnterMonitorLONG")


def _deliver_3002(app):
    rb.SKQuoteLibEvents().OnConnection(3002, 0)
    app._process_ui_queue()


def _deliver_3003(app, fake, clock, when):
    clock["now"] = when
    fake.set_now(when)
    fake.note_3003()
    rb.SKQuoteLibEvents().OnConnection(3003, 0)
    app._process_ui_queue()
    _tick(app, fake, clock, when)


def test_handshake_3002_then_late_3003_stays_one_enter(monkeypatch):
    """EnterMonitor, the handshake 3002, then 3003 about 3 min later.

    That 3002 is the #178 attempt talking, not a new outage. The 15s
    timer must not mark it down, and the late 3003 must reach READY
    with no second EnterMonitorLONG.
    """
    drop = tpe(2026, 11, 12, 8, 48)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    entered, enters = _recover_enter(app, fake, clock, drop)
    # Half-up after EnterMonitorLONG, the way the handshake reports.
    fake.force_quote_state = None
    assert fake.is_connected() == 2
    started = app._link_host.machine._quote_connect_started_at
    _deliver_3002(app)
    # Production fires the 15s timer if one was armed. A handshake
    # must not have armed one; if it did, the replay lets it run.
    step = entered
    deadline = entered + timedelta(minutes=3)
    while step < deadline:
        step += timedelta(seconds=5)
        _tick(app, fake, clock, step)
        if app._quote_ready_timer_id is not None and step >= entered + timedelta(seconds=15):
            _fire_timeout(app, fake, clock, step)
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.CONNECTING
    assert machine._pending_3003 is False
    assert machine._quote_connect_started_at == started
    assert fake.calls.count("EnterMonitorLONG") == enters
    subs = fake.calls.count("SubscribeQuotes")
    _deliver_3003(app, fake, clock, deadline)
    assert machine.quote == LinkPhase.READY
    assert machine._pending_3003 is False
    assert fake.calls.count("EnterMonitorLONG") == enters
    assert fake.calls.count("SubscribeQuotes") == subs + 1
    assert "LeaveMonitor" not in fake.calls
    assert "RestartProcess" not in fake.calls


def test_mid_session_timeout_then_late_3003_becomes_ready(monkeypatch):
    """Unexpected 3002 while READY, IsConnected stays 2, then a late 3003.

    The timeout may mark the quote down. The 3003 must still be
    honored: READY and a resubscribe, not a stuck DOWN with the
    pending flag ignored.
    """
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    enters = fake.calls.count("EnterMonitorLONG")
    subs = fake.calls.count("SubscribeQuotes")
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 2
    _deliver_3002(app)
    assert app._quote_ready_timer_id is not None
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.DOWN
    assert fake.calls.count("EnterMonitorLONG") == enters
    late = drop + timedelta(seconds=45)
    _deliver_3003(app, fake, clock, late)
    assert machine.quote == LinkPhase.READY
    assert machine._pending_3003 is False
    assert fake.calls.count("SubscribeQuotes") == subs + 1
    assert fake.calls.count("EnterMonitorLONG") == enters
    assert machine.degraded is False


def test_repeated_3002_does_not_rearm_the_timer(monkeypatch):
    """One armed timer per unexpected 3002. Later 3002s do not reset it.

    A handshake 3002 while CONNECTING does not arm a timer at all, so
    a burst cannot start the timeout/EnterMonitor loop.
    """
    drop = tpe(2026, 11, 12, 9, 30)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    _replay_drop(app, fake, clock, drop)
    _deliver_3002(app)
    token = app._quote_ready_timer_id
    seen = app._quote_intermediate_seen_at
    assert token is not None
    _deliver_3002(app)
    _deliver_3002(app)
    assert app._quote_ready_timer_id == token
    assert app._quote_intermediate_seen_at == seen
    assert list(app.root.pending) == [token]
    entered, _enters = _recover_enter(app, fake, clock, drop + timedelta(minutes=30))
    del entered
    fake.force_quote_state = None
    _deliver_3002(app)
    _deliver_3002(app)
    assert app._quote_ready_timer_id is None


def _arm_q4(monkeypatch, app):
    app._live_runner = _Runner()
    app._live_tick_com_symbol = "TX00"
    app._live_tick_active = True
    app._tick_watchdog = _Watch("session_resubscribe")

    def call_com(name, fn, *args, **kwargs):
        if name == "RequestTicks":
            return 0
        return fn(*args, **kwargs)

    monkeypatch.setattr(rb, "call_com", call_com)
    monkeypatch.setattr(
        rb, "skQ",
        type("Q", (), {"SKQuoteLib_RequestTicks": staticmethod(lambda *a: 0)})(),
    )


def test_q4_while_connecting_does_not_enter_again(monkeypatch):
    """Deferred 08:45:00 EnterMonitor, then Q4 at 08:45:20.

    The quote is CONNECTING, still inside its wait for 3003. Q4 must
    not reset that attempt to DOWN. Exactly one EnterMonitorLONG.
    """
    drop = tpe(2026, 11, 12, 7, 30)
    app, fake, clock, logged = _ready(monkeypatch, drop - timedelta(minutes=30))
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    opened = tpe(2026, 11, 12, 8, 45)
    _tick(app, fake, clock, opened)
    enters = fake.calls.count("EnterMonitorLONG")
    assert app._link_host.machine.quote == LinkPhase.CONNECTING
    assert enters == fake.calls.count("EnterMonitorLONG")
    q4_at = opened + timedelta(seconds=20)
    clock["now"] = q4_at
    fake.set_now(q4_at)
    _arm_q4(monkeypatch, app)
    before = len(logged)
    app._check_tick_watchdog()
    assert "starting quote-down now" not in "\n".join(logged[before:])
    _tick(app, fake, clock, opened + timedelta(seconds=26))
    assert fake.calls.count("EnterMonitorLONG") == enters
    assert app._link_host.machine.quote == LinkPhase.CONNECTING
    assert "LeaveMonitor" not in fake.calls


def test_3003_one_second_after_timeout_becomes_ready(monkeypatch):
    """Open session, IsConnected stays 1, 3003 arrives one second late.

    The timeout may mark the quote down. The 3003 must still resubscribe
    and reach READY. It must not sit DOWN until DEGRADED stuck at +320s.
    """
    drop = tpe(2026, 11, 12, 10, 15)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    _running(app)
    enters = fake.calls.count("EnterMonitorLONG")
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 1
    subs = fake.calls.count("SubscribeQuotes")
    _deliver_3002(app)
    fired = drop + timedelta(seconds=15)
    _fire_timeout(app, fake, clock, fired)
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.DOWN
    entry, _ = app._trading_guard.decide("auto", "ENTRY_FILL", "LONG")
    assert entry == TradingGuard.BLOCK_LINK
    _deliver_3003(app, fake, clock, fired + timedelta(seconds=1))
    assert machine.quote == LinkPhase.READY
    assert machine._pending_3003 is False
    assert fake.calls.count("SubscribeQuotes") == subs + 1
    assert fake.calls.count("EnterMonitorLONG") == enters
    opened, _ = app._trading_guard.decide("auto", "ENTRY_FILL", "LONG")
    assert opened != TradingGuard.BLOCK_LINK
    _tick(app, fake, clock, fired + timedelta(seconds=320))
    assert machine.degraded is False
    assert machine.degraded_reason == ""
    assert machine.quote == LinkPhase.READY


def test_stuck_countdown_does_not_degrade_across_the_close(monkeypatch):
    """Quote-down at 13:44:45, IsConnected 2. The close is 13:45.

    stuck_limit must not reach DEGRADED at 13:50:45, inside the gap,
    just because the countdown started while the session was still open.
    """
    drop = tpe(2026, 11, 12, 13, 44, 30)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    enters = fake.calls.count("EnterMonitorLONG")
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 2
    fired = drop + timedelta(seconds=15)
    assert fired == tpe(2026, 11, 12, 13, 44, 45)
    _fire_timeout(app, fake, clock, fired)
    assert app._link_host.machine.degraded is False
    _tick(app, fake, clock, tpe(2026, 11, 12, 13, 44, 50))
    assert app._link_host.machine.degraded is False
    _tick(app, fake, clock, tpe(2026, 11, 12, 13, 50, 45))
    machine = app._link_host.machine
    assert machine.degraded is False
    assert machine.quote == LinkPhase.DOWN
    assert fake.calls.count("EnterMonitorLONG") == enters
    _tick(app, fake, clock, tpe(2026, 11, 12, 14, 59))
    assert machine.degraded is False
    assert machine.quote != LinkPhase.READY


def test_disconnect_after_timeout_still_cleans_up(monkeypatch):
    """3021 after the timeout cleared ``_quote_connected`` still pauses.

    The flag is already false, so the old early return skipped the
    tick-feed pause, the status line, and the connection monitor.
    """
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    app._live_runner = _Runner()
    app._live_tick_active = True

    class _Mon:
        def __init__(self):
            self.downs = 0

        def on_disconnected(self):
            self.downs += 1

        def schedule_next(self, **_kwargs):
            return type("A", (), {
                "type": "give_up", "message": "paused", "delay_seconds": 0,
            })()

    app._conn_monitor = _Mon()
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    _deliver_3002(app)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    assert app._quote_connected is False
    assert app._live_tick_active is True
    rb.SKQuoteLibEvents().OnConnection(3021, 0)
    app._process_ui_queue()
    assert app._live_tick_active is False
    assert app._conn_monitor.downs == 1
    assert any("Disconnected" in msg for msg, _level in app._statuses)
    rb.SKQuoteLibEvents().OnConnection(3033, 0)
    app._process_ui_queue()
    assert app._conn_monitor.downs == 1


def _give_up_monitor():
    class _Mon:
        def __init__(self):
            self.downs = 0

        def on_disconnected(self):
            self.downs += 1

        def reset(self):
            return None

        def on_manual_reconnect(self):
            return type("A", (), {"message": "manual"})()

        def schedule_next(self, **_kwargs):
            return type("A", (), {
                "type": "give_up", "message": "paused", "delay_seconds": 0,
            })()

    return _Mon()


def test_3021_in_the_gap_reconnects_after_settle_not_at_the_open(monkeypatch):
    """A plain 3021 at 06:00 reconnects about 6s later, not at 08:45.

    The closed-session hold is only for the 15s 3002 timeout.
    """
    at = tpe(2026, 11, 12, 6, 0)
    app, fake, clock, _logged = _ready(monkeypatch, at)
    app._conn_monitor = _give_up_monitor()
    clock["now"] = at
    fake.set_now(at)
    enters = fake.calls.count("EnterMonitorLONG")
    fake.note_quote_down(at)
    rb.SKQuoteLibEvents().OnConnection(3021, 0)
    app._process_ui_queue()
    _tick(app, fake, clock, at + timedelta(seconds=4))
    assert fake.calls.count("EnterMonitorLONG") == enters
    _tick(app, fake, clock, at + timedelta(seconds=6))
    assert fake.calls.count("EnterMonitorLONG") == enters + 1
    assert app._link_host.machine.quote == LinkPhase.CONNECTING
    assert app._link_host.machine._quote_timeout_hold is False
    assert app._link_host.machine._quote_enter_not_before is None


def test_manual_reconnect_in_the_gap_is_not_held_until_the_open(monkeypatch):
    """IsConnected 0 does not defer EnterMonitor until 08:45.

    The timeout tick itself is still inside the 5s settle, so the
    quote is DOWN. Manual Reconnect at 07:40 enters now.
    """
    drop = tpe(2026, 11, 12, 7, 30)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=20))
    app._conn_monitor = _give_up_monitor()
    app._reconnect_timer_id = None
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.DOWN
    assert machine._quote_enter_not_before is None
    enters = fake.calls.count("EnterMonitorLONG")
    press = tpe(2026, 11, 12, 7, 40)
    clock["now"] = press
    fake.set_now(press)
    app._manual_reconnect()
    assert fake.calls.count("EnterMonitorLONG") == enters + 1
    assert machine.quote == LinkPhase.CONNECTING
    assert machine._quote_timeout_hold is False
    assert machine._quote_enter_not_before is None


def test_timeout_after_leaving_ready_does_not_reset_the_attempt(monkeypatch):
    """The 15s callback fires after the quote has already left READY.

    Removing the phase guard, the mark guard, or note_quote_not_ready's
    READY check would mark this attempt down.
    """
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    _replay_drop(app, fake, clock, drop)
    _deliver_3002(app)
    assert app._quote_ready_timer_id is not None
    machine = app._link_host.machine
    started = drop + timedelta(seconds=1)
    machine.quote = LinkPhase.CONNECTING
    machine._quote_connect_started_at = started
    machine._pending_3003 = False
    app._quote_connected = True
    before = len(logged)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    text = "\n".join(logged[before:])
    assert "quote marked down" not in text
    assert machine.quote == LinkPhase.CONNECTING
    assert machine._quote_connect_started_at == started
    assert machine._quote_enter_not_before is None
    app._mark_quote_down_for_recovery(reason="probe")
    assert app._quote_connected is True
    assert machine.quote == LinkPhase.CONNECTING
    machine.note_quote_not_ready(drop + timedelta(seconds=16))
    assert machine.quote == LinkPhase.CONNECTING
    assert machine._quote_connect_started_at == started
    assert machine._pending_3003 is False


def test_3003_clears_the_disconnect_cleanup_latch(monkeypatch):
    """Ready (3003) clears the cleanup latch so a later 3021 still pauses.

    The timeout clears ``_quote_connected`` without running cleanup.
    After a 3021 the latch is set. A 3003 must clear it, or the next
    timeout-then-3021 skips the tick-feed pause.
    """
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    app._live_runner = _Runner()
    app._live_tick_active = True
    app._conn_monitor = _give_up_monitor()
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    _deliver_3002(app)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    rb.SKQuoteLibEvents().OnConnection(3021, 0)
    app._process_ui_queue()
    assert app._conn_monitor.downs == 1
    assert app._disconnect_cleanup_done is True
    assert app._live_tick_active is False
    ready_at = drop + timedelta(seconds=20)
    _deliver_3003(app, fake, clock, ready_at)
    assert app._quote_connected is True
    assert app._disconnect_cleanup_done is False
    assert app._link_host.machine.quote == LinkPhase.READY
    app._live_tick_active = True
    fake.force_quote_state = 0
    _deliver_3002(app)
    _fire_timeout(app, fake, clock, ready_at + timedelta(seconds=15))
    assert app._quote_connected is False
    rb.SKQuoteLibEvents().OnConnection(3021, 0)
    app._process_ui_queue()
    assert app._conn_monitor.downs == 2
    assert app._live_tick_active is False


def test_0730_missing_3003_logs_clears_flag_and_starts_quote_down(monkeypatch):
    """The 07:30 sequence. Stale ``_quote_connected`` is not a 3003."""
    drop = tpe(2026, 11, 12, 7, 30)
    app, fake, clock, logged = _ready(monkeypatch, drop - timedelta(minutes=30))
    base = len(fake.calls)
    enters = fake.calls.count("EnterMonitorLONG")
    _replay_drop(app, fake, clock, drop)
    assert app._quote_connected is True
    # IsConnected 0 marks the quote down and does not wait for 08:45.
    # The timeout tick is still inside the 5s settle, so no enter yet.
    fake.force_quote_state = 0
    before = len(logged)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    text = "\n".join(logged[before:])
    assert "Ready (3003) did not arrive" in text
    assert app._quote_connected is False
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.DOWN
    assert machine._quote_enter_not_before is None
    assert machine.degraded is False
    assert machine.orders_paused is False
    assert fake.calls.count("EnterMonitorLONG") == enters
    _no_forbidden(fake, base)
    entry, _details = app._trading_guard.decide("auto", "ENTRY_FILL", "LONG")
    exit_v, _d2 = app._trading_guard.decide("auto", "TRADE_CLOSE", "LONG")
    assert entry == TradingGuard.BLOCK_LINK
    assert exit_v == TradingGuard.SEND_EXIT


def test_3003_within_15s_cancels_timer_and_skips_recovery(monkeypatch):
    drop = tpe(2026, 11, 12, 9, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    enters = fake.calls.count("EnterMonitorLONG")
    _replay_drop(app, fake, clock, drop)
    rb.SKQuoteLibEvents().OnConnection(3003, 0)
    app._process_ui_queue()
    assert app._quote_ready_timer_id is None
    assert app.root.pending == {}
    assert app._quote_connected is True
    assert app._link_host.machine.quote == LinkPhase.READY
    _tick(app, fake, clock, drop + timedelta(seconds=30))
    assert fake.calls.count("EnterMonitorLONG") == enters
    assert app._link_host.machine.quote == LinkPhase.READY
    assert app._link_host.machine.degraded is False
    entry, _ = app._trading_guard.decide("auto", "ENTRY_FILL", "LONG")
    assert entry != TradingGuard.BLOCK_LINK


def test_timeout_body_does_not_return_on_stale_quote_connected():
    """The old early return treated a previous 3003 as this 3002's Ready."""
    src = inspect.getsource(rb.BacktestApp._on_quote_ready_timeout)
    assert "if self._quote_connected" not in src
    assert "trigger a fresh reconnect" not in src
    assert "EnterMonitorLONG" in src


@pytest.mark.parametrize(
    "drop,open_at",
    [
        (tpe(2026, 11, 12, 7, 30), tpe(2026, 11, 12, 8, 45)),
        (tpe(2026, 11, 12, 13, 50), tpe(2026, 11, 12, 15, 0)),
    ],
)
def test_closed_gap_defers_enter_monitor_until_open(monkeypatch, drop, open_at):
    """stuck_limit_s=300 must not DEGRADE across the gap. Not 08:44 / 14:59."""
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=20))
    enters = fake.calls.count("EnterMonitorLONG")
    _replay_drop(app, fake, clock, drop)
    # Half-up (3002). Entering now would fault; the stuck clock would
    # DEGRADE 300s later if the gap did not hold the quote-down path.
    fake.force_quote_state = 2
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.DOWN
    assert machine._quote_enter_not_before == open_at
    cursor = drop + timedelta(minutes=1)
    while cursor < open_at:
        _tick(app, fake, clock, cursor)
        assert machine.degraded is False
        assert machine.quote != LinkPhase.READY
        assert fake.calls.count("EnterMonitorLONG") == enters
        cursor += timedelta(minutes=1)
    # The minute before the open (08:44 or 14:59) is still closed.
    _tick(app, fake, clock, open_at - timedelta(seconds=1))
    assert machine.degraded is False
    assert fake.calls.count("EnterMonitorLONG") == enters
    fake.force_quote_state = 0
    _tick(app, fake, clock, open_at)
    assert fake.calls.count("EnterMonitorLONG") == enters + 1
    assert machine.degraded is False
    assert "LeaveMonitor" not in fake.calls
    assert fake.calls.count("LoginSetQuote") == 1
    assert "RestartProcess" not in fake.calls


def test_open_session_timeout_enters_after_five_second_settle(monkeypatch):
    """Market open: #178 is 5s settle, then EnterMonitorLONG iff IsConnected()==0."""
    drop = tpe(2026, 11, 12, 9, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    enters = fake.calls.count("EnterMonitorLONG")
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    fired = drop + timedelta(seconds=15)
    _fire_timeout(app, fake, clock, fired)
    assert app._link_host.machine.quote == LinkPhase.DOWN
    assert app._link_host.machine._quote_enter_not_before is None
    assert fake.calls.count("EnterMonitorLONG") == enters
    _tick(app, fake, clock, fired + timedelta(seconds=4))
    assert fake.calls.count("EnterMonitorLONG") == enters
    _tick(app, fake, clock, fired + timedelta(seconds=5))
    assert fake.calls.count("EnterMonitorLONG") == enters + 1
    assert app._link_host.machine.degraded is False
    assert "LeaveMonitor" not in fake.calls
    assert "RestartProcess" not in fake.calls


def test_open_half_up_still_reaches_stuck_limit(monkeypatch):
    """An open half-up quote still degrades at stuck_limit. The gap hold is not on."""
    drop = tpe(2026, 11, 12, 9, 10)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    enters = fake.calls.count("EnterMonitorLONG")
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 2
    fired = drop + timedelta(seconds=15)
    _fire_timeout(app, fake, clock, fired)
    _tick(app, fake, clock, fired + timedelta(seconds=5))
    assert fake.calls.count("EnterMonitorLONG") == enters
    assert app._link_host.machine.degraded is False
    _tick(app, fake, clock, fired + timedelta(seconds=5 + 300))
    assert app._link_host.machine.degraded is True
    assert app._link_host.machine.degraded_reason == "stuck"
    assert fake.calls.count("EnterMonitorLONG") == enters
    assert "LeaveMonitor" not in fake.calls
    assert "RestartProcess" not in fake.calls


class _Runner:
    symbol = "TX00"
    suppress_strategy = False
    _is_reloading = False

    def reset_bar_monotonicity(self, bar_was_truncated=False):
        self.bar_was_truncated = bar_was_truncated


class _Watch:
    def __init__(self, action):
        self.action = action
        self.active = False
        self.resubscribed = False

    def check(self):
        return self.action

    def elapsed_minutes(self):
        return 90

    def on_session_resubscribe(self):
        self.resubscribed = True

    def on_resubscribe(self):
        self.on_resub = True

    def set_grace(self, seconds):
        self.grace = seconds


def test_q4_open_resubscribe_disconnected_starts_quote_down(monkeypatch):
    """08:45, RequestTicks==0 and IsConnected()==0 starts #178 now.

    The warn ladder would wait ~120s plus a second bad read. This must
    not.
    """
    opened = tpe(2026, 11, 12, 8, 45)
    app, fake, clock, logged = _ready(monkeypatch, opened)
    clock["now"] = opened
    fake.set_now(opened)
    fake.force_quote_state = 0
    app._live_runner = _Runner()
    app._live_tick_com_symbol = "TX00"
    app._live_tick_active = True
    app._tick_watchdog = _Watch("session_resubscribe")
    seen = []

    def call_com(name, fn, *args, **kwargs):
        seen.append(name)
        if name == "RequestTicks":
            return 0
        return fn(*args, **kwargs)

    monkeypatch.setattr(rb, "call_com", call_com)
    monkeypatch.setattr(
        rb, "skQ", type("Q", (), {"SKQuoteLib_RequestTicks": staticmethod(lambda *a: 0)})()
    )
    enters = fake.calls.count("EnterMonitorLONG")
    before = len(logged)
    app._check_tick_watchdog()
    assert "RequestTicks" in seen
    assert app._tick_watchdog.resubscribed is True
    text = "\n".join(logged[before:])
    assert "IsConnected()=0" in text
    assert app._link_host.machine.quote == LinkPhase.DOWN
    assert app._quote_connected is False
    assert fake.calls.count("EnterMonitorLONG") == enters
    # Settle, then enter. Not ~150s later.
    _tick(app, fake, clock, opened + timedelta(seconds=5))
    assert fake.calls.count("EnterMonitorLONG") == enters + 1
    assert app._link_host.machine.degraded is False
    assert "LeaveMonitor" not in fake.calls
    assert "RestartProcess" not in fake.calls
    entry, _ = app._trading_guard.decide("auto", "ENTRY_FILL", "LONG")
    exit_v, _ = app._trading_guard.decide("auto", "TRADE_CLOSE", "LONG")
    assert entry == TradingGuard.BLOCK_LINK
    assert exit_v == TradingGuard.SEND_EXIT


def test_q4_connected_quote_does_not_start_recovery(monkeypatch):
    """RequestTicks==0 with IsConnected()==1 stays on the warn ladder."""
    opened = tpe(2026, 11, 12, 8, 45)
    app, fake, clock, _logged = _ready(monkeypatch, opened)
    clock["now"] = opened
    fake.set_now(opened)
    fake.force_quote_state = 1
    app._live_runner = _Runner()
    app._live_tick_com_symbol = "TX00"
    app._live_tick_active = True
    app._tick_watchdog = _Watch("session_resubscribe")

    def call_com(name, fn, *args, **kwargs):
        if name == "RequestTicks":
            return 0
        return fn(*args, **kwargs)

    monkeypatch.setattr(rb, "call_com", call_com)
    monkeypatch.setattr(
        rb, "skQ", type("Q", (), {"SKQuoteLib_RequestTicks": staticmethod(lambda *a: 0)})()
    )
    enters = fake.calls.count("EnterMonitorLONG")
    app._check_tick_watchdog()
    assert app._link_host.machine.quote == LinkPhase.READY
    assert app._quote_connected is True
    assert fake.calls.count("EnterMonitorLONG") == enters


def test_heartbeat_quote_is_not_ready_after_timeout(monkeypatch, tmp_path):
    drop = tpe(2026, 11, 12, 7, 30)
    app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=30))
    writer = hb.HeartbeatWriter(str(tmp_path))
    monkeypatch.setattr(hb, "_writer", writer)
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    app._publish_link_heartbeat()
    writer.write(now=1.0)
    data = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert data["link"]["quote"] != "ready"
    assert data["link"]["quote"] == LinkPhase.DOWN.value
    assert data["link"]["degraded"] is False


def test_logger_typeerror_is_not_retried():
    """A TypeError inside the logger is not a 3-argument mismatch."""
    calls = []

    def logger(name, elapsed_ms, failed, code=None):
        del name, elapsed_ms, failed
        calls.append(code)
        raise TypeError("boom inside the logger")

    hb.set_com_return_logger(logger)
    try:
        hb._emit_com_return("EnterMonitorLONG", 1.0, failed=False, code=7)
    finally:
        hb.set_com_return_logger(rb._log_com_return)
    assert calls == [7]


def test_three_arg_com_logger_is_still_called_once():
    calls = []

    def logger(name, elapsed_ms, failed):
        del elapsed_ms, failed
        calls.append(name)

    hb.set_com_return_logger(logger)
    try:
        hb._emit_com_return("EnterMonitorLONG", 1.0, failed=False, code=4)
    finally:
        hb.set_com_return_logger(rb._log_com_return)
    assert calls == ["EnterMonitorLONG"]


def test_open_session_3002_blip_with_tick_stays_ready(monkeypatch):
    """Open session, a tick since the 3002, IsConnected stays 1.

    The 15s timeout must not mark the quote down, must not set the
    timeout hold, and must not reach DEGRADED. A second blip in the
    same session stays READY too: the one-shot resubscribe key is
    never spent.
    """
    from src.live.quote_stuck_recovery import QuoteStuckRecovery

    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    _running(app)
    app._quote_stuck = QuoteStuckRecovery()
    enters = fake.calls.count("EnterMonitorLONG")
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 1

    def _blip(at, label):
        clock["now"] = at
        fake.set_now(at)
        before = len(logged)
        _deliver_3002(app)
        # Wall clock of a fresh tick after this 3002. _on_com_tick writes
        # the same field through _note_fresh_quote_tick. _fire_timeout
        # then places the 3002 15s earlier, so the tick is since the 3002.
        app._last_fresh_tick_at = time.time()
        _fire_timeout(app, fake, clock, at + timedelta(seconds=15))
        text = "\n".join(logged[before:])
        assert "staying READY" in text, label
        assert "quote marked down" not in text, label
        machine = app._link_host.machine
        assert machine.quote == LinkPhase.READY, label
        assert machine._quote_timeout_hold is False, label
        assert machine._quote_enter_not_before is None, label
        assert machine.degraded is False, label
        assert machine.orders_paused is False, label
        assert app._quote_connected is True, label
        assert fake.calls.count("EnterMonitorLONG") == enters, label
        entry, _ = app._trading_guard.decide("auto", "ENTRY_FILL", "LONG")
        assert entry != TradingGuard.BLOCK_LINK, label
        return at + timedelta(seconds=15)

    first = _blip(drop, "first blip")
    # Past stuck_limit. A mark-down with IsConnected 1 would be DEGRADED.
    _tick(app, fake, clock, first + timedelta(seconds=320))
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.READY
    assert machine.degraded is False
    assert machine.degraded_reason == ""
    assert app._quote_stuck.attempted_key is None
    second = drop + timedelta(minutes=30)
    fired = _blip(second, "second blip")
    _tick(app, fake, clock, fired + timedelta(seconds=320))
    assert machine.quote == LinkPhase.READY
    assert machine.degraded is False
    assert app._quote_stuck.attempted_key is None
    assert "LeaveMonitor" not in fake.calls
    assert "RestartProcess" not in fake.calls
    tick_src = inspect.getsource(rb.BacktestApp._on_com_tick)
    assert "_note_fresh_quote_tick" in tick_src
    alive_src = inspect.getsource(rb.BacktestApp._quote_timeout_ticks_are_fresh)
    assert "port.is_connected" not in alive_src
    assert "FRESH_TICK_LIVENESS_S" in alive_src


def test_fresh_tick_stays_ready_even_when_is_connected_0(monkeypatch):
    """IsConnected()==0 does not mark the quote down while ticks flow."""
    drop = tpe(2026, 11, 12, 9, 0)
    app, fake, clock, logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    before = len(logged)
    _deliver_3002(app)
    app._last_fresh_tick_at = time.time() - 10
    fired = drop + timedelta(seconds=15)
    _fire_timeout(app, fake, clock, fired)
    text = "\n".join(logged[before:])
    assert "ticks are flowing" in text
    assert "staying READY" in text
    assert "quote marked down" not in text
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.READY
    assert machine._quote_timeout_hold is False
    assert app._quote_connected is True
    entry, _ = app._trading_guard.decide("auto", "ENTRY_FILL", "LONG")
    assert entry != TradingGuard.BLOCK_LINK


def test_enter_monitor_return_code_is_logged(monkeypatch):
    """P3: the #178 port path logs EnterMonitorLONG's code, not only duration."""
    logs: list[str] = []
    monkeypatch.setattr(rb, "_log_debug", lambda msg: logs.append(str(msg)))
    hb.set_com_return_logger(rb._log_com_return)
    try:
        assert hb.call_com("EnterMonitorLONG", lambda: 7) == 7
    finally:
        hb.set_com_return_logger(rb._log_com_return)
    text = "\n".join(logs)
    assert "EnterMonitorLONG returned" in text
    assert "code=7" in text


def _closed_gap_down(monkeypatch, state=0):
    """07:30 3002 with no fresh tick.

    ``state`` 0 does not defer EnterMonitor (the timeout tick is still
    inside the 5s settle). Any other state holds EnterMonitor until
    08:45 and makes no further SKCOM call during the gap.
    """
    drop = tpe(2026, 11, 12, 7, 30)
    open_at = tpe(2026, 11, 12, 8, 45)
    app, fake, clock, logged = _ready(monkeypatch, drop - timedelta(minutes=30))
    enters = fake.calls.count("EnterMonitorLONG")
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = state
    calls_at_fire = len(fake.calls)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.DOWN
    if state == 0:
        assert machine._quote_enter_not_before is None
    else:
        assert machine._quote_enter_not_before == open_at
        assert fake.calls[calls_at_fire:] == ["IsConnected"]
    assert app._quote_connected is False
    return app, fake, clock, logged, enters, open_at


def test_closed_gap_healthy_tick_at_open_restores_ready(monkeypatch):
    """IsConnected 2 holds until 08:45. A tick then restores READY.

    The tick is the liveness signal. IsConnected is not read again.
    """
    app, fake, clock, _logged, enters, open_at = _closed_gap_down(
        monkeypatch, state=2)
    fake.force_quote_state = 1
    clock["now"] = open_at
    fake.set_now(open_at)
    app._note_fresh_quote_tick()
    _tick(app, fake, clock, open_at)
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.READY
    assert machine.degraded is False
    assert machine._quote_timeout_hold is False
    assert machine._quote_enter_not_before is None
    assert machine._quote_stuck_since is None
    assert app._quote_connected is True
    assert fake.calls.count("EnterMonitorLONG") == enters
    entry, _ = app._trading_guard.decide("auto", "ENTRY_FILL", "LONG")
    assert entry != TradingGuard.BLOCK_LINK
    _tick(app, fake, clock, open_at + timedelta(seconds=320))
    assert machine.quote == LinkPhase.READY
    assert machine.degraded is False
    assert machine.degraded_reason == ""
    assert "LeaveMonitor" not in fake.calls
    restore_src = inspect.getsource(machine._restore_if_feed_healthy)
    assert "is_connected" not in restore_src


def test_closed_gap_without_ticks_after_open_still_reaches_stuck(monkeypatch):
    """IsConnected 1 holds the gap. No tick after 08:45 starts the stuck clock."""
    app, fake, clock, _logged, enters, open_at = _closed_gap_down(
        monkeypatch, state=1)
    fake.force_quote_state = 1
    _tick(app, fake, clock, open_at)
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.DOWN
    assert machine.degraded is False
    assert app._quote_connected is False
    assert fake.calls.count("EnterMonitorLONG") == enters
    _tick(app, fake, clock, open_at + timedelta(seconds=300))
    assert machine.degraded is True
    assert machine.degraded_reason == "stuck"
    assert machine.quote != LinkPhase.READY
    assert fake.calls.count("EnterMonitorLONG") == enters


def test_tick_after_open_restores_even_when_is_connected_0(monkeypatch):
    """A fresh tick after the hold releases READY. IsConnected stays 0."""
    app, fake, clock, _logged, enters, open_at = _closed_gap_down(
        monkeypatch, state=2)
    fake.force_quote_state = 0
    clock["now"] = open_at
    fake.set_now(open_at)
    app._note_fresh_quote_tick()
    _tick(app, fake, clock, open_at)
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.READY
    assert app._quote_connected is True
    assert fake.calls.count("EnterMonitorLONG") == enters
    assert machine.degraded is False


def test_duplicate_3003_while_ready_does_not_reconnect(monkeypatch):
    """A second 3003 on a READY quote does not resubscribe."""
    at = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, logged = _ready(monkeypatch, at)
    seen: list[str] = []

    def _on_reconnected():
        seen.append("reconnected")

    def subscribe_quotes():
        seen.append("subscribe")
        app._on_reconnected()

    app._on_reconnected = _on_reconnected
    fake.subscribe_quotes = subscribe_quotes
    before = len(logged)
    _deliver_3003(app, fake, clock, at)
    _deliver_3003(app, fake, clock, at + timedelta(seconds=1))
    assert seen == []
    text = "\n".join(logged[before:])
    assert text.count("3003 ignored: quote already READY") == 1
    assert app._link_host.machine.quote == LinkPhase.READY


def test_no_runner_is_connected_1_without_a_tick_marks_down(monkeypatch):
    """No runner and IsConnected()==1 still marks down when no tick is fresh.

    IsConnected is not liveness. DEGRADED after the stuck clock is the
    accepted end state.
    """
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    assert app._live_runner is None
    enters = fake.calls.count("EnterMonitorLONG")
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 1
    before = len(logged)
    _deliver_3002(app)
    assert not getattr(app, "_last_fresh_tick_at", 0.0)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    text = "\n".join(logged[before:])
    assert "quote marked down" in text
    assert "ticks are flowing" not in text
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.DOWN
    assert app._quote_connected is False
    assert fake.calls.count("EnterMonitorLONG") == enters
    fired = drop + timedelta(seconds=15)
    _tick(app, fake, clock, fired + timedelta(seconds=5))
    _tick(app, fake, clock, fired + timedelta(seconds=5 + 300))
    assert machine.degraded is True
    assert machine.degraded_reason == "stuck"


def test_no_runner_is_connected_0_still_marks_down(monkeypatch):
    """(B) GUI idle and IsConnected()==0 still marks the quote down."""
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    assert app._live_runner is None
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    before = len(logged)
    _deliver_3002(app)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    text = "\n".join(logged[before:])
    assert "quote marked down" in text
    assert "staying READY" not in text
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.DOWN
    assert app._quote_connected is False
    assert machine._quote_timeout_hold is True


def test_tick_without_runner_records_fresh_tick_clock(monkeypatch):
    """(C) A fresh tick updates the clock even when no runner is running."""
    app, _logged = _app(monkeypatch)
    when = tpe(2026, 11, 12, 10, 0)
    monkeypatch.setattr(rb, "_taipei_now", lambda: when)
    assert float(getattr(app, "_last_fresh_tick_at", 0.0) or 0.0) == 0.0
    before = time.time()
    app._on_com_tick(20261112, 100000, 0, 1, 1, 100, 1, 0, False)
    assert app._last_fresh_tick_at >= before


def test_stale_tick_before_enter_monitor_does_not_restore_ready(monkeypatch):
    """(D) A tick from before EnterMonitorLONG must not restore READY."""
    app, fake, clock, _logged, enters, open_at = _closed_gap_down(monkeypatch)
    clock["now"] = open_at
    fake.set_now(open_at)
    app._note_fresh_quote_tick()
    fake.force_quote_state = 0
    _tick(app, fake, clock, open_at)
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.CONNECTING
    assert fake.calls.count("EnterMonitorLONG") == enters + 1
    # The attempt ends. IsConnected comes back as 1. The old tick stays.
    later = open_at + timedelta(seconds=61)
    fake.force_quote_state = 0
    _tick(app, fake, clock, later)
    assert machine.quote == LinkPhase.DOWN
    fake.force_quote_state = 1
    _tick(app, fake, clock, later + timedelta(seconds=1))
    assert machine.quote != LinkPhase.READY
    assert machine._fresh_tick_at is None
    assert machine._quote_timeout_hold is False
    assert machine._quote_hold_open_at is None


def test_hold_release_then_3021_still_cleans_up(monkeypatch):
    """Hold release clears the disconnect latch, so a later 3021 pauses.

    A previous cleanup left the latch set. The closed-gap hold is still
    in place (no 3021 was delivered to the machine). After the open
    restores READY, another timeout marks the quote down without
    cleanup. The following 3021 must pause the tick feed.
    """
    app, fake, clock, _logged, _enters, open_at = _closed_gap_down(
        monkeypatch, state=1)
    app._live_runner = _Runner()
    app._live_tick_active = True
    app._conn_monitor = _give_up_monitor()
    app._disconnect_cleanup_done = True
    fake.force_quote_state = 1
    clock["now"] = open_at
    fake.set_now(open_at)
    app._note_fresh_quote_tick()
    _tick(app, fake, clock, open_at)
    assert app._quote_connected is True
    assert app._link_host.machine.quote == LinkPhase.READY
    app._live_tick_active = True
    fake.force_quote_state = 0
    # The open's tick is minutes old by the next 3002. Wall clock, not
    # the faked session clock, is what the timeout reads.
    app._last_fresh_tick_at = time.time() - 60
    again = open_at + timedelta(minutes=30)
    _deliver_3002(app)
    _fire_timeout(app, fake, clock, again)
    assert app._quote_connected is False
    rb.SKQuoteLibEvents().OnConnection(3021, 0)
    app._process_ui_queue()
    assert app._live_tick_active is False
    assert app._conn_monitor.downs == 1
    assert any("Disconnected" in msg for msg, _level in app._statuses)


def test_manual_reconnect_drops_duplicate_3003_alert(monkeypatch):
    """A queued duplicate 3003 on Manual Reconnect is not an alert."""
    at = tpe(2026, 11, 12, 10, 0)
    app, _fake, _clock, logged = _ready(monkeypatch, at)
    app._conn_monitor = _give_up_monitor()
    app._reconnect_timer_id = None
    app._statuses.clear()

    class _Discord:
        enabled = True

        def __init__(self):
            self.notes = []

        def notify(self, msg, priority=False):
            self.notes.append((msg, priority))

    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    from src.live.link_state_machine import KIND_READY
    app._link_host.note_connection(KIND_READY, 0, at)
    before = len(logged)
    app._manual_reconnect()
    ignored = [msg for msg, _level in app._statuses if "3003 ignored" in msg]
    assert ignored == []
    assert discord.notes == []
    assert "3003 ignored: quote already READY" in "\n".join(logged[before:])
    assert app._link_host.machine.quote == LinkPhase.READY


def _fire_pending(app, fake, clock, when):
    """Run the armed Quote→Ready callback without rewriting its clock."""
    clock["now"] = when
    fake.set_now(when)
    token = app._quote_ready_timer_id
    assert token is not None
    ms, fn, args = app.root.pending.pop(token)
    fn(*args)
    return ms


class _SubRunner:
    """Enough of a live runner for tick subscribe and a stop-close."""

    def __init__(self, state):
        self.state = state
        self.symbol = "TX00"
        self.suppress_strategy = False
        self.csv_logger = None
        self._aggregated_bars = []
        self._1m_bars = []
        self.broker = type("B", (), {
            "position_size": 1,
            "position_side": type("S", (), {"value": "LONG"})(),
        })()

    def reload_1m_bars(self, progress_cb=None):
        return 0

    def stop(self):
        return {"trades": 0, "pnl": 0, "bars_1m": 0, "bars_agg": 0}

    def log_blocked_signal(self, *args, **kwargs):
        return None


def _arm_subscribe(monkeypatch, app):
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb._broker, "request_ticks", lambda *a, **k: 0)
    app._drain_tick_queue = lambda: None
    app._schedule_status_update = lambda: None
    app._tick_watchdog = rb.TickWatchdog()
    app._real_account_poll_id = None
    app._live_poll_id = None
    app._fill_poll_timer_id = None
    app._live_tick_count = 0
    app._live_history_done = False
    app._live_chart = None
    app._conn_monitor = _give_up_monitor()


def test_warmup_timeout_defers_then_marks_down_without_a_tick(monkeypatch):
    """Deploy warmup must not clear the quote. After subscribe, no tick marks down."""
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop)
    _replay_drop(app, fake, clock, drop)
    runner = _SubRunner(LiveState.IDLE)
    app._live_runner = runner
    app._live_warmup_mode = True
    app._quote_connected = True
    stopped = []
    app._stop_live = lambda: stopped.append("stop")
    _deliver_3002(app)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    assert app._quote_connected is True
    assert app._link_host.machine.quote == LinkPhase.READY
    assert app.root.pending[app._quote_ready_timer_id][0] == 5000
    runner.state = LiveState.RUNNING
    app._live_warmup_mode = False
    _arm_subscribe(monkeypatch, app)
    app._start_live_tick_subscription()
    assert stopped == []
    assert _fire_pending(app, fake, clock, drop + timedelta(seconds=20)) == 5000
    assert app.root.pending[app._quote_ready_timer_id][0] == 15000
    app._last_fresh_tick_at = 0.0
    _fire_pending(app, fake, clock, drop + timedelta(seconds=36))
    assert app._link_host.machine.quote == LinkPhase.DOWN
    assert app._quote_connected is False


def test_warmup_timeout_stays_ready_when_a_tick_follows_the_rearm(monkeypatch):
    """After the deploy window, a fresh tick keeps the quote READY."""
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop)
    _replay_drop(app, fake, clock, drop)
    runner = _SubRunner(LiveState.IDLE)
    app._live_runner = runner
    app._live_warmup_mode = True
    app._quote_connected = True
    app._stop_live = lambda: None
    _deliver_3002(app)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    assert app._quote_ready_timer_id is not None
    assert app.root.pending[app._quote_ready_timer_id][0] == 5000
    runner.state = LiveState.RUNNING
    app._live_warmup_mode = False
    _fire_pending(app, fake, clock, drop + timedelta(seconds=20))
    assert app.root.pending[app._quote_ready_timer_id][0] == 15000
    app._last_fresh_tick_at = time.time()
    _fire_pending(app, fake, clock, drop + timedelta(seconds=36))
    assert app._link_host.machine.quote == LinkPhase.READY
    assert app._quote_connected is True
    assert app._quote_ready_timer_id is None


def test_deploy_deferral_is_capped_at_60s(monkeypatch):
    """A deploy still in flight defers, then marks down once 60s have passed."""
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop)
    _replay_drop(app, fake, clock, drop)
    app._live_runner = _SubRunner(LiveState.WARMING_UP)
    app._live_warmup_mode = True
    app._quote_connected = True
    _deliver_3002(app)
    app._quote_intermediate_seen_at = time.time() - 20
    _fire_pending(app, fake, clock, drop + timedelta(seconds=15))
    assert app._quote_connected is True
    assert app._link_host.machine.quote == LinkPhase.READY
    assert app.root.pending[app._quote_ready_timer_id][0] == 5000
    app._quote_intermediate_seen_at = time.time() - 61
    app._quote_timeout_deferred_since = time.time() - 61
    _fire_pending(app, fake, clock, drop + timedelta(seconds=21))
    assert app._link_host.machine.quote == LinkPhase.DOWN
    assert app._quote_connected is False


def test_deploy_pending_without_a_runner_defers(monkeypatch):
    """_deploy_pending alone is enough to hold the timeout."""
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop)
    _replay_drop(app, fake, clock, drop)
    app._live_runner = None
    app._deploy_pending = True
    app._live_warmup_mode = False
    app._quote_connected = True
    _deliver_3002(app)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    assert app._quote_connected is True
    assert app._link_host.machine.quote == LinkPhase.READY
    assert app.root.pending[app._quote_ready_timer_id][0] == 5000


def test_closed_gap_after_deploy_keeps_the_open_rules(monkeypatch):
    """Once subscribe is up, a 07:30 timeout uses the closed-gap rules."""
    drop = tpe(2026, 11, 12, 7, 30)

    def _finish_deploy(state):
        app, fake, clock, _logged = _ready(monkeypatch, drop - timedelta(minutes=20))
        _replay_drop(app, fake, clock, drop)
        runner = _SubRunner(LiveState.IDLE)
        app._live_runner = runner
        app._live_warmup_mode = True
        app._quote_connected = True
        fake.force_quote_state = state
        _deliver_3002(app)
        _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
        assert app._quote_ready_timer_id is not None
        assert app.root.pending[app._quote_ready_timer_id][0] == 5000
        runner.state = LiveState.RUNNING
        app._live_warmup_mode = False
        _fire_pending(app, fake, clock, drop + timedelta(seconds=20))
        assert app.root.pending[app._quote_ready_timer_id][0] == 15000
        app._last_fresh_tick_at = 0.0
        return app, fake, clock

    app, fake, clock = _finish_deploy(2)
    enters = fake.calls.count("EnterMonitorLONG")
    _fire_pending(app, fake, clock, drop + timedelta(seconds=36))
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.DOWN
    assert machine._quote_enter_not_before == tpe(2026, 11, 12, 8, 45)
    _tick(app, fake, clock, tpe(2026, 11, 12, 8, 44))
    assert fake.calls.count("EnterMonitorLONG") == enters
    assert machine.degraded is False

    app, fake, clock = _finish_deploy(0)
    enters = fake.calls.count("EnterMonitorLONG")
    fired = drop + timedelta(seconds=36)
    _fire_pending(app, fake, clock, fired)
    assert app._link_host.machine._quote_enter_not_before is None
    _tick(app, fake, clock, fired + timedelta(seconds=5))
    assert fake.calls.count("EnterMonitorLONG") == enters + 1
    assert app._link_host.machine.degraded is False


def test_warmup_timeout_does_not_stop_close_a_resumed_position(monkeypatch):
    """A confirmed auto position is not closed by a 3002 during warmup."""
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop)
    _replay_drop(app, fake, clock, drop)
    app._trading_mode = "auto"
    app._trading_guard.real_entry_confirmed = True
    app._live_runner = _SubRunner(LiveState.WARMING_UP)
    app._live_warmup_mode = True
    app._quote_connected = True
    sent = []
    app._send_real_order = lambda *a, **k: sent.append("SendFutureOrderCLR")
    app._warmup_timeout_id = None
    app._position_log = lambda *a, **k: None
    app._update_live_results = lambda: None
    app._update_live_status = lambda: None
    app._stop_main_heartbeat = lambda: None
    app.btn_manual_buy = _Btn()
    app.btn_manual_sell = _Btn()
    app.btn_manual_close = _Btn()
    app.chart_tf_combo = _Btn()
    app.chart_tf_var = _Var("Native")
    app.btn_tv = _Btn()
    app.btn_update = _Btn()
    app.symbol_combo = _Btn()
    app.strategy_combo = _Btn()
    app.bot_name_var = _Var("")
    app.trading_mode_var = _Var("")
    app.mode_combo = _Btn()
    _deliver_3002(app)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    _arm_subscribe(monkeypatch, app)
    app._live_runner.state = LiveState.RUNNING
    app._live_warmup_mode = False
    app._start_live_tick_subscription()
    assert sent == []


def test_tick_before_the_3002_does_not_count_as_fresh(monkeypatch):
    """A tick 10s before the 3002, and none after, marks the quote down."""
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, logged = _ready(monkeypatch, drop)
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 1
    _deliver_3002(app)
    seen = app._quote_intermediate_seen_at
    app._last_fresh_tick_at = seen - 10
    before = len(logged)
    _fire_pending(app, fake, clock, drop + timedelta(seconds=15))
    assert "quote marked down" in "\n".join(logged[before:])
    assert app._link_host.machine.quote == LinkPhase.DOWN
    assert app._quote_connected is False


def test_tick_one_second_after_the_3002_stays_ready(monkeypatch):
    """A tick 1s after the 3002 is fresh even when IsConnected()==0."""
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, logged = _ready(monkeypatch, drop)
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    _deliver_3002(app)
    seen = app._quote_intermediate_seen_at
    app._last_fresh_tick_at = seen + 1
    before = len(logged)
    _fire_pending(app, fake, clock, drop + timedelta(seconds=15))
    assert "ticks are flowing" in "\n".join(logged[before:])
    assert app._link_host.machine.quote == LinkPhase.READY
    assert app._quote_connected is True


def test_tick_exactly_at_the_3002_counts_as_fresh(monkeypatch):
    """tick_at == _quote_intermediate_seen_at is fresh."""
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop)
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    _deliver_3002(app)
    seen = app._quote_intermediate_seen_at
    app._last_fresh_tick_at = seen
    _fire_pending(app, fake, clock, drop + timedelta(seconds=15))
    assert app._link_host.machine.quote == LinkPhase.READY
    assert app._quote_connected is True


@pytest.mark.parametrize("age,ready", [(29.9, True), (30.0, True), (30.1, False)])
def test_fresh_tick_window_boundary(monkeypatch, age, ready):
    """29.9s and 30.0s stay READY. 30.1s marks down.

    The 3002 is 40s before the tick, so the tick is after seen_at and
    the pre-3002 rule does not decide the case.
    """
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, _logged = _ready(monkeypatch, drop)
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 1
    _deliver_3002(app)
    # Freeze the wall clock so 30.0s is not nudged over the limit.
    frozen = time.time()
    monkeypatch.setattr(rb.time, "time", lambda: frozen)
    app._quote_intermediate_seen_at = frozen - 40
    app._last_fresh_tick_at = frozen - age
    _fire_pending(app, fake, clock, drop + timedelta(seconds=15))
    if ready:
        assert app._link_host.machine.quote == LinkPhase.READY
        assert app._quote_connected is True
    else:
        assert app._link_host.machine.quote == LinkPhase.DOWN
        assert app._quote_connected is False


def test_open_session_tick_10s_ago_stays_ready(monkeypatch):
    """A tick 10s ago keeps the quote READY. IsConnected is 0."""
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 0
    before = len(logged)
    _deliver_3002(app)
    app._last_fresh_tick_at = time.time() - 10
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    text = "\n".join(logged[before:])
    assert "ticks are flowing" in text
    assert "quote marked down" not in text
    assert app._link_host.machine.quote == LinkPhase.READY
    assert app._quote_connected is True
    assert app._quote_ready_timer_id is None
    entry, _ = app._trading_guard.decide("auto", "ENTRY_FILL", "LONG")
    assert entry != TradingGuard.BLOCK_LINK


def test_open_session_tick_60s_ago_marks_down(monkeypatch):
    """No fresh tick for 60s marks the quote down. IsConnected stays 1."""
    drop = tpe(2026, 11, 12, 10, 0)
    app, fake, clock, logged = _ready(monkeypatch, drop - timedelta(minutes=5))
    _replay_drop(app, fake, clock, drop)
    fake.force_quote_state = 1
    before = len(logged)
    _deliver_3002(app)
    app._last_fresh_tick_at = time.time() - 60
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    text = "\n".join(logged[before:])
    assert "quote marked down" in text
    assert "ticks are flowing" not in text
    assert app._link_host.machine.quote == LinkPhase.DOWN
    assert app._quote_connected is False


def test_0730_is_connected_0_enters_after_five_seconds(monkeypatch):
    """07:30 and IsConnected 0: EnterMonitor about 5s later, not at 08:45."""
    drop = tpe(2026, 11, 12, 7, 30)
    app, fake, clock, _logged, enters, _open_at = _closed_gap_down(monkeypatch)
    fired = drop + timedelta(seconds=15)
    assert app._link_host.machine._quote_enter_not_before is None
    assert app._link_host.machine._quote_timeout_hold is True
    _tick(app, fake, clock, fired + timedelta(seconds=4))
    assert fake.calls.count("EnterMonitorLONG") == enters
    _tick(app, fake, clock, fired + timedelta(seconds=5))
    assert fake.calls.count("EnterMonitorLONG") == enters + 1
    assert app._link_host.machine.quote == LinkPhase.CONNECTING
    _tick(app, fake, clock, tpe(2026, 11, 12, 8, 44))
    assert app._link_host.machine.degraded is False
    assert "LeaveMonitor" not in fake.calls
    assert "RestartProcess" not in fake.calls
    assert fake.calls.count("LoginSetQuote") == 1


def test_0730_is_connected_1_waits_for_the_open_then_stuck(monkeypatch):
    """07:30 and IsConnected 1: no SKCOM call in the gap. Stuck starts at 08:45."""
    app, fake, clock, _logged, enters, open_at = _closed_gap_down(
        monkeypatch, state=1)
    base = len(fake.calls)
    cursor = tpe(2026, 11, 12, 7, 46)
    while cursor < open_at:
        _tick(app, fake, clock, cursor)
        assert app._link_host.machine.degraded is False
        assert fake.calls.count("EnterMonitorLONG") == enters
        cursor += timedelta(minutes=1)
    _tick(app, fake, clock, open_at - timedelta(seconds=1))
    assert fake.calls[base:] == []
    assert app._link_host.machine.degraded is False
    assert app._link_host.machine._quote_stuck_since is None
    _tick(app, fake, clock, open_at)
    assert app._link_host.machine.degraded is False
    assert app._link_host.machine._quote_stuck_since == open_at
    assert fake.calls.count("EnterMonitorLONG") == enters
    _tick(app, fake, clock, open_at + timedelta(seconds=299))
    assert app._link_host.machine.degraded is False
    _tick(app, fake, clock, open_at + timedelta(seconds=300))
    assert app._link_host.machine.degraded is True
    assert app._link_host.machine.degraded_reason == "stuck"
    assert "LeaveMonitor" not in fake.calls
    assert "RestartProcess" not in fake.calls
