"""Market data dataclasses: Tick, Bar, Quote, OrderBook."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Direction(Enum):
    BUY = "BUY"
    SELL = "SELL"
    FLAT = "FLAT"


@dataclass
class Tick:
    symbol: str
    dt: datetime
    price: int          # raw integer price from SK (divide by 10^decimal for real price)
    qty: int
    bid: int = 0
    ask: int = 0
    simulate: bool = False


@dataclass
class Bar:
    symbol: str
    dt: datetime        # bar open time
    open: int
    high: int
    low: int
    close: int
    volume: int
    interval: int       # seconds


@dataclass
class Quote:
    symbol: str
    name: str = ""
    open: int = 0
    high: int = 0
    low: int = 0
    close: int = 0
    volume: int = 0
    ref_price: int = 0
    bid: int = 0
    ask: int = 0
    bid_qty: int = 0
    ask_qty: int = 0
    tick_qty: int = 0


@dataclass
class OrderBookLevel:
    price: int
    qty: int


@dataclass
class OrderBook:
    symbol: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)


@dataclass
class Signal:
    direction: Direction
    strength: float = 1.0   # 0.0 - 1.0
    price: int = 0
    reason: str = ""
