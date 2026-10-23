"""Live host for the #157 link machine (P2).

One ``LinkStateMachine`` per app process. The GUI and the headless bot
share this helper. Headless does not have its own reconnect sequence:
``HeadlessBotApp`` is a ``BacktestApp``, and both pump flags through the
existing Tk ``after`` drain (``_drain_ui_queue``).

Lifetime (chosen, see the module docstring on ``adopt_after_process_login``):

* Constructed once, after the process-start login in ``_do_login`` has
  already called LoginSetQuote, ConnectByID, and EnterMonitorLONG.
* ``LinkStateMachine.start()`` is not used. That method logs in. The
  process already did.
* The machine is not rebuilt when a bot is stopped or deployed again.
  The COM login is process-scoped.

Event handlers only enqueue. The main thread calls ``note_*`` (flags
only, no broker calls) and then ``tick``. ``tick`` does not sleep.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.live.link_state_machine import (
    LinkMachineConfig,
    LinkPhase,
    LinkStateMachine,
    MachineRules,
)


# Steady pump. The machine's settle windows are 5s and 10s, so one
# second is enough and does not call IsConnected on every UI drain.
TICK_INTERVAL_S = 1.0


class LinkHost:
    """Owns one machine and the main-thread pump. No Tk, no COM."""

    def __init__(self, machine: LinkStateMachine) -> None:
        self.machine = machine
        self._last_tick_at: datetime | None = None

    def note_connection(self, kind: int, code: int, now: datetime) -> None:
        """Queue an OnConnection kind. Does not call the broker."""
        self.machine.on_connection(kind, code, now)

    def note_reply_disconnect(self, now: datetime) -> None:
        """Queue OnSolaceReplyDisconnect. Does not call the broker."""
        self.machine.on_solace_reply_disconnect(now)

    def note_complete(self, now: datetime) -> None:
        """Queue OnComplete. Does not call the broker."""
        self.machine.on_complete(now)

    def tick(self, now: datetime) -> list[str]:
        """One machine step. Returns alerts raised by this step."""
        before = len(self.machine.alerts)
        self.machine.tick(now)
        self._last_tick_at = now
        return list(self.machine.alerts[before:])

    def maybe_tick(self, now: datetime) -> list[str]:
        """Tick when ``TICK_INTERVAL_S`` has elapsed. Otherwise do nothing.

        A backwards clock ticks immediately. The drain calls this so a
        100ms UI pump does not become a 10Hz IsConnected poll.
        """
        last = self._last_tick_at
        if last is not None and now >= last:
            if now - last < timedelta(seconds=TICK_INTERVAL_S):
                return []
        return self.tick(now)

    def manual_reconnect(self, now: datetime) -> list[str]:
        """Operator Reconnect. The machine defines the effect, then one tick.

        Not degraded: ``manual_reconnect`` is a no-op inside the machine
        (no LeaveMonitor, no second teardown). The tick still runs.
        """
        self.machine.manual_reconnect(now)
        return self.tick(now)

    def shutdown(self, now: datetime) -> None:
        """User shutdown. The machine's only LeaveMonitor call."""
        self.machine.shutdown(now)

    def entries_allowed(self) -> bool:
        """True only when both links are up and the reply is reconciled."""
        machine = self.machine
        return (
            machine.quote == LinkPhase.READY
            and machine.reply == LinkPhase.READY
            and machine.reply_reconciled
            and not machine.entries_blocked
            and not machine.orders_paused
            and not machine.degraded
        )

    def snapshot(self) -> dict:
        """Heartbeat / status payload. Writes nothing."""
        snap = self.machine.snapshot()
        snap["reply_reconciled"] = self.machine.reply_reconciled
        snap["entries_allowed"] = self.entries_allowed()
        return snap


def adopt_after_process_login(
    port,
    *,
    now: datetime,
    user_id: str,
    config: LinkMachineConfig | None = None,
    rules: MachineRules | None = None,
) -> LinkHost:
    """Build the one machine for this process after ``_do_login`` succeeded.

    Does not call ``start()`` and does not assign machine fields. The
    public ``LinkStateMachine.adopt_after_process_login`` owns that state.
    If process-start OnComplete never arrives, the machine stays
    ``orders_paused`` until ``stuck_limit``, then DEGRADED. Recovery is
    manual Reconnect or an operator restart.
    """
    cfg = config or LinkMachineConfig()
    cfg.user_id = user_id or cfg.user_id
    machine = LinkStateMachine(port, cfg, rules or MachineRules())
    machine.adopt_after_process_login(now)
    return LinkHost(machine)
