"""Thin broker port for the issue #157 reconnect machine, widened for #169.

P1 typing surface. P2 wires the link machine from ``ComBrokerPort`` +
``LinkHost``. Implementations must not be imported from here — this
module has no Tk and no COM.

The first eleven methods are the link machine's call set. ``leave_monitor``
is user shutdown only: CapitalAPI 2.13.57 §4-4-1 says LeaveMonitor drops
every Solace link, quote and reply. ``logout`` and ``restart_process``
are on the port so a test double can prove the reconnect path never
calls them. A correct machine does not, and the COM adapter raises.

The methods below that line are the in-process app calls (#169 phase 1).
They are not a revive of the #170 ``InProcessSkcomPort`` that #174
reverted. #170 hid the stamped ``call_com`` sequence used to investigate
the #157 ``EnterMonitorLONG`` access violation, called ``LogOut`` when
the object happened to have the attribute, and moved DLL search into
the port. Those three stay out: stamps keep their old names, ``logout``
still raises, and COM object creation stays in ``run_backtest._init_com``.
"""

from __future__ import annotations

from typing import Protocol

# Code default. Not read from settings.yaml. Only this mode exists;
# a child process is a later phase and must not be selected by a file.
BROKER_MODE_INPROCESS = "inprocess"
DEFAULT_BROKER_MODE = BROKER_MODE_INPROCESS


def require_inprocess(mode: str | None = None) -> str:
    """Return ``inprocess`` or raise. ``None`` means the code default."""
    chosen = DEFAULT_BROKER_MODE if mode is None else mode
    if chosen != BROKER_MODE_INPROCESS:
        raise RuntimeError(
            f"broker_mode {chosen!r} is not implemented; "
            f"only {BROKER_MODE_INPROCESS!r} exists")
    return chosen


class BrokerPort(Protocol):
    """SKCOM operations the link state machine may invoke.

    Event handlers must not call these. The host runs the machine from
    the main loop, on the thread that created the COM objects.
    """

    def login(self) -> int:
        """``SKCenterLib_LoginSetQuote``. Once per process, plus a
        not-logged-in recovery. ``2003`` means the login is already valid.
        """

    def enter_monitor(self) -> int:
        """``SKQuoteLib_EnterMonitorLONG``. Quote link only."""

    def leave_monitor(self) -> int:
        """``SKQuoteLib_LeaveMonitor``. User shutdown only (§4-4-1)."""

    def connect_reply(self, user_id: str) -> int:
        """``SKReplyLib_ConnectByID`` for one account."""

    def is_connected(self) -> int:
        """``SKQuoteLib_IsConnected``. ``0`` means the quote link is down."""

    def is_connected_by_id(self, user_id: str) -> int:
        """``SKReplyLib_IsConnectedByID``. State query (§4-3-3)."""

    def subscribe_quotes(self) -> None:
        """Resubscribe every quote. Legal only after ``3003`` (§4-4-2)."""

    def reconcile_positions(self) -> None:
        """Open interest and fills. Required before orders resume."""

    def release_orders(self) -> None:
        """Allow orders after OnComplete and a position reconcile."""

    def logout(self) -> None:
        """Not a reconnect operation. 2.13.57 has no working LogOut."""

    def restart_process(self) -> None:
        """Not a reconnect operation. The process never relaunches itself."""

    # ── in-process app surface (#169). The link machine does not call these.

    def set_log_path(self, log_dir: str):
        """``SKCenterLib_SetLogPath``. Unstamped, as on the login path."""

    def set_authority(self, flag: int):
        """``SKCenterLib_SetAuthority``. Unstamped, as on the login path."""

    def login_set_quote(self, user_id: str, password: str, quote_flag: str = "Y"):
        """Process-start ``SKCenterLib_LoginSetQuote``. Not the machine's ``login``."""

    def connect_by_id(self, user_id: str):
        """Process-start ``SKReplyLib_ConnectByID``. No reconnect before-call log."""

    def enter_monitor_long(self):
        """Process-start ``SKQuoteLib_EnterMonitorLONG``. No reconnect before-call log."""

    def initialize_order(self):
        """``SKOrderLib_Initialize``."""

    def read_cert(self, user_id: str):
        """``ReadCertByID``."""

    def get_user_account(self):
        """``GetUserAccount``."""

    def is_quote_connected(self):
        """``SKQuoteLib_IsConnected`` for the app probes. Raw return, no before-call log."""

    def read_is_connected_by_id(self, user_id: str):
        """``SKReplyLib_IsConnectedByID`` for the reply poll. Raw return."""

    def request_stock_list(self, market: int):
        """``SKQuoteLib_RequestStockList``."""

    def request_ticks(self, page: int, symbol: str):
        """``SKQuoteLib_RequestTicks``. Returns the raw COM result."""

    def request_kline(
        self, symbol, kline_type, session, trade_session,
        start_date, end_date, minute_num,
    ):
        """``SKQuoteLib_RequestKLineAMByDate``."""

    def get_open_interest(self, user_id: str, account: str, fmt: int = 1):
        """``GetOpenInterestGW``."""

    def get_future_rights(self, user_id: str, account: str, currency: int = 1):
        """``GetFutureRights``."""

    def get_fulfill_report(self, user_id: str, account: str, fmt: int = 1):
        """``GetFulfillReport``. Callers pass format 1."""

    def place_order(self, user_id, order, async_order: bool = False):
        """``SendFutureOrderCLR(user_id, async_order, order)``. ``async_order`` is False."""

    def return_code_message(self, code):
        """``SKCenterLib_GetReturnCodeMessage``, stamped ``GetReturnCodeMessage``.

        ``None`` when the center or the method is missing. No call in that case.
        """

    def enqueue_ui(self, item) -> None:
        """One ``_ui_queue`` put. The sink's try/except stays with the caller."""

    def enqueue_tick(self, item) -> None:
        """One ``_tick_queue`` put from the COM callback thread."""

    def schedule_on_main(self, root, delay_ms: int, callback) -> None:
        """``root.after`` on the calling thread. KLine complete uses 100 ms."""

    def attach_events(
        self, handlers_out: list, quote_handler, reply_handler, order_handler,
        bind, get_events,
    ) -> None:
        """Bind quote, then reply, then order. Append before each bind."""
