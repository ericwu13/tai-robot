"""Connection monitoring: reconnection state machine with exponential backoff.

GUI-independent — no Tkinter, no COM. Returns ReconnectAction dataclasses
that tell the caller what to do (schedule timer, attempt now, give up, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReconnectAction:
    """Instruction returned by ConnectionMonitor for the GUI to execute."""
    type: str  # "attempt" | "defer_to_market" | "give_up" | "connected" | "resubscribe_retry"
    delay_seconds: int = 0
    attempt: int = 0
    max_attempts: int = 0
    message: str = ""


class ConnectionMonitor:
    """Reconnection state machine with exponential backoff.

    Pure logic — no Tkinter, no COM. Returns ReconnectAction instructions
    that the GUI layer executes via root.after() and COM calls.
    """

    RECONNECT_DELAYS: list[int] = [5, 10, 20, 30, 60]
    MAX_RECONNECT_ATTEMPTS: int = 10
    RESUBSCRIBE_MAX_RETRIES: int = 3
    RESUBSCRIBE_RETRY_DELAY_S: int = 5

    def __init__(self) -> None:
        self._attempt: int = 0
        self._active: bool = False

    def reset(self) -> None:
        """Reset all state (called on stop or new deploy)."""
        self._attempt = 0
        self._active = False

    @property
    def attempt(self) -> int:
        return self._attempt

    @property
    def is_active(self) -> bool:
        return self._active

    # ── Core state transitions ──

    def on_disconnected(self) -> ReconnectAction:
        """Called when connection loss is detected.

        Resets attempt counter and signals the caller to start reconnection.
        """
        self._attempt = 0
        self._active = True
        return ReconnectAction(
            type="start_reconnect",
            message="斷線 Disconnected — starting auto-reconnect",
        )

    def on_manual_reconnect(self) -> ReconnectAction:
        """User clicked reconnect button — attempt immediately."""
        self._attempt = 0
        self._active = True
        return ReconnectAction(
            type="attempt_now",
            message="手動重連中 Manual reconnecting...",
        )

    def on_connected(self) -> ReconnectAction:
        """Connection restored. Reset state."""
        self._attempt = 0
        self._active = False
        return ReconnectAction(type="connected")

    # ── Scheduling logic ──

    def schedule_next(self, has_live_runner: bool,
                      market_open: bool,
                      secs_until_open: int) -> ReconnectAction:
        """Determine the next reconnection action.

        Called after a failed attempt to decide: retry with backoff,
        defer to market open, or give up.

        Args:
            has_live_runner: True if a live bot is running
            market_open: True if market is currently open
            secs_until_open: seconds until next market session opens
        """
        # Max attempts reached
        if self._attempt >= self.MAX_RECONNECT_ATTEMPTS:
            # With live runner, defer to next market open
            if secs_until_open > 0 and has_live_runner:
                defer_secs = max(secs_until_open - 120, 60)
                self._attempt = 0  # reset for fresh cycle
                defer_mins = defer_secs // 60
                return ReconnectAction(
                    type="defer_to_market",
                    delay_seconds=defer_secs,
                    message=(f"休市中 Market closed — reconnecting in ~{defer_mins}m "
                             f"(before next session)"),
                )
            # No live runner or market open → give up
            self._active = False
            return ReconnectAction(
                type="give_up",
                message="自動重連失敗 Auto-reconnect failed — use Reconnect or Login button",
            )

        # Off-market hours with live runner — defer to near market open
        if not market_open and has_live_runner and secs_until_open > 120:
            defer_secs = max(secs_until_open - 120, 60)
            defer_mins = defer_secs // 60
            return ReconnectAction(
                type="defer_to_market",
                delay_seconds=defer_secs,
                message=(f"休市中 Market closed — deferring reconnect ~{defer_mins}m "
                         f"(before next session)"),
            )

        # Normal backoff
        idx = min(self._attempt, len(self.RECONNECT_DELAYS) - 1)
        delay = self.RECONNECT_DELAYS[idx]
        self._attempt += 1

        return ReconnectAction(
            type="attempt",
            delay_seconds=delay,
            attempt=self._attempt,
            max_attempts=self.MAX_RECONNECT_ATTEMPTS,
            message=(f"重連中 Reconnecting in {delay}s "
                     f"(attempt {self._attempt}/{self.MAX_RECONNECT_ATTEMPTS})..."),
        )

    # ── Resubscribe retry logic ──

    def should_retry_resubscribe(self, retry_count: int) -> ReconnectAction | None:
        """Check if tick resubscription should be retried.

        Returns a ReconnectAction with delay if retry is warranted,
        or None if max retries exceeded.
        """
        if retry_count < self.RESUBSCRIBE_MAX_RETRIES:
            return ReconnectAction(
                type="resubscribe_retry",
                delay_seconds=self.RESUBSCRIBE_RETRY_DELAY_S,
                attempt=retry_count + 1,
                max_attempts=self.RESUBSCRIBE_MAX_RETRIES,
                message=(f"重試訂閱 Retrying tick subscribe in {self.RESUBSCRIBE_RETRY_DELAY_S}s "
                         f"(attempt {retry_count + 1}/{self.RESUBSCRIBE_MAX_RETRIES})..."),
            )
        return None
