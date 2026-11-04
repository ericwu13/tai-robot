"""Reply-link liveness when SKReplyLib events never reach Python.

Issue #219. The link machine rebuilds the reply link only after a
reply-disconnect or OnComplete. On the live host those events do not
arrive, so a dropped reply stayed up in the machine and entries were
never paused.

This poller reads ``IsConnectedByID`` (the caller does the COM call).
``0`` pauses new entries. One ``ConnectByID`` is sent per down episode,
and only after ``POLL_CONNECT_MIN_ZEROS`` consecutive 0 readings that
span the reply settle, and only when the link machine permits it. A
reading of 2 means SKCOM is reconnecting itself and breaks that run.
``1`` marks the reply READY on the poll path and lifts that pause. It
never logs out. A connect stays in flight until OnComplete, a poll of
1, a non-zero return, a raised call, or ``POLL_CONNECT_TIMEOUT_S``.
After that timeout another gated call is allowed, up to
``REPLY_CONNECT_ATTEMPT_CAP``. Only then does the poller degrade.

Poll READY and a reply event are different facts. ``IsConnectedByID==1``
does not mean OnConnect, OnComplete, or OnNewData arrived. The silent
alert fires when no reply event arrives within 60s of login, of a
detected drop, or of a real order being sent. It does not pause entries.

After ``REPLY_CONNECT_ATTEMPT_CAP`` (10) ConnectByID calls with the
link still down, one distinct alert is sent and retries stop. A later
``IsConnectedByID==1`` or a new drop clears the counter. An operator
reconnect or a process restart is what starts another series.

#203 quote-stuck recovery resubscribes ticks. It must not call
``ConnectByID`` and must not clear ``orders_paused`` while this poll
has the reply link down. This module does not perform that recovery.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

# Seconds to wait before each ConnectByID after a 0 reading.
# The last value repeats. A step past the tuple stays at 60.
REPLY_BACKOFF_S = (5, 10, 30, 60)
REPLY_POLL_S = 60
SILENT_AFTER_S = 60
# Stop ConnectByID after this many attempts in one down episode.
# Ten attempts span the 5/10/30/60 schedule and several capped 60s
# steps (about eight minutes) without hammering SKCOM all session.
REPLY_CONNECT_ATTEMPT_CAP = 10
# Consecutive clean 0 readings, spanning at least the reply settle,
# before the one ConnectByID. A 2 breaks the run.
POLL_CONNECT_MIN_ZEROS = 3
# One call per down episode. No release by then: degrade, alert, stop.
POLL_CONNECT_TIMEOUT_S = 90.0

SILENT_ALERT = (
    "[CONN] REPLY CHANNEL SILENT — no reply event within 60s"
)
STILL_DOWN_ALERT = (
    "[CONN] reply link still down after "
    f"{REPLY_CONNECT_ATTEMPT_CAP} ConnectByID attempts; "
    "entries paused; retry stopped"
)


def _connect_code_ok(code) -> bool:
    """True only for a numeric 0. None and any other code release the call."""
    if code is None or isinstance(code, bool):
        return False
    try:
        return int(code) == 0
    except (TypeError, ValueError):
        return False


def backoff_delay_s(step: int) -> int:
    """Delay before the next ConnectByID. ``step`` 0 is the first wait.

    Steps past the schedule stay at 60. They do not grow.
    """
    if step < 0:
        step = 0
    if step >= len(REPLY_BACKOFF_S):
        return REPLY_BACKOFF_S[-1]
    return REPLY_BACKOFF_S[step]


def resolve_outgoing_iid(source) -> str:
    """IID of the COM source's outgoing event interface.

    ``comtypes.client._events.FindOutgoingInterface(source)._iid_`` is
    the interface the sink is actually bound to. A missing comtypes or
    a source with no outgoing interface returns ``unknown``.
    """
    if source is None:
        return "unknown"
    try:
        from comtypes.client._events import FindOutgoingInterface
    except Exception:
        return "unknown"
    try:
        iface = FindOutgoingInterface(source)
    except Exception:
        return "unknown"
    iid = getattr(iface, "_iid_", None)
    if iid is None:
        iid = getattr(iface, "_iid", None)
    return str(iid) if iid else "unknown"


def _comtypes_facts() -> tuple[str, str]:
    try:
        import comtypes
        version = str(getattr(comtypes, "__version__", "unknown"))
        client = sys.modules.get("comtypes.client")
        if client is None:
            import comtypes.client as client
        gen_dir = str(getattr(client, "gen_dir", "") or "")
        return version, gen_dir
    except Exception:
        return "unavailable", ""


def _process_start() -> str:
    """Clock-tick start time from /proc, or ``unknown`` off Linux."""
    try:
        with open("/proc/self/stat", encoding="ascii") as handle:
            stat = handle.read()
        after = stat.rsplit(")", 1)[1].split()
        return after[19]
    except Exception:
        return "unknown"


def _run_backtest_module_keys() -> str:
    keys = sorted(
        key for key in sys.modules
        if key == "__main__" or "run_backtest" in key
    )
    return ",".join(keys)


def format_event_sink(kind: str, sink, handler, module, source=None) -> str:
    """One registration line for the three sink hypotheses.

    Frozen bundle, module file, every ``sys.modules`` key that is
    ``__main__`` or mentions ``run_backtest``, pid and ppid, process
    start, comtypes version and gen cache, and the outgoing event IID.
    No login id and no account data.
    """
    frozen = bool(getattr(sys, "frozen", False))
    meipass = getattr(sys, "_MEIPASS", "") or ""
    path = getattr(module, "__file__", "") or ""
    version, gen_dir = _comtypes_facts()
    return (
        f"[CONN] event-sink {kind} "
        f"sink_id={id(sink)} handler_id={id(handler)} "
        f"iid={resolve_outgoing_iid(source)} "
        f"module={getattr(module, '__name__', '?')} "
        f"module_id={id(module)} file={path} "
        f"frozen={int(frozen)} meipass={meipass} "
        f"modules={_run_backtest_module_keys()} "
        f"pid={os.getpid()} ppid={os.getppid()} start={_process_start()} "
        f"comtypes={version} gen_dir={gen_dir}"
    )


class ReplyLinkPoller:
    """Clock-driven reply poll. The host supplies ``read`` and ``connect``.

    ``read`` returns IsConnectedByID. ``connect`` sends one ConnectByID
    and logs its return code. Neither may log out. ``notify`` is the
    Discord path. Failures are logged and swallowed.
    """

    def __init__(
        self,
        machine_getter,
        *,
        read,
        connect,
        notify,
        log_error,
        log_info,
        reschedule_silent,
    ) -> None:
        self._machine_getter = machine_getter
        self._read = read
        self._connect = connect
        self._notify = notify
        self._log_error = log_error
        self._log_info = log_info
        self._reschedule_silent = reschedule_silent
        self._ready = False
        self._down = False
        self._heard_event = False
        self._episode_alerted = False
        self._episode_open = False
        self._episode_started: datetime | None = None
        self._step = 0
        self._attempts = 0
        self._capped = False
        self._ceiling_alerted = False
        self._stopped = False
        self._next_connect_at: datetime | None = None
        self._connects = 0
        self._zero_at: list[datetime] = []
        self._blocked_by_two = False

    @property
    def connect_count(self) -> int:
        return self._connects

    @property
    def down(self) -> bool:
        return self._down

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def stopped(self) -> bool:
        return self._stopped

    def note_login(self, now: datetime) -> None:
        """One silent episode from this login. Does not read SKCOM.

        Poll READY is not an event. The episode stays open until
        OnConnect, OnComplete, or OnNewData, or until the alert fires.
        """
        self._stopped = False
        self._ready = False
        self._down = False
        self._heard_event = False
        self._episode_alerted = False
        self._episode_open = True
        self._episode_started = now
        self._step = 0
        self._attempts = 0
        self._capped = False
        self._ceiling_alerted = False
        self._next_connect_at = None
        self._zero_at = []
        self._blocked_by_two = False

    def note_reply_event(self, kind: str) -> None:
        """OnConnect, OnComplete, or OnNewData. This episode is not silent.

        OnComplete also ends a poller reconnect: the machine owns the
        release. Further backoff connects are cancelled. The other two
        events only close the silent window.
        """
        if self._stopped:
            return
        self._heard_event = True
        self._episode_open = False
        if kind == "OnComplete":
            self._down = False
            self._ready = True
            self._next_connect_at = None
            self._capped = False

    def note_order_sent(self, now: datetime) -> None:
        """A real order went out. Alert if no reply event follows.

        Does not pause entries and does not call ConnectByID. An event
        that arrived before the order does not cover this order.
        """
        if self._stopped:
            return
        self._heard_event = False
        self._episode_alerted = False
        self._episode_open = True
        self._episode_started = now
        try:
            self._reschedule_silent()
        except Exception as exc:
            self._safe_error(
                "[CONN] reply silent rearm failed: "
                f"[{type(exc).__name__}] {exc}")

    def stop(self) -> None:
        """Shutdown / logout. Further polls must not connect or alert."""
        self._stopped = True
        self._down = False
        self._next_connect_at = None
        self._episode_open = False

    def observe(self, state: int, now: datetime) -> None:
        """Apply one IsConnectedByID reading. Never raises."""
        if self._stopped:
            return
        try:
            if state == 1:
                self._clear_zero_run()
                self._on_ready(now)
            elif state == 0:
                self._note_zero(now)
                self._on_down(now)
            elif state == 2:
                self._note_two()
                self._log_info(
                    "[CONN] IsConnectedByID=2 poll (SKCOM reconnecting)")
            else:
                self._log_info(f"[CONN] IsConnectedByID={state} poll ignored")
            self.consider_silent(now, force=False)
        except Exception as exc:
            self._safe_error(
                "[CONN] reply poll failed: "
                f"[{type(exc).__name__}] {exc}")

    def poll(self, now: datetime) -> None:
        """Read IsConnectedByID and apply it. A read error is logged."""
        if self._stopped:
            return
        try:
            state = self._read()
        except Exception as exc:
            self._safe_error(
                "[CONN] IsConnectedByID raised: "
                f"[{type(exc).__name__}] {exc}")
            return
        try:
            self._log_info(f"[CONN] IsConnectedByID={state} poll")
        except Exception:
            pass
        self.observe(int(state), now)

    def pending_delay_s(self, now: datetime) -> float | None:
        """Seconds until the next ConnectByID, or None when not down."""
        if not self._down or self._next_connect_at is None:
            return None
        return max(0.0, (self._next_connect_at - now).total_seconds())

    def consider_silent(self, now: datetime | None = None, *, force: bool = False) -> bool:
        """Alert once per open episode with no reply event.

        Poll READY does not close the episode. ``force`` is the 60s
        timer firing. The clock path alerts when ``SILENT_AFTER_S`` has
        passed since the episode opened.
        """
        try:
            if self._stopped or self._episode_alerted or self._heard_event:
                return False
            if not self._episode_open:
                return False
            if not force:
                started = self._episode_started
                if started is None or now is None:
                    return False
                if (now - started).total_seconds() < SILENT_AFTER_S:
                    return False
            self._episode_alerted = True
            self._episode_open = False
            self._safe_error(SILENT_ALERT)
            try:
                self._notify(SILENT_ALERT)
            except Exception as exc:
                self._safe_error(
                    "[CONN] reply silent discord failed: "
                    f"[{type(exc).__name__}] {exc}")
            return True
        except Exception as exc:
            self._safe_error(
                "[CONN] reply silent watch failed: "
                f"[{type(exc).__name__}] {exc}")
            return False

    def _on_ready(self, now: datetime) -> None:
        machine = self._machine()
        if machine is not None:
            machine.note_polled_ready(now)
        self._ready = True
        self._down = False
        self._next_connect_at = None
        self._step = 0
        self._attempts = 0
        self._capped = False
        self._clear_zero_run()
        # Leave the silent episode open. IsConnectedByID==1 is not
        # OnConnect, OnComplete, or OnNewData.

    def _on_down(self, now: datetime) -> None:
        fresh = not self._down
        self._down = True
        self._ready = False
        if fresh:
            self._heard_event = False
            machine = self._machine()
            if machine is not None:
                machine.note_polled_down(now)
            self._step = 0
            self._attempts = 0
            self._capped = False
            self._ceiling_alerted = False
            self._next_connect_at = now + timedelta(seconds=backoff_delay_s(0))
            self._episode_alerted = False
            self._episode_open = True
            self._episode_started = now
            try:
                self._reschedule_silent()
            except Exception as exc:
                self._safe_error(
                    "[CONN] reply silent rearm failed: "
                    f"[{type(exc).__name__}] {exc}")
        self._connect_if_due(now)

    def _connect_if_due(self, now: datetime) -> None:
        if self._stopped or self._capped:
            return
        if not self._down or self._next_connect_at is None:
            return
        if now < self._next_connect_at:
            return
        machine = self._machine()
        user_id = ""
        if machine is not None:
            user_id = str(getattr(machine.config, "user_id", "") or "").strip()
        # Shutdown drops the machine. An empty id is not an account to
        # reconnect. Either case must not call ConnectByID.
        if machine is None or not user_id:
            return
        # Keep checking on the backoff clock. The call itself is once
        # per down episode, and only when the gates below all pass.
        self._step += 1
        self._next_connect_at = now + timedelta(seconds=backoff_delay_s(self._step))
        started = self._inflight_started(machine)
        if started is not None:
            if (now - started).total_seconds() >= POLL_CONNECT_TIMEOUT_S:
                # One call is still the rule. The 90s mark releases it
                # so a later backoff tick may send the next gated call.
                # The tenth unanswered call degrades.
                self._release_inflight(machine)
                if self._attempts >= REPLY_CONNECT_ATTEMPT_CAP:
                    self._give_up(machine, now)
            return
        if not self._zeros_ready(self._settle_s(machine)):
            return
        ask = getattr(machine, "may_poll_connect", None)
        if callable(ask) and not ask(now):
            return
        self._attempts += 1
        accepted = False
        try:
            code = self._connect(user_id)
            self._connects += 1
            accepted = _connect_code_ok(code)
        except Exception as exc:
            self._safe_error(
                "[CONN] ConnectByID raised: "
                f"[{type(exc).__name__}] {exc}")
            self._release_inflight(machine)
        else:
            if not accepted:
                self._release_inflight(machine)
        if self._attempts >= REPLY_CONNECT_ATTEMPT_CAP and not accepted:
            self._give_up(machine, now)

    def _clear_zero_run(self) -> None:
        self._zero_at = []
        self._blocked_by_two = False

    def _note_two(self) -> None:
        """SKCOM is reconnecting itself. The clean-0 run starts over."""
        self._zero_at = []
        self._blocked_by_two = True

    def _note_zero(self, now: datetime) -> None:
        if self._blocked_by_two:
            self._blocked_by_two = False
            self._zero_at = []
        self._zero_at.append(now)

    def _settle_s(self, machine) -> float:
        config = getattr(machine, "config", None)
        settle = getattr(config, "reply_settle_s", None)
        if isinstance(settle, (int, float)) and not isinstance(settle, bool):
            return float(settle)
        return 10.0

    def _zeros_ready(self, settle_s: float) -> bool:
        if self._blocked_by_two or len(self._zero_at) < POLL_CONNECT_MIN_ZEROS:
            return False
        span = (self._zero_at[-1] - self._zero_at[0]).total_seconds()
        return span >= float(settle_s)

    def _inflight_started(self, machine):
        if not getattr(machine, "_reply_awaiting_complete", False):
            return None
        return getattr(machine, "_reply_connect_started_at", None)

    def _release_inflight(self, machine) -> None:
        fn = getattr(machine, "release_poll_connect", None)
        if not callable(fn):
            return
        try:
            fn()
        except Exception as exc:
            self._safe_error(
                "[CONN] reply connect release failed: "
                f"[{type(exc).__name__}] {exc}")

    def _give_up(self, machine, now: datetime) -> None:
        """Cap reached. Degrade, alert once, do not connect again."""
        self._capped = True
        self._next_connect_at = None
        try:
            note = getattr(machine, "note_poll_connect_timeout", None)
            if callable(note):
                note(now)
        except Exception as exc:
            self._safe_error(
                "[CONN] reply connect timeout failed: "
                f"[{type(exc).__name__}] {exc}")
        self._notify_ceiling()

    def _notify_ceiling(self) -> None:
        if self._ceiling_alerted:
            return
        self._ceiling_alerted = True
        self._safe_error(STILL_DOWN_ALERT)
        try:
            self._notify(STILL_DOWN_ALERT)
        except Exception as exc:
            self._safe_error(
                "[CONN] reply ceiling discord failed: "
                f"[{type(exc).__name__}] {exc}")

    def _machine(self):
        try:
            return self._machine_getter()
        except Exception:
            return None

    def _safe_error(self, msg: str) -> None:
        try:
            self._log_error(msg)
        except Exception:
            pass
