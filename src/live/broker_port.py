"""In-process SKCOM broker port (issue #169, Phase 1).

``BrokerPort`` is the seam every broker call crosses. The only adapter in
this phase is ``InProcessSkcomPort``: it calls the same COM methods, with
the same arguments, through ``call_com`` where the app used to. There is
no child process (Phase 2) and no order-safety change (Phase 3).

COM objects are resolved on each call from the handles callable so a test
that replaces ``run_backtest.skQ`` still sees its fake.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from enum import Enum
from typing import Callable, Protocol, runtime_checkable

from src.live.main_heartbeat import call_com

logger = logging.getLogger(__name__)


class BrokerEvent(str, Enum):
    """Events the port delivers to subscribers.

    Phase 1 fires them in-process, on the thread that already handled the
    COM callback (the Tk thread for tick, order reply, fill, and disconnect;
    the COM callback thread for KLine bars, which already touched Python).
    A listener exception is logged and does not replace the broker result.
    """

    TICK = "tick"
    BAR = "bar"
    ORDER_REPLY = "order_reply"
    FILL = "fill"
    DISCONNECT = "disconnect"


@runtime_checkable
class BrokerPort(Protocol):
    """Broker operations shared by the GUI and the headless CLI.

    Login, quote/KLine subscription, place/cancel, and the OI/fills/position
    queries are the IPC boundary. Reconnect primitives (leave, logout,
    is-connected) stay on the same object so the app's control flow can
    keep its current order and its per-call try/except.
    """

    def login(self, user_id: str, password: str, quote_flag: str = "Y"): ...

    def subscribe_quotes(self, page: int, symbol: str): ...

    def subscribe_kline(
        self, symbol, kline_type, session, trade_session,
        start_date, end_date, minute_num,
    ): ...

    def place_order(self, user_id, order, async_order: bool = False): ...

    def cancel_order(self, *args, **kwargs): ...

    def query_open_interest(self, user_id, account, fmt: int = 1): ...

    def query_fills(self, user_id, account, fmt: int = 1): ...

    def query_position(self, user_id, account, fmt: int = 1): ...

    def subscribe(self, event: BrokerEvent, callback: Callable) -> None: ...

    def emit(self, event: BrokerEvent, payload=None) -> None: ...


class InProcessSkcomPort:
    """Current behaviour: one process, the existing COM objects, ``call_com``."""

    def __init__(self, handles: Callable):
        self._handles = handles
        self._listeners: dict[BrokerEvent, list] = {event: [] for event in BrokerEvent}
        # comtypes sinks must stay referenced for the life of the process.
        self._bindings: list = []

    def _coms(self):
        return self._handles()

    # ── login / reconnect primitives ──

    def set_log_path(self, log_dir: str):
        """Not stamped. The pre-port login path called this outside ``call_com``."""
        skC, _, _, _ = self._coms()
        return skC.SKCenterLib_SetLogPath(log_dir)

    def set_authority(self, flag: int):
        """Not stamped. Same as the pre-port ``SKCenterLib_SetAuthority`` call."""
        skC, _, _, _ = self._coms()
        return skC.SKCenterLib_SetAuthority(flag)

    def login(self, user_id: str, password: str, quote_flag: str = "Y"):
        """``SKCenterLib_LoginSetQuote``. Callers still sequence the steps around it."""
        skC, _, _, _ = self._coms()
        return call_com(
            "LoginSetQuote", skC.SKCenterLib_LoginSetQuote,
            user_id, password, quote_flag)

    def supports_logout(self) -> bool:
        """Capital API 2.13.57 has no LogOut. Call it only when the object does."""
        skC, _, _, _ = self._coms()
        return hasattr(skC, "SKCenterLib_LogOut")

    def logout(self, user_id: str):
        skC, _, _, _ = self._coms()
        return call_com("LogOut", skC.SKCenterLib_LogOut, user_id)

    def is_connected(self):
        _, skQ, _, _ = self._coms()
        return call_com("IsConnected", skQ.SKQuoteLib_IsConnected)

    def leave_monitor(self):
        _, skQ, _, _ = self._coms()
        return call_com("LeaveMonitor", skQ.SKQuoteLib_LeaveMonitor)

    def connect_reply(self, user_id: str):
        _, _, skR, _ = self._coms()
        return call_com("ConnectByID", skR.SKReplyLib_ConnectByID, user_id)

    def enter_monitor(self):
        _, skQ, _, _ = self._coms()
        return call_com("EnterMonitorLONG", skQ.SKQuoteLib_EnterMonitorLONG)

    def return_code_message(self, code: int):
        """Not stamped. Callers that tolerate a missing center keep their own guard."""
        skC, _, _, _ = self._coms()
        return skC.SKCenterLib_GetReturnCodeMessage(code)

    def initialize_order(self):
        _, _, _, skO = self._coms()
        return call_com("SKOrderLib_Initialize", skO.SKOrderLib_Initialize)

    def read_cert(self, user_id: str):
        _, _, _, skO = self._coms()
        return call_com("ReadCertByID", skO.ReadCertByID, user_id)

    def get_user_account(self):
        _, _, _, skO = self._coms()
        return call_com("GetUserAccount", skO.GetUserAccount)

    # ── subscribe quotes / KLine ──

    def request_stock_list(self, market: int):
        _, skQ, _, _ = self._coms()
        return call_com(
            "RequestStockList", skQ.SKQuoteLib_RequestStockList, market)

    def subscribe_quotes(self, page: int, symbol: str):
        """``SKQuoteLib_RequestTicks`` — the live quote/tick subscription."""
        _, skQ, _, _ = self._coms()
        return call_com("RequestTicks", skQ.SKQuoteLib_RequestTicks, page, symbol)

    def subscribe_kline(
        self, symbol, kline_type, session, trade_session,
        start_date, end_date, minute_num,
    ):
        """``SKQuoteLib_RequestKLineAMByDate``."""
        _, skQ, _, _ = self._coms()
        return call_com(
            "RequestKLineAMByDate", skQ.SKQuoteLib_RequestKLineAMByDate,
            symbol, kline_type, session, trade_session,
            start_date, end_date, minute_num)

    # ── orders ──

    def new_future_order(self):
        """An empty ``FUTUREORDER``. Field assignment stays with the caller."""
        import comtypes.gen.SKCOMLib as sk
        return sk.FUTUREORDER()

    def place_order(self, user_id, order, async_order: bool = False):
        """``SendFutureOrderCLR``. ``async_order`` is False on every current caller."""
        _, _, _, skO = self._coms()
        return call_com(
            "SendFutureOrderCLR", skO.SendFutureOrderCLR,
            user_id, async_order, order)

    def cancel_order(self, *args, **kwargs):
        """Declared on the port. The in-process bot has no cancel call site.

        Phase 1 does not invent one. Order-safety across restarts is Phase 3.
        """
        raise NotImplementedError(
            "no SKCOM cancel call site exists on the in-process path")

    # ── queries: OI, fills, position ──

    def query_open_interest(self, user_id, account, fmt: int = 1):
        _, _, _, skO = self._coms()
        return call_com(
            "GetOpenInterestGW", skO.GetOpenInterestGW,
            user_id, account, fmt)

    def query_position(self, user_id, account, fmt: int = 1):
        """Position is the open-interest snapshot. One COM call, same stamp."""
        return self.query_open_interest(user_id, account, fmt)

    def query_fills(self, user_id, account, fmt: int = 1):
        _, _, _, skO = self._coms()
        return call_com(
            "GetFulfillReport", skO.GetFulfillReport,
            user_id, account, fmt)

    def query_future_rights(self, user_id, account, currency: int = 1):
        """Account equity. Not a position. Stamped ``GetFutureRights`` as before."""
        _, _, _, skO = self._coms()
        return call_com(
            "GetFutureRights", skO.GetFutureRights,
            user_id, account, currency)

    # ── events ──

    def subscribe(self, event: BrokerEvent, callback: Callable) -> None:
        self._listeners[BrokerEvent(event)].append(callback)

    def emit(self, event: BrokerEvent, payload=None) -> None:
        listeners = self._listeners.get(BrokerEvent(event))
        if not listeners:
            return
        for fn in list(listeners):
            try:
                fn(payload)
            except Exception:
                logger.exception("broker listener failed for %s", event)

    def attach_events(self, quote_event, reply_event, order_event=None) -> list:
        """Bind the existing COM sinks. Same ``GetEvents`` order as before."""
        import comtypes.client
        _, skQ, skR, skO = self._coms()
        bound = [
            quote_event,
            comtypes.client.GetEvents(skQ, quote_event),
            reply_event,
            comtypes.client.GetEvents(skR, reply_event),
        ]
        if order_event is not None and skO is not None:
            bound.append(order_event)
            bound.append(comtypes.client.GetEvents(skO, order_event))
        self._bindings.extend(bound)
        return bound


def initialize_com(bundle_root: str, project_root: str, skC, skQ, skR, skO):
    """Create the SKCOM COM objects.

    Returns ``None`` when no SDK/libs directory exists, matching the old
    early ``return`` that left the caller's globals untouched. On success
    or on a later failure, returns ``(available, skC, skQ, skR, skO)``
    with whatever objects were assigned before the error.

    The DLL-search comments below ("Phase 1/2/3") describe Windows DLL
    lookup inside this function. They are not issue #169's phases.
    """
    try:
        import ctypes
        import comtypes
        import comtypes.client

        frozen = getattr(sys, 'frozen', False)

        # Locate DLL directory: bundled libs first (deployed EXE), SDK fallback (dev)
        bundled_libs = os.path.join(bundle_root, "libs")
        sdk_libs = os.path.abspath(os.path.join(
            project_root,
            "CapitalAPI_2.13.57", "CapitalAPI_2.13.57_PythonExample",
            "SKDLLPythonTester", "libs",
        ))

        if frozen and os.path.isdir(bundled_libs):
            libs_path = bundled_libs
        elif os.path.isdir(sdk_libs):
            libs_path = sdk_libs
        elif os.path.isdir(bundled_libs):
            libs_path = bundled_libs
        else:
            print("COM not available: no SDK or bundled libs found")
            return None

        # ── Phase 1: Configure DLL search paths ──
        os.environ["PATH"] = libs_path + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(libs_path)
        except (OSError, AttributeError):
            pass
        # SetDllDirectoryW — this is what COM's LoadLibrary actually uses.
        # In frozen EXEs, PyInstaller set it to _MEIPASS; override to libs.
        kernel32 = ctypes.windll.kernel32
        kernel32.SetDllDirectoryW(libs_path)

        # ── Phase 2: Load typelib and create COM objects ──
        dll_path = os.path.join(libs_path, "SKCOM.dll")
        comtypes.client.GetModule(dll_path)
        import comtypes.gen.SKCOMLib as sk

        try:
            skC = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
        except OSError:
            # COM not registered — register from libs_path (requires admin once)
            import subprocess
            print(f"COM not registered. Attempting regsvr32 from: {libs_path}")
            reg_ok = True
            for dll_name in ("SKCOM.dll", "CTSecuritiesATL.dll"):
                dll_to_reg = os.path.join(libs_path, dll_name)
                if os.path.isfile(dll_to_reg):
                    r = subprocess.run(
                        ["regsvr32", "/s", dll_to_reg],
                        capture_output=True, text=True,
                    )
                    if r.returncode != 0:
                        print(f"  regsvr32 FAILED for {dll_name} (code {r.returncode})")
                        reg_ok = False
                    else:
                        print(f"  regsvr32 OK: {dll_name}")
                else:
                    print(f"  WARNING: {dll_name} not found in {libs_path}")
                    reg_ok = False
            if not reg_ok:
                print("COM registration failed — right-click the EXE > Run as administrator")
            try:
                skC = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
            except OSError:
                print("COM CreateObject failed after registration attempt.")
                print("Fix: right-click tai_backtest.exe > Run as administrator (once)")
                raise

        skQ = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
        skR = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
        skO = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)

        # ── Phase 3: Restore default DLL search order ──
        # None = default Windows search (app dir → System32 → Windows → PATH).
        # This lets SKCOM's internal network calls find WinHTTP, Schannel, etc.
        kernel32.SetDllDirectoryW(None)

        return True, skC, skQ, skR, skO
    except Exception as e:
        print(f"COM not available: {e}")
        traceback.print_exc()
        return False, skC, skQ, skR, skO
