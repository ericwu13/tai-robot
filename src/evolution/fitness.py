"""Multi-metric composite fitness scoring for strategy backtest results.

Two entry points consume the data structures that already exist in this
codebase — no new shapes are invented:

* :func:`compute_fitness` consumes the ``BacktestResult`` shape produced
  by :class:`src.backtest.engine.BacktestEngine` (``result.trades``,
  ``result.equity_curve``, ``result.metrics``). Regime scoring uses a
  per-trade price-direction proxy because ``BacktestResult`` has no
  market-context attached.

* :func:`compute_fitness_from_reports` is the canonical SEE entry point.
  It consumes the daily-report dicts produced by
  :func:`src.daily_report.report_generator.generate_report_from_backtest`
  (the same JSONs persisted at ``data/daily-reports/YYYY-MM-DD.json``).
  Regime scoring uses the real ADX/ATR/EMA labels emitted by
  :func:`src.daily_report.regime_classifier.classify_regime`, mapped down
  to bull / bear / sideways buckets.

The composite is a weighted, normalized combination of risk-adjusted
return, drawdown, profit factor, win rate, monthly consistency, and
regime balance. Strategies with fewer than ``MIN_TRADES`` trades are
gated to a composite of 0 — small samples score well by luck and should
not propagate through the gene pool.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

# Minimum trades for the composite to be non-zero. Below this, sample
# size is too small to distinguish signal from luck.
MIN_TRADES = 30

# Default weights — must sum to 1.0. Tuned to favor risk-adjusted return
# while penalizing drawdown and rewarding regime robustness.
DEFAULT_WEIGHTS: dict[str, float] = {
    "sharpe": 0.20,
    "sortino": 0.15,
    "drawdown": 0.20,
    "profit_factor": 0.15,
    "win_rate": 0.10,
    "consistency": 0.10,
    "regime_balance": 0.10,
}

# Normalization caps — values beyond these saturate at 1.0.
_SHARPE_CAP = 3.0
_SORTINO_CAP = 3.0
_PROFIT_FACTOR_CAP = 3.0   # PF=3 is excellent; Inf maps to 1.0
_DRAWDOWN_CAP_PCT = 30.0   # 30% DD or worse → 0.0

# Regime classification — each trade is bucketed by the price move during
# its lifetime (exit_price vs entry_price). The threshold separates "trend"
# from "sideways"; 1% is small enough that intraday noise doesn't dominate
# but large enough that a directional bar move counts as a regime signal.
_REGIME_TREND_PCT = 0.01


def _clip01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _parse_trade_dt(s: str) -> datetime | None:
    """Trade.entry_dt / exit_dt are written by the engine as either
    ``%Y-%m-%d %H:%M:%S`` (normal bars) or ``%Y-%m-%d %H:%M`` (force_close
    fallback). Try both before giving up."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def sortino_ratio(trade_pnls: list[float]) -> float:
    """Sortino = mean / downside_std, scaled by sqrt(N) to match the
    annualization style used by ``calculate_metrics`` for Sharpe.

    Downside std uses only negative returns. If there are no negative
    returns the strategy has no downside risk and we return a large
    positive number capped by the caller's normalization."""
    if len(trade_pnls) < 2:
        return 0.0
    mean = sum(trade_pnls) / len(trade_pnls)
    downside = [r for r in trade_pnls if r < 0]
    if not downside:
        # No losses — return a strong but finite signal so downstream
        # normalization treats it as max-good rather than infinity.
        return _SORTINO_CAP * 2 if mean > 0 else 0.0
    # Population std of downside returns vs mean of ALL returns. This is
    # the standard Sortino convention (penalize drawdown, not symmetry).
    variance = sum((r - mean) ** 2 for r in downside) / len(downside)
    downside_std = math.sqrt(variance) if variance > 0 else 0.0
    if downside_std == 0:
        return 0.0
    return (mean / downside_std) * math.sqrt(len(trade_pnls))


def consistency_score(trade_pnls_by_month: dict[str, float]) -> float:
    """Score in [0, 1] rewarding low std-dev relative to mean of monthly
    PnL. A strategy that makes the same money every month scores 1.0;
    one with wild swings around a small positive mean scores low.

    Returns 0.0 when there's <2 months of data (no variance to measure).
    """
    monthly = list(trade_pnls_by_month.values())
    if len(monthly) < 2:
        return 0.0
    mean = sum(monthly) / len(monthly)
    if mean <= 0:
        # Net-losing months on average — no consistency credit.
        return 0.0
    variance = sum((m - mean) ** 2 for m in monthly) / (len(monthly) - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return 1.0
    # Coefficient of variation: lower is better. CV=1 (std=mean) → ~0.5,
    # CV=0 → 1.0, CV>>1 → near 0.
    cv = std / mean
    return _clip01(1.0 / (1.0 + cv))


def _group_pnl_by_month(trades: Iterable[Any]) -> dict[str, float]:
    """Bucket trade PnL by entry-month (YYYY-MM) string."""
    out: dict[str, float] = {}
    for t in trades:
        dt = _parse_trade_dt(getattr(t, "entry_dt", "") or "")
        if dt is None:
            continue
        key = dt.strftime("%Y-%m")
        out[key] = out.get(key, 0.0) + float(t.pnl)
    return out


def _classify_trade_regime(trade: Any) -> str:
    """Classify a single trade by the price direction during its lifetime.

    This is a per-trade proxy for "what was the market doing while I was
    holding this position?" — measured directly from entry_price/exit_price
    on the trade itself rather than re-deriving from the bar feed (which
    fitness doesn't have access to). Coarse but practical and stable.
    """
    entry = getattr(trade, "entry_price", 0)
    exit_ = getattr(trade, "exit_price", 0)
    if not entry or not exit_:
        return "sideways"
    pct = (exit_ - entry) / entry
    if pct > _REGIME_TREND_PCT:
        return "bull"
    if pct < -_REGIME_TREND_PCT:
        return "bear"
    return "sideways"


def regime_scores(trades: Iterable[Any]) -> dict[str, float]:
    """Return per-regime win rates in [0, 1] for bull / bear / sideways.

    A regime with no trades scores 0.0 — the strategy hasn't earned credit
    for handling that regime. This is intentional: we want robustness
    across regimes, not specialization in one.
    """
    buckets: dict[str, list[Any]] = {"bull": [], "bear": [], "sideways": []}
    for t in trades:
        buckets[_classify_trade_regime(t)].append(t)

    out: dict[str, float] = {}
    for regime, ts in buckets.items():
        if not ts:
            out[regime] = 0.0
            continue
        wins = sum(1 for t in ts if getattr(t, "pnl", 0) > 0)
        out[regime] = wins / len(ts)
    return out


def _normalize(metrics: dict[str, float]) -> dict[str, float]:
    """Normalize raw metrics to [0, 1] for weighted combination."""
    sharpe = metrics["sharpe"]
    sortino = metrics["sortino"]
    pf = metrics["profit_factor"]
    dd = metrics["max_drawdown_pct"]
    wr = metrics["win_rate"]
    consistency = metrics["consistency"]
    regimes = (metrics["regime_bull"], metrics["regime_bear"],
               metrics["regime_sideways"])

    pf_capped = pf if math.isfinite(pf) else _PROFIT_FACTOR_CAP
    return {
        "sharpe": _clip01(sharpe / _SHARPE_CAP),
        "sortino": _clip01(sortino / _SORTINO_CAP),
        # PF=1 is breakeven → 0; PF=cap → 1.
        "profit_factor": _clip01((pf_capped - 1.0) / (_PROFIT_FACTOR_CAP - 1.0)),
        # 0% DD → 1.0; cap% DD or worse → 0.0.
        "drawdown": _clip01(1.0 - dd / _DRAWDOWN_CAP_PCT),
        "win_rate": _clip01(wr),
        "consistency": _clip01(consistency),
        # Reward the WORST regime — a strategy strong in bull only is
        # penalized; one that's mediocre across all three is rewarded.
        "regime_balance": _clip01(min(regimes)),
    }


@dataclass
class FitnessResult:
    """Bundle of raw metrics + composite score returned to callers."""
    composite: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    profit_factor: float
    win_rate: float
    consistency: float
    regime_bull: float
    regime_bear: float
    regime_sideways: float
    total_trades: int
    gated: bool   # True when total_trades < MIN_TRADES (composite forced to 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "composite": self.composite,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "max_drawdown_pct": self.max_drawdown_pct,
            "profit_factor": self.profit_factor,
            "win_rate": self.win_rate,
            "consistency": self.consistency,
            "regime_bull": self.regime_bull,
            "regime_bear": self.regime_bear,
            "regime_sideways": self.regime_sideways,
            "total_trades": self.total_trades,
            "gated": self.gated,
        }


def _extract_metric_floats(metrics: Any) -> dict[str, float]:
    """Pull the four scalar metrics fitness needs out of a
    ``PerformanceMetrics`` instance OR a plain dict (the JSON shape used
    by daily-report ``summary`` blocks)."""
    if metrics is None:
        return {"sharpe": 0.0, "profit_factor": 0.0,
                "win_rate": 0.0, "max_drawdown_pct": 0.0}
    if isinstance(metrics, dict):
        get = metrics.get
    else:
        get = lambda k, d=0.0: getattr(metrics, k, d)
    return {
        "sharpe": float(get("sharpe_ratio", 0.0) or 0.0),
        "profit_factor": float(get("profit_factor", 0.0) or 0.0),
        "win_rate": float(get("win_rate", 0.0) or 0.0),
        "max_drawdown_pct": float(get("max_drawdown_pct", 0.0) or 0.0),
    }


def _compose(
    trades: list[Any],
    metrics: Any,
    regimes: dict[str, float],
    weights: dict[str, float],
) -> FitnessResult:
    """Shared assembly path. ``regimes`` is the pre-computed
    bull/bear/sideways win-rate dict; how it was derived (naive proxy or
    real ADX/ATR labels) is the caller's choice."""
    total_trades = len(trades)
    scalars = _extract_metric_floats(metrics)

    pnls = [float(getattr(t, "pnl", 0) or 0) if not isinstance(t, dict)
            else float(t.get("pnl", 0) or 0) for t in trades]
    sortino = sortino_ratio(pnls)

    monthly = _group_pnl_by_month(trades)
    consistency = consistency_score(monthly)

    raw = {
        "sharpe": scalars["sharpe"],
        "sortino": sortino,
        "profit_factor": scalars["profit_factor"],
        "max_drawdown_pct": scalars["max_drawdown_pct"],
        "win_rate": scalars["win_rate"],
        "consistency": consistency,
        "regime_bull": regimes["bull"],
        "regime_bear": regimes["bear"],
        "regime_sideways": regimes["sideways"],
    }
    normalized = _normalize(raw)

    composite = _clip01(sum(weights[k] * normalized[k] for k in weights))

    gated = total_trades < MIN_TRADES
    if gated:
        composite = 0.0

    return FitnessResult(
        composite=composite,
        sharpe=scalars["sharpe"],
        sortino=sortino,
        max_drawdown_pct=scalars["max_drawdown_pct"],
        profit_factor=scalars["profit_factor"],
        win_rate=scalars["win_rate"],
        consistency=consistency,
        regime_bull=regimes["bull"],
        regime_bear=regimes["bear"],
        regime_sideways=regimes["sideways"],
        total_trades=total_trades,
        gated=gated,
    )


# Map the 7 labels emitted by ``classify_regime`` down to the spec's three
# buckets. ``high-volatility`` and ``low-volatility-chop`` are bucketed as
# sideways because direction is undefined in those regimes — a strategy
# that performs in chop without a directional bias is what we want to
# reward as "sideways-capable".
_LABEL_TO_REGIME: dict[str, str] = {
    "trending-up": "bull",
    "transitional-bullish": "bull",
    "trending-down": "bear",
    "transitional-bearish": "bear",
    "range-bound": "sideways",
    "low-volatility-chop": "sideways",
    "high-volatility": "sideways",
}


def label_to_regime(label: str | None) -> str | None:
    """Public mapping: ``classify_regime`` label → bull/bear/sideways.
    Returns None for an unknown / missing label so the caller can decide
    whether to fall back to the per-trade naive proxy."""
    if not label:
        return None
    return _LABEL_TO_REGIME.get(label)


def regime_scores_from_reports(reports: Iterable[dict]) -> dict[str, float]:
    """Bucket trades by the regime label of the day they closed on, then
    compute per-bucket win rate. Trades on days without a regime label
    (insufficient bars for ADX) are skipped — they don't add evidence
    either way. Buckets with no trades score 0.0 (same convention as the
    naive ``regime_scores``).
    """
    buckets: dict[str, list[dict]] = {"bull": [], "bear": [], "sideways": []}
    for report in reports:
        regime_block = report.get("market_regime") or {}
        bucket = label_to_regime(regime_block.get("label"))
        if bucket is None:
            continue
        for t in report.get("trades", []) or []:
            buckets[bucket].append(t)

    out: dict[str, float] = {}
    for regime, ts in buckets.items():
        if not ts:
            out[regime] = 0.0
            continue
        wins = sum(1 for t in ts if (t.get("pnl") or 0) > 0)
        out[regime] = wins / len(ts)
    return out


def compute_fitness(
    backtest_result: Any,
    weights: dict[str, float] | None = None,
) -> FitnessResult:
    """Score a ``BacktestResult`` (or a ``{"trades":..., "metrics":...}``
    dict). Regime scoring uses the per-trade naive proxy because no
    market-context is attached.

    For SEE evaluations, prefer :func:`compute_fitness_from_reports` —
    it consumes the same daily-report dicts the rest of the pipeline
    already produces and uses the real ADX/ATR regime labels.
    """
    w = weights if weights is not None else DEFAULT_WEIGHTS

    if isinstance(backtest_result, dict):
        trades = backtest_result.get("trades", [])
        metrics = backtest_result.get("metrics")
    else:
        trades = getattr(backtest_result, "trades", [])
        metrics = getattr(backtest_result, "metrics", None)

    return _compose(trades, metrics, regime_scores(trades), w)


class _TradeStub:
    """Minimal Trade-shaped wrapper around a report's trade-dict so it
    can be fed to :func:`src.backtest.metrics.calculate_metrics` and
    the rest of the fitness pipeline without re-instantiating the
    full :class:`src.backtest.broker.Trade` dataclass (which requires
    an ``OrderSide`` enum that the JSON form has lost)."""
    __slots__ = ("pnl", "entry_dt", "exit_dt", "entry_bar_index",
                 "exit_bar_index", "entry_price", "exit_price")

    def __init__(self, d: dict):
        self.pnl = int(d.get("pnl") or 0)
        self.entry_dt = d.get("entry_dt") or ""
        self.exit_dt = d.get("exit_dt") or ""
        self.entry_bar_index = int(d.get("entry_bar_index") or 0)
        self.exit_bar_index = int(d.get("exit_bar_index") or 0)
        self.entry_price = int(d.get("entry_price") or 0)
        self.exit_price = int(d.get("exit_price") or 0)


def compute_fitness_from_reports(
    reports: list[dict],
    weights: dict[str, float] | None = None,
) -> FitnessResult:
    """Score a strategy from its daily-report dicts.

    ``reports`` is the list returned by
    :func:`src.daily_report.report_generator.generate_report_from_backtest`
    (or a list of JSONs read back from ``data/daily-reports/``). All
    trades from all reports are concatenated, fresh aggregate
    ``PerformanceMetrics`` are computed, and per-regime win rates use
    the labels recorded on each report.

    Falls back gracefully on the per-trade naive regime proxy when ALL
    reports are missing regime data (e.g. backtests too short for ADX),
    so this function never refuses to score for missing context.
    """
    w = weights if weights is not None else DEFAULT_WEIGHTS

    # Lazy import: keeps fitness usable without the daily_report package
    # imported at module load (e.g. minimal CI environments).
    from src.backtest.metrics import calculate_metrics

    all_trades: list[dict] = []
    for r in reports:
        for t in r.get("trades", []) or []:
            all_trades.append(t)

    # Build aggregate metrics from the concatenated trade-dicts. The
    # report's per-day ``summary`` blocks can't be summed directly
    # (Sharpe, PF, max_dd are non-additive), so we re-derive from a
    # synthetic Trade list shaped to satisfy ``calculate_metrics``.
    synthetic = [_TradeStub(t) for t in all_trades]
    equity_curve: list[int] = []
    cum = 0
    for t in synthetic:
        cum += int(t.pnl or 0)
        equity_curve.append(cum)
    metrics = calculate_metrics(synthetic, equity_curve, initial_balance=0)

    # Real regime labels first; fall back to the per-trade proxy when
    # NO report carries a label at all.
    regimes = regime_scores_from_reports(reports)
    if all(v == 0.0 for v in regimes.values()) and synthetic:
        regimes = regime_scores(synthetic)

    return _compose(synthetic, metrics, regimes, w)
