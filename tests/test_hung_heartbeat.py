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
import os
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
    """
    hung_s = 0.4
    alerts = []
    writer = HeartbeatWriter(str(tmp_path))
    watchdog = HangWatchdog(
        str(tmp_path), hung_s=hung_s, inflight_hung_s=hung_s,
        poll_s=0.05, alert_fn=alerts.append)
    watchdog.start()
    hb._writer = writer
    pid = os.getpid()
    release = threading.Event()
    seen = {}

    def waiter():
        deadline = time.monotonic() + hung_s + 5
        freeze_checked = False
        try:
            while time.monotonic() < deadline:
                data = hb.read_heartbeat(writer.path)
                inflight = (data or {}).get("inflight_com_call") or {}
                if (not freeze_checked and isinstance(inflight, dict)
                        and inflight.get("name") == "LeaveMonitor"):
                    ts1 = data["ts"]
                    time.sleep(0.12)
                    again = hb.read_heartbeat(writer.path)
                    seen["frozen"] = (
                        again is not None and again.get("ts") == ts1)
                    freeze_checked = True
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
        for _ in range(2):
            writer.write()
            time.sleep(0.02)

        def fake_leave_monitor():
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


def test_attempt_reconnect_stamps_leave_monitor():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "run_backtest.py")
    text = open(path, encoding="utf-8").read()
    assert 'call_com("LeaveMonitor"' in text
    send_at = text.find("skO.SendFutureOrderCLR")
    assert send_at != -1
    assert "call_com(" in text[max(0, send_at - 120):send_at]
