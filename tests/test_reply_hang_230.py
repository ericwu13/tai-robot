"""Issue #230 — reply-disconnect arity, poller gates, and hang evidence.

Each test in this module fails on v2.19.11 (00e67b4) and passes once
the fix on this branch is in place. COM is not loaded; the handlers
are the Python sink methods.
"""

from __future__ import annotations

import ast
import json
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
from src.live.reply_link_poller import STILL_DOWN_ALERT, ReplyLinkPoller
import src.live.reply_link_poller as reply_poller
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


def test_backoff_callback_reschedules_on_exception():
    """_on_reply_backoff re-arms the gate and the timer when its body raises."""
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    gated = []
    scheduled = []

    class _Poller:
        stopped = False

        def pending_delay_s(self, now):
            return 5.0

    class _Root:
        def after(self, ms, fn, *args):
            scheduled.append(ms)
            return "tok"

        def after_cancel(self, token):
            return None

    app._reply_poller = _Poller()
    app._reply_backoff_token = "old"
    app.root = _Root()
    app._apply_link_gate = lambda: gated.append("gate")

    def _boom(cause):
        raise RuntimeError(f"poll {cause}")

    app._poll_reply_link = _boom
    try:
        app._on_reply_backoff()
    except RuntimeError:
        pass
    else:
        raise AssertionError("backoff body did not raise")
    assert gated == ["gate"]
    assert scheduled == [5000]
    assert app._reply_backoff_token == "tok"


def test_main_heartbeat_reschedules_when_body_raises():
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    scheduled = []

    class _Root:
        def after(self, ms, fn, *args):
            scheduled.append(ms)
            return "hb"

    app.root = _Root()
    app._heartbeat_active = True
    app._heartbeat_after_id = "old"

    def _boom():
        raise RuntimeError("publish")

    app._publish_link_heartbeat = _boom
    try:
        app._on_main_heartbeat()
    except RuntimeError:
        pass
    else:
        raise AssertionError("heartbeat body did not raise")
    assert scheduled == [hb.HEARTBEAT_INTERVAL_MS]
    assert app._heartbeat_after_id == "hb"


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
        assert "Process was NOT killed or restarted; manual restart: Mira" in text
        assert text.count("restarted") == 1
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
        assert all(
            "Process was NOT killed or restarted; manual restart: Mira" in item
            for item in alerts[:2]
        )
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
        assert len(names) == getattr(hb, "HANG_ARCHIVE_KEEP", 10)
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
    timed = connected_at + timedelta(
        seconds=getattr(reply_poller, "POLL_CONNECT_TIMEOUT_S", 90.0))
    fake.set_now(timed)
    poller.observe(0, timed)
    # The timeout tick releases the in-flight call and does not send
    # another on the same tick, and does not degrade yet.
    assert fake.calls.count("ConnectByID") == 1
    assert machine.degraded is False
    assert machine._reply_awaiting_complete is False
    assert STILL_DOWN_ALERT not in alerts
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


def test_poller_connect_by_id_stamps_inflight_around_the_call(tmp_path):
    """ConnectByID is the inflight name while the call runs, and null after."""
    writer = hb.HeartbeatWriter(str(tmp_path))
    hb._writer = writer
    seen = {}

    def connect(user):
        payload = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
        seen["during"] = (payload.get("inflight_com_call") or {}).get("name")
        seen["user"] = user
        return 0

    try:
        assert hb.call_com("ConnectByID", connect, "U") == 0
    finally:
        hb._writer = None
    after = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert seen == {"during": "ConnectByID", "user": "U"}
    assert after["inflight_com_call"] is None


def test_realert_interval_is_a_start_argument(tmp_path):
    try:
        hb.start(str(tmp_path), hung_s=90.0, poll_s=30.0, realert_s=12.0)
        assert hb._watchdog is not None
        assert hb._watchdog.realert_s == 12.0
    finally:
        _stop_watchdog()


def test_stamp_depth_stays_on_the_calling_thread():
    import threading

    seen = {}

    def worker():
        def body():
            seen["during"] = hb._stamp_depth_value()
        hb.call_com("Background", body)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen["during"] == 1
    assert hb._stamp_depth_value() == 0


def test_wake_gap_rebase_logs_the_gap(tmp_path, caplog):
    writer = hb.HeartbeatWriter(str(tmp_path))
    writer.write()
    watchdog = hb.HangWatchdog(
        str(tmp_path), hung_s=90.0, poll_s=0.05, alert_fn=lambda _msg: None)
    watchdog._last_loop_mono = time.monotonic() - 5.0
    with caplog.at_level(logging.WARNING, logger="src.live.main_heartbeat"):
        watchdog._poll()
    matched = [rec.message for rec in caplog.records if "wake-gap rebase" in rec.message]
    assert matched
    assert "gap=" in matched[0]
    assert "5." in matched[0] or "4." in matched[0]


def test_stamp_write_failure_is_logged_every_time(tmp_path, monkeypatch):
    logged = []
    hb.reset_replace_failure_log()
    hb.set_replace_failure_logger(logged.append)
    writer = hb.HeartbeatWriter(str(tmp_path))
    hb._writer = writer

    def deny(src, dst):
        raise PermissionError("busy")

    monkeypatch.setattr(hb.os, "replace", deny)
    try:
        hb.call_com("ConnectByID", lambda: 0)
        hb.call_com("ConnectByID", lambda: 0)
    finally:
        hb._writer = None
        hb.set_replace_failure_logger(None)
        hb.reset_replace_failure_log()
    assert len(logged) >= 4
    assert all("ConnectByID" in line or "os.replace" in line for line in logged)


def test_no_account_query_between_down_poll_and_ready(monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    queries: list[str] = []

    class _Port:
        def connect_reply(self, user_id):
            return 0

        def __getattr__(self, name):
            return lambda *args, **kwargs: 0

    machine = LinkStateMachine(
        _Port(), LinkMachineConfig(user_id="U", reply_settle_s=10))
    machine.adopt_after_process_login(_T0)
    machine.quote = LinkPhase.READY
    poller = ReplyLinkPoller(
        lambda: machine,
        read=lambda: 0,
        connect=lambda _user: 0,
        notify=lambda _msg: None,
        log_error=lambda _msg: None,
        log_info=lambda _msg: None,
        reschedule_silent=lambda: None,
    )
    poller.note_login(_T0)
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._reply_poller = poller
    app._link_host = SimpleNamespace(machine=machine)
    app._logged_in = True
    app._futures_account = "ACCT"
    app.login_user_var = SimpleNamespace(get=lambda: "U")
    app._account_monitor = SimpleNamespace(begin_oi_query=lambda: None)
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(
        GetOpenInterestGW=lambda *args: 0,
        GetFutureRights=lambda *args: 0,
    ))
    monkeypatch.setattr(
        rb, "call_com", lambda name, fn, *args, **kwargs: queries.append(name) or 0)

    poller.observe(1, _T0)
    app._query_real_account()
    assert queries == ["GetOpenInterestGW", "GetFutureRights"]
    queries.clear()
    poller.observe(0, _T0 + timedelta(seconds=70))
    app._query_real_account()
    assert queries == []
    poller.observe(1, _T0 + timedelta(seconds=80))
    app._query_real_account()
    assert queries == ["GetOpenInterestGW", "GetFutureRights"]


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


def test_quote_connecting_refuses_poller_connect():
    from datetime import timedelta

    fake, machine, poller = _poll_world([])
    fake.set_now(_T0)
    poller.observe(1, _T0)
    machine.quote = LinkPhase.CONNECTING
    down = _T0 + timedelta(minutes=1)
    fake.set_now(down)
    poller.observe(0, down)
    poller.observe(0, down + timedelta(seconds=5))
    poller.observe(0, down + timedelta(seconds=15))
    assert fake.calls.count("ConnectByID") == 0


def test_unknown_return_code_lookup_is_stamped(tmp_path, monkeypatch):
    writer = hb.HeartbeatWriter(str(tmp_path))
    hb._writer = writer
    seen = {}

    def lookup(code):
        payload = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
        seen["name"] = (payload.get("inflight_com_call") or {}).get("name")
        return "vendor text"

    class _Center:
        def SKCenterLib_GetReturnCodeMessage(self, code):
            return lookup(code)

    monkeypatch.setattr(rb, "skC", _Center())
    try:
        text = rb._return_code_text(151)
    finally:
        hb._writer = None
    assert seen["name"] == "GetReturnCodeMessage"
    assert text.startswith("151")


def test_poll_connect_timeout_releases_without_a_second_call_or_ready():
    """90s with no ready reading releases the one in-flight call.

    It does not send another ConnectByID on that tick and does not
    degrade. There is still no poll of 1.
    """
    from datetime import timedelta

    alerts: list[str] = []
    fake, machine, poller = _poll_world(alerts)
    fake.set_now(_T0)
    poller.observe(1, _T0)
    down = _T0 + timedelta(seconds=1)
    fake.set_now(down)
    poller.observe(0, down)
    poller.observe(0, down + timedelta(seconds=5))
    connected = down + timedelta(seconds=15)
    fake.set_now(connected)
    poller.observe(0, connected)
    assert fake.calls.count("ConnectByID") == 1
    assert machine._reply_awaiting_complete is True
    timed = connected + timedelta(seconds=getattr(reply_poller, "POLL_CONNECT_TIMEOUT_S", 90.0))
    fake.set_now(timed)
    poller.observe(0, timed)
    assert fake.calls.count("ConnectByID") == 1
    assert machine._reply_awaiting_complete is False
    assert machine.degraded is False
    assert STILL_DOWN_ALERT not in alerts
    assert machine.reply != LinkPhase.READY


def test_nonzero_or_raised_connect_releases_inflight_immediately():
    """A failed ConnectByID does not stay in flight for 90s."""
    from datetime import timedelta

    from src.live.reply_link_poller import ReplyLinkPoller

    def _run(connect):
        machine = LinkStateMachine(
            connect, LinkMachineConfig(user_id="U", reply_settle_s=10))
        # The port is the connect callable's machine via a tiny port.
        port_calls = []

        class _Port:
            def connect_reply(self, user_id):
                port_calls.append(user_id)
                return connect(user_id)

        machine.port = _Port()
        machine.adopt_after_process_login(_T0)
        machine.quote = LinkPhase.READY
        machine._reply_awaiting_complete = False
        machine._reply_connect_started_at = None
        machine.reply = LinkPhase.READY
        poller = ReplyLinkPoller(
            lambda: machine,
            read=lambda: 0,
            connect=lambda user: machine.port.connect_reply(user),
            notify=lambda _msg: None,
            log_error=lambda _msg: None,
            log_info=lambda _msg: None,
            reschedule_silent=lambda: None,
        )
        poller.note_login(_T0)
        down = _T0 + timedelta(minutes=1)
        poller.observe(0, down)
        poller.observe(0, down + timedelta(seconds=5))
        poller.observe(0, down + timedelta(seconds=15))
        return machine, port_calls, poller

    machine, calls, _poller = _run(lambda _user: 5)
    assert calls == ["U"]
    assert machine._reply_awaiting_complete is False

    raised = []

    def boom(_user):
        raised.append(1)
        raise RuntimeError("connect blew")

    machine2, calls2, _poller2 = _run(boom)
    assert calls2 == ["U"]
    assert raised == [1]
    assert machine2._reply_awaiting_complete is False
    assert machine2.degraded is False


def test_account_hold_released_by_poll_of_1_and_by_on_complete(monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    def _app_for(poller, machine):
        app = rb.BacktestApp.__new__(rb.BacktestApp)
        app._reply_poller = poller
        app._link_host = SimpleNamespace(machine=machine)
        app._logged_in = True
        app._futures_account = "ACCT"
        app.login_user_var = SimpleNamespace(get=lambda: "U")
        app._account_monitor = SimpleNamespace(begin_oi_query=lambda: None)
        return app

    queries: list[str] = []
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(
        GetOpenInterestGW=lambda *a: 0,
        GetFutureRights=lambda *a: 0,
    ))
    monkeypatch.setattr(
        rb, "call_com",
        lambda name, fn, *args, **kwargs: queries.append(name) or 0)

    fake, machine, poller = _poll_world([])
    down = _T0 + timedelta(seconds=20)
    fake.set_now(down)
    poller.observe(0, down)
    app = _app_for(poller, machine)
    assert app._account_query_blocked() is True
    app._query_real_account()
    assert queries == []
    poller.observe(1, down + timedelta(seconds=5))
    assert app._account_query_blocked() is False
    app._query_real_account()
    assert queries == ["GetOpenInterestGW", "GetFutureRights"]

    queries.clear()
    fake2, machine2, poller2 = _poll_world([])
    down2 = _T0 + timedelta(seconds=20)
    fake2.set_now(down2)
    poller2.observe(0, down2)
    app2 = _app_for(poller2, machine2)
    assert app2._account_query_blocked() is True
    app2._query_real_account()
    assert queries == []
    done = down2 + timedelta(seconds=30)
    fake2.set_now(done)
    fake2.note_on_complete()
    machine2.on_complete(done)
    poller2.note_reply_event("OnComplete")
    machine2.tick(done)
    assert app2._account_query_blocked() is False
    app2._query_real_account()
    assert queries == ["GetOpenInterestGW", "GetFutureRights"]


def test_entry_gate_waits_for_equity_refresh_after_release(monkeypatch):
    """Release, then refresh equity, then the entry gate may pass."""
    from types import SimpleNamespace

    from src.live.link_host import LinkHost
    from src.live.trading_guard import TradingGuard

    order: list[str] = []
    fake, machine, poller = _poll_world([])
    host = LinkHost(machine)
    guard = TradingGuard(daily_loss_limit=10000)
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._link_host = host
    app._trading_guard = guard
    app._reply_poller = poller
    app._trading_mode = "auto"
    app._logged_in = True
    app._futures_account = "ACCT"
    app.login_user_var = SimpleNamespace(get=lambda: "U")
    app._equity_refresh_owed = False
    app._equity_refresh_sent = False
    app.set_status = lambda *args, **kwargs: None
    app._set_conn_dot = lambda *args, **kwargs: None

    def call_com(name, fn, *args, **kwargs):
        order.append("refresh")
        assert guard.link_entries_blocked is True
        app._equity_refresh_owed = False
        guard.update_pnl(0)
        return 0

    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(GetFutureRights=lambda *a: 0))
    monkeypatch.setattr(rb, "call_com", call_com)

    down = _T0 + __import__("datetime").timedelta(seconds=30)
    poller.observe(0, down)
    app._apply_link_gate()
    assert guard.link_entries_blocked is True
    assert app._equity_refresh_owed is True

    order.append("release")
    poller.observe(1, down + __import__("datetime").timedelta(seconds=5))
    app._apply_link_gate()
    order.append("entry-gate")
    assert order == ["release", "refresh", "entry-gate"]
    assert guard.link_entries_blocked is False
    assert guard.decide("auto", "ENTRY_FILL", "LONG")[0] == guard.SEND_ENTRY


def test_auto_switch_refused_while_reply_down(tmp_path, monkeypatch):
    """A switch to REAL does not query the account while the reply link is down."""
    from datetime import datetime
    from types import SimpleNamespace

    import src.live.mode_request as mreq
    from src.live.fill_poller import FillPoller
    from src.live.trading_guard import TradingGuard

    now = datetime(2026, 11, 3, 15, 31, tzinfo=rb._TZ_TAIPEI)
    body = {
        "request_id": "req-down",
        "mode": "auto",
        "loss_limit": 3000,
        "requested_by": "tester",
        "requested_at": now.isoformat(timespec="seconds"),
    }
    (tmp_path / "mode_request.json").write_text(
        json.dumps(body), encoding="utf-8")
    queries: list[str] = []
    guard = TradingGuard()
    guard.reset()
    runner = SimpleNamespace(
        bot_dir=str(tmp_path),
        trading_mode="paper",
        _dashboard_auto_opt_in=True,
        _allow_live_override=True,
        _mode_switch_loss_cap=20_000,
        _is_reloading=False,
        suppress_strategy=False,
        broker=SimpleNamespace(position_size=0),
        csv_logger=None,
    )
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._live_runner = runner
    app._trading_guard = guard
    app._fill_poller = FillPoller(guard)
    app._trading_mode = "paper"
    app._futures_account = "ACCT"
    app._logged_in = True
    app.login_user_var = SimpleNamespace(get=lambda: "U")
    app._reply_poller = SimpleNamespace(down=True)
    app._link_host = None
    app._account_monitor = SimpleNamespace(
        oi_query_in_flight=lambda _timeout: False)
    app._mode_request_clock = lambda: now
    app._live_log_msg = lambda *args, **kwargs: None
    app._log_order_decision = lambda *args, **kwargs: None
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(
        GetOpenInterestGW=lambda *a: 0,
        GetFutureRights=lambda *a: 0,
    ))
    monkeypatch.setattr(
        rb, "call_com",
        lambda name, fn, *args, **kwargs: queries.append(name) or 0)
    monkeypatch.setattr(rb, "_discord", None)
    app._service_mode_request(str(tmp_path))
    assert queries == []
    result = json.loads(
        (tmp_path / "mode_request_result.json").read_text(encoding="utf-8"))
    assert result["status"] == "refused"
    assert "連線" in result["reason"]
    assert app._trading_mode == "paper"
    assert not (tmp_path / mreq.REQUEST_FILENAME).exists()


def test_exit_fill_during_reply_down_confirms_without_downgrade(monkeypatch):
    """An exit fill still queries the position while the reply link is down."""
    from types import SimpleNamespace

    from src.live.fill_poller import FillPoller
    from src.live.trading_guard import TradingGuard

    queries: list[str] = []
    alerts: list[str] = []
    guard = TradingGuard()
    guard.on_fill_pending("exit")
    fill = FillPoller(guard, timeout=10.0)
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._trading_guard = guard
    app._fill_poller = fill
    app._trading_mode = "auto"
    app._live_runner = SimpleNamespace(
        trading_mode="auto",
        broker=SimpleNamespace(trade_source="real"),
        symbol="TX00",
    )
    app._fill_poll_timer_id = None
    app._futures_account = "ACCT"
    app._logged_in = True
    app.login_user_var = SimpleNamespace(get=lambda: "U")
    app.trading_mode_var = SimpleNamespace(set=lambda _value: None)
    app._reply_poller = SimpleNamespace(down=True)
    app._link_host = None
    app.root = SimpleNamespace(
        after=lambda _ms, _fn, *args: "tok",
        after_cancel=lambda _token: None,
    )
    assert app._account_query_blocked() is True
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(GetOpenInterestGW=lambda *a: 0))
    monkeypatch.setattr(
        rb, "call_com",
        lambda name, fn, *args, **kwargs: queries.append(name) or 0)
    monkeypatch.setattr(rb, "_discord", SimpleNamespace(
        enabled=True,
        fill_timeout_downgrade=lambda *args: alerts.append("downgrade"),
    ))

    started = fill.start("exit", current_position=-1)
    assert started.type == "start_polling"
    app._poll_fill_confirmation()
    assert queries == ["GetOpenInterestGW"]
    confirmed = fill.on_position_update(0)
    assert confirmed is not None and confirmed.type == "confirmed"
    assert app._trading_mode == "auto"
    assert alerts == []
    assert guard.halted is False
