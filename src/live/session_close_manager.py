"""Session close manager: force-close state machine for live trading.

Extracts session-end close logic from the GUI into a standalone,
testable module with injectable dependencies.

Two separate mechanisms, clearly decoupled:
1. Force-close (hard stop) -- universal clock-based, same for all intervals.
2. Session-end-pending -- blocks new entries when approaching close.

The GUI timer calls ``tick()`` periodically; the tick handler calls
``on_market_tick()`` for emergency sweep.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from ..market_data.sessions import minutes_until_close as _default_minutes_until_close


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class CloseState(Enum):
    """States for the session close state machine."""
    NORMAL = "NORMAL"
    APPROACHING_CLOSE = "APPROACHING_CLOSE"
    FORCE_CLOSE_ATTEMPT_1 = "FORCE_CLOSE_ATTEMPT_1"
    FORCE_CLOSE_ATTEMPT_2 = "FORCE_CLOSE_ATTEMPT_2"
    FORCE_CLOSE_ATTEMPT_3 = "FORCE_CLOSE_ATTEMPT_3"
    EMERGENCY_TICK_SWEEP = "EMERGENCY_TICK_SWEEP"
    SESSION_END_PENDING = "SESSION_END_PENDING"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minutes before session close to start watching / acting
APPROACHING_MINUTES = 5
EMERGENCY_MINUTES = 1

# Recommended polling intervals (seconds) returned by tick()
POLL_NORMAL = 30
POLL_APPROACHING = 5
POLL_FORCE_CLOSE = 2
POLL_EMERGENCY = 1

# Seconds to wait for fill confirmation before retrying
ATTEMPT_TIMEOUTS = {
    CloseState.FORCE_CLOSE_ATTEMPT_1: 15,
    CloseState.FORCE_CLOSE_ATTEMPT_2: 10,
    CloseState.FORCE_CLOSE_ATTEMPT_3: 10,
}

# State to transition to on timeout (None = final attempt)
_NEXT_ATTEMPT = {
    CloseState.FORCE_CLOSE_ATTEMPT_1: CloseState.FORCE_CLOSE_ATTEMPT_2,
    CloseState.FORCE_CLOSE_ATTEMPT_2: CloseState.FORCE_CLOSE_ATTEMPT_3,
}

_FORCE_CLOSE_STATES = frozenset({
    CloseState.FORCE_CLOSE_ATTEMPT_1,
    CloseState.FORCE_CLOSE_ATTEMPT_2,
    CloseState.FORCE_CLOSE_ATTEMPT_3,
})

# ---------------------------------------------------------------------------
# Event names
# ---------------------------------------------------------------------------

EVT_FORCE_CLOSE_STARTED = "force_close_started"
EVT_FORCE_CLOSE_RETRIED = "force_close_retried"
EVT_FORCE_CLOSE_FAILED = "force_close_failed"
EVT_EMERGENCY_SWEEP = "emergency_sweep"
EVT_SESSION_END_PENDING_SET = "session_end_pending_set"
EVT_SESSION_END_PENDING_CLEARED = "session_end_pending_cleared"


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class SessionCloseManager:
    """Force-close state machine for session end.

    All dependencies are injectable so the class can be unit-tested
    without GUI, COM, or real clocks.
    """

    def __init__(
        self,
        *,
        force_close_fn,
        minutes_until_close_fn=None,
        clear_deferred_close_fn=None,
        on_event=None,
        alarm_fn=None,
    ):
        """Create a new SessionCloseManager.

        Args:
            force_close_fn: ``(attempt: int) -> bool``.
                Send a force-close order.  Return True if the order was
                accepted by the exchange gateway, False on send failure.
            minutes_until_close_fn: ``(dt: datetime) -> int | None``.
                Override for testing; defaults to
                ``sessions.minutes_until_close``.
            clear_deferred_close_fn: ``() -> None``.
                Clear any deferred close stored by TradingGuard (issue #50)
                to prevent double-fire when force-close takes over.
            on_event: ``(event: str, detail: dict) -> None``.
                Callback for state-machine events (logging, GUI updates).
            alarm_fn: ``(message: str) -> None``.
                Emergency alarm callback (beep, popup, Discord notify).
        """
        self._force_close = force_close_fn
        self._minutes_until_close = (
            minutes_until_close_fn or _default_minutes_until_close
        )
        self._clear_deferred = clear_deferred_close_fn or (lambda: None)
        self._on_event = on_event or (lambda e, d: None)
        self._alarm = alarm_fn or (lambda m: None)

        self.state: CloseState = CloseState.NORMAL
        self.session_end_pending: bool = False
        self._attempt: int = 0
        self._attempt_sent_at: datetime | None = None
        self._emergency_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def tick(self, now: datetime, position_size: int) -> int:
        """Process one clock tick of the state machine.

        Args:
            now: Current time (Taipei timezone, naive or aware).
            position_size: Effective position size.  For live trading,
                pass ``broker.position_size or (1 if guard.fill_pending
                else 0)`` so that pending fills are treated as open.

        Returns:
            Recommended seconds until next call.
        """
        mins = self._minutes_until_close(now)

        if self.state == CloseState.NORMAL:
            return self._tick_normal(now, position_size, mins)
        if self.state == CloseState.APPROACHING_CLOSE:
            return self._tick_approaching(now, position_size, mins)
        if self.state in _FORCE_CLOSE_STATES:
            return self._tick_force_close(now, position_size, mins)
        if self.state == CloseState.EMERGENCY_TICK_SWEEP:
            return self._tick_emergency(now, position_size, mins)
        if self.state == CloseState.SESSION_END_PENDING:
            return self._tick_session_end(now, position_size, mins)
        return POLL_NORMAL  # pragma: no cover

    def on_market_tick(self, now: datetime, position_size: int) -> bool:
        """Called on each market tick for emergency sweep.

        Only acts in ``EMERGENCY_TICK_SWEEP`` state.  Sends a force-close
        order on every tick until the position is flat.

        Returns:
            True if a force-close was attempted on this tick.
        """
        if self.state != CloseState.EMERGENCY_TICK_SWEEP:
            return False
        if position_size == 0:
            self._enter_session_end_pending()
            return False
        self._emergency_count += 1
        self._on_event(EVT_EMERGENCY_SWEEP, {
            "attempt": self._emergency_count,
            "time": now.isoformat(),
        })
        return self._force_close(self._attempt)

    def reset(self) -> None:
        """Reset to NORMAL state (new deploy or manual reset)."""
        was_pending = self.session_end_pending
        self.state = CloseState.NORMAL
        self.session_end_pending = False
        self._attempt = 0
        self._attempt_sent_at = None
        self._emergency_count = 0
        if was_pending:
            self._on_event(EVT_SESSION_END_PENDING_CLEARED, {})

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _tick_normal(self, now, pos, mins):
        if mins is not None and mins <= APPROACHING_MINUTES:
            self._set_state(CloseState.APPROACHING_CLOSE)
            return self._tick_approaching(now, pos, mins)
        return POLL_NORMAL

    def _tick_approaching(self, now, pos, mins):
        # Market closed while approaching
        if mins is None:
            self._enter_session_end_pending()
            return POLL_NORMAL

        # Session changed (new session far from close)
        if mins > APPROACHING_MINUTES:
            self._set_state(CloseState.NORMAL)
            if self.session_end_pending:
                self.session_end_pending = False
                self._on_event(EVT_SESSION_END_PENDING_CLEARED, {})
            return POLL_NORMAL

        # Set session_end_pending flag (blocks new entries)
        if not self.session_end_pending:
            self.session_end_pending = True
            self._on_event(EVT_SESSION_END_PENDING_SET, {"minutes": mins})

        # No position -- just wait
        if pos == 0:
            return POLL_APPROACHING

        # Position open -- force close needed
        if mins <= EMERGENCY_MINUTES:
            self._set_state(CloseState.EMERGENCY_TICK_SWEEP)
            self._send_force_close(now, 1)
            return POLL_EMERGENCY

        self._set_state(CloseState.FORCE_CLOSE_ATTEMPT_1)
        self._send_force_close(now, 1)
        return POLL_FORCE_CLOSE

    def _tick_force_close(self, now, pos, mins):
        # Position went flat (fill confirmed or strategy closed it)
        if pos == 0:
            self._enter_session_end_pending()
            return POLL_NORMAL

        # Market closed mid-attempt
        if mins is None:
            self._on_event(EVT_FORCE_CLOSE_FAILED, {
                "reason": "market closed with position open",
                "attempt": self._attempt,
            })
            self._alarm("CRITICAL: Market closed with position still open!")
            self._enter_session_end_pending()
            return POLL_NORMAL

        # Escalate to emergency sweep
        if mins <= EMERGENCY_MINUTES:
            self._set_state(CloseState.EMERGENCY_TICK_SWEEP)
            return POLL_EMERGENCY

        # Check retry timeout
        timeout = ATTEMPT_TIMEOUTS.get(self.state, 10)
        if self._attempt_sent_at is not None:
            elapsed = (now - self._attempt_sent_at).total_seconds()
            if elapsed >= timeout:
                next_state = _NEXT_ATTEMPT.get(self.state)
                if next_state is not None:
                    # Transition to next retry attempt
                    self._attempt += 1
                    self._set_state(next_state)
                    self._send_force_close(now, self._attempt)
                    self._on_event(EVT_FORCE_CLOSE_RETRIED, {
                        "attempt": self._attempt,
                        "time": now.isoformat(),
                    })
                else:
                    # Final attempt exhausted -- alarm and reset timer
                    self._on_event(EVT_FORCE_CLOSE_FAILED, {
                        "reason": "all retry attempts exhausted",
                        "attempt": self._attempt,
                    })
                    self._alarm(
                        f"CRITICAL: Force close failed after {self._attempt} "
                        f"attempts! Position still open, {mins} min to close."
                    )
                    # Reset sent_at so we keep retrying until emergency
                    self._attempt_sent_at = now

        return POLL_FORCE_CLOSE

    def _tick_emergency(self, now, pos, mins):
        if pos == 0:
            self._enter_session_end_pending()
            return POLL_NORMAL
        if mins is None:
            self._alarm(
                "CRITICAL: Market closed during emergency sweep! "
                "Position may still be open."
            )
            self._enter_session_end_pending()
            return POLL_NORMAL
        # Keep sweeping -- actual close happens in on_market_tick()
        return POLL_EMERGENCY

    def _tick_session_end(self, now, pos, mins):
        if not self.session_end_pending:
            self.session_end_pending = True
            self._on_event(EVT_SESSION_END_PENDING_SET, {})

        # New session started (far from close)
        if mins is not None and mins > APPROACHING_MINUTES:
            self._set_state(CloseState.NORMAL)
            self.session_end_pending = False
            self._attempt = 0
            self._attempt_sent_at = None
            self._emergency_count = 0
            self._on_event(EVT_SESSION_END_PENDING_CLEARED, {})
            return POLL_NORMAL

        return POLL_NORMAL

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _set_state(self, new_state: CloseState) -> None:
        """Transition to a new state."""
        self.state = new_state

    def _send_force_close(self, now: datetime, attempt: int) -> bool:
        """Send force-close order and update tracking state."""
        self._attempt = attempt
        self._attempt_sent_at = now

        # Clear deferred close to prevent double-fire (issue #50)
        self._clear_deferred()

        if attempt == 1:
            self._on_event(EVT_FORCE_CLOSE_STARTED, {
                "attempt": attempt,
                "time": now.isoformat(),
            })

        return self._force_close(attempt)

    def _enter_session_end_pending(self) -> None:
        """Transition to SESSION_END_PENDING and set the flag."""
        self._set_state(CloseState.SESSION_END_PENDING)
        if not self.session_end_pending:
            self.session_end_pending = True
            self._on_event(EVT_SESSION_END_PENDING_SET, {})
