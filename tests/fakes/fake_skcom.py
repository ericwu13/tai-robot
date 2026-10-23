"""Fake SKCOM that faults on the #157 reconnect-contract violations.

The real crashes were access violations inside SKCOM after a call
sequence the vendor does not document. This double records every call
and raises ``SimulatedAV`` for the sequences P1 must make unreachable:

* any SKCOM call from inside an event handler (handlers only set flags;
  §4-4-a forbids EnterMonitor and LeaveMonitor there)
* ``subscribe_quotes`` before 3003 STOCKS_READY (§4-4-2)
* quote ``EnterMonitorLONG`` after 3033 before the reply link has been
  rebuilt, or ``ConnectByID`` before ``OnSolaceReplyDisconnect``
* ``LeaveMonitor`` except a test-armed user shutdown (§4-4-1 drops
  every link, so it is never a reconnect step)
* ``EnterMonitorLONG`` while a reply rebuild is in flight
* ``ConnectByID`` inside the reply settle window, or while
  ``IsConnectedByID`` is not 0
* ``EnterMonitorLONG`` while ``IsConnected`` is 1 or 2
* ``Login`` / ``LogOut`` on the reconnect path, or a second login while
  the process is already logged in
* orders released before OnComplete and a position reconcile
* any process restart

``IsConnected`` / ``IsConnectedByID`` reads are cheap and legal. The
*gates* that refuse a reconnect call based on those reads are the
machine's safety design; this fake still faults if the call is made.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta


# Codes the machine and the fake agree on. Not a COM import.
LOGIN_ALREADY = 2003
API_UPDATE_REQUIRED = 9996


class SimulatedAV(RuntimeError):
    """Stand-in for the SKCOM access violation those call orders hit."""


class FakeSkcom:
    """In-memory SKCOM. ``now`` is the test clock; the machine does not set it."""

    def __init__(self, reply_settle_s: float = 10.0) -> None:
        self.reply_settle_s = float(reply_settle_s)
        self.now: datetime | None = None
        self.calls: list[str] = []
        self.logged_in = False
        self.pending_login_recovery = False
        self.reconnect_episode = False
        self.inside_event = False
        self.user_shutdown_armed = False

        self.quote_state = 0  # 0 down, 1 up, 2 connecting
        self.quote_inflight = False
        self.force_quote_state: int | None = None
        self.stocks_ready = False
        self.subscribed = False

        self.reply_up = False
        self.reply_ever_connected = False
        self.reply_rebuild_inflight = False
        self.reply_disconnect_at: datetime | None = None
        self.solace_session_down = False
        self.reply_rebuilt_after_solace_down = False
        self.teardown_until: datetime | None = None
        self.hold_reply_connected = False
        self.held_reply_state = 1

        self.complete_seen = False
        self.reconciled = False
        self.orders_released = False

        self.login_codes: list[int] = []
        self.connect_codes: list[int] = []
        self.enter_codes: list[int] = []

    # ── clock / event scope ──────────────────────────────────────────

    def set_now(self, now: datetime) -> None:
        self.now = now

    @contextmanager
    def handling_event(self):
        """Wrap a machine event handler. Any SKCOM call inside faults."""
        if self.inside_event:
            raise SimulatedAV("re-entered an SKCOM event handler")
        self.inside_event = True
        try:
            yield
        finally:
            self.inside_event = False

    def arm_user_shutdown(self) -> None:
        """The test arms this before a user shutdown. Reconnect never does."""
        self.user_shutdown_armed = True

    def _reject_if_in_event(self, what: str) -> None:
        if self.inside_event:
            raise SimulatedAV(f"{what} inside OnConnection")

    def _require_now(self) -> datetime:
        if self.now is None:
            raise SimulatedAV("fake clock was not set")
        return self.now

    @staticmethod
    def _pop(queue: list[int], default: int = 0) -> int:
        if queue:
            return queue.pop(0)
        return default

    # ── world events the test injects (not SKCOM calls) ─────────────

    def note_3033(self, now: datetime) -> None:
        """OnConnection 3033: shared Solace session is down."""
        self.now = now
        self.solace_session_down = True
        self.reply_rebuilt_after_solace_down = False
        self.reconnect_episode = True
        self.quote_state = 0
        self.quote_inflight = False
        self.stocks_ready = False
        self.subscribed = False
        self.reply_up = False
        self.reply_rebuild_inflight = False
        # The previous OnSolaceReplyDisconnect does not authorize this
        # episode. ConnectByID stays illegal until a fresh disconnect.
        self.reply_disconnect_at = None
        self.complete_seen = False
        self.reconciled = False
        self.orders_released = False

    def note_quote_down(self, now: datetime) -> None:
        """Quote-side 3021. Does not by itself drop the reply link."""
        self.now = now
        self.reconnect_episode = True
        self.quote_state = 0
        self.quote_inflight = False
        self.stocks_ready = False
        self.subscribed = False

    def note_solace_reply_disconnect(self, now: datetime) -> None:
        self.now = now
        self.reply_disconnect_at = now
        self.reconnect_episode = True
        self.reply_up = False
        self.reply_rebuild_inflight = False
        self.complete_seen = False
        self.reconciled = False
        self.orders_released = False

    def note_3003(self) -> None:
        """STOCKS_READY. Subscribe stays illegal until this runs."""
        if self.quote_inflight or self.quote_state == 2:
            self.quote_state = 1
            self.quote_inflight = False
            self.stocks_ready = True

    def note_on_complete(self) -> None:
        """OnComplete: reply 回補完成 (§4-3-c)."""
        self.complete_seen = True
        self.reply_rebuild_inflight = False
        self.reply_up = True
        self.reply_ever_connected = True
        if self.solace_session_down:
            self.reply_rebuilt_after_solace_down = True

    # ── reported state ───────────────────────────────────────────────

    def _quote_reported(self) -> int:
        if self.force_quote_state is not None:
            return self.force_quote_state
        return self.quote_state

    def _reply_reported(self) -> int:
        if self.hold_reply_connected:
            return self.held_reply_state
        now = self.now
        if (
            self.teardown_until is not None
            and now is not None
            and now < self.teardown_until
        ):
            return 2
        # Settle is a timer the machine enforces (§4-3-h). It is not
        # implied by this state query, so a dropped reply reports 0
        # once the fake's teardown window (if any) has passed. Calling
        # ConnectByID inside the settle window still faults.
        if self.reply_disconnect_at is not None and not self.reply_up:
            return 0
        if self.reply_rebuild_inflight:
            return 2
        if self.reply_up:
            return 1
        return 0

    def _initial_reply_connect(self) -> bool:
        return (
            not self.reply_ever_connected
            and not self.solace_session_down
            and self.reply_disconnect_at is None
            and not self.reconnect_episode
        )

    def _note_not_logged_in(self) -> None:
        self.logged_in = False
        self.pending_login_recovery = True
        self.quote_state = 0
        self.quote_inflight = False
        self.stocks_ready = False
        self.reply_rebuild_inflight = False
        self.reply_up = False

    # ── port ─────────────────────────────────────────────────────────

    def login(self) -> int:
        self._reject_if_in_event("LoginSetQuote")
        self.calls.append("LoginSetQuote")
        recovery = self.pending_login_recovery
        if self.logged_in and not recovery:
            raise SimulatedAV("login once per process")
        if self.reconnect_episode and not recovery:
            raise SimulatedAV("Login on the reconnect path")
        code = self._pop(self.login_codes, 0)
        if code in (0, LOGIN_ALREADY):
            self.logged_in = True
            self.pending_login_recovery = False
            return code
        self.logged_in = False
        return code

    def enter_monitor(self) -> int:
        self._reject_if_in_event("EnterMonitorLONG")
        self.calls.append("EnterMonitorLONG")
        reported = self._quote_reported()
        if reported in (1, 2):
            raise SimulatedAV(
                f"EnterMonitorLONG while IsConnected is {reported}"
            )
        if self.reply_rebuild_inflight:
            raise SimulatedAV(
                "EnterMonitorLONG while a reply rebuild is in flight"
            )
        if self.solace_session_down and not self.reply_rebuilt_after_solace_down:
            raise SimulatedAV(
                "3033 shared Solace session: quote EnterMonitorLONG "
                "before the reply link is reconnected"
            )
        if (
            self.teardown_until is not None
            and self.now is not None
            and self.now < self.teardown_until
        ):
            raise SimulatedAV(
                "EnterMonitorLONG during reply teardown"
            )
        code = self._pop(self.enter_codes, 0)
        if 1000 <= code < 2000:
            self._note_not_logged_in()
            return code
        if code in (0, LOGIN_ALREADY):
            self.quote_state = 2
            self.quote_inflight = True
            self.stocks_ready = False
            return 0
        return code

    def leave_monitor(self) -> int:
        self._reject_if_in_event("LeaveMonitor")
        self.calls.append("LeaveMonitor")
        # §4-4-1: this drops quote and reply together. Reconnect must
        # not be the caller. The test arms a real user shutdown.
        if not self.user_shutdown_armed:
            raise SimulatedAV(
                "LeaveMonitor drops all links; forbidden on reconnect"
            )
        self.user_shutdown_armed = False
        self.quote_state = 0
        self.quote_inflight = False
        self.stocks_ready = False
        self.subscribed = False
        self.reply_up = False
        self.reply_rebuild_inflight = False
        self.solace_session_down = True
        self.reply_rebuilt_after_solace_down = False
        return 0

    def connect_reply(self, user_id: str) -> int:
        self._reject_if_in_event("ConnectByID")
        self.calls.append("ConnectByID")
        if not user_id:
            raise SimulatedAV("ConnectByID missing user id")
        if self.quote_inflight:
            raise SimulatedAV(
                "ConnectByID while quote EnterMonitor is in flight"
            )
        if self.reply_rebuild_inflight:
            raise SimulatedAV(
                "ConnectByID re-called while a reply rebuild is in flight"
            )
        if not self._initial_reply_connect():
            if self.reply_disconnect_at is None and (
                self.solace_session_down or self.reply_ever_connected
            ):
                raise SimulatedAV(
                    "ConnectByID before OnSolaceReplyDisconnect"
                )
            if self.reply_disconnect_at is not None:
                elapsed = (
                    self._require_now() - self.reply_disconnect_at
                ).total_seconds()
                if elapsed < self.reply_settle_s:
                    raise SimulatedAV(
                        "ConnectByID within reply settle of "
                        "OnSolaceReplyDisconnect"
                    )
            if (
                self.teardown_until is not None
                and self.now is not None
                and self.now < self.teardown_until
            ):
                raise SimulatedAV("ConnectByID during reply teardown")
            if self._reply_reported() != 0:
                raise SimulatedAV(
                    "ConnectByID while IsConnectedByID != 0"
                )
        code = self._pop(self.connect_codes, 0)
        if 1000 <= code < 2000:
            self._note_not_logged_in()
            return code
        if code in (0, LOGIN_ALREADY):
            self.reply_rebuild_inflight = True
            return 0
        return code

    def is_connected(self) -> int:
        self._reject_if_in_event("IsConnected")
        self.calls.append("IsConnected")
        return self._quote_reported()

    def is_connected_by_id(self, user_id: str) -> int:
        self._reject_if_in_event("IsConnectedByID")
        self.calls.append("IsConnectedByID")
        if not user_id:
            raise SimulatedAV("IsConnectedByID missing user id")
        return self._reply_reported()

    def subscribe_quotes(self) -> None:
        self._reject_if_in_event("SubscribeQuotes")
        self.calls.append("SubscribeQuotes")
        if not self.stocks_ready:
            raise SimulatedAV(
                "subscribe before 3003 STOCKS_READY"
            )
        self.subscribed = True

    def reconcile_positions(self) -> None:
        self._reject_if_in_event("Reconcile")
        self.calls.append("Reconcile")
        if not self.complete_seen:
            raise SimulatedAV("reconcile before OnComplete")
        self.reconciled = True

    def release_orders(self) -> None:
        self._reject_if_in_event("ReleaseOrders")
        self.calls.append("ReleaseOrders")
        if not self.complete_seen or not self.reconciled:
            raise SimulatedAV(
                "orders resumed before OnComplete and position reconcile"
            )
        self.orders_released = True

    def logout(self) -> None:
        self._reject_if_in_event("LogOut")
        self.calls.append("LogOut")
        raise SimulatedAV("LogOut on the reconnect path")

    def restart_process(self) -> None:
        self._reject_if_in_event("RestartProcess")
        self.calls.append("RestartProcess")
        raise SimulatedAV("process restart is forbidden")


def advance(now: datetime, seconds: float) -> datetime:
    return now + timedelta(seconds=seconds)
