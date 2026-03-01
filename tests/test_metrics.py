"""Tests for PerformanceMetrics calculation."""

import pytest

from src.backtest.broker import Trade, OrderSide
from src.backtest.metrics import calculate_metrics


def make_trade(entry, exit_, bar_in=0, bar_out=1, pv=200):
    pnl = (exit_ - entry) * pv
    return Trade(
        tag="L", side=OrderSide.LONG, qty=1,
        entry_price=entry, exit_price=exit_,
        entry_bar_index=bar_in, exit_bar_index=bar_out,
        pnl=pnl,
    )


class TestBasicMetrics:
    def test_empty_trades(self):
        m = calculate_metrics([], [], initial_balance=1_000_000)
        assert m.total_trades == 0
        assert m.win_rate == 0.0
        assert m.total_pnl == 0
        assert m.final_balance == 1_000_000

    def test_single_winning_trade(self):
        trades = [make_trade(20000, 20100)]  # +100 pts * 200 = +20000
        equity = [20000]
        m = calculate_metrics(trades, equity, initial_balance=1_000_000)

        assert m.total_trades == 1
        assert m.winning_trades == 1
        assert m.losing_trades == 0
        assert m.win_rate == 1.0
        assert m.total_pnl == 20000
        assert m.gross_profit == 20000
        assert m.gross_loss == 0
        assert m.final_balance == 1_020_000

    def test_single_losing_trade(self):
        trades = [make_trade(20000, 19900)]  # -100 pts * 200 = -20000
        equity = [-20000]
        m = calculate_metrics(trades, equity, initial_balance=1_000_000)

        assert m.total_trades == 1
        assert m.winning_trades == 0
        assert m.losing_trades == 1
        assert m.win_rate == 0.0
        assert m.total_pnl == -20000

    def test_mixed_trades(self):
        trades = [
            make_trade(20000, 20100),   # +20000
            make_trade(20100, 20000),   # -20000
            make_trade(20000, 20200),   # +40000
        ]
        equity = [20000, 0, 40000]
        m = calculate_metrics(trades, equity, initial_balance=500_000)

        assert m.total_trades == 3
        assert m.winning_trades == 2
        assert m.losing_trades == 1
        assert m.win_rate == pytest.approx(2 / 3)
        assert m.total_pnl == 40000
        assert m.gross_profit == 60000
        assert m.gross_loss == 20000
        assert m.profit_factor == pytest.approx(3.0)
        assert m.final_balance == 540_000


class TestDrawdown:
    def test_max_drawdown(self):
        trades = [
            make_trade(20000, 20200),   # +40000 -> equity 40000
            make_trade(20200, 20000),   # -40000 -> equity 0
            make_trade(20000, 20050),   # +10000 -> equity 10000
        ]
        equity = [40000, 0, 10000]
        m = calculate_metrics(trades, equity, initial_balance=1_000_000)

        # Peak = 1040000, then drops to 1000000 -> DD = 40000
        assert m.max_drawdown == 40000

    def test_no_drawdown_all_winners(self):
        trades = [
            make_trade(20000, 20100),   # +20000
            make_trade(20100, 20200),   # +20000
        ]
        equity = [20000, 40000]
        m = calculate_metrics(trades, equity, initial_balance=1_000_000)

        assert m.max_drawdown == 0

    def test_drawdown_pct(self):
        trades = [
            make_trade(20000, 20200),  # +40000 -> balance 1040000
            make_trade(20200, 20100),  # -20000 -> balance 1020000
        ]
        equity = [40000, 20000]
        m = calculate_metrics(trades, equity, initial_balance=1_000_000)

        assert m.max_drawdown == 20000
        # DD% = 20000 / 1040000 * 100
        assert m.max_drawdown_pct == pytest.approx(20000 / 1040000 * 100, rel=1e-3)


class TestAvgBarsHeld:
    def test_avg_bars_held(self):
        t1 = make_trade(20000, 20100)
        t1.entry_bar_index = 0
        t1.exit_bar_index = 3  # held 3 bars
        t2 = make_trade(20100, 20200)
        t2.entry_bar_index = 5
        t2.exit_bar_index = 10  # held 5 bars

        m = calculate_metrics([t1, t2], [20000, 40000])
        assert m.avg_bars_held == pytest.approx(4.0)


class TestProfitFactor:
    def test_no_losses_inf(self):
        trades = [make_trade(20000, 20100)]
        m = calculate_metrics(trades, [20000])
        assert m.profit_factor == float("inf")

    def test_no_wins_zero(self):
        trades = [make_trade(20000, 19900)]
        m = calculate_metrics(trades, [-20000])
        assert m.profit_factor == 0.0


class TestLargestWinLoss:
    def test_largest(self):
        trades = [
            make_trade(20000, 20100),   # +20000
            make_trade(20000, 20200),   # +40000
            make_trade(20000, 19900),   # -20000
            make_trade(20000, 19800),   # -40000
        ]
        equity = [20000, 60000, 40000, 0]
        m = calculate_metrics(trades, equity)

        assert m.largest_win == 40000
        assert m.largest_loss == -40000


class TestInitialBalance:
    def test_zero_initial_balance(self):
        trades = [make_trade(20000, 20100)]
        m = calculate_metrics(trades, [20000], initial_balance=0)
        assert m.initial_balance == 0
        assert m.final_balance == 20000

    def test_custom_initial_balance(self):
        m = calculate_metrics([], [], initial_balance=2_000_000)
        assert m.initial_balance == 2_000_000
        assert m.final_balance == 2_000_000
