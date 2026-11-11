"""#169 phase 1: in-process BrokerPort, same COM order as before the seam.

The sequences below are the pre-change call order from run_backtest
(process-start login, account query, place_order, event queues). A fake
SKCOM drives the port. The link-machine methods still raise on logout
and restart, and they still log the reconnect about-to-call line. The
app methods do not.
"""

from __future__ import annotations

import ast
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live.broker_port import (
    BROKER_MODE_INPROCESS,
    DEFAULT_BROKER_MODE,
    require_inprocess,
)
from src.live.com_broker_port import ComBrokerPort, ReconnectForbidden

ROOT = Path(__file__).resolve().parents[1]
_SK_NAMES = {"skC", "skQ", "skR", "skO"}
_ADAPTER = "src/live/com_broker_port.py"


def _drain(q) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            return items


def _port(libs, *, before=None, ui=None, tick=None):
    seen = []

    def call(name, fn, *args, **kwargs):
        seen.append((name, args))
        return fn(*args, **kwargs)

    port = ComBrokerPort(
        call=call,
        libs=lambda: libs,
        credentials=lambda: ("user", "secret"),
        on_subscribe=lambda: None,
        on_reconcile=lambda: None,
        on_release=lambda: None,
        before_call=before,
        ui_queue=ui,
        tick_queue=tick,
    )
    return port, seen


class _Center:
    def __init__(self, log):
        self._log = log

    def SKCenterLib_SetLogPath(self, path):
        self._log.append(("SetLogPath", (path,)))
        return 0

    def SKCenterLib_SetAuthority(self, flag):
        self._log.append(("SetAuthority", (flag,)))
        return 0

    def SKCenterLib_LoginSetQuote(self, user, password, flag):
        self._log.append(("LoginSetQuote", (user, password, flag)))
        return 0

    def SKCenterLib_GetReturnCodeMessage(self, code):
        self._log.append(("GetReturnCodeMessage", (code,)))
        return f"text-{code}"


class _Quote:
    def __init__(self, log):
        self._log = log

    def SKQuoteLib_EnterMonitorLONG(self):
        self._log.append(("EnterMonitorLONG", ()))
        return 0

    def SKQuoteLib_IsConnected(self):
        self._log.append(("IsConnected", ()))
        return 1

    def SKQuoteLib_RequestStockList(self, market):
        self._log.append(("RequestStockList", (market,)))
        return 0

    def SKQuoteLib_RequestTicks(self, page, symbol):
        self._log.append(("RequestTicks", (page, symbol)))
        return (0, 9)

    def SKQuoteLib_RequestKLineAMByDate(self, *args):
        self._log.append(("RequestKLineAMByDate", args))
        return 0


class _Reply:
    def __init__(self, log):
        self._log = log

    def SKReplyLib_ConnectByID(self, user):
        self._log.append(("ConnectByID", (user,)))
        return 151

    def SKReplyLib_IsConnectedByID(self, user):
        self._log.append(("IsConnectedByID", (user,)))
        return 0


class _Order:
    def __init__(self, log):
        self._log = log

    def SKOrderLib_Initialize(self):
        self._log.append(("SKOrderLib_Initialize", ()))
        return 0

    def ReadCertByID(self, user):
        self._log.append(("ReadCertByID", (user,)))
        return 0

    def GetUserAccount(self):
        self._log.append(("GetUserAccount", ()))
        return "ACCT"

    def GetOpenInterestGW(self, user, account, fmt):
        self._log.append(("GetOpenInterestGW", (user, account, fmt)))
        return 0

    def GetFutureRights(self, user, account, currency):
        self._log.append(("GetFutureRights", (user, account, currency)))
        return 0

    def GetFulfillReport(self, user, account, fmt):
        self._log.append(("GetFulfillReport", (user, account, fmt)))
        return "fills"

    def SendFutureOrderCLR(self, user, async_order, order):
        self._log.append(("SendFutureOrderCLR", (user, async_order, order)))
        return ("SEQ1", 0)


def _libs(log):
    return _Center(log), _Quote(log), _Reply(log), _Order(log)


def test_broker_mode_default_is_inprocess_and_other_modes_raise():
    assert DEFAULT_BROKER_MODE == BROKER_MODE_INPROCESS == "inprocess"
    assert require_inprocess(None) == "inprocess"
    assert require_inprocess("inprocess") == "inprocess"
    with pytest.raises(RuntimeError, match="not implemented"):
        require_inprocess("child")
    text = (ROOT / "run_backtest.py").read_text(encoding="utf-8")
    assert 'get("broker_mode"' not in text
    assert ".get('broker_mode'" not in text
    assert '"broker_mode": DEFAULT_BROKER_MODE' in text


def test_app_login_methods_match_today_and_skip_reconnect_log():
    """Process-start order. SetLogPath and SetAuthority stay unstamped."""
    direct = []
    logged = []
    port, stamped = _port(_libs(direct), before=logged.append)
    assert port.set_log_path("CapitalLog_Backtest") == 0
    assert port.set_authority(1) == 0
    assert port.login_set_quote("user", "secret", "Y") == 0
    assert port.connect_by_id("user") == 151
    assert port.enter_monitor_long() == 0
    assert port.initialize_order() == 0
    assert port.read_cert("user") == 0
    assert port.get_user_account() == "ACCT"
    assert [name for name, _args in direct] == [
        "SetLogPath",
        "SetAuthority",
        "LoginSetQuote",
        "ConnectByID",
        "EnterMonitorLONG",
        "SKOrderLib_Initialize",
        "ReadCertByID",
        "GetUserAccount",
    ]
    assert direct[2][1] == ("user", "secret", "Y")
    assert direct[3][1] == ("user",)
    assert [name for name, _args in stamped] == [
        "LoginSetQuote",
        "ConnectByID",
        "EnterMonitorLONG",
        "SKOrderLib_Initialize",
        "ReadCertByID",
        "GetUserAccount",
    ]
    assert logged == []


def test_do_login_keeps_that_order(monkeypatch):
    """The app still calls the port in today's order, including the flag."""
    direct = []
    monkeypatch.setattr(rb, "_log", lambda *_a, **_k: None)
    monkeypatch.setattr(rb, "_log_debug", lambda *_a, **_k: None)
    monkeypatch.setattr(rb, "skC", _Center(direct))
    monkeypatch.setattr(rb, "skQ", _Quote(direct))
    monkeypatch.setattr(rb, "skR", _Reply(direct))
    monkeypatch.setattr(rb, "skO", _Order(direct))
    monkeypatch.setattr(rb.os, "makedirs", lambda *_a, **_k: None)

    class _Root:
        def after(self, _ms, _fn):
            return "tok"

    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = _Root()
    app._settings = {"authority_flag": 1}
    app._logged_in = False
    app._pending_api_fetch = False
    app.login_status_var = SimpleNamespace(set=lambda *_a: None, get=lambda: "")
    app.login_user_var = SimpleNamespace(get=lambda: "user")
    app.login_pass_var = SimpleNamespace(get=lambda: "secret")
    app.set_status = lambda *_a, **_k: None
    app._enable_buttons = lambda: None
    app.btn_login = SimpleNamespace(config=lambda *_a, **_k: None)
    app._adopt_link_machine = lambda: None
    app._start_reply_poller = lambda: None
    app._arm_reply_channel_watch = lambda *_a, **_k: None
    app._do_login("user", "secret")
    assert [name for name, _args in direct] == [
        "SetLogPath",
        "SetAuthority",
        "LoginSetQuote",
        "ConnectByID",
        "EnterMonitorLONG",
        "SKOrderLib_Initialize",
        "ReadCertByID",
        "GetUserAccount",
    ]
    assert direct[2][1] == ("user", "secret", "Y")


def test_queries_and_place_order_keep_args_and_return_codes():
    direct = []
    port, stamped = _port(_libs(direct))
    order = object()
    assert port.request_stock_list(2) == 0
    assert port.request_ticks(0, "TX00") == (0, 9)
    assert port.request_kline("TX00", 0, 1, 0, "20260101", "20260102", 1) == 0
    assert port.get_open_interest("U", "ACCT", 1) == 0
    assert port.get_future_rights("U", "ACCT", 1) == 0
    assert port.get_fulfill_report("U", "ACCT", 1) == "fills"
    assert port.is_quote_connected() == 1
    assert port.read_is_connected_by_id("U") == 0
    assert port.place_order("U", order, False) == ("SEQ1", 0)
    assert direct == [
        ("RequestStockList", (2,)),
        ("RequestTicks", (0, "TX00")),
        ("RequestKLineAMByDate", ("TX00", 0, 1, 0, "20260101", "20260102", 1)),
        ("GetOpenInterestGW", ("U", "ACCT", 1)),
        ("GetFutureRights", ("U", "ACCT", 1)),
        ("GetFulfillReport", ("U", "ACCT", 1)),
        ("IsConnected", ()),
        ("IsConnectedByID", ("U",)),
        ("SendFutureOrderCLR", ("U", False, order)),
    ]
    assert stamped[-1] == ("SendFutureOrderCLR", ("U", False, order))


def test_place_order_stays_before_register_order():
    text = (ROOT / "run_backtest.py").read_text(encoding="utf-8")
    start = text.find("def _send_real_order(")
    end = text.find("\n    def ", start + 1)
    body = text[start:end]
    send_at = body.find("_broker.place_order(")
    register_at = body.find("self._fill_tracker.register_order(")
    assert 0 <= send_at < register_at
    assert "sTradeType = 1" in body
    assert "sTradeType = 0" in body
    assert 'bstrPrice = "M"' in body


def test_return_code_message_stamps_and_skips_a_missing_center():
    direct = []
    port, stamped = _port(_libs(direct))
    assert port.return_code_message(151) == "text-151"
    assert stamped == [("GetReturnCodeMessage", (151,))]
    empty, stamped_empty = _port((None, None, None, None))
    assert empty.return_code_message(151) is None
    assert stamped_empty == []
    center = SimpleNamespace()
    bare, stamped_bare = _port((center, None, None, None))
    assert bare.return_code_message(151) is None
    assert stamped_bare == []


def test_machine_methods_still_log_and_logout_still_raises():
    direct = []
    logged = []
    port, stamped = _port(_libs(direct), before=logged.append)
    assert port.enter_monitor() == 0
    assert logged == ["EnterMonitorLONG"]
    assert stamped == [("EnterMonitorLONG", ())]
    with pytest.raises(ReconnectForbidden):
        port.logout()
    with pytest.raises(ReconnectForbidden):
        port.restart_process()
    assert "LogOut" not in [name for name, _args in stamped]


def test_attach_events_binds_quote_reply_order_and_keeps_partial():
    bound = []

    def get_events(com, handler):
        return ("sink", com, handler)

    def bind(kind, com, handler, get):
        sink = get(com, handler)
        bound.append((kind, com, handler, sink))
        return sink

    libs = ("C", "Q", "R", "O")
    port, _stamped = _port(libs)
    out = []
    port.attach_events(out, "qh", "rh", "oh", bind, get_events)
    assert [item[0] for item in bound] == ["quote", "reply", "order"]
    assert bound[0][1:] == ("Q", "qh", ("sink", "Q", "qh"))
    assert bound[1][1:] == ("R", "rh", ("sink", "R", "rh"))
    assert bound[2][1:] == ("O", "oh", ("sink", "O", "oh"))
    assert out[0] == "qh" and out[1][0] == "sink"
    assert out[2] == "rh" and out[4] == "oh"

    def boom(kind, com, handler, get):
        if kind == "reply":
            raise RuntimeError("reply bind failed")
        return bind(kind, com, handler, get)

    partial = []
    with pytest.raises(RuntimeError, match="reply bind failed"):
        port.attach_events(partial, "qh", "rh", "oh", boom, get_events)
    assert partial[0] == "qh"
    assert partial[1][0] == "sink"
    assert partial[2] == "rh"
    assert len(partial) == 3


def test_enqueue_feeds_the_same_queues_in_order():
    ui = queue.Queue()
    tick = queue.Queue()
    port, _seen = _port((None, None, None, None), ui=lambda: ui, tick=lambda: tick)
    port.enqueue_ui(("link", ("connection", 3003, 0)))
    port.enqueue_ui(("log", "ready"))
    port.enqueue_tick((1, False))
    port.enqueue_tick((2, True))
    assert _drain(ui) == [("link", ("connection", 3003, 0)), ("log", "ready")]
    assert _drain(tick) == [(1, False), (2, True)]


def test_quote_events_keep_queue_order_and_generation(monkeypatch):
    _drain(rb._ui_queue)
    _drain(rb._tick_queue)
    monkeypatch.setattr(rb, "_app", SimpleNamespace(
        _account_monitor=SimpleNamespace(oi_query_generation=7)))
    try:
        rb.SKQuoteLibEvents().OnNotifyHistoryTicksLONG(
            0, 0, 0, 20261111, 84500, 0, 1, 2, 3, 4, 0)
        rb.SKQuoteLibEvents().OnNotifyTicksLONG(
            0, 0, 0, 20261111, 84501, 0, 1, 2, 3, 5, 0)
        rb.SKQuoteLibEvents().OnConnection(3003, 0)
        rb.SKOrderLibEvents().OnOpenInterest("ROW")
        ticks = _drain(rb._tick_queue)
        assert ticks[0][-1] is True
        assert ticks[1][-1] is False
        assert ticks[0][3:7] == (1, 2, 3, 4)
        ui = _drain(rb._ui_queue)
        assert ui[0] == ("link", ("connection", 3003, 0))
        assert ui[1][0] == "log"
        assert ("conn", "ready") in ui
        assert ui[-1] == ("open_interest", ("ROW", 7))
    finally:
        _drain(rb._ui_queue)
        _drain(rb._tick_queue)


def test_open_interest_without_a_generation_stays_a_string(monkeypatch):
    _drain(rb._ui_queue)
    monkeypatch.setattr(rb, "_app", None)
    try:
        rb.SKOrderLibEvents().OnOpenInterest("PLAIN")
        assert _drain(rb._ui_queue) == [("open_interest", "PLAIN")]
    finally:
        _drain(rb._ui_queue)


def test_kline_data_stays_inline_and_complete_uses_root_after(monkeypatch):
    seen = []

    class _Fetcher:
        total_bar_count = 99

        def on_kline_data(self, data):
            seen.append((data, threading.get_ident()))

    app = SimpleNamespace(
        _live_warmup_mode=False,
        _live_polling=False,
        _fetcher=_Fetcher(),
        _live_warmup_data=[],
        root=None,
    )
    monkeypatch.setattr(rb, "_app", app)
    before = threading.active_count()
    rb.SKQuoteLibEvents().OnNotifyKLineData("TX00", "bar-1")
    assert seen == [("bar-1", threading.get_ident())]
    assert threading.current_thread() is threading.main_thread()
    assert threading.active_count() == before

    scheduled = []

    class _Root:
        def after(self, delay, callback):
            scheduled.append((
                delay, callback, threading.current_thread(), threading.get_ident()))
            return "tok"

    done = lambda: None
    app._live_warmup_mode = True
    app.root = _Root()
    app._on_live_warmup_complete = done
    monkeypatch.setattr(rb, "_log", lambda *_a, **_k: None)
    before = threading.active_count()
    rb.SKQuoteLibEvents().OnKLineComplete(0)
    assert threading.active_count() == before
    assert len(scheduled) == 1
    delay, callback, thread, ident = scheduled[0]
    assert delay == 100
    assert callback is done
    assert thread is threading.main_thread()
    assert ident == threading.get_ident()


def test_no_sk_attribute_access_outside_the_adapter():
    """skC / skQ / skR / skO attribute access lives only in the COM adapter."""
    offenders = []
    files = [ROOT / "run_backtest.py"]
    files.extend(sorted((ROOT / "src").rglob("*.py")))
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if rel == _ADAPTER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in _SK_NAMES
            ):
                offenders.append(f"{rel}:{node.lineno} {node.value.id}.{node.attr}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in _SK_NAMES
            ):
                offenders.append(f"{rel}:{node.lineno} getattr({node.args[0].id}, ...)")
    assert offenders == []
