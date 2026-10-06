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


def test_attempt_reconnect_stamps_leave_monitor():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "run_backtest.py")
    text = open(path, encoding="utf-8").read()
    assert 'call_com("LeaveMonitor"' in text
    send_at = text.find("skO.SendFutureOrderCLR")
    assert send_at != -1
    assert "call_com(" in text[max(0, send_at - 120):send_at]
