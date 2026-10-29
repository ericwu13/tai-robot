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

Quote-stuck recovery (#203) resubscribes ticks. It must not call
``ConnectByID`` and must not lift this pause while the reply link is
down. ``quote_recovery_may_release`` is that gate.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

from src.live.link_state_machine import LinkPhase, LinkStateMachine

# Seconds to wait before each ConnectByID after a 0 reading.
# The last value repeats. A step past the tuple stays at 60.
REPLY_BACKOFF_S = (5, 10, 30, 60)
REPLY_POLL_S = 60
SILENT_AFTER_S = 60

SILENT_ALERT = (
    "[CONN] REPLY CHANNEL SILENT — no reply READY within 60s"
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


def apply_quote_stuck_recovery(machine: LinkStateMachine) -> None:
    """What #203 may change on the machine after ticks resume.

    No ConnectByID, no logout, no broker call. Entries stay paused
    while the reply link is not READY. A READY reconciled reply may
    drop ``orders_paused``; a down reply is left alone so this does
    not fight the poller.
    """
    if machine.reply == LinkPhase.READY and machine.reply_reconciled:
        machine.orders_paused = False
        machine.entries_blocked = False


def format_event_sink(kind: str, sink, handler, module, pid: int) -> str:
    """One registration line: identity, IID, module id, PID.

    ``module`` is the module that registered the sink. A frozen
    double-import shows up as ``module_id`` differing from
    ``run_backtest_id``. No account id, no host name.
    """
    iid = "unknown"
    for obj in (sink, handler):
        if obj is None:
            continue
        for attr in ("_iid", "iid", "_interface"):
            value = getattr(obj, attr, None)
            if value:
                iid = getattr(value, "__name__", None) or str(value)
                break
        else:
            continue
        break
    other = sys.modules.get("run_backtest")
    main_mod = sys.modules.get("__main__")
    name = getattr(module, "__name__", "?")
    return (
        f"[CONN] event-sink {kind} "
        f"sink_id={id(sink)} handler_id={id(handler)} "
        f"iid={iid} module={name} module_id={id(module)} "
        f"main_id={id(main_mod)} run_backtest_id={id(other)} pid={pid}"
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

    def note_login(self, now: datetime) -> None:
        """One silent episode from this login. Does not read SKCOM."""
        self._ready = False
        self._down = False
        self._heard_event = False
        self._episode_alerted = False
        self._episode_open = True
        self._episode_started = now
        self._step = 0
        self._next_connect_at = None

    def note_reply_event(self, kind: str) -> None:
        """OnConnect or OnComplete. This episode is not silent.

        OnComplete also ends a poller reconnect: the machine owns the
        release. Further backoff connects are cancelled.
        """
        self._heard_event = True
        if kind == "OnComplete":
            self._down = False
            self._ready = True
            self._next_connect_at = None
            self._episode_open = False

    def observe(self, state: int, now: datetime) -> None:
        """Apply one IsConnectedByID reading. Never raises."""
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
        """Alert once per open episode with no READY and no reply event.

        ``force`` is the 60s timer firing. The clock path alerts when
        ``SILENT_AFTER_S`` has passed since the episode opened.
        """
        try:
            if self._episode_alerted or self._ready or self._heard_event:
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
        self._episode_open = False

    def _on_down(self, now: datetime) -> None:
        fresh = not self._down
        self._down = True
        self._ready = False
        self._heard_event = False
        if fresh:
            machine = self._machine()
            if machine is not None:
                machine.note_polled_down(now)
            self._step = 0
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
        if not self._down or self._next_connect_at is None:
            return
        if now < self._next_connect_at:
            return
        # One connect for this step. The next wait is capped at 60s.
        self._step += 1
        self._next_connect_at = now + timedelta(seconds=backoff_delay_s(self._step))
        machine = self._machine()
        user_id = machine.config.user_id if machine is not None else ""
        try:
            self._connect(user_id)
            self._connects += 1
        except Exception as exc:
            self._safe_error(
                "[CONN] ConnectByID raised: "
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
