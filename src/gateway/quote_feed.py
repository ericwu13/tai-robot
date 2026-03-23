"""Quote feed: subscribe to real-time ticks, quotes, and best5 data.

Registers SK DLL callbacks, normalizes raw data into dataclasses,
and publishes events to the EventBus.
"""

from __future__ import annotations

import logging
import sys

from ..market_data.models import Tick, Quote, OrderBook, OrderBookLevel
from ..utils.time_utils import combine_sk_datetime
from .event_bus import Event, EventBus, EventType

logger = logging.getLogger(__name__)


def _get_sk():
    from .connection import _get_sk
    return _get_sk()


class QuoteFeed:
    """Subscribes to market data and publishes normalized events."""

    def __init__(self, event_bus: EventBus, login_id: str, market_no: int = 2):
        self._event_bus = event_bus
        self._login_id = login_id
        self._market_no = market_no
        self._subscribed_symbols: set[str] = set()

    def register_callbacks(self) -> None:
        """Register all DLL event handlers."""
        sk = _get_sk()
        sk.OnNotifyTicksLONG(self._on_ticks)
        sk.OnNotifyQuoteLONG(self._on_quote)
        sk.OnNotifyBest5LONG(self._on_best5)
        sk.OnReplyMessage(self._on_reply)
        logger.info("Quote feed callbacks registered.")

    def subscribe(self, symbol: str) -> None:
        """Subscribe to quote and tick data for a symbol."""
        sk = _get_sk()

        logger.info("Subscribing to quotes for %s...", symbol)
        code = sk.SKQuoteLib_RequestStocks(symbol)
        if code != 0:
            logger.warning("RequestStocks(%s) returned %d: %s", symbol, code, sk.GetMessage(code))

        logger.info("Subscribing to ticks for %s...", symbol)
        code = sk.SKQuoteLib_RequestTicks(0, symbol)
        if code != 0:
            logger.warning("RequestTicks(%s) returned %d: %s", symbol, code, sk.GetMessage(code))

        self._subscribed_symbols.add(symbol)

    def unsubscribe(self, symbol: str) -> None:
        """Cancel quote and tick subscriptions."""
        sk = _get_sk()
        sk.SKQuoteLib_CancelRequestStocks(symbol)
        sk.SKQuoteLib_CancelRequestTicks(symbol)
        self._subscribed_symbols.discard(symbol)
        logger.info("Unsubscribed from %s.", symbol)

    def resubscribe_all(self) -> None:
        """Re-subscribe all symbols after reconnection."""
        for symbol in list(self._subscribed_symbols):
            self.subscribe(symbol)

    def change_symbol(self, new_symbol: str) -> None:
        """Change subscription from current symbol(s) to a new symbol.

        This unsubscribes from all currently subscribed symbols and subscribes
        to the new symbol. Useful when the user changes the active symbol
        and needs to update what gets resubscribed during reconnections.

        Args:
            new_symbol: The new symbol to subscribe to
        """
        # Unsubscribe from all current symbols
        current_symbols = list(self._subscribed_symbols)
        for symbol in current_symbols:
            self.unsubscribe(symbol)

        # Subscribe to the new symbol
        self.subscribe(new_symbol)
        logger.info("Changed symbol subscription from %s to %s", current_symbols, new_symbol)

    def _on_ticks(self, market_no: int, stock_no: str, ptr: int, date: int,
                  time_hms: int, time_millismicros: int, bid: int, ask: int,
                  close: int, qty: int, simulate: int) -> None:
        """DLL callback for tick data."""
        try:
            tick = Tick(
                symbol=stock_no,
                dt=combine_sk_datetime(date, time_hms, time_millismicros),
                price=close,
                qty=qty,
                bid=bid,
                ask=ask,
                simulate=bool(simulate),
            )
            self._event_bus.publish(Event(type=EventType.TICK, data=tick))
        except Exception:
            logger.exception("Error processing tick for %s", stock_no)

    def _on_quote(self, market_no: int, stock_no: str) -> None:
        """DLL callback for quote updates. Fetch full quote data from SK."""
        try:
            sk = _get_sk()
            stock = sk.SKQuoteLib_GetStockByStockNo(market_no, stock_no)
            quote = Quote(
                symbol=stock.strStockNo,
                name=stock.strStockName,
                open=stock.nOpen,
                high=stock.nHigh,
                low=stock.nLow,
                close=stock.nClose,
                volume=stock.nTQty,
                ref_price=stock.nRef,
                bid=stock.nBid,
                ask=stock.nAsk,
                bid_qty=stock.nBc,
                ask_qty=stock.nAc,
                tick_qty=stock.nTickQty,
            )
            self._event_bus.publish(Event(type=EventType.QUOTE_UPDATE, data=quote))
        except Exception:
            logger.exception("Error processing quote for %s", stock_no)

    def _on_best5(self, market_no: int, stock_no: str,
                  best_bids: list, bid_qtys: list,
                  best_asks: list, ask_qtys: list,
                  extend_bid: int, extend_bid_qty: int,
                  extend_ask: int, extend_ask_qty: int,
                  simulate: int) -> None:
        """DLL callback for best 5 bid/ask data."""
        try:
            stock_no_str = stock_no.decode("ansi") if isinstance(stock_no, bytes) else stock_no
            bids = [OrderBookLevel(price=p, qty=q) for p, q in zip(best_bids, bid_qtys)]
            asks = [OrderBookLevel(price=p, qty=q) for p, q in zip(best_asks, ask_qtys)]
            book = OrderBook(symbol=stock_no_str, bids=bids, asks=asks)
            self._event_bus.publish(Event(type=EventType.BEST5, data=book))
        except Exception:
            logger.exception("Error processing best5 for %s", stock_no)

    def _on_reply(self, message1: str, message2: str) -> None:
        """DLL callback for reply messages."""
        logger.info("Reply message: %s | %s", message1, message2)
        self._event_bus.publish(Event(
            type=EventType.REPLY_MESSAGE,
            data={"message1": message1, "message2": message2},
        ))
