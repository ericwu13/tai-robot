"""Execution engine: Signal -> order conversion with mode awareness.

Supports paper, semi_auto, and full_auto modes.
"""

from __future__ import annotations

import logging
import sys
import threading

from ..config.settings import AppConfig
from ..gateway.order_gateway import OrderGateway, OrderRequest
from ..market_data.models import Direction, Signal
from ..risk.manager import RiskManager
from ..utils.errors import RiskLimitError
from .order_manager import ManagedOrder, OrderManager
from .position_tracker import Fill, PositionTracker

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """Converts strategy signals into orders, respecting trading mode and risk."""

    def __init__(
        self,
        config: AppConfig,
        order_gateway: OrderGateway | None,
        risk_manager: RiskManager,
        position_tracker: PositionTracker,
        order_manager: OrderManager,
    ):
        self._config = config
        self._gateway = order_gateway
        self._risk = risk_manager
        self._tracker = position_tracker
        self._order_manager = order_manager
        self._mode = config.trading.mode

    def on_signal(self, signal: Signal) -> bool:
        """Process a strategy signal. Returns True if an order was sent."""
        if signal.direction == Direction.FLAT:
            return False

        symbol = self._config.trading.symbol
        qty = self._config.trading.default_qty

        # Risk check
        try:
            self._risk.check(signal, symbol, qty)
        except RiskLimitError as e:
            logger.warning("Signal rejected by risk: %s", e)
            return False

        buy_sell = 0 if signal.direction == Direction.BUY else 1

        if self._mode == "paper":
            return self._paper_execute(signal, symbol, qty, buy_sell)
        elif self._mode == "semi_auto":
            return self._semi_auto_execute(signal, symbol, qty, buy_sell)
        elif self._mode == "full_auto":
            return self._full_auto_execute(signal, symbol, qty, buy_sell)
        else:
            logger.error("Unknown trading mode: %s", self._mode)
            return False

    def _paper_execute(self, signal: Signal, symbol: str, qty: int, buy_sell: int) -> bool:
        """Log the signal as a paper trade."""
        side = "BUY" if buy_sell == 0 else "SELL"
        logger.info(
            "[PAPER] %s %s x%d @ %d | reason: %s | strength: %.2f",
            side, symbol, qty, signal.price, signal.reason, signal.strength,
        )
        # Simulate fill for position tracking
        fill = Fill(symbol=symbol, buy_sell=buy_sell, price=signal.price, qty=qty)
        self._tracker.on_fill(fill)
        return True

    def _semi_auto_execute(self, signal: Signal, symbol: str, qty: int, buy_sell: int) -> bool:
        """Print signal and wait for user confirmation."""
        side = "BUY" if buy_sell == 0 else "SELL"
        print(f"\n{'='*60}")
        print(f"SIGNAL: {side} {symbol} x{qty} @ {signal.price}")
        print(f"Reason: {signal.reason}")
        print(f"Strength: {signal.strength:.2f}")
        print(f"{'='*60}")

        try:
            answer = input("Execute? [y/N]: ").strip().lower()
        except EOFError:
            answer = "n"

        if answer != "y":
            logger.info("User rejected signal.")
            return False

        return self._submit_order(symbol, qty, buy_sell, signal)

    def _full_auto_execute(self, signal: Signal, symbol: str, qty: int, buy_sell: int) -> bool:
        """Submit order immediately."""
        return self._submit_order(symbol, qty, buy_sell, signal)

    def _submit_order(self, symbol: str, qty: int, buy_sell: int, signal: Signal) -> bool:
        """Build and submit an order via the gateway."""
        if self._gateway is None:
            logger.error("No order gateway available.")
            return False

        request = OrderRequest(
            symbol=symbol,
            buy_sell=buy_sell,
            qty=qty,
            price=str(signal.price) if self._config.trading.price_flag == 1 else "",
            price_flag=self._config.trading.price_flag,
            trade_type=self._config.trading.trade_type,
            order_type=self._config.trading.order_type,
        )

        managed = ManagedOrder(
            symbol=symbol, buy_sell=buy_sell, qty=qty,
            price=request.price,
        )
        self._order_manager.track_order(managed)

        result = self._gateway.send_future_order(request)
        if not result.success:
            logger.error("Order submission failed: %s", result.message)
            return False

        logger.info("Order submitted successfully: %s", result.message)
        return True
