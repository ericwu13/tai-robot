"""Headless self-restart once per open session on a quote-zombie DEGRADED.

Issues #322 and #279. A headless bot that stays DEGRADED, with no fresh
tick, for five continuous minutes of an open session stops while keeping
its position and spawns one ``--resume`` replacement. The same session
does not restart again. ``semi_auto`` alerts only.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live import degraded_restart as dr
from src.live import headless
from src.live.headless import (
    EXIT_BOT_STOPPED,
    EXIT_DEPLOY_REFUSED,
    EXIT_LOGIN,
    EXIT_NO_COM,
    EXIT_OK,
    build_parser,
    restart_command,
)
from src.live.headless_app import run_deploy
from src.live.live_runner import LiveRunner
from src.live.session_store import load_session
from src.live.tick_watchdog import TickWatchdog
from tests.test_deploy_abort_keeps_position import _app, _runner

TPE = timezone(timedelta(hours=8))
DAY = datetime(2026, 11, 19, 10, 0, tzinfo=TPE)
NOW = 1_700_000_000.0
SESSION = "2026-11-19|DAY"
NEXT_SESSION = "2026-11-19|NIGHT"


class _Clock:
    def __init__(self):
        self.now = NOW
        self.session = SESSION
        self.mins = 100

    def policy(self, **kwargs):
        return dr.DegradedRestartPolicy(
            now=lambda: self.now,
            now_dt=lambda: DAY,
            session_for_fn=lambda _dt: self.session,
            minutes_left_fn=lambda _symbol=None: self.mins,
            **kwargs,
        )


def _app_view(*, degraded=True, fresh=0.0, mode="paper", fill_pending=False,
              poller_active=False, retry=None, confirmed=False, size=0,
              side="LONG"):
    side_obj = SimpleNamespace(value=side) if size else None
    return SimpleNamespace(
        _link_host=SimpleNamespace(snapshot=lambda: {"degraded": degraded}),
        _last_fresh_tick_at=fresh,
        _trading_mode=mode,
        _trading_guard=SimpleNamespace(
            fill_pending=fill_pending,
            real_entry_confirmed=confirmed,
        ),
        _fill_poller=SimpleNamespace(active=poller_active),
        _exit_fill_retry_ctx=retry,
        _live_runner=SimpleNamespace(
            symbol="TMF00",
            bot_name="night1",
            strategy=SimpleNamespace(name="BollingerBreakout"),
            broker=SimpleNamespace(position_size=size, position_side=side_obj),
        ),
    )


def _arm(policy, age_s=300.0):
    """Clock already past the restart threshold, no fresh tick since."""
    policy.degraded_open_since = NOW - age_s


class _Discord:
    enabled = True

    def __init__(self):
        self.started = []
        self.recovered = []
        self.skipped = []
        self.failed = []
        self.notes = []
        self.stopped = []

    def degraded_restart_starting(self, **kwargs):
        self.started.append(kwargs)

    def degraded_restart_recovered(self, **kwargs):
        self.recovered.append(kwargs)

    def degraded_restart_skipped(self, **kwargs):
        self.skipped.append(kwargs)

    def degraded_restart_failed(self, reason, **kwargs):
        self.failed.append((reason, kwargs))

    def notify(self, message, *, priority=False):
        self.notes.append((message, priority))

    def bot_stopped(self, trades, pnl):
        self.stopped.append((trades, pnl))


def _deploy_args(tmp_path, **extra):
    bot_dir = tmp_path / "data" / "live" / "TMF00_night1"
    args = SimpleNamespace(
        symbol="TMF00",
        bot="night1",
        detach=False,
        wait_for_pid=0,
        relogin_stagger=False,
        login_timeout=1,
        heartbeat_min=0,
        _raw_argv=["deploy", "--symbol", "TMF00", "--bot", "night1",
                   "--mode", "paper"],
    )
    for key, value in extra.items():
        setattr(args, key, value)
    return args, str(bot_dir)


# ── pure policy + clock ──────────────────────────────────────────────


def test_closed_or_near_close_resets_the_clock_and_does_not_fire(tmp_path):
    clock = _Clock()
    policy = clock.policy()
    app = _app_view()
    bot = str(tmp_path)
    policy.degraded_open_since = NOW - 500

    clock.session = None
    clock.mins = None
    out = policy.observe_app(app, bot_dir=bot)
    assert isinstance(out, dr.Noop)
    assert policy.degraded_open_since is None

    policy.degraded_open_since = NOW - 500
    clock.session = SESSION
    for mins in (10, 11, 15, 9, 0, TickWatchdog.NEAR_CLOSE_SUPPRESS,
                 dr.MIN_MINUTES_LEFT_FOR_RESTART):
        clock.mins = mins
        policy.degraded_open_since = NOW - 500
        out = policy.observe_app(app, bot_dir=bot)
        assert isinstance(out, dr.Noop), mins
        assert not isinstance(out, dr.RestartDecision)
        assert policy.degraded_open_since is None

    clock.mins = dr.MIN_MINUTES_LEFT_FOR_RESTART + 1
    out = policy.observe_app(app, bot_dir=bot)
    assert isinstance(out, dr.Noop)
    assert policy.degraded_open_since == NOW


def test_restart_buffer_covers_the_handoff_before_session_end_close(tmp_path):
    """11–18 minutes left is still inside the restart handoff. Do not fire."""
    assert dr.MIN_MINUTES_LEFT_FOR_RESTART == 18
    assert dr.MIN_MINUTES_LEFT_FOR_RESTART > (
        5 + (dr.PID_WAIT_S + dr.DEFAULT_RELOGIN_WAIT_S) / 60
    )
    clock = _Clock()
    policy = clock.policy()
    app = _app_view()
    bot = str(tmp_path)
    for mins in (10, 11, 15, dr.MIN_MINUTES_LEFT_FOR_RESTART):
        clock.mins = mins
        policy.degraded_open_since = NOW - 500
        out = policy.observe_app(app, bot_dir=bot)
        assert isinstance(out, dr.Noop), mins
        assert policy.degraded_open_since is None
    clock.mins = dr.MIN_MINUTES_LEFT_FOR_RESTART + 1
    out = policy.observe_app(app, bot_dir=bot)
    assert isinstance(out, dr.Noop)
    assert policy.degraded_open_since == NOW


def test_fresh_tick_resets_the_clock_while_degraded_stays_true(tmp_path):
    """A tick newer than the degraded clock restarts the wait.

    Covers reply-only DEGRADED while quotes still flow, and a bot that
    was DEGRADED overnight: the first tick at the open must not restart
    immediately.
    """
    clock = _Clock()
    policy = clock.policy()
    policy.degraded_open_since = NOW - 10_000
    app = _app_view(degraded=True, fresh=NOW - 9_000)
    out = policy.observe_app(app, bot_dir=str(tmp_path))
    assert isinstance(out, dr.Noop)
    assert not isinstance(out, dr.RestartDecision)
    assert policy.degraded_open_since is None

    clock.now = NOW + 1
    out = policy.observe_app(app, bot_dir=str(tmp_path))
    assert isinstance(out, dr.Noop)
    assert policy.degraded_open_since == NOW + 1


def test_open_session_degraded_fires_once_per_session_key(tmp_path):
    clock = _Clock()
    policy = clock.policy()
    app = _app_view(fresh=NOW - 10_000)
    bot = str(tmp_path)

    policy.degraded_open_since = None
    assert isinstance(policy.observe_app(app, bot_dir=bot), dr.Noop)
    assert policy.degraded_open_since == NOW

    _arm(policy, 299.0)
    assert isinstance(policy.observe_app(app, bot_dir=bot), dr.Noop)

    _arm(policy, 300.0)
    decision = policy.observe_app(app, bot_dir=bot)
    assert isinstance(decision, dr.RestartDecision)
    assert decision.session_key == SESSION
    assert not os.path.exists(dr.auto_restart_path(bot))

    dr.save_auto_restart(bot, SESSION, old_pid=42)
    saved = load_session(dr.auto_restart_path(bot))
    assert saved["session_key"] == SESSION
    assert saved["pending_notify"] is True
    assert saved["old_pid"] == 42
    assert saved["at"]

    again = policy.observe_app(app, bot_dir=bot)
    assert isinstance(again, dr.AlertOnly)
    assert again.reason == "already_restarted"
    assert again.session_key == SESSION
    third = policy.observe_app(app, bot_dir=bot)
    assert isinstance(third, dr.Noop)

    clock.session = NEXT_SESSION
    policy.degraded_open_since = NOW - 300
    nxt = policy.observe_app(app, bot_dir=bot)
    assert isinstance(nxt, dr.RestartDecision)
    assert nxt.session_key == NEXT_SESSION


def test_pending_notify_false_still_blocks_the_same_session(tmp_path):
    clock = _Clock()
    policy = clock.policy()
    _arm(policy)
    app = _app_view(fresh=0.0)
    bot = str(tmp_path)
    dr.save_auto_restart(bot, SESSION, pending_notify=False)
    out = policy.observe_app(app, bot_dir=bot)
    assert isinstance(out, dr.AlertOnly)
    assert out.reason == "already_restarted"


@pytest.mark.parametrize("flag", ["guard", "poller", "retry"])
def test_each_fill_gate_defers_without_consuming_the_budget(tmp_path, flag):
    clock = _Clock()
    policy = clock.policy()
    _arm(policy)
    kwargs = {"fresh": 0.0}
    if flag == "guard":
        kwargs["fill_pending"] = True
    elif flag == "poller":
        kwargs["poller_active"] = True
    else:
        kwargs["retry"] = {"action": "exit"}
    app = _app_view(**kwargs)
    bot = str(tmp_path)

    deferred = policy.observe_app(app, bot_dir=bot)
    assert isinstance(deferred, dr.Defer)
    assert deferred.reason == "fill_pending"
    assert policy.degraded_open_since == NOW - 300
    assert not os.path.exists(dr.auto_restart_path(bot))

    if flag == "guard":
        app._trading_guard.fill_pending = False
    elif flag == "poller":
        app._fill_poller.active = False
    else:
        app._exit_fill_retry_ctx = None
    fired = policy.observe_app(app, bot_dir=bot)
    assert isinstance(fired, dr.RestartDecision)
    assert fired.session_key == SESSION
    assert not os.path.exists(dr.auto_restart_path(bot))


def test_semi_auto_alerts_once_and_never_restarts(tmp_path):
    clock = _Clock()
    policy = clock.policy()
    _arm(policy)
    app = _app_view(mode="semi_auto", fresh=0.0, confirmed=True, size=1)
    bot = str(tmp_path)
    first = policy.observe_app(app, bot_dir=bot)
    assert isinstance(first, dr.AlertOnly)
    assert first.reason == "semi_auto"
    assert first.session_key == SESSION
    assert not isinstance(first, dr.RestartDecision)
    assert not os.path.exists(dr.auto_restart_path(bot))
    assert isinstance(policy.observe_app(app, bot_dir=bot), dr.Noop)
    clock.session = NEXT_SESSION
    policy.degraded_open_since = NOW - 300
    nxt = policy.observe_app(app, bot_dir=bot)
    assert isinstance(nxt, dr.AlertOnly)
    assert nxt.session_key == NEXT_SESSION


def test_restart_after_default_is_five_minutes():
    assert dr.DEFAULT_RESTART_AFTER_S == 300.0
    assert TickWatchdog.NEAR_CLOSE_SUPPRESS == 10


# ── command builder ──────────────────────────────────────────────────


def _argv():
    return [
        "deploy", "--symbol", "TMF00", "--bot", "night1",
        "--mode", "paper", "--new", "--detach", "--wait-ready", "30",
        "--wait-ready=5", "--wait-for-pid", "999", "--wait-for-pid=888",
        "--relogin-stagger", "--strategy", "Bollinger",
    ]


def test_restart_command_uses_child_command_for_source_and_frozen(monkeypatch):
    seen = []
    real = headless.child_command

    def _wrapped(raw, *, frozen, executable, script):
        seen.append(frozen)
        return real(raw, frozen=frozen, executable=executable, script=script)

    monkeypatch.setattr(headless, "child_command", _wrapped)
    source = restart_command(
        _argv(), frozen=False, executable="/usr/bin/python",
        script="/opt/run_bot_cli.py", trading_mode="auto", old_pid=42,
        keep_confirmed_real_position=False,
    )
    frozen = restart_command(
        _argv(), frozen=True, executable="/opt/tai_backtest.exe",
        script="/opt/run_bot_cli.py", trading_mode="paper", old_pid=7,
        keep_confirmed_real_position=False,
    )
    assert seen == [False, True]
    assert source[:3] == ["/usr/bin/python", "/opt/run_bot_cli.py", "deploy"]
    assert frozen[0] == "/opt/tai_backtest.exe"
    assert frozen[1] == "deploy"
    assert "/opt/run_bot_cli.py" not in frozen
    assert not any(part.endswith(".py") for part in frozen)


@pytest.mark.parametrize("mode_tokens", [
    ["--mode", "paper"],
    ["--mode=paper"],
    [],
])
@pytest.mark.parametrize("trading_mode,keep", [
    ("auto", True),
    ("auto", False),
    ("paper", True),
    ("paper", False),
])
def test_restart_command_rewrites_flags(mode_tokens, trading_mode, keep):
    raw = [
        "deploy", "--symbol", "TMF00", "--bot", "night1", *mode_tokens,
        "--new", "--resume", "--detach", "--wait-ready", "12",
        "--wait-for-pid=5", "--relogin-stagger",
    ]
    if keep and trading_mode == "paper":
        raw.append("--allow-existing-position")
    cmd = restart_command(
        raw, frozen=True, executable="tai.exe", script="ignored.py",
        trading_mode=trading_mode, old_pid=4242,
        keep_confirmed_real_position=keep and trading_mode == "auto",
    )
    assert "--new" not in cmd
    assert cmd.count("--resume") == 1
    assert "--detach" not in cmd
    assert "--wait-ready" not in cmd
    assert not any(part.startswith("--wait-ready=") for part in cmd)
    assert cmd.count("--relogin-stagger") == 1
    assert cmd.count("--wait-for-pid") == 1
    pid_at = cmd.index("--wait-for-pid")
    assert cmd[pid_at + 1] == "4242"
    assert "5" not in cmd
    assert "999" not in cmd
    if "--mode" in cmd:
        assert cmd[cmd.index("--mode") + 1] == trading_mode
    else:
        assert f"--mode={trading_mode}" in cmd
    assert cmd.count("--mode") + sum(
        part.startswith("--mode=") for part in cmd) == 1
    wants_allow = keep and trading_mode == "auto"
    assert ("--allow-existing-position" in cmd) is (
        wants_allow or "--allow-existing-position" in raw)
    if wants_allow:
        assert cmd.count("--allow-existing-position") == 1
    assert "--allow-unknown-position" not in cmd


def test_restart_command_does_not_duplicate_resume_or_existing_position():
    raw = ["deploy", "--symbol", "T", "--bot", "b", "--mode=auto",
           "--resume", "--allow-existing-position"]
    cmd = restart_command(
        raw, frozen=False, executable="python", script="run_bot_cli.py",
        trading_mode="auto", old_pid=3,
        keep_confirmed_real_position=True,
    )
    assert cmd.count("--resume") == 1
    assert cmd.count("--allow-existing-position") == 1
    assert "--allow-unknown-position" not in cmd


def test_hidden_restart_flags_parse_and_stay_out_of_help():
    parser = build_parser()
    deploy = parser._subparsers._group_actions[0].choices["deploy"]
    by_dest = {action.dest: action for action in deploy._actions}
    assert by_dest["wait_for_pid"].help is argparse.SUPPRESS
    assert by_dest["wait_for_pid"].type is int
    assert by_dest["wait_for_pid"].default == 0
    assert by_dest["relogin_stagger"].help is argparse.SUPPRESS
    args = parser.parse_args([
        "deploy", "--symbol", "TMF00", "--bot", "b",
        "--wait-for-pid", "15", "--relogin-stagger",
    ])
    assert args.wait_for_pid == 15
    assert args.relogin_stagger is True
    plain = parser.parse_args(["deploy", "--symbol", "TMF00", "--bot", "b"])
    assert plain.wait_for_pid == 0
    assert plain.relogin_stagger is False
    help_text = deploy.format_help()
    assert "--wait-for-pid" not in help_text
    assert "--relogin-stagger" not in help_text


# ── keep-position stop vs unchanged abort ────────────────────────────


def test_keep_position_stop_skips_flatten_and_abort_alert(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "auto")
    app, discord = _app(monkeypatch, runner, "auto")
    app._stop_live(keep_position=True)
    assert app.orders == []
    assert runner.broker.position_size == 1
    saved = load_session(runner.session_path)
    assert saved["broker"]["position_size"] == 1
    assert discord.stopped == []
    assert not any("部署中止" in message or "Deploy aborted" in message
                   for message, _prio in discord.notes)
    assert app.alerts == []


def test_paper_keep_position_stop_does_not_flatten_sim(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "paper")
    app, discord = _app(monkeypatch, runner, "paper", confirmed=False)
    app._stop_live(keep_position=True)
    assert app.orders == []
    assert runner.broker.position_size == 1
    assert load_session(runner.session_path)["broker"]["position_size"] == 1
    assert discord.stopped == []


def test_abort_deploy_still_alerts_and_keeps_the_position(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "auto")
    app, discord = _app(monkeypatch, runner, "auto")
    app._abort_deploy("tick subscribe failed")
    assert app.orders == []
    assert runner.broker.position_size == 1
    assert any("部署中止" in message and "Deploy aborted" in message
               for message, prio in discord.notes if prio)
    assert discord.stopped
    assert app.alerts


def test_plain_stop_still_flattens_a_confirmed_auto_position(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "auto")
    app, discord = _app(monkeypatch, runner, "auto")
    app._stop_live()
    assert len(app.orders) == 1
    assert runner.broker.position_size == 0
    assert discord.stopped


# ── driver handoff ───────────────────────────────────────────────────


def _poll_driver(tmp_path, app, monkeypatch, *, mode="paper", raw=None,
                 size=0, confirmed=False):
    from src.live.headless_app import DeployDriver

    bot = str(tmp_path / "TMF00_night1")
    os.makedirs(bot)
    app._trading_mode = mode
    app._trading_guard.real_entry_confirmed = confirmed
    app._live_runner.broker.position_size = size
    app._link_host = SimpleNamespace(snapshot=lambda: {"degraded": True})
    app._last_fresh_tick_at = 0.0
    app._exit_fill_retry_ctx = None
    app._service_mode_request = lambda _bot: None
    order = []
    real_stop = app._stop_live

    def _stop(*args, **kwargs):
        order.append("stop")
        saved = load_session(dr.auto_restart_path(bot))
        order.append(("marker", None if saved is None else dict(saved)))
        return real_stop(*args, **kwargs)

    app._stop_live = _stop
    discord = _Discord()

    def _starting(**kwargs):
        order.append("discord")
        discord.started.append(kwargs)

    discord.degraded_restart_starting = _starting
    monkeypatch.setattr(rb, "_discord", discord)
    driver = DeployDriver.__new__(DeployDriver)
    driver.app = app
    driver.root = app.root
    driver.bot_dir = bot
    driver.exit_code = None
    driver._stop_flag = False
    driver._stop_via_us = False
    driver._pending_restart_cmd = None
    driver.args = SimpleNamespace(
        heartbeat_min=0,
        symbol="TMF00",
        bot="night1",
        _raw_argv=raw or ["deploy", "--symbol", "TMF00", "--bot", "night1",
                          "--mode", "paper", "--new"],
    )
    driver.finish = lambda code, message="": (
        order.append(("finish", code, message)) or setattr(driver, "exit_code", code))
    clock = _Clock()
    policy = clock.policy()
    _arm(policy)
    driver._restart_policy = policy
    driver._order = order
    driver._discord = discord
    return driver


def test_restart_decision_notifies_before_keep_position_stop(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "auto")
    app, _discord = _app(monkeypatch, runner, "auto")
    driver = _poll_driver(tmp_path, app, monkeypatch, mode="auto",
                          confirmed=True, size=1)
    serviced = []
    app._service_mode_request = lambda _bot: serviced.append("mode")
    driver._poll_once()
    assert driver._order[0] == "discord"
    assert driver._order[1] == "stop"
    assert serviced == []
    assert driver.exit_code == EXIT_OK
    assert driver._order[-1][0] == "finish"
    assert "degraded auto-restart" in driver._order[-1][2]
    marker = load_session(dr.auto_restart_path(driver.bot_dir))
    assert marker["pending_notify"] is True
    assert marker["session_key"] == SESSION
    assert marker["old_pid"] == os.getpid()
    cmd = driver._pending_restart_cmd
    assert "--resume" in cmd and "--new" not in cmd
    assert "--allow-existing-position" in cmd
    assert "--allow-unknown-position" not in cmd
    assert "--relogin-stagger" in cmd
    assert str(os.getpid()) in cmd
    note = driver._discord.started[0]
    assert note["session_key"] == SESSION
    assert note["mode"] == "auto"
    assert note["old_pid"] == os.getpid()
    assert "kept" in note["position_kept"].lower() or "x1" in note["position_kept"]


@pytest.mark.parametrize("mode,confirmed,size,allow", [
    ("paper", True, 1, False),
    ("auto", False, 1, False),
    ("auto", True, 0, False),
    ("auto", True, 1, True),
])
def test_allow_existing_position_only_for_confirmed_real(
        tmp_path, monkeypatch, mode, confirmed, size, allow):
    runner = _runner(tmp_path, "auto" if mode == "auto" else "paper")
    app, _discord = _app(monkeypatch, runner, mode, confirmed=confirmed)
    driver = _poll_driver(tmp_path, app, monkeypatch, mode=mode,
                          confirmed=confirmed, size=size)
    driver._poll_once()
    cmd = driver._pending_restart_cmd
    assert ("--allow-existing-position" in cmd) is allow
    assert "--allow-unknown-position" not in cmd
    assert f"--mode" in cmd or any(part.startswith("--mode=") for part in cmd)


def test_alert_only_sends_one_discord_and_does_not_stop(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "auto")
    app, _discord = _app(monkeypatch, runner, "semi_auto", confirmed=False)
    driver = _poll_driver(tmp_path, app, monkeypatch, mode="semi_auto")
    driver._poll_once()
    driver._poll_once()
    assert driver._discord.skipped
    assert len(driver._discord.skipped) == 1
    assert driver._discord.skipped[0]["reason"] == "semi_auto"
    assert driver._discord.skipped[0]["mode"] == "semi_auto"
    assert "stop" not in driver._order
    assert driver._pending_restart_cmd is None
    assert driver.exit_code is None
    assert not os.path.exists(dr.auto_restart_path(driver.bot_dir))

    dr.save_auto_restart(driver.bot_dir, SESSION)
    app._trading_mode = "paper"
    driver._restart_policy._alerted.clear()
    driver._poll_once()
    driver._poll_once()
    reasons = [note["reason"] for note in driver._discord.skipped]
    assert reasons.count("already_restarted") == 1
    assert driver._pending_restart_cmd is None


# ── run_deploy lifecycle ─────────────────────────────────────────────


def _patch_project(monkeypatch, tmp_path):
    monkeypatch.setattr(rb, "project_root", str(tmp_path))


def test_spawn_runs_only_after_teardown_returns(tmp_path, monkeypatch):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    order = []
    args, bot_dir = _deploy_args(tmp_path)
    app = SimpleNamespace(
        _release_relogin_slot=None, root=SimpleNamespace())

    def _boot(*_a, **_k):
        order.append("boot")
        return app, EXIT_OK

    def _drive(driver, _app):
        order.append("drive")
        driver.bot_dir = bot_dir
        driver._pending_restart_cmd = ["tai", "deploy", "--resume"]
        return EXIT_OK

    def _teardown(_app):
        order.append("teardown")
        order.append("teardown-return")

    def _spawn(cmd, stdout_path, cwd=None):
        order.append("spawn")
        assert order[-2] == "teardown-return"
        spawned["cmd"] = cmd
        spawned["stdout"] = stdout_path
        spawned["cwd"] = cwd
        return SimpleNamespace(pid=99)

    spawned = {}
    monkeypatch.setattr(ha, "_boot", _boot)
    monkeypatch.setattr(ha, "_drive_until_exit", _drive)
    monkeypatch.setattr(ha, "_teardown", _teardown)
    monkeypatch.setattr(ha, "spawn_detached", _spawn)
    code = run_deploy(args)
    assert code == EXIT_OK
    assert order.index("teardown-return") < order.index("spawn")
    assert spawned["cmd"] == ["tai", "deploy", "--resume"]
    assert spawned["stdout"].endswith(os.path.join("TMF00_night1", "cli_stdout.log"))
    from src.paths import project_root
    assert spawned["cwd"] == project_root()


def test_spawn_oserror_alerts_and_returns_nonzero(tmp_path, monkeypatch):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    args, bot_dir = _deploy_args(tmp_path)
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)

    def _boot(*_a, **_k):
        return SimpleNamespace(root=SimpleNamespace()), EXIT_OK

    def _drive(driver, _app):
        driver.bot_dir = bot_dir
        driver._pending_restart_cmd = ["tai", "deploy", "--resume"]
        return EXIT_OK

    def _spawn(*_a, **_k):
        raise OSError("spawn failed")

    monkeypatch.setattr(ha, "_boot", _boot)
    monkeypatch.setattr(ha, "_drive_until_exit", _drive)
    monkeypatch.setattr(ha, "_teardown", lambda _app: None)
    monkeypatch.setattr(ha, "spawn_detached", _spawn)
    code = run_deploy(args)
    assert code not in (0, None)
    assert discord.failed
    assert discord.failed[0][1].get("detail") or discord.failed[0][0]


def test_wait_for_pid_polls_alive_and_times_out_at_60s(monkeypatch):
    clock = {"t": 0.0}
    pids = []

    def _alive(pid):
        pids.append(pid)
        return True

    def _mono():
        return clock["t"]

    def _sleep(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(LiveRunner, "_pid_alive", staticmethod(_alive))
    ok = dr.wait_for_old_pid(
        321, timeout_s=dr.PID_WAIT_S, poll_s=1.0, sleep=_sleep, monotonic=_mono)
    assert ok is False
    assert dr.PID_WAIT_S == 60.0
    assert pids and set(pids) == {321}
    assert clock["t"] >= 60.0


def test_wait_for_pid_returns_when_the_process_exits(monkeypatch):
    state = {"alive": True}

    def _alive(_pid):
        return state["alive"]

    monkeypatch.setattr(LiveRunner, "_pid_alive", staticmethod(_alive))
    state["alive"] = False
    assert dr.wait_for_old_pid(8, timeout_s=60, poll_s=5, sleep=lambda _s: None)


def test_run_deploy_aborts_before_login_when_old_pid_stays_alive(
        tmp_path, monkeypatch):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    args, _bot = _deploy_args(tmp_path, wait_for_pid=555)
    booted = []
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(dr, "PID_WAIT_S", 0.0)
    monkeypatch.setattr(dr, "DEFAULT_RELOGIN_WAIT_S", 0.0)
    monkeypatch.setattr(LiveRunner, "_pid_alive", staticmethod(lambda _pid: True))
    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: booted.append("boot"))
    code = run_deploy(args)
    assert code not in (0, None)
    assert booted == []
    assert discord.failed


def test_relogin_slot_serializes_and_times_out(tmp_path):
    bot = str(tmp_path / "TMF00_a")
    os.makedirs(bot)
    assert dr.DEFAULT_RELOGIN_WAIT_S == 13 * 45
    slot = dr.relogin_slot_path(bot)
    assert slot == os.path.join(os.path.dirname(os.path.abspath(bot)),
                                ".relogin.slot")
    held = threading.Event()
    release = threading.Event()
    errors = []

    def _holder():
        try:
            assert dr.acquire_relogin_slot(bot, wait_s=2, poll_s=0.01)
            held.set()
            assert release.wait(2)
            dr.release_relogin_slot(bot)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=_holder)
    thread.start()
    assert held.wait(2)
    assert dr.acquire_relogin_slot(bot, wait_s=0.05, poll_s=0.01) is False
    release.set()
    thread.join(2)
    assert not errors, errors
    assert dr.acquire_relogin_slot(bot, wait_s=1, poll_s=0.01) is True
    dr.release_relogin_slot(bot)
    dr.release_relogin_slot(bot)


def test_relogin_stagger_is_held_across_login_and_released(
        tmp_path, monkeypatch):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    args, bot_dir = _deploy_args(tmp_path, relogin_stagger=True)
    os.makedirs(bot_dir)
    seen = []
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)

    def _boot(*_a, **_k):
        seen.append("boot")
        assert dr.acquire_relogin_slot(bot_dir, wait_s=0.05, poll_s=0.01) is False
        return SimpleNamespace(
            _release_relogin_slot=None, root=SimpleNamespace()), EXIT_OK

    def _drive(driver, app):
        seen.append("drive")
        assert callable(app._release_relogin_slot)
        app._release_relogin_slot()
        assert dr.acquire_relogin_slot(bot_dir, wait_s=0.5, poll_s=0.01) is True
        dr.release_relogin_slot(bot_dir)
        driver.bot_dir = bot_dir
        driver._pending_restart_cmd = None
        return EXIT_OK

    monkeypatch.setattr(ha, "_boot", _boot)
    monkeypatch.setattr(ha, "_drive_until_exit", _drive)
    monkeypatch.setattr(ha, "_teardown", lambda _app: seen.append("teardown"))
    code = run_deploy(args)
    assert code == EXIT_OK
    assert seen == ["boot", "drive", "teardown"]
    assert dr.acquire_relogin_slot(bot_dir, wait_s=0.5, poll_s=0.01) is True
    dr.release_relogin_slot(bot_dir)


def test_relogin_timeout_alerts_and_does_not_log_in(tmp_path, monkeypatch):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    args, bot_dir = _deploy_args(tmp_path, relogin_stagger=True)
    os.makedirs(bot_dir)
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    booted = []
    assert dr.acquire_relogin_slot(bot_dir, wait_s=1, poll_s=0.01)
    monkeypatch.setattr(dr, "PID_WAIT_S", 0.0)
    monkeypatch.setattr(dr, "DEFAULT_RELOGIN_WAIT_S", 0.0)
    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: booted.append(1))
    try:
        code = run_deploy(args)
    finally:
        dr.release_relogin_slot(bot_dir)
    assert code not in (0, None)
    assert booted == []
    assert discord.failed


def test_plain_deploy_does_not_touch_the_relogin_slot(tmp_path, monkeypatch):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    args, bot_dir = _deploy_args(tmp_path)
    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: (None, 3))
    code = run_deploy(args)
    assert code == 3
    slot = dr.relogin_slot_path(bot_dir)
    assert slot is not None
    assert not os.path.exists(slot)


def test_tick_subscription_notifies_once_and_keeps_the_session_key(
        tmp_path, monkeypatch):
    runner = _runner(tmp_path, "auto")
    app, discord = _app(monkeypatch, runner, "auto")
    discord.recovered = []

    def _recovered(**kwargs):
        discord.recovered.append(kwargs)

    discord.degraded_restart_recovered = _recovered
    released = []
    app._release_relogin_slot = lambda: released.append("slot")
    dr.save_auto_restart(runner.bot_dir, SESSION, old_pid=9, pending_notify=True)
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "_broker", SimpleNamespace(request_ticks=lambda *_a: 0))
    app._quote_connected = True
    app._quote_intermediate_seen_at = 0.0
    app._drain_tick_queue = lambda: None
    app._schedule_status_update = lambda: None

    app._start_live_tick_subscription()

    assert released == ["slot"]
    assert len(discord.recovered) == 1
    note = discord.recovered[0]
    assert note["session_key"] == SESSION
    assert note["mode"] == "auto"
    assert note["strategy"]
    saved = load_session(dr.auto_restart_path(runner.bot_dir))
    assert saved["pending_notify"] is False
    assert saved["session_key"] == SESSION
    assert saved["old_pid"] == 9

    app._release_relogin_slot = lambda: released.append("again")
    app._notify_degraded_restart_recovered(runner.symbol)
    assert len(discord.recovered) == 1
    assert load_session(dr.auto_restart_path(runner.bot_dir))["session_key"] == SESSION
    assert released == ["slot"]

    clock = _Clock()
    policy = clock.policy()
    _arm(policy)
    view = _app_view(fresh=0.0)
    blocked = policy.observe_app(view, bot_dir=runner.bot_dir)
    assert isinstance(blocked, dr.AlertOnly)
    assert blocked.reason == "already_restarted"


def test_discord_methods_priority_and_wording():
    from src.live.discord_notify import DiscordNotifier

    sent = []

    def _send(content, channel_id="", *, priority=False):
        sent.append((content, priority))

    note = DiscordNotifier("token", "123", bot_name="night1", symbol="TMF00")
    note._send = _send
    note.degraded_restart_starting(
        bot="night1", session_key=SESSION, mode="auto",
        position_kept="SHORT x1 kept", old_pid=77, strategy="ShortOnly",
    )
    note.degraded_restart_recovered(
        strategy="ShortOnly", position="SHORT x1", mode="auto",
        session_key=SESSION,
    )
    note.degraded_restart_skipped(
        bot="night1", session_key=SESSION, mode="semi_auto", reason="semi_auto",
    )
    note.degraded_restart_failed("spawn failed", detail="OSError", bot="night1")
    assert sent[0][1] is True
    assert SESSION in sent[0][0] and "auto" in sent[0][0]
    assert "77" in sent[0][0] and "SHORT x1" in sent[0][0]
    assert sent[1][1] is False
    assert "ShortOnly" in sent[1][0] and "SHORT x1" in sent[1][0]
    assert SESSION in sent[1][0] and "auto" in sent[1][0]
    assert "semi_auto" in sent[2][0] and SESSION in sent[2][0]
    assert sent[3][1] is True
    assert "spawn failed" in sent[3][0] and "OSError" in sent[3][0]


def _plant_pending(bot_dir: str) -> None:
    os.makedirs(bot_dir, exist_ok=True)
    dr.save_auto_restart(bot_dir, SESSION, old_pid=77, pending_notify=True)


def _pending(bot_dir: str) -> dict:
    return load_session(dr.auto_restart_path(bot_dir))


def test_boot_failure_alerts_and_clears_pending_notify(tmp_path, monkeypatch):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    args, bot_dir = _deploy_args(tmp_path, wait_for_pid=41)
    _plant_pending(bot_dir)
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(LiveRunner, "_pid_alive", staticmethod(lambda _pid: False))
    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: (None, EXIT_NO_COM))
    code = run_deploy(args)
    assert code == EXIT_NO_COM
    assert discord.failed[0][0] == "replacement deploy failed"
    assert discord.failed[0][1]["detail"] == f"exit {EXIT_NO_COM}"
    assert discord.failed[0][1]["bot"] == "night1"
    saved = _pending(bot_dir)
    assert saved["pending_notify"] is False
    assert saved["session_key"] == SESSION


@pytest.mark.parametrize("exit_code", [EXIT_LOGIN, EXIT_DEPLOY_REFUSED])
def test_child_deploy_failure_alerts_and_clears_pending_notify(
        tmp_path, monkeypatch, exit_code):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    args, bot_dir = _deploy_args(tmp_path, wait_for_pid=42)
    _plant_pending(bot_dir)
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(LiveRunner, "_pid_alive", staticmethod(lambda _pid: False))
    app = SimpleNamespace(root=SimpleNamespace(), _release_relogin_slot=None)
    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: (app, EXIT_OK))
    monkeypatch.setattr(ha, "_drive_until_exit", lambda *_a, **_k: exit_code)
    monkeypatch.setattr(ha, "_teardown", lambda *_a, **_k: None)
    code = run_deploy(args)
    assert code == exit_code
    assert discord.failed[0][0] == "replacement deploy failed"
    assert discord.failed[0][1]["detail"] == f"exit {exit_code}"
    saved = _pending(bot_dir)
    assert saved["pending_notify"] is False
    assert saved["session_key"] == SESSION


@pytest.mark.parametrize("where", ["boot", "drive"])
def test_uncaught_replacement_error_alerts_clears_and_releases_the_slot(
        tmp_path, monkeypatch, where):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    args, bot_dir = _deploy_args(tmp_path, wait_for_pid=43, relogin_stagger=True)
    _plant_pending(bot_dir)
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(LiveRunner, "_pid_alive", staticmethod(lambda _pid: False))
    app = SimpleNamespace(root=SimpleNamespace(), _release_relogin_slot=None)

    def _boot(*_a, **_k):
        if where == "boot":
            raise RuntimeError("boot blew up")
        return app, EXIT_OK

    def _drive(*_a, **_k):
        raise RuntimeError("drive blew up")

    monkeypatch.setattr(ha, "_boot", _boot)
    monkeypatch.setattr(ha, "_drive_until_exit", _drive)
    monkeypatch.setattr(ha, "_teardown", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="blew up"):
        run_deploy(args)
    assert discord.failed[0][0] == "replacement deploy failed"
    assert "RuntimeError" in discord.failed[0][1]["detail"]
    saved = _pending(bot_dir)
    assert saved["pending_notify"] is False
    assert saved["session_key"] == SESSION
    assert dr.acquire_relogin_slot(bot_dir, wait_s=0.5, poll_s=0.01) is True
    dr.release_relogin_slot(bot_dir)


def test_recovered_replacement_that_later_stops_does_not_alert(
        tmp_path, monkeypatch):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    args, bot_dir = _deploy_args(tmp_path, wait_for_pid=44)
    _plant_pending(bot_dir)
    dr.clear_pending_notify(bot_dir)
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(LiveRunner, "_pid_alive", staticmethod(lambda _pid: False))
    app = SimpleNamespace(root=SimpleNamespace(), _release_relogin_slot=None)
    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: (app, EXIT_OK))
    monkeypatch.setattr(ha, "_drive_until_exit", lambda *_a, **_k: EXIT_BOT_STOPPED)
    monkeypatch.setattr(ha, "_teardown", lambda *_a, **_k: None)
    code = run_deploy(args)
    assert code == EXIT_BOT_STOPPED
    assert discord.failed == []
    saved = _pending(bot_dir)
    assert saved["pending_notify"] is False
    assert saved["session_key"] == SESSION


def test_normal_deploy_does_not_emit_a_replacement_failure(tmp_path, monkeypatch):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    args, bot_dir = _deploy_args(tmp_path)
    _plant_pending(bot_dir)
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: (None, EXIT_LOGIN))
    code = run_deploy(args)
    assert code == EXIT_LOGIN
    assert discord.failed == []
    assert _pending(bot_dir)["pending_notify"] is True


def test_early_handoff_failures_clear_pending_notify(tmp_path, monkeypatch):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(dr, "PID_WAIT_S", 0.0)
    monkeypatch.setattr(dr, "DEFAULT_RELOGIN_WAIT_S", 0.0)
    monkeypatch.setattr(LiveRunner, "_pid_alive", staticmethod(lambda _pid: True))

    args, bot_dir = _deploy_args(tmp_path, wait_for_pid=55)
    _plant_pending(bot_dir)
    code = run_deploy(args)
    assert code != EXIT_OK
    assert discord.failed[0][0] == "old process still alive"
    assert _pending(bot_dir)["pending_notify"] is False
    assert _pending(bot_dir)["session_key"] == SESSION

    discord.failed.clear()
    args, bot_dir = _deploy_args(tmp_path, relogin_stagger=True, wait_for_pid=0)
    _plant_pending(bot_dir)
    assert dr.acquire_relogin_slot(bot_dir, wait_s=1, poll_s=0.01)
    try:
        code = run_deploy(args)
    finally:
        dr.release_relogin_slot(bot_dir)
    assert code != EXIT_OK
    assert discord.failed[-1][0] == "relogin stagger timed out"
    assert _pending(bot_dir)["pending_notify"] is False

    discord.failed.clear()
    args, bot_dir = _deploy_args(tmp_path)
    _plant_pending(bot_dir)
    app = SimpleNamespace(root=SimpleNamespace())

    def _drive(driver, _app):
        driver.bot_dir = bot_dir
        driver._pending_restart_cmd = ["tai", "deploy", "--resume"]
        return EXIT_OK

    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: (app, EXIT_OK))
    monkeypatch.setattr(ha, "_drive_until_exit", _drive)
    monkeypatch.setattr(ha, "_teardown", lambda *_a, **_k: None)
    monkeypatch.setattr(ha, "spawn_detached", lambda *_a, **_k: (_ for _ in ()).throw(OSError("spawn failed")))
    code = run_deploy(args)
    assert code != EXIT_OK
    assert discord.failed[-1][0] == "spawn failed"
    assert _pending(bot_dir)["pending_notify"] is False
    assert _pending(bot_dir)["session_key"] == SESSION


def test_src_live_does_not_import_src_evolution():
    import ast

    root = os.path.join(os.path.dirname(__file__), "..", "src", "live")
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                assert not any("evolution" in module for module in modules), path


def _settings_with_discord():
    return {"discord_bot_token": "tok", "discord_channel_id": "999"}


def _slow_deliver(box, delay_s=0.08):
    def _deliver(self, content, target):
        time.sleep(delay_s)
        box.append((time.monotonic(), content))
        return True
    return _deliver


def test_missing_discord_still_delivers_before_run_deploy_returns(
        tmp_path, monkeypatch):
    """rb._discord is None until deploy. The alert must still leave the process."""
    import src.live.headless_app as ha
    from src.live.discord_notify import DiscordNotifier

    _patch_project(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(rb, "_discord", None)
    monkeypatch.setattr(rb, "_load_settings", _settings_with_discord)
    monkeypatch.setattr(DiscordNotifier, "_deliver", _slow_deliver(sent))
    monkeypatch.setattr(LiveRunner, "_pid_alive", staticmethod(lambda _pid: False))
    app = SimpleNamespace(root=SimpleNamespace(), _release_relogin_slot=None)
    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: (app, EXIT_OK))
    monkeypatch.setattr(ha, "_teardown", lambda *_a, **_k: None)

    def _run(exit_code):
        sent.clear()
        args, bot_dir = _deploy_args(tmp_path, wait_for_pid=81)
        _plant_pending(bot_dir)
        monkeypatch.setattr(
            ha, "_drive_until_exit", lambda *_a, **_k: exit_code)
        code = run_deploy(args)
        finished = time.monotonic()
        assert code == exit_code
        assert sent, "alert was not delivered before run_deploy returned"
        assert sent[0][0] <= finished
        assert "replacement deploy failed" in sent[0][1]
        assert _pending(bot_dir)["pending_notify"] is False
        assert _pending(bot_dir)["session_key"] == SESSION

    _run(EXIT_DEPLOY_REFUSED)
    _run(EXIT_LOGIN)


def test_pid_and_slot_timeouts_deliver_with_no_installed_notifier(
        tmp_path, monkeypatch, capsys):
    import src.live.headless_app as ha
    from src.live.discord_notify import DiscordNotifier

    _patch_project(monkeypatch, tmp_path)
    sent = []
    timeouts = []
    orig_flush = DiscordNotifier.flush

    def _flush(self, timeout_s=5.0):
        timeouts.append(timeout_s)
        return orig_flush(self, timeout_s)

    monkeypatch.setattr(rb, "_discord", None)
    monkeypatch.setattr(rb, "_load_settings", _settings_with_discord)
    monkeypatch.setattr(DiscordNotifier, "_deliver", _slow_deliver(sent))
    monkeypatch.setattr(DiscordNotifier, "flush", _flush)
    monkeypatch.setattr(dr, "PID_WAIT_S", 0.0)
    monkeypatch.setattr(dr, "DEFAULT_RELOGIN_WAIT_S", 0.0)
    monkeypatch.setattr(LiveRunner, "_pid_alive", staticmethod(lambda _pid: True))

    args, bot_dir = _deploy_args(tmp_path, wait_for_pid=82)
    _plant_pending(bot_dir)
    code = run_deploy(args)
    finished = time.monotonic()
    assert code != EXIT_OK
    assert sent and sent[-1][0] <= finished
    assert "old process still alive" in sent[-1][1]
    assert _pending(bot_dir)["pending_notify"] is False
    assert "discord flush True" in capsys.readouterr().out
    from src.live.discord_notify import FLUSH_EXIT_TIMEOUT_S
    assert timeouts == [FLUSH_EXIT_TIMEOUT_S]

    sent.clear()
    args, bot_dir = _deploy_args(tmp_path, relogin_stagger=True, wait_for_pid=0)
    _plant_pending(bot_dir)
    assert dr.acquire_relogin_slot(bot_dir, wait_s=1, poll_s=0.01)
    try:
        code = run_deploy(args)
    finally:
        dr.release_relogin_slot(bot_dir)
    finished = time.monotonic()
    assert code != EXIT_OK
    assert sent and sent[-1][0] <= finished
    assert "relogin stagger timed out" in sent[-1][1]
    assert _pending(bot_dir)["pending_notify"] is False
    assert _pending(bot_dir)["session_key"] == SESSION
    assert "discord flush True" in capsys.readouterr().out
    assert timeouts == [FLUSH_EXIT_TIMEOUT_S, FLUSH_EXIT_TIMEOUT_S]


def test_empty_discord_settings_log_and_still_clear_pending(tmp_path, monkeypatch, capsys):
    import src.live.headless_app as ha
    from src.live.discord_notify import DiscordNotifier

    def _boom(self, content, target):
        raise AssertionError("empty settings must not send")

    _patch_project(monkeypatch, tmp_path)
    monkeypatch.setattr(rb, "_discord", None)
    monkeypatch.setattr(rb, "_load_settings", lambda: {
        "discord_bot_token": "", "discord_channel_id": "",
    })
    monkeypatch.setattr(DiscordNotifier, "_deliver", _boom)
    monkeypatch.setattr(LiveRunner, "_pid_alive", staticmethod(lambda _pid: False))
    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: (None, EXIT_DEPLOY_REFUSED))
    args, bot_dir = _deploy_args(tmp_path, wait_for_pid=83)
    _plant_pending(bot_dir)
    code = run_deploy(args)
    assert code == EXIT_DEPLOY_REFUSED
    assert _pending(bot_dir)["pending_notify"] is False
    assert _pending(bot_dir)["session_key"] == SESSION
    assert "replacement deploy failed" in capsys.readouterr().out


def test_flush_waits_for_a_priority_send_and_times_out_when_it_blocks():
    from src.live.discord_notify import DiscordNotifier

    delivered = threading.Event()

    def _deliver(content, target):
        time.sleep(0.12)
        delivered.set()
        return True

    note = DiscordNotifier("tok", "999", bot_name="night1", symbol="TMF00")
    note._deliver = _deliver
    note.degraded_restart_failed("replacement deploy failed", detail="exit 4", bot="night1")
    assert note.flush(timeout_s=2.0) is True
    assert delivered.is_set()

    def _block(content, target):
        time.sleep(2.0)
        return True

    stuck = DiscordNotifier("tok", "999")
    stuck._deliver = _block
    stuck.degraded_restart_failed("stuck", detail="x", bot="b")
    started = time.monotonic()
    assert stuck.flush(timeout_s=0.15) is False
    assert time.monotonic() - started < 1.0


def test_emit_restart_failed_finishes_the_send_before_the_process_exits(tmp_path):
    marker = tmp_path / "delivered"
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    script = (
        "import time\n"
        "import run_backtest as rb\n"
        "from src.live.discord_notify import DiscordNotifier\n"
        "import src.live.headless_app as ha\n"
        "def _deliver(self, content, target):\n"
        f"    time.sleep(0.15)\n"
        f"    open({str(marker)!r}, 'w', encoding='utf-8').write('ok')\n"
        "    return True\n"
        "DiscordNotifier._deliver = _deliver\n"
        "rb._discord = None\n"
        "rb._load_settings = lambda: "
        "{'discord_bot_token': 't', 'discord_channel_id': '1'}\n"
        "ha._emit_restart_failed('replacement deploy failed', "
        "detail='exit 4', bot='night1', symbol='TMF00')\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo, env=env, timeout=20, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert marker.is_file(), proc.stderr
    assert "discord flush True" in proc.stdout, proc.stdout
    assert "may have been cut off" not in proc.stdout


def _com_globals():
    return (
        list(rb._com_event_handlers),
        (rb.skC, rb.skQ, rb.skR, rb.skO),
        rb._com_released, rb._exit_dump_skip_logged,
        rb._comtypes_atexit_wrapped, rb._faulthandler_log,
    )


def _restore_com_globals(saved) -> None:
    handlers, sk, released, skip, wrapped, handle = saved
    rb._com_event_handlers[:] = handlers
    rb.skC, rb.skQ, rb.skR, rb.skO = sk
    rb._com_released = released
    rb._exit_dump_skip_logged = skip
    rb._comtypes_atexit_wrapped = wrapped
    rb._faulthandler_log = handle


class _FlushNote:
    enabled = True

    def __init__(self):
        self.timeouts = []

    def degraded_restart_failed(self, reason, **kwargs):
        self.reason = reason

    def flush(self, timeout_s=5.0):
        self.timeouts.append(timeout_s)
        return False


def test_flush_false_is_logged_and_restart_failed_uses_the_long_cap(
        monkeypatch, capsys):
    import src.live.headless_app as ha
    from src.live.discord_notify import FLUSH_EXIT_TIMEOUT_S

    note = _FlushNote()
    monkeypatch.setattr(rb, "_discord", note)
    ha._emit_restart_failed(
        "old process still alive", detail="pid 1", bot="night1", symbol="TMF00")
    out = capsys.readouterr().out
    assert "discord flush False" in out and "may have been cut off" in out
    assert note.timeouts == [FLUSH_EXIT_TIMEOUT_S]


def test_spawn_runs_before_the_only_flush_and_that_flush_is_five_seconds(
        tmp_path, monkeypatch):
    """A stalling flush must not run until the replacement has been spawned."""
    import threading

    import src.live.headless_app as ha

    order = []
    entered = threading.Event()
    release = threading.Event()

    def _spawn(*_a, **_k):
        order.append("spawn")
        return SimpleNamespace(pid=4242)

    def _flush(timeout_s=5.0):
        order.append(("enter", timeout_s))
        entered.set()
        release.wait(2)
        order.append(("flush", timeout_s))
        return True

    note = _FlushNote()
    note.flush = _flush
    monkeypatch.setattr(rb, "_discord", note)
    monkeypatch.setattr(ha, "spawn_detached", _spawn)
    driver = SimpleNamespace(
        _pending_restart_cmd=["tai", "deploy"], bot_dir=str(tmp_path),
        args=SimpleNamespace(symbol="TMF00"))
    box = {}

    def _run():
        try:
            box["code"] = ha._spawn_pending_restart(driver, 0)
        except Exception as exc:
            box["err"] = exc

    thread = threading.Thread(target=_run)
    thread.start()
    try:
        assert entered.wait(2)
        assert order == ["spawn", ("enter", 5.0)]
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert box.get("err") is None and box.get("code") == 0
    assert [item for item in order if item[0] == "flush"] == [("flush", 5.0)]


def test_discord_warning_reaches_stdout_before_pid_wait(
        tmp_path, monkeypatch, capsys):
    import logging
    import src.live.headless_app as ha

    log = logging.getLogger("src.live.discord_notify")
    saved = (log.level, list(log.handlers))
    seen = {}

    def _wait(pid, timeout_s=None):
        seen["before"] = capsys.readouterr().out
        log.warning("discord-before-boot")
        return False

    _patch_project(monkeypatch, tmp_path)
    monkeypatch.setattr(dr, "wait_for_old_pid", _wait)
    monkeypatch.setattr(rb, "_discord", None)
    monkeypatch.setattr(rb, "_load_settings", lambda: {})
    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("boot")))
    try:
        code = run_deploy(_deploy_args(tmp_path, wait_for_pid=5151)[0])
    finally:
        log.setLevel(saved[0])
        log.handlers[:] = saved[1]
    assert code != EXIT_OK
    assert "pid-wait start old=5151" in seen["before"]
    assert "[src.live.discord_notify] discord-before-boot" in capsys.readouterr().out


def test_spawn_logs_the_replacement_pid_and_oserror_does_not(
        tmp_path, monkeypatch, capsys):
    import src.live.headless_app as ha

    _patch_project(monkeypatch, tmp_path)
    monkeypatch.setattr(rb, "_discord", None)
    args, bot_dir = _deploy_args(tmp_path)
    app = SimpleNamespace(root=SimpleNamespace(), _release_relogin_slot=None)

    def _drive(driver, _app):
        driver.bot_dir = bot_dir
        driver._pending_restart_cmd = ["tai", "deploy", "--resume"]
        return EXIT_OK

    monkeypatch.setattr(ha, "_boot", lambda *_a, **_k: (app, EXIT_OK))
    monkeypatch.setattr(ha, "_drive_until_exit", _drive)
    monkeypatch.setattr(ha, "_teardown", lambda *_a: None)
    monkeypatch.setattr(
        ha, "spawn_detached", lambda *_a, **_k: SimpleNamespace(pid=4242))
    assert run_deploy(args) == EXIT_OK
    assert "spawned replacement pid=4242" in capsys.readouterr().out
    monkeypatch.setattr(rb, "_discord", _Discord())
    monkeypatch.setattr(
        ha, "spawn_detached",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("spawn failed")))
    assert run_deploy(args) == EXIT_BOT_STOPPED
    assert "spawned replacement pid=" not in capsys.readouterr().out


def test_root_destroy_is_logged_on_headless_and_gui_close(capsys):
    import src.live.headless_app as ha

    def _boom():
        raise RuntimeError("destroy failed")

    ha._teardown(SimpleNamespace(
        root=SimpleNamespace(destroy=_boom), _shutdown_link=lambda: None))
    assert "root.destroy done" in capsys.readouterr().out
    gui = rb.BacktestApp.__new__(rb.BacktestApp)
    gui._auto_save_chat = lambda: None
    gui._shutdown_link = lambda: None
    gui.root = SimpleNamespace(destroy=_boom)
    gui._on_closing()
    assert "root.destroy done" in capsys.readouterr().out


def test_release_com_is_idempotent_and_shutdown_without_a_host_releases(capsys):
    from run_backtest import release_com_connections

    saved = _com_globals()
    calls = []

    class _Ok:
        def disconnect(self):
            calls.append("ok")

    class _Bad:
        def disconnect(self):
            calls.append("bad")
            raise RuntimeError("unadvise failed")

    try:
        rb._com_released = False
        rb._faulthandler_log = None
        rb._com_event_handlers[:] = [_Ok(), _Bad(), object()]
        rb.skC = rb.skQ = rb.skR = rb.skO = "live"
        release_com_connections()
        out = capsys.readouterr().out
        assert calls == ["ok", "bad"]
        assert rb._com_event_handlers == []
        assert rb.skC is rb.skQ is rb.skR is rb.skO is None
        assert "COM disconnect ok=1" in out
        release_com_connections()
        assert calls == ["ok", "bad"]
        assert "COM disconnect ok=" not in capsys.readouterr().out
        text = open(os.path.join(os.path.dirname(__file__), "..", "run_backtest.py"),
                    encoding="utf-8").read()
        body = text[text.index("    def _stop_live("):text.index("\n# Event sinks")]
        assert "release_com_connections" not in body
        assert "_shutdown_link(" not in body

        calls.clear()
        rb._com_released = False
        rb._com_event_handlers[:] = [_Ok()]
        rb.skC = "live"
        app = rb.BacktestApp.__new__(rb.BacktestApp)
        app._link_host = None
        app._cancel_reply_poll = lambda: None
        app._shutdown_link()
        assert calls == ["ok"] and rb.skC is None
        assert "COM disconnect ok=1" in capsys.readouterr().out
    finally:
        _restore_com_globals(saved)


def test_shutdown_arms_dump_and_return_from_main_does_not_cancel(
        tmp_path, monkeypatch, capsys):
    import faulthandler
    from run_backtest import note_return_from_main

    saved = _com_globals()
    handle = open(tmp_path / "faulthandler.log", "a", encoding="utf-8")
    order, dumps = [], []

    def _dump(timeout, exit=False, file=None):
        dumps.append((timeout, exit, file is handle))
        order.append("dump")

    monkeypatch.setattr(faulthandler, "dump_traceback_later", _dump)
    monkeypatch.setattr(faulthandler, "cancel_dump_traceback_later",
                        lambda: order.append("cancel"))

    class _Host:
        def shutdown(self, now):
            order.append("leave")

    class _Conn:
        def disconnect(self):
            order.append("disc")

    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._cancel_reply_poll = lambda: None
    try:
        rb._faulthandler_log = handle
        rb._exit_dump_skip_logged = False
        rb._com_released = False
        rb._com_event_handlers[:] = [_Conn()]
        app._link_host = _Host()
        app._shutdown_link()
        assert order == ["leave", "dump", "disc"]
        assert dumps == [(30, False, True)] and "cancel" not in order
        note_return_from_main()
        assert "cancel" not in order
        assert "return from main" in capsys.readouterr().out
        order.clear()
        dumps.clear()
        rb._faulthandler_log = None
        rb._exit_dump_skip_logged = False
        rb._com_released = True
        app._link_host = _Host()
        app._shutdown_link()
        assert dumps == [] and "leave" in order
        closed = open(tmp_path / "closed.log", "a", encoding="utf-8")
        closed.close()
        rb._faulthandler_log = closed
        app._shutdown_link()
        assert dumps == []
    finally:
        _restore_com_globals(saved)
        handle.close()


def test_atexit_brackets_comtypes_shutdown(monkeypatch, capsys, tmp_path):
    import atexit
    import faulthandler
    import logging
    import sys
    import types
    from run_backtest import arm_process_exit_diagnostics

    saved = _com_globals()
    rb._faulthandler_log = None
    rb._comtypes_atexit_wrapped = False
    monkeypatch.delitem(sys.modules, "comtypes", raising=False)
    kept = []
    try:
        arm_process_exit_diagnostics()
        assert "comtypes not loaded" in capsys.readouterr().out
        called = []

        def original():
            called.append(1)
            if len(called) > 1:
                raise RuntimeError("co")

        mod = types.ModuleType("comtypes")
        mod._shutdown = original
        monkeypatch.setitem(sys.modules, "comtypes", mod)
        atexit.register(original)
        seen = []
        real_reg, real_unreg = atexit.register, atexit.unregister
        monkeypatch.setattr(
            atexit, "register",
            lambda fn, *a, **k: seen.append(fn) or real_reg(fn, *a, **k))
        monkeypatch.setattr(
            atexit, "unregister",
            lambda fn, *a, **k: seen.append(("u", fn)) or real_unreg(fn, *a, **k))
        rb._comtypes_atexit_wrapped = False
        arm_process_exit_diagnostics()
        assert ("u", original) in seen
        wrappers = [fn for fn in seen if not isinstance(fn, tuple)]
        assert wrappers and wrappers[0] is not original
        kept.extend(wrappers)
        logging.getLogger("comtypes._post_coinit.unknwn").debug(
            "Calling CoUninitialize()")
        assert "Calling CoUninitialize()" in capsys.readouterr().out
        handle = open(tmp_path / "fh.log", "a", encoding="utf-8")
        rb._faulthandler_log = handle
        rb._exit_dump_skip_logged = False
        dumps = []
        monkeypatch.setattr(
            faulthandler, "dump_traceback_later",
            lambda timeout, exit=False, file=None: dumps.append(
                (timeout, exit, file is handle)))
        wrappers[-1]()
        out = capsys.readouterr().out
        assert dumps == [(30, False, True)]
        assert "atexit begin" in out and "atexit end" in out and called == [1]
        handle.close()
        rb._exit_dump_skip_logged = False
        dumps.clear()
        with pytest.raises(RuntimeError, match="co"):
            wrappers[-1]()
        out = capsys.readouterr().out
        assert dumps == [] and "no open handle" in out and "atexit end" in out
        seen.clear()
        arm_process_exit_diagnostics()
        assert seen == []
    finally:
        _restore_com_globals(saved)
        if "handle" in locals() and not handle.closed:
            handle.close()
        atexit.unregister(locals().get("original", lambda: None))
        for fn in kept:
            atexit.unregister(fn)
        log = logging.getLogger("comtypes")
        log.handlers[:] = [h for h in log.handlers
                           if h.__class__.__name__ != "_AppLogHandler"]
