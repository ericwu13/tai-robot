"""COM ``BrokerPort`` for the live link machine.

Runs on the thread that owns the COM objects (Tk main / headless STA).
The machine calls these methods from ``tick`` only. Event handlers must
not.

``leave_monitor`` is user shutdown only (§4-4-1). ``logout`` and
``restart_process`` raise. The reconnect path must not call them, and
this adapter refuses even if a mutant machine does.

No Tk import. The host passes ``call_com`` and the SKCOM objects in.
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
    ) -> None:
        self._call = call
        self._libs = libs
        self._credentials = credentials
        self._on_subscribe = on_subscribe
        self._on_reconcile = on_reconcile
        self._on_release = on_release
        self._before_call = before_call

    def _invoke(self, name: str, fn, *args):
        if self._before_call is not None:
            self._before_call(name)
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

    def logout(self) -> None:
        raise ReconnectForbidden(
            "LogOut is not a reconnect operation and is not in Capital API 2.13.57")

    def restart_process(self) -> None:
        raise ReconnectForbidden(
            "process restart is forbidden; DEGRADED waits for manual Reconnect")
