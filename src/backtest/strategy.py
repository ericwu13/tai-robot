"""Backtest strategy interface and adapter for existing AbstractStrategy."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..market_data.models import Bar, Direction
from ..market_data.data_store import DataStore
from ..strategy.base import AbstractStrategy
from .broker import BrokerContext, OrderSide


class BacktestStrategy(ABC):
    """Strategy interface for the backtesting engine.

    Unlike AbstractStrategy (which returns Signals), BacktestStrategy
    directly calls broker.entry()/exit() to manage positions -- closer
    to TradingView's Pine Script model.
    """

    # Subclasses set these to declare required timeframe
    # kline_type: 0=minute, 4=daily, 5=weekly, 6=monthly
    # kline_minute: N for N-minute bars (only used when kline_type=0)
    kline_type: int = 0
    kline_minute: int = 240

    # MTF: declare higher-timeframe (HTF) subscriptions in seconds. Empty
    # by default — single-TF strategies pay zero MTF overhead. Each value
    # must be larger than and an exact multiple of the primary interval.
    htf_intervals: list[int] = []

    @abstractmethod
    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        ...

    @abstractmethod
    def required_bars(self) -> int:
        ...

    def htf_required_bars(self) -> dict[int, int]:
        """Minimum completed HTF bars before the strategy receives on_bar().

        Default: 1 per declared HTF interval. Strategies that compute
        indicators on HTF data (e.g. BB(20) on 60m) should override to
        return the period needed for stable values, e.g. {3600: 20}.
        """
        return {iv: 1 for iv in self.htf_intervals}

    @property
    def name(self) -> str:
        return self.__class__.__name__


class SignalStrategyAdapter(BacktestStrategy):
    """Wraps an existing AbstractStrategy so it works in the backtester.

    Converts Signal(BUY) -> broker.entry(LONG) and Signal(SELL) -> broker.exit().
    """

    def __init__(self, strategy: AbstractStrategy):
        self._strategy = strategy

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        signal = self._strategy.on_bar(bar, data_store)
        if signal is None:
            return

        if signal.direction == Direction.BUY and broker.position_size == 0:
            broker.entry("signal_buy", OrderSide.LONG)
        elif signal.direction == Direction.SELL and broker.position_size > 0:
            broker.exit("signal_sell", "signal_buy")

    def required_bars(self) -> int:
        return self._strategy.required_bars()

    @property
    def name(self) -> str:
        return self._strategy.name
