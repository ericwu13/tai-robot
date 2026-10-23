"""P2: ``_attempt_reconnect`` does not run the pre-#158 SKCOM sequence.

The old order was LeaveMonitor, LoginSetQuote, ConnectByID,
EnterMonitorLONG, then a 3s IsConnected poll that treated half-up as
ready. That sequence is the #157 crash. The method now ticks the link
machine. Process-start login stays in ``_do_login``.

Duration lines are still emitted by ``call_com`` when the machine does
call the broker, from the main-thread tick, not from this method.
"""

from __future__ import annotations

from types import SimpleNamespace

import run_backtest as rb


class _Var:
    def __init__(self, value: str):
        self._value = value

    def get(self):
        return self._value


def test_attempt_reconnect_does_not_call_skcom(monkeypatch):
    """No host yet: the method returns without touching the broker."""
    calls = []

    def _record(name, fn, *args, **kwargs):
        calls.append(name)
        return fn(*args, **kwargs)

    monkeypatch.setattr(rb, "call_com", _record)
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._link_host = None
    app._reconnect_timer_id = None
    app._attempt_reconnect()
    assert calls == []


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


def test_manual_reconnect_without_a_machine_does_not_call_skcom(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rb, "call_com",
        lambda name, fn, *a, **k: calls.append(name) or fn(*a, **k))
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._link_host = None
    app._reconnect_timer_id = None
    app._quote_ready_timer_id = None
    app.root = SimpleNamespace(after_cancel=lambda *_a: None)
    app.btn_reconnect = SimpleNamespace(config=lambda **_k: None)
    app.login_status_var = _Var("")
    app.login_status_var.set = lambda _v: None
    app._conn_monitor = SimpleNamespace(
        on_manual_reconnect=lambda: SimpleNamespace(message="manual"))
    app.set_status = lambda *_a, **_k: None
    app._set_conn_dot = lambda *_a, **_k: None
    app._cancel_quote_ready_timer = lambda *_a, **_k: None
    app._manual_reconnect()
    assert calls == []
