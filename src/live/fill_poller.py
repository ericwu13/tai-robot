"""Fill confirmation polling: monitors position changes after order submission.

GUI-independent — no Tkinter, no COM. Returns FillPollAction dataclasses
that tell the caller what to do (start polling, confirm, timeout, etc.).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.live.trading_guard import TradingGuard


@dataclass
class FillPollAction:
    """Instruction returned by FillPoller for the GUI to execute."""
    type: str  # "start_polling" | "already_confirmed" | "confirmed" | "timeout" | "poll_again" | "no_com"
    delay_ms: int = 0
    action_type: str = ""  # "entry" | "exit"
    message: str = ""


@dataclass
class FillConfirmResult:
    """Details of a confirmed fill."""
    action_type: str
    message: str


@dataclass
class FillTimeoutResult:
    """Details of a fill timeout."""
    action_type: str
    timeout_seconds: float
    message: str
    new_trading_mode: str = "semi_auto"


class FillPoller:
    """Tracks fill confirmation via position change monitoring.

    After a real order is sent, monitors position changes from
    OpenInterest callbacks to confirm the fill actually occurred.

    No Tkinter, no COM dependencies.
    """

    FILL_POLL_TIMEOUT: float = 10.0  # seconds
    INITIAL_POLL_DELAY_MS: int = 2000  # first poll after 2s
    POLL_INTERVAL_MS: int = 3000  # subsequent polls every 3s

    def __init__(self, guard: TradingGuard, timeout: float | None = None) -> None:
        self._guard = guard
        if timeout is not None:
            self.FILL_POLL_TIMEOUT = timeout
        self._active: bool = False
        self._action_type: str = ""
        self._pos_before: int = 0
        self._pos_current: int | None = None
        self._start_time: float = 0

    def reset(self) -> None:
        """Clear all state."""
        self._active = False
        self._action_type = ""
        self._pos_before = 0
        self._pos_current = None
        self._start_time = 0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def action_type(self) -> str:
        return self._action_type

    @property
    def pos_before(self) -> int:
        return self._pos_before

    @property
    def pos_current(self) -> int | None:
        return self._pos_current

    # ── Start polling ──

    def start(self, action_type: str, current_position: int,
              com_available: bool = True) -> FillPollAction:
        """Begin monitoring for a fill.

        Args:
            action_type: "entry" or "exit"
            current_position: signed position qty before the order
            com_available: False if COM is unavailable (auto-confirm)

        Returns FillPollAction telling the caller what to do next.
        """
        if not com_available:
            # Auto-confirm immediately when COM unavailable
            self._guard.on_fill_confirmed()
            if action_type == "entry":
                self._guard.on_entry_sent()
            elif action_type == "exit":
                self._guard.on_exit_sent()
            return FillPollAction(
                type="no_com",
                action_type=action_type,
                message="COM unavailable — fill auto-confirmed",
            )

        self._active = True
        self._action_type = action_type
        self._pos_before = current_position
        self._pos_current = None
        self._start_time = time.monotonic()

        # Exit already flat — IOC filled before we could even read position
        if action_type == "exit" and current_position == 0:
            return FillPollAction(
                type="already_confirmed",
                action_type=action_type,
                message=f"exit already flat — confirming immediately "
                        f"(position={current_position:+d})",
            )

        return FillPollAction(
            type="start_polling",
            delay_ms=self.INITIAL_POLL_DELAY_MS,
            action_type=action_type,
            message=f"等待成交確認 Waiting for {action_type} fill confirmation "
                    f"(position={current_position:+d})",
        )

    # ── Position update from OI callback ──

    def on_position_update(self, signed_position: int) -> FillPollAction | None:
        """Called when OpenInterest callback reports a matching position.

        Uses target-state checking (NOT before/after delta):
          - entry: position != 0 (we have a position now)
          - exit:  position == 0 (we're flat now)

        Returns FillPollAction("confirmed") if fill detected, else None.
        """
        if not self._active:
            return None

        self._pos_current = signed_position

        confirmed = False
        if self._action_type == "entry":
            confirmed = signed_position != 0
        elif self._action_type == "exit":
            confirmed = signed_position == 0

        if confirmed:
            elapsed = time.monotonic() - self._start_time
            return FillPollAction(
                type="confirmed",
                action_type=self._action_type,
                message=f"position {self._pos_before} -> {signed_position} "
                        f"({elapsed:.1f}s)",
            )
        return None

    # ── Poll tick (called on timer) ──

    def check_poll(self, now: float | None = None) -> FillPollAction:
        """Called on each poll tick to check timeout or schedule next poll.

        Args:
            now: monotonic time (injectable for testing)

        Returns:
            FillPollAction with type "timeout" or "poll_again"
        """
        if now is None:
            now = time.monotonic()

        elapsed = now - self._start_time

        if elapsed >= self.FILL_POLL_TIMEOUT:
            return FillPollAction(
                type="timeout",
                action_type=self._action_type,
                message=f"TIMEOUT after {elapsed:.1f}s, "
                        f"position: {self._pos_before} -> {self._pos_current}",
            )

        return FillPollAction(
            type="poll_again",
            delay_ms=self.POLL_INTERVAL_MS,
            action_type=self._action_type,
            message=f"pending... position={self._pos_current} "
                    f"(baseline={self._pos_before}, {elapsed:.1f}s elapsed)",
        )

    # ── Finalize ──

    def confirm(self) -> FillConfirmResult:
        """Finalize fill confirmation. Updates TradingGuard state.

        Must be called after on_position_update() returns "confirmed"
        or after start() returns "already_confirmed".
        """
        action_type = self._action_type

        self._guard.on_fill_confirmed()
        if action_type == "entry":
            self._guard.on_entry_sent()
        elif action_type == "exit":
            self._guard.on_exit_sent()

        self._active = False
        return FillConfirmResult(
            action_type=action_type,
            message=f"成交已確認 {action_type.upper()} fill confirmed",
        )

    def timeout(self) -> FillTimeoutResult:
        """Handle fill timeout. Clears pending state, returns downgrade info.

        Assumes the order DID fill (conservative for position safety):
          - entry timeout: assume we have a position → allow exits
          - exit timeout:  assume we closed → prevent double exits
        """
        action_type = self._action_type

        # Clear fill pending state — do NOT halt
        self._guard.fill_pending = False
        self._guard.fill_pending_type = ""

        # Conservative assumption: order filled
        if action_type == "entry":
            self._guard.on_entry_sent()
        elif action_type == "exit":
            self._guard.on_exit_sent()

        self._active = False
        timeout_s = self.FILL_POLL_TIMEOUT
        return FillTimeoutResult(
            action_type=action_type,
            timeout_seconds=timeout_s,
            message=(f"成交超時 Fill timeout: {action_type} not confirmed after "
                     f"{timeout_s:.0f}s — 降級為半自動 downgraded to semi-auto. "
                     f"策略仍可出場 Strategy exits still active, "
                     f"新進場需手動確認 new entries require confirmation."),
            new_trading_mode="semi_auto",
        )
