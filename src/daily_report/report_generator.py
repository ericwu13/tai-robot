"""Daily report generator: structured JSON reports from trade and bar data.

Generates per-day reports with trade details, daily summary metrics,
strategy metadata, and market regime context. Reports are stored in
``data/daily-reports/YYYY-MM-DD.json``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from src.backtest.broker import Trade, OrderSide
from src.backtest.metrics import PerformanceMetrics, calculate_metrics
from .regime_classifier import classify_regime, RegimeResult

_REPORTS_DIR = Path("data/daily-reports")


def _trade_to_dict(t: Trade, point_value: int = 1) -> dict:
    """Convert a Trade to a JSON-serializable dict with computed fields.

    Handles missing or default-valued fields gracefully so that trades from
    any strategy (hand-written, AI-generated, live, backtest) produce valid
    output even if optional fields were never populated.
    """
    side_val = t.side.value if hasattr(t.side, "value") else str(t.side)
    pnl = getattr(t, "pnl", 0) or 0
    entry_bar = getattr(t, "entry_bar_index", 0) or 0
    exit_bar = getattr(t, "exit_bar_index", 0) or 0
    return {
        "tag": getattr(t, "tag", ""),
        "side": side_val,
        "qty": getattr(t, "qty", 1),
        "entry_price": getattr(t, "entry_price", 0),
        "exit_price": getattr(t, "exit_price", 0),
        "entry_dt": getattr(t, "entry_dt", ""),
        "exit_dt": getattr(t, "exit_dt", ""),
        "pnl": pnl,
        "pnl_currency": pnl * point_value if point_value != 1 else pnl,
        "bars_held": exit_bar - entry_bar,
        "exit_tag": getattr(t, "exit_tag", ""),
        "real_entry_price": getattr(t, "real_entry_price", 0) or None,
        "real_exit_price": getattr(t, "real_exit_price", 0) or None,
    }


def _metrics_to_dict(m: PerformanceMetrics) -> dict:
    return asdict(m)


def _group_trades_by_date(trades: list[Trade]) -> dict[str, list[Trade]]:
    """Group trades by their exit date (YYYY-MM-DD)."""
    grouped: dict[str, list[Trade]] = {}
    for t in trades:
        if not t.exit_dt:
            continue
        # exit_dt format: "YYYY-MM-DD HH:MM" or "YYYY-MM-DD HH:MM:SS"
        date_str = t.exit_dt[:10]
        grouped.setdefault(date_str, []).append(t)
    return grouped


def generate_daily_report(
    date: str,
    trades: list[Trade],
    bars_highs: list[int] | None = None,
    bars_lows: list[int] | None = None,
    bars_closes: list[int] | None = None,
    strategy_name: str = "",
    strategy_version: str = "",
    strategy_params: dict | None = None,
    point_value: int = 1,
    symbol: str = "",
    save: bool = True,
) -> dict:
    """Generate a structured daily report for the given date.

    Parameters
    ----------
    date : "YYYY-MM-DD" string
    trades : trades that closed on this date
    bars_highs/lows/closes : bar data for regime classification (optional)
    strategy_name : human-readable strategy name
    strategy_version : strategy version string
    strategy_params : current parameter dict
    point_value : contract multiplier (e.g. 200 for TX, 50 for MTX)
    symbol : trading symbol
    save : whether to write the report to disk

    Returns
    -------
    dict : the complete report structure
    """
    # Build equity curve from these trades for metrics
    equity_curve = []
    cumulative = 0
    for t in trades:
        cumulative += t.pnl
        equity_curve.append(cumulative)

    metrics = calculate_metrics(trades, equity_curve, initial_balance=0)

    # Market regime (optional — needs bar data)
    regime: RegimeResult | None = None
    if bars_highs and bars_lows and bars_closes:
        regime = classify_regime(bars_highs, bars_lows, bars_closes)

    report = {
        "date": date,
        "symbol": symbol,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": {
            "name": strategy_name,
            "version": strategy_version,
            "params": strategy_params or {},
        },
        "trades": [_trade_to_dict(t, point_value) for t in trades],
        "summary": _metrics_to_dict(metrics),
        "market_regime": regime.to_dict() if regime else None,
    }

    if save:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = _REPORTS_DIR / f"{date}.json"
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    return report


def generate_report_from_backtest(
    trades: list[Trade],
    equity_curve: list[int],
    bars_highs: list[int] | None = None,
    bars_lows: list[int] | None = None,
    bars_closes: list[int] | None = None,
    strategy_name: str = "",
    strategy_version: str = "",
    strategy_params: dict | None = None,
    point_value: int = 1,
    symbol: str = "",
    initial_balance: int = 0,
    save: bool = True,
) -> list[dict]:
    """Generate daily reports from a full backtest result.

    Splits trades by exit date and generates one report per day.
    Returns list of all generated report dicts.
    """
    grouped = _group_trades_by_date(trades)
    reports = []

    for date, day_trades in sorted(grouped.items()):
        report = generate_daily_report(
            date=date,
            trades=day_trades,
            bars_highs=bars_highs,
            bars_lows=bars_lows,
            bars_closes=bars_closes,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            strategy_params=strategy_params,
            point_value=point_value,
            symbol=symbol,
            save=save,
        )
        reports.append(report)

    return reports


def generate_session_report(
    broker,
    data_store,
    strategy_name: str = "",
    strategy_params: dict | None = None,
    point_value: int = 1,
    symbol: str = "",
    date: str = "",
) -> dict | None:
    """Generate a daily report from a live session's broker and data store.

    This is the convenience entry point called by LiveRunner.stop().
    Extracts trades and bar data from the live components and delegates to
    generate_daily_report().

    Returns the report dict, or None if there are no completed trades.
    """
    trades = list(getattr(broker, "trades", []))
    if not trades:
        return None

    if not date:
        # Use the exit date of the last trade, or today
        last_exit = getattr(trades[-1], "exit_dt", "")
        date = last_exit[:10] if last_exit else datetime.now().strftime("%Y-%m-%d")

    # Extract bar data for regime classification (best-effort)
    bars_highs = bars_lows = bars_closes = None
    if data_store is not None:
        try:
            bars_highs = data_store.get_highs()
            bars_lows = data_store.get_lows()
            bars_closes = data_store.get_closes()
        except Exception:
            pass  # regime classification will be skipped

    # Filter to trades that closed on this date
    day_trades = [
        t for t in trades
        if getattr(t, "exit_dt", "")[:10] == date
    ]
    if not day_trades:
        day_trades = trades  # fallback: include all trades

    return generate_daily_report(
        date=date,
        trades=day_trades,
        bars_highs=bars_highs,
        bars_lows=bars_lows,
        bars_closes=bars_closes,
        strategy_name=strategy_name,
        strategy_params=strategy_params,
        point_value=point_value,
        symbol=symbol,
        save=True,
    )


def load_report(date: str) -> dict | None:
    """Load a previously saved daily report. Returns None if not found."""
    path = _REPORTS_DIR / f"{date}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_reports() -> list[str]:
    """List all available report dates, sorted ascending."""
    if not _REPORTS_DIR.exists():
        return []
    return sorted(p.stem for p in _REPORTS_DIR.glob("*.json"))
