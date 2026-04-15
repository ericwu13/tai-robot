"""Trading safety guard: pre-trade checks for semi-auto and auto modes.

Encapsulates all safety logic in a GUI-independent class so it can be
unit-tested without Tkinter or COM dependencies.
"""

from __future__ import annotations


class TradingGuard:
    """Validates orders against safety rules before they are sent.

    Tracks real position state, daily P&L, fill confirmation, and margin.
    All mutating methods return a (allowed: bool, reason: str) tuple.
    """

    def __init__(self, daily_loss_limit: int = 10000):
        self.daily_loss_limit: int = daily_loss_limit
        self.real_entry_confirmed: bool = False
        self.paused: bool = False  # True when daily loss limit hit
        self._last_net_pnl: int = 0

        # Fill confirmation gate (auto mode only)
        self.fill_pending: bool = False
        self.fill_pending_type: str = ""  # "entry" or "exit"
        self.halted: bool = False
        self.halt_reason: str = ""

        # Deferred close (issue #50). When a TRADE_CLOSE is blocked by
        # BLOCK_FILL_PENDING, the decision is stored here. When
        # _on_fill_confirmed("entry") fires, the caller pops and replays
        # the deferred close so the real exit order isn't permanently lost.
        self._deferred_close: dict | None = None

    def reset(self) -> None:
        """Reset all state (called on new deploy)."""
        self.real_entry_confirmed = False
        self.paused = False
        self._last_net_pnl = 0
        self.fill_pending = False
        self.fill_pending_type = ""
        self.halted = False
        self.halt_reason = ""
        self._deferred_close = None

    # ── Entry / exit gating ──

    def check_entry(self, trading_mode: str) -> tuple[bool, str]:
        """Check if an entry order should proceed.

        Returns (allowed, reason).
        """
        if self.paused:
            return False, f"daily loss limit reached ({self.daily_loss_limit:,} NTD)"
        return True, ""

    def check_exit(self, action: str) -> tuple[bool, str]:
        """Check if an exit order should proceed.

        Exits are only sent if we confirmed a real entry.  This prevents
        sending close orders when there is no real position (the bug that
        caused issue #17).

        Returns (allowed, reason).
        """
        if not self.real_entry_confirmed:
            return False, f"{action.lower()} — no real entry was confirmed"
        return True, ""

    # ── State updates ──

    def on_entry_sent(self) -> None:
        """Called when a real entry order is confirmed filled."""
        self.real_entry_confirmed = True

    def on_exit_sent(self) -> None:
        """Called when a real exit order is confirmed filled."""
        self.real_entry_confirmed = False

    def on_entry_skipped(self) -> None:
        """Called when user skips/times out on entry confirmation."""
        # real_entry_confirmed stays False — exits won't auto-send
        pass

    # ── Fill confirmation gate ──

    def on_fill_pending(self, action_type: str) -> None:
        """Called after a real order is accepted (code==0) in auto mode.

        Blocks all new real orders until fill is confirmed or timeout.
        """
        self.fill_pending = True
        self.fill_pending_type = action_type

    def on_fill_confirmed(self) -> None:
        """Called when GetFulfillReport shows a new fill."""
        self.fill_pending = False
        self.fill_pending_type = ""

    def on_fill_timeout(self) -> None:
        """Called when fill is not confirmed within the timeout.

        Enters HALTED state — all orders permanently blocked until
        manual intervention via clear_halt().
        """
        self.halted = True
        self.halt_reason = f"{self.fill_pending_type} fill not confirmed"
        self.fill_pending = False
        self.fill_pending_type = ""
        self._deferred_close = None  # discard — system is halting

    # ── Deferred close (issue #50) ──

    def defer_close(self, decision: dict) -> None:
        """Store a close decision that was blocked by fill_pending.

        Called when a TRADE_CLOSE is rejected with BLOCK_FILL_PENDING.
        The caller retrieves it via pop_deferred_close() after the
        entry fill is confirmed, so the exit order isn't permanently
        lost.
        """
        self._deferred_close = decision

    def pop_deferred_close(self) -> dict | None:
        """Retrieve and clear the stored deferred close, if any."""
        d = self._deferred_close
        self._deferred_close = None
        return d

    def clear_halt(self) -> None:
        """Manual reset of halted state after human verification."""
        self.halted = False
        self.halt_reason = ""

    # ── Margin check ──

    @staticmethod
    def check_margin(available: float, required: int) -> tuple[bool, str]:
        """Check if available margin is sufficient for a new position.

        Only applies to entries. Exits bypass this check.
        Returns (allowed, reason).
        """
        if required > 0 and available < required:
            return False, f"insufficient margin: available {available:,.0f} < required {required:,}"
        return True, ""

    # ── Daily loss limit ──

    def update_pnl(self, net_pnl: int) -> bool:
        """Update daily P&L and check loss limit.

        Returns True if the limit was JUST triggered (for one-time logging).
        """
        self._last_net_pnl = net_pnl
        if (self.daily_loss_limit > 0
                and net_pnl < -self.daily_loss_limit
                and not self.paused):
            self.paused = True
            return True  # just triggered
        return False

    # ── Decision engine ──

    # Decision constants
    BLOCK_ENTRY = "block_entry"              # daily loss limit
    SKIP_EXIT = "skip_exit"                  # no real position to close
    SEND_EXIT = "send_exit"                  # auto-send exit order
    SEND_ENTRY = "send_entry"                # auto-send entry (auto mode)
    CONFIRM_ENTRY = "confirm_entry"          # show dialog (semi-auto mode)
    BLOCK_FILL_PENDING = "block_fill_pending"  # waiting for prior fill
    BLOCK_HALTED = "block_halted"            # system halted after fill timeout

    def decide(self, trading_mode: str, action: str, side: str) -> tuple[str, dict]:
        """Decide what to do with a simulated fill.

        This is the core decision function that _handle_semi_auto_order calls.
        Returns (decision, details) where decision is one of the constants above
        and details contains buy_sell, action_type, new_close.

        Args:
            trading_mode: "semi_auto" or "auto"
            action: "ENTRY_FILL", "TRADE_CLOSE", or "FORCE_CLOSE"
            side: "LONG" or "SHORT"
        """
        # Determine order direction
        if action == "ENTRY_FILL":
            buy_sell = 0 if side == "LONG" else 1
            action_type = "entry"
        else:
            buy_sell = 1 if side == "LONG" else 0  # reverse to close
            action_type = "exit"

        # Force-close: use sNewClose=1 (explicit close) instead of 2 (auto).
        # We KNOW a position exists (caller checked position_side), and auto
        # can misclassify close as open, causing error 980 (insufficient margin).
        if action == "FORCE_CLOSE":
            new_close = 1
        else:
            new_close = self.order_params(action_type)["new_close"]

        details = {
            "buy_sell": buy_sell,
            "action_type": action_type,
            "new_close": new_close,
        }

        # Fill confirmation gate (blocks entries and normal exits)
        # FORCE_CLOSE bypasses — user's emergency exit must not be blocked
        if action != "FORCE_CLOSE":
            if self.halted:
                details["reason"] = f"system halted: {self.halt_reason}"
                return self.BLOCK_HALTED, details

            if self.fill_pending:
                details["reason"] = (
                    f"waiting for {self.fill_pending_type} fill confirmation")
                return self.BLOCK_FILL_PENDING, details

        # Entry: check daily loss limit
        if action == "ENTRY_FILL":
            allowed, reason = self.check_entry(trading_mode)
            if not allowed:
                details["reason"] = reason
                return self.BLOCK_ENTRY, details

            if trading_mode == "auto":
                return self.SEND_ENTRY, details
            return self.CONFIRM_ENTRY, details

        # Exit: check if real position exists
        allowed, reason = self.check_exit(action)
        if not allowed:
            details["reason"] = reason
            return self.SKIP_EXIT, details

        return self.SEND_EXIT, details

    # ── Order parameters ──

    @staticmethod
    def order_params(action_type: str) -> dict:
        """Return order parameters based on action type.

        - entry: IOC (cancel if no immediate fill), sNewClose=0 (new only)
        - exit:  ROD (stay until filled), sNewClose=2 (auto — avoids 980 if already flat)
        """
        if action_type == "entry":
            return {"trade_type": 1, "new_close": 0}  # IOC, new
        else:
            return {"trade_type": 0, "new_close": 2}  # ROD, auto (exchange decides)
