"""Backtesting framework for strategy evaluation."""

from .broker import OrderSide, Order, Trade, BrokerContext, SimulatedBroker
from .strategy import BacktestStrategy, SignalStrategyAdapter
from .engine import BacktestEngine, BacktestResult
from .metrics import PerformanceMetrics, calculate_metrics
from .report import print_report, format_report, export_trades_csv
from .data_loader import parse_kline_strings, load_bars_from_csv
