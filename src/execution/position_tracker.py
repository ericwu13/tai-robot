"""Real-time position tracking from fill events."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    qty: int = 0              # positive=long, negative=short
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    last_update: datetime | None = None


@dataclass
class Fill:
    symbol: str
    buy_sell: int    # 0=buy, 1=sell
    price: float
    qty: int
    dt: datetime | None = None


class PositionTracker:
    """Maintains real-time position state from fill events."""

    def __init__(self):
        self._positions: dict[str, Position] = {}
        self._daily_realized_pnl: float = 0.0
        self._fills: list[Fill] = []

    @property
    def daily_realized_pnl(self) -> float:
        return self._daily_realized_pnl

    def get_position(self, symbol: str) -> Position:
        if symbol not in self._positions:
            self._positions[symbol] = Position(symbol=symbol)
        return self._positions[symbol]

    def on_fill(self, fill: Fill) -> None:
        """Process a fill event and update position."""
        self._fills.append(fill)
        pos = self.get_position(fill.symbol)

        signed_qty = fill.qty if fill.buy_sell == 0 else -fill.qty

        if pos.qty == 0:
            # Opening a new position
            pos.qty = signed_qty
            pos.avg_price = fill.price
        elif (pos.qty > 0 and signed_qty > 0) or (pos.qty < 0 and signed_qty < 0):
            # Adding to existing position
            total_cost = pos.avg_price * abs(pos.qty) + fill.price * fill.qty
            pos.qty += signed_qty
            pos.avg_price = total_cost / abs(pos.qty) if pos.qty != 0 else 0
        else:
            # Reducing or flipping position
            close_qty = min(abs(signed_qty), abs(pos.qty))
            if pos.qty > 0:
                pnl = (fill.price - pos.avg_price) * close_qty
            else:
                pnl = (pos.avg_price - fill.price) * close_qty

            pos.realized_pnl += pnl
            self._daily_realized_pnl += pnl
            pos.qty += signed_qty

            if pos.qty == 0:
                pos.avg_price = 0
            elif abs(signed_qty) > close_qty:
                # Position flipped
                pos.avg_price = fill.price

        pos.last_update = fill.dt
        logger.info(
            "Position update: %s qty=%d avg=%.1f realized=%.1f",
            fill.symbol, pos.qty, pos.avg_price, pos.realized_pnl,
        )

    def update_unrealized(self, symbol: str, current_price: float) -> None:
        """Update unrealized P&L with the current market price."""
        pos = self.get_position(symbol)
        if pos.qty == 0:
            pos.unrealized_pnl = 0
        elif pos.qty > 0:
            pos.unrealized_pnl = (current_price - pos.avg_price) * pos.qty
        else:
            pos.unrealized_pnl = (pos.avg_price - current_price) * abs(pos.qty)

    def reset_daily(self) -> None:
        """Reset daily P&L counters (call at start of trading day)."""
        self._daily_realized_pnl = 0.0
        self._fills.clear()
