"""Track open orders and match fills."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class ManagedOrder:
    symbol: str
    buy_sell: int
    qty: int
    price: str
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    order_no: str = ""
    seq_no: str = ""
    book_no: str = ""
    submit_time: datetime | None = None
    fill_time: datetime | None = None
    message: str = ""


class OrderManager:
    """Tracks lifecycle of submitted orders."""

    def __init__(self):
        self._orders: dict[str, ManagedOrder] = {}  # keyed by order_no or seq_no
        self._pending: list[ManagedOrder] = []

    def track_order(self, order: ManagedOrder) -> None:
        order.submit_time = datetime.now()
        self._pending.append(order)
        logger.info("Tracking order: %s %s qty=%d",
                     "BUY" if order.buy_sell == 0 else "SELL",
                     order.symbol, order.qty)

    def on_order_response(self, stamp_id: int, code: int, message: str) -> None:
        """Update order status from proxy order response."""
        if self._pending:
            order = self._pending[0]
            if code == 0:
                order.status = OrderStatus.SUBMITTED
                order.message = message
            else:
                order.status = OrderStatus.REJECTED
                order.message = message
                self._pending.pop(0)

    def on_order_data(self, data) -> None:
        """Update order tracking from order data callback."""
        order_no = getattr(data, "OrderNo", "")
        seq_no = getattr(data, "SeqNo", "")
        key = order_no or seq_no
        if not key:
            return

        if key in self._orders:
            order = self._orders[key]
        elif self._pending:
            order = self._pending.pop(0)
            order.order_no = order_no
            order.seq_no = seq_no
            self._orders[key] = order
        else:
            return

        order_err = getattr(data, "OrderErr", "")
        if order_err and order_err != "00":
            order.status = OrderStatus.REJECTED
            order.message = getattr(data, "ErrorMsg", "")

    def on_fill_data(self, data) -> ManagedOrder | None:
        """Match a fill to a tracked order. Returns the order if matched."""
        order_no = getattr(data, "OrderNo", "")
        seq_no = getattr(data, "SeqNo", "")
        key = order_no or seq_no
        order = self._orders.get(key)
        if order is None:
            return None

        try:
            fill_qty = int(getattr(data, "Qty", "0"))
            fill_price = float(getattr(data, "Price", "0"))
        except (ValueError, TypeError):
            return None

        total_cost = order.avg_fill_price * order.filled_qty + fill_price * fill_qty
        order.filled_qty += fill_qty
        order.avg_fill_price = total_cost / order.filled_qty if order.filled_qty else 0
        order.fill_time = datetime.now()

        if order.filled_qty >= order.qty:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED

        return order

    @property
    def open_orders(self) -> list[ManagedOrder]:
        return [o for o in self._orders.values()
                if o.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)]
