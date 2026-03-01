"""Shared test fixtures."""

import sys
from pathlib import Path

import pytest

# Ensure src is importable
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.config.settings import AppConfig, RiskConfig, TradingConfig, StrategyConfig
from src.execution.position_tracker import PositionTracker
from src.gateway.event_bus import EventBus
from src.market_data.data_store import DataStore
from src.risk.manager import RiskManager


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def data_store():
    return DataStore(max_bars=1000)


@pytest.fixture
def position_tracker():
    return PositionTracker()


@pytest.fixture
def default_config():
    return AppConfig(
        trading=TradingConfig(mode="paper", symbol="TXFD0", default_qty=1),
        risk=RiskConfig(max_position=2, max_daily_loss=20000, order_rate_limit=100),
        strategy=StrategyConfig(name="ma_crossover", params={"fast_period": 5, "slow_period": 20}),
    )


@pytest.fixture
def risk_manager(default_config, position_tracker):
    return RiskManager(default_config.risk, position_tracker)
