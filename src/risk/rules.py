"""Pre-trade risk rules. All rules must pass for an order to be sent."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque

from ..execution.position_tracker import PositionTracker
from ..market_data.models import Signal, Direction


class RiskRule(ABC):
    @abstractmethod
    def check(self, signal: Signal, symbol: str, qty: int,
              tracker: PositionTracker) -> str | None:
        """Return None if OK, or an error message string if rejected."""
        ...


class MaxPositionRule(RiskRule):
    """Reject if projected position exceeds limit."""

    def __init__(self, max_position: int):
        self.max_position = max_position

    def check(self, signal: Signal, symbol: str, qty: int,
              tracker: PositionTracker) -> str | None:
        pos = tracker.get_position(symbol)
        if signal.direction == Direction.BUY:
            projected = pos.qty + qty
        elif signal.direction == Direction.SELL:
            projected = pos.qty - qty
        else:
            return None

        if abs(projected) > self.max_position:
            return (f"Projected position {projected} exceeds max {self.max_position} "
                    f"(current={pos.qty}, order_qty={qty})")
        return None


class MaxDailyLossRule(RiskRule):
    """Reject if cumulative daily realized loss exceeds limit."""

    def __init__(self, max_daily_loss: float):
        self.max_daily_loss = max_daily_loss

    def check(self, signal: Signal, symbol: str, qty: int,
              tracker: PositionTracker) -> str | None:
        if tracker.daily_realized_pnl < -self.max_daily_loss:
            return (f"Daily loss {tracker.daily_realized_pnl:.0f} "
                    f"exceeds limit -{self.max_daily_loss:.0f}")
        return None


class OrderRateLimitRule(RiskRule):
    """Reject if too many orders in the last minute."""

    def __init__(self, max_per_minute: int):
        self.max_per_minute = max_per_minute
        self._timestamps: deque[float] = deque()

    def check(self, signal: Signal, symbol: str, qty: int,
              tracker: PositionTracker) -> str | None:
        now = time.time()
        # Purge old timestamps
        while self._timestamps and now - self._timestamps[0] > 60:
            self._timestamps.popleft()

        if len(self._timestamps) >= self.max_per_minute:
            return f"Order rate limit exceeded ({self.max_per_minute}/min)"

        self._timestamps.append(now)
        return None


class MaxDrawdownRule(RiskRule):
    """Reject if equity drawdown exceeds percentage threshold."""

    def __init__(self, max_drawdown_pct: float, starting_equity: float = 0):
        self.max_drawdown_pct = max_drawdown_pct
        self.starting_equity = starting_equity
        self.peak_equity = starting_equity

    def set_equity(self, equity: float) -> None:
        if equity > self.peak_equity:
            self.peak_equity = equity

    def check(self, signal: Signal, symbol: str, qty: int,
              tracker: PositionTracker) -> str | None:
        if self.peak_equity <= 0:
            return None  # Not tracking equity yet

        current = self.peak_equity + tracker.daily_realized_pnl
        drawdown_pct = (self.peak_equity - current) / self.peak_equity * 100

        if drawdown_pct > self.max_drawdown_pct:
            return (f"Drawdown {drawdown_pct:.1f}% exceeds limit {self.max_drawdown_pct}%")
        return None
