"""Backtest report: console output and CSV export."""

from __future__ import annotations

import csv
from pathlib import Path

from .broker import Trade
from .metrics import PerformanceMetrics, calculate_metrics


def live_trading_period(
    started_at: str | None,
    trades: list[Trade] | None,
    last_bar_dt: str | None,
) -> tuple[str, str] | None:
    """Return the ``(start, end)`` of a live session's full trading period.

    A resumed session keeps its original ``started_at`` and restored trade
    history, but the process only holds live bars since the latest restart —
    so the period must be derived from all three sources, not the bar list:

    - start: the earlier of ``started_at`` and the first trade's ``entry_dt``
      (old session files may lack ``started_at``)
    - end:   the later of ``last_bar_dt`` and the last trade's ``exit_dt``
      (the bot may have been offline since the last trade)

    All datetimes are "YYYY-MM-DD HH:MM" strings (Trade dt convention), which
    compare correctly as strings. ``started_at`` may be an ISO timestamp with
    a "T" separator and seconds; it is normalized. Returns None when either
    endpoint has no data (e.g. fresh session still warming up).
    """
    starts: list[str] = []
    ends: list[str] = []
    if started_at:
        starts.append(started_at.replace("T", " ")[:16])
    if last_bar_dt:
        ends.append(last_bar_dt)
    for t in trades or []:
        if t.entry_dt:
            starts.append(t.entry_dt)
        if t.exit_dt:
            ends.append(t.exit_dt)
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def format_report(strategy_name: str, metrics: PerformanceMetrics,
                  trades: list[Trade] | None = None,
                  regime: dict | None = None) -> str:
    """Format the metrics block; when ``trades`` is given and any carry a
    real-order tag, append a 實單/模擬 source-split section. The headline
    numbers are the full simulated view (paper = all trades, including
    real-mirrored ones); the split shows the broker-executed subset.
    """
    m = metrics
    pf_str = f"{m.profit_factor:.2f}" if m.profit_factor != float("inf") else "INF"
    header = [
        "=" * 60,
        f" \u56de\u6e2c\u5831\u544a Backtest Report: {strategy_name}",
    ]
    if regime:
        long_name = regime.get("long") or "\u2014"
        short_name = regime.get("short") or "\u2014"
        active_label = regime.get("active_label") or "\u2014"
        active_strat = regime.get("active_strategy") or "\u2014"
        # \u505a\u591a Long / \u505a\u7a7a Short / \u73fe\u884c Active
        header.append(
            f" \u505a\u591a Long: {long_name} | \u505a\u7a7a Short: {short_name}")
        header.append(
            f" \u73fe\u884c Active: {active_label} ({active_strat})")
    header.append("=" * 60)
    lines = header + [
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
    ]
    if trades:
        real = [t for t in trades if getattr(t, "source", "") == "real"]
        if real:
            # Full metrics on the real-order subset \u2014 same engine as the
            # daily report's real_summary. Sharpe deliberately omitted:
            # it's noise below ~30 trades and misleads more than informs.
            real_eq = []
            cum = 0
            for t in real:
                cum += t.pnl
                real_eq.append(cum)
            rm = calculate_metrics(real, real_eq, initial_balance=0)
            rpf = (f"{rm.profit_factor:.2f}"
                   if rm.profit_factor != float("inf") else "INF")
            lines.extend([
                "-" * 60,
                f" \u5be6\u55ae\u7d71\u8a08 Real-Order Subset: {len(real)} trades",
                f" \u5be6\u55ae\u52dd\u7387 Real Win Rate:       {rm.win_rate * 100:>11.1f}%"
                f"  ({rm.winning_trades}W / {rm.losing_trades}L)",
                f" \u5be6\u55ae\u640d\u76ca Real P&L:            {rm.total_pnl:>12,}",
                f" \u5be6\u55ae\u7372\u5229\u56e0\u5b50 Real PF:          {rpf:>12}",
                f" \u5be6\u55ae\u5e73\u5747\u7372\u5229 Real Avg Win:     {rm.avg_win:>12,.0f}",
                f" \u5be6\u55ae\u5e73\u5747\u8667\u640d Real Avg Loss:    {rm.avg_loss:>12,.0f}",
                f" \u5be6\u55ae\u6700\u5927\u7372\u5229 Real Largest Win: {rm.largest_win:>12,}",
                f" \u5be6\u55ae\u6700\u5927\u8667\u640d Real Largest Loss:{rm.largest_loss:>12,}",
                f" \u5be6\u55ae\u6700\u5927\u56de\u64a4 Real Max DD:      {rm.max_drawdown:>12,}"
                f"  ({rm.max_drawdown_pct:.2f}%)",
                " (\u4ee5\u4e0a\u7e3d\u8a08\u70ba\u6a21\u64ec\u5168\u8996\u5716 totals above = simulated view, all trades)",
            ])

        # Per-strategy breakdown \u2014 shown when trades span more than one
        # strategy (e.g. a regime-switching bot that swapped long/short
        # legs). Each leg's metrics are recomputed on its own subset.
        by_strategy: dict[str, list] = {}
        for t in trades:
            sname = getattr(t, "strategy", "") or ""
            by_strategy.setdefault(sname, []).append(t)
        if len(by_strategy) > 1:
            lines.append("-" * 60)
            lines.append(" \u5404\u7b56\u7565\u660e\u7d30 Per-Strategy Breakdown:")
            for sname, strades in by_strategy.items():
                seq, cum = [], 0
                for t in strades:
                    cum += t.pnl
                    seq.append(cum)
                sm = calculate_metrics(strades, seq, initial_balance=0)
                label = sname or "(\u672a\u6a19\u8a18 unlabeled)"
                lines.append(
                    f"   {label}: {sm.total_trades} trades, "
                    f"\u52dd\u7387 WR {sm.win_rate * 100:.1f}%, "
                    f"P&L {sm.total_pnl:+,}")
    lines.append("=" * 60)
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
            "Entry DT", "Exit DT", "Entry Bar", "Exit Bar", "P&L", "Source",
            "Strategy",
        ])
        for t in trades:
            writer.writerow([
                t.tag, t.side.value, t.qty, t.entry_price, t.exit_price,
                t.entry_dt, t.exit_dt, t.entry_bar_index, t.exit_bar_index, t.pnl,
                getattr(t, "source", ""),
                getattr(t, "strategy", ""),
            ])
