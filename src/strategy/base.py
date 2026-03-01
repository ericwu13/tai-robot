"""Abstract strategy interface and Signal dataclass."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..market_data.models import Bar, Signal, Tick
from ..market_data.data_store import DataStore


class AbstractStrategy(ABC):
    """Plugin interface for trading strategies.

    Strategies receive completed bars (and optionally ticks) and return
    trading Signals or None.
    """

    def __init__(self, params: dict | None = None):
        self.params = params or {}

    @abstractmethod
    def on_bar(self, bar: Bar, data_store: DataStore) -> Signal | None:
        """Called when a new bar completes. Return a Signal or None."""
        ...

    @abstractmethod
    def required_bars(self) -> int:
        """Minimum number of bars needed before the strategy can emit signals."""
        ...

    def on_tick(self, tick: Tick) -> Signal | None:
        """Optional: called on every tick. Default returns None."""
        return None

    @property
    def name(self) -> str:
        return self.__class__.__name__
