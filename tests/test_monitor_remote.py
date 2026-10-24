"""Tests for remote-host monitoring (scripts/monitor/remote.py).

Since 2026-10-24 the news bridge (W1-W4) and the 08-04-rss regime bot run
on a second trading PC.  The local daily review used to read
``C:\\Users\\eric8\\n8n-bridge`` and the main tree's stale copy of the bot
directory, so it graded both as dead and the stopped n8n-service as P1.
These tests pin the replacement: a per-host snapshot fetched over ONE ssh
call, mirrored locally with mtimes preserved, and the unchanged checks run
against the mirror.

Nothing here touches the network: the ssh runner is injected, and the
PowerShell round-trip test runs the real fetch script against a local
temp directory (skipped where PowerShell is unavailable, e.g. CI).

Anchor time: Tuesday 2026-08-25 16:00 TPE — NIGHT session open.
"""

import base64
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

import scripts.monitor.daily_review as daily_review
from scripts.monitor.common import TZ_TPE, discover_bot_dirs
from scripts.monitor.remote import (
    RemoteHost, build_fetch_script, check_tasks, fetch_snapshot, load_hosts,
    materialize, REMOTE_COMMAND,
)

NOW = datetime(2026, 8, 25, 16, 0, tzinfo=TZ_TPE)
RSS = "TMF00_08-04-rss-機器人"
TONIGHT = "2026-08-25|NIGHT"
# This PC after the move: Discord wired, news.* still pointing at the old
# local n8n-bridge directory nobody writes any more.
LOCAL_SETTINGS = {
    "notifications": {"discord_bot_token": "t", "discord_channel_id": "c"},
    "news": {"discord_webhook": "",
             "signal_path": "/nonexistent/n8n-bridge/signal.json",
             "regime_vote_path": "/nonexistent/n8n-bridge/regime_vote.json",
             "events_path": "/nonexistent/n8n-bridge/events.json",
             "rss_state_file": "/nonexistent/n8n-bridge/rss_scorer_state.json"},
}


def b64(text) -> str:
    raw = text if isinstance(text, bytes) else str(text).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def wire_file(scope, name, content, mtime):
    return f"FILE {b64(scope)} {b64(name)} {mtime:.3f} {b64(content)}"


def host(**kw):
    base = dict(name="steven", address="100.64.0.9", user="tester",
                identity_file="", bridge_dir=r"C:\remote\n8n-bridge",
                live_dir=r"C:\remote\tai-robot-src\data\live", bots=(RSS,),
                task_path="\\TaiRobot\\")
    base.update(kw)
    return RemoteHost(**base)


def healthy_wire(now=NOW, *, pid_alive=True, with_lock=True, tasks=None):
    """The wire text a healthy Steven returns at ``now``."""
    t = now.timestamp()
    rows = [f"NOW {t:.3f}", f"DIR {b64('@bridge')} 1"]
    bridge = {
        "monitor_state.json": json.dumps(
            {"last_check": (now - timedelta(minutes=10)).isoformat(),
             "last_result": "fresh 5/11 | fired: none"}),
        "rss_scorer_state.json": json.dumps(
            {"last_check": (now - timedelta(minutes=20)).isoformat(),
             "session_net": 1.09, "session_peak": 2.29}),
        ".chips_state.json": json.dumps(
            {"last_check": (now - timedelta(hours=14)).isoformat(),
             "last_result": "vote trending-up"}),
        "events.json": json.dumps(
            {"version": 1, "updated_at": (now - timedelta(hours=6)).isoformat(),
             "events": []}),
        "monitor.log": f"{(now - timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')}"
                       f" TPE | fresh 5/11 | fired: none | vote: -\n",
        "chips_monitor.log": "2026-08-24 16:15:04 TPE | vote trending-up\n"
                             "2026-08-24 18:15:04 TPE | no vote — hysteresis band\n",
        "task_W2-crossmarket.last.log": "W2 pass ok\n",
    }
    for name, content in bridge.items():
        rows.append(wire_file("@bridge", name, content, t - 600))

    rows.append(f"DIR {b64(RSS)} 1")
    bot = {
        "session.json": json.dumps({
            "regime_mode": True, "strategy": "AI: BbandSmaShortV3",
            "trading_mode": "paper", "active_leg": "short",
            "long_strategy": "L", "short_strategy": "S",
            "broker": {"_cumulative_pnl": 120, "position_size": 0,
                       "position_side": None, "trades": []}}),
        "heartbeat.json": json.dumps(
            {"ts": t - 5, "pid": 26824, "inflight_com_call": None}),
        "debug_20260825.log": f"[2026-08-25 15:59:30.100 TPE / x local] tick\n",
    }
    if with_lock:
        bot[".lock"] = "26824"
    for name, content in bot.items():
        rows.append(wire_file(RSS, name, content, t - 30))
    if with_lock:
        rows.append(f"PID {b64(RSS)} 26824 {1 if pid_alive else 0}")
    for task in tasks if tasks is not None else [
            {"name": "Bridge-W2-crossmarket", "state": "Ready",
             "last_result": 0, "last_run": t - 120, "next_run": t + 480}]:
        rows.append(f"TASK {b64(json.dumps(task))}")
    return "\n".join(rows) + "\n"


def stale_local_tree(tmp_path):
    """The main tree after the move: the rss bot's STALE copy is still there."""
    live = tmp_path / "local_live"
    stale = live / RSS
    stale.mkdir(parents=True)
    (stale / "session.json").write_text(json.dumps(
        {"regime_mode": True, "trading_mode": "paper", "strategy": "S"}),
        encoding="utf-8")
    (stale / "regime_state.json").write_text(json.dumps(
        {"last_assessed": "2026-08-12|NIGHT", "paused_until_session": 50,
         "session_count": 49, "flip_history": ["2026-08-06"]}),
        encoding="utf-8")
    # an ordinary local bot that stays local
    other = live / "TMF00_0422"
    other.mkdir()
    (other / "session.json").write_text(json.dumps(
        {"strategy": "S", "trading_mode": "paper"}), encoding="utf-8")
    return live


def no_n8n(monkeypatch):
    """The local n8n probe must not run when the bridge lives elsewhere."""
    def boom(now):
        raise AssertionError("check_n8n ran although the bridge is remote")
    monkeypatch.setattr(daily_review, "check_n8n", boom)


# ── config ──────────────────────────────────────────────────────────────

def test_load_hosts_reads_only_hosts_with_a_monitor_block(tmp_path):
    path = tmp_path / "hosts.local.yaml"
    # a UTF-8 BOM, like the owner's real file
    path.write_bytes("\ufeff".encode("utf-8") + (
        "hosts:\n"
        "  steven:\n"
        "    tailscale_ip: 100.64.0.9\n"
        "    user: tester\n"
        "    identity_file: ~/.ssh/id_test\n"
        "    monitor:\n"
        "      bridge_dir: 'C:\\remote\\n8n-bridge'\n"
        "      live_dir: 'C:\\remote\\live'\n"
        f"      bots: ['{RSS}']\n"
        "      task_path: '\\TaiRobot\\'\n"
        "  oreopie:\n"
        "    tailscale_ip: 100.64.0.1\n").encode("utf-8"))
    hosts = load_hosts(str(path))
    assert [h.name for h in hosts] == ["steven"]
    h = hosts[0]
    assert h.address == "100.64.0.9" and h.user == "tester"
    assert h.bots == (RSS,) and h.runs_bridge
    assert h.task_path == "\\TaiRobot\\"


def test_load_hosts_missing_file_is_empty(tmp_path):
    assert load_hosts(str(tmp_path / "nope.yaml")) == []


def test_discover_bot_dirs_excludes_relocated_bots(tmp_path):
    live = stale_local_tree(tmp_path)
    names = [os.path.basename(d) for d in discover_bot_dirs(str(live), exclude={RSS})]
    assert names == ["TMF00_0422"]


# ── fetch script / wire format ──────────────────────────────────────────

def test_fetch_script_is_ascii_and_carries_no_host_identity():
    script = build_fetch_script(host())
    script.encode("ascii")  # the bot name travels base64-encoded
    for secret in ("100.64.0.9", "tester"):
        assert secret not in script


def test_materialize_mirrors_files_with_remote_mtimes(tmp_path):
    snap = materialize(host(), healthy_wire(), str(tmp_path / "mirror"))
    assert not snap.error
    lock = os.path.join(snap.live_dir, RSS, ".lock")
    assert open(lock, encoding="utf-8").read() == "26824"
    assert abs(os.path.getmtime(lock) - (NOW.timestamp() - 30)) < 1
    assert os.path.isfile(os.path.join(snap.bridge_dir, "monitor.log"))
    assert snap.pid_alive(26824) is True
    assert snap.pid_alive(1) is True, "an unprobed PID must read as alive"


def test_materialize_refuses_path_traversal(tmp_path):
    wire = healthy_wire() + wire_file("@bridge", "..\\..\\evil.txt", "x",
                                      NOW.timestamp()) + "\n" \
        + wire_file("SOME_OTHER_BOT", "session.json", "{}", NOW.timestamp()) + "\n"
    snap = materialize(host(), wire, str(tmp_path / "mirror"))
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "mirror" / "evil.txt").exists()
    assert not os.path.exists(os.path.join(snap.live_dir, "SOME_OTHER_BOT"))


def test_fetch_snapshot_ssh_failure_is_an_error_not_a_crash(tmp_path):
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 255, b"", b"ssh: connect timed out")
    snap = fetch_snapshot(host(), str(tmp_path / "mirror"), runner=runner)
    assert snap.error and "timed out" in snap.error


def test_fetch_snapshot_sends_the_script_on_stdin(tmp_path):
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw.get("input")
        return subprocess.CompletedProcess(cmd, 0, healthy_wire().encode("ascii"), b"")
    h = host(identity_file="~/.ssh/id_test")
    snap = fetch_snapshot(h, str(tmp_path / "mirror"), runner=runner)
    assert not snap.error
    assert os.path.basename(seen["cmd"][0]).lower() in ("ssh", "ssh.exe")
    assert seen["cmd"][1:3] == ["-F", os.devnull] and "BatchMode=yes" in seen["cmd"]
    assert "tester@100.64.0.9" in seen["cmd"] and seen["cmd"][-1] == REMOTE_COMMAND
    assert seen["input"] == build_fetch_script(h).encode("ascii")


@pytest.mark.skipif(shutil.which("powershell") is None,
                    reason="needs Windows PowerShell")
def test_fetch_script_round_trip_through_real_powershell(tmp_path):
    """The real PowerShell script, run the way sshd runs it, over a local tree."""
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "monitor_state.json").write_text('{"last_check": "x"}', encoding="utf-8")
    (bridge / "monitor.log").write_text("line\n" * 10, encoding="utf-8")
    (bridge / "unrelated.txt").write_text("not fetched", encoding="utf-8")
    bot = tmp_path / "live" / RSS
    bot.mkdir(parents=True)
    (bot / ".lock").write_text(str(os.getpid()), encoding="utf-8")
    (bot / "session.json").write_text('{"strategy": "空單"}', encoding="utf-8")
    for day in ("20260823", "20260824", "20260825"):
        (bot / f"debug_{day}.log").write_text(f"{day}\n", encoding="utf-8")
    (bot / "cli_stdout.log").write_text("huge", encoding="utf-8")
    h = host(bridge_dir=str(bridge), live_dir=str(tmp_path / "live"),
             task_path="\\NoSuchTaskFolderForTests\\")

    out = subprocess.run(REMOTE_COMMAND, shell=True, capture_output=True,
                         input=build_fetch_script(h).encode("ascii"), timeout=120)
    snap = materialize(h, out.stdout.decode("ascii", "replace"),
                       str(tmp_path / "mirror"))

    assert not snap.error, (snap.error, out.stderr[-500:])
    assert sorted(os.listdir(snap.bridge_dir)) == ["monitor.log", "monitor_state.json"]
    mirrored = sorted(os.listdir(os.path.join(snap.live_dir, RSS)))
    assert mirrored == [".lock", "debug_20260824.log", "debug_20260825.log",
                        "session.json"], mirrored
    assert json.load(open(os.path.join(snap.live_dir, RSS, "session.json"),
                          encoding="utf-8"))["strategy"] == "空單"
    assert snap.pid_alive(os.getpid()) is True
    assert abs(os.path.getmtime(os.path.join(snap.bridge_dir, "monitor.log"))
               - os.path.getmtime(bridge / "monitor.log")) < 1


# ── checks over the mirror ──────────────────────────────────────────────

def test_failed_scheduled_task_is_p2(tmp_path):
    t = NOW.timestamp()
    snap = materialize(host(), healthy_wire(tasks=[
        {"name": "Bridge-W3-rss", "state": "Ready", "last_result": 1,
         "last_run": t - 60, "next_run": t + 1700},
        {"name": "Bridge-W4-chips", "state": "Running", "last_result": 267009,
         "last_run": t - 5, "next_run": t + 3600},
    ]), str(tmp_path / "mirror"))
    findings, lines = check_tasks(NOW, snap)
    p2 = [f.message for f in findings if f.level == "P2"]
    assert any("Bridge-W3-rss" in m for m in p2), p2
    assert not any("Bridge-W4-chips" in m for m in p2), "0x41301 = running, not a failure"


def test_disabled_scheduled_task_is_p2(tmp_path):
    t = NOW.timestamp()
    snap = materialize(host(), healthy_wire(tasks=[
        {"name": "Bridge-W1-calendar", "state": "Disabled", "last_result": 0,
         "last_run": t - 3600, "next_run": 0}]), str(tmp_path / "mirror"))
    findings, _ = check_tasks(NOW, snap)
    assert any("Disabled" in f.message for f in findings if f.level == "P2")


def test_clock_skew_is_p2(tmp_path):
    snap = materialize(host(), healthy_wire(now=NOW + timedelta(minutes=20)),
                       str(tmp_path / "mirror"))
    findings, _ = check_tasks(NOW, snap)
    assert any("clock" in f.message for f in findings if f.level == "P2")


# ── daily review integration ────────────────────────────────────────────

def test_review_with_healthy_remote_flags_nothing_dead(tmp_path, monkeypatch):
    """The 2026-10-24 regression: bridge + rss bot moved, review said dead."""
    no_n8n(monkeypatch)
    live = stale_local_tree(tmp_path)
    snap = materialize(host(), healthy_wire(), str(tmp_path / "mirror"))
    grade, findings, report = daily_review.run_review(
        NOW, LOCAL_SETTINGS, str(live), remotes=[snap])

    bad = [f for f in findings if f.level in ("P1", "P2")]
    assert not bad, [(f.level, f.area, f.message) for f in bad]
    assert "## BOTS @steven" in report and "## BRIDGE @steven" in report
    # the stale local copy is not graded at all
    assert not any(RSS in f.message and "@" not in f.area for f in findings), \
        [f.message for f in findings if RSS in f.message]
    assert "intentionally stopped" in report


def test_review_remote_bot_with_dead_pid_is_p1(tmp_path, monkeypatch):
    no_n8n(monkeypatch)
    snap = materialize(host(), healthy_wire(pid_alive=False),
                       str(tmp_path / "mirror"))
    _g, findings, _r = daily_review.run_review(
        NOW, LOCAL_SETTINGS, str(stale_local_tree(tmp_path)), remotes=[snap])
    p1 = [f for f in findings if f.level == "P1"]
    assert any(f.area == "bots@steven" and RSS in f.message for f in p1), \
        [(f.area, f.message) for f in p1]


def test_review_remote_bot_without_lock_is_p2(tmp_path, monkeypatch):
    """A configured remote bot is EXPECTED to run; P3 'no .lock' would hide it."""
    no_n8n(monkeypatch)
    snap = materialize(host(), healthy_wire(with_lock=False),
                       str(tmp_path / "mirror"))
    _g, findings, _r = daily_review.run_review(
        NOW, LOCAL_SETTINGS, str(stale_local_tree(tmp_path)), remotes=[snap])
    assert any(f.level == "P2" and RSS in f.message and "not running" in f.message
               for f in findings), [(f.level, f.message) for f in findings]


def test_review_unreachable_host_is_p1(tmp_path, monkeypatch):
    no_n8n(monkeypatch)

    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 255, b"", b"Connection timed out")
    snap = fetch_snapshot(host(), str(tmp_path / "mirror"), runner=runner)
    _g, findings, report = daily_review.run_review(
        NOW, LOCAL_SETTINGS, str(stale_local_tree(tmp_path)), remotes=[snap])
    p1 = [f.message for f in findings if f.level == "P1"]
    assert any("steven" in m and "UNVERIFIED" in m for m in p1), p1
    # still no local verdict on the relocated bot
    assert not any(RSS in m for m in p1 if "steven" not in m)


def test_review_missing_remote_bot_dir_is_p2(tmp_path, monkeypatch):
    no_n8n(monkeypatch)
    wire = healthy_wire().replace(f"DIR {b64(RSS)} 1", f"DIR {b64(RSS)} 0")
    wire = "\n".join(ln for ln in wire.splitlines()
                     if not ln.startswith(("FILE " + b64(RSS), "PID ")))
    snap = materialize(host(), wire, str(tmp_path / "mirror"))
    _g, findings, _r = daily_review.run_review(
        NOW, LOCAL_SETTINGS, str(stale_local_tree(tmp_path)), remotes=[snap])
    assert any(f.level == "P2" and RSS in f.message and "not found" in f.message
               for f in findings), [(f.level, f.message) for f in findings]


def test_summary_lists_remote_bot_and_bridge(tmp_path, monkeypatch):
    no_n8n(monkeypatch)
    live = stale_local_tree(tmp_path)
    snap = materialize(host(), healthy_wire(), str(tmp_path / "mirror"))
    grade, findings, _r = daily_review.run_review(NOW, LOCAL_SETTINGS, str(live), remotes=[snap])
    text = daily_review.build_summary(NOW, grade, findings, str(live), LOCAL_SETTINGS,
                                      remotes=[snap])
    assert f"{RSS}@steven" in text
    assert "W2@steven" in text
    assert "tester" not in text and "100.64.0.9" not in text


def test_review_without_remotes_still_runs_local_n8n_check(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(daily_review, "check_n8n",
                        lambda now: (calls.append(now) or ([], ["n8n ok"])))
    daily_review.run_review(NOW, LOCAL_SETTINGS, str(stale_local_tree(tmp_path)))
    assert calls, "a PC that still hosts the bridge must keep its n8n check"
