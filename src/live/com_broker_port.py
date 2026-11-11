"""COM ``BrokerPort`` for the live link machine and the in-process app.

Runs on the thread that owns the COM objects (Tk main / headless STA).
The machine calls the reconnect methods from ``tick`` only. Event
handlers must not call those.

``leave_monitor`` is user shutdown only (§4-4-1). ``logout`` and
``restart_process`` raise. The reconnect path must not call them, and
this adapter refuses even if a mutant machine does.

App methods (login quote, orders, quotes, queue puts) go through
``_direct``: the same ``call_com`` stamp, no ``[RECONNECT] about to call``
line. ``SetLogPath`` and ``SetAuthority`` stay unstamped. There is no
``LogOut`` call.

No Tk import and no comtypes import. The host passes ``call_com``, the
SKCOM objects, the queues, and ``GetEvents``.
"""

from __future__ import annotations

from collections.abc import Callable


class ReconnectForbidden(RuntimeError):
    """logout / process restart are not reconnect operations."""


class ComBrokerPort:
    """Map ``BrokerPort`` onto ``call_com`` + ``skC`` / ``skQ`` / ``skR``.

    ``libs()`` returns ``(skC, skQ, skR, skO)`` at call time so a test
    can replace the module globals before the call.

    ``credentials()`` returns ``(user_id, password)`` for the one
    not-logged-in recovery login. The password is not logged.
    """

    def __init__(
        self,
        *,
        call: Callable,
        libs: Callable[[], tuple],
        credentials: Callable[[], tuple[str, str]],
        on_subscribe: Callable[[], None],
        on_reconcile: Callable[[], None],
        on_release: Callable[[], None],
        before_call: Callable[[str], None] | None = None,
        ui_queue: Callable[[], object] | None = None,
        tick_queue: Callable[[], object] | None = None,
    ) -> None:
        self._call = call
        self._libs = libs
        self._credentials = credentials
        self._on_subscribe = on_subscribe
        self._on_reconcile = on_reconcile
        self._on_release = on_release
        self._before_call = before_call
        self._ui_queue = ui_queue
        self._tick_queue = tick_queue

    def _invoke(self, name: str, fn, *args):
        if self._before_call is not None:
            self._before_call(name)
        return self._call(name, fn, *args)

    def _direct(self, name: str, fn, *args):
        """Stamped call without the reconnect about-to-call log."""
        return self._call(name, fn, *args)

    def login(self) -> int:
        skc, _skq, _skr, _sko = self._libs()
        user_id, password = self._credentials()
        code = self._invoke(
            "LoginSetQuote", skc.SKCenterLib_LoginSetQuote,
            user_id, password, "Y")
        return int(code)

    def enter_monitor(self) -> int:
        _skc, skq, _skr, _sko = self._libs()
        return int(self._invoke(
            "EnterMonitorLONG", skq.SKQuoteLib_EnterMonitorLONG))

    def leave_monitor(self) -> int:
        """User shutdown only. The machine calls this from ``shutdown``."""
        _skc, skq, _skr, _sko = self._libs()
        return int(self._invoke(
            "LeaveMonitor", skq.SKQuoteLib_LeaveMonitor))

    def connect_reply(self, user_id: str) -> int:
        _skc, _skq, skr, _sko = self._libs()
        return int(self._invoke(
            "ConnectByID", skr.SKReplyLib_ConnectByID, user_id))

    def is_connected(self) -> int:
        _skc, skq, _skr, _sko = self._libs()
        return int(self._invoke("IsConnected", skq.SKQuoteLib_IsConnected))

    def is_connected_by_id(self, user_id: str) -> int:
        _skc, _skq, skr, _sko = self._libs()
        return int(self._invoke(
            "IsConnectedByID", skr.SKReplyLib_IsConnectedByID, user_id))

    def subscribe_quotes(self) -> None:
        """Host resubscribes ticks. Legal only after the machine saw 3003."""
        self._on_subscribe()

    def reconcile_positions(self) -> None:
        """Ask the host to refresh open interest. The machine then releases."""
        self._on_reconcile()

    def release_orders(self) -> None:
        self._on_release()

    # ── in-process app calls. No before_call. Raw COM return values. ──

    def set_log_path(self, log_dir: str):
        skc, _skq, _skr, _sko = self._libs()
        return skc.SKCenterLib_SetLogPath(log_dir)

    def set_authority(self, flag: int):
        skc, _skq, _skr, _sko = self._libs()
        return skc.SKCenterLib_SetAuthority(flag)

    def login_set_quote(self, user_id: str, password: str, quote_flag: str = "Y"):
        skc, _skq, _skr, _sko = self._libs()
        return self._direct(
            "LoginSetQuote", skc.SKCenterLib_LoginSetQuote,
            user_id, password, quote_flag)

    def connect_by_id(self, user_id: str):
        _skc, _skq, skr, _sko = self._libs()
        return self._direct(
            "ConnectByID", skr.SKReplyLib_ConnectByID, user_id)

    def enter_monitor_long(self):
        _skc, skq, _skr, _sko = self._libs()
        return self._direct(
            "EnterMonitorLONG", skq.SKQuoteLib_EnterMonitorLONG)

    def initialize_order(self):
        _skc, _skq, _skr, sko = self._libs()
        return self._direct(
            "SKOrderLib_Initialize", sko.SKOrderLib_Initialize)

    def read_cert(self, user_id: str):
        _skc, _skq, _skr, sko = self._libs()
        return self._direct("ReadCertByID", sko.ReadCertByID, user_id)

    def get_user_account(self):
        _skc, _skq, _skr, sko = self._libs()
        return self._direct("GetUserAccount", sko.GetUserAccount)

    def is_quote_connected(self):
        _skc, skq, _skr, _sko = self._libs()
        return self._direct("IsConnected", skq.SKQuoteLib_IsConnected)

    def read_is_connected_by_id(self, user_id: str):
        _skc, _skq, skr, _sko = self._libs()
        return self._direct(
            "IsConnectedByID", skr.SKReplyLib_IsConnectedByID, user_id)

    def request_stock_list(self, market: int):
        _skc, skq, _skr, _sko = self._libs()
        return self._direct(
            "RequestStockList", skq.SKQuoteLib_RequestStockList, market)

    def request_ticks(self, page: int, symbol: str):
        _skc, skq, _skr, _sko = self._libs()
        return self._direct(
            "RequestTicks", skq.SKQuoteLib_RequestTicks, page, symbol)

    def request_kline(
        self, symbol, kline_type, session, trade_session,
        start_date, end_date, minute_num,
    ):
        _skc, skq, _skr, _sko = self._libs()
        return self._direct(
            "RequestKLineAMByDate", skq.SKQuoteLib_RequestKLineAMByDate,
            symbol, kline_type, session, trade_session,
            start_date, end_date, minute_num)

    def get_open_interest(self, user_id: str, account: str, fmt: int = 1):
        _skc, _skq, _skr, sko = self._libs()
        return self._direct(
            "GetOpenInterestGW", sko.GetOpenInterestGW,
            user_id, account, fmt)

    def get_future_rights(self, user_id: str, account: str, currency: int = 1):
        _skc, _skq, _skr, sko = self._libs()
        return self._direct(
            "GetFutureRights", sko.GetFutureRights,
            user_id, account, currency)

    def get_fulfill_report(self, user_id: str, account: str, fmt: int = 1):
        _skc, _skq, _skr, sko = self._libs()
        return self._direct(
            "GetFulfillReport", sko.GetFulfillReport,
            user_id, account, fmt)

    def place_order(self, user_id, order, async_order: bool = False):
        _skc, _skq, _skr, sko = self._libs()
        return self._direct(
            "SendFutureOrderCLR", sko.SendFutureOrderCLR,
            user_id, async_order, order)

    def return_code_message(self, code):
        skc, _skq, _skr, _sko = self._libs()
        if skc is None:
            return None
        fn = getattr(skc, "SKCenterLib_GetReturnCodeMessage", None)
        if not callable(fn):
            return None
        return self._direct("GetReturnCodeMessage", fn, code)

    def enqueue_ui(self, item) -> None:
        self._ui_queue().put_nowait(item)

    def enqueue_tick(self, item) -> None:
        self._tick_queue().put_nowait(item)

    def schedule_on_main(self, root, delay_ms: int, callback) -> None:
        root.after(delay_ms, callback)

    def attach_events(
        self, handlers_out: list, quote_handler, reply_handler, order_handler,
        bind, get_events,
    ) -> None:
        """Quote, then reply, then order. Each handler is stored before its bind."""
        _skc, skq, skr, sko = self._libs()
        handlers_out.append(quote_handler)
        handlers_out.append(bind("quote", skq, quote_handler, get_events))
        handlers_out.append(reply_handler)
        handlers_out.append(bind("reply", skr, reply_handler, get_events))
        if order_handler is not None:
            handlers_out.append(order_handler)
            handlers_out.append(bind("order", sko, order_handler, get_events))

    def logout(self) -> None:
        raise ReconnectForbidden(
            "LogOut is not a reconnect operation and is not in Capital API 2.13.57")

    def restart_process(self) -> None:
        raise ReconnectForbidden(
            "process restart is forbidden; DEGRADED waits for manual Reconnect")
