"""Issue #230 — reply-disconnect arity, poller gates, and hang evidence.

Each test in this module fails on v2.19.11 (00e67b4) and passes once
the fix on this branch is in place. COM is not loaded; the handlers
are the Python sink methods.
"""

from __future__ import annotations

import ast
import logging
import os
import queue
import time
from pathlib import Path

import run_backtest as rb
import src.live.main_heartbeat as hb
from src.live.link_state_machine import (
    LinkMachineConfig,
    LinkPhase,
    LinkStateMachine,
)
from src.live.reply_link_poller import (
    POLL_CONNECT_TIMEOUT_S,
    STILL_DOWN_ALERT,
    ReplyLinkPoller,
)
from tests.fakes.fake_skcom import FakeSkcom


def _drain() -> list:
    found = []
    while True:
        try:
            found.append(rb._ui_queue.get_nowait())
        except queue.Empty:
            return found


# Vendor COM arguments (not counting self). Quote and order arities are
# the ones this process has been receiving; the reply disconnect arity
# is the vendor's (bstrUserID, nErrorCode). One extra positional argument
# stands in for a future IDL drift.
_REPLY_CALLS = (
    ("OnReplyMessage", ("U", "hello"), -1),
    ("OnConnect", ("U", 0), None),
    ("OnComplete", ("U",), None),
    ("OnSolaceReplyDisconnect", ("U", 3033), None),
    ("OnSolaceReplyConnection", ("U", 0), None),
    ("OnNewData", ("U", "row"), None),
)

_QUOTE_CALLS = (
    ("OnNotifyStockList", (2, "data")),
    ("OnConnection", (3003, 0)),
    ("OnNotifyKLineData", ("TX00", "bar")),
    ("OnKLineComplete", (0,)),
    ("OnNotifyQuoteLONG", (2, 1)),
    ("OnNotifyHistoryTicksLONG", (2, 1, 0, 20261103, 150000, 0, 1, 2, 3, 4, 0)),
    ("OnNotifyTicksLONG", (2, 1, 0, 20261103, 150000, 0, 1, 2, 3, 4, 0)),
    ("OnNotifyBest5LONG", (2, 1)),
    ("OnNotifyServerTime", (15, 0, 0, 1)),
)

_ORDER_CALLS = (
    ("OnAccount", ("U", "TF,0,,1")),
    ("OnAsyncOrder", (1, 0, "ok")),
    ("OnOpenInterest", ("row",)),
    ("OnFutureRights", ("rights",)),
)


def test_reply_handlers_accept_com_arity():
    """Vendor arity and vendor arity + 1 must not raise inside the sink."""
    rb._app = None
    _drain()
    reply = rb.SKReplyLibEvent()
    for name, args, expected in _REPLY_CALLS:
        method = getattr(reply, name)
        assert method(*args) == expected
        assert method(*args, "drift") == expected
    quote = rb.SKQuoteLibEvents()
    for name, args in _QUOTE_CALLS:
        method = getattr(quote, name)
        method(*args)
        method(*args, "drift")
    order = rb.SKOrderLibEvents()
    for name, args in _ORDER_CALLS:
        method = getattr(order, name)
        method(*args)
        method(*args, "drift")
    queued = _drain()
    assert ("link", ("reply_disconnect",)) in queued
    assert any(
        item[0] == "log" and "code=3033" in str(item[1])
        for item in queued
    )
    assert ("reply_seen", "OnNewData") in queued


def test_poll_reply_link_reschedules_when_poll_raises():
    """A raising poll still re-arms the entry gate and the backoff timer."""
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    gated = []
    scheduled = []

    class _Poller:
        stopped = False

        def poll(self, now):
            raise RuntimeError("poll blew")

        def pending_delay_s(self, now):
            return 5.0

    class _Root:
        def after(self, ms, fn, *args):
            scheduled.append(ms)
            return "tok"

        def after_cancel(self, token):
            return None

    app._reply_poller = _Poller()
    app._reply_backoff_token = None
    app.root = _Root()
    app._apply_link_gate = lambda: gated.append("gate")
    app._poll_reply_link("backoff")
    assert gated == ["gate"]
    assert scheduled == [5000]


def test_connect_by_id_log_never_calls_return_lookup(monkeypatch):
    """A lookup that would sleep or raise is not used for ConnectByID."""
    called = []
    slept = []

    def lookup(code):
        called.append(code)
        time.sleep(30)
        raise RuntimeError("lookup")

    class _Center:
        def SKCenterLib_GetReturnCodeMessage(self, code):
            return lookup(code)

    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr(rb, "skC", _Center())
    logged = []
    monkeypatch.setattr(rb, "_log", logged.append)
    rb._log_connect_by_id_result(0)
    rb._log_connect_by_id_result(151)
    assert called == []
    assert slept == []
    text = "\n".join(logged)
    assert "0 SK_SUCCESS" in text
    assert "returned 151" in text
    assert "SK_SUCCESS" not in text.split("returned 151", 1)[-1]


def test_no_unwrapped_get_return_code_message():
    """No main-thread call of SKCenterLib_GetReturnCodeMessage remains."""
    tree = ast.parse(Path("run_backtest.py").read_text(encoding="utf-8"))
    naked = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "SKCenterLib_GetReturnCodeMessage"
    ]
    assert naked == []


def test_hung_alert_names_mode_position_and_manual_restart(tmp_path):
    alerts = []
    writer = hb.HeartbeatWriter(str(tmp_path))
    writer.link_state = {
        "entries_blocked": True,
        "mode": "REAL",
        "position_open": True,
    }
    writer.write()
    watchdog = hb.HangWatchdog(
        str(tmp_path), hung_s=0.15, poll_s=0.05, alert_fn=alerts.append)
    watchdog.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not alerts:
            time.sleep(0.02)
        assert alerts
        text = alerts[0]
        assert "mode=REAL" in text
        assert "entries_blocked=True" in text
        assert "position_open=True" in text
        assert "process NOT restarted; manual restart: Mira" in text
        assert "Process was NOT killed or restarted" in text
    finally:
        watchdog.disable()


def test_hung_alert_repeats_while_still_hung(tmp_path):
    alerts = []
    writer = hb.HeartbeatWriter(str(tmp_path))
    writer.write()
    watchdog = hb.HangWatchdog(
        str(tmp_path), hung_s=0.12, poll_s=0.04, alert_fn=alerts.append)
    watchdog.realert_s = 0.2
    watchdog.start()
    try:
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline and len(alerts) < 2:
            time.sleep(0.02)
        assert len(alerts) >= 2
        assert all("manual restart: Mira" in item for item in alerts[:2])
    finally:
        watchdog.disable()


def _plant_hang(directory: Path, stamp: str, hang: str, stacks: str) -> None:
    when = time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S"))
    for name, text in (("hang.json", hang), ("hang_stacks.txt", stacks)):
        path = directory / name
        path.write_text(text, encoding="utf-8")
        os.utime(path, (when, when))


def _stop_watchdog() -> None:
    hb.stop()
    if hb._watchdog is not None:
        hb._watchdog.disable()


def test_two_restarts_keep_both_hang_archives(tmp_path):
    _plant_hang(tmp_path, "20261103-120000", "ep1", "stacks1")
    try:
        hb.start(str(tmp_path), hung_s=90.0, poll_s=30.0)
        _plant_hang(tmp_path, "20261103-120002", "ep2", "stacks2")
        hb.start(str(tmp_path), hung_s=90.0, poll_s=30.0)
        assert not (tmp_path / "hang.json").exists()
        assert not (tmp_path / "hang_stacks.txt").exists()
        first = tmp_path / "hang_20261103-120000.json"
        second = tmp_path / "hang_20261103-120002.json"
        assert first.read_text(encoding="utf-8") == "ep1"
        assert second.read_text(encoding="utf-8") == "ep2"
        assert (tmp_path / "hang_stacks_20261103-120000.txt").read_text(
            encoding="utf-8") == "stacks1"
        assert (tmp_path / "hang_stacks_20261103-120002.txt").read_text(
            encoding="utf-8") == "stacks2"
    finally:
        _stop_watchdog()


def test_hang_archives_prune_the_oldest_past_ten(tmp_path):
    for i in range(1, 11):
        (tmp_path / f"hang_20261101-{i:06d}.json").write_text(
            f"old{i}", encoding="utf-8")
    _plant_hang(tmp_path, "20261101-000011", "newest", "stacks")
    try:
        hb.start(str(tmp_path), hung_s=90.0, poll_s=30.0)
        names = sorted(path.name for path in tmp_path.glob("hang_*.json"))
        assert "hang_20261101-000001.json" not in names
        assert "hang_20261101-000011.json" in names
        assert len(names) == hb.HANG_ARCHIVE_KEEP
        assert (tmp_path / "hang_20261101-000011.json").read_text(
            encoding="utf-8") == "newest"
        assert (tmp_path / "hang.json").exists() is False
        assert (tmp_path / "hang_stacks.txt").exists() is False
    finally:
        _stop_watchdog()


def test_hang_archive_rename_failure_leaves_the_file(tmp_path, monkeypatch, caplog):
    (tmp_path / "hang.json").write_text("keep-me", encoding="utf-8")
    (tmp_path / "hang_stacks.txt").write_text("keep-stacks", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(hb.os, "replace", boom)
    try:
        with caplog.at_level(logging.ERROR, logger="src.live.main_heartbeat"):
            hb.start(str(tmp_path), hung_s=90.0, poll_s=30.0)
        assert (tmp_path / "hang.json").read_text(encoding="utf-8") == "keep-me"
        assert (tmp_path / "hang_stacks.txt").read_text(encoding="utf-8") == "keep-stacks"
        assert any("archive failed" in rec.message for rec in caplog.records)
    finally:
        _stop_watchdog()


def test_start_with_no_hang_file_does_not_create_an_archive(tmp_path):
    try:
        hb.start(str(tmp_path), hung_s=90.0, poll_s=30.0)
        assert list(tmp_path.glob("hang_*.json")) == []
        assert list(tmp_path.glob("hang_stacks_*.txt")) == []
        assert not (tmp_path / "hang.json").exists()
        assert not (tmp_path / "hang_stacks.txt").exists()
    finally:
        _stop_watchdog()


_T0 = __import__("datetime").datetime(2026, 11, 3, 15, 31, 6)


def _poll_world(alerts: list[str]):
    fake = FakeSkcom(reply_settle_s=10)
    machine = LinkStateMachine(
        fake, LinkMachineConfig(user_id="U", reply_settle_s=10, stuck_limit_s=300))
    machine.adopt_after_process_login(_T0)
    machine.quote = LinkPhase.READY
    poller = ReplyLinkPoller(
        lambda: machine,
        read=lambda: 0,
        connect=lambda user: fake.connect_reply(user),
        notify=alerts.append,
        log_error=lambda _msg: None,
        log_info=lambda _msg: None,
        reschedule_silent=lambda: None,
    )
    poller.note_login(_T0)
    return fake, machine, poller


def test_poller_connect_success_without_followup_event():
    """One ConnectByID, then no follow-up event: return, reschedule, no second call."""
    from datetime import timedelta

    alerts: list[str] = []
    scheduled: list[int] = []

    class _Root:
        def after(self, ms, fn, *args):
            scheduled.append(int(ms))
            return "tok"

        def after_cancel(self, token):
            return None

    # The poll returns. The next poll is on the after chain.
    lone = ReplyLinkPoller(
        lambda: None,
        read=lambda: 0,
        connect=lambda _user: None,
        notify=alerts.append,
        log_error=lambda _msg: None,
        log_info=lambda _msg: None,
        reschedule_silent=lambda: None,
    )
    lone.note_login(_T0)
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._reply_poller = lone
    app._reply_backoff_token = None
    app.root = _Root()
    app._link_host = None
    app._trading_guard = None
    app._poll_reply_link("60s")
    assert scheduled, "next poll was not scheduled"

    fake, machine, poller = _poll_world(alerts)
    fake.set_now(_T0)
    poller.observe(1, _T0)
    down = _T0 + timedelta(seconds=1)
    fake.set_now(down)
    poller.observe(0, down)
    poller.observe(0, down + timedelta(seconds=5))
    assert fake.calls.count("ConnectByID") == 0
    connected_at = down + timedelta(seconds=15)
    fake.set_now(connected_at)
    poller.observe(0, connected_at)
    assert fake.calls.count("ConnectByID") == 1
    later = connected_at + timedelta(seconds=30)
    fake.set_now(later)
    poller.observe(0, later)
    assert fake.calls.count("ConnectByID") == 1
    timed = connected_at + timedelta(seconds=POLL_CONNECT_TIMEOUT_S)
    fake.set_now(timed)
    poller.observe(0, timed)
    assert fake.calls.count("ConnectByID") == 1
    assert machine.degraded is True
    assert STILL_DOWN_ALERT in alerts
    assert "SimulatedAV" not in " ".join(alerts)


def test_poller_connect_refused_while_rebuild_inflight():
    alerts: list[str] = []
    fake, machine, poller = _poll_world(alerts)
    assert machine._reply_awaiting_complete is True
    fake.set_now(_T0)
    poller.observe(0, _T0)
    poller.observe(0, _T0 + __import__("datetime").timedelta(seconds=15))
    poller.observe(0, _T0 + __import__("datetime").timedelta(seconds=45))
    assert fake.calls.count("ConnectByID") == 0


def test_poller_connect_waits_settle_after_poll0():
    alerts: list[str] = []
    fake, _machine, poller = _poll_world(alerts)
    poller.observe(1, _T0)
    down = _T0 + __import__("datetime").timedelta(seconds=2)
    fake.set_now(down)
    poller.observe(0, down)
    poller.observe(0, down + __import__("datetime").timedelta(seconds=5))
    assert fake.calls.count("ConnectByID") == 0


def test_recent_two_blocks_poller_connect():
    alerts: list[str] = []
    fake, _machine, poller = _poll_world(alerts)
    poller.observe(1, _T0)
    down = _T0 + __import__("datetime").timedelta(minutes=1)
    poller.observe(0, down)
    poller.observe(2, down + __import__("datetime").timedelta(seconds=3))
    poller.observe(0, down + __import__("datetime").timedelta(seconds=5))
    assert fake.calls.count("ConnectByID") == 0


def test_too_few_or_too_short_zero_run_blocks_connect():
    alerts: list[str] = []
    fake, _machine, poller = _poll_world(alerts)
    poller.observe(1, _T0)
    down = _T0 + __import__("datetime").timedelta(minutes=1)
    # Two 0s spanning the settle are still too few.
    poller.observe(0, down)
    poller.observe(0, down + __import__("datetime").timedelta(seconds=15))
    assert fake.calls.count("ConnectByID") == 0
    # Three 0s inside the settle are too short a span.
    fresh = down + __import__("datetime").timedelta(minutes=5)
    poller.observe(1, fresh)
    poller.observe(0, fresh)
    poller.observe(0, fresh + __import__("datetime").timedelta(seconds=4))
    poller.observe(0, fresh + __import__("datetime").timedelta(seconds=6))
    assert fake.calls.count("ConnectByID") == 0


def test_connect_by_id_call_sites_are_login_and_the_machine_port():
    """SKReplyLib_ConnectByID is only the login call and the machine port."""
    app_lines = []
    for lineno, line in enumerate(
            Path("run_backtest.py").read_text(encoding="utf-8").splitlines(), 1):
        if "SKReplyLib_ConnectByID" in line and not line.strip().startswith("#"):
            app_lines.append((lineno, line.strip()))
    assert len(app_lines) == 1
    src_hits = []
    for path in Path("src").rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "SKReplyLib_ConnectByID" not in line:
                continue
            if line.strip().startswith("#"):
                continue
            src_hits.append((path.as_posix(), lineno, line.strip()))
    port_hits = [hit for hit in src_hits if hit[0].endswith("com_broker_port.py")]
    assert len(port_hits) == 1
    other = [hit for hit in src_hits if not hit[0].endswith("com_broker_port.py")]
    assert other == [] or all(
        hit[0].endswith("broker_port.py") for hit in other
    )
