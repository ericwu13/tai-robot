"""Thread-safe typed pub/sub event bus.

DLL callbacks enqueue events via publish(). The main thread calls run()
which dispatches events to registered subscribers -- no locks needed in
business logic.
"""

from __future__ import annotations

import enum
import logging
import queue
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventType(enum.Enum):
    CONNECTION_STATUS = "CONNECTION_STATUS"
    TICK = "TICK"
    QUOTE_UPDATE = "QUOTE_UPDATE"
    BEST5 = "BEST5"
    ORDER_RESPONSE = "ORDER_RESPONSE"
    ORDER_DATA = "ORDER_DATA"
    FILL = "FILL"
    REPLY_MESSAGE = "REPLY_MESSAGE"
    BAR = "BAR"
    COMPLETE = "COMPLETE"


@dataclass
class Event:
    type: EventType
    data: Any = None
    timestamp: float = 0.0  # filled automatically

    def __post_init__(self):
        if self.timestamp == 0.0:
            import time
            self.timestamp = time.time()


class EventBus:
    """Thread-safe event bus using queue.Queue."""

    def __init__(self):
        self._queue: queue.Queue[Event | None] = queue.Queue()
        self._subscribers: dict[EventType, list[Callable[[Event], None]]] = {}
        self._running = False

    def subscribe(self, event_type: EventType, handler: Callable[[Event], None]) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: EventType, handler: Callable[[Event], None]) -> None:
        handlers = self._subscribers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def publish(self, event: Event) -> None:
        """Thread-safe: can be called from DLL callback threads."""
        self._queue.put(event)

    def stop(self) -> None:
        """Signal the run loop to exit."""
        self._running = False
        self._queue.put(None)  # sentinel to unblock get()

    def run(self, timeout: float = 0.1) -> None:
        """Main-thread dispatch loop. Blocks until stop() is called."""
        self._running = True
        while self._running:
            try:
                event = self._queue.get(timeout=timeout)
            except queue.Empty:
                continue
            if event is None:
                break
            self._dispatch(event)

    def drain(self, max_events: int = 100) -> int:
        """Process up to max_events without blocking. Returns count processed."""
        count = 0
        while count < max_events:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            if event is None:
                self._running = False
                break
            self._dispatch(event)
            count += 1
        return count

    def _dispatch(self, event: Event) -> None:
        handlers = self._subscribers.get(event.type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("Error in event handler for %s", event.type)
