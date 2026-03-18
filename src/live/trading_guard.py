"""Trading safety guard: pre-trade checks for semi-auto and auto modes.

Encapsulates all safety logic in a GUI-independent class so it can be
unit-tested without Tkinter or COM dependencies.
"""

from __future__ import annotations


class TradingGuard:
    """Validates orders against safety rules before they are sent.

    Tracks real position state, daily P&L, and margin requirements.
    All mutating methods return a (allowed: bool, reason: str) tuple.
    """

    def __init__(self, daily_loss_limit: int = 1000):
        self.daily_loss_limit: int = daily_loss_limit
        self.real_entry_confirmed: bool = False
        self.paused: bool = False  # True when daily loss limit hit
        self._last_net_pnl: int = 0

    def reset(self) -> None:
        """Reset all state (called on new deploy)."""
        self.real_entry_confirmed = False
        self.paused = False
        self._last_net_pnl = 0

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
        """Called when a real entry order is successfully sent."""
        self.real_entry_confirmed = True

    def on_exit_sent(self) -> None:
        """Called when a real exit order is successfully sent."""
        self.real_entry_confirmed = False

    def on_entry_skipped(self) -> None:
        """Called when user skips/times out on entry confirmation."""
        # real_entry_confirmed stays False — exits won't auto-send
        pass

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
    BLOCK_ENTRY = "block_entry"      # daily loss limit
    SKIP_EXIT = "skip_exit"          # no real position to close
    SEND_EXIT = "send_exit"          # auto-send exit order
    SEND_ENTRY = "send_entry"        # auto-send entry (auto mode)
    CONFIRM_ENTRY = "confirm_entry"  # show dialog (semi-auto mode)

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

        details = {
            "buy_sell": buy_sell,
            "action_type": action_type,
            "new_close": self.order_params(action_type)["new_close"],
        }

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
        - exit:  ROD (stay until filled), sNewClose=1 (close only)
        """
        if action_type == "entry":
            return {"trade_type": 1, "new_close": 0}  # IOC, new
        else:
            return {"trade_type": 0, "new_close": 1}  # ROD, close
