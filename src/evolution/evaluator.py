"""Batch backtest evaluator for the Strategy Evolution Engine.

Two-phase evaluation:

* ``screen`` — quick 30-day in-sample backtest. Cheap, used to filter
  obvious losers from a large mutation batch before paying for the full
  evaluation. Tagged with the Flash AI tier so any downstream AI scoring
  picks the cheap model. Reports are NOT persisted (screen is a throw-
  away pass over many candidates).

* ``deep`` — full evaluation: in-sample composite on a 3-month window
  plus walk-forward fitness on the next 3-month out-of-sample window.
  Tagged with the Pro AI tier. Walk-forward fitness is the number that
  drives pool promotion (in-sample composite overfits trivially).
  Reports ARE persisted to ``data/daily-reports/`` so deep evaluations
  surface in the existing daily-report UI exactly like manual backtests.

Both phases run the backtest engine, then route the trades through
:func:`src.daily_report.report_generator.generate_report_from_backtest`
so regime classification (ADX/ATR/EMA labels) comes from the existing
classifier rather than an evolution-private heuristic. Fitness is then
computed from those daily-report dicts via
:func:`src.evolution.fitness.compute_fitness_from_reports`.

Anti-overfitting: each strategy can additionally be checked for
parameter robustness via Monte Carlo perturbation (±10% on numeric
``__init__`` defaults; >30% fitness variance flags the strategy as
fragile).
"""

from __future__ import annotations

import inspect
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..ai.chat_client import PROVIDER_GOOGLE, model_for_tier
from ..backtest.engine import BacktestEngine
from ..ai.code_sandbox import load_strategy_from_source
from ..daily_report.report_generator import generate_report_from_backtest
from ..market_data.models import Bar
from .fitness import (
    FitnessResult,
    compute_fitness,
    compute_fitness_from_reports,
)


# Days of bars used for the cheap "screen" pass. 30 calendar days is
# enough for a few dozen H4 trades or hundreds of 1-min trades —
# plenty to filter out broken strategies but cheap to run.
SCREEN_DAYS = 30
# In-sample window for the deep pass.
DEEP_TRAIN_DAYS = 90
# Out-of-sample (walk-forward) window for the deep pass.
DEEP_TEST_DAYS = 90

# Monte Carlo robustness check.
MC_PERTURBATION = 0.10   # ±10%
MC_TRIALS = 3
MC_FRAGILE_VARIANCE = 0.30   # composite variance > this → flagged fragile

# Sentinel call_site values — match the conventions used by
# ``src/ai/chat_client.py::_log_token_usage`` so AI usage attribution
# downstream lines up with the screen / deep phases.
CALL_SITE_SCREEN = "evolution_screen"
CALL_SITE_DEEP = "evolution_deep"


@dataclass
class EvalResult:
    """Per-strategy output of an evaluation pass.

    ``reports`` is the list of daily-report dicts produced by
    :func:`generate_report_from_backtest`. Persisting these to disk
    (deep phase default) makes evolution-driven backtests visible in
    the same ``data/daily-reports/`` UI as manual ones."""
    name: str
    source_code: str
    fitness: FitnessResult
    walkforward_fitness: float = 0.0
    walkforward_metrics: dict[str, Any] | None = None
    train_period: tuple[str, str] | None = None
    test_period: tuple[str, str] | None = None
    fragile: bool = False
    mc_variance: float = 0.0
    error: str | None = None
    phase: str = "screen"
    ai_tier: str = "light"
    ai_model_hint: str | None = None
    reports: list[dict] | None = None
    walkforward_reports: list[dict] | None = None


def _slice_bars_by_date(
    bars: list[Bar],
    start: Any | None = None,
    end: Any | None = None,
) -> list[Bar]:
    """Return bars whose ``dt`` falls in ``[start, end)``. ``None`` on
    either side means open-ended on that side."""
    if not bars:
        return []
    out = bars
    if start is not None:
        out = [b for b in out if b.dt >= start]
    if end is not None:
        out = [b for b in out if b.dt < end]
    return out


def _last_n_days(bars: list[Bar], days: int) -> list[Bar]:
    """Tail slice of bars covering approximately the last ``days`` of
    real time. Computed off the last bar's timestamp so test data with
    an arbitrary date range still produces a sensible window."""
    if not bars:
        return []
    cutoff = bars[-1].dt - timedelta(days=days)
    return [b for b in bars if b.dt >= cutoff]


def _split_train_test(
    bars: list[Bar],
    train_days: int,
    test_days: int,
) -> tuple[list[Bar], list[Bar]]:
    """Walk-forward split anchored at the END of ``bars``: most recent
    ``test_days`` is the test window, the ``train_days`` immediately
    before it is the training (in-sample) window. Older bars are
    discarded — they're not the period we want to evaluate on."""
    if not bars:
        return [], []
    end = bars[-1].dt
    test_start = end - timedelta(days=test_days)
    train_start = test_start - timedelta(days=train_days)
    train = _slice_bars_by_date(bars, train_start, test_start)
    test = _slice_bars_by_date(bars, test_start, None)
    return train, test


def _instantiate(
    strategy_cls: type,
    param_overrides: dict[str, Any] | None = None,
) -> Any:
    """Instantiate a strategy with optional parameter overrides.

    Strategies have varied ``__init__`` signatures (some take no args,
    some take a dict, most take individual numeric kwargs). We try the
    most common shapes in turn and let the strategy raise if none fit.
    """
    overrides = param_overrides or {}
    try:
        return strategy_cls(**overrides)
    except TypeError:
        # Fall back to no-args (used by AbstractStrategy-derived classes
        # that take a config dict instead of kwargs).
        if not overrides:
            return strategy_cls()
        raise


def _numeric_param_defaults(strategy_cls: type) -> dict[str, float]:
    """Pull numeric defaults from the strategy's ``__init__`` for MC
    perturbation. Booleans are excluded — they're integers in Python
    but not numeric in any meaningful sense for ±10%."""
    out: dict[str, float] = {}
    try:
        sig = inspect.signature(strategy_cls.__init__)
    except (TypeError, ValueError):
        return out
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        if p.default is inspect.Parameter.empty:
            continue
        if isinstance(p.default, bool):
            continue
        if isinstance(p.default, (int, float)):
            out[name] = float(p.default)
    return out


def _perturb(
    defaults: dict[str, float],
    rng: random.Random,
    pct: float = MC_PERTURBATION,
) -> dict[str, Any]:
    """Return a new dict with each default jittered by ±``pct``. Values
    that started as ints stay ints (integer periods like SMA(20) are
    nonsensical as 19.7); floats stay floats."""
    out: dict[str, Any] = {}
    for k, v in defaults.items():
        delta = rng.uniform(-pct, pct)
        new = v * (1.0 + delta)
        if float(v).is_integer():
            new = max(1, int(round(new)))
        out[k] = new
    return out


def _run_backtest(
    strategy_cls: type,
    bars: list[Bar],
    point_value: int = 200,
    fill_mode: str = "on_close",
    param_overrides: dict[str, Any] | None = None,
) -> Any:
    """Single backtest run. Returns the engine's BacktestResult."""
    strategy = _instantiate(strategy_cls, param_overrides)
    engine = BacktestEngine(
        strategy,
        point_value=point_value,
        max_bars=max(5000, len(bars)),
        fill_mode=fill_mode,
    )
    return engine.run(bars)


def _ohlc_arrays(bars: list[Bar]) -> tuple[list[int], list[int], list[int]]:
    """Materialize the highs/lows/closes arrays the regime classifier
    expects. Pulled out so the evaluator never silently passes None
    where the daily-report pipeline expects real data."""
    return ([b.high for b in bars],
            [b.low for b in bars],
            [b.close for b in bars])


def _backtest_with_reports(
    strategy_cls: type,
    bars: list[Bar],
    name: str,
    point_value: int,
    fill_mode: str,
    save_reports: bool,
    param_overrides: dict[str, Any] | None = None,
) -> tuple[Any, list[dict]]:
    """Run a backtest AND route the trades through the daily-report
    pipeline so each evaluation produces the same artifact shape as a
    manual backtest. Returns ``(BacktestResult, list[report_dict])``.

    ``save_reports=True`` persists each daily JSON to
    ``data/daily-reports/`` (deep phase default); screen-phase calls
    pass ``False`` to keep the throwaway pass cheap.
    """
    result = _run_backtest(strategy_cls, bars, point_value, fill_mode,
                           param_overrides)
    highs, lows, closes = _ohlc_arrays(bars)
    reports = generate_report_from_backtest(
        trades=result.trades,
        equity_curve=result.equity_curve,
        bars_highs=highs,
        bars_lows=lows,
        bars_closes=closes,
        strategy_name=name,
        point_value=point_value,
        save=save_reports,
    )
    return result, reports


def _bar_period(bars: list[Bar]) -> tuple[str, str] | None:
    if not bars:
        return None
    fmt = "%Y-%m-%d"
    return (bars[0].dt.strftime(fmt), bars[-1].dt.strftime(fmt))


def _ai_hint(tier: str) -> str | None:
    """Return the Google model name we'd prefer for the given tier so
    downstream AI calls (e.g. AI-driven mutation review) line up with
    the evaluator's screen/deep cost split. Returns ``None`` for
    non-Google providers — caller falls back to user default."""
    return model_for_tier(PROVIDER_GOOGLE, tier)


def monte_carlo_robustness(
    strategy_cls: type,
    bars: list[Bar],
    point_value: int = 200,
    fill_mode: str = "on_close",
    trials: int = MC_TRIALS,
    seed: int | None = 42,
) -> tuple[float, float]:
    """Run ``trials`` backtests with ±10% jittered numeric params.

    Returns ``(mean_composite, variance_pct)`` — variance_pct is the
    coefficient of variation (std/|mean|). Caller flags as fragile when
    ``variance_pct > MC_FRAGILE_VARIANCE``.
    """
    defaults = _numeric_param_defaults(strategy_cls)
    if not defaults or trials < 2:
        # No numeric params or not enough trials to compute variance —
        # robustness check is a no-op, reported as 0 variance.
        return 0.0, 0.0

    rng = random.Random(seed)
    composites: list[float] = []
    for _ in range(trials):
        overrides = _perturb(defaults, rng)
        try:
            result = _run_backtest(
                strategy_cls, bars,
                point_value=point_value,
                fill_mode=fill_mode,
                param_overrides=overrides,
            )
            fit = compute_fitness(result)
            composites.append(fit.composite)
        except Exception:
            # A perturbation that crashes the strategy IS fragility —
            # record a zero so it drags the mean down and inflates variance.
            composites.append(0.0)

    n = len(composites)
    mean = sum(composites) / n if n else 0.0
    if mean == 0:
        # All trials zero → no signal but no variance to report either.
        return 0.0, 0.0
    var = sum((c - mean) ** 2 for c in composites) / n
    std = var ** 0.5
    cv = std / abs(mean)
    return mean, cv


def evaluate_screen(
    strategies: list[tuple[str, str]],
    bars: list[Bar],
    point_value: int = 200,
    fill_mode: str = "on_close",
    days: int = SCREEN_DAYS,
    save_reports: bool = False,
) -> list[EvalResult]:
    """Cheap pass: 30-day in-sample backtest, regime-aware fitness, no
    persistence by default.

    Each strategy is run through the same daily-report pipeline used by
    manual backtests so regime scoring uses the real ADX/ATR labels.
    ``strategies`` is a list of ``(name, source_code)`` pairs. Failures
    (validation, runtime) are captured as ``EvalResult.error`` rather
    than raised — one bad mutation shouldn't kill the batch.
    """
    window = _last_n_days(bars, days)
    period = _bar_period(window)
    out: list[EvalResult] = []

    for name, source in strategies:
        try:
            cls = load_strategy_from_source(source)
            _, reports = _backtest_with_reports(
                cls, window, name, point_value, fill_mode, save_reports,
            )
            fit = compute_fitness_from_reports(reports)
            out.append(EvalResult(
                name=name,
                source_code=source,
                fitness=fit,
                walkforward_fitness=0.0,
                train_period=period,
                phase="screen",
                ai_tier="light",
                ai_model_hint=_ai_hint("light"),
                reports=reports,
            ))
        except Exception as e:
            out.append(EvalResult(
                name=name,
                source_code=source,
                fitness=compute_fitness({"trades": [], "metrics": None}),
                error=f"{type(e).__name__}: {e}",
                phase="screen",
                ai_tier="light",
                ai_model_hint=_ai_hint("light"),
            ))
    return out


def evaluate_deep(
    strategies: list[tuple[str, str]],
    bars: list[Bar],
    point_value: int = 200,
    fill_mode: str = "on_close",
    train_days: int = DEEP_TRAIN_DAYS,
    test_days: int = DEEP_TEST_DAYS,
    monte_carlo: bool = True,
    save_reports: bool = True,
) -> list[EvalResult]:
    """Full pass: 3-month in-sample + 3-month walk-forward + (optional)
    Monte Carlo robustness. ``walkforward_fitness`` on the returned
    EvalResult is the number that should drive pool promotion.

    Daily reports for both windows are persisted to
    ``data/daily-reports/`` by default so deep evaluations show up in
    the existing report UI alongside manual backtests. Pass
    ``save_reports=False`` to suppress (e.g. when sweeping a large
    candidate batch you don't want cluttering the report directory).
    """
    train, test = _split_train_test(bars, train_days, test_days)
    train_period = _bar_period(train)
    test_period = _bar_period(test)
    out: list[EvalResult] = []

    for name, source in strategies:
        try:
            cls = load_strategy_from_source(source)

            _, train_reports = _backtest_with_reports(
                cls, train, name, point_value, fill_mode, save_reports,
            )
            train_fit = compute_fitness_from_reports(train_reports)

            _, test_reports = _backtest_with_reports(
                cls, test, name, point_value, fill_mode, save_reports,
            )
            test_fit = compute_fitness_from_reports(test_reports)

            fragile = False
            mc_var = 0.0
            if monte_carlo:
                # MC stays on direct compute_fitness — perturbed
                # backtests are throw-away noise; persisting their
                # reports would clutter the data directory.
                _, mc_var = monte_carlo_robustness(
                    cls, train, point_value, fill_mode,
                )
                fragile = mc_var > MC_FRAGILE_VARIANCE

            out.append(EvalResult(
                name=name,
                source_code=source,
                fitness=train_fit,
                walkforward_fitness=test_fit.composite,
                walkforward_metrics=test_fit.to_dict(),
                train_period=train_period,
                test_period=test_period,
                fragile=fragile,
                mc_variance=mc_var,
                phase="deep",
                ai_tier="heavy",
                ai_model_hint=_ai_hint("heavy"),
                reports=train_reports,
                walkforward_reports=test_reports,
            ))
        except Exception as e:
            out.append(EvalResult(
                name=name,
                source_code=source,
                fitness=compute_fitness({"trades": [], "metrics": None}),
                walkforward_fitness=0.0,
                train_period=train_period,
                test_period=test_period,
                error=f"{type(e).__name__}: {e}",
                phase="deep",
                ai_tier="heavy",
                ai_model_hint=_ai_hint("heavy"),
            ))
    return out
