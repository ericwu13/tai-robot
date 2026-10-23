"""Pure quote/reply reconnect machine for issue #157.

No Tk, no COM, no session clock. The host (P2, not this change) feeds
events and a clock in, and performs the ``BrokerPort`` calls this
machine returns to on ``tick``. Event methods only queue flags. Every
SKCOM call happens from ``tick``, which does not sleep and does not
block waiting for 3003 or OnComplete.

Vendor rules used here (CapitalAPI 2.13.57):

* §4-4-a — do not call EnterMonitor or LeaveMonitor inside OnConnection.
* §4-4-2 — subscribe only after STOCKS_READY (3003).
* §4-4-1 — LeaveMonitor drops every Solace link. Shutdown only.
* §4-3-h — on OnSolaceReplyDisconnect the underlying disconnect is not
  finished. Do not reconnect inside the event. Wait a few seconds,
  then ConnectByID.
* 3033 is the shared Solace session down, so both links go down and
  the reply link is reconnected first.
* 2003 (SK_WARNING_LOGIN_ALREADY) means the login is still valid.
* 9996 (SK_ERROR_UPDATE_API_REQUIRED) means this API build cannot log in.
* OnComplete (§4-3-c) means the reply backfill finished.

The IsConnected()==0 gate, the IsConnectedByID==0 gate, and the
single-in-flight rule are OUR safety design, not vendor rules. The
2.13.57 docs describe those two calls as state queries only.

Login runs once per process. A later login is allowed only after a
call returns a not-logged-in code (1xxx, the 1108 seen on 10-12) and
both links are down and idle. Three login failures, a stuck link, or
9996 enter DEGRADED: stay disconnected, alert, block entries, probe
``IsConnected`` every 60s, and wait for ``manual_reconnect``. This
machine never restarts the process. The vendor note that five login
failures require an API restart is deliberately not followed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from src.live.broker_port import BrokerPort


# OnConnection kinds. 3001/3002 are intermediate "up" events, not drops.
KIND_REPLY = 3001
KIND_QUOTE = 3002
KIND_READY = 3003
KIND_CONN_ERROR = 3021
KIND_SOLACE_DOWN = 3033

LOGIN_ALREADY = 2003
API_UPDATE_REQUIRED = 9996


class LinkPhase(Enum):
    IDLE = "idle"
    READY = "ready"
    DOWN = "down"
    CONNECTING = "connecting"


class LoginPhase(Enum):
    NOT_LOGGED_IN = "not_logged_in"
    LOGGED_IN = "logged_in"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class MachineRules:
    """Each flag defaults to the safe rule. One mutant flips one flag.

    The flags are the rules under test. Turning one off makes ``tick``
    or an event method perform the call the fake SKCOM faults on.
    """

    events_flag_only: bool = True
    subscribe_after_3003: bool = True
    solace_3033_both_links_reply_first: bool = True
    leave_monitor_reconnect_forbidden: bool = True
    login_once: bool = True
    logout_forbidden: bool = True
    quote_is_connected_gate: bool = True
    enter_once_while_connecting: bool = True
    reply_settle: bool = True
    reply_is_connected_by_id_gate: bool = True
    one_link_at_a_time: bool = True
    orders_after_complete_and_reconcile: bool = True
    degraded_instead_of_restart: bool = True
    # A finished or degraded 3033 must not authorize the next one.
    reset_3033_episode: bool = True
    # One ConnectByID until OnComplete. No retry while that call is in flight.
    reply_connect_once_until_complete: bool = True
    # 3021 → 1108 → login OK, reply still down, no disconnect event:
    # one settle, then one ConnectByID. Mutant connects immediately.
    stuck_after_login_recovery: bool = True
    # Manual Reconnect restarts the stuck window instead of ConnectByID.
    manual_reconnect_clears_stuck: bool = True
    # Reply still down, and no 3033/1108 clock is running: arm one.
    # Do not ConnectByID while the previous rebuild has no OnComplete.
    manual_reconnect_arms_reply_stuck: bool = True
    # Late OnComplete after degrade marks reply up. No second ConnectByID.
    late_complete_without_reconnect: bool = True
    # IsConnectedByID==1 after degrade marks reply up. No ConnectByID.
    connected_id_without_reconnect: bool = True
    # Manual Reconnect on the login wedge waits out settle, then one connect.
    manual_login_wedge_settles: bool = True


@dataclass
class LinkMachineConfig:
    """Timeouts. The reply settle matches §4-3-h ("a few seconds").

    ``quote_settle_s``, ``connecting_timeout_s``, ``stuck_limit_s`` and
    the probe interval are OUR timing choices. They are not a session
    clock and they do not special-case the open.
    """

    user_id: str = "USER"
    quote_settle_s: float = 5.0
    reply_settle_s: float = 10.0
    connecting_timeout_s: float = 60.0
    stuck_limit_s: float = 300.0
    probe_interval_s: float = 60.0
    login_failure_cap: int = 3
    login_retry_s: float = 5.0


@dataclass
class _Queued:
    name: str
    now: datetime
    kind: int = 0
    code: int = 0


@dataclass
class LinkStateMachine:
    """Quote link, reply link, and login, driven by flags plus ``tick``."""

    port: BrokerPort
    config: LinkMachineConfig = field(default_factory=LinkMachineConfig)
    rules: MachineRules = field(default_factory=MachineRules)

    quote: LinkPhase = LinkPhase.IDLE
    reply: LinkPhase = LinkPhase.IDLE
    login: LoginPhase = LoginPhase.NOT_LOGGED_IN

    degraded: bool = False
    degraded_reason: str = ""
    entries_blocked: bool = False
    orders_paused: bool = True
    reply_reconciled: bool = False
    login_failures: int = 0
    alerts: list[str] = field(default_factory=list)

    _started: bool = False
    _shut_down: bool = False
    _login_permitted: bool = False
    _login_recovery: bool = False
    _next_login_at: datetime | None = None
    _backoff_until: datetime | None = None

    _inbox: list[_Queued] = field(default_factory=list)
    _saw_3033: bool = False
    _solace_down_at: datetime | None = None
    _saw_reply_disconnect: bool = False
    _reply_down_at: datetime | None = None
    _quote_down_at: datetime | None = None
    _quote_connect_started_at: datetime | None = None
    _reply_connect_started_at: datetime | None = None
    _pending_3003: bool = False
    _pending_complete: bool = False
    _quote_stuck_since: datetime | None = None
    _reply_stuck_since: datetime | None = None
    _last_probe_at: datetime | None = None
    _reply_awaiting_complete: bool = False
    _down_without_reply_event_since: datetime | None = None
    _login_wedge_settle_at: datetime | None = None
    _settle_before_stuck: bool = False

    # ── events: flags only ───────────────────────────────────────────

    def on_connection(self, kind: int, code: int, now: datetime) -> None:
        """Queue an OnConnection kind. Does not call the broker.

        §4-4-a: EnterMonitor and LeaveMonitor must not run here. The
        main loop (`tick`) is the only place that calls them.
        """
        self._inbox.append(_Queued("connection", now, kind, code))
        if not self.rules.events_flag_only:
            # Mutant: call EnterMonitor from the event, which §4-4-a forbids.
            self.port.enter_monitor()

    def on_solace_reply_disconnect(self, now: datetime) -> None:
        """Queue OnSolaceReplyDisconnect. §4-3-h: do not reconnect here."""
        self._inbox.append(_Queued("reply_disconnect", now))

    def on_complete(self, now: datetime) -> None:
        """Queue OnComplete (§4-3-c). Orders stay paused until ``tick``."""
        self._inbox.append(_Queued("complete", now))

    def start(self, now: datetime) -> None:
        """Process start. One LoginSetQuote. No link calls."""
        if self._started:
            return
        self._started = True
        self._login_permitted = True
        self._attempt_login(now)

    def shutdown(self, now: datetime) -> None:
        """User shutdown. The only ``LeaveMonitor`` call site.

        §4-4-1: LeaveMonitor drops every Solace link, quote and reply.
        Reconnect never reaches this method.
        """
        del now
        if self._shut_down:
            return
        self._shut_down = True
        self.port.leave_monitor()
        self.quote = LinkPhase.DOWN
        self.reply = LinkPhase.DOWN
        self.orders_paused = True
        self._pending_3003 = False

    def manual_reconnect(self, now: datetime) -> None:
        """Leave DEGRADED and re-enter this same machine.

        No LeaveMonitor, no Login, no process restart. The next
        ``tick`` follows the same gates as any other attempt.

        ``entries_blocked`` stays set until both links are up and
        positions have been reconciled. A ConnectByID that is still
        waiting for OnComplete is not sent again. The 1108 wedge,
        which can miss OnSolaceReplyDisconnect, waits out the reply
        settle from this press and then sends one ConnectByID.
        """
        if not self.degraded:
            return
        self.degraded = False
        self.degraded_reason = ""
        self._quote_stuck_since = None
        self._reply_stuck_since = None
        self.login_failures = 0
        self._next_login_at = None
        if self.login == LoginPhase.DEGRADED:
            self.login = LoginPhase.NOT_LOGGED_IN
            self._login_permitted = True
        self.orders_paused = not (
            self.reply == LinkPhase.READY and self.reply_reconciled
        )
        if self.quote == LinkPhase.CONNECTING:
            self.quote = LinkPhase.DOWN
            if self._quote_down_at is None:
                self._quote_down_at = now
        # The stuck window that just fired must not still be expired.
        # Restart it from this press. Do not call ConnectByID here.
        if not self.rules.manual_reconnect_clears_stuck:
            self.port.connect_reply(self.config.user_id)
            return
        if self._saw_3033:
            self._solace_down_at = now
        if self._down_without_reply_event_since is not None:
            self._down_without_reply_event_since = now
        self._arm_reply_stuck_after_manual(now)

    def snapshot(self) -> dict:
        """State a later heartbeat can publish. This method writes nothing."""
        return {
            "quote": self.quote.value,
            "reply": self.reply.value,
            "login": self.login.value,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "entries_blocked": self.entries_blocked,
            "orders_paused": self.orders_paused,
            "login_failures": self.login_failures,
            "alerts": list(self.alerts),
        }

    # ── main loop ────────────────────────────────────────────────────

    def tick(self, now: datetime) -> None:
        """One non-blocking step. At most one link call, unless a mutant
        turns the single-in-flight rule off.
        """
        if self._shut_down:
            return
        self._tick_body(now)
        if not self.degraded:
            self._maybe_unblock_entries()

    def _tick_body(self, now: datetime) -> None:
        if self.degraded:
            self._drain(now)
            if not self._recover_late_reply(now):
                self._probe(now)
                return

        self._drain(now)
        self._forbidden_reconnect_calls()

        if self._needs_login():
            self._attempt_login(now)
            return

        if self.login != LoginPhase.LOGGED_IN:
            return

        # OUR safety design, not a vendor rule: one SKCOM link
        # transition in flight at a time. A reply rebuild must not
        # overlap EnterMonitorLONG (the 10-12 / 10-22 race).
        if self.reply == LinkPhase.CONNECTING and self.rules.one_link_at_a_time:
            self._service_reply(now)
            return
        if self.quote == LinkPhase.CONNECTING and self.rules.one_link_at_a_time:
            self._service_quote(now)
            return

        if self.reply in (LinkPhase.IDLE, LinkPhase.DOWN):
            self._kick_reply(now)
            if self.rules.one_link_at_a_time or self.reply == LinkPhase.CONNECTING:
                if self.rules.one_link_at_a_time:
                    return
        if self.quote in (LinkPhase.IDLE, LinkPhase.DOWN):
            self._start_quote(now)

    # ── drain ────────────────────────────────────────────────────────

    def _drain(self, now: datetime) -> None:
        queued = self._inbox
        self._inbox = []
        for event in queued:
            self._apply(event, now)

    def _apply(self, event: _Queued, now: datetime) -> None:
        del now
        if event.name == "reply_disconnect":
            self._saw_reply_disconnect = True
            self._reply_down_at = event.now
            self.reply = LinkPhase.DOWN
            self.reply_reconciled = False
            self.orders_paused = True
            self._pending_complete = False
            self._reply_stuck_since = None
            self._reply_awaiting_complete = False
            self._down_without_reply_event_since = None
            return
        if event.name == "complete":
            self._pending_complete = True
            return
        if event.name != "connection":
            return
        kind, code, at = event.kind, event.code, event.now
        if kind == KIND_SOLACE_DOWN:
            self._saw_3033 = True
            self.quote = LinkPhase.DOWN
            self._quote_down_at = at
            self._pending_3003 = False
            self._quote_stuck_since = None
            self.orders_paused = True
            # 3033 is the shared Solace session. Both links go down.
            # Reply reconnects first, and only after a fresh
            # OnSolaceReplyDisconnect plus the §4-3-h settle. A disconnect
            # left over from the previous episode does not count.
            if self.rules.reset_3033_episode:
                self._solace_down_at = at
                self._saw_reply_disconnect = False
                self._reply_down_at = None
                self._reply_awaiting_complete = False
                self._down_without_reply_event_since = None
            elif self._solace_down_at is None:
                self._solace_down_at = at
            if self.rules.solace_3033_both_links_reply_first:
                self.reply = LinkPhase.DOWN
                self.reply_reconciled = False
                self._pending_complete = False
            return
        if kind == KIND_CONN_ERROR:
            self.quote = LinkPhase.DOWN
            self._quote_down_at = at
            self._pending_3003 = False
            self._quote_stuck_since = None
            # A new quote drop is a new observation. An earlier
            # login-recovery clock must not already be expired.
            if self._down_without_reply_event_since is not None:
                self._down_without_reply_event_since = at
            return
        if kind == KIND_READY and code == 0:
            self._pending_3003 = True
            return
        if kind == KIND_QUOTE and code == 0:
            # 3002 is intermediate. It is not STOCKS_READY and not a drop.
            if (
                not self.rules.subscribe_after_3003
                and self.quote == LinkPhase.CONNECTING
            ):
                self.port.subscribe_quotes()
            return
        if kind == KIND_REPLY and code == 0:
            # 3001 is intermediate. It is not OnComplete.
            return

    def _forbidden_reconnect_calls(self) -> None:
        if not self._reconnect_in_progress():
            return
        if not self.rules.leave_monitor_reconnect_forbidden:
            self.port.leave_monitor()
        if not self.rules.logout_forbidden:
            self.port.logout()
        if not self.rules.login_once and self.login == LoginPhase.LOGGED_IN:
            self.port.login()

    def _reconnect_in_progress(self) -> bool:
        return (
            self.quote == LinkPhase.DOWN
            or self.reply == LinkPhase.DOWN
            or self._saw_3033
        )

    # ── login ────────────────────────────────────────────────────────

    def _needs_login(self) -> bool:
        if self.login == LoginPhase.LOGGED_IN:
            return False
        idle = (
            self.quote != LinkPhase.CONNECTING
            and self.reply != LinkPhase.CONNECTING
        )
        if not idle:
            return False
        if self._login_recovery:
            both_down = self.quote in (LinkPhase.IDLE, LinkPhase.DOWN) and (
                self.reply in (LinkPhase.IDLE, LinkPhase.DOWN)
            )
            return both_down
        return self._login_permitted and self.login == LoginPhase.NOT_LOGGED_IN

    def _attempt_login(self, now: datetime) -> None:
        if self.login_failures >= self.config.login_failure_cap:
            self._degrade(now, "login_failures")
            return
        if self._next_login_at is not None and now < self._next_login_at:
            return
        code = self.port.login()
        if code in (0, LOGIN_ALREADY):
            # 2003: already logged in. Success. Do not tear anything down.
            self.login = LoginPhase.LOGGED_IN
            self._login_recovery = False
            self._login_permitted = True
            self.login_failures = 0
            self._arm_stuck_if_links_down_without_reply_event(now)
            return
        if code == API_UPDATE_REQUIRED:
            self.login = LoginPhase.DEGRADED
            self._degrade(now, "api_update_required")
            return
        self.login_failures += 1
        self.login = LoginPhase.NOT_LOGGED_IN
        # Vendor: wait 5s between login failures. We stop at 3 and do
        # not restart the process (the vendor's fifth-failure restart
        # is the thing this machine refuses).
        self._next_login_at = now + timedelta(seconds=self.config.login_retry_s)
        if self.login_failures >= self.config.login_failure_cap:
            self.login = LoginPhase.DEGRADED
            self._login_permitted = False
            self._degrade(now, "login_failures")

    def _mark_login_lost(self) -> None:
        self.login = LoginPhase.NOT_LOGGED_IN
        self._login_recovery = True
        self._login_permitted = False
        self.orders_paused = True
        self.reply_reconciled = False
        if self._saw_3033 or self._saw_reply_disconnect or self.reply != LinkPhase.IDLE:
            self.reply = LinkPhase.DOWN
            self.quote = LinkPhase.DOWN
        else:
            self.reply = LinkPhase.IDLE
            self.quote = LinkPhase.IDLE

    # ── reply link ───────────────────────────────────────────────────

    def _kick_reply(self, now: datetime) -> None:
        # OUR safety design, not a vendor rule: ConnectByID was already
        # accepted and OnComplete has not arrived. Do not call it again.
        if (
            self._reply_awaiting_complete
            and self.rules.reply_connect_once_until_complete
        ):
            if self._pending_complete:
                self._finish_reply()
                self._clear_reply_stuck_clocks()
                return
            self._maybe_stuck_waiting_for_reply_event(now)
            self._maybe_stuck_after_login_recovery(now)
            return
        initial = (
            self.reply == LinkPhase.IDLE
            and not self._saw_reply_disconnect
            and not self._saw_3033
        )
        if not initial:
            if not self._saw_reply_disconnect or self._reply_down_at is None:
                if self._login_wedge_connect(now):
                    return
                self._maybe_stuck_waiting_for_reply_event(now)
                self._maybe_stuck_after_login_recovery(now)
                return
            elapsed = (now - self._reply_down_at).total_seconds()
            # §4-3-h: the disconnect is not finished. Wait out the settle
            # before ConnectByID. The timer is the vendor rule.
            if self.rules.reply_settle and elapsed < self.config.reply_settle_s:
                return
            # OUR safety design, not a vendor rule. §4-3-3 only defines
            # IsConnectedByID as a state query. We refuse ConnectByID
            # while that query is not 0.
            if self.rules.reply_is_connected_by_id_gate:
                state = self.port.is_connected_by_id(self.config.user_id)
                if state != 0:
                    self._track_reply_stuck(now, state)
                    return
                self._reply_stuck_since = None
        if self._backoff_until is not None and now < self._backoff_until:
            return
        code = self.port.connect_reply(self.config.user_id)
        if not self._accept_link_code(code, now):
            return
        self.reply = LinkPhase.CONNECTING
        self._reply_connect_started_at = now
        self._pending_complete = False
        self._reply_awaiting_complete = True
        if not self.rules.one_link_at_a_time:
            # Mutant: start the quote link while ConnectByID is in flight.
            self.port.enter_monitor()

    def _service_reply(self, now: datetime) -> None:
        if self._pending_complete:
            self._finish_reply()
            return
        elapsed = (now - self._reply_connect_started_at).total_seconds()
        state = self.port.is_connected_by_id(self.config.user_id)
        if state in (1, 2):
            self._track_reply_stuck(now, state)
            if self.degraded:
                return
        else:
            self._reply_stuck_since = None
        if self.rules.reply_connect_once_until_complete:
            # Stay in CONNECTING until OnComplete. IsConnectedByID==0
            # is not permission to call ConnectByID again.
            if elapsed >= self.config.stuck_limit_s:
                self._degrade(now, "stuck")
            return
        if elapsed >= self.config.connecting_timeout_s:
            # Mutant: a second ConnectByID while the first is still open.
            self.port.connect_reply(self.config.user_id)

    def _finish_reply(self) -> None:
        if self.rules.orders_after_complete_and_reconcile:
            self.port.reconcile_positions()
            self.port.release_orders()
            self.reply_reconciled = True
            self.orders_paused = False
        else:
            self.port.release_orders()
        self.reply = LinkPhase.READY
        self._pending_complete = False
        self._reply_connect_started_at = None
        self._reply_awaiting_complete = False
        self._reply_stuck_since = None
        self._down_without_reply_event_since = None
        self._login_wedge_settle_at = None
        self._settle_before_stuck = False

    def _maybe_stuck_waiting_for_reply_event(self, now: datetime) -> None:
        """3033 with no OnSolaceReplyDisconnect cannot move either link.

        OUR safety design: do not invent a ConnectByID to get unstuck.
        """
        if not self._saw_3033 or self._solace_down_at is None:
            return
        waited = (now - self._solace_down_at).total_seconds()
        if waited >= self.config.stuck_limit_s:
            self._degrade(now, "stuck")

    # ── quote link ───────────────────────────────────────────────────

    def _start_quote(self, now: datetime) -> None:
        if (
            self._saw_3033
            and self.rules.solace_3033_both_links_reply_first
            and self.reply != LinkPhase.READY
        ):
            return
        if self.reply == LinkPhase.DOWN and self.rules.one_link_at_a_time:
            return
        if self.quote == LinkPhase.DOWN and self._quote_down_at is not None:
            # OUR timing choice, not a vendor requirement. The vendor
            # timer is the reply settle (§4-3-h). Quote waits out its
            # own settle so a drop is not answered inside the event.
            waited = (now - self._quote_down_at).total_seconds()
            if waited < self.config.quote_settle_s:
                return
        # OUR safety design, not a vendor rule. The docs describe
        # IsConnected (§4-4-19, 0 = 斷線) and do not require this gate
        # or forbid re-entry. We call EnterMonitorLONG only when it is 0.
        if self.rules.quote_is_connected_gate:
            state = self.port.is_connected()
            if state != 0:
                self._track_quote_stuck(now, state)
                return
            self._quote_stuck_since = None
        code = self.port.enter_monitor()
        if not self._accept_link_code(code, now):
            return
        self.quote = LinkPhase.CONNECTING
        self._quote_connect_started_at = now
        self._pending_3003 = False

    def _service_quote(self, now: datetime) -> None:
        if not self.rules.enter_once_while_connecting:
            # Mutant: call EnterMonitorLONG again while waiting for 3003.
            self.port.enter_monitor()
            return
        if not self.rules.subscribe_after_3003 and not self._pending_3003:
            # Mutant: subscribe before STOCKS_READY (§4-4-2).
            self.port.subscribe_quotes()
            return
        if self._pending_3003:
            # §4-4-2: subscribe only now, from the main loop, after 3003.
            self._pending_3003 = False
            self.port.subscribe_quotes()
            self.quote = LinkPhase.READY
            self._quote_stuck_since = None
            self._quote_connect_started_at = None
            self._maybe_finish_solace_episode()
            return
        elapsed = (now - self._quote_connect_started_at).total_seconds()
        state = self.port.is_connected()
        if state in (1, 2):
            # Still up or half-up. The only legal next call is another
            # EnterMonitorLONG, and only once IsConnected()==0. Until
            # then we wait. Past stuck_limit we stop and degrade.
            self._track_quote_stuck(now, state)
            return
        self._quote_stuck_since = None
        if elapsed >= self.config.connecting_timeout_s:
            self.quote = LinkPhase.DOWN
            if self._quote_down_at is None:
                self._quote_down_at = self._quote_connect_started_at or now

    # ── shared outcomes ──────────────────────────────────────────────

    def _accept_link_code(self, code: int, now: datetime) -> bool:
        if code in (0, LOGIN_ALREADY):
            return True
        if code == API_UPDATE_REQUIRED:
            self._degrade(now, "api_update_required")
            return False
        if 1000 <= code < 2000:
            self._mark_login_lost()
            return False
        self._backoff_until = now + timedelta(
            seconds=self.config.connecting_timeout_s
        )
        return False

    def _track_quote_stuck(self, now: datetime, state: int) -> None:
        if state not in (1, 2):
            self._quote_stuck_since = None
            return
        if self._quote_stuck_since is None:
            self._quote_stuck_since = now
            return
        held = (now - self._quote_stuck_since).total_seconds()
        if held >= self.config.stuck_limit_s:
            self._degrade(now, "stuck")

    def _track_reply_stuck(self, now: datetime, state: int) -> None:
        if state not in (1, 2):
            self._reply_stuck_since = None
            return
        if self._reply_stuck_since is None:
            self._reply_stuck_since = now
            return
        held = (now - self._reply_stuck_since).total_seconds()
        if held >= self.config.stuck_limit_s:
            self._degrade(now, "stuck")

    def _degrade(self, now: datetime, reason: str) -> None:
        if self.degraded:
            return
        self.degraded = True
        self.degraded_reason = reason
        self.entries_blocked = True
        self.orders_paused = True
        self._login_permitted = False
        self._last_probe_at = now
        # Stay disconnected without LeaveMonitor. A half-up quote attempt
        # is abandoned in this machine only; the other link is left as it
        # is so a later manual Reconnect does not invent a reply teardown.
        if self.quote == LinkPhase.CONNECTING:
            self.quote = LinkPhase.DOWN
            if self._quote_down_at is None:
                self._quote_down_at = now
        if self.reply == LinkPhase.CONNECTING:
            self.reply = LinkPhase.DOWN
        self.alerts.append(
            f"DEGRADED {reason}: entries blocked; "
            f"probe IsConnected every {int(self.config.probe_interval_s)}s; "
            "waiting for manual Reconnect"
        )
        # This episode failed. A later 3033 has to see a new
        # OnSolaceReplyDisconnect and run the settle path again.
        if self.rules.reset_3033_episode:
            self._saw_reply_disconnect = False
            self._reply_down_at = None
            # Keep _reply_awaiting_complete. The ConnectByID that just
            # timed out is still in flight until OnComplete or a new
            # 3033. Clearing it here is what sent a second ConnectByID.
            self._solace_down_at = None
        if not self.rules.degraded_instead_of_restart:
            # Mutant: relaunch the process instead of staying down.
            self.port.restart_process()

    def _maybe_finish_solace_episode(self) -> None:
        """Both links are back. Forget this 3033 so the next one is new."""
        if not self._saw_3033 or self.reply != LinkPhase.READY:
            return
        if not self.rules.reset_3033_episode:
            return
        self._saw_3033 = False
        self._solace_down_at = None
        self._saw_reply_disconnect = False
        self._reply_down_at = None
        self._reply_awaiting_complete = False
        self._reply_stuck_since = None
        self._down_without_reply_event_since = None

    def _arm_reply_stuck_after_manual(self, now: datetime) -> None:
        """Reply is down after the press. Arm a clock, and maybe a settle.

        A 3033 already restarted ``_solace_down_at``. A ConnectByID that
        is still waiting for OnComplete is not sent again. Otherwise
        this is the login wedge: the disconnect event may have been
        missed, so wait out the reply settle from this press, then one
        ConnectByID. Not on the press itself.
        """
        if self.reply != LinkPhase.DOWN or self._saw_3033:
            return
        if (
            self._reply_awaiting_complete
            and self.rules.reply_connect_once_until_complete
        ):
            if self._down_without_reply_event_since is not None:
                return
            if self.rules.manual_reconnect_arms_reply_stuck:
                self._down_without_reply_event_since = now
                return
            # Mutant: ConnectByID while that rebuild is still in flight.
            self.port.connect_reply(self.config.user_id)
            return
        if self._down_without_reply_event_since is None:
            self._down_without_reply_event_since = now
        if not self.rules.manual_login_wedge_settles:
            # Mutant: ConnectByID on the button press, inside the settle.
            self.port.connect_reply(self.config.user_id)
            return
        self._login_wedge_settle_at = now
        self._settle_before_stuck = True

    def _arm_stuck_if_links_down_without_reply_event(self, now: datetime) -> None:
        """3021 → EnterMonitor 1108 → login succeeded, reply still down.

        OnSolaceReplyDisconnect can be missed while login is down. Arm
        one reply settle, then one ConnectByID. Also arm the stuck
        clock so a connect that never completes still degrades.
        """
        if self.reply != LinkPhase.DOWN or self._saw_reply_disconnect or self._saw_3033:
            return
        if self.rules.stuck_after_login_recovery:
            if self._login_wedge_settle_at is None:
                self._login_wedge_settle_at = now
            if self._down_without_reply_event_since is None:
                self._down_without_reply_event_since = now
            return
        self.port.connect_reply(self.config.user_id)

    def _login_wedge_connect(self, now: datetime) -> bool:
        """Settle, then one ConnectByID, when the disconnect event was missed.

        Returns True when this tick belongs to that wait or that call.
        """
        if self._login_wedge_settle_at is None or self._saw_3033:
            return False
        if self._saw_reply_disconnect and self._reply_down_at is not None:
            return False
        elapsed = (now - self._login_wedge_settle_at).total_seconds()
        if elapsed < self.config.reply_settle_s:
            # A manual press asked for this connect. Do not declare the
            # link stuck before that one ConnectByID is sent.
            if not self._settle_before_stuck:
                self._maybe_stuck_after_login_recovery(now)
                if self.degraded:
                    return True
            return True
        self._login_wedge_settle_at = None
        self._settle_before_stuck = False
        code = self.port.connect_reply(self.config.user_id)
        if not self._accept_link_code(code, now):
            return True
        self.reply = LinkPhase.CONNECTING
        self._reply_connect_started_at = now
        self._pending_complete = False
        self._reply_awaiting_complete = True
        if not self.rules.one_link_at_a_time:
            self.port.enter_monitor()
        return True

    def _recover_late_reply(self, now: datetime) -> bool:
        """OnComplete or IsConnectedByID==1 after degrade. No ConnectByID.

        Clears degraded and the reply stuck clocks. ``entries_blocked``
        stays set until both links are up and positions are reconciled.
        """
        del now
        if self._pending_complete:
            if not self.rules.late_complete_without_reconnect:
                self.port.connect_reply(self.config.user_id)
                return True
            self._finish_reply()
            self._clear_reply_stuck_clocks()
            self.degraded = False
            self.degraded_reason = ""
            return True
        if self.reply == LinkPhase.READY:
            return False
        state = self.port.is_connected_by_id(self.config.user_id)
        if state != 1:
            return False
        if not self.rules.connected_id_without_reconnect:
            self.port.connect_reply(self.config.user_id)
            return True
        self.reply = LinkPhase.READY
        self._reply_awaiting_complete = False
        self._reply_connect_started_at = None
        self._pending_complete = False
        self._clear_reply_stuck_clocks()
        self.degraded = False
        self.degraded_reason = ""
        return True

    def _clear_reply_stuck_clocks(self) -> None:
        self._solace_down_at = None
        self._down_without_reply_event_since = None
        self._reply_stuck_since = None
        self._login_wedge_settle_at = None
        self._reply_down_at = None

    def _maybe_unblock_entries(self) -> None:
        """Drop the entry block only when both links are up and reconciled."""
        if self.degraded:
            return
        if (
            self.quote == LinkPhase.READY
            and self.reply == LinkPhase.READY
            and self.reply_reconciled
        ):
            self.entries_blocked = False

    def _maybe_stuck_after_login_recovery(self, now: datetime) -> None:
        if self._down_without_reply_event_since is None:
            return
        waited = (now - self._down_without_reply_event_since).total_seconds()
        if waited >= self.config.stuck_limit_s:
            self._degrade(now, "stuck")

    def _probe(self, now: datetime) -> None:
        """Cheap IsConnected read. Never EnterMonitor, ConnectByID, or Login."""
        if self._last_probe_at is None:
            self._last_probe_at = now
            return
        elapsed = (now - self._last_probe_at).total_seconds()
        if elapsed < self.config.probe_interval_s:
            return
        self.port.is_connected()
        self._last_probe_at = now
