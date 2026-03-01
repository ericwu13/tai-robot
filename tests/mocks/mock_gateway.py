"""Simulated SK responses for offline testing."""

from __future__ import annotations

from dataclasses import dataclass
from src.gateway.order_gateway import OrderGateway, OrderRequest, OrderResult


class MockOrderGateway:
    """Simulates order gateway without any DLL dependency."""

    def __init__(self):
        self.sent_orders: list[OrderRequest] = []
        self.next_code: int = 0
        self.next_message: str = "OK"

    def send_future_order(self, request: OrderRequest) -> OrderResult:
        self.sent_orders.append(request)
        return OrderResult(
            code=self.next_code,
            message=self.next_message,
            success=self.next_code == 0,
        )

    def register_callbacks(self) -> None:
        pass


@dataclass
class MockFillData:
    """Simulates OrderFulfillData from SK DLL."""
    OrderNo: str = "F00001"
    SeqNo: str = "001"
    Qty: str = "1"
    Price: str = "20000"
    BuySell: str = "B"
    ComId: str = "TXFD0"
    Type: str = "D"
    OrderErr: str = "00"
    ErrorMsg: str = ""
    Raw: str = ""

    def __post_init__(self):
        if not self.Raw:
            self.Raw = f",,,{self.Type},{self.OrderErr},,{self.BuySell},,{self.ComId},,{self.OrderNo},{self.Price},,,,,,,,{self.Qty}"
