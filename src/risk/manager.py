"""Pre-trade risk gate. All rules must pass before an order is sent."""

from __future__ import annotations

import logging

from ..config.settings import RiskConfig
from ..execution.position_tracker import PositionTracker
from ..market_data.models import Signal
from ..utils.errors import RiskLimitError
from .rules import (
    MaxDailyLossRule,
    MaxDrawdownRule,
    MaxPositionRule,
    OrderRateLimitRule,
    RiskRule,
)

logger = logging.getLogger(__name__)


class RiskManager:
    """Runs all risk rules before allowing an order through."""

    def __init__(self, config: RiskConfig, tracker: PositionTracker):
        self._tracker = tracker
        self._rules: list[RiskRule] = [
            MaxPositionRule(config.max_position),
            MaxDailyLossRule(config.max_daily_loss),
            OrderRateLimitRule(config.order_rate_limit),
            MaxDrawdownRule(config.max_drawdown_pct),
        ]

    def check(self, signal: Signal, symbol: str, qty: int) -> None:
        """Run all risk checks. Raises RiskLimitError if any fail."""
        for rule in self._rules:
            error = rule.check(signal, symbol, qty, self._tracker)
            if error:
                logger.warning("Risk check REJECTED by %s: %s",
                               rule.__class__.__name__, error)
                raise RiskLimitError(rule.__class__.__name__, error)
        logger.debug("Risk checks passed for %s %s qty=%d",
                     signal.direction.value, symbol, qty)
