"""Performance metrics calculation for backtest results."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .broker import Trade


@dataclass
class PerformanceMetrics:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: int = 0
    gross_profit: int = 0
    gross_loss: int = 0
    profit_factor: float = 0.0
    max_drawdown: int = 0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_bars_held: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: int = 0
    largest_loss: int = 0
    initial_balance: int = 0
    final_balance: int = 0


def calculate_metrics(
    trades: list[Trade],
    equity_curve: list[int],
    initial_balance: int = 0,
) -> PerformanceMetrics:
    m = PerformanceMetrics()
    m.initial_balance = initial_balance
    m.total_trades = len(trades)

    if not trades:
        m.final_balance = initial_balance
        return m

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]

    m.winning_trades = len(wins)
    m.losing_trades = len(losses)
    m.win_rate = m.winning_trades / m.total_trades if m.total_trades else 0.0

    m.total_pnl = sum(t.pnl for t in trades)
    m.gross_profit = sum(t.pnl for t in wins)
    m.gross_loss = abs(sum(t.pnl for t in losses))
    m.profit_factor = m.gross_profit / m.gross_loss if m.gross_loss > 0 else float("inf")

    m.avg_win = m.gross_profit / len(wins) if wins else 0.0
    m.avg_loss = m.gross_loss / len(losses) if losses else 0.0
    m.largest_win = max((t.pnl for t in wins), default=0)
    m.largest_loss = min((t.pnl for t in losses), default=0)

    total_bars = sum(t.exit_bar_index - t.entry_bar_index for t in trades)
    m.avg_bars_held = total_bars / m.total_trades if m.total_trades else 0.0

    m.final_balance = initial_balance + m.total_pnl

    # Max drawdown from equity curve
    if equity_curve:
        peak = initial_balance
        max_dd = 0
        for eq in equity_curve:
            equity = initial_balance + eq
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
        m.max_drawdown = max_dd
        m.max_drawdown_pct = (max_dd / peak * 100) if peak > 0 else 0.0

    # Sharpe ratio (annualized, using per-trade returns)
    if len(trades) >= 2:
        returns = [t.pnl for t in trades]
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        std_ret = math.sqrt(variance) if variance > 0 else 0
        if std_ret > 0:
            m.sharpe_ratio = (mean_ret / std_ret) * math.sqrt(len(trades))

    return m
