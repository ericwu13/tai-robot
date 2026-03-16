"""Backtest report: console output and CSV export."""

from __future__ import annotations

import csv
from pathlib import Path

from .broker import Trade
from .metrics import PerformanceMetrics


def format_report(strategy_name: str, metrics: PerformanceMetrics) -> str:
    m = metrics
    pf_str = f"{m.profit_factor:.2f}" if m.profit_factor != float("inf") else "INF"
    lines = [
        "=" * 60,
        f" \u56de\u6e2c\u5831\u544a Backtest Report: {strategy_name}",
        "=" * 60,
        f" \u521d\u59cb\u8cc7\u91d1 Initial Balance:     {m.initial_balance:>12,}",
        f" \u6700\u7d42\u8cc7\u91d1 Final Balance:       {m.final_balance:>12,}",
        "-" * 60,
        f" \u7e3d\u4ea4\u6613\u6578 Total Trades:        {m.total_trades:>12}",
        f" \u7372\u5229\u6b21\u6578 Winning Trades:      {m.winning_trades:>12}",
        f" \u8667\u640d\u6b21\u6578 Losing Trades:       {m.losing_trades:>12}",
        f" \u52dd\u7387   Win Rate:            {m.win_rate * 100:>11.1f}%",
        "-" * 60,
        f" \u7e3d\u640d\u76ca Total P&L:            {m.total_pnl:>12,}",
        f" \u7e3d\u7372\u5229 Gross Profit:         {m.gross_profit:>12,}",
        f" \u7e3d\u8667\u640d Gross Loss:           {m.gross_loss:>12,}",
        f" \u7372\u5229\u56e0\u5b50 Profit Factor:       {pf_str:>12}",
        "-" * 60,
        f" \u5e73\u5747\u7372\u5229 Avg Win:             {m.avg_win:>12,.0f}",
        f" \u5e73\u5747\u8667\u640d Avg Loss:            {m.avg_loss:>12,.0f}",
        f" \u6700\u5927\u7372\u5229 Largest Win:         {m.largest_win:>12,}",
        f" \u6700\u5927\u8667\u640d Largest Loss:        {m.largest_loss:>12,}",
        "-" * 60,
        f" \u6700\u5927\u56de\u64a4 Max Drawdown:        {m.max_drawdown:>12,}",
        f" \u6700\u5927\u56de\u64a4% Max Drawdown %:     {m.max_drawdown_pct:>11.2f}%",
        f" \u590f\u666e\u6bd4\u7387 Sharpe Ratio:        {m.sharpe_ratio:>12.2f}",
        f" \u5e73\u5747\u6301\u5009 Avg Bars Held:       {m.avg_bars_held:>12.1f}",
        "=" * 60,
    ]
    return "\n".join(lines)


def print_report(strategy_name: str, metrics: PerformanceMetrics) -> None:
    print(format_report(strategy_name, metrics))


def export_trades_csv(trades: list[Trade], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Tag", "Side", "Qty", "Entry Price", "Exit Price",
            "Entry DT", "Exit DT", "Entry Bar", "Exit Bar", "P&L",
        ])
        for t in trades:
            writer.writerow([
                t.tag, t.side.value, t.qty, t.entry_price, t.exit_price,
                t.entry_dt, t.exit_dt, t.entry_bar_index, t.exit_bar_index, t.pnl,
            ])
