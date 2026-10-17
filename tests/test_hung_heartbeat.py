"""Hung-but-alive detection (issue #157 phase 2).

A blocked broker COM call freezes the Tk main thread, which is also the
message pump. The process stays alive. These tests pin:

- heartbeat.json is written only on the main thread, atomically
- a LeaveMonitor that never returns stops the heartbeat, and within
  HUNG_S+5s the watchdog writes hang.json (inflight_com_call=LeaveMonitor),
  dumps stacks, and sends exactly one P1 — without exiting the process
- the watchdog re-arms only after the heartbeat recovers
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

import pytest

import src.live.main_heartbeat as hb
from src.live.main_heartbeat import (
    HeartbeatWriter, HangWatchdog, MainThreadHeartbeatError,
)


def test_background_thread_cannot_write_heartbeat(tmp_path):
    writer = HeartbeatWriter(str(tmp_path))
    box = {}

    def run():
        try:
            writer.write()
        except MainThreadHeartbeatError as exc:
            box["err"] = exc

    thread = threading.Thread(target=run, name="not-main")
    thread.start()
    thread.join()
    assert "err" in box
    assert not (tmp_path / "heartbeat.json").exists()


def test_heartbeat_write_is_atomic_replace(tmp_path, monkeypatch):
    calls = []
    real_replace = hb.os.replace

    def wrapped(src, dst):
        calls.append(os.path.basename(dst))
        # The temp file must still be there when replace runs.
        assert os.path.exists(src)
        return real_replace(src, dst)

    monkeypatch.setattr(hb.os, "replace", wrapped)
    writer = HeartbeatWriter(str(tmp_path))
    writer.write()
    assert calls == ["heartbeat.json"]
    data = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data["inflight_com_call"] is None
    assert list(tmp_path.glob(".hb-*")) == []


def test_blocking_leave_monitor_detected_once_without_exit(tmp_path):
    """A fake LeaveMonitor that blocks forever stops the heartbeat.

    Within HUNG_S+5s, hang.json names LeaveMonitor, stacks are dumped,
    exactly one alert fires, and the process is still alive.

    The waiter does not open heartbeat.json until the main thread is
    blocked inside the COM call. Opening it during os.replace is a
    POSIX replace-while-open race: on Windows the replace raises
    PermissionError for as long as the reader holds the file. The
    watchdog reads the in-memory snapshot, so it is not that reader.
    """
    hung_s = 0.4
    alerts = []
    writer = HeartbeatWriter(str(tmp_path))
    # Publish the file and the snapshot before the watchdog starts, so
    # its first poll does not open the file while a later replace runs.
    writer.write()
    writer.write()
    watchdog = HangWatchdog(
        str(tmp_path), hung_s=hung_s, inflight_hung_s=hung_s,
        poll_s=0.05, alert_fn=alerts.append)
    watchdog.start()
    hb._writer = writer
    pid = os.getpid()
    release = threading.Event()
    entered = threading.Event()
    seen = {}

    def waiter():
        # File reads start only after begin_inflight has returned and
        # the main thread is inside the blocked call. Nobody replaces
        # heartbeat.json in that window.
        if not entered.wait(timeout=hung_s + 5):
            return
        try:
            data = hb.read_heartbeat(writer.path)
            inflight = (data or {}).get("inflight_com_call") or {}
            ts1 = (data or {}).get("ts")
            seen["saw_inflight"] = (
                isinstance(inflight, dict)
                and inflight.get("name") == "LeaveMonitor")
            time.sleep(0.12)
            again = hb.read_heartbeat(writer.path)
            seen["frozen"] = (
                seen["saw_inflight"] and again is not None
                and again.get("ts") == ts1)
            deadline = time.monotonic() + hung_s + 5
            while time.monotonic() < deadline:
                hang_path = tmp_path / hb.HANG_FILENAME
                stacks_path = tmp_path / hb.STACKS_FILENAME
                if hang_path.is_file() and stacks_path.is_file() and alerts:
                    time.sleep(0.3)  # several watchdog polls while still hung
                    seen["alerts_while_hung"] = len(alerts)
                    return
                time.sleep(0.02)
        finally:
            release.set()

    threading.Thread(target=waiter, name="hang-waiter", daemon=True).start()
    try:
        def fake_leave_monitor():
            entered.set()
            release.wait(timeout=hung_s + 5)

        hb.call_com("LeaveMonitor", fake_leave_monitor)
    finally:
        hb._writer = None
        watchdog.disable()

    assert seen.get("frozen") is True, "heartbeat kept moving during LeaveMonitor"
    hang = json.loads((tmp_path / hb.HANG_FILENAME).read_text(encoding="utf-8"))
    assert hang["inflight_com_call"]["name"] == "LeaveMonitor"
    stacks = (tmp_path / hb.STACKS_FILENAME).read_text(encoding="utf-8")
    assert stacks.strip()
    assert "fake_leave_monitor" in stacks
    assert seen.get("alerts_while_hung") == 1
    assert len(alerts) == 1
    assert "P1" in alerts[0]
    assert "LeaveMonitor" in alerts[0]
    assert os.getpid() == pid
    assert watchdog._thread is not None and watchdog._thread.is_alive()


def test_watchdog_rearms_only_after_recovery(tmp_path):
    alerts = []
    writer = HeartbeatWriter(str(tmp_path))
    watchdog = HangWatchdog(
        str(tmp_path), hung_s=0.3, inflight_hung_s=60.0,
        poll_s=0.05, alert_fn=alerts.append)
    watchdog.start()
    try:
        writer.write(now=time.time() - 5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(alerts) < 1:
            time.sleep(0.02)
        assert len(alerts) == 1
        time.sleep(0.25)
        assert len(alerts) == 1, "second alert fired before recovery"
        writer.write()  # fresh heartbeat — recovery
        time.sleep(0.2)
        assert len(alerts) == 1
        writer.write(now=time.time() - 5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(alerts) < 2:
            time.sleep(0.02)
        assert len(alerts) == 2
        assert os.getpid() > 0
        assert watchdog._thread.is_alive()
    finally:
        watchdog.disable()


class _BrokerBoom(Exception):
    """Stand-in for a COM/broker failure. Identity must survive a bad stamp."""


def _failing_replace(monkeypatch, fail_on: int):
    real = hb.os.replace
    seen = {"n": 0}

    def wrapped(src, dst):
        seen["n"] += 1
        if seen["n"] == fail_on:
            raise PermissionError(f"replace {fail_on}")
        return real(src, dst)

    monkeypatch.setattr(hb.os, "replace", wrapped)
    return seen


@pytest.mark.parametrize("fail_on", [1, 2])
def test_call_com_stamp_failure_returns_fn_result(tmp_path, monkeypatch, fail_on):
    """(a) start stamp is replace #1, (b) end stamp is replace #2."""
    writer = HeartbeatWriter(str(tmp_path))
    hb._writer = writer
    _failing_replace(monkeypatch, fail_on)
    calls = []
    token = object()

    def fn():
        calls.append(1)
        return token

    try:
        assert hb.call_com("SendFutureOrderCLR", fn) is token
    finally:
        hb._writer = None
    assert calls == [1]


@pytest.mark.parametrize("fail_on", [1, 2])
def test_call_com_stamp_failure_reraises_fn_exception(tmp_path, monkeypatch, fail_on):
    writer = HeartbeatWriter(str(tmp_path))
    hb._writer = writer
    _failing_replace(monkeypatch, fail_on)
    calls = []
    boom = _BrokerBoom("order rejected")

    def fn():
        calls.append(1)
        raise boom

    try:
        with pytest.raises(_BrokerBoom) as caught:
            hb.call_com("SendFutureOrderCLR", fn)
    finally:
        hb._writer = None
    assert caught.value is boom
    assert calls == [1]


def test_default_thresholds_and_strict_boundaries(tmp_path):
    """Pins 90s / 60s and ``>`` (age == threshold is not hung)."""
    assert hb.DEFAULT_HUNG_S == 90.0
    assert hb.DEFAULT_INFLIGHT_HUNG_S == 60.0
    alerts = []
    writer = HeartbeatWriter(str(tmp_path))
    writer.write()
    watchdog = HangWatchdog(str(tmp_path), alert_fn=alerts.append)
    assert watchdog.hung_s == 90.0
    assert watchdog.inflight_hung_s == 60.0
    data = hb.read_heartbeat(writer.path)
    watchdog._seen_ts = data["ts"]
    watchdog._seen_mono = 0.0
    watchdog._check_once(90.0)
    assert alerts == []
    watchdog._check_once(90.01)
    assert len(alerts) == 1

    alerts.clear()
    inflight_dir = tmp_path / "inflight"
    inflight_dir.mkdir()
    writer = HeartbeatWriter(str(inflight_dir))
    writer._inflight = {"name": "LeaveMonitor", "start_ts": 1.0}
    writer.write()
    watchdog = HangWatchdog(str(inflight_dir), alert_fn=alerts.append)
    watchdog._check_once(0.0)
    watchdog._check_once(60.0)
    assert alerts == []
    watchdog._check_once(60.01)
    assert len(alerts) == 1
    assert "LeaveMonitor" in alerts[0]


def test_wall_jump_and_sleep_do_not_false_alarm_backward_jump_still_hung(tmp_path, monkeypatch):
    alerts = []
    writer = HeartbeatWriter(str(tmp_path))
    writer.write()
    watchdog = HangWatchdog(
        str(tmp_path), hung_s=90.0, inflight_hung_s=60.0,
        poll_s=2.0, alert_fn=alerts.append)
    watchdog._check_once()
    base = time.time()
    monkeypatch.setattr(hb.time, "time", lambda: base + 600.0)
    watchdog._check_once()
    assert alerts == [], "wall-clock jump must not raise a P1"

    watchdog._seen_mono = time.monotonic() - 91.0
    monkeypatch.setattr(hb.time, "time", lambda: 1.0)
    watchdog._check_once()
    assert len(alerts) == 1, "backward wall jump must not hide a real stall"

    alerts.clear()
    sleep_dir = tmp_path / "sleep"
    sleep_dir.mkdir()
    writer = HeartbeatWriter(str(sleep_dir))
    writer.write()
    watchdog = HangWatchdog(
        str(sleep_dir), hung_s=90.0, poll_s=2.0, alert_fn=alerts.append)
    # Unlatched. A resume whose wake gap is ~600s must rebase, not P1.
    assert watchdog._latched is False
    watchdog._poll()
    watchdog._seen_mono = time.monotonic() - 600.0
    watchdog._last_loop_mono = time.monotonic() - 600.0
    watchdog._poll()
    assert alerts == [], "resume from sleep must not false-P1"
    assert watchdog._latched is False
    # An episode that was already latched stays latched across that wake.
    watchdog._latched = True
    watchdog._latched_ts = hb.read_heartbeat(writer.path)["ts"]
    watchdog._seen_mono = time.monotonic() - 600.0
    watchdog._last_loop_mono = time.monotonic() - 600.0
    watchdog._poll()
    assert alerts == []
    assert watchdog._latched is True
    watchdog._poll()
    assert alerts == []
    assert watchdog._latched is True, "wake rebase must not re-arm the latch"
    # A modest wake (2× poll) is not a sleep. A real stall still alerts.
    watchdog._latched = False
    watchdog._seen_mono = time.monotonic() - 91.0
    watchdog._last_loop_mono = time.monotonic() - (watchdog.poll_s * 2)
    watchdog._poll()
    assert len(alerts) == 1


def test_alert_goes_out_when_dump_fails_and_missing_file_does_not_rearm(tmp_path, monkeypatch):
    alerts = []
    writer = HeartbeatWriter(str(tmp_path))
    writer.write()
    raw = (tmp_path / "heartbeat.json").read_bytes()
    watchdog = HangWatchdog(str(tmp_path), hung_s=90.0, alert_fn=alerts.append)

    def boom(_path):
        raise OSError("disk full")

    monkeypatch.setattr(hb, "dump_thread_stacks", boom)
    watchdog._check_once(0.0)
    watchdog._check_once(100.0)
    assert len(alerts) == 1
    watchdog._check_once(200.0)
    assert len(alerts) == 1
    os.remove(writer.path)
    watchdog._check_once(300.0)
    assert watchdog._latched is True
    (tmp_path / "heartbeat.json").write_bytes(b"{broken")
    watchdog._check_once(400.0)
    assert watchdog._latched is True
    assert len(alerts) == 1
    (tmp_path / "heartbeat.json").write_bytes(raw)
    watchdog._check_once(500.0)
    assert len(alerts) == 1, "same heartbeat must not send a second P1"
    writer.write()
    watchdog._check_once(500.1)
    assert watchdog._latched is False
    watchdog._check_once(700.0)
    assert len(alerts) == 2


def test_watchdog_default_poll_waits_two_seconds(tmp_path, monkeypatch):
    """DEFAULT_POLL_S is 2s. A 60s wait is a different watchdog."""
    waits = []
    orig = hb.threading.Event.wait

    def wrapped(self, timeout=None):
        if threading.current_thread().name == "hang-watchdog":
            waits.append(timeout)
            return orig(self, 30)
        return orig(self, timeout)

    monkeypatch.setattr(hb.threading.Event, "wait", wrapped)
    watchdog = HangWatchdog(str(tmp_path))
    watchdog.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not waits:
            time.sleep(0.01)
    finally:
        watchdog.disable()
    assert waits, "watchdog never waited"
    assert waits[0] == 2.0


def test_stop_idles_watchdog_and_foreign_heartbeat_is_quiet(tmp_path):
    alerts = []
    bot = str(tmp_path)
    hb.start(bot, alert_fn=alerts.append, hung_s=0.2, poll_s=0.05)
    try:
        hb.stop()
        assert hb._watchdog is not None and hb._watchdog._enabled is False
        foreign = {
            "ts": time.time() - 500,
            "pid": os.getpid() + 7,
            "inflight_com_call": None,
        }
        (tmp_path / "heartbeat.json").write_text(
            json.dumps(foreign), encoding="utf-8")
        time.sleep(0.3)
        assert alerts == []
        assert not (tmp_path / "hang.json").exists()
        hb._watchdog._check_once(time.monotonic() + 500)
        assert alerts == []
        assert not (tmp_path / "hang.json").exists()
    finally:
        hb.stop()
        if hb._watchdog is not None:
            hb._watchdog.disable()


def test_foreign_pid_heartbeat_while_running_does_not_alert(tmp_path):
    alerts = []
    watchdog = HangWatchdog(str(tmp_path), hung_s=90.0, alert_fn=alerts.append)
    payload = {
        "ts": 1_000.0,
        "pid": os.getpid() + 99,
        "inflight_com_call": None,
    }
    (tmp_path / "heartbeat.json").write_text(json.dumps(payload), encoding="utf-8")
    watchdog._seen_ts = payload["ts"]
    watchdog._seen_mono = 0.0
    watchdog._check_once(1_000.0)
    assert alerts == []
    assert not (tmp_path / "hang.json").exists()
    assert not (tmp_path / "hang_stacks.txt").exists()


def test_stop_then_start_reenables_watchdog_and_alerts(tmp_path):
    """configure() must set _enabled again. A second start after stop alerts.

    Removing ``self._enabled = True`` from configure() leaves the thread
    parked and this hang produces no alert.
    """
    alerts = []
    bot = str(tmp_path)
    hb.start(bot, alert_fn=alerts.append, hung_s=0.25, poll_s=0.05)
    try:
        hb.stop()
        assert hb._watchdog is not None and hb._watchdog._enabled is False
        hb.start(bot, alert_fn=alerts.append, hung_s=0.25, poll_s=0.05)
        assert hb._watchdog._enabled is True
        alerts.clear()
        # start() just wrote a fresh ts. Age the snapshot the watchdog
        # actually reads. An on-disk overwrite is invisible to this
        # process (the file is for check_bots). The first sight baselines
        # the stall at 0; after hung_s of an unchanged ts it alerts.
        hb._writer.write(now=time.time() - 10_000)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not alerts:
            time.sleep(0.05)
        assert alerts, "watchdog stayed idle after configure()/start()"
    finally:
        hb.stop()
        if hb._watchdog is not None:
            hb._watchdog.disable()


def test_start_reraises_main_thread_heartbeat_error(tmp_path, monkeypatch):
    """A refused off-thread write must not be swallowed or install a writer."""
    def boom(self, now=None):
        raise MainThreadHeartbeatError("off thread")

    monkeypatch.setattr(HeartbeatWriter, "write", boom)
    try:
        with pytest.raises(MainThreadHeartbeatError):
            hb.start(str(tmp_path))
        assert hb._writer is None
    finally:
        hb.stop()
        if hb._watchdog is not None:
            hb._watchdog.disable()


def test_start_rotates_hang_artifacts(tmp_path):
    (tmp_path / "hang.json").write_text("old-hang", encoding="utf-8")
    (tmp_path / "hang_stacks.txt").write_text("old-stacks", encoding="utf-8")
    try:
        hb.start(str(tmp_path), hung_s=90.0, poll_s=30.0)
        assert (tmp_path / "hang.json.prev").read_text(encoding="utf-8") == "old-hang"
        assert (tmp_path / "hang_stacks.txt.prev").read_text(encoding="utf-8") == "old-stacks"
        assert not (tmp_path / "hang.json").exists()
        assert not (tmp_path / "hang_stacks.txt").exists()
    finally:
        hb.stop()
        if hb._watchdog is not None:
            hb._watchdog.disable()


def _deny_replace(_src, _dst):
    raise PermissionError("denied")


def test_replace_retries_then_succeeds_without_log(tmp_path, monkeypatch, caplog):
    """N-1 PermissionErrors then success: no raise, no log line."""
    hb.set_replace_failure_logger(None)
    hb.reset_replace_failure_log()
    sleeps = []
    calls = {"n": 0}
    real = hb.os.replace

    def wrapped(src, dst):
        calls["n"] += 1
        if calls["n"] < hb._REPLACE_ATTEMPTS:
            raise PermissionError("busy")
        return real(src, dst)

    monkeypatch.setattr(hb.os, "replace", wrapped)
    path = tmp_path / "heartbeat.json"
    with caplog.at_level(logging.ERROR, logger="src.live.main_heartbeat"):
        hb.atomic_write_json(
            str(path), {"ts": 1, "pid": 1},
            sleep=lambda delay: sleeps.append(delay),
            monotonic=lambda: 0.0)
    assert calls["n"] == hb._REPLACE_ATTEMPTS
    assert sleeps == list(hb._REPLACE_BACKOFF_S)
    assert sum(hb._REPLACE_BACKOFF_S) <= 0.035
    assert caplog.records == []
    assert json.loads(path.read_text(encoding="utf-8"))["ts"] == 1
    assert list(tmp_path.glob(".hb-*")) == []


def test_replace_always_fails_cleans_tmp_and_rate_limits(tmp_path, monkeypatch, caplog):
    """Every attempt fails: raise, tmp removed, one line, then a counted repeat."""
    hb.set_replace_failure_logger(None)
    hb.reset_replace_failure_log()
    clock = {"t": 1_000.0}
    monkeypatch.setattr(hb.os, "replace", _deny_replace)
    path = str(tmp_path / "heartbeat.json")

    def write():
        hb.atomic_write_json(
            path, {"ts": 1},
            sleep=lambda _delay: None,
            monotonic=lambda: clock["t"])

    try:
        with caplog.at_level(logging.ERROR, logger="src.live.main_heartbeat"):
            with pytest.raises(PermissionError):
                write()
            assert list(tmp_path.glob(".hb-*")) == []
            assert len(caplog.records) == 1
            assert "suppressed" not in caplog.records[0].message
            assert "PermissionError" in caplog.records[0].message
            assert "\n" not in caplog.records[0].message
            with pytest.raises(PermissionError):
                write()
            with pytest.raises(PermissionError):
                write()
            assert len(caplog.records) == 1
            clock["t"] += hb._REPLACE_LOG_INTERVAL_S
            with pytest.raises(PermissionError):
                write()
            assert len(caplog.records) == 2
            assert "2 suppressed" in caplog.records[1].message
    finally:
        hb.reset_replace_failure_log()


def test_replace_does_not_retry_other_oserror(tmp_path, monkeypatch, caplog):
    hb.reset_replace_failure_log()
    calls = {"n": 0}
    sleeps = []

    def boom(_src, _dst):
        calls["n"] += 1
        raise OSError("disk full")

    monkeypatch.setattr(hb.os, "replace", boom)
    with caplog.at_level(logging.ERROR, logger="src.live.main_heartbeat"):
        with pytest.raises(OSError, match="disk full"):
            hb.atomic_write_json(
                str(tmp_path / "heartbeat.json"), {"ts": 1},
                sleep=lambda delay: sleeps.append(delay))
    assert calls["n"] == 1
    assert sleeps == []
    assert caplog.records == []
    assert list(tmp_path.glob(".hb-*")) == []


def test_read_heartbeat_retries_permission_error_then_succeeds(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"ts": 5, "pid": 7}), encoding="utf-8")
    sleeps = []
    calls = {"n": 0}
    real = hb._open_read

    def flaky(target):
        calls["n"] += 1
        if calls["n"] < hb._READ_ATTEMPTS:
            raise PermissionError("busy")
        return real(target)

    monkeypatch.setattr(hb, "_open_read", flaky)
    data = hb.read_heartbeat(str(path), sleep=lambda delay: sleeps.append(delay))
    assert data["ts"] == 5
    assert data["pid"] == 7
    assert calls["n"] == hb._READ_ATTEMPTS
    assert sleeps == [hb._READ_BACKOFF_S, hb._READ_BACKOFF_S]


def test_read_heartbeat_persistent_permission_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    path.write_text("{}", encoding="utf-8")
    sleeps = []
    calls = {"n": 0}

    def boom(_target):
        calls["n"] += 1
        raise PermissionError("busy")

    monkeypatch.setattr(hb, "_open_read", boom)
    assert hb.read_heartbeat(
        str(path), sleep=lambda delay: sleeps.append(delay)) is None
    assert calls["n"] == hb._READ_ATTEMPTS
    assert sleeps == [hb._READ_BACKOFF_S, hb._READ_BACKOFF_S]


def test_read_heartbeat_does_not_retry_corrupt_json(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    path.write_text("{broken", encoding="utf-8")
    sleeps = []
    calls = {"n": 0}
    real = hb._open_read

    def counting(target):
        calls["n"] += 1
        return real(target)

    monkeypatch.setattr(hb, "_open_read", counting)
    assert hb.read_heartbeat(
        str(path), sleep=lambda delay: sleeps.append(delay)) is None
    assert calls["n"] == 1
    assert sleeps == []


def test_watchdog_reads_snapshot_not_a_replaced_file(tmp_path):
    """An on-disk overwrite is another process. This watchdog keeps the snapshot."""
    alerts = []
    writer = HeartbeatWriter(str(tmp_path))
    writer.write(now=1_000.0)
    (tmp_path / "heartbeat.json").write_text(
        json.dumps({
            "ts": 1.0,
            "pid": os.getpid() + 50,
            "inflight_com_call": None,
        }),
        encoding="utf-8")
    watchdog = HangWatchdog(str(tmp_path), hung_s=90.0, alert_fn=alerts.append)
    watchdog._check_once(0.0)
    watchdog._check_once(100.0)
    assert len(alerts) == 1
    assert "P1" in alerts[0]


def test_call_com_exhausted_replace_logs_once(tmp_path, monkeypatch, caplog):
    """Stamp failures go through the rate-limited sink, not logger.exception."""
    hb.reset_replace_failure_log()
    lines = []
    hb.set_replace_failure_logger(lines.append)
    monkeypatch.setattr(hb, "_sleep", lambda _delay: None)
    monkeypatch.setattr(hb.os, "replace", _deny_replace)
    writer = HeartbeatWriter(str(tmp_path))
    hb._writer = writer
    try:
        with caplog.at_level(logging.ERROR, logger="src.live.main_heartbeat"):
            assert hb.call_com("LeaveMonitor", lambda: "ok") == "ok"
    finally:
        hb._writer = None
        hb.set_replace_failure_logger(None)
        hb.reset_replace_failure_log()
    assert len(lines) == 1
    assert "os.replace failed" in lines[0]
    assert "suppressed" not in lines[0]
    assert caplog.records == []


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows sharing-violation stress test")
def test_windows_reader_writer_stress(tmp_path):
    """Tight reader plus 500 replaces. Both sides retry; no exception escapes."""
    path = str(tmp_path / "heartbeat.json")
    hb.atomic_write_json(path, {"ts": 0, "pid": os.getpid(), "inflight_com_call": None})
    stop = threading.Event()
    errors = []

    def reader():
        while not stop.is_set():
            try:
                hb.read_heartbeat(path)
            except Exception as exc:  # noqa: BLE001 — the test's contract
                errors.append(exc)
                return

    thread = threading.Thread(target=reader, name="hb-stress-reader")
    thread.start()
    try:
        for i in range(500):
            hb.atomic_write_json(
                path, {"ts": i, "pid": os.getpid(), "inflight_com_call": None})
    finally:
        stop.set()
        thread.join(timeout=5)
    assert errors == []
    assert thread.is_alive() is False
    assert hb.read_heartbeat(path)["ts"] == 499


def test_attempt_reconnect_stamps_leave_monitor():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "run_backtest.py")
    text = open(path, encoding="utf-8").read()
    assert 'call_com("LeaveMonitor"' in text
    send_at = text.find("skO.SendFutureOrderCLR")
    assert send_at != -1
    assert "call_com(" in text[max(0, send_at - 120):send_at]
