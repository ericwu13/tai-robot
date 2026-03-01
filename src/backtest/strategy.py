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

    @abstractmethod
    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        ...

    @abstractmethod
    def required_bars(self) -> int:
        ...

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
