"""Issue #219 — reply poll when SKReply events never arrive.

IsConnectedByID goes 1, then 0, then 1. No OnConnect / OnComplete /
OnSolaceReplyDisconnect is delivered. The poller must reconnect once
per backoff step, pause entries on 0, lift that pause on 1, keep
exits allowed, cap the backoff at 60s, never log out, and alert once
per down episode.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.live.link_host import LinkHost
from src.live.link_state_machine import (
    KIND_READY,
    LinkMachineConfig,
    LinkPhase,
    LinkStateMachine,
)
from src.live.reply_link_poller import (
    SILENT_ALERT,
    ReplyLinkPoller,
    apply_quote_stuck_recovery,
    backoff_delay_s,
    format_event_sink,
)
from src.live.trading_guard import TradingGuard


T0 = datetime(2026, 10, 28, 6, 36, 48)


class RecordingPort:
    """Reply events never fire. The test flips IsConnectedByID itself."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.reply_state = 1
        self.quote_state = 1

    def login(self) -> int:
        self.calls.append("login")
        return 0

    def enter_monitor(self) -> int:
        self.calls.append("enter_monitor")
        return 0

    def leave_monitor(self) -> int:
        self.calls.append("leave_monitor")
        return 0

    def connect_reply(self, user_id: str) -> int:
        self.calls.append("connect_reply")
        return 0

    def is_connected(self) -> int:
        self.calls.append("is_connected")
        return self.quote_state

    def is_connected_by_id(self, user_id: str) -> int:
        self.calls.append("is_connected_by_id")
        return self.reply_state

    def subscribe_quotes(self) -> None:
        self.calls.append("subscribe_quotes")

    def reconcile_positions(self) -> None:
        self.calls.append("reconcile_positions")

    def release_orders(self) -> None:
        self.calls.append("release_orders")

    def logout(self) -> None:
        self.calls.append("logout")

    def restart_process(self) -> None:
        self.calls.append("restart_process")


def _connects(port: RecordingPort) -> int:
    return port.calls.count("connect_reply")


def _build(alerts: list[str]):
    port = RecordingPort()
    machine = LinkStateMachine(
        port, LinkMachineConfig(user_id="user", stuck_limit_s=30))
    machine.adopt_after_process_login(T0)
    # 3003 already happened for this scenario. The pause under test is
    # the reply link, so the quote link is READY.
    machine.quote = LinkPhase.READY
    host = LinkHost(machine)

    def connect(_user_id: str) -> int:
        return port.connect_reply(_user_id)

    poller = ReplyLinkPoller(
        lambda: machine,
        read=lambda: port.reply_state,
        connect=connect,
        notify=alerts.append,
        log_error=lambda _msg: None,
        log_info=lambda _msg: None,
        reschedule_silent=lambda: None,
    )
    poller.note_login(T0)
    return port, machine, host, poller


def _gate(host: LinkHost, guard: TradingGuard) -> None:
    guard.link_entries_blocked = not host.entries_allowed()


def test_backoff_schedule_caps_at_60s():
    assert [backoff_delay_s(i) for i in range(6)] == [5, 10, 30, 60, 60, 60]


def test_poll_reconnects_pauses_and_alerts_without_reply_events():
    """Fake SKCOM: no reply events. IsConnectedByID is 1, then 0, then 1."""
    alerts: list[str] = []
    port, machine, host, poller = _build(alerts)
    guard = TradingGuard()
    guard.on_entry_sent()

    port.reply_state = 1
    poller.observe(1, T0)
    _gate(host, guard)
    assert machine.orders_paused is False
    assert host.entries_allowed() is True
    assert guard.decide("auto", "ENTRY_FILL", "LONG")[0] == guard.SEND_ENTRY
    assert _connects(port) == 0
    assert alerts == []

    # Drop. No ConnectByID until the first backoff step (5s).
    down_at = T0 + timedelta(seconds=70)
    port.reply_state = 0
    poller.observe(0, down_at)
    _gate(host, guard)
    assert machine.reply == LinkPhase.DOWN
    assert machine.orders_paused is True
    assert host.entries_allowed() is False
    assert guard.decide("auto", "ENTRY_FILL", "LONG")[0] == guard.BLOCK_LINK
    exit_verdict, _details = guard.decide("auto", "TRADE_CLOSE", "LONG")
    assert exit_verdict == guard.SEND_EXIT
    force, _force_details = guard.decide("auto", "FORCE_CLOSE", "LONG")
    assert force == guard.SEND_EXIT
    assert _connects(port) == 0
    assert "logout" not in port.calls
    assert "reconcile_positions" not in port.calls
    assert "release_orders" not in port.calls

    # Still inside the 5s step: a second reading must not connect.
    poller.observe(0, down_at + timedelta(seconds=4))
    assert _connects(port) == 0

    # One ConnectByID per backoff step: 5, then 10, then 30, then 60, then 60.
    steps = [5, 10, 30, 60, 60]
    cursor = down_at
    for expected in range(1, len(steps) + 1):
        cursor = cursor + timedelta(seconds=steps[expected - 1])
        before = _connects(port)
        poller.observe(0, cursor)
        assert _connects(port) == before + 1
        poller.observe(0, cursor)
        assert _connects(port) == before + 1, "one ConnectByID per backoff step"
    assert poller.pending_delay_s(cursor) == 60
    # The 105s step is past 60s of the drop. One alert, and only one,
    # even though later steps keep polling.
    assert alerts == [SILENT_ALERT]
    assert "REPLY CHANNEL SILENT" in alerts[0]
    assert poller.consider_silent(cursor, force=True) is False

    # Back to 1. Pause lifts. No further ConnectByID. No logout.
    back = cursor + timedelta(seconds=1)
    connects = _connects(port)
    port.reply_state = 1
    poller.observe(1, back)
    _gate(host, guard)
    assert machine.reply == LinkPhase.READY
    assert machine.orders_paused is False
    assert host.entries_allowed() is True
    assert guard.decide("auto", "ENTRY_FILL", "SHORT")[0] == guard.SEND_ENTRY
    assert _connects(port) == connects
    assert "logout" not in port.calls
    poller.observe(1, back + timedelta(seconds=60))
    assert alerts == [SILENT_ALERT]


def test_second_drop_alerts_once_per_episode():
    alerts: list[str] = []
    port, _machine, _host, poller = _build(alerts)
    poller.observe(1, T0)
    first = T0 + timedelta(minutes=2)
    poller.observe(0, first)
    assert poller.consider_silent(first + timedelta(seconds=60), force=False) is True
    poller.observe(1, first + timedelta(seconds=90))
    second = first + timedelta(minutes=5)
    poller.observe(0, second)
    assert poller.consider_silent(second + timedelta(seconds=60), force=False) is True
    assert poller.consider_silent(second + timedelta(seconds=61), force=True) is False
    assert len(alerts) == 2


def test_quote_stuck_recovery_does_not_connect_or_lift_reply_pause():
    """#203 resubscribes quotes. It must not ConnectByID or clear a down reply.

    The machine's own stuck degrade only probes IsConnected. The poller
    still owns the single ConnectByID on its backoff step.
    """
    alerts: list[str] = []
    port, machine, host, poller = _build(alerts)
    port.reply_state = 1
    poller.observe(1, T0)
    # Quote is half-up (IsConnected==1) while CONNECTING, long enough to
    # degrade. That path must not call ConnectByID.
    machine.quote = LinkPhase.CONNECTING
    machine._quote_connect_started_at = T0
    machine.tick(T0)
    machine.tick(T0 + timedelta(seconds=30))
    assert machine.degraded is True
    assert machine.degraded_reason == "stuck"
    assert _connects(port) == 0
    assert "logout" not in port.calls

    down_at = T0 + timedelta(seconds=40)
    port.reply_state = 0
    poller.observe(0, down_at)
    assert machine.orders_paused is True
    assert host.entries_allowed() is False
    # Quote-stuck recovery leaves the reply pause in place and connects nothing.
    apply_quote_stuck_recovery(machine)
    machine.tick(down_at + timedelta(seconds=1))
    assert machine.orders_paused is True
    assert _connects(port) == 0
    poller.observe(0, down_at + timedelta(seconds=5))
    assert _connects(port) == 1
    machine.tick(down_at + timedelta(seconds=6))
    apply_quote_stuck_recovery(machine)
    assert _connects(port) == 1
    assert machine.orders_paused is True
    assert "logout" not in port.calls
    # 3003 is not a second connect by itself; the poll on that event
    # still obeys the backoff (this reading is inside the 10s step).
    machine.on_connection(KIND_READY, 0, down_at + timedelta(seconds=6))
    poller.observe(0, down_at + timedelta(seconds=6))
    assert _connects(port) == 1


def test_event_sink_line_names_identity_iid_module_and_pid():
    import run_backtest as rb

    class _Sink:
        _iid = "IID-REPLY"

    handler = object()
    line = format_event_sink("reply", _Sink(), handler, rb, 4242)
    assert "event-sink reply" in line
    assert "iid=IID-REPLY" in line
    assert "sink_id=" in line
    assert f"handler_id={id(handler)}" in line
    assert f"module={rb.__name__}" in line
    assert f"module_id={id(rb)}" in line
    assert "main_id=" in line
    assert f"run_backtest_id={id(rb)}" in line
    assert "pid=4242" in line


def test_startup_lines_land_in_the_debug_log(tmp_path):
    import run_backtest as rb

    rb._close_debug_log()
    rb._boot_log_lines.clear()
    rb._log("登入中 Logging in as ***")
    rb._log("登入成功 LOGIN OK (code=0)")
    rb._log("[CONN] ConnectByID returned 0 OK")
    rb._log_debug("[CONN] OnComplete — reply backfill finished")
    rb._log_debug(
        "[RECONNECT] link machine adopted after process-start login")
    try:
        rb._open_debug_log(str(tmp_path))
        files = list(tmp_path.glob("debug_*.log"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
    finally:
        rb._close_debug_log()
        rb._boot_log_lines.clear()
    for needle in (
        "LOGIN OK",
        "ConnectByID returned",
        "OnComplete",
        "link machine adopted",
    ):
        assert needle in text
