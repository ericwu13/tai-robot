"""Issue #169 Phase 1: in-process BrokerPort at the call_com seam.

The COM call order below is the pre-port order. These tests drive the
real app methods (and the port directly) and require that order, the
same call_com stamp names, and no SKCOM call outside the adapter.
"""

from __future__ import annotations

import ast
import os
import queue
import sys
import types
from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live.broker_port import (
    BrokerEvent,
    BrokerPort,
    InProcessSkcomPort,
    initialize_com,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER = os.path.join(PROJECT_ROOT, "src", "live", "broker_port.py")

# Historical _do_login COM order, authority_flag == 0, order lib present.
LOGIN_COM = [
    "SKCenterLib_SetLogPath",
    "SKCenterLib_LoginSetQuote",
    "SKReplyLib_ConnectByID",
    "SKQuoteLib_EnterMonitorLONG",
    "SKOrderLib_Initialize",
    "ReadCertByID",
    "GetUserAccount",
]
LOGIN_STAMPS = [
    "LoginSetQuote",
    "ConnectByID",
    "EnterMonitorLONG",
    "SKOrderLib_Initialize",
    "ReadCertByID",
    "GetUserAccount",
]
# SetLogPath / SetAuthority were never inside call_com.
UNSTAMPED = {"SKCenterLib_SetLogPath", "SKCenterLib_SetAuthority"}

# Historical _attempt_reconnect teardown, when the center object has LogOut.
RECONNECT_COM = [
    "SKQuoteLib_IsConnected",
    "SKQuoteLib_LeaveMonitor",
    "SKCenterLib_LogOut",
    "SKCenterLib_LoginSetQuote",
    "SKReplyLib_ConnectByID",
    "SKQuoteLib_EnterMonitorLONG",
]
RECONNECT_STAMPS = [
    "IsConnected",
    "LeaveMonitor",
    "LogOut",
    "LoginSetQuote",
    "ConnectByID",
    "EnterMonitorLONG",
]


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

    def append(self, line, tag="status"):
        self.line = line


class _Root:
    def __init__(self):
        self.pending = []

    def after(self, ms, fn, *args):
        self.pending.append((ms, fn, args))
        return len(self.pending)

    def after_cancel(self, token):
        return None


class _Rec:
    """COM stand-in. Every method records (name, args) and returns ``result``."""

    def __init__(self, log, result=0):
        self.log = log
        self.result = result

    def __getattr__(self, name):
        def fn(*args, **kwargs):
            self.log.append((name, args))
            return self.result
        return fn


def _names(log):
    return [name for name, _args in log]


@pytest.fixture
def stamps(monkeypatch):
    seen = []

    def wrapped(name, fn, *args, **kwargs):
        seen.append(name)
        return fn(*args, **kwargs)

    monkeypatch.setattr("src.live.broker_port.call_com", wrapped)
    return seen


def _install(monkeypatch, log, *, connected=0, login_code=0, has_order=True):
    monkeypatch.setattr(rb, "skC", _Rec(log, login_code))
    monkeypatch.setattr(rb, "skQ", _Rec(log, connected))
    monkeypatch.setattr(rb, "skR", _Rec(log))
    order = _Rec(log) if has_order else None
    monkeypatch.setattr(rb, "skO", order)
    monkeypatch.setattr(rb, "_com_available", True)
    return order


def _login_app(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._settings = {}
    app.login_status_var = _Var("")
    app.set_status = lambda *_a, **_k: None
    app.btn_login = _Widget()
    app._pending_api_fetch = False
    app._enable_buttons = lambda: None
    app._logged_in = False
    app.root = _Root()
    return app


def test_port_is_the_shared_inprocess_adapter():
    assert isinstance(rb.broker_port, InProcessSkcomPort)
    assert isinstance(rb.broker_port, BrokerPort)


def test_login_com_sequence_matches_pre_port(tmp_path, monkeypatch, stamps):
    log = []
    _install(monkeypatch, log)
    app = _login_app(tmp_path, monkeypatch)
    app._do_login("user-1", "secret")
    assert _names(log) == LOGIN_COM
    assert stamps == LOGIN_STAMPS
    login_args = next(args for name, args in log if name == "SKCenterLib_LoginSetQuote")
    assert login_args == ("user-1", "secret", "Y")
    assert app._logged_in is True


def test_login_authority_inserts_set_authority_unstamped(tmp_path, monkeypatch, stamps):
    log = []
    _install(monkeypatch, log)
    app = _login_app(tmp_path, monkeypatch)
    app._settings = {"authority_flag": 2}
    app._do_login("user-1", "secret")
    assert _names(log)[:2] == ["SKCenterLib_SetLogPath", "SKCenterLib_SetAuthority"]
    assert stamps[0] == "LoginSetQuote"
    assert "SetAuthority" not in stamps
    assert "SetLogPath" not in stamps
    assert next(args for name, args in log if name == "SKCenterLib_SetAuthority") == (2,)


def test_login_without_order_lib_stops_after_enter_monitor(tmp_path, monkeypatch, stamps):
    log = []
    _install(monkeypatch, log, has_order=False)
    app = _login_app(tmp_path, monkeypatch)
    app._do_login("user-1", "secret")
    assert _names(log) == LOGIN_COM[:4]
    assert stamps == LOGIN_STAMPS[:3]


def test_reconnect_com_sequence_matches_pre_port(monkeypatch, stamps):
    log = []
    _install(monkeypatch, log, connected=0)
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._quote_connected = False
    app._logged_in = True
    app._conn_monitor = SimpleNamespace(attempt=0)
    app._reconnect_controller = rb.ReconnectController()
    app._reconnect_schedule = SimpleNamespace(
        note_fired=lambda: None,
        arm=lambda *_a, **_k: "timer",
    )
    app._reconnect_timer_id = None
    app.login_user_var = _Var("user-1")
    app.login_pass_var = _Var("secret")
    app.root = _Root()
    app._attempt_reconnect()
    assert _names(log) == RECONNECT_COM
    assert stamps == RECONNECT_STAMPS
    assert app._logged_in is True


def test_account_query_order_is_oi_then_rights(monkeypatch, stamps):
    log = []
    _install(monkeypatch, log)
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._logged_in = True
    app._futures_account = "ACCT"
    app.login_user_var = _Var("user-1")
    cleared = []
    app._account_monitor = SimpleNamespace(
        clear_positions=lambda: cleared.append(True))
    app._query_real_account()
    assert _names(log) == ["GetOpenInterestGW", "GetFutureRights"]
    assert stamps == ["GetOpenInterestGW", "GetFutureRights"]
    assert cleared == [True]
    assert all(args == ("user-1", "ACCT", 1) for _name, args in log)


def test_place_order_still_sends_once_async_false(monkeypatch, stamps):
    log = []
    _install(monkeypatch, log)
    # SendFutureOrderCLR returns (message, code), not the default 0.
    rb.skO.result = ("SEQ1", 0)
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
    monkeypatch.setattr(rb, "_discord", None)
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._futures_account = "ACCT"
    app._live_runner = SimpleNamespace(
        symbol="TMF00",
        csv_logger=None,
        strategy_display_name="Probe",
        broker=SimpleNamespace(entry_bar_index=3, trades=[]),
    )
    app._fill_tracker = SimpleNamespace(register_order=lambda *_a, **_k: None)
    app._trading_guard = SimpleNamespace(
        on_entry_sent=lambda: None,
        on_exit_sent=lambda: None,
        on_fill_pending=lambda *_a: None,
    )
    app._trading_mode = "semi_auto"
    app.login_user_var = _Var("user-1")
    app.live_log = _Widget()
    app.root = _Root()
    assert app._send_real_order(1, "TMFH6", "exit", 20000) is True
    assert _names(log) == ["SendFutureOrderCLR"]
    assert stamps == ["SendFutureOrderCLR"]
    _name, args = log[0]
    assert args[0] == "user-1"
    assert args[1] is False
    assert isinstance(args[2], _Order)


def test_quote_and_kline_methods_keep_call_arguments(stamps):
    log = []
    port = InProcessSkcomPort(lambda: (_Rec(log), _Rec(log), _Rec(log), _Rec(log)))
    port.subscribe_quotes(0, "TX00")
    port.subscribe_kline("TX00", 0, 1, 0, "20260101", "20260102", 60)
    port.request_stock_list(2)
    assert _names(log) == [
        "SKQuoteLib_RequestTicks",
        "SKQuoteLib_RequestKLineAMByDate",
        "SKQuoteLib_RequestStockList",
    ]
    assert log[0][1] == (0, "TX00")
    assert log[1][1] == ("TX00", 0, 1, 0, "20260101", "20260102", 60)
    assert log[2][1] == (2,)
    assert stamps == [
        "RequestTicks", "RequestKLineAMByDate", "RequestStockList"]


def test_query_position_is_one_open_interest_call(stamps):
    log = []
    port = InProcessSkcomPort(lambda: (None, None, None, _Rec(log)))
    port.query_position("user-1", "ACCT", 1)
    assert _names(log) == ["GetOpenInterestGW"]
    assert stamps == ["GetOpenInterestGW"]
    port.query_fills("user-1", "ACCT", 1)
    assert stamps[-1] == "GetFulfillReport"


def test_cancel_order_is_not_wired_and_not_called():
    port = InProcessSkcomPort(lambda: (None, None, None, None))
    with pytest.raises(NotImplementedError):
        port.cancel_order("user", "seq")
    tree = ast.parse(open(
        os.path.join(PROJECT_ROOT, "run_backtest.py"), encoding="utf-8").read())
    calls = [
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "cancel_order"
    ]
    assert calls == []


def test_logout_guard_skips_when_the_method_is_absent(stamps):
    class _Center:
        def SKCenterLib_LoginSetQuote(self, *args):
            return 0

        def SKCenterLib_GetReturnCodeMessage(self, code):
            return ""

    log = []
    quote = _Rec(log, 0)
    # Explicit class: no SKCenterLib_LogOut, so hasattr is false.
    monkey_handles = lambda: (_Center(), quote, _Rec(log), None)
    port = InProcessSkcomPort(monkey_handles)
    assert port.supports_logout() is False


def test_events_round_trip_and_a_bad_listener_does_not_raise():
    port = InProcessSkcomPort(lambda: (None, None, None, None))
    seen = []
    port.subscribe(BrokerEvent.FILL, seen.append)

    def boom(_payload):
        raise RuntimeError("listener failed")

    port.subscribe(BrokerEvent.FILL, boom)
    port.emit(BrokerEvent.FILL, "raw-fill")
    assert seen == ["raw-fill"]
    port.emit(BrokerEvent.TICK, None)  # no listeners: no-op


def test_bar_tick_fill_disconnect_and_order_reply_emit(monkeypatch):
    seen = {event: [] for event in BrokerEvent}

    def _listen(event):
        def cb(payload, _event=event):
            seen[_event].append(payload)
        rb.broker_port.subscribe(event, cb)
        return cb

    callbacks = [_listen(event) for event in BrokerEvent]
    try:
        fetcher = SimpleNamespace(
            on_kline_data=lambda raw: None, total_bar_count=10)
        holder = SimpleNamespace(
            _live_warmup_mode=False, _live_polling=False, _fetcher=fetcher)
        monkeypatch.setattr(rb, "_app", holder)
        rb.SKQuoteLibEvents().OnNotifyKLineData("TMF00", "01/02/2026 08:45,1,2,3,4,5")

        tick_app = rb.BacktestApp.__new__(rb.BacktestApp)
        tick_app._live_runner = None
        tick_app._on_com_tick(20260102, 84500, 0, 1, 2, 100, 1, 0, False)

        fill_app = rb.BacktestApp.__new__(rb.BacktestApp)
        fill_app._futures_account = ""
        fill_app._fill_tracker = SimpleNamespace(on_new_data=lambda _raw: None)
        fill_app._live_runner = None
        fill_app._on_new_data("D,seq,100")

        disc = rb.BacktestApp.__new__(rb.BacktestApp)
        disc._quote_connected = True

        def _set(value, _reason):
            disc._quote_connected = value

        disc._cancel_quote_ready_timer = lambda *_a, **_k: None
        disc._set_quote_connected = _set
        disc._reconnect_controller = SimpleNamespace(on_clean_disconnect=lambda: None)
        disc._cancel_reconnect_timer = lambda: None
        disc.set_status = lambda *_a, **_k: None
        disc._set_conn_dot = lambda *_a, **_k: None
        disc.login_status_var = _Var()
        disc.btn_login = _Widget()
        disc.btn_reconnect = _Widget()
        disc._live_runner = None
        disc._live_tick_active = False
        disc._conn_monitor = SimpleNamespace(on_disconnected=lambda: None)
        disc._schedule_reconnect = lambda: None
        disc._on_disconnected()
        disc._on_disconnected()

        while True:
            try:
                rb._ui_queue.get_nowait()
            except queue.Empty:
                break
        order_app = rb.BacktestApp.__new__(rb.BacktestApp)
        order_app._live_runner = None
        monkeypatch.setattr(rb, "_app", None)
        rb._ui_queue.put_nowait(("order_result", (0, "ok")))
        order_app._process_ui_queue()
    finally:
        for event, cb in zip(BrokerEvent, callbacks):
            rb.broker_port._listeners[event].remove(cb)

    assert seen[BrokerEvent.BAR] == ["01/02/2026 08:45,1,2,3,4,5"]
    assert seen[BrokerEvent.TICK] == [(20260102, 84500, 0, 1, 2, 100, 1, 0, False)]
    assert seen[BrokerEvent.FILL] == ["D,seq,100"]
    assert seen[BrokerEvent.DISCONNECT] == [None]
    assert seen[BrokerEvent.ORDER_REPLY] == [(0, "ok")]


def test_initialize_com_missing_sdk_does_not_clobber_handles(tmp_path, monkeypatch, capsys):
    """No libs directory is the old early return: globals stay as they were.

    A stand-in ``comtypes`` lets the function reach that check on a machine
    where the real package is not installed. The import itself still sits
    where it used to, before the directory check.
    """
    fake = types.ModuleType("comtypes")
    fake.__path__ = []
    fake.client = types.ModuleType("comtypes.client")
    monkeypatch.setitem(sys.modules, "comtypes", fake)
    monkeypatch.setitem(sys.modules, "comtypes.client", fake.client)
    saved = (rb._com_available, rb.skC, rb.skQ, rb.skR, rb.skO)
    try:
        monkeypatch.setattr(rb, "bundle_root", str(tmp_path))
        monkeypatch.setattr(rb, "project_root", str(tmp_path))
        rb._com_available = True
        rb.skC = "keep"
        assert initialize_com(
            str(tmp_path), str(tmp_path), "c", "q", "r", "o") is None
        rb._init_com()
        assert rb.skC == "keep"
        assert rb._com_available is True
    finally:
        rb._com_available, rb.skC, rb.skQ, rb.skR, rb.skO = saved
    assert "no SDK or bundled libs found" in capsys.readouterr().out


def test_gui_and_headless_use_the_same_port():
    import inspect

    import src.live.headless_app as ha

    boot = inspect.getsource(ha._boot)
    assert "rb._init_com()" in boot
    assert "rb.register_com_events()" in boot
    assert "InProcessSkcomPort" not in inspect.getsource(ha)
    main_src = inspect.getsource(rb.main)
    assert "_init_com()" in main_src
    assert "register_com_events()" in main_src
    assert "--broker" not in inspect.getsource(ha)
    assert "--broker" not in open(ADAPTER, encoding="utf-8").read()


def test_phase1_does_not_spawn_a_broker_child():
    text = open(ADAPTER, encoding="utf-8").read()
    assert "multiprocessing" not in text
    assert "--broker" not in text
    app = open(os.path.join(PROJECT_ROOT, "run_backtest.py"), encoding="utf-8").read()
    assert "--broker" not in app
    assert "multiprocessing.Process" not in app


# ── lint: no direct SKCOM call outside the adapter ──

_PREFIXES = (
    "SKQuoteLib_", "SKCenterLib_", "SKReplyLib_", "SKOrderLib_", "ISK",
)
_EXACT = frozenset({
    "SKCenterLib", "SKQuoteLib", "SKReplyLib", "SKOrderLib",
    "FUTUREORDER", "SendFutureOrderCLR", "GetOpenInterestGW",
    "GetFulfillReport", "GetFutureRights", "ReadCertByID", "GetUserAccount",
})


def _forbidden_attr(name: str) -> bool:
    return name in _EXACT or name.startswith(_PREFIXES)


def violations_in_source(source: str, filename: str) -> list[str]:
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _forbidden_attr(node.attr):
            found.append(f"{filename}:{node.lineno}: {node.attr}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "call_com":
                found.append(f"{filename}:{node.lineno}: call_com")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.endswith("SKCOMLib"):
                found.append(f"{filename}:{node.lineno}: import {node.module}")
            if node.module == "src.live.main_heartbeat":
                for alias in node.names:
                    if alias.name == "call_com":
                        found.append(f"{filename}:{node.lineno}: import call_com")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("SKCOMLib"):
                    found.append(f"{filename}:{node.lineno}: import {alias.name}")
    return found


def _production_files():
    yield os.path.join(PROJECT_ROOT, "run_backtest.py")
    cli = os.path.join(PROJECT_ROOT, "run_bot_cli.py")
    if os.path.isfile(cli):
        yield cli
    src = os.path.join(PROJECT_ROOT, "src")
    for dirpath, dirnames, filenames in os.walk(src):
        # DLL gateway (SKDLLPython), not the COM call_com seam.
        if "gateway" in dirnames:
            dirnames.remove("gateway")
        for filename in filenames:
            if filename.endswith(".py"):
                yield os.path.join(dirpath, filename)


def test_lint_flags_a_direct_skcom_call():
    src = "def f(skQ):\n    return skQ.SKQuoteLib_LeaveMonitor()\n"
    hits = violations_in_source(src, "example.py")
    assert hits == ["example.py:2: SKQuoteLib_LeaveMonitor"]
    assert violations_in_source("import comtypes.gen.SKCOMLib as sk\n", "e.py")


def test_no_direct_skcom_calls_outside_the_adapter():
    adapter_hits = violations_in_source(
        open(ADAPTER, encoding="utf-8").read(), ADAPTER)
    assert any("SKQuoteLib_LeaveMonitor" in hit for hit in adapter_hits), (
        "the adapter must be the SKCOM call site; the lint would not "
        "see a call if this assertion failed")
    leaked = []
    for path in _production_files():
        if os.path.normpath(path) == os.path.normpath(ADAPTER):
            continue
        leaked.extend(violations_in_source(
            open(path, encoding="utf-8").read(),
            os.path.relpath(path, PROJECT_ROOT)))
    assert leaked == []
