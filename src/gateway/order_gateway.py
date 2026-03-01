"""Order gateway: send/cancel/modify futures orders via SK DLL.

Uses SK.SendFutureProxyOrder() with FUTUREPROXYORDER2 struct.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config.settings import AppConfig
from ..utils.errors import OrderError
from .event_bus import Event, EventBus, EventType

logger = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    symbol: str
    buy_sell: int       # 0=buy, 1=sell
    qty: int
    price: str = ""     # empty for market order
    price_flag: int = 0  # 0=market, 1=limit
    trade_type: int = 0  # 0=ROD, 1=IOC, 2=FOK
    order_type: str = "2"  # "2"=auto new/close
    day_trade: int = 0   # 0=no, 1=yes
    settle_ym: str = ""  # empty for front month
    reserved: int = 0    # 0=current session, 1=T+1 pre-order


@dataclass
class OrderResult:
    code: int
    message: str
    success: bool


class OrderGateway:
    """Thin adapter over SK futures order API."""

    def __init__(self, config: AppConfig, event_bus: EventBus, login_id: str):
        self._config = config
        self._event_bus = event_bus
        self._login_id = login_id

    def register_callbacks(self) -> None:
        """Register DLL callbacks for order and fill events."""
        from .connection import _get_sk
        sk = _get_sk()
        sk.OnProxyOrder(self._on_proxy_order)
        sk.OnNewOrderData(self._on_order_data)
        sk.OnNewFulfillData(self._on_fill_data)
        logger.info("Order gateway callbacks registered.")

    def send_future_order(self, request: OrderRequest) -> OrderResult:
        """Submit a futures order."""
        from .connection import _get_sk
        sk = _get_sk()

        full_account = self._config.account.full_account
        if not full_account:
            raise OrderError("No account configured. Check settings.yaml or login result.")

        logger.info(
            "Sending order: %s %s qty=%d price=%s flag=%d type=%d",
            "BUY" if request.buy_sell == 0 else "SELL",
            request.symbol,
            request.qty,
            request.price or "MKT",
            request.price_flag,
            request.trade_type,
        )

        code, message = sk.SendFutureProxyOrder(
            self._login_id,
            full_account,
            request.symbol,
            request.settle_ym,
            request.buy_sell,
            request.price_flag,
            request.day_trade,
            request.order_type,
            request.reserved,
            request.qty,
            request.price,
            request.trade_type,
        )

        success = code == 0
        if not success:
            err_msg = sk.GetMessage(code)
            logger.error("Order failed (code=%d): %s / %s", code, err_msg, message)
        else:
            logger.info("Order submitted: %s", message)

        return OrderResult(code=code, message=message, success=success)

    def alter_future_order(self, full_account: str, order_type: str, price: str,
                           reserved: int, qty: int, trade_type: int,
                           book_no: str, seq_no: str) -> OrderResult:
        """Modify an existing futures order."""
        from .connection import _get_sk
        sk = _get_sk()

        code, message = sk.SendFutureProxyAlter(
            self._login_id,
            full_account,
            order_type,
            price,
            reserved,
            qty,
            trade_type,
            book_no,
            seq_no,
        )
        success = code == 0
        if not success:
            logger.error("Order alter failed (code=%d): %s", code, sk.GetMessage(code))
        return OrderResult(code=code, message=message, success=success)

    def _on_proxy_order(self, stamp_id: int, code: int, message: str) -> None:
        """DLL callback for order submission acknowledgement."""
        logger.info("ProxyOrder response: stamp=%d code=%d msg=%s", stamp_id, code, message)
        self._event_bus.publish(Event(
            type=EventType.ORDER_RESPONSE,
            data={"stamp_id": stamp_id, "code": code, "message": message},
        ))

    def _on_order_data(self, login_id: str, data) -> None:
        """DLL callback for order status updates."""
        logger.info("Order data: %s", data.Raw)
        self._event_bus.publish(Event(
            type=EventType.ORDER_DATA,
            data=data,
        ))

    def _on_fill_data(self, login_id: str, data) -> None:
        """DLL callback for fill/execution data."""
        logger.info("Fill data: %s", data.Raw)
        self._event_bus.publish(Event(
            type=EventType.FILL,
            data=data,
        ))
