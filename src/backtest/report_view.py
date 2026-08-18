"""Report tab view model — pure shaping, no Tkinter.

Turns a ``PerformanceMetrics`` (+ trade list) into the card/section
structure the GUI renders: headline metric cards with a tone, two
detail sections of label/value rows, the real-order subset, and the
per-strategy breakdown. ``format_report`` (text) stays untouched for
Discord/console/export — this module only feeds the on-screen tab, and
reuses the same numbers and formatting rules.
"""

from __future__ import annotations

from dataclasses import dataclass

from .metrics import PerformanceMetrics, calculate_metrics


@dataclass
class Metric:
    label: str
    value: str
    sub: str = ""
    tone: str = "neutral"     # "good" | "bad" | "neutral"


def _fmt_pf(pf: float) -> str:
    return "INF" if pf == float("inf") else f"{pf:.2f}"


def _pnl_tone(value: float) -> str:
    return "good" if value > 0 else ("bad" if value < 0 else "neutral")


def headline_cards(m: PerformanceMetrics) -> list:
    """The four numbers worth reading first."""
    pf = m.profit_factor
    if pf == float("inf") or pf >= 1.5:
        pf_tone = "good"
    elif pf < 1.0 and m.total_trades:
        pf_tone = "bad"
    else:
        pf_tone = "neutral"
    return [
        Metric("總損益 Total P&L", f"{m.total_pnl:+,}",
               f"{m.initial_balance:,} → {m.final_balance:,}",
               _pnl_tone(m.total_pnl)),
        Metric("勝率 Win Rate", f"{m.win_rate * 100:.1f}%",
               f"{m.winning_trades}W / {m.losing_trades}L "
               f"({m.total_trades} 筆)"),
        Metric("獲利因子 Profit Factor", _fmt_pf(pf),
               f"+{m.gross_profit:,} / -{abs(m.gross_loss):,}", pf_tone),
        Metric("最大回撤 Max Drawdown", f"{m.max_drawdown:,}",
               f"{m.max_drawdown_pct:.2f}%",
               "bad" if m.max_drawdown else "neutral"),
    ]


def stat_sections(m: PerformanceMetrics) -> list:
    """(title, rows) pairs for the detail grids under the cards."""
    trade_rows = [
        Metric("平均獲利 Avg Win", f"{m.avg_win:,.0f}"),
        Metric("平均虧損 Avg Loss", f"{m.avg_loss:,.0f}"),
        Metric("最大獲利 Largest Win", f"{m.largest_win:,}",
               tone=_pnl_tone(m.largest_win)),
        Metric("最大虧損 Largest Loss", f"{m.largest_loss:,}",
               tone=_pnl_tone(m.largest_loss)),
        Metric("平均持倉 Avg Bars Held", f"{m.avg_bars_held:.1f}"),
    ]
    sharpe_tone = ("good" if m.sharpe_ratio >= 1.0
                   else "bad" if m.sharpe_ratio < 0 else "neutral")
    risk_rows = [
        Metric("總獲利 Gross Profit", f"{m.gross_profit:,}", tone="good"),
        Metric("總虧損 Gross Loss", f"{m.gross_loss:,}",
               tone="bad" if m.gross_loss else "neutral"),
        Metric("夏普比率 Sharpe Ratio", f"{m.sharpe_ratio:.2f}",
               tone=sharpe_tone),
        Metric("最大回撤 Max DD", f"{m.max_drawdown:,}"),
        Metric("回撤比例 Max DD %", f"{m.max_drawdown_pct:.2f}%"),
    ]
    return [("交易統計 Trade Stats", trade_rows),
            ("風險 Risk", risk_rows)]


def real_subset(trades: list | None):
    """(count, cards) for the broker-executed subset, or None when the
    result has no real-order trades. Sharpe deliberately omitted — it's
    noise below ~30 trades (same rule as format_report)."""
    real = [t for t in (trades or []) if getattr(t, "source", "") == "real"]
    if not real:
        return None
    eq, cum = [], 0
    for t in real:
        cum += t.pnl
        eq.append(cum)
    rm = calculate_metrics(real, eq, initial_balance=0)
    cards = [
        Metric("實單損益 Real P&L", f"{rm.total_pnl:+,}",
               tone=_pnl_tone(rm.total_pnl)),
        Metric("實單勝率 Real WR", f"{rm.win_rate * 100:.1f}%",
               f"{rm.winning_trades}W / {rm.losing_trades}L"),
        Metric("實單獲利因子 Real PF", _fmt_pf(rm.profit_factor)),
        Metric("實單最大回撤 Real Max DD", f"{rm.max_drawdown:,}",
               f"{rm.max_drawdown_pct:.2f}%"),
    ]
    return len(real), cards


def per_strategy_rows(trades: list | None):
    """[(strategy, trades, win-rate, pnl)] when trades span more than one
    strategy (regime-switching bots), else None. Row order follows first
    appearance — chronological, matching format_report."""
    by_strategy: dict[str, list] = {}
    for t in trades or []:
        by_strategy.setdefault(getattr(t, "strategy", "") or "", []).append(t)
    if len(by_strategy) < 2:
        return None
    rows = []
    for sname, strades in by_strategy.items():
        eq, cum = [], 0
        for t in strades:
            cum += t.pnl
            eq.append(cum)
        sm = calculate_metrics(strades, eq, initial_balance=0)
        rows.append((sname or "(未標記 unlabeled)", sm.total_trades,
                     f"{sm.win_rate * 100:.1f}%", sm.total_pnl))
    return rows
