"""P1 reconnect machine for issue #157.

The machine is not wired into the live reconnect path. These tests
drive it with a fake SKCOM that raises SimulatedAV on the call
sequences behind the 10-06, 10-12 and 10-22 crashes, plus the
CapitalAPI 2.13.57 constraints folded in from the docs check:

* handlers only set flags; EnterMonitor/LeaveMonitor never run inside
  OnConnection (§4-4-a)
* subscribe only after 3003 STOCKS_READY (§4-4-2)
* 3033 drops both links; reply reconnects first (§4-3-h)
* LeaveMonitor is user shutdown only (§4-4-1)

Each MachineRules flag has one mutant. The mutant must raise
SimulatedAV on its scenario; the same scenario with the default rules
must not.
"""

from __future__ import annotations

import ast
import time
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.live.broker_port import BrokerPort
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
from tests.fakes.fake_skcom import (
    LOGIN_ALREADY,
    API_UPDATE_REQUIRED,
    FakeSkcom,
    SimulatedAV,
)

TPE = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=TPE)
ROOT = Path(__file__).resolve().parents[1]
MACHINE_PATH = ROOT / "src" / "live" / "link_state_machine.py"
PORT_PATH = ROOT / "src" / "live" / "broker_port.py"

_READS = {"IsConnected", "IsConnectedByID"}


def tpe(y, mo, d, hh, mm, ss=0, us=0) -> datetime:
    return datetime(y, mo, d, hh, mm, ss, us, tzinfo=TPE)


def pair(rules=None, cfg=None) -> tuple[LinkStateMachine, FakeSkcom]:
    cfg = cfg or LinkMachineConfig()
    fake = FakeSkcom(reply_settle_s=cfg.reply_settle_s)
    return LinkStateMachine(fake, cfg, rules or MachineRules()), fake


def at(machine: LinkStateMachine, fake: FakeSkcom, now: datetime) -> None:
    fake.set_now(now)
    machine.tick(now)


def pump(machine, fake, start, seconds, step=1.0) -> datetime:
    n = int(round(seconds / step))
    now = start
    for _ in range(n + 1):
        at(machine, fake, now)
        now += timedelta(seconds=step)
    return now - timedelta(seconds=step)


def inject_connection(machine, fake, kind, now, code=0) -> None:
    fake.set_now(now)
    if kind == KIND_SOLACE_DOWN:
        fake.note_3033(now)
    elif kind == KIND_CONN_ERROR:
        fake.note_quote_down(now)
    with fake.handling_event():
        machine.on_connection(kind, code, now)
    if kind == KIND_READY:
        fake.note_3003()


def inject_reply_disconnect(machine, fake, now) -> None:
    fake.set_now(now)
    fake.note_solace_reply_disconnect(now)
    with fake.handling_event():
        machine.on_solace_reply_disconnect(now)


def inject_complete(machine, fake, now) -> None:
    fake.set_now(now)
    fake.note_on_complete()
    with fake.handling_event():
        machine.on_complete(now)


def boot_until_entered(rules=None, cfg=None, t0=T0):
    """Logged in, reply reconciled, EnterMonitorLONG called, no 3003 yet."""
    machine, fake = pair(rules, cfg)
    fake.set_now(t0)
    machine.start(t0)
    at(machine, fake, t0)  # ConnectByID
    inject_complete(machine, fake, t0)
    at(machine, fake, t0)  # reconcile + release
    at(machine, fake, t0)  # EnterMonitorLONG
    assert machine.quote == LinkPhase.CONNECTING
    assert machine.reply == LinkPhase.READY
    assert fake.quote_state == 2
    return machine, fake, t0


def boot_ready(rules=None, cfg=None, t0=T0):
    machine, fake, t0 = boot_until_entered(rules, cfg, t0)
    inject_connection(machine, fake, KIND_READY, t0)
    at(machine, fake, t0)
    assert machine.quote == LinkPhase.READY
    assert machine.reply == LinkPhase.READY
    assert not machine.orders_paused
    assert fake.subscribed
    return machine, fake, t0


def mutating(calls) -> list[str]:
    return [name for name in calls if name not in _READS]


def finish_reply_then_quote(machine, fake, now) -> datetime:
    """OnComplete, then EnterMonitor, 3003, subscribe. Returns the last tick."""
    inject_complete(machine, fake, now)
    at(machine, fake, now)
    assert machine.reply == LinkPhase.READY
    assert not machine.orders_paused
    entered = now + timedelta(seconds=1)
    at(machine, fake, entered)
    assert machine.quote == LinkPhase.CONNECTING
    ready = entered + timedelta(seconds=1)
    inject_connection(machine, fake, KIND_READY, ready)
    at(machine, fake, ready)
    assert machine.quote == LinkPhase.READY
    assert fake.subscribed
    return ready


def recover_3033_with_reply_event(machine, fake, now) -> datetime:
    """3033, a fresh reply disconnect, OnComplete, then 3003."""
    inject_connection(machine, fake, KIND_SOLACE_DOWN, now)
    inject_reply_disconnect(machine, fake, now + timedelta(seconds=1))
    now = pump(machine, fake, now + timedelta(seconds=1), 15)
    assert machine.reply == LinkPhase.CONNECTING, machine.reply
    inject_complete(machine, fake, now)
    at(machine, fake, now)
    assert machine.reply == LinkPhase.READY
    now = pump(machine, fake, now, 10)
    assert machine.quote == LinkPhase.CONNECTING
    inject_connection(machine, fake, KIND_READY, now)
    at(machine, fake, now)
    assert machine.quote == LinkPhase.READY
    return now


# ── fake contract ────────────────────────────────────────────────────


class TestFakeContract:
    def test_port_surface(self):
        port: BrokerPort = FakeSkcom()
        for name in (
            "login", "enter_monitor", "leave_monitor", "connect_reply",
            "is_connected", "is_connected_by_id", "subscribe_quotes",
            "reconcile_positions", "release_orders", "logout",
            "restart_process",
        ):
            assert callable(getattr(port, name))

    def test_event_handler_rejects_enter_and_leave(self):
        fake = FakeSkcom()
        with fake.handling_event():
            with pytest.raises(SimulatedAV, match="OnConnection"):
                fake.enter_monitor()
        with fake.handling_event():
            with pytest.raises(SimulatedAV, match="LeaveMonitor"):
                fake.leave_monitor()

    def test_subscribe_before_3003(self):
        fake = FakeSkcom()
        with pytest.raises(SimulatedAV, match="3003"):
            fake.subscribe_quotes()

    def test_3033_quote_before_reply_reconnect(self):
        fake = FakeSkcom()
        fake.set_now(T0)
        fake.note_3033(T0)
        with pytest.raises(SimulatedAV, match="shared Solace"):
            fake.enter_monitor()

    def test_connect_before_reply_disconnect(self):
        fake = FakeSkcom()
        fake.set_now(T0)
        fake.note_3033(T0)
        fake.reply_ever_connected = True
        with pytest.raises(SimulatedAV, match="OnSolaceReplyDisconnect"):
            fake.connect_reply("USER")

    def test_leave_monitor_on_reconnect(self):
        fake = FakeSkcom()
        fake.reconnect_episode = True
        with pytest.raises(SimulatedAV, match="LeaveMonitor"):
            fake.leave_monitor()

    def test_enter_during_reply_rebuild(self):
        fake = FakeSkcom()
        fake.reply_rebuild_inflight = True
        with pytest.raises(SimulatedAV, match="reply rebuild is in flight"):
            fake.enter_monitor()

    def test_connect_inside_settle(self):
        fake = FakeSkcom()
        fake.set_now(T0)
        fake.note_solace_reply_disconnect(T0)
        fake.reply_ever_connected = True
        with pytest.raises(SimulatedAV, match="settle"):
            fake.connect_reply("USER")

    def test_connect_while_id_reports_connected(self):
        fake = FakeSkcom()
        fake.note_solace_reply_disconnect(T0)
        fake.reply_ever_connected = True
        fake.hold_reply_connected = True
        fake.set_now(T0 + timedelta(seconds=10))
        with pytest.raises(SimulatedAV, match="IsConnectedByID"):
            fake.connect_reply("USER")

    def test_enter_while_is_connected(self):
        fake = FakeSkcom()
        fake.force_quote_state = 2
        with pytest.raises(SimulatedAV, match="IsConnected"):
            fake.enter_monitor()

    def test_second_login_and_login_on_reconnect(self):
        fake = FakeSkcom()
        assert fake.login() == 0
        with pytest.raises(SimulatedAV, match="login once"):
            fake.login()
        fresh = FakeSkcom()
        fresh.reconnect_episode = True
        with pytest.raises(SimulatedAV, match="reconnect path"):
            fresh.login()

    def test_logout_and_restart_and_early_release(self):
        fake = FakeSkcom()
        with pytest.raises(SimulatedAV, match="LogOut"):
            fake.logout()
        with pytest.raises(SimulatedAV, match="process restart"):
            fake.restart_process()
        fake.complete_seen = True
        with pytest.raises(SimulatedAV, match="reconcile"):
            fake.release_orders()


# ── bring-up and handlers ────────────────────────────────────────────


class TestBringUp:
    def test_tick_before_start_calls_nothing(self):
        machine, fake = pair()
        at(machine, fake, T0)
        assert fake.calls == []

    def test_login_once_at_process_start(self):
        machine, fake, _t0 = boot_ready()
        assert fake.calls.count("LoginSetQuote") == 1
        machine.start(T0)
        assert fake.calls.count("LoginSetQuote") == 1
        assert mutating(fake.calls) == [
            "LoginSetQuote",
            "ConnectByID",
            "Reconcile",
            "ReleaseOrders",
            "EnterMonitorLONG",
            "SubscribeQuotes",
        ]

    def test_2003_is_success_without_teardown(self):
        machine, fake = pair()
        fake.login_codes = [LOGIN_ALREADY]
        fake.set_now(T0)
        machine.start(T0)
        assert machine.login == LoginPhase.LOGGED_IN
        assert "LeaveMonitor" not in fake.calls

    def test_handlers_only_set_flags(self):
        machine, fake, t0 = boot_ready()
        before = list(fake.calls)
        now = t0 + timedelta(seconds=1)
        with fake.handling_event():
            machine.on_connection(KIND_QUOTE, 0, now)
            machine.on_connection(KIND_REPLY, 0, now)
            machine.on_solace_reply_disconnect(now)
            machine.on_complete(now)
        assert fake.calls == before

    def test_shutdown_is_the_only_leave_monitor(self):
        machine, fake, t0 = boot_ready()
        assert "LeaveMonitor" not in fake.calls
        fake.arm_user_shutdown()
        machine.shutdown(t0)
        assert fake.calls.count("LeaveMonitor") == 1
        assert machine.quote == LinkPhase.DOWN
        assert machine.reply == LinkPhase.DOWN
        at(machine, fake, t0 + timedelta(seconds=1))
        assert fake.calls.count("LeaveMonitor") == 1
        assert fake.calls.count("EnterMonitorLONG") == 1

    def test_no_sleep_in_the_machine_source(self):
        text = MACHINE_PATH.read_text(encoding="utf-8")
        assert "time.sleep" not in text
        assert "subprocess" not in text


# ── replays ──────────────────────────────────────────────────────────


class TestReplays:
    def test_replay_20261022_3033_without_reply_event(self):
        """rss 15:42:26.49 OnConnection 3033, no reply event.

        The old path called LeaveMonitor, LoginSetQuote, ConnectByID
        and EnterMonitorLONG inside the next half-minute and faulted.
        3033 is the shared session: both links stay down until
        OnSolaceReplyDisconnect, which never arrives here.
        """
        boot_at = tpe(2026, 10, 22, 15, 40)
        machine, fake, _boot = boot_ready(t0=boot_at)
        base = list(fake.calls)
        t0 = tpe(2026, 10, 22, 15, 42, 26, 490000)
        inject_connection(machine, fake, KIND_SOLACE_DOWN, t0)
        for delta in (0, 5.09, 24.3, 29.5):
            at(machine, fake, t0 + timedelta(seconds=delta))
        assert machine.quote == LinkPhase.DOWN
        assert machine.reply == LinkPhase.DOWN
        assert machine.orders_paused
        assert not machine.entries_blocked
        assert not machine.degraded
        assert mutating(fake.calls) == mutating(base)
        # Still waiting at the old crash window. Stuck, not a call.
        at(machine, fake, t0 + timedelta(seconds=299))
        assert not machine.degraded
        at(machine, fake, t0 + timedelta(seconds=300))
        assert machine.degraded
        assert machine.degraded_reason == "stuck"
        assert machine.entries_blocked
        assert machine.orders_paused
        assert machine.alerts
        assert "RestartProcess" not in fake.calls
        assert mutating(fake.calls) == mutating(base)
        at(machine, fake, t0 + timedelta(seconds=360))
        assert fake.calls.count("IsConnected") == base.count("IsConnected") + 1
        assert "EnterMonitorLONG" not in fake.calls[len(base):]
        assert "ConnectByID" not in fake.calls[len(base):]
        assert "LeaveMonitor" not in fake.calls
        assert "LogOut" not in fake.calls

    def test_replay_3033_reply_first_then_quote(self):
        machine, fake, t0 = boot_ready()
        t = t0 + timedelta(minutes=30)
        base = mutating(fake.calls)
        inject_connection(machine, fake, KIND_SOLACE_DOWN, t)
        inject_reply_disconnect(machine, fake, t)
        at(machine, fake, t)
        assert machine.quote == LinkPhase.DOWN
        assert machine.reply == LinkPhase.DOWN
        assert machine.orders_paused
        at(machine, fake, t + timedelta(seconds=9))
        assert fake.calls.count("ConnectByID") == 1
        at(machine, fake, t + timedelta(seconds=10))
        assert fake.calls.count("ConnectByID") == 2
        assert machine.reply == LinkPhase.CONNECTING
        assert machine.orders_paused
        assert fake.calls.count("EnterMonitorLONG") == 1
        finish_reply_then_quote(machine, fake, t + timedelta(seconds=11))
        assert not machine.orders_paused
        assert not machine.entries_blocked
        assert not machine.degraded
        assert mutating(fake.calls) == base + [
            "ConnectByID",
            "Reconcile",
            "ReleaseOrders",
            "EnterMonitorLONG",
            "SubscribeQuotes",
        ]

    @pytest.mark.parametrize("slow_s", [13, 24, 37])
    def test_replay_20261012_slow_teardown_then_1108(self, slow_s):
        """Three 10-12 crashes: 3033, a slow reply teardown, then 1108.

        ConnectByID during the teardown window faults. The machine
        waits until IsConnectedByID is 0, takes the 1108, and logs in
        once (2003 = already logged in) with no LeaveMonitor.
        """
        machine, fake, t0 = boot_ready()
        t = tpe(2026, 10, 12, 9, 0)
        fake.teardown_until = t + timedelta(seconds=slow_s)
        fake.connect_codes = [1108, 0]
        fake.login_codes = [LOGIN_ALREADY]
        inject_connection(machine, fake, KIND_SOLACE_DOWN, t)
        inject_reply_disconnect(machine, fake, t)
        before = mutating(fake.calls)
        pump(machine, fake, t, slow_s - 1)
        assert mutating(fake.calls) == before
        assert "LeaveMonitor" not in fake.calls
        at(machine, fake, t + timedelta(seconds=slow_s))
        assert machine.login == LoginPhase.NOT_LOGGED_IN
        at(machine, fake, t + timedelta(seconds=slow_s + 1))
        assert machine.login == LoginPhase.LOGGED_IN
        assert "LeaveMonitor" not in fake.calls
        at(machine, fake, t + timedelta(seconds=slow_s + 2))
        assert machine.reply == LinkPhase.CONNECTING
        finish_reply_then_quote(
            machine, fake, t + timedelta(seconds=slow_s + 3)
        )
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

    def test_replay_20261006_preopen_half_up_across_the_open(self):
        """10-06 night open is 15:00. 3001/3002 and no 3003.

        IsConnected stays 2. The open and open+90 must not produce
        LeaveMonitor, Login, or a second EnterMonitorLONG. stuck_limit
        (5 min from the attempt), not the open, is what degrades.
        """
        t0 = tpe(2026, 10, 6, 14, 58)
        machine, fake, _t = boot_until_entered(t0=t0)
        inject_connection(machine, fake, KIND_REPLY, t0)
        inject_connection(machine, fake, KIND_QUOTE, t0)
        at(machine, fake, t0)  # start the stuck clock
        assert machine.quote == LinkPhase.CONNECTING
        for stamp in (
            tpe(2026, 10, 6, 14, 59),
            tpe(2026, 10, 6, 15, 0),
            tpe(2026, 10, 6, 15, 1, 30),
        ):
            at(machine, fake, stamp)
            assert machine.quote == LinkPhase.CONNECTING
            assert not machine.degraded
        assert fake.calls.count("EnterMonitorLONG") == 1
        assert fake.calls.count("LoginSetQuote") == 1
        assert "LeaveMonitor" not in fake.calls
        assert "SubscribeQuotes" not in fake.calls
        at(machine, fake, t0 + timedelta(seconds=300))
        assert machine.degraded
        assert machine.degraded_reason == "stuck"
        assert machine.entries_blocked
        assert fake.calls.count("EnterMonitorLONG") == 1
        assert "LeaveMonitor" not in fake.calls
        assert "RestartProcess" not in fake.calls
        snap = machine.snapshot()
        assert snap["entries_blocked"] is True
        assert snap["quote"] == LinkPhase.DOWN.value
        assert snap["degraded"] is True

    def test_replay_3003_never_arrives_degrades_and_probes(self):
        cfg = LinkMachineConfig(stuck_limit_s=5, probe_interval_s=60)
        machine, fake, t0 = boot_until_entered(cfg=cfg)
        at(machine, fake, t0)
        at(machine, fake, t0 + timedelta(seconds=5))
        assert machine.degraded
        assert machine.entries_blocked
        assert machine.alerts
        assert "RestartProcess" not in fake.calls
        assert fake.calls.count("EnterMonitorLONG") == 1
        reads = fake.calls.count("IsConnected")
        at(machine, fake, t0 + timedelta(seconds=64))
        assert fake.calls.count("IsConnected") == reads
        at(machine, fake, t0 + timedelta(seconds=65))
        assert fake.calls.count("IsConnected") == reads + 1
        assert fake.calls.count("EnterMonitorLONG") == 1
        fake.force_quote_state = 1
        at(machine, fake, t0 + timedelta(seconds=125))
        assert machine.degraded
        assert machine.entries_blocked
        assert fake.calls.count("EnterMonitorLONG") == 1
        machine.manual_reconnect(t0 + timedelta(seconds=125))
        assert not machine.degraded
        assert not machine.entries_blocked
        at(machine, fake, t0 + timedelta(seconds=125))
        assert "LeaveMonitor" not in fake.calls
        assert fake.calls.count("LoginSetQuote") == 1
        assert fake.calls.count("EnterMonitorLONG") == 1
        assert "RestartProcess" not in fake.calls

    def test_replay_subscribe_before_3003_is_rejected(self):
        machine, fake, t0 = boot_until_entered()
        inject_connection(machine, fake, KIND_QUOTE, t0)
        at(machine, fake, t0)
        assert "SubscribeQuotes" not in fake.calls
        assert machine.quote == LinkPhase.CONNECTING
        with pytest.raises(SimulatedAV, match="3003"):
            fake.subscribe_quotes()
        ready = t0 + timedelta(seconds=1)
        inject_connection(machine, fake, KIND_READY, ready)
        at(machine, fake, ready)
        assert machine.quote == LinkPhase.READY
        assert fake.subscribed
        assert fake.calls.count("SubscribeQuotes") == 2  # the rejected one plus the real one

    def test_reply_only_drop_does_not_touch_quote(self):
        machine, fake, t0 = boot_ready()
        t = t0 + timedelta(minutes=5)
        inject_reply_disconnect(machine, fake, t)
        at(machine, fake, t + timedelta(seconds=10))
        assert machine.reply == LinkPhase.CONNECTING
        assert machine.orders_paused
        assert fake.calls.count("EnterMonitorLONG") == 1
        inject_complete(machine, fake, t + timedelta(seconds=11))
        at(machine, fake, t + timedelta(seconds=11))
        assert machine.reply == LinkPhase.READY
        assert machine.quote == LinkPhase.READY
        assert not machine.orders_paused
        assert fake.calls.count("EnterMonitorLONG") == 1
        assert fake.calls.count("ConnectByID") == 2

    def test_quote_and_reply_drop_one_at_a_time(self):
        machine, fake, t0 = boot_ready()
        t = t0 + timedelta(minutes=5)
        base = mutating(fake.calls)
        inject_connection(machine, fake, KIND_CONN_ERROR, t)
        inject_reply_disconnect(machine, fake, t)
        at(machine, fake, t + timedelta(seconds=10))
        assert machine.reply == LinkPhase.CONNECTING
        assert fake.calls.count("EnterMonitorLONG") == 1
        finish_reply_then_quote(machine, fake, t + timedelta(seconds=11))
        assert mutating(fake.calls) == base + [
            "ConnectByID",
            "Reconcile",
            "ReleaseOrders",
            "EnterMonitorLONG",
            "SubscribeQuotes",
        ]

    def test_quote_only_drop_never_calls_connect_by_id(self):
        machine, fake, t0 = boot_ready()
        t = t0 + timedelta(minutes=5)
        inject_connection(machine, fake, KIND_CONN_ERROR, t)
        at(machine, fake, t + timedelta(seconds=4))
        assert fake.calls.count("EnterMonitorLONG") == 1
        at(machine, fake, t + timedelta(seconds=5))
        assert machine.quote == LinkPhase.CONNECTING
        assert fake.calls.count("ConnectByID") == 1
        assert fake.calls.count("LoginSetQuote") == 1
        assert not machine.orders_paused
        ready = t + timedelta(seconds=6)
        inject_connection(machine, fake, KIND_READY, ready)
        at(machine, fake, ready)
        assert machine.quote == LinkPhase.READY

    def test_missing_3003_retries_enter_only_after_is_connected_0(self):
        machine, fake, t0 = boot_until_entered()
        fake.quote_state = 0
        fake.quote_inflight = False
        at(machine, fake, t0 + timedelta(seconds=59))
        assert fake.calls.count("EnterMonitorLONG") == 1
        assert machine.quote == LinkPhase.CONNECTING
        at(machine, fake, t0 + timedelta(seconds=60))
        assert machine.quote == LinkPhase.DOWN
        assert fake.calls.count("EnterMonitorLONG") == 1
        at(machine, fake, t0 + timedelta(seconds=61))
        assert fake.calls.count("EnterMonitorLONG") == 2
        assert machine.quote == LinkPhase.CONNECTING
        assert "LeaveMonitor" not in fake.calls
        assert fake.calls.count("LoginSetQuote") == 1
        assert not machine.degraded

    def test_after_queue_keeps_firing_during_connecting(self, monkeypatch):
        def _asleep(*_a, **_k):
            raise AssertionError("tick blocked in sleep")

        monkeypatch.setattr(time, "sleep", _asleep)
        machine, fake, t0 = boot_until_entered()
        fired = []
        due = t0 + timedelta(seconds=2)

        def on_clock(now):
            if now >= due and not fired:
                fired.append(now)

        now = t0
        for _ in range(6):
            on_clock(now)
            at(machine, fake, now)
            now += timedelta(seconds=1)
        assert fired == [due]
        assert machine.quote == LinkPhase.CONNECTING
        assert "SubscribeQuotes" not in fake.calls

    def test_three_login_failures_then_manual_reconnect(self):
        machine, fake = pair()
        fake.login_codes = [1, 1, 1, 0]
        fake.set_now(T0)
        machine.start(T0)
        at(machine, fake, T0 + timedelta(seconds=5))
        at(machine, fake, T0 + timedelta(seconds=10))
        assert machine.degraded
        assert machine.degraded_reason == "login_failures"
        assert machine.entries_blocked
        assert machine.login_failures == 3
        at(machine, fake, T0 + timedelta(seconds=20))
        assert fake.calls.count("LoginSetQuote") == 3
        assert "RestartProcess" not in fake.calls
        reads = fake.calls.count("IsConnected")
        at(machine, fake, T0 + timedelta(seconds=70))
        assert fake.calls.count("IsConnected") == reads + 1
        assert fake.calls.count("LoginSetQuote") == 3
        machine.manual_reconnect(T0 + timedelta(seconds=70))
        assert not machine.entries_blocked
        at(machine, fake, T0 + timedelta(seconds=70))
        assert machine.login == LoginPhase.LOGGED_IN
        assert fake.calls.count("LoginSetQuote") == 4
        assert "LeaveMonitor" not in fake.calls
        assert "RestartProcess" not in fake.calls

    def test_9996_degrades_immediately(self):
        machine, fake = pair()
        fake.login_codes = [API_UPDATE_REQUIRED, 0]
        fake.set_now(T0)
        machine.start(T0)
        assert machine.degraded
        assert machine.degraded_reason == "api_update_required"
        assert machine.entries_blocked
        at(machine, fake, T0 + timedelta(seconds=30))
        assert fake.calls == ["LoginSetQuote"]
        assert "RestartProcess" not in fake.calls


# ── Reed review: 3033 reset, 1108 wedge, in-flight ConnectByID ───────


class TestReedReview:
    def test_A_second_3033_without_reply_event_no_connect_by_id(self):
        """A finished 3033 must not authorize the next one."""
        machine, fake, t0 = boot_ready()
        done = recover_3033_with_reply_event(
            machine, fake, t0 + timedelta(minutes=1)
        )
        later = done + timedelta(hours=2)
        inject_connection(machine, fake, KIND_SOLACE_DOWN, later)
        before = len(fake.calls)
        pump(machine, fake, later, 120)
        new = mutating(fake.calls[before:])
        assert "ConnectByID" not in new, new
        assert machine.reply == LinkPhase.DOWN
        assert machine.quote == LinkPhase.DOWN
        assert not machine.degraded

    def test_B_second_reply_disconnect_settle_uses_fresh_time(self):
        machine, fake, t0 = boot_ready()
        done = recover_3033_with_reply_event(
            machine, fake, t0 + timedelta(minutes=1)
        )
        later = done + timedelta(hours=2)
        inject_connection(machine, fake, KIND_SOLACE_DOWN, later)
        start = later + timedelta(seconds=1)
        inject_reply_disconnect(machine, fake, start)
        before = fake.calls.count("ConnectByID")
        pump(machine, fake, start, 9)
        assert fake.calls.count("ConnectByID") == before
        pump(machine, fake, start, 30)
        assert fake.calls.count("ConnectByID") == before + 1
        assert machine.reply == LinkPhase.CONNECTING

    def test_C_manual_reconnect_after_stuck_gets_fresh_window(self):
        machine, fake, t0 = boot_ready()
        inject_connection(machine, fake, KIND_SOLACE_DOWN, t0)
        stuck = pump(machine, fake, t0, 310)
        assert machine.degraded and machine.degraded_reason == "stuck"
        press = stuck + timedelta(seconds=1)
        before = fake.calls.count("ConnectByID")
        machine.manual_reconnect(press)
        at(machine, fake, press)
        assert not machine.degraded, "manual Reconnect re-degraded on the very next tick"
        assert not machine.entries_blocked
        assert fake.calls.count("ConnectByID") == before
        assert "LeaveMonitor" not in fake.calls
        pump(machine, fake, press, 299)
        assert not machine.degraded
        at(machine, fake, press + timedelta(seconds=300))
        assert machine.degraded
        assert machine.degraded_reason == "stuck"
        assert fake.calls.count("ConnectByID") == before

    def test_D_login_failures_reset_after_success(self):
        machine, fake, t0 = boot_ready()
        t = t0
        for i in range(3):
            t += timedelta(hours=1)
            inject_connection(machine, fake, KIND_CONN_ERROR, t)
            fake.enter_codes = [1108]
            fake.login_codes = [1108, 0]
            t = pump(machine, fake, t, 30)
            assert not machine.degraded, (i, machine.degraded_reason, machine.login_failures)
            assert machine.login == LoginPhase.LOGGED_IN, (i, machine.login)
            assert machine.login_failures == 0, (i, machine.login_failures)

    def test_E_slow_on_complete_no_second_connect_by_id(self):
        """OnComplete past 60s: one ConnectByID, then the late complete."""
        machine, fake, t0 = boot_ready()
        inject_connection(machine, fake, KIND_SOLACE_DOWN, t0)
        start = t0 + timedelta(seconds=1)
        inject_reply_disconnect(machine, fake, start)
        before = fake.calls.count("ConnectByID")
        held = pump(machine, fake, start, 120)
        assert fake.calls.count("ConnectByID") == before + 1
        assert machine.reply == LinkPhase.CONNECTING
        assert not machine.degraded
        inject_complete(machine, fake, held)
        at(machine, fake, held)
        assert machine.reply == LinkPhase.READY
        assert fake.calls.count("ConnectByID") == before + 1
        assert not machine.orders_paused

    def test_F_quote_drop_then_1108_does_not_wedge_silently(self):
        """3021 → EnterMonitorLONG 1108 → login OK degrades, it does not wedge.

        Alert, block entries, probe IsConnected, wait for manual Reconnect.
        """
        machine, fake, t0 = boot_ready()
        t = t0 + timedelta(hours=1)
        inject_connection(machine, fake, KIND_CONN_ERROR, t)
        fake.enter_codes = [1108]
        fake.login_codes = [0]
        connects = fake.calls.count("ConnectByID")
        enters = fake.calls.count("EnterMonitorLONG")
        reads = fake.calls.count("IsConnected")
        end = pump(machine, fake, t, 3600, step=5)
        assert machine.degraded
        assert machine.degraded_reason == "stuck"
        assert machine.entries_blocked
        assert machine.orders_paused
        assert machine.login == LoginPhase.LOGGED_IN
        assert machine.quote == LinkPhase.DOWN
        assert machine.reply == LinkPhase.DOWN
        assert machine.alerts
        assert "manual Reconnect" in machine.alerts[-1]
        assert fake.calls.count("ConnectByID") == connects
        assert fake.calls.count("EnterMonitorLONG") == enters + 1
        assert fake.calls.count("IsConnected") >= reads + 2
        assert "RestartProcess" not in fake.calls
        assert "LeaveMonitor" not in fake.calls
        press = end + timedelta(seconds=1)
        machine.manual_reconnect(press)
        at(machine, fake, press)
        assert not machine.degraded
        assert not machine.entries_blocked
        assert fake.calls.count("ConnectByID") == connects
        assert "LeaveMonitor" not in fake.calls


# ── mutants: one per rule, each killed by its scenario ──────────────


def scenario_events(rules):
    machine, fake = pair(rules)
    fake.set_now(T0)
    machine.start(T0)
    with fake.handling_event():
        machine.on_connection(KIND_CONN_ERROR, 0, T0)


def scenario_subscribe(rules):
    machine, fake, t0 = boot_until_entered(rules)
    inject_connection(machine, fake, KIND_QUOTE, t0)
    at(machine, fake, t0)


def scenario_solace_3033(rules):
    machine, fake, t0 = boot_ready(rules)
    t = t0 + timedelta(minutes=10)
    inject_connection(machine, fake, KIND_SOLACE_DOWN, t)
    pump(machine, fake, t, 6)


def scenario_leave(rules):
    machine, fake, t0 = boot_ready(rules)
    t = t0 + timedelta(minutes=1)
    inject_connection(machine, fake, KIND_CONN_ERROR, t)
    at(machine, fake, t)


def scenario_login_once(rules):
    scenario_leave(rules)


def scenario_logout(rules):
    scenario_leave(rules)


def scenario_quote_gate(rules):
    machine, fake, t0 = boot_ready(rules)
    t = t0 + timedelta(minutes=1)
    inject_connection(machine, fake, KIND_CONN_ERROR, t)
    fake.force_quote_state = 2
    pump(machine, fake, t, 6)


def scenario_enter_once(rules):
    machine, fake, t0 = boot_until_entered(rules)
    at(machine, fake, t0)


def scenario_reply_settle(rules):
    machine, fake, t0 = boot_ready(rules)
    t = t0 + timedelta(minutes=1)
    inject_reply_disconnect(machine, fake, t)
    at(machine, fake, t)


def scenario_reply_gate(rules):
    machine, fake, t0 = boot_ready(rules)
    t = t0 + timedelta(minutes=1)
    inject_reply_disconnect(machine, fake, t)
    fake.hold_reply_connected = True
    fake.held_reply_state = 1
    at(machine, fake, t + timedelta(seconds=10))


def scenario_one_link(rules):
    boot_ready(rules)


def scenario_orders(rules):
    boot_ready(rules)


def scenario_degraded(rules):
    cfg = LinkMachineConfig(stuck_limit_s=5)
    machine, fake, t0 = boot_until_entered(rules, cfg)
    at(machine, fake, t0)
    at(machine, fake, t0 + timedelta(seconds=5))


def scenario_reset_3033(rules):
    machine, fake, t0 = boot_ready(rules)
    done = recover_3033_with_reply_event(
        machine, fake, t0 + timedelta(minutes=1)
    )
    later = done + timedelta(hours=2)
    inject_connection(machine, fake, KIND_SOLACE_DOWN, later)
    at(machine, fake, later)


def scenario_reply_once(rules):
    machine, fake, t0 = boot_ready(rules)
    t = t0 + timedelta(minutes=1)
    inject_connection(machine, fake, KIND_SOLACE_DOWN, t)
    inject_reply_disconnect(machine, fake, t)
    at(machine, fake, t + timedelta(seconds=10))
    at(machine, fake, t + timedelta(seconds=71))


def scenario_stuck_after_login(rules):
    machine, fake, t0 = boot_ready(rules)
    t = t0 + timedelta(hours=1)
    inject_connection(machine, fake, KIND_CONN_ERROR, t)
    fake.enter_codes = [1108]
    fake.login_codes = [0]
    pump(machine, fake, t, 10)


def scenario_manual_clears_stuck(rules):
    cfg = LinkMachineConfig(stuck_limit_s=5)
    machine, fake, t0 = boot_ready(rules, cfg)
    t = t0 + timedelta(minutes=1)
    inject_connection(machine, fake, KIND_SOLACE_DOWN, t)
    pump(machine, fake, t, 6)
    machine.manual_reconnect(t + timedelta(seconds=6))


# field, scenario, SimulatedAV match, rule the mutant deletes
MUTANTS = [
    (
        "events_flag_only",
        scenario_events,
        "OnConnection",
        "§4-4-a: event handlers only set flags; no EnterMonitor inside OnConnection",
    ),
    (
        "subscribe_after_3003",
        scenario_subscribe,
        "3003",
        "§4-4-2: subscribe only after 3003 STOCKS_READY",
    ),
    (
        "solace_3033_both_links_reply_first",
        scenario_solace_3033,
        "shared Solace",
        "3033 marks both links down and reconnects reply before quote",
    ),
    (
        "leave_monitor_reconnect_forbidden",
        scenario_leave,
        "LeaveMonitor",
        "§4-4-1: never LeaveMonitor on reconnect (it drops every link)",
    ),
    (
        "login_once",
        scenario_login_once,
        "login once",
        "log in once per process; no Login on the reconnect path",
    ),
    (
        "logout_forbidden",
        scenario_logout,
        "LogOut",
        "no LogOut on the reconnect path",
    ),
    (
        "quote_is_connected_gate",
        scenario_quote_gate,
        "IsConnected",
        "OUR safety design: EnterMonitorLONG only when IsConnected()==0",
    ),
    (
        "enter_once_while_connecting",
        scenario_enter_once,
        "IsConnected",
        "do not call EnterMonitorLONG again while waiting for 3003",
    ),
    (
        "reply_settle",
        scenario_reply_settle,
        "settle",
        "§4-3-h: wait out the reply settle before ConnectByID",
    ),
    (
        "reply_is_connected_by_id_gate",
        scenario_reply_gate,
        "IsConnectedByID",
        "OUR safety design: ConnectByID only when IsConnectedByID==0",
    ),
    (
        "one_link_at_a_time",
        scenario_one_link,
        "reply rebuild is in flight",
        "OUR safety design: one link transition in flight at a time",
    ),
    (
        "orders_after_complete_and_reconcile",
        scenario_orders,
        "reconcile",
        "orders resume only after OnComplete and a position reconcile",
    ),
    (
        "degraded_instead_of_restart",
        scenario_degraded,
        "process restart",
        "stuck or 3 login failures: stay down, alert, probe; never restart",
    ),
    (
        "reset_3033_episode",
        scenario_reset_3033,
        "OnSolaceReplyDisconnect",
        "a finished 3033 does not authorize ConnectByID on the next 3033",
    ),
    (
        "reply_connect_once_until_complete",
        scenario_reply_once,
        "re-called",
        "one ConnectByID until OnComplete; no second call while it is in flight",
    ),
    (
        "stuck_after_login_recovery",
        scenario_stuck_after_login,
        "OnSolaceReplyDisconnect",
        "3021 → 1108 → login OK with reply still down degrades; no ConnectByID",
    ),
    (
        "manual_reconnect_clears_stuck",
        scenario_manual_clears_stuck,
        "OnSolaceReplyDisconnect",
        "manual Reconnect clears the stuck window instead of ConnectByID",
    ),
]


@pytest.mark.parametrize(
    "name,scenario,match,note",
    MUTANTS,
    ids=[row[0] for row in MUTANTS],
)
def test_mutant_is_killed(name, scenario, match, note):
    del note
    rules = MachineRules(**{name: False})
    with pytest.raises(SimulatedAV, match=match):
        scenario(rules)


@pytest.mark.parametrize(
    "name,scenario,match,note",
    MUTANTS,
    ids=[row[0] for row in MUTANTS],
)
def test_baseline_survives_mutant_scenario(name, scenario, match, note):
    del name, match, note
    scenario(MachineRules())


def test_every_rule_has_exactly_one_mutant():
    rule_names = [item.name for item in fields(MachineRules)]
    mutant_names = [row[0] for row in MUTANTS]
    assert mutant_names == rule_names
    assert len(mutant_names) == len(set(mutant_names))


# ── module boundaries ────────────────────────────────────────────────


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_p1_modules_do_not_import_tk_com_or_the_half_up_clock():
    banned_prefixes = (
        "tkinter",
        "comtypes",
        "pythoncom",
        "win32com",
        "subprocess",
        "src.live.reconnect_controller",
        "src.regime",
        "src.market_data",
        "src.live.live_runner",
    )
    for path in (MACHINE_PATH, PORT_PATH):
        imported = _imported_modules(path)
        for name in imported:
            for prefix in banned_prefixes:
                assert not name.startswith(prefix), f"{path.name} imports {name}"
    text = MACHINE_PATH.read_text(encoding="utf-8")
    for banned in ("open+90", "is_market_open", "current_session", "08:45", "15:00"):
        assert banned not in text
    assert text.count("OUR safety design, not a vendor rule") >= 3
