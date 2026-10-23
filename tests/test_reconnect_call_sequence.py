"""P0: reconnect call order stays the pre-#158 sequence, plus return logs.

Pre-#158 ``_attempt_reconnect`` called, in order:

    LeaveMonitor, LogOut, LoginSetQuote, ConnectByID, EnterMonitorLONG

and three seconds later ``_check_reconnection`` called IsConnected.

LogOut is the one kept removal (the method is not in Capital API 2.13.57).
Nothing else is added. IsConnectedByID is not read, because pre-#158 never
read it. Duration lines are logged as each existing call returns.
"""

from __future__ import annotations

from types import SimpleNamespace

import run_backtest as rb


# Pre-#158 order, with the kept LogOut removal and nothing inserted.
_ATTEMPT_ORDER = (
    "LeaveMonitor",
    "LoginSetQuote",
    "ConnectByID",
    "EnterMonitorLONG",
)


class _Var:
    def __init__(self, value: str):
        self._value = value

    def get(self):
        return self._value


class _Root:
    def __init__(self):
        self.scheduled = None

    def after(self, ms, fn):
        self.scheduled = (ms, fn)
        return "timer"

    def after_cancel(self, _token):
        return None


def _recorder(calls, name, result=0):
    def fn(*_args, **_kwargs):
        calls.append(name)
        return result
    return fn


def _app(calls):
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = _Root()
    app._quote_connected = False
    app._reconnect_timer_id = None
    app._logged_in = False
    def schedule_next(**_kwargs):
        return SimpleNamespace(type="attempt", message="next", delay_seconds=5)

    app._conn_monitor = SimpleNamespace(attempt=0, schedule_next=schedule_next)
    app.set_status = lambda *_a, **_k: None
    app._live_runner = None
    app.login_user_var = _Var("user")
    app.login_pass_var = _Var("secret")
    return app


def _install_fake(monkeypatch, calls):
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skQ", SimpleNamespace(
        SKQuoteLib_LeaveMonitor=_recorder(calls, "LeaveMonitor"),
        SKQuoteLib_EnterMonitorLONG=_recorder(calls, "EnterMonitorLONG"),
        SKQuoteLib_IsConnected=_recorder(calls, "IsConnected", 0),
    ))
    monkeypatch.setattr(rb, "skC", SimpleNamespace(
        SKCenterLib_LoginSetQuote=_recorder(calls, "LoginSetQuote", 0),
        SKCenterLib_GetReturnCodeMessage=lambda _code: "",
        # Present on the fake so a leftover call would show up in ``calls``.
        SKCenterLib_LogOut=_recorder(calls, "LogOut"),
    ))
    monkeypatch.setattr(rb, "skR", SimpleNamespace(
        SKReplyLib_ConnectByID=_recorder(calls, "ConnectByID", 0),
        SKReplyLib_IsConnectedByID=_recorder(calls, "IsConnectedByID", 0),
    ))


def test_attempt_reconnect_matches_pre_158_and_logs_duration(monkeypatch, capsys):
    """Fake SKCOM: same calls as pre-#158, minus LogOut, plus return timings.

    The reply-link value is logged only when the caller already holds it.
    This path does not hold one, so IsConnectedByID is not called and no
    reply-link state line is written. The quote IsConnected poll still logs
    the value it already fetched.
    """
    calls = []
    _install_fake(monkeypatch, calls)
    app = _app(calls)
    app._attempt_reconnect()

    assert calls == list(_ATTEMPT_ORDER)
    assert "LogOut" not in calls
    assert "IsConnected" not in calls
    assert "IsConnectedByID" not in calls
    assert app.root.scheduled is not None
    delay_ms, follow_up = app.root.scheduled
    assert delay_ms == 3000
    assert follow_up == app._check_reconnection

    out = capsys.readouterr().out
    for name in _ATTEMPT_ORDER:
        assert f"[RECONNECT] about to call {name} " in out
        assert f"[COM] {name} returned in " in out
        assert " ms " in out.split(f"[COM] {name} returned in ", 1)[1].split("\n", 1)[0]
    assert "reply-link IsConnectedByID=" not in out

    follow_up()
    assert calls == list(_ATTEMPT_ORDER) + ["IsConnected"]
    out = capsys.readouterr().out
    assert "[COM] IsConnected returned in " in out
    assert "[RECONNECT] IsConnected()=0 after 3s poll" in out
    assert "IsConnectedByID" not in calls
    assert "reply-link IsConnectedByID=" not in out


def test_reply_link_state_line_logs_an_already_read_value(capsys):
    """The state line prints a value the caller already has. No COM call."""
    rb._log_reply_link_state(0, where="already-read")
    out = capsys.readouterr().out
    assert "[RECONNECT] reply-link IsConnectedByID=0 at already-read " in out


def test_duration_log_does_not_replace_a_raised_call(monkeypatch):
    """A COM exception still propagates. The duration line says raised."""
    import src.live.main_heartbeat as hb

    seen = {}

    def logger(name, elapsed_ms, failed):
        seen["name"] = name
        seen["failed"] = failed
        seen["ms"] = elapsed_ms

    monkeypatch.setattr(hb, "_com_return_logger", logger)

    def boom():
        raise RuntimeError("skcom")

    try:
        hb.call_com("ConnectByID", boom)
        raised = False
    except RuntimeError as exc:
        raised = str(exc) == "skcom"
    assert raised
    assert seen["name"] == "ConnectByID"
    assert seen["failed"] is True
    assert seen["ms"] >= 0
