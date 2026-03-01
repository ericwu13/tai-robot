"""Tests for execution engine: signal -> order flow."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

from src.config.settings import AppConfig, TradingConfig, RiskConfig
from src.execution.engine import ExecutionEngine
from src.execution.order_manager import OrderManager
from src.execution.position_tracker import PositionTracker
from src.market_data.models import Direction, Signal
from src.risk.manager import RiskManager
from tests.mocks.mock_gateway import MockOrderGateway


def _make_engine(mode: str = "paper", gateway=None) -> tuple[ExecutionEngine, PositionTracker, MockOrderGateway]:
    config = AppConfig(
        trading=TradingConfig(mode=mode, symbol="TXFD0", default_qty=1),
        risk=RiskConfig(max_position=5, max_daily_loss=50000, order_rate_limit=100),
    )
    tracker = PositionTracker()
    risk = RiskManager(config.risk, tracker)
    order_mgr = OrderManager()
    mock_gw = gateway or MockOrderGateway()

    engine = ExecutionEngine(
        config=config,
        order_gateway=mock_gw if mode != "paper" else None,
        risk_manager=risk,
        position_tracker=tracker,
        order_manager=order_mgr,
    )
    return engine, tracker, mock_gw


class TestPaperMode:
    def test_buy_signal(self):
        engine, tracker, _ = _make_engine("paper")
        signal = Signal(Direction.BUY, strength=0.8, price=20000, reason="test buy")
        result = engine.on_signal(signal)
        assert result is True
        pos = tracker.get_position("TXFD0")
        assert pos.qty == 1

    def test_sell_signal(self):
        engine, tracker, _ = _make_engine("paper")
        # First buy
        engine.on_signal(Signal(Direction.BUY, price=20000, reason="buy"))
        # Then sell
        result = engine.on_signal(Signal(Direction.SELL, price=20050, reason="sell"))
        assert result is True
        pos = tracker.get_position("TXFD0")
        assert pos.qty == 0
        assert pos.realized_pnl == 50  # 20050 - 20000

    def test_flat_signal_ignored(self):
        engine, tracker, _ = _make_engine("paper")
        result = engine.on_signal(Signal(Direction.FLAT, reason="flat"))
        assert result is False


class TestFullAutoMode:
    def test_order_submitted(self):
        mock_gw = MockOrderGateway()
        engine, tracker, _ = _make_engine("full_auto", mock_gw)
        signal = Signal(Direction.BUY, strength=0.9, price=20000, reason="auto buy")
        result = engine.on_signal(signal)
        assert result is True
        assert len(mock_gw.sent_orders) == 1
        assert mock_gw.sent_orders[0].buy_sell == 0

    def test_failed_order(self):
        mock_gw = MockOrderGateway()
        mock_gw.next_code = -1
        mock_gw.next_message = "Insufficient margin"
        engine, tracker, _ = _make_engine("full_auto", mock_gw)
        signal = Signal(Direction.SELL, price=20000, reason="sell")
        result = engine.on_signal(signal)
        assert result is False


class TestRiskIntegration:
    def test_risk_rejection(self):
        config = AppConfig(
            trading=TradingConfig(mode="paper", symbol="TXFD0", default_qty=1),
            risk=RiskConfig(max_position=1, max_daily_loss=50000, order_rate_limit=100),
        )
        tracker = PositionTracker()
        risk = RiskManager(config.risk, tracker)
        order_mgr = OrderManager()

        engine = ExecutionEngine(config, None, risk, tracker, order_mgr)

        # Fill position to max
        engine.on_signal(Signal(Direction.BUY, price=20000, reason="first"))
        # Second buy should be rejected
        result = engine.on_signal(Signal(Direction.BUY, price=20050, reason="second"))
        assert result is False
        assert tracker.get_position("TXFD0").qty == 1
