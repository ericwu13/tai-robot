"""P2: live host pump over LinkStateMachine + FakeSkcom.

The GUI and the headless bot share ``LinkHost``. COM callbacks only
enqueue. ``tick`` runs on the main thread and is the only broker caller.
These replays are the 10-22 rss drop, the 10-12 slow ConnectByID + 1108,
the pre-open half-up, a reply-only drop, a quote+reply drop, and a 3003
that never arrives.

Mutants that call LeaveMonitor or Login on the reconnect path, EnterMonitor
while CONNECTING, ConnectByID inside the settle or while IsConnectedByID
is not 0, or EnterMonitor while IsConnected is 1 or 2 must raise.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live.com_broker_port import ComBrokerPort, ReconnectForbidden
from src.live.headless_app import HeadlessBotApp
from src.live.link_host import adopt_after_process_login
from src.live.link_state_machine import (
    KIND_CONN_ERROR,
    KIND_QUOTE,
    KIND_READY,
    KIND_REPLY,
    KIND_SOLACE_DOWN,
    LinkMachineConfig,
    LinkPhase,
    LinkStateMachine,
    LoginPhase,
    MachineRules,
)
from src.live.main_heartbeat import HeartbeatWriter
from src.live.trading_guard import TradingGuard
from tests.fakes.fake_skcom import LOGIN_ALREADY, FakeSkcom, SimulatedAV

TPE = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=TPE)
USER = "USER"
ROOT = Path(__file__).resolve().parents[1]
_READS = {"IsConnected", "IsConnectedByID"}
_FORBIDDEN = (
    "SKQuoteLib_LeaveMonitor",
    "SKCenterLib_LoginSetQuote",
    "SKCenterLib_LogOut",
    'call_com("LeaveMonitor"',
    'call_com("LoginSetQuote"',
    'call_com("LogOut"',
    "SKQuoteLib_EnterMonitorLONG",
    "SKReplyLib_ConnectByID",
)


def tpe(y, mo, d, hh, mm, ss=0, us=0) -> datetime:
    return datetime(y, mo, d, hh, mm, ss, us, tzinfo=TPE)


def mutating(calls) -> list[str]:
    return [name for name in calls if name not in _READS]


def step(host, fake, now):
    fake.set_now(now)
    return host.tick(now)


def prime(fake, now) -> None:
    """Process-start login, outside the host. ``_do_login`` does this.

    The fake faults EnterMonitor while a reply rebuild is in flight, so
    the test world records OnComplete between ConnectByID and
    EnterMonitorLONG. The live ``_do_login`` still issues those two calls
    back to back; the machine is adopted after both have returned and
    waits for the host's OnComplete flag before it will touch the quote
    link again.
    """
    fake.set_now(now)
    assert fake.login() == 0
    assert fake.connect_reply(USER) == 0
    fake.note_on_complete()
    assert fake.enter_monitor() == 0


def _adopt(fake, now, *, cfg=None, rules=None):
    before = list(fake.calls)
    host = adopt_after_process_login(
        fake, now=now, user_id=USER, config=cfg, rules=rules)
    assert fake.calls == before
    return host


def boot_quote_connecting(rules=None, cfg=None, t0=T0):
    cfg = cfg or LinkMachineConfig()
    fake = FakeSkcom(reply_settle_s=cfg.reply_settle_s)
    prime(fake, t0)
    host = _adopt(fake, t0, cfg=cfg, rules=rules)
    fake.note_on_complete()
    host.note_complete(t0)
    step(host, fake, t0)
    assert host.machine.reply == LinkPhase.READY
    assert host.machine.quote == LinkPhase.CONNECTING
    return host, fake, t0


def boot_ready(rules=None, cfg=None, t0=T0):
    host, fake, t0 = boot_quote_connecting(rules, cfg, t0)
    fake.note_3003()
    host.note_connection(KIND_READY, 0, t0)
    step(host, fake, t0)
    assert host.machine.quote == LinkPhase.READY
    assert host.entries_allowed()
    assert fake.subscribed
    return host, fake, t0


def flag_3033(host, fake, now) -> None:
    before = len(fake.calls)
    with fake.handling_event():
        queued = (KIND_SOLACE_DOWN, 0)
    assert len(fake.calls) == before
    fake.note_3033(now)
    host.note_connection(queued[0], queued[1], now)


def flag_quote_down(host, fake, now) -> None:
    fake.note_quote_down(now)
    host.note_connection(KIND_CONN_ERROR, 0, now)


def flag_reply(host, fake, now) -> None:
    fake.note_solace_reply_disconnect(now)
    host.note_reply_disconnect(now)


def flag_complete(host, fake, now) -> None:
    fake.note_on_complete()
    host.note_complete(now)


def flag_ready(host, fake, now) -> None:
    fake.note_3003()
    host.note_connection(KIND_READY, 0, now)


def _body(source: str, name: str) -> str:
    match = re.search(
        rf"def {name}\(.*?(?=\n    def |\nclass |\Z)",
        source,
        re.DOTALL,
    )
    assert match, name
    return match.group(0)


# ── replays ──────────────────────────────────────────────────────────


def test_replay_20261022_rss_3033_without_reply_event():
    """rss 15:42:26.49 OnConnection 3033, no reply event. No teardown."""
    t0 = tpe(2026, 10, 22, 15, 40)
    host, fake, _boot = boot_ready(t0=t0)
    base = list(fake.calls)
    t = tpe(2026, 10, 22, 15, 42, 26, 490000)
    flag_3033(host, fake, t)
    for delta in (0, 5.09, 24.3, 29.5, 299):
        step(host, fake, t + timedelta(seconds=delta))
        assert not host.machine.degraded
    assert host.machine.quote == LinkPhase.DOWN
    assert host.machine.reply == LinkPhase.DOWN
    assert host.machine.orders_paused
    assert not host.entries_allowed()
    assert not host.machine.entries_blocked
    step(host, fake, t + timedelta(seconds=300))
    assert host.machine.degraded
    assert host.machine.degraded_reason == "stuck"
    assert host.machine.entries_blocked
    assert host.machine.alerts
    assert "RestartProcess" not in fake.calls
    assert "LeaveMonitor" not in fake.calls
    assert "LogOut" not in fake.calls
    assert fake.calls.count("LoginSetQuote") == 1
    assert mutating(fake.calls) == mutating(base)
    reads = fake.calls.count("IsConnected")
    step(host, fake, t + timedelta(seconds=360))
    assert fake.calls.count("IsConnected") == reads + 1
    assert "EnterMonitorLONG" not in fake.calls[len(base):]
    assert "ConnectByID" not in fake.calls[len(base):]


@pytest.mark.parametrize("slow_s", [13, 24, 37])
def test_replay_20261012_slow_connect_then_1108(slow_s):
    host, fake, _t0 = boot_ready()
    t = tpe(2026, 10, 12, 9, 0)
    fake.teardown_until = t + timedelta(seconds=slow_s)
    fake.connect_codes = [1108, 0]
    fake.login_codes = [LOGIN_ALREADY]
    flag_3033(host, fake, t)
    flag_reply(host, fake, t)
    before = mutating(fake.calls)
    now = t
    while (now - t).total_seconds() < slow_s - 1:
        step(host, fake, now)
        now += timedelta(seconds=1)
    assert mutating(fake.calls) == before
    assert "LeaveMonitor" not in fake.calls
    step(host, fake, t + timedelta(seconds=slow_s))
    assert host.machine.login.value == "not_logged_in"
    step(host, fake, t + timedelta(seconds=slow_s + 1))
    assert host.machine.login.value == "logged_in"
    assert "LeaveMonitor" not in fake.calls
    step(host, fake, t + timedelta(seconds=slow_s + 2))
    assert host.machine.reply == LinkPhase.CONNECTING
    done = t + timedelta(seconds=slow_s + 3)
    flag_complete(host, fake, done)
    step(host, fake, done)
    assert host.machine.reply == LinkPhase.READY
    entered = done + timedelta(seconds=1)
    step(host, fake, entered)
    assert host.machine.quote == LinkPhase.CONNECTING
    ready = entered + timedelta(seconds=1)
    flag_ready(host, fake, ready)
    step(host, fake, ready)
    assert host.machine.quote == LinkPhase.READY
    assert host.entries_allowed()
    assert mutating(fake.calls) == before + [
        "ConnectByID",
        "LoginSetQuote",
        "ConnectByID",
        "Reconcile",
        "ReleaseOrders",
        "EnterMonitorLONG",
        "SubscribeQuotes",
    ]
    assert "RestartProcess" not in fake.calls
    assert "LogOut" not in fake.calls


def test_replay_preopen_half_up_no_3003_across_the_open():
    """3001/3002 and no 3003. The open does not re-enter or re-login."""
    t0 = tpe(2026, 10, 6, 14, 58)
    host, fake, _t = boot_quote_connecting(t0=t0)
    host.note_connection(KIND_REPLY, 0, t0)
    host.note_connection(KIND_QUOTE, 0, t0)
    step(host, fake, t0)
    assert host.machine.quote == LinkPhase.CONNECTING
    for stamp in (
        tpe(2026, 10, 6, 14, 59),
        tpe(2026, 10, 6, 15, 0),
        tpe(2026, 10, 6, 15, 1, 30),
    ):
        step(host, fake, stamp)
        assert host.machine.quote == LinkPhase.CONNECTING
        assert not host.machine.degraded
    assert fake.calls.count("EnterMonitorLONG") == 1
    assert fake.calls.count("LoginSetQuote") == 1
    assert "LeaveMonitor" not in fake.calls
    assert "SubscribeQuotes" not in fake.calls
    assert not host.entries_allowed()
    step(host, fake, t0 + timedelta(seconds=300))
    assert host.machine.degraded
    assert host.machine.degraded_reason == "stuck"
    assert host.machine.entries_blocked
    assert fake.calls.count("EnterMonitorLONG") == 1
    assert "LeaveMonitor" not in fake.calls
    assert "RestartProcess" not in fake.calls


def test_replay_reply_only_drop_does_not_touch_quote():
    host, fake, t0 = boot_ready()
    t = t0 + timedelta(minutes=5)
    flag_reply(host, fake, t)
    step(host, fake, t + timedelta(seconds=10))
    assert host.machine.reply == LinkPhase.CONNECTING
    assert host.machine.orders_paused
    assert not host.entries_allowed()
    assert fake.calls.count("EnterMonitorLONG") == 1
    done = t + timedelta(seconds=11)
    flag_complete(host, fake, done)
    step(host, fake, done)
    assert host.machine.reply == LinkPhase.READY
    assert host.machine.quote == LinkPhase.READY
    assert host.entries_allowed()
    assert fake.calls.count("EnterMonitorLONG") == 1
    assert fake.calls.count("ConnectByID") == 2
    assert "LeaveMonitor" not in fake.calls


def test_replay_quote_and_reply_drop_one_at_a_time():
    host, fake, t0 = boot_ready()
    t = t0 + timedelta(minutes=5)
    base = mutating(fake.calls)
    flag_quote_down(host, fake, t)
    flag_reply(host, fake, t)
    step(host, fake, t + timedelta(seconds=10))
    assert host.machine.reply == LinkPhase.CONNECTING
    assert fake.calls.count("EnterMonitorLONG") == 1
    done = t + timedelta(seconds=11)
    flag_complete(host, fake, done)
    step(host, fake, done)
    entered = done + timedelta(seconds=1)
    step(host, fake, entered)
    ready = entered + timedelta(seconds=1)
    flag_ready(host, fake, ready)
    step(host, fake, ready)
    assert host.entries_allowed()
    assert mutating(fake.calls) == base + [
        "ConnectByID",
        "Reconcile",
        "ReleaseOrders",
        "EnterMonitorLONG",
        "SubscribeQuotes",
    ]
    assert "LeaveMonitor" not in fake.calls
    assert "LogOut" not in fake.calls


def test_replay_3003_never_degrades_and_probes():
    cfg = LinkMachineConfig(stuck_limit_s=5, probe_interval_s=60)
    host, fake, t0 = boot_quote_connecting(cfg=cfg)
    step(host, fake, t0)
    step(host, fake, t0 + timedelta(seconds=5))
    assert host.machine.degraded
    assert host.machine.entries_blocked
    assert not host.entries_allowed()
    assert host.machine.alerts
    assert "RestartProcess" not in fake.calls
    assert fake.calls.count("EnterMonitorLONG") == 1
    reads = fake.calls.count("IsConnected")
    step(host, fake, t0 + timedelta(seconds=64))
    assert fake.calls.count("IsConnected") == reads
    step(host, fake, t0 + timedelta(seconds=65))
    assert fake.calls.count("IsConnected") == reads + 1
    assert fake.calls.count("EnterMonitorLONG") == 1
    fake.force_quote_state = 1
    later = t0 + timedelta(seconds=125)
    step(host, fake, later)
    assert host.machine.degraded
    assert host.machine.entries_blocked
    host.manual_reconnect(later)
    assert not host.machine.degraded
    assert not host.entries_allowed()
    step(host, fake, later)
    assert not host.entries_allowed()
    assert "LeaveMonitor" not in fake.calls
    assert fake.calls.count("LoginSetQuote") == 1
    assert fake.calls.count("EnterMonitorLONG") == 1
    assert "RestartProcess" not in fake.calls


def test_adopt_then_start_does_not_login_again():
    host, fake, t0 = boot_ready()
    before = fake.calls.count("LoginSetQuote")
    host.machine.start(t0)
    assert fake.calls.count("LoginSetQuote") == before == 1


def test_public_adopt_does_not_call_the_broker():
    """The machine method owns the process-login state. No port calls."""
    fake = FakeSkcom()
    now = T0
    machine = LinkStateMachine(fake, LinkMachineConfig(user_id=USER))
    machine.adopt_after_process_login(now)
    assert fake.calls == []
    assert machine.login == LoginPhase.LOGGED_IN
    assert machine.quote == LinkPhase.CONNECTING
    assert machine.reply == LinkPhase.CONNECTING
    assert machine.orders_paused
    assert machine.reply_reconciled is False
    assert machine.entries_blocked is False
    machine.start(now)
    assert fake.calls == []


def test_second_adopt_does_not_restart_the_stuck_clock():
    """A second adopt must not move the OnComplete wait that degrades."""
    cfg = LinkMachineConfig(user_id=USER, stuck_limit_s=5)
    fake = FakeSkcom(reply_settle_s=cfg.reply_settle_s)
    machine = LinkStateMachine(fake, cfg)
    t0 = T0
    machine.adopt_after_process_login(t0)
    machine.adopt_after_process_login(t0 + timedelta(seconds=4))
    machine.tick(t0 + timedelta(seconds=5))
    assert machine.degraded
    assert machine.orders_paused
    for name in ("LoginSetQuote", "LeaveMonitor", "EnterMonitorLONG", "ConnectByID"):
        assert name not in fake.calls


def test_host_does_not_assign_machine_private_fields():
    text = (ROOT / "src" / "live" / "link_host.py").read_text(encoding="utf-8")
    for name in (
        "_started",
        "_login_permitted",
        "_quote_connect_started_at",
        "_reply_connect_started_at",
        "_reply_awaiting_complete",
        "_pending_complete",
    ):
        assert name not in text
    assert "adopt_after_process_login(now)" in text
    run = (ROOT / "run_backtest.py").read_text(encoding="utf-8")
    assert "_login_permitted" not in run
    assert "machine._started" not in run


def test_app_attempt_reconnect_does_not_teardown():
    host, fake, _t0 = boot_ready()
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._link_host = host
    app._reconnect_timer_id = None
    app._trading_guard = TradingGuard()
    app._link_gate_latched = False
    app._attempt_reconnect()
    assert "LeaveMonitor" not in fake.calls
    assert "LogOut" not in fake.calls
    assert fake.calls.count("LoginSetQuote") == 1
    assert app._trading_guard.link_entries_blocked is False


# ── mutants ──────────────────────────────────────────────────────────


def test_mutant_leave_monitor_on_reconnect():
    host, fake, t0 = boot_ready(
        rules=MachineRules(leave_monitor_reconnect_forbidden=False))
    t = t0 + timedelta(minutes=30)
    flag_3033(host, fake, t)
    with pytest.raises(SimulatedAV, match="LeaveMonitor"):
        step(host, fake, t)


def test_mutant_login_on_reconnect():
    host, fake, t0 = boot_ready(rules=MachineRules(login_once=False))
    t = t0 + timedelta(minutes=30)
    flag_3033(host, fake, t)
    with pytest.raises(SimulatedAV, match="login once"):
        step(host, fake, t)


def test_mutant_enter_while_connecting():
    host, fake, t0 = boot_quote_connecting(
        rules=MachineRules(enter_once_while_connecting=False))
    with pytest.raises(SimulatedAV, match="IsConnected"):
        step(host, fake, t0)


@pytest.mark.parametrize("state", [1, 2])
def test_mutant_enter_while_is_connected(state):
    host, fake, t0 = boot_ready(
        rules=MachineRules(quote_is_connected_gate=False))
    t = t0 + timedelta(minutes=5)
    flag_quote_down(host, fake, t)
    fake.force_quote_state = state
    with pytest.raises(SimulatedAV, match="IsConnected"):
        step(host, fake, t + timedelta(seconds=5))


def test_mutant_connect_inside_settle():
    host, fake, t0 = boot_ready(rules=MachineRules(reply_settle=False))
    t = t0 + timedelta(minutes=5)
    flag_reply(host, fake, t)
    with pytest.raises(SimulatedAV, match="settle"):
        step(host, fake, t)


def test_mutant_connect_while_id_not_zero():
    host, fake, t0 = boot_ready(
        rules=MachineRules(reply_is_connected_by_id_gate=False))
    t = t0 + timedelta(minutes=5)
    flag_reply(host, fake, t)
    fake.hold_reply_connected = True
    fake.held_reply_state = 1
    with pytest.raises(SimulatedAV, match="IsConnectedByID"):
        step(host, fake, t + timedelta(seconds=10))


# ── source scan + both hosts ─────────────────────────────────────────


def test_reconnect_source_has_no_teardown_calls():
    source = (ROOT / "run_backtest.py").read_text(encoding="utf-8")
    body = _body(source, "_attempt_reconnect")
    for forbidden in _FORBIDDEN:
        assert forbidden not in body, forbidden
    manual = _body(source, "_manual_reconnect")
    for forbidden in _FORBIDDEN:
        assert forbidden not in manual, forbidden
    assert "def _check_reconnection(" not in source
    for name in ("OnConnection", "OnComplete", "OnSolaceReplyDisconnect"):
        handler = _body(source, name)
        assert "call_com(" not in handler
        assert "SKQuoteLib_" not in handler
        assert "SKCenterLib_" not in handler
        assert "SKReplyLib_" not in handler


def test_gui_and_headless_share_the_reconnect_method():
    assert HeadlessBotApp._attempt_reconnect is rb.BacktestApp._attempt_reconnect
    assert HeadlessBotApp._drain_ui_queue is rb.BacktestApp._drain_ui_queue
    assert HeadlessBotApp._adopt_link_machine is rb.BacktestApp._adopt_link_machine
    text = (ROOT / "src" / "live" / "headless_app.py").read_text(encoding="utf-8")
    for forbidden in _FORBIDDEN:
        assert forbidden not in text, forbidden
    assert "link={snap['quote']}/{snap['reply']}" in text
    assert "entries_blocked={snap['entries_blocked']}" in text
    assert "_shutdown_link" in text


def test_host_modules_do_not_import_tk_or_comtypes():
    for name in ("link_host.py", "com_broker_port.py"):
        tree = ast.parse((ROOT / "src" / "live" / name).read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module.split(".")[0])
        assert "tkinter" not in imported
        assert "comtypes" not in imported


def test_com_port_logout_and_restart_raise_and_map_calls():
    seen = []

    def call(name, fn, *args):
        seen.append(name)
        return fn(*args)

    libs = (
        SimpleNamespace(SKCenterLib_LoginSetQuote=lambda *a: 0),
        SimpleNamespace(
            SKQuoteLib_EnterMonitorLONG=lambda: 7,
            SKQuoteLib_LeaveMonitor=lambda: 0,
            SKQuoteLib_IsConnected=lambda: 0,
        ),
        SimpleNamespace(
            SKReplyLib_ConnectByID=lambda user: 0,
            SKReplyLib_IsConnectedByID=lambda user: 0,
        ),
        None,
    )
    hooks = {"sub": 0, "rec": 0, "rel": 0}
    logged = []
    port = ComBrokerPort(
        call=call,
        libs=lambda: libs,
        credentials=lambda: ("user", "secret"),
        on_subscribe=lambda: hooks.__setitem__("sub", hooks["sub"] + 1),
        on_reconcile=lambda: hooks.__setitem__("rec", hooks["rec"] + 1),
        on_release=lambda: hooks.__setitem__("rel", hooks["rel"] + 1),
        before_call=logged.append,
    )
    assert port.enter_monitor() == 7
    assert port.is_connected() == 0
    port.subscribe_quotes()
    port.reconcile_positions()
    port.release_orders()
    assert hooks == {"sub": 1, "rec": 1, "rel": 1}
    assert logged[:2] == ["EnterMonitorLONG", "IsConnected"]
    with pytest.raises(ReconnectForbidden):
        port.logout()
    with pytest.raises(ReconnectForbidden):
        port.restart_process()
    assert "LogOut" not in seen
    assert "RestartProcess" not in seen


def test_heartbeat_shows_link_state_when_set(tmp_path):
    writer = HeartbeatWriter(str(tmp_path))
    writer.write(now=1.0)
    plain = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert "link" not in plain
    writer.link_state = {
        "quote": "ready",
        "reply": "ready",
        "degraded": False,
        "entries_blocked": False,
    }
    writer.write(now=2.0)
    data = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert data["link"]["quote"] == "ready"
    assert data["inflight_com_call"] is None
    assert data["ts"] == 2.0


def test_degraded_blocks_real_entries_until_manual_clear():
    """The guard blocks entries only. A confirmed exit still sends."""
    cfg = LinkMachineConfig(stuck_limit_s=5, probe_interval_s=60)
    host, _fake, t0 = boot_quote_connecting(cfg=cfg)
    guard = TradingGuard()
    guard.on_entry_sent()
    step(host, _fake, t0)
    step(host, _fake, t0 + timedelta(seconds=5))
    guard.link_entries_blocked = not host.entries_allowed()
    verdict, _details = guard.decide("auto", "ENTRY_FILL", "LONG")
    assert verdict == guard.BLOCK_LINK
    close, _cd = guard.decide("auto", "TRADE_CLOSE", "LONG")
    assert close == guard.SEND_EXIT
