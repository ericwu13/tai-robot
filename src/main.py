"""Entry point: wire everything together and run the event loop."""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

from .config.settings import AppConfig, load_config
from .execution.engine import ExecutionEngine
from .execution.order_manager import OrderManager
from .execution.position_tracker import Fill, PositionTracker
from .gateway.event_bus import Event, EventBus, EventType
from .gateway.connection import ConnectionManager
from .gateway.order_gateway import OrderGateway
from .gateway.quote_feed import QuoteFeed
from .logging_.trade_logger import TradeLogger, setup_logging
from .market_data.bar_builder import BarBuilder
from .market_data.data_store import DataStore
from .market_data.models import Tick
from .risk.manager import RiskManager
from .strategy.base import AbstractStrategy
from .strategy.registry import get_strategy

logger = logging.getLogger(__name__)


class TaiRobot:
    """Main application orchestrator."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.event_bus = EventBus()
        self.data_store = DataStore()
        self.position_tracker = PositionTracker()
        self.order_manager = OrderManager()
        self.risk_manager = RiskManager(config.risk, self.position_tracker)
        self.trade_logger = TradeLogger(config.logging.trade_csv)

        self.connection: ConnectionManager | None = None
        self.quote_feed: QuoteFeed | None = None
        self.order_gateway: OrderGateway | None = None
        self.bar_builder: BarBuilder | None = None
        self.strategy: AbstractStrategy | None = None
        self.engine: ExecutionEngine | None = None

    def setup(self) -> None:
        """Initialize all components and connect to the API."""
        symbol = self.config.trading.symbol
        interval = self.config.strategy.bar_interval

        # Bar builder
        self.bar_builder = BarBuilder(symbol, interval, self.event_bus)

        # Strategy
        self.strategy = get_strategy(
            self.config.strategy.name,
            self.config.strategy.params,
        )
        logger.info("Strategy loaded: %s (requires %d bars)",
                     self.strategy.name, self.strategy.required_bars())

        # Connect to Capital API
        self.connection = ConnectionManager(self.config, self.event_bus)
        self.connection.login()
        self.connection.connect_services()

        login_id = self.connection.login_id

        # Quote feed
        self.quote_feed = QuoteFeed(
            self.event_bus, login_id, self.config.trading.market_no,
        )
        self.quote_feed.register_callbacks()
        self.quote_feed.subscribe(symbol)

        # Order gateway
        self.order_gateway = OrderGateway(self.config, self.event_bus, login_id)
        self.order_gateway.register_callbacks()

        # Execution engine
        self.engine = ExecutionEngine(
            self.config,
            self.order_gateway if self.config.trading.mode != "paper" else None,
            self.risk_manager,
            self.position_tracker,
            self.order_manager,
        )

        # Subscribe to events
        self.event_bus.subscribe(EventType.TICK, self._on_tick)
        self.event_bus.subscribe(EventType.BAR, self._on_bar)
        self.event_bus.subscribe(EventType.FILL, self._on_fill)
        self.event_bus.subscribe(EventType.ORDER_RESPONSE, self._on_order_response)
        self.event_bus.subscribe(EventType.ORDER_DATA, self._on_order_data)
        self.event_bus.subscribe(EventType.CONNECTION_STATUS, self._on_connection)

        logger.info("Setup complete. Mode: %s", self.config.trading.mode)

    def run(self) -> None:
        """Start the main event loop."""
        logger.info("Starting event loop...")
        try:
            self.event_bus.run()
        except KeyboardInterrupt:
            logger.info("Interrupted by user.")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Clean shutdown of all components."""
        logger.info("Shutting down...")
        self.event_bus.stop()
        if self.bar_builder:
            self.bar_builder.flush()
        if self.connection:
            self.connection.disconnect()
        self.data_store.close()
        logger.info("Shutdown complete.")

    def _on_tick(self, event: Event) -> None:
        tick: Tick = event.data
        completed_bar = self.bar_builder.on_tick(tick)
        # Also update unrealized P&L
        self.position_tracker.update_unrealized(tick.symbol, tick.price)

    def _on_bar(self, event: Event) -> None:
        bar = event.data
        self.data_store.add_bar(bar)

        if len(self.data_store) < self.strategy.required_bars():
            logger.debug("Warming up: %d/%d bars",
                         len(self.data_store), self.strategy.required_bars())
            return

        signal = self.strategy.on_bar(bar, self.data_store)
        if signal:
            logger.info("Signal: %s strength=%.2f reason=%s",
                         signal.direction.value, signal.strength, signal.reason)
            executed = self.engine.on_signal(signal)
            if executed:
                pos = self.position_tracker.get_position(bar.symbol)
                self.trade_logger.log_trade(
                    symbol=bar.symbol,
                    side=signal.direction.value,
                    qty=self.config.trading.default_qty,
                    price=signal.price,
                    position_after=pos.qty,
                    realized_pnl=pos.realized_pnl,
                    reason=signal.reason,
                    mode=self.config.trading.mode,
                )

    def _on_fill(self, event: Event) -> None:
        data = event.data
        # Match fill to tracked order
        matched = self.order_manager.on_fill_data(data)
        if matched:
            try:
                fill = Fill(
                    symbol=matched.symbol,
                    buy_sell=matched.buy_sell,
                    price=matched.avg_fill_price,
                    qty=int(getattr(data, "Qty", "0")),
                )
                self.position_tracker.on_fill(fill)
            except (ValueError, TypeError):
                logger.exception("Error processing fill data")

    def _on_order_response(self, event: Event) -> None:
        d = event.data
        self.order_manager.on_order_response(d["stamp_id"], d["code"], d["message"])

    def _on_order_data(self, event: Event) -> None:
        self.order_manager.on_order_data(event.data)

    def change_symbol(self, new_symbol: str) -> None:
        """Change the trading symbol and update quote subscriptions.

        This updates the config and switches quote feed subscriptions to the new symbol.
        Used when the user selects a different symbol in the GUI.

        Args:
            new_symbol: The new symbol to trade and subscribe to (e.g., "TMF", "TXFD0")
        """
        old_symbol = self.config.trading.symbol
        self.config.trading.symbol = new_symbol

        if self.quote_feed:
            self.quote_feed.change_symbol(new_symbol)

        logger.info("Changed trading symbol from %s to %s", old_symbol, new_symbol)

    def _on_connection(self, event: Event) -> None:
        d = event.data
        if d["code"] in (1, 2):  # disconnected / lost
            logger.warning("Connection lost. Attempting reconnect...")
            if self.connection and self.connection.reconnect():
                logger.info("Reconnected. Re-subscribing quotes...")
                if self.quote_feed:
                    # Ensure we're subscribed to the current symbol, not stale ones
                    current_symbol = self.config.trading.symbol
                    self.quote_feed.change_symbol(current_symbol)


def main() -> None:
    # Find config file
    config_path = Path("settings.yaml")
    if not config_path.exists():
        # Try project root
        project_root = Path(__file__).resolve().parent.parent
        config_path = project_root / "settings.yaml"

    config = load_config(config_path)
    setup_logging(config.logging)

    robot = TaiRobot(config)

    # Graceful shutdown on SIGINT/SIGTERM
    def _signal_handler(sig, frame):
        logger.info("Received signal %s, shutting down...", sig)
        robot.event_bus.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except (OSError, AttributeError):
        pass  # SIGTERM not available on Windows in all contexts

    robot.setup()
    robot.run()


if __name__ == "__main__":
    main()
