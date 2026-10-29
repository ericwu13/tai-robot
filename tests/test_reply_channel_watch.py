"""Issue #219 — reply-channel diagnostics after process-start login.

Each test fails if that one behaviour is removed: the ConnectByID code
log, the single 60s silent alert, cancellation on OnConnect/OnComplete,
the one 30s IsConnectedByID read, a swallowed COM error, and the Discord
text. GUI and headless share ``_do_login`` and Tk ``root.after``.
"""

from __future__ import annotations

import logging
import queue

import pytest

import run_backtest as rb
from src.live.reply_watch import clear_reply_arrived


def _empty_ui_queue() -> None:
    while True:
        try:
            rb._ui_queue.get_nowait()
        except queue.Empty:
            return


@pytest.fixture(autouse=True)
def _isolate_reply_diagnostics(monkeypatch):
    monkeypatch.setattr(rb, "_discord", None)
    clear_reply_arrived()
    _empty_ui_queue()
    yield
    clear_reply_arrived()
    _empty_ui_queue()


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Root:
    def __init__(self):
        self.pending = {}
        self.cancelled = []
        self._next = 1

    def after(self, ms, fn, *args):
        token = f"after-{self._next}"
        self._next += 1
        self.pending[token] = (int(ms), fn, args)
        return token

    def after_cancel(self, token):
        self.cancelled.append(token)
        self.pending.pop(token, None)

    def token_for(self, ms):
        for token, (delay, _fn, _args) in self.pending.items():
            if delay == ms:
                return token
        return None

    def pop_delay(self, ms):
        token = self.token_for(ms)
        if token is None:
            have = sorted(delay for delay, _fn, _args in self.pending.values())
            raise AssertionError(f"no callback at {ms}ms; pending {have}")
        delay, fn, args = self.pending.pop(token)
        return delay, fn, args


class _Discord:
    def __init__(self):
        self.enabled = True
        self.messages = []

    def notify(self, message: str) -> None:
        self.messages.append(message)


class _Libs:
    def __init__(self):
        self.calls = []
        self.connect_code = 151
        self.message = "reply connect rejected"
        self.lookup_raises = False
        self.has_lookup = True
        self.probe_value = 7
        self.probe_raises = False
        self.probe_calls = 0
        self.probe_users = []


def _center_for(libs: _Libs):
    if not libs.has_lookup:
        class _Center:
            def SKCenterLib_SetLogPath(self, path):
                return None

            def SKCenterLib_LoginSetQuote(self, user, password, flag):
                libs.calls.append("LoginSetQuote")
                return 0

        return _Center()

    class _Center:
        def SKCenterLib_SetLogPath(self, path):
            return None

        def SKCenterLib_LoginSetQuote(self, user, password, flag):
            libs.calls.append("LoginSetQuote")
            return 0

        def SKCenterLib_GetReturnCodeMessage(self, code):
            if libs.lookup_raises:
                raise OSError("lookup down")
            return libs.message

    return _Center()


class _Reply:
    def __init__(self, libs: _Libs):
        self._libs = libs

    def SKReplyLib_ConnectByID(self, user):
        self._libs.calls.append("ConnectByID")
        return self._libs.connect_code

    def SKReplyLib_IsConnectedByID(self, user):
        self._libs.probe_calls += 1
        self._libs.probe_users.append(user)
        if self._libs.probe_raises:
            raise RuntimeError("COM blew")
        return self._libs.probe_value


class _Quote:
    def __init__(self, libs: _Libs):
        self._libs = libs

    def SKQuoteLib_EnterMonitorLONG(self):
        self._libs.calls.append("EnterMonitorLONG")
        return 0


def _login(monkeypatch, libs: _Libs | None = None, *, user: str = "tester"):
    """Run the real ``_do_login`` against fake SKCOM and a fake Tk root."""
    libs = libs or _Libs()
    logged: list[str] = []
    statuses: list[tuple] = []
    enabled: list[bool] = []

    monkeypatch.setattr(rb, "_log", lambda msg: logged.append(msg))
    monkeypatch.setattr(rb, "_log_debug", lambda msg: logged.append(msg))
    monkeypatch.setattr(rb, "skC", _center_for(libs))
    monkeypatch.setattr(rb, "skR", _Reply(libs))
    monkeypatch.setattr(rb, "skQ", _Quote(libs))
    monkeypatch.setattr(rb, "skO", None)

    real_makedirs = rb.os.makedirs

    def makedirs(path, *args, **kwargs):
        if str(path).endswith("CapitalLog_Backtest"):
            return None
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(rb.os, "makedirs", makedirs)

    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = _Root()
    app._settings = {}
    app._logged_in = False
    app._pending_api_fetch = False
    app.login_status_var = _Var("")
    app.login_user_var = _Var(user)
    app._link_host = None
    app._link_flags_pending = []
    app._reply_watch = None
    app.set_status = lambda msg, level="info": statuses.append((msg, level))
    app._enable_buttons = lambda: enabled.append(True)
    app.btn_login = type("B", (), {"config": lambda *a, **k: None})()
    app._adopt_link_machine = lambda: None
    app._do_login(user, "pw")
    return app, libs, logged, statuses, enabled


def _deliver(kind: str) -> None:
    if kind == "OnConnect":
        rb.SKReplyLibEvent().OnConnect("tester", 0)
    else:
        rb.SKReplyLibEvent().OnComplete("tester")


def test_connect_by_id_return_code_is_logged(monkeypatch):
    app, libs, logged, statuses, enabled = _login(monkeypatch)
    assert app._logged_in is True
    assert enabled == []
    assert ("錯誤 Error", "error") not in [(m, lv) for m, lv in statuses]
    assert libs.calls.index("ConnectByID") < libs.calls.index("EnterMonitorLONG")
    assert "[CONN] ConnectByID returned 151 reply connect rejected" in logged


def test_connect_by_id_logs_raw_code_when_lookup_unavailable(monkeypatch):
    for has_lookup, lookup_raises in ((False, False), (True, True)):
        clear_reply_arrived()
        _empty_ui_queue()
        libs = _Libs()
        libs.has_lookup = has_lookup
        libs.lookup_raises = lookup_raises
        _app, _libs, logged, statuses, enabled = _login(monkeypatch, libs)
        assert _app._logged_in is True
        assert enabled == []
        assert not any(level == "error" for _msg, level in statuses)
        assert "[CONN] ConnectByID returned 151" in logged
        assert "reply connect rejected" not in "\n".join(logged)


def test_reply_watchdog_fires_once_at_60s_when_silent(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="tai.reply_watch")
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    app, libs, logged, _statuses, _enabled = _login(monkeypatch)
    assert discord.messages == []
    assert libs.probe_calls == 0
    _delay, fn, args = app.root.pop_delay(60_000)
    fn(*args)
    fn(*args)
    # The 60s poll timer stays armed. The silent callback does not alert twice.
    assert app.root.token_for(60_000) is not None
    assert discord.messages == [
        "[CONN] REPLY CHANNEL SILENT — no reply READY within 60s"
    ]
    assert sum("REPLY CHANNEL SILENT" in line for line in logged) == 1
    assert any(
        rec.levelno == logging.ERROR and "REPLY CHANNEL SILENT" in rec.getMessage()
        for rec in caplog.records
    )
    # A second login arms a new watchdog. The spent callback stays quiet.
    app._do_login("tester", "pw")
    _delay, fn2, args2 = app.root.pop_delay(60_000)
    fn(*args)
    assert len(discord.messages) == 1
    fn2(*args2)
    assert len(discord.messages) == 2
    assert libs.probe_calls == 0


@pytest.mark.parametrize("kind", ["OnConnect", "OnComplete"])
def test_watchdog_does_not_fire_when_reply_arrives_first(monkeypatch, kind):
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    app, _libs, logged, _statuses, _enabled = _login(monkeypatch)
    _deliver(kind)
    _delay, fn, args = app.root.pop_delay(60_000)
    fn(*args)
    assert discord.messages == []
    assert not any("REPLY CHANNEL SILENT" in line for line in logged)


@pytest.mark.parametrize("kind", ["OnConnect", "OnComplete"])
def test_watchdog_is_cancelled_on_reply(monkeypatch, kind):
    app, _libs, _logged, _statuses, _enabled = _login(monkeypatch)
    token = app.root.token_for(60_000)
    assert token is not None
    probe = app.root.token_for(30_000)
    assert probe is not None
    _deliver(kind)
    app._process_ui_queue()
    assert token not in app.root.pending
    assert token in app.root.cancelled
    assert app.root.token_for(30_000) == probe


def test_is_connected_by_id_logged_once_at_30s(monkeypatch):
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    app, libs, logged, _statuses, _enabled = _login(monkeypatch)
    _delay, fn, args = app.root.pop_delay(30_000)
    assert app.root.token_for(30_000) is None
    fn(*args)
    fn(*args)
    assert libs.probe_calls == 1
    assert libs.probe_users == ["tester"]
    assert "[CONN] IsConnectedByID=7 at 30s after login" in logged
    assert discord.messages == []
    assert app.root.token_for(30_000) is None
    assert "tester" not in "\n".join(
        line for line in logged if "IsConnectedByID=" in line)


def test_is_connected_by_id_exception_is_logged_and_swallowed(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="tai.reply_watch")
    libs = _Libs()
    libs.probe_raises = True
    app, libs, logged, _statuses, _enabled = _login(monkeypatch, libs)
    _delay, fn, args = app.root.pop_delay(30_000)
    fn(*args)
    fn(*args)
    assert libs.probe_calls == 1
    assert any(
        "IsConnectedByID raised" in line and "RuntimeError" in line and "COM blew" in line
        for line in logged
    )
    assert any(
        rec.levelno == logging.ERROR and "IsConnectedByID raised" in rec.getMessage()
        for rec in caplog.records
    )


def test_discord_alert_contains_reply_channel_silent(monkeypatch):
    """Notifier is installed after login, which is when deploy builds it."""
    app, _libs, _logged, _statuses, _enabled = _login(monkeypatch)
    discord = _Discord()

    def notify(message: str) -> None:
        discord.messages.append(message)
        raise RuntimeError("discord down")

    discord.notify = notify
    monkeypatch.setattr(rb, "_discord", discord)
    _delay, fn, args = app.root.pop_delay(60_000)
    fn(*args)
    assert len(discord.messages) == 1
    assert "REPLY CHANNEL SILENT" in discord.messages[0]


def test_watch_arm_failure_does_not_fail_login(monkeypatch):
    def boom(self, user_id):
        raise RuntimeError("timer down")

    monkeypatch.setattr(rb.ReplyChannelWatch, "arm", boom)
    app, libs, _logged, statuses, enabled = _login(monkeypatch)
    assert app._logged_in is True
    assert enabled == []
    assert not any(level == "error" for _msg, level in statuses)
    assert "EnterMonitorLONG" in libs.calls
    assert "ConnectByID" in libs.calls


def test_watch_uses_tk_after_on_the_shared_login_path():
    import inspect

    from src.live.headless_app import HeadlessBotApp
    import src.live.reply_watch as reply_watch

    assert HeadlessBotApp._do_login is rb.BacktestApp._do_login
    ensure = inspect.getsource(rb.BacktestApp._ensure_reply_watch)
    assert "root.after" in ensure
    assert "threading" not in ensure
    module_src = inspect.getsource(reply_watch)
    assert "threading" not in module_src
    login = inspect.getsource(rb.BacktestApp._do_login)
    arm_at = login.find("_arm_reply_channel_watch")
    assert arm_at != -1
    assert "threading" not in login[arm_at:arm_at + 500]
