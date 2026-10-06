"""Production wiring for the main-thread heartbeat (issue #157 phase 2).

Drives the real BacktestApp methods with a fake Tk root. A comment that
mentions call_com, or a source-text pin, does not satisfy these tests.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import run_backtest as rb
import src.live.main_heartbeat as hb


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Widget:
    def config(self, **kwargs):
        self.kwargs = kwargs

    def set(self, value):
        self.value = value

    def get(self):
        return getattr(self, "value", "")


class _Log:
    def __init__(self):
        self.lines = []

    def append(self, line, tag="status"):
        self.lines.append(line)


class _Root:
    def __init__(self):
        self.pending = {}
        self._next = 1

    def after(self, ms, fn, *args):
        token = self._next
        self._next += 1
        self.pending[token] = (ms, fn, args)
        return token

    def after_cancel(self, token):
        self.pending.pop(token, None)

    def fire_one(self):
        token, (ms, fn, args) = next(iter(self.pending.items()))
        del self.pending[token]
        fn(*args)
        return ms


class _Strategy:
    name = "Probe"


def _app(bot_dir: str):
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = _Root()
    app.symbol_var = _Var("TMF00")
    app.pv_var = _Var("200")
    app.strategy_var = _Var("Probe")
    app.chart_tf_var = _Widget()
    app.bot_name_var = _Widget()
    app.trading_mode_var = _Widget()
    app.btn_deploy = _Widget()
    app.btn_api = _Widget()
    app.btn_tv = _Widget()
    app.btn_update = _Widget()
    app.symbol_combo = _Widget()
    app.strategy_combo = _Widget()
    app.chart_tf_combo = _Widget()
    app.mode_combo = _Widget()
    app.live_log = _Log()
    app._settings = {}
    app._trading_guard = SimpleNamespace(
        daily_loss_limit=0, fill_pending=False, reset=lambda: None)
    app._quote_connected = False
    app._trading_mode = "paper"
    app._live_runner = None
    app._regime_manager = None
    app._update_manual_order_buttons = lambda: None
    app.set_status = lambda _msg: None
    app._heartbeat_active = False
    app._heartbeat_after_id = None
    app._bot_dir = bot_dir
    return app


def _install_runner(monkeypatch, bot_dir: str):
    os.makedirs(bot_dir, exist_ok=True)

    class FakeRunner:
        def __init__(self, strategy, symbol, point_value=0, log_dir="",
                     bot_name="", strategy_display_name=""):
            self.bot_dir = bot_dir
            self.symbol = symbol
            self.broker = SimpleNamespace(
                trade_source=None, position_size=0, position_side=None)
            self.strategy_display_name = strategy_display_name
            self.suppress_strategy = False

        def acquire_lock(self):
            self.locked = True

        def on(self, *_a, **_k):
            return None

        def stop(self):
            return {"trades": 0, "pnl": 0, "bars_1m": 0, "bars_agg": 0}

    monkeypatch.setattr(rb, "LiveRunner", FakeRunner)


def _stop_attrs(app):
    app._stop_account_polling = lambda: None
    app._live_poll_id = None
    app._reconnect_timer_id = None
    app._conn_monitor = SimpleNamespace(attempt=0, reset=lambda: None)
    app._warmup_timeout_id = None
    app._fill_poll_timer_id = None
    app._live_tick_count = 0
    app._live_history_done = False
    app._live_tick_active = False
    app._tick_watchdog = SimpleNamespace(reset=lambda: None)
    app._live_chart = None
    app._update_live_results = lambda: None
    app._update_live_status = lambda: None
    app._run_evolution_check_after_report = lambda: None


def _cleanup():
    hb.stop()
    if hb._watchdog is not None:
        hb._watchdog.disable()
    rb._close_debug_log()


def test_deploy_wires_heartbeat_tick_and_stop(tmp_path, monkeypatch):
    bot_dir = str(tmp_path / "bot")
    _install_runner(monkeypatch, bot_dir)
    app = _app(bot_dir)
    app._start_live_warmup = lambda: None
    exited = []
    monkeypatch.setattr(rb.os, "_exit", lambda code: exited.append(code))
    monkeypatch.setattr(hb.os, "_exit", lambda code: exited.append(code))
    try:
        ok = app._deploy_live_continue(
            None, _Strategy, False, None, False, False, False,
            None, "paper", "wirebot")
        assert ok is True
        hb_path = os.path.join(bot_dir, "heartbeat.json")
        assert os.path.isfile(hb_path)
        assert app._heartbeat_active is True
        assert len(app.root.pending) == 1
        ms = app.root.fire_one()
        assert ms == hb.HEARTBEAT_INTERVAL_MS
        assert len(app.root.pending) == 1, "heartbeat tick did not re-arm"
        hb._watchdog.alert_fn("probe")
        assert exited == []
        _stop_attrs(app)
        app._stop_live()
        assert not os.path.isfile(hb_path)
    finally:
        _cleanup()


def test_deploy_continues_when_heartbeat_start_raises(tmp_path, monkeypatch):
    bot_dir = str(tmp_path / "bot")
    _install_runner(monkeypatch, bot_dir)
    notes = []

    class FakeNotifier:
        def __init__(self, *_a, **_k):
            self.enabled = True

        def bot_deployed(self, **_k):
            return None

        def notify(self, message):
            notes.append(message)

    monkeypatch.setattr(
        "src.live.discord_notify.DiscordNotifier", FakeNotifier)

    def boom(*_a, **_k):
        raise PermissionError("bot dir")

    monkeypatch.setattr(hb, "start", boom)
    app = _app(bot_dir)
    warmups = []
    app._start_live_warmup = lambda: warmups.append(True)
    try:
        ok = app._deploy_live_continue(
            None, _Strategy, False, None, False, False, False,
            None, "paper", "wirebot")
        assert ok is True
        assert warmups == [True]
        assert len(notes) == 1
        assert "P2" in notes[0]
        log_files = list((tmp_path / "bot").glob("debug_*.log"))
        assert log_files
        text = log_files[0].read_text(encoding="utf-8")
        assert "start failed" in text
        assert "PermissionError" in text
    finally:
        _cleanup()


def test_leave_monitor_is_stamped_by_call_com(tmp_path, monkeypatch):
    """Behavior, not a source grep: inflight name during the COM call."""
    bot_dir = str(tmp_path / "bot")
    os.makedirs(bot_dir, exist_ok=True)
    writer = hb.HeartbeatWriter(bot_dir)
    writer.write()
    hb._writer = writer
    seen = {}

    def leave():
        data = json.loads(open(writer.path, encoding="utf-8").read())
        inflight = data.get("inflight_com_call") or {}
        seen["name"] = inflight.get("name")
        return 0

    skq = SimpleNamespace(
        SKQuoteLib_LeaveMonitor=leave,
        SKQuoteLib_EnterMonitorLONG=lambda: 0,
    )
    skc = SimpleNamespace(
        SKCenterLib_LogOut=lambda _user: 0,
        SKCenterLib_LoginSetQuote=lambda *_a: 0,
        SKCenterLib_GetReturnCodeMessage=lambda _code: "",
    )
    skr = SimpleNamespace(SKReplyLib_ConnectByID=lambda _user: 0)
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skQ", skq)
    monkeypatch.setattr(rb, "skC", skc)
    monkeypatch.setattr(rb, "skR", skr)
    app = _app(bot_dir)
    app._conn_monitor = SimpleNamespace(attempt=0, reset=lambda: None)
    app.login_user_var = _Var("user")
    app.login_pass_var = _Var("secret")
    try:
        app._attempt_reconnect()
    finally:
        hb._writer = None
        _cleanup()
    assert seen.get("name") == "LeaveMonitor"
