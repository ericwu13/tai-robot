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


def test_0730_missing_3003_logs_clears_flag_and_starts_quote_down(monkeypatch):
    """The 07:30 sequence. Stale ``_quote_connected`` is not a 3003."""
    drop = tpe(2026, 11, 12, 7, 30)
    app, fake, clock, logged = _ready(monkeypatch, drop - timedelta(minutes=30))
    base = len(fake.calls)
    enters = fake.calls.count("EnterMonitorLONG")
    _replay_drop(app, fake, clock, drop)
    assert app._quote_connected is True
    before = len(logged)
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    text = "\n".join(logged[before:])
    assert "Ready (3003) did not arrive" in text
    assert app._quote_connected is False
    machine = app._link_host.machine
    assert machine.quote == LinkPhase.DOWN
    assert machine._quote_enter_not_before == tpe(2026, 11, 12, 8, 45)
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
    _fire_timeout(app, fake, clock, drop + timedelta(seconds=15))
    app._publish_link_heartbeat()
    writer.write(now=1.0)
    data = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert data["link"]["quote"] != "ready"
    assert data["link"]["quote"] == LinkPhase.DOWN.value
    assert data["link"]["degraded"] is False


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
