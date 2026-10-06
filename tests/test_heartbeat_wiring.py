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
    # Present before and after the #158 reconnect merge. This branch does
    # not read them; #158's _attempt_reconnect / stop path does.
    app._reconnect_schedule = SimpleNamespace(
        note_fired=lambda: None,
        suspend=lambda *_a, **_k: None,
        resume=lambda *_a, **_k: None,
        arm=lambda *_a, **_k: None,
        cancel_all=lambda *_a, **_k: None,
    )
    app._quote_ready_timer_id = None
    # #158 adds ReconnectController. This branch does not have the class.
    # Construct it when the name exists so a merge does not AttributeError
    # on the wiring fake; otherwise leave it unset-as-None.
    controller_cls = getattr(rb, "ReconnectController", None)
    app._reconnect_controller = controller_cls() if controller_cls else None
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
        assert ms == 5000
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


def test_tick_rearms_after_a_failed_write(tmp_path, monkeypatch):
    bot_dir = str(tmp_path / "bot")
    _install_runner(monkeypatch, bot_dir)
    app = _app(bot_dir)
    app._start_live_warmup = lambda: None
    calls = {"n": 0}
    real = hb.tick

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real()

    monkeypatch.setattr(hb, "tick", flaky)
    try:
        assert app._deploy_live_continue(
            None, _Strategy, False, None, False, False, False,
            None, "paper", "wirebot") is True
        app.root.fire_one()
        assert len(app.root.pending) == 1, "tick stopped re-arming after one failure"
        app.root.fire_one()
        assert calls["n"] == 2
        assert len(app.root.pending) == 1
    finally:
        _cleanup()


def test_deploy_continues_when_write_raises_main_thread_error(
        tmp_path, monkeypatch):
    """start() re-raises MainThreadHeartbeatError; the deploy guard catches it."""
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

    def boom(self, now=None):
        raise hb.MainThreadHeartbeatError("off thread")

    monkeypatch.setattr(hb.HeartbeatWriter, "write", boom)
    app = _app(bot_dir)
    warmups = []
    app._start_live_warmup = lambda: warmups.append(True)
    try:
        ok = app._deploy_live_continue(
            None, _Strategy, False, None, False, False, False,
            None, "paper", "wirebot")
        assert ok is True
        assert warmups == [True]
        assert app._heartbeat_active is False
        assert hb._writer is None
        assert len(notes) == 1
        assert "P2" in notes[0]
        assert "MainThreadHeartbeatError" in notes[0]
    finally:
        _cleanup()


def test_first_write_failure_logs_p2_and_still_arms(tmp_path, monkeypatch):
    """A PermissionError on the first write is visible and the tick still arms.

    main_heartbeat's logger is not the bot log, so the P2 has to be raised
    by _start_main_heartbeat from writer.initial_write_error.
    """
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
    calls = {"n": 0}
    real = hb.HeartbeatWriter.write

    def flaky(self, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("first write")
        return real(self, now=now)

    monkeypatch.setattr(hb.HeartbeatWriter, "write", flaky)
    app = _app(bot_dir)
    app._start_live_warmup = lambda: None
    try:
        ok = app._deploy_live_continue(
            None, _Strategy, False, None, False, False, False,
            None, "paper", "wirebot")
        assert ok is True
        assert app._heartbeat_active is True
        assert len(app.root.pending) == 1
        assert hb._writer is not None
        assert isinstance(hb._writer.initial_write_error, PermissionError)
        assert len(notes) == 1
        assert "P2" in notes[0]
        assert "PermissionError" in notes[0]
        log_files = list((tmp_path / "bot").glob("debug_*.log"))
        assert log_files
        text = log_files[0].read_text(encoding="utf-8")
        assert "start failed" in text
        assert "PermissionError" in text
        assert not os.path.isfile(os.path.join(bot_dir, "heartbeat.json"))
        app.root.fire_one()
        assert os.path.isfile(os.path.join(bot_dir, "heartbeat.json"))
    finally:
        _cleanup()


def test_first_write_failure_still_arms_later_ticks(tmp_path, monkeypatch):
    bot_dir = str(tmp_path / "bot")
    os.makedirs(bot_dir, exist_ok=True)
    calls = {"n": 0}
    real = hb.HeartbeatWriter.write

    def flaky(self, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("first write")
        return real(self, now=now)

    monkeypatch.setattr(hb.HeartbeatWriter, "write", flaky)
    app = _app(bot_dir)
    app._live_runner = SimpleNamespace(bot_dir=bot_dir)
    try:
        app._start_main_heartbeat()
        assert app._heartbeat_active is True
        assert len(app.root.pending) == 1
        assert not os.path.isfile(os.path.join(bot_dir, "heartbeat.json"))
        app.root.fire_one()
        assert os.path.isfile(os.path.join(bot_dir, "heartbeat.json"))
        assert len(app.root.pending) == 1
    finally:
        _cleanup()


def test_send_future_order_stamps_inflight(tmp_path, monkeypatch):
    """The real send path stamps inflight_com_call. A comment does not."""
    import sys
    import types

    bot_dir = str(tmp_path / "bot")
    os.makedirs(bot_dir, exist_ok=True)
    writer = hb.HeartbeatWriter(bot_dir)
    writer.write()
    hb._writer = writer
    seen = {}

    def send(*_a, **_k):
        data = json.loads(open(writer.path, encoding="utf-8").read())
        inflight = data.get("inflight_com_call") or {}
        seen["name"] = inflight.get("name")
        return ("SEQ1", 0)

    sk_mod = types.ModuleType("comtypes.gen.SKCOMLib")

    class _Order:
        pass

    sk_mod.FUTUREORDER = _Order
    gen = types.ModuleType("comtypes.gen")
    comtypes_mod = types.ModuleType("comtypes")
    comtypes_mod.gen = gen
    monkeypatch.setitem(sys.modules, "comtypes", comtypes_mod)
    monkeypatch.setitem(sys.modules, "comtypes.gen", gen)
    monkeypatch.setitem(sys.modules, "comtypes.gen.SKCOMLib", sk_mod)
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(SendFutureOrderCLR=send))
    monkeypatch.setattr(rb, "_discord", None)
    app = _app(bot_dir)
    app._futures_account = "ACCT"
    app._live_runner = SimpleNamespace(
        symbol="TMF00",
        csv_logger=None,
        strategy_display_name="Probe",
        broker=SimpleNamespace(entry_bar_index=1, trades=[
            SimpleNamespace(exit_bar_index=2)]),
    )
    app._fill_tracker = SimpleNamespace(register_order=lambda *_a, **_k: None)
    app._trading_guard = SimpleNamespace(
        on_entry_sent=lambda: None,
        on_exit_sent=lambda: None,
        on_fill_pending=lambda *_a: None,
    )
    app._trading_mode = "semi_auto"
    app.login_user_var = _Var("user")
    try:
        ok = app._send_real_order(1, "TMFH6", "exit", 20000)
    finally:
        hb._writer = None
        _cleanup()
    assert ok is True
    assert seen.get("name") == "SendFutureOrderCLR"


def test_check_reconnection_stamps_is_connected(tmp_path, monkeypatch):
    """The reconnect probe wraps IsConnected. Unwrapping it leaves no stamp."""
    bot_dir = str(tmp_path / "bot")
    os.makedirs(bot_dir, exist_ok=True)
    writer = hb.HeartbeatWriter(bot_dir)
    writer.write()
    hb._writer = writer
    seen = {}

    def is_connected():
        data = json.loads(open(writer.path, encoding="utf-8").read())
        inflight = data.get("inflight_com_call") or {}
        seen["name"] = inflight.get("name")
        return 0

    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(
        rb, "skQ", SimpleNamespace(SKQuoteLib_IsConnected=is_connected))
    app = _app(bot_dir)
    app._quote_connected = False
    app._conn_monitor = SimpleNamespace(attempt=1)
    scheduled = []
    app._schedule_reconnect = lambda: scheduled.append(True)
    try:
        app._check_reconnection()
    finally:
        hb._writer = None
        _cleanup()
    assert seen.get("name") == "IsConnected"
    assert scheduled == [True]


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
