"""Strategy discovery and instantiation."""

from __future__ import annotations

from ..utils.errors import StrategyError
from .base import AbstractStrategy

# Registry of available strategies (name -> class)
_STRATEGIES: dict[str, type[AbstractStrategy]] = {}


def register_strategy(name: str, cls: type[AbstractStrategy]) -> None:
    _STRATEGIES[name] = cls


def get_strategy(name: str, params: dict | None = None) -> AbstractStrategy:
    """Look up a strategy by name and instantiate it."""
    cls = _STRATEGIES.get(name)
    if cls is None:
        available = ", ".join(_STRATEGIES.keys()) or "(none)"
        raise StrategyError(f"Unknown strategy '{name}'. Available: {available}")
    return cls(params)


def list_strategies() -> list[str]:
    return list(_STRATEGIES.keys())


def _auto_register() -> None:
    """Import example strategies to trigger registration."""
    from .examples import ma_crossover, rsi_reversal, bollinger_breakout  # noqa: F401


_auto_register()
