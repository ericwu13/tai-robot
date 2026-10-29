"""Reply-link liveness when SKReplyLib events never reach Python.

Issue #219. The link machine rebuilds the reply link only after a
reply-disconnect or OnComplete. On the live host those events do not
arrive, so a dropped reply stayed up in the machine and entries were
never paused.

This poller reads ``IsConnectedByID`` (the caller does the COM call).
``0`` pauses new entries and schedules one ``ConnectByID`` per backoff
step (5/10/30/60 seconds, then 60). ``1`` marks the reply READY on the
poll path and lifts that pause. It never logs out. A connect already in
flight is not duplicated inside one backoff step.

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

SILENT_ALERT = (
    "[CONN] REPLY CHANNEL SILENT — no reply event within 60s"
)
STILL_DOWN_ALERT = (
    "[CONN] reply link still down after "
    f"{REPLY_CONNECT_ATTEMPT_CAP} ConnectByID attempts; "
    "entries paused; retry stopped"
)


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
                self._on_ready(now)
            elif state == 0:
                self._on_down(now)
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
        # One connect for this step. The next wait is capped at 60s
        # until REPLY_CONNECT_ATTEMPT_CAP, then retries stop.
        self._step += 1
        self._attempts += 1
        self._next_connect_at = now + timedelta(seconds=backoff_delay_s(self._step))
        try:
            self._connect(user_id)
            self._connects += 1
        except Exception as exc:
            self._safe_error(
                "[CONN] ConnectByID raised: "
                f"[{type(exc).__name__}] {exc}")
        if self._attempts >= REPLY_CONNECT_ATTEMPT_CAP:
            self._capped = True
            self._next_connect_at = None
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
