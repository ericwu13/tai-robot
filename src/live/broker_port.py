"""Thin broker port for the issue #157 reconnect machine.

P1 only. The live GUI and the headless bot do not call this yet; P2
is the switchover and is on hold. Implementations must not be imported
from here — this module is a typing surface with no Tk and no COM.

The call set is the one the reconnect machine is allowed to make.
``leave_monitor`` is user shutdown only: CapitalAPI 2.13.57 §4-4-1 says
LeaveMonitor drops every Solace link, quote and reply. ``logout`` and
``restart_process`` are on the port so a test double can prove the
reconnect path never calls them. A correct machine does not.
"""

from __future__ import annotations

from typing import Protocol


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
