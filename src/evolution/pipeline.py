"""Auto-evolution pipeline: plan directives → A/B validation → verdict.

Pure, testable logic only — the AI calls, threading, and Tk wiring live
in run_backtest.py. Flow when the user clicks 🧬 Bot Evolution with
auto-pipeline enabled:

  1. The evolution plan prompt requires a trailing machine-readable
     ```json directives block (action + validation criteria).
  2. ``parse_plan_directives`` extracts it (tolerant of garbage).
  3. The GUI generates a candidate strategy via codegen, then backtests
     baseline and candidate on the same bars (``run_ab_backtest``).
  4. ``decide_verdict`` checks the plan's own criteria (or a default
     not-worse-than-baseline guard) plus hard floors.
  5. PASS → the GUI saves the candidate to the StrategyStore and
     registers it in the dropdown. Deployment stays manual — an AI
     that redeploys its own live trading logic is a failure mode.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ..backtest.engine import BacktestEngine, BacktestResult
from ..backtest.metrics import PerformanceMetrics
from .fitness import FitnessResult, compute_fitness_from_trades

_JSON_FENCE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

# Criteria keys the plan may specify. Units: max_drawdown_pct_max in
# percent (35 = 35%), win_rate_min as a 0-1 fraction (45 is accepted
# and normalized), profit_factor_min plain, total_pnl_min in points.
_CRITERIA_KEYS = (
    "max_drawdown_pct_max",
    "profit_factor_min",
    "win_rate_min",
    "total_pnl_min",
)

# A candidate with fewer trades than this cannot pass validation no
# matter how good its metrics look — "filter everything away" trivially
# produces zero drawdown. Floor, not a fitness gate (that's MIN_TRADES).
MIN_CANDIDATE_TRADES = 10


def parse_plan_directives(plan_text: str) -> dict[str, Any]:
    """Extract the machine-readable directives from an evolution plan.

    Returns ``{"action": "change"|"no_change", "criteria": dict|None}``.
    Tolerant by design: a missing or corrupt block degrades to
    ``action="change", criteria=None`` so the pipeline still runs with
    the default not-worse-than-baseline guard rather than dying on a
    formatting whim of the model.
    """
    out: dict[str, Any] = {"action": "change", "criteria": None}
    matches = _JSON_FENCE.findall(plan_text or "")
    if not matches:
        return out
    try:
        data = json.loads(matches[-1])
    except (json.JSONDecodeError, TypeError):
        return out
    if not isinstance(data, dict):
        return out

    action = str(data.get("action", "change")).strip().lower()
    if action == "no_change":
        out["action"] = "no_change"

    criteria: dict[str, float] = {}
    for key in _CRITERIA_KEYS:
        if key in data:
            try:
                val = float(data[key])
            except (TypeError, ValueError):
                continue
            if key == "win_rate_min" and val > 1:
                val = val / 100.0  # model wrote 45 for 45%
            criteria[key] = val
    out["criteria"] = criteria or None
    return out


def next_candidate_name(base_name: str, taken: set[str] | None = None) -> str:
    """``FooStrategy`` → first free ``FooStrategyEvoN``.

    An existing ``EvoN`` suffix is stripped first so evolving an evolved
    strategy yields ``FooStrategyEvo2``, not ``FooStrategyEvo1Evo1``.
    """
    taken = taken or set()
    root = re.sub(r"Evo\d+$", "", base_name)
    n = 1
    while f"{root}Evo{n}" in taken or f"{root}Evo{n}" == base_name:
        n += 1
    return f"{root}Evo{n}"


@dataclass
class ABResult:
    """One side of the A/B validation backtest."""
    name: str
    result: BacktestResult | None = None
    fitness: FitnessResult | None = None
    error: str = ""

    @property
    def metrics(self) -> PerformanceMetrics | None:
        return self.result.metrics if self.result else None


def run_ab_backtest(strategy_cls, bars, point_value: int, name: str) -> ABResult:
    """Backtest one strategy class (default-constructed) on the given bars.

    Both sides of the A/B get identical treatment — default params, same
    bars, same fill mode — so the comparison isolates the code change.
    Never raises; failures come back in ``error``.
    """
    try:
        strategy = strategy_cls()
        engine = BacktestEngine(strategy, point_value=point_value)
        result = engine.run(list(bars))
        fitness = compute_fitness_from_trades(result.trades, result.equity_curve)
        return ABResult(name=name, result=result, fitness=fitness)
    except Exception as e:
        return ABResult(name=name, error=f"[{type(e).__name__}] {e}")


@dataclass
class EvolutionVerdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    used_criteria: bool = False


def decide_verdict(baseline: ABResult, candidate: ABResult,
                   criteria: dict[str, float] | None) -> EvolutionVerdict:
    """PASS/FAIL the candidate.

    With plan criteria: every specified criterion must hold (absolute
    checks on the candidate). Without: default guard — candidate must
    not be worse than baseline on profit factor AND max drawdown.
    Either way the MIN_CANDIDATE_TRADES floor applies.
    """
    if candidate.error:
        return EvolutionVerdict(False, [f"candidate backtest error: {candidate.error}"])
    cm = candidate.metrics
    if cm is None or cm.total_trades == 0:
        return EvolutionVerdict(False, ["candidate produced 0 trades — nothing to validate"])

    reasons: list[str] = []
    floor_ok = cm.total_trades >= MIN_CANDIDATE_TRADES
    if not floor_ok:
        reasons.append(
            f"hard floor: candidate has {cm.total_trades} trades "
            f"(< {MIN_CANDIDATE_TRADES}) — too few to validate")

    if criteria:
        passed = floor_ok
        for key, val in criteria.items():
            if key == "max_drawdown_pct_max":
                ok = cm.max_drawdown_pct <= val
                reasons.append(f"MaxDD% {cm.max_drawdown_pct:.2f} ≤ {val:g}: {'✓' if ok else '✗'}")
            elif key == "profit_factor_min":
                ok = cm.profit_factor >= val
                pf = "INF" if cm.profit_factor == float("inf") else f"{cm.profit_factor:.2f}"
                reasons.append(f"PF {pf} ≥ {val:g}: {'✓' if ok else '✗'}")
            elif key == "win_rate_min":
                ok = cm.win_rate >= val
                reasons.append(f"WR {cm.win_rate * 100:.1f}% ≥ {val * 100:g}%: {'✓' if ok else '✗'}")
            elif key == "total_pnl_min":
                ok = cm.total_pnl >= val
                reasons.append(f"P&L {cm.total_pnl:+,} ≥ {val:+,.0f}: {'✓' if ok else '✗'}")
            else:
                continue
            passed = passed and ok
        return EvolutionVerdict(passed, reasons, used_criteria=True)

    # Default guard: not worse than baseline on PF and MaxDD.
    if baseline.error or baseline.metrics is None:
        return EvolutionVerdict(
            False, [f"baseline backtest error: {baseline.error or 'no result'}"])
    bm = baseline.metrics
    ok_pf = cm.profit_factor >= bm.profit_factor
    ok_dd = cm.max_drawdown_pct <= bm.max_drawdown_pct
    cpf = "INF" if cm.profit_factor == float("inf") else f"{cm.profit_factor:.2f}"
    bpf = "INF" if bm.profit_factor == float("inf") else f"{bm.profit_factor:.2f}"
    reasons.append(f"PF {cpf} vs baseline {bpf}: {'✓' if ok_pf else '✗'}")
    reasons.append(
        f"MaxDD% {cm.max_drawdown_pct:.2f} vs baseline "
        f"{bm.max_drawdown_pct:.2f}: {'✓' if ok_dd else '✗'}")
    return EvolutionVerdict(floor_ok and ok_pf and ok_dd, reasons)


def effective_windows(span_days: int, train_days: int,
                      test_days: int) -> tuple[int, int]:
    """Scale the walk-forward windows to the available history.

    Full SEE-spec windows (90/90) when data suffices. With less data the
    split is scaled — test gets half the span (min 7 days), train the
    rest — so the verdict stays out-of-sample, just over a shorter
    horizon. Below 21 days walk-forward is meaningless: returns (0, 0)
    to tell the caller to use the simple single-window A/B instead.
    """
    if span_days >= train_days + test_days:
        return train_days, test_days
    if span_days < 21:
        return 0, 0
    test = max(7, span_days // 2)
    return span_days - test, test


def split_train_test(bars: list, train_days: int,
                     test_days: int) -> tuple[list, list]:
    """Walk-forward split anchored at the END of ``bars`` (same semantics
    as evaluator._split_train_test): the most recent ``test_days`` form
    the out-of-sample test window, the ``train_days`` before it the
    in-sample window. Older bars are discarded.
    """
    if not bars:
        return [], []
    end = bars[-1].dt
    test_start = end - timedelta(days=test_days)
    train_start = test_start - timedelta(days=train_days)
    train = [b for b in bars if train_start <= b.dt < test_start]
    test = [b for b in bars if b.dt >= test_start]
    return train, test


@dataclass
class DeepResult:
    """One side of the walk-forward validation."""
    name: str
    train: ABResult
    test: ABResult
    fragile: bool = False
    mc_variance: float = 0.0
    train_span: str = ""
    test_span: str = ""


def _span_desc(bars: list) -> str:
    if not bars:
        return "(empty)"
    return (f"{bars[0].dt:%Y-%m-%d} → {bars[-1].dt:%Y-%m-%d} "
            f"({len(bars)} bars)")


def run_deep_validation(strategy_cls, bars, point_value: int, name: str,
                        train_days: int, test_days: int,
                        monte_carlo: bool = True) -> DeepResult:
    """Walk-forward validation per the SEE spec: backtest the train and
    test windows separately; optionally Monte Carlo the train window
    (±10% jitter on numeric __init__ defaults — a candidate whose edge
    evaporates under tiny param changes is curve-fit, not improved).
    """
    train, test = split_train_test(bars, train_days, test_days)
    tr = run_ab_backtest(strategy_cls, train, point_value, f"{name} train")
    te = run_ab_backtest(strategy_cls, test, point_value, f"{name} test")
    fragile = False
    mc_var = 0.0
    if monte_carlo and not tr.error and train:
        try:
            from .evaluator import MC_FRAGILE_VARIANCE, monte_carlo_robustness
            _, mc_var = monte_carlo_robustness(strategy_cls, train, point_value)
            fragile = mc_var > MC_FRAGILE_VARIANCE
        except Exception:
            pass  # robustness check is best-effort, never kills the run
    return DeepResult(name=name, train=tr, test=te, fragile=fragile,
                      mc_variance=mc_var, train_span=_span_desc(train),
                      test_span=_span_desc(test))


def decide_deep_verdict(baseline: DeepResult, candidate: DeepResult,
                        criteria: dict[str, float] | None) -> EvolutionVerdict:
    """Walk-forward verdict: the OUT-OF-SAMPLE test window decides.

    Gates, in order: candidate must run in the test window; must not be
    Monte Carlo fragile; OOS fitness composite must not be below the
    baseline's (skipped when both are gated by small samples); then the
    plan's criteria / default guard apply to the test-window metrics via
    decide_verdict (which also enforces the trade floor).
    """
    if candidate.test.error:
        return EvolutionVerdict(
            False, [f"candidate test-window error: {candidate.test.error}"])
    cm = candidate.test.metrics
    if cm is None or cm.total_trades == 0:
        return EvolutionVerdict(
            False, ["candidate produced 0 trades in the test window"])

    reasons: list[str] = []
    passed = True

    if candidate.fragile:
        reasons.append(
            f"Monte Carlo FRAGILE: CV {candidate.mc_variance:.2f} > 0.30 — "
            f"±10% param jitter destabilizes results (curve-fit)")
        passed = False
    elif candidate.mc_variance > 0:
        reasons.append(
            f"Monte Carlo robustness: CV {candidate.mc_variance:.2f} ≤ 0.30 ✓")

    cf, bf = candidate.test.fitness, baseline.test.fitness
    if cf is not None and bf is not None and not (cf.gated and bf.gated):
        ok = cf.composite >= bf.composite
        reasons.append(
            f"OOS composite {cf.composite:.3f} vs baseline "
            f"{bf.composite:.3f}: {'✓' if ok else '✗'}")
        passed = passed and ok
    else:
        reasons.append(
            "OOS composite gated on both sides (< 30 test-window trades) — "
            "composite gate skipped")

    sub = decide_verdict(baseline.test, candidate.test, criteria)
    reasons.extend(sub.reasons)
    passed = passed and sub.passed
    return EvolutionVerdict(passed, reasons, used_criteria=sub.used_criteria)


def format_deep_verdict_block(baseline: DeepResult, candidate: DeepResult,
                              verdict: EvolutionVerdict, data_desc: str,
                              saved_as: str = "") -> str:
    """Human-readable walk-forward verdict for the chat log."""
    lines = [
        f"🧬 EVO VERDICT (walk-forward) — {'PASS ✅' if verdict.passed else 'FAIL ❌'}",
        f"資料 Data: {data_desc}",
        f"訓練期 Train (in-sample): {candidate.train_span}",
        f"測試期 Test (out-of-sample, decides verdict): {candidate.test_span}",
        "── 訓練期 In-sample ──",
        _side_summary(baseline.train),
        _side_summary(candidate.train),
        "── 測試期 Out-of-sample ──",
        _side_summary(baseline.test),
        _side_summary(candidate.test),
        ("依計畫標準 plan criteria + walk-forward gates:" if verdict.used_criteria
         else "預設門檻 default guard + walk-forward gates:"),
    ]
    lines.extend(f"  {r}" for r in verdict.reasons)
    if verdict.passed and saved_as:
        lines.append(f"已存檔 Saved as「{saved_as}」— 部署仍需手動 deploy manually when ready.")
    elif not verdict.passed:
        lines.append("候選未通過，未存檔 Candidate rejected — nothing was saved.")
    return "\n".join(lines)


def _side_summary(side: ABResult) -> str:
    if side.error:
        return f"  {side.name}: ERROR {side.error}"
    m = side.metrics
    pf = "INF" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
    comp = ""
    if side.fitness is not None:
        gated = " (gated)" if side.fitness.gated else ""
        comp = f" | composite {side.fitness.composite:.3f}{gated}"
    return (f"  {side.name}: {m.total_trades} trades | "
            f"P&L {m.total_pnl:+,} | PF {pf} | "
            f"WR {m.win_rate * 100:.1f}% | MaxDD% {m.max_drawdown_pct:.2f}{comp}")


def format_verdict_block(baseline: ABResult, candidate: ABResult,
                         verdict: EvolutionVerdict, window_desc: str,
                         saved_as: str = "") -> str:
    """Human-readable verdict for the chat log."""
    lines = [
        f"🧬 EVO VERDICT — {'PASS ✅' if verdict.passed else 'FAIL ❌'}",
        f"驗證範圍 Validation window: {window_desc}",
        _side_summary(baseline),
        _side_summary(candidate),
    ]
    header = ("依計畫標準 plan criteria:" if verdict.used_criteria
              else "預設門檻 default guard (not worse than baseline):")
    lines.append(header)
    lines.extend(f"  {r}" for r in verdict.reasons)
    if verdict.passed and saved_as:
        lines.append(f"已存檔 Saved as「{saved_as}」— 部署仍需手動 deploy manually when ready.")
    elif not verdict.passed:
        lines.append("候選未通過，未存檔 Candidate rejected — nothing was saved.")
    return "\n".join(lines)
