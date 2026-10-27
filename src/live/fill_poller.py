"""Fill confirmation polling: monitors position changes after order submission.

GUI-independent — no Tkinter, no COM. Returns FillPollAction dataclasses
that tell the caller what to do (start polling, confirm, timeout, etc.).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.live.trading_guard import TradingGuard


def _position_sign(qty: int) -> int:
    """+1 long, -1 short, 0 flat."""
    if qty > 0:
        return 1
    if qty < 0:
        return -1
    return 0


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

    # Last-resort alarm: OnNewData deal rows confirm in <1s (issue #103)
    # and OI polling in 2-6s, so reaching this means both broker report
    # channels are silent — alert the user fast rather than wait.
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
        # Snapshot of broker.entry_bar_index at the moment the real
        # entry order was sent. Used by the GUI's _on_fill_confirmed
        # guard to reject a late entry-fill callback that arrives
        # AFTER the original trade already closed (issue #45 race).
        self._entry_bar_index: int = 0
        # Resume only. +1 long, -1 short, None = any non-zero (a live
        # order that was not a resumed book). timeout() must not assume
        # a resumed entry filled unless this sign was actually seen.
        self._expected_sign: int | None = None
        self._resumed: bool = False

    def reset(self) -> None:
        """Clear all state."""
        self._active = False
        self._action_type = ""
        self._pos_before = 0
        self._pos_current = None
        self._start_time = 0
        self._entry_bar_index = 0
        self._expected_sign = None
        self._resumed = False

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

    @property
    def entry_bar_index(self) -> int:
        """Broker bar index at which the real entry order was sent.

        Used to match a late entry-fill confirmation to the trade
        that actually triggered it (issue #45).
        """
        return self._entry_bar_index

    # ── Start polling ──

    def start(self, action_type: str, current_position: int,
              com_available: bool = True,
              entry_bar_index: int = 0,
              expected_sign: int | None = None,
              resumed: bool = False) -> FillPollAction:
        """Begin monitoring for a fill.

        Args:
            action_type: "entry" or "exit"
            current_position: signed position qty before the order
            com_available: False if COM is unavailable (auto-confirm)
            entry_bar_index: broker bar index at order-send time;
                stored so _on_fill_confirmed can verify a late
                confirmation still belongs to the current trade
                (issue #45)
            expected_sign: +1 long / -1 short for a resumed entry.
                None keeps the historical "any non-zero" entry check.
            resumed: True when this poll continues an order from the
                previous process. ``timeout()`` will not assume that
                entry filled unless ``expected_sign`` was seen.

        Returns FillPollAction telling the caller what to do next.
        """
        # Always replace the previous run's resume bookkeeping, including
        # the no-COM return below. A later live order must not inherit
        # expected_sign/resumed — a stale long sign would reject a short
        # fill, and a stale resumed flag would refuse a normal timeout.
        self._expected_sign = expected_sign
        self._resumed = bool(resumed)
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
        self._entry_bar_index = entry_bar_index

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
          - entry: position != 0, and when ``expected_sign`` was set
            the sign must match (a real short must not confirm a
            resumed long)
          - exit:  position == 0 (we're flat now)

        Returns FillPollAction("confirmed") if fill detected, else None.
        """
        if not self._active:
            return None

        self._pos_current = signed_position

        confirmed = False
        if self._action_type == "entry":
            if self._expected_sign is None:
                confirmed = signed_position != 0
            else:
                confirmed = _position_sign(signed_position) == self._expected_sign
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

    # ── Deal confirmation from OnNewData ──

    def on_deal_confirmed(self, action_type: str) -> FillPollAction | None:
        """Called when an OnNewData deal row confirms a fill.

        OnNewData deal rows are the definitive proof that an order filled —
        they carry per-order price/qty keyed by the seq no from
        SendFutureOrderCLR.  When a matched deal arrives while fill polling
        is active, this is a faster confirmation path than waiting for the
        OpenInterest position to update (issue #103).

        Args:
            action_type: "entry" or "exit" — must match the pending poll

        Returns FillPollAction("confirmed") if the deal matches, else None.
        """
        if not self._active:
            return None
        if action_type != self._action_type:
            return None

        elapsed = time.monotonic() - self._start_time
        return FillPollAction(
            type="confirmed",
            action_type=self._action_type,
            message=f"confirmed via OnNewData deal row ({elapsed:.1f}s)",
        )

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

        # Compare ``now`` against the deadline directly rather than computing
        # ``elapsed = now - start`` and comparing to the timeout.  IEEE-754
        # subtraction can lose a ULP for certain monotonic values
        # (e.g. ``(7.9 + 10.0) - 7.9 == 9.999999999999998``), which made the
        # boundary test ``now == start + timeout`` flake on fresh-boot CI
        # runners where ``time.monotonic()`` returns small values.  The
        # additive form is exact whenever both sides are derived from the
        # same start + timeout, so the boundary check is deterministic.
        deadline = self._start_time + self.FILL_POLL_TIMEOUT
        elapsed = now - self._start_time

        if now >= deadline:
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

    def _resumed_entry_may_confirm(self) -> bool:
        """True when a resumed entry's book shows the expected sign.

        A live (non-resume) poll has no expected sign and may assume
        the fill. A resumed entry may not: timeout used to call
        ``on_entry_sent`` after 10s, and the deferred close then sold
        into a real short.
        """
        if not self._resumed or self._expected_sign is None:
            return True
        observed = (self._pos_current
                    if self._pos_current is not None else self._pos_before)
        return _position_sign(observed) == self._expected_sign

    def timeout(self) -> FillTimeoutResult:
        """Handle fill timeout. Clears pending state, returns downgrade info.

        Assumes the order DID fill (conservative for position safety):
          - entry timeout: assume we have a position → allow exits
          - exit timeout:  assume we closed → prevent double exits

        A resumed entry whose side was not seen is not assumed filled.
        The gate stays up so the deferred close cannot be replayed.
        """
        action_type = self._action_type

        if (action_type == "entry" and self._resumed
                and not self._resumed_entry_may_confirm()):
            self._active = False
            timeout_s = self.FILL_POLL_TIMEOUT
            return FillTimeoutResult(
                action_type=action_type,
                timeout_seconds=timeout_s,
                message=(f"成交超時 Fill timeout: resumed entry side did not "
                         f"match after {timeout_s:.0f}s — 未確認進場 "
                         f"entry NOT confirmed. 降級為半自動 downgraded to "
                         f"semi-auto. 請人工核對持倉 Manual review required."),
                new_trading_mode="semi_auto",
            )

        # Clear fill pending state — do NOT halt.
        # Any timeout that drops the gate also drops the deferred close,
        # whether the order was an entry or an exit. This is the only
        # timeout path the app uses (_on_fill_timeout).
        # TradingGuard.on_fill_timeout discards the close, but the app
        # never calls it. The close belonged to the order that just
        # timed out. The next entry confirm pops whatever is left, and
        # after the downgrade to semi-auto that confirm only happens
        # once the operator switches back to auto. An exit timeout had
        # the same leak before #171. The resumed-mismatch return above
        # keeps the gate up and does not reach here.
        self._guard.fill_pending = False
        self._guard.fill_pending_type = ""
        self._guard._deferred_close = None

        # Conservative assumption: order filled. A resumed entry is
        # the exception — assuming it filled lets _on_fill_confirmed
        # replay a deferred close into the wrong real book (issue #168).
        if action_type == "entry":
            if self._resumed_entry_may_confirm():
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
