"""Headless CONTROL file: reconnect, paper only, consume once."""

from __future__ import annotations

import os
from pathlib import Path

from src.backtest.strategy import BacktestStrategy
from src.live.headless import (
    apply_control_command,
    consume_control_file,
    control_ack_path,
    control_file_path,
)
from src.live.live_runner import LiveRunner
from src.market_data.data_store import DataStore
from src.market_data.models import Bar
from src.backtest.broker import BrokerContext


class _Quiet(BacktestStrategy):
    kline_type = 0
    kline_minute = 1

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        return None

    def required_bars(self) -> int:
        return 1


def _runner(tmp_path, mode="paper"):
    runner = LiveRunner(_Quiet(), "TX00", log_dir=str(tmp_path / "logs"))
    runner.trading_mode = mode
    return runner


def _write(bot_dir, text):
    os.makedirs(bot_dir, exist_ok=True)
    path = control_file_path(bot_dir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _apply(text, *, reconnect=None, set_mode=None, logs=None, runner=None):
    logs = [] if logs is None else logs
    calls = {"reconnect": 0, "modes": []}

    def _reconnect():
        calls["reconnect"] += 1
        if reconnect is not None:
            reconnect()

    def _set_mode(mode):
        calls["modes"].append(mode)
        if set_mode is not None:
            set_mode(mode)
        elif runner is not None:
            runner._apply_mode_switch(mode)

    result = apply_control_command(
        text, reconnect=_reconnect, set_mode=_set_mode, log=logs.append,
    )
    return result, calls, logs


def test_reconnect_uses_the_callback_once(tmp_path):
    bot = str(tmp_path / "TX00_night")
    path = _write(bot, "reconnect\n")
    text = consume_control_file(bot)
    result, calls, logs = _apply(text)
    assert result == "reconnect"
    assert calls["reconnect"] == 1
    assert calls["modes"] == []
    assert any("CONTROL reconnect" in line for line in logs)
    assert not os.path.exists(path)
    assert consume_control_file(bot) is None
    again, calls2, _logs2 = _apply(consume_control_file(bot) or "")
    assert again == "reject"
    assert calls2["reconnect"] == 0


def test_control_file_consumed_once(tmp_path):
    bot = str(tmp_path / "TMF00_b1")
    _write(bot, "set-mode paper\n")
    first = consume_control_file(bot)
    second = consume_control_file(bot)
    assert first.strip() == "set-mode paper"
    assert second is None
    assert not os.path.exists(control_file_path(bot))
    assert os.path.isfile(control_ack_path(bot))
    ack = Path(control_ack_path(bot)).read_text(encoding="utf-8")
    assert ack.strip() == "set-mode paper"


def test_set_mode_paper_switches(tmp_path):
    runner = _runner(tmp_path, "auto")
    result, calls, _logs = _apply("set-mode paper", runner=runner)
    assert result == "set-mode paper"
    assert calls["modes"] == ["paper"]
    assert runner.trading_mode == "paper"


def test_set_mode_auto_rejected_even_with_override(tmp_path):
    runner = _runner(tmp_path, "paper")
    runner._allow_live_override = True
    result, calls, logs = _apply("set-mode auto", runner=runner)
    assert result == "reject"
    assert calls["modes"] == []
    assert runner.trading_mode == "paper"
    assert any("set-mode auto" in line for line in logs)


def test_set_mode_real_rejected(tmp_path):
    runner = _runner(tmp_path, "auto")
    runner._allow_live_override = True
    result, calls, logs = _apply("set-mode real", runner=runner)
    assert result == "reject"
    assert calls["modes"] == []
    assert runner.trading_mode == "auto"
    assert any("set-mode real" in line for line in logs)


def test_set_mode_paper_honours_veto(tmp_path):
    runner = _runner(tmp_path, "auto")
    runner.mode_switch_veto = lambda: "real order fill pending"
    statuses = []
    runner.on("on_status", lambda msg: statuses.append(msg))
    result, calls, _logs = _apply("set-mode paper", runner=runner)
    assert result == "set-mode paper"
    assert calls["modes"] == ["paper"]
    assert runner.trading_mode == "auto"
    assert any("real order fill pending" in s for s in statuses)


def test_poll_control_reconnect_is_idempotent(tmp_path):
    from src.live.headless_app import DeployDriver

    bot = str(tmp_path / "TX00_h")
    _write(bot, "reconnect\n")
    called = []
    driver = DeployDriver.__new__(DeployDriver)
    driver.bot_dir = bot
    driver.app = type("A", (), {})()
    driver.app._manual_reconnect = lambda: called.append("reconnect")
    driver.app._live_runner = _runner(tmp_path)
    driver._poll_control()
    driver._poll_control()
    assert called == ["reconnect"]
    assert not os.path.exists(control_file_path(bot))


def test_poll_control_rejects_auto_with_override(tmp_path):
    from src.live.headless_app import DeployDriver

    bot = str(tmp_path / "TX00_h")
    runner = _runner(tmp_path, "paper")
    runner._allow_live_override = True
    _write(bot, "set-mode auto\n")
    driver = DeployDriver.__new__(DeployDriver)
    driver.bot_dir = bot
    driver.app = type("A", (), {})()
    driver.app._manual_reconnect = lambda: None
    driver.app._live_runner = runner
    driver._poll_control()
    assert runner.trading_mode == "paper"
    assert consume_control_file(bot) is None


def test_readme_documents_the_control_file():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "data/live/{symbol}_{bot}/CONTROL" in text
    assert "CONTROL.ack" in text
    assert "CONTROL.stale" in text
    assert "set-mode paper" in text
    assert "set-mode auto" in text
    assert "set-mode real" in text


def _poll_driver(bot, runner, reconnect):
    from src.live.headless_app import DeployDriver

    driver = DeployDriver.__new__(DeployDriver)
    driver.bot_dir = bot
    driver.exit_code = None
    driver._stop_flag = False
    driver._stop_via_us = False
    driver._last_heartbeat = 0.0
    driver.args = type("Args", (), {"heartbeat_min": 0})()
    scheduled = []

    class _Root:
        def after(self, ms, fn):
            scheduled.append((ms, fn))

        def quit(self):
            return None

    driver.root = _Root()
    driver.app = type("A", (), {})()
    driver.app._manual_reconnect = reconnect
    driver.app._live_runner = runner
    driver.app._stop_live = lambda: None
    driver.scheduled = scheduled
    return driver


def _rescheduled(driver):
    return [fn for _ms, fn in driver.scheduled if fn == driver._poll]


def test_utf16_reconnect_is_applied_and_acked(tmp_path):
    bot = str(tmp_path / "TX00_ps")
    os.makedirs(bot)
    path = control_file_path(bot)
    with open(path, "wb") as f:
        f.write("reconnect\r\n".encode("utf-16"))
    called = []
    driver = _poll_driver(bot, _runner(tmp_path), lambda: called.append("reconnect"))
    driver._poll()
    assert called == ["reconnect"]
    assert not os.path.exists(path)
    assert os.path.isfile(control_ack_path(bot))
    assert _rescheduled(driver) == [driver._poll]
    text = Path(control_ack_path(bot)).read_bytes()
    assert text.startswith((b"\xff\xfe", b"\xfe\xff"))


def test_utf8_bom_reconnect_is_applied(tmp_path):
    bot = str(tmp_path / "TX00_bom")
    path = _write(bot, "")
    with open(path, "wb") as f:
        f.write(b"\xef\xbb\xbfreconnect\n")
    text = consume_control_file(bot)
    result, calls, _logs = _apply(text)
    assert result == "reconnect"
    assert calls["reconnect"] == 1
    assert not os.path.exists(path)


def test_garbage_control_is_acked_and_the_poll_continues(tmp_path):
    bot = str(tmp_path / "TX00_bin")
    os.makedirs(bot)
    path = control_file_path(bot)
    with open(path, "wb") as f:
        f.write(b"\x80\x81\xff\x00not-a-command")
    called = []
    driver = _poll_driver(bot, _runner(tmp_path), lambda: called.append("reconnect"))
    driver._poll()
    assert called == []
    assert not os.path.exists(path)
    assert os.path.isfile(control_ack_path(bot))
    assert _rescheduled(driver) == [driver._poll]
    driver.scheduled.clear()
    driver._poll()
    assert driver.exit_code is None
    assert _rescheduled(driver) == [driver._poll]


def test_raising_reconnect_keeps_the_poll_alive_and_stop_works(tmp_path):
    from src.live.headless import EXIT_OK, request_stop

    bot = str(tmp_path / "TX00_boom")
    _write(bot, "reconnect\n")

    def boom():
        raise RuntimeError("reconnect blew up")

    stopped = []
    driver = _poll_driver(bot, _runner(tmp_path), boom)
    driver.app._stop_live = lambda: stopped.append("stopped")
    driver._poll()
    assert driver.exit_code is None
    assert _rescheduled(driver) == [driver._poll]
    assert not os.path.exists(control_file_path(bot))
    driver.scheduled.clear()
    request_stop(bot, "test")
    driver._poll()
    assert stopped == ["stopped"]
    assert driver.exit_code == EXIT_OK
    assert _rescheduled(driver) == []


def test_deploy_archives_stale_control(tmp_path):
    bot = str(tmp_path / "TX00_stale")
    path = _write(bot, "set-mode paper\n")
    driver = _deploy_driver(bot, object())
    driver._deploy()
    assert not os.path.exists(path)
    stale = Path(bot) / "CONTROL.stale"
    assert stale.read_text(encoding="utf-8").strip() == "set-mode paper"
    assert driver.scheduled and driver.scheduled[0][1] == driver._poll


def _deploy_driver(bot, runner):
    from src.live.headless_app import DeployDriver

    driver = DeployDriver.__new__(DeployDriver)
    driver.bot_dir = bot
    driver.req = object()
    driver.exit_code = None
    driver._stop_flag = False
    driver._stop_via_us = False
    driver._last_heartbeat = 0.0
    driver._ignored_control = None
    driver._ignored_control_logged = False
    driver.args = type("Args", (), {"heartbeat_min": 0})()
    scheduled = []

    class _Root:
        def after(self, ms, fn):
            scheduled.append((ms, fn))

        def quit(self):
            return None

    driver.root = _Root()
    driver.app = type("A", (), {})()
    driver.app._deploy_live_from = lambda req: True
    driver.app._live_runner = runner
    driver.app.alerts = []
    driver.app._manual_reconnect = lambda: None
    driver.app._stop_live = lambda: None
    driver.scheduled = scheduled
    return driver


def _replace_raises_for_stale(monkeypatch, exc):
    real_replace = os.replace

    def _replace(src, dst):
        if str(dst).endswith("CONTROL.stale"):
            raise exc
        return real_replace(src, dst)

    monkeypatch.setattr("src.live.headless.os.replace", _replace)


def test_deploy_when_stale_archive_is_a_directory(tmp_path):
    """A directory at CONTROL.stale must not skip the poll or apply the file."""
    from src.live.headless import EXIT_OK, request_stop

    bot = str(tmp_path / "TX00_dir")
    _write(bot, "set-mode paper\n")
    os.mkdir(os.path.join(bot, "CONTROL.stale"))
    runner = _runner(tmp_path, "auto")
    driver = _deploy_driver(bot, runner)
    driver._deploy()
    assert _rescheduled(driver) == [driver._poll]
    assert runner.trading_mode == "auto"
    assert not os.path.exists(control_file_path(bot))
    driver.scheduled.clear()
    driver._poll()
    assert runner.trading_mode == "auto"
    request_stop(bot, "test")
    driver.scheduled.clear()
    driver._poll()
    assert driver.exit_code == EXIT_OK
    assert runner.trading_mode == "auto"


def test_deploy_when_archive_replace_raises_permission_error(tmp_path, monkeypatch):
    from src.live.headless import EXIT_OK, request_stop

    bot = str(tmp_path / "TX00_perm")
    _write(bot, "reconnect\n")
    _replace_raises_for_stale(monkeypatch, PermissionError("sharing violation"))
    called = []
    runner = _runner(tmp_path, "auto")
    driver = _deploy_driver(bot, runner)
    driver.app._manual_reconnect = lambda: called.append("reconnect")
    driver._deploy()
    assert _rescheduled(driver) == [driver._poll]
    driver.scheduled.clear()
    driver._poll()
    assert called == []
    assert runner.trading_mode == "auto"
    assert not os.path.exists(control_file_path(bot))
    request_stop(bot, "test")
    driver.scheduled.clear()
    driver._poll()
    assert driver.exit_code == EXIT_OK
    assert called == []


def test_undeletable_stale_control_is_ignored_until_it_changes(tmp_path, monkeypatch):
    bot = str(tmp_path / "TX00_stuck")
    _write(bot, "set-mode paper\n")
    _replace_raises_for_stale(monkeypatch, PermissionError("sharing violation"))
    real_remove = os.remove

    def _remove(path):
        if os.path.basename(path) == "CONTROL":
            raise PermissionError("read-only")
        return real_remove(path)

    monkeypatch.setattr("src.live.headless.os.remove", _remove)
    runner = _runner(tmp_path, "auto")
    driver = _deploy_driver(bot, runner)
    driver._deploy()
    assert _rescheduled(driver) == [driver._poll]
    assert driver._ignored_control is not None
    driver.scheduled.clear()
    driver._poll()
    assert runner.trading_mode == "auto"
    assert os.path.isfile(control_file_path(bot))
    _write(bot, "set-mode paper\n\n")
    driver.scheduled.clear()
    driver._poll()
    assert runner.trading_mode == "paper"
    assert not os.path.exists(control_file_path(bot))


def test_invalid_utf8_control_is_one_rejected_token(tmp_path):
    bot = str(tmp_path / "TX00_bin2")
    os.makedirs(bot)
    path = control_file_path(bot)
    with open(path, "wb") as f:
        f.write(b"\x80\x81\xff\x00not-a-command")
    text = consume_control_file(bot)
    assert text == "\ufffd"
    result, calls, logs = _apply(text)
    assert result == "reject"
    assert calls["reconnect"] == 0
    assert calls["modes"] == []
    assert any("\ufffd" in line for line in logs)
    assert not os.path.exists(path)
    assert os.path.isfile(control_ack_path(bot))


def test_failed_stop_leaves_stop_file_and_retries(tmp_path):
    from src.live.headless import EXIT_OK, request_stop, stop_requested

    bot = str(tmp_path / "TX00_stop")
    os.makedirs(bot)
    calls = []

    def _stop():
        calls.append("stop")
        if len(calls) == 1:
            raise RuntimeError("stop blew up")

    driver = _poll_driver(bot, _runner(tmp_path), lambda: None)
    driver.app._stop_live = _stop
    request_stop(bot, "test")
    driver._poll()
    assert calls == ["stop"]
    assert stop_requested(bot)
    assert driver.exit_code is None
    assert _rescheduled(driver) == [driver._poll]
    driver.scheduled.clear()
    driver._poll()
    assert calls == ["stop", "stop"]
    assert driver.exit_code == EXIT_OK
    assert not stop_requested(bot)
    assert _rescheduled(driver) == []
