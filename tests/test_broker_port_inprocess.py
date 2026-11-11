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
    assert "require_inprocess(DEFAULT_BROKER_MODE)" in text


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


def _name_list(node: ast.AST) -> list[str | None] | None:
    """Names in a plain Name or a tuple/list of Names. None if not plain."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str | None] = []
        for elt in node.elts:
            if isinstance(elt, ast.Name):
                names.append(elt.id)
            else:
                names.append(None)
        return names
    return None


def _plain_bindings(tree: ast.AST) -> list[tuple[str, str, int]]:
    """``(target, rhs, lineno)`` for plain name-to-name assignments."""
    bindings: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            rhs = _name_list(node.value)
            if rhs is None:
                continue
            if (
                len(node.targets) == 1
                and isinstance(node.targets[0], (ast.Tuple, ast.List))
                and len(rhs) > 1
            ):
                targets = _name_list(node.targets[0])
                if targets is not None and len(targets) == len(rhs):
                    for target, source in zip(targets, rhs):
                        if target and source:
                            bindings.append((target, source, node.lineno))
                continue
            if len(rhs) == 1 and rhs[0]:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings.append((target.id, rhs[0], node.lineno))
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Name)
        ):
            bindings.append((node.target.id, node.value.id, node.lineno))
    return bindings


def _alias_names(bindings: list[tuple[str, str, int]]) -> set[str]:
    """Names bound, directly or through another alias, to skC/skQ/skR/skO."""
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for target, source, _lineno in bindings:
            if target in _SK_NAMES:
                continue
            if source in _SK_NAMES or source in aliases:
                if target not in aliases:
                    aliases.add(target)
                    changed = True
    return aliases


class _CallComVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.calls: list[tuple[int, str | None]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "call_com":
            owner = self.stack[-1] if self.stack else None
            self.calls.append((node.lineno, owner))
        self.generic_visit(node)


def com_seam_offenders(source: str, relpath: str) -> list[str]:
    """SKCOM seam violations in one source file.

    Flags attribute access and getattr() on skC/skQ/skR/skO and on any
    plain alias of those names, the alias assignment itself, and — in
    run_backtest.py only — call_com() outside ``_live_com_call``.
    """
    offenders: list[str] = []
    tree = ast.parse(source, filename=relpath)
    bindings = _plain_bindings(tree)
    aliases = _alias_names(bindings)
    watched = _SK_NAMES | aliases
    for target, source_name, lineno in bindings:
        if source_name in _SK_NAMES or source_name in aliases:
            offenders.append(
                f"{relpath}:{lineno} alias {target} = {source_name}")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in watched
        ):
            offenders.append(
                f"{relpath}:{node.lineno} {node.value.id}.{node.attr}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in watched
        ):
            offenders.append(
                f"{relpath}:{node.lineno} getattr({node.args[0].id}, ...)")
    if relpath.replace("\\", "/").endswith("run_backtest.py"):
        visitor = _CallComVisitor()
        visitor.visit(tree)
        for lineno, owner in visitor.calls:
            if owner != "_live_com_call":
                offenders.append(
                    f"{relpath}:{lineno} call_com() outside _live_com_call")
    return offenders


def test_no_sk_attribute_access_outside_the_adapter():
    """skC / skQ / skR / skO attribute access lives only in the COM adapter."""
    offenders = []
    files = [ROOT / "run_backtest.py"]
    files.extend(sorted((ROOT / "src").rglob("*.py")))
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if rel == _ADAPTER:
            continue
        offenders.extend(com_seam_offenders(path.read_text(encoding="utf-8"), rel))
    assert offenders == []


def test_lint_rejects_alias_and_call_com_outside_the_wrapper():
    """An alias of skQ plus call_com() outside _live_com_call must fail the lint.

    The planted source is only this string. It is not written into the repo.
    """
    planted = (
        "def sneak():\n"
        "    _q = skQ\n"
        "    return call_com('IsConnected', _q.SKQuoteLib_IsConnected)\n"
    )
    offenders = com_seam_offenders(planted, "run_backtest.py")
    text = "\n".join(offenders)
    assert "_q" in text
    assert "call_com" in text
    wrapper = (
        "def _live_com_call(name, fn, *args, **kwargs):\n"
        "    return call_com(name, fn, *args, **kwargs)\n"
    )
    assert com_seam_offenders(wrapper, "run_backtest.py") == []


def test_app_broker_login_cannot_send_empty_credentials(monkeypatch):
    """_broker.login() must not send ('', '') and must not reach call_com."""
    sent = []

    class _Center:
        def SKCenterLib_LoginSetQuote(self, user, password, flag):
            sent.append((user, password, flag))
            return 0

    monkeypatch.setattr(rb, "skC", _Center())
    with pytest.raises(RuntimeError, match="credentials"):
        rb._broker.login()
    assert sent == []
    assert rb._broker.login_set_quote("user", "pw", "Y") == 0
    assert sent == [("user", "pw", "Y")]


def test_load_settings_does_not_store_an_unread_broker_mode():
    text = (ROOT / "run_backtest.py").read_text(encoding="utf-8")
    start = text.find("def _load_settings(")
    end = text.find("\ndef ", start + 1)
    assert start != -1 and end != -1
    assert "broker_mode" not in text[start:end]
    assert "require_inprocess(DEFAULT_BROKER_MODE)" in text


def test_port_docstring_says_queue_puts_run_on_event_callbacks():
    doc = ast.get_docstring(ast.parse(
        (ROOT / "src/live/com_broker_port.py").read_text(encoding="utf-8")))
    assert doc is not None
    assert "enqueue_ui" in doc
    assert "enqueue_tick" in doc
    lowered = doc.lower()
    assert "callback" in lowered
    assert "not only" in lowered or "not all" in lowered
