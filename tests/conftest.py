"""Shared test fixtures."""

import sys
from pathlib import Path

import pytest

# Ensure src is importable
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


@pytest.fixture(autouse=True)
def _isolate_ai_usage_log(tmp_path, tmp_path_factory, monkeypatch):
    """Redirect writers that would otherwise touch the repo ``data/`` tree.

    The AI usage CSV and the host grid slot (``data/evo_grid.slot``)
    both land in a per-test temp directory. Tests that pass their own
    slot path still do, because that argument wins over the env var.
    """
    monkeypatch.setenv("TAI_AI_USAGE_LOG", str(tmp_path / "ai_usage.csv"))
    slot_dir = tmp_path_factory.mktemp("evo_grid_slot")
    monkeypatch.setenv("TAI_EVO_GRID_SLOT", str(slot_dir / "evo_grid.slot"))
    # Reset the one-time stderr notice so each test starts from a clean state.
    from src.ai import chat_client as _cc
    monkeypatch.setattr(_cc, "_usage_log_notified", False)

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
