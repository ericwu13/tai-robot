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

A third, additive validation layer lives at the bottom of this module:
``run_multifold_validation`` / ``decide_multifold_verdict`` slice one
continuous backtest into consecutive rolling folds and compare the two
sides fold by fold, because a single 14-day holdout on a strategy that
trades ~30×/month decides on ~14 trades (issue #99).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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

# Trade floor: a candidate that "filters everything away" trivially
# produces zero drawdown, so too few trades cannot pass — but "too few"
# must scale with the window. An absolute 10 was right for a 90-day
# test window and wrongly rejected an 8-trade candidate on a 14-day
# holdout where the baseline itself only made 18. The floor is now
# relative to the baseline's count in the SAME window (with an absolute
# minimum), falling back to the legacy absolute when no baseline exists.
MIN_CANDIDATE_TRADES = 10          # legacy absolute (no-baseline fallback)
MIN_CANDIDATE_TRADES_ABS = 5       # never pass with fewer than this
CANDIDATE_TRADES_RATIO = 0.25      # ≥ 25% of baseline's same-window count


def candidate_trade_floor(baseline_trades: int | None) -> int:
    """Minimum candidate trades for a valid verdict in this window."""
    if not baseline_trades or baseline_trades <= 0:
        return MIN_CANDIDATE_TRADES
    import math
    return max(MIN_CANDIDATE_TRADES_ABS,
               math.ceil(CANDIDATE_TRADES_RATIO * baseline_trades))

# Non-collapse gate on the DESIGN-side (train) window: the holdout
# decides the verdict, but a candidate that blows up on the very data
# its change was designed from is curve-fit to a sub-period. Generous
# ratios — this only catches collapse, not mild degradation (e.g. the
# observed atr-stop case: candidate train MaxDD 71% vs baseline 21%).
TRAIN_COLLAPSE_PF_RATIO = 0.8   # candidate PF < 80% of baseline's → collapse
TRAIN_COLLAPSE_DD_RATIO = 1.5   # candidate MaxDD% > 150% of baseline's → collapse

# Holdout drawdown is gated RELATIVE to the baseline, not against the
# plan's absolute threshold: dd% is measured from a zero-based P&L
# curve, so a short window's tiny peak makes the percentage explode
# (observed: baseline holdout dd% = 129 on 14 days while the long
# window reads 35). Absolute dd criteria are therefore checked on the
# train window — the same long-window basis the AI's threshold was
# anchored on — and the holdout only requires "not materially worse
# than baseline".
HOLDOUT_DD_RATIO_MAX = 1.2

# Absolute profitability floor (issue #99). Every gate above is RELATIVE
# — "not worse than the baseline" — which silently passes a candidate
# that lost every one of its 14 holdout trades because the baseline was
# equally broken in that window (PF 0 ≥ PF 0 ✓). A net-losing candidate
# is never an improvement worth saving, whatever the baseline did, so
# the candidate's own profit factor must clear 1.0 on its own. PF of
# float("inf") (wins, no losses) passes.
ABS_PF_FLOOR = 1.0

# No-op mutation gate (issue #99). The same incident's candidate made
# holdout trades nearly identical to the baseline's — the mutation
# barely changed behaviour, so the A/B had no discriminating power and
# the verdict was noise either way. Below this many DIVERGENT trades
# (present on exactly one side) there is nothing to judge; defer to the
# next window rather than PASS or FAIL on coincidence.
MIN_EXPRESSED_TRADES = 3


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
    Either way the trade floor and the ABS_PF_FLOOR profitability floor
    apply — neither can be overridden by the plan's criteria.
    """
    if candidate.error:
        return EvolutionVerdict(False, [f"candidate backtest error: {candidate.error}"])
    cm = candidate.metrics
    if cm is None or cm.total_trades == 0:
        return EvolutionVerdict(False, ["candidate produced 0 trades — nothing to validate"])

    reasons: list[str] = []
    base_n = (baseline.metrics.total_trades
              if baseline.metrics is not None else None)
    floor = candidate_trade_floor(base_n)
    floor_ok = cm.total_trades >= floor
    if not floor_ok:
        reasons.append(
            f"hard floor: candidate has {cm.total_trades} trades "
            f"(< {floor}, floor = max({MIN_CANDIDATE_TRADES_ABS}, "
            f"{CANDIDATE_TRADES_RATIO:.0%} of baseline's "
            f"{base_n if base_n else '?'}) ) — looks like the change "
            f"filtered (nearly) everything away")

    # Absolute profitability floor — applies in BOTH paths and cannot be
    # relaxed by the plan's criteria (issue #99).
    pf_ok = cm.profit_factor >= ABS_PF_FLOOR
    cpf_abs = ("INF" if cm.profit_factor == float("inf")
               else f"{cm.profit_factor:.2f}")
    reasons.append(
        f"hard floor: PF {cpf_abs} ≥ {ABS_PF_FLOOR:g} (absolute — a "
        f"net-losing candidate can never pass): {'✓' if pf_ok else '✗'}")

    if criteria:
        passed = floor_ok and pf_ok
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
    return EvolutionVerdict(floor_ok and pf_ok and ok_pf and ok_dd, reasons)


def trade_key(t) -> tuple:
    """Identity of a trade for baseline/candidate overlap counting.

    Two trades are "the same trade" when both sides opened and closed at
    the same time, the same price and the same bar. Timestamps alone are
    not enough (a strategy can re-enter on the same bar at a different
    price) and prices alone are not enough (the same price recurs).
    ``getattr`` with defaults keeps this usable on hand-built or
    deserialized trade-likes that lack some fields.
    """
    return (
        getattr(t, "entry_dt", "") or "",
        getattr(t, "exit_dt", "") or "",
        getattr(t, "entry_price", 0),
        getattr(t, "exit_price", 0),
        getattr(t, "entry_bar_index", -1),
        getattr(t, "exit_bar_index", -1),
    )


def split_trade_overlap(base_trades: list,
                        cand_trades: list) -> tuple[int, int, int]:
    """Returns ``(shared, base_only, cand_only)`` trade counts.

    Multiset intersection, not set intersection — a strategy that takes
    the identical trade twice must count as two shared trades, otherwise
    a repetitive baseline looks artificially divergent.
    """
    bc = Counter(trade_key(t) for t in (base_trades or []))
    cc = Counter(trade_key(t) for t in (cand_trades or []))
    shared = sum((bc & cc).values())
    return shared, sum(bc.values()) - shared, sum(cc.values()) - shared


def _side_trades(side: ABResult) -> list | None:
    """The trade list behind an ABResult, or None when there isn't one.

    Hand-built fixtures and error sides carry no result — callers use
    None to mean "skip the trade-level gate", never "zero trades".
    """
    res = getattr(side, "result", None)
    if res is None:
        return None
    trades = getattr(res, "trades", None)
    return list(trades) if trades is not None else None


def design_cutoff_index(trades: list, start: int, cut: str) -> int:
    """First index ≥ ``start`` whose exit_dt falls in the holdout (≥ cut).

    Trades are appended in close order, so the holdout is a suffix of
    the list: everything before the returned index is design evidence,
    everything from it on is withheld. Trades with no exit_dt can't be
    dated — they stay in the design set (conservative: the AI may see
    them, the verdict window never contains them since they predate
    the live session's recent tail).

    ``cut`` is a "YYYY-MM-DD HH:MM" string — zero-padded ISO ordering
    makes lexicographic comparison correct.
    """
    for i in range(start, len(trades)):
        ed = getattr(trades[i], "exit_dt", "")
        if ed and ed >= cut:
            return i
    return len(trades)


def compute_design_cut(trades: list, holdout_days: int) -> str:
    """The design/holdout boundary as a "YYYY-MM-DD HH:MM" string.

    Anchored at the LAST trade's exit (the evidence timeline), not the
    wall clock — a bot idle for weeks would otherwise put its entire
    history in the design set and leave an empty holdout. Falls back
    to "" (no holdout) when no trade is parseable.
    """
    last_exit = ""
    for t in reversed(trades):
        ed = getattr(t, "exit_dt", "")
        if ed:
            last_exit = ed
            break
    if not last_exit:
        return ""
    try:
        from datetime import datetime as _dt
        anchor = _dt.strptime(last_exit[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return ""
    return (anchor - timedelta(days=holdout_days)).strftime("%Y-%m-%d %H:%M")


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

    Gates, in order: candidate must run in the test window; the mutation
    must actually express itself (enough holdout trades that differ from
    the baseline's); must not be Monte Carlo fragile; OOS fitness
    composite must not be below the baseline's (skipped when both are
    gated by small samples); then the plan's criteria / default guard
    apply to the test-window metrics via ``decide_verdict`` (which also
    enforces the trade floor AND the absolute ABS_PF_FLOOR — that is the
    delegation path that keeps a net-losing holdout from passing).
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

    # No-op mutation gate (issue #99): when both sides expose their trade
    # lists, compare them trade-by-trade. Near-identical holdouts mean the
    # change never expressed — the metric comparison downstream is then
    # measuring noise, not the mutation. Silently skipped when either side
    # has no trade list (hand-built fixtures, error sides).
    base_test_trades = _side_trades(baseline.test)
    cand_test_trades = _side_trades(candidate.test)
    if base_test_trades is not None and cand_test_trades is not None:
        shared, base_only, cand_only = split_trade_overlap(
            base_test_trades, cand_test_trades)
        divergent = base_only + cand_only
        if divergent < MIN_EXPRESSED_TRADES:
            reasons.append(
                f"no-op mutation: only {divergent} divergent trades in the "
                f"holdout ({shared} identical) — the change did not express; "
                f"nothing to judge ✗ (defer to next window)")
            passed = False
        else:
            reasons.append(
                f"mutation expressed: {divergent} divergent holdout trades "
                f"({base_only} baseline-only / {cand_only} candidate-only, "
                f"{shared} identical) ✓")

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

    # Design-window non-collapse gate: catastrophic in-sample degradation
    # means the change is curve-fit to a sub-period, no matter how the
    # (often small) holdout looks.
    ctm, btm = candidate.train.metrics, baseline.train.metrics
    if (ctm is not None and btm is not None
            and ctm.total_trades > 0 and btm.total_trades > 0):
        if btm.profit_factor not in (0.0, float("inf")):
            pf_ratio = ctm.profit_factor / btm.profit_factor
            ok = pf_ratio >= TRAIN_COLLAPSE_PF_RATIO
            reasons.append(
                f"design-window PF ratio {pf_ratio:.2f} ≥ "
                f"{TRAIN_COLLAPSE_PF_RATIO:g}: {'✓' if ok else '✗ (collapse)'}")
            passed = passed and ok
        base_dd = max(btm.max_drawdown_pct, 1.0)  # floor avoids div-by-~0
        dd_ratio = ctm.max_drawdown_pct / base_dd
        ok = dd_ratio <= TRAIN_COLLAPSE_DD_RATIO
        reasons.append(
            f"design-window MaxDD ratio {dd_ratio:.2f} ≤ "
            f"{TRAIN_COLLAPSE_DD_RATIO:g}: {'✓' if ok else '✗ (collapse)'}")
        passed = passed and ok

    # The plan's absolute dd criterion is window-length sensitive —
    # check it on the train window (the long-window basis the AI's
    # threshold was anchored on) and replace it with a relative gate
    # on the holdout.
    holdout_criteria = dict(criteria) if criteria else None
    dd_max = (holdout_criteria.pop("max_drawdown_pct_max", None)
              if holdout_criteria else None)
    if dd_max is not None and candidate.train.metrics is not None \
            and candidate.train.metrics.total_trades > 0:
        tdd = candidate.train.metrics.max_drawdown_pct
        ok = tdd <= dd_max
        reasons.append(
            f"train-window MaxDD% {tdd:.2f} ≤ {dd_max:g} "
            f"(plan dd criterion, long-window basis): {'✓' if ok else '✗'}")
        passed = passed and ok
    btm_test = baseline.test.metrics
    if btm_test is not None and btm_test.total_trades > 0:
        bdd = max(btm_test.max_drawdown_pct, 1.0)
        dd_ratio = cm.max_drawdown_pct / bdd
        ok = dd_ratio <= HOLDOUT_DD_RATIO_MAX
        reasons.append(
            f"holdout MaxDD ratio {dd_ratio:.2f} ≤ "
            f"{HOLDOUT_DD_RATIO_MAX:g} (vs baseline): {'✓' if ok else '✗'}")
        passed = passed and ok

    sub = decide_verdict(baseline.test, candidate.test,
                         holdout_criteria or None)
    reasons.extend(sub.reasons)
    passed = passed and sub.passed

    if passed and cm.total_trades < 30:
        reasons.append(
            f"⚠ OOS 樣本少 small holdout sample ({cm.total_trades} trades "
            f"< 30) — PASS is PROVISIONAL; the rolling weekly holdout and "
            f"paper incubation provide the statistical confirmation")
    return EvolutionVerdict(passed, reasons, used_criteria=bool(criteria))


def format_deep_verdict_block(baseline: DeepResult, candidate: DeepResult,
                              verdict: EvolutionVerdict, data_desc: str,
                              saved_as: str = "") -> str:
    """Human-readable walk-forward verdict for the chat log."""
    lines = [
        f"🧬 EVO VERDICT (walk-forward) — {'PASS ✅' if verdict.passed else 'FAIL ❌'}",
        f"資料 Data: {data_desc}",
        f"訓練期 Train (design evidence): {candidate.train_span}",
        f"測試期 Test (holdout — UNSEEN by design, decides verdict): {candidate.test_span}",
        "── 訓練期 Design window (in-sample) ──",
        _side_summary(baseline.train),
        _side_summary(candidate.train),
        "── 測試期 Holdout (out-of-sample) ──",
        _side_summary(baseline.test),
        _side_summary(candidate.test),
        ("依計畫標準 plan criteria + walk-forward gates:" if verdict.used_criteria
         else "預設門檻 default guard + walk-forward gates:"),
    ]
    lines.extend(f"  {r}" for r in verdict.reasons)
    lines.extend(_verdict_trailer(verdict, saved_as))
    return "\n".join(lines)


def _verdict_trailer(verdict: EvolutionVerdict, saved_as: str) -> list[str]:
    """The saved-as / rejected footer shared by every verdict formatter."""
    if verdict.passed and saved_as:
        return [
            f"已存檔 Saved as「{saved_as}」— 部署仍需手動 deploy manually when ready.",
            "建議孵化 Forward incubation: deploy it in PAPER mode on the same "
            "session for ≥14 days / ≥30 trades before switching to "
            "semi_auto — the future is the only true out-of-sample test.",
        ]
    if not verdict.passed:
        return ["候選未通過，未存檔 Candidate rejected — nothing was saved."]
    return []


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
    lines.extend(_verdict_trailer(verdict, saved_as))
    return "\n".join(lines)


# ── Multi-fold paired validation (issue #99) ──────────────────────────
#
# A single 14-day holdout on a strategy that trades ~30×/month is
# statistically meaningless: 14 trades decide whether a change ships.
# This layer instead slices the SAME continuous backtest into
# consecutive rolling windows and compares the two sides fold by fold,
# so the evidence is paired (identical market data per fold) and pooled
# across months. It is additive — the walk-forward path above is
# untouched — and works on any trade list, including recorded live
# trades, because nothing here touches bars.


@dataclass
class WindowStats:
    """Trade statistics for one side inside one fold."""
    n: int = 0
    pnl: int = 0
    wins: int = 0
    gross_win: int = 0
    gross_loss: int = 0
    profit_factor: float = 0.0
    max_drawdown: int = 0   # points, peak-to-trough on cumulative P&L


def window_stats(trades: list) -> WindowStats:
    """Summarize a trade list.

    Deliberately independent of ``calculate_metrics`` (which needs an
    equity curve and a balance): a fold is a slice of trades with no
    equity curve of its own, and drawdown here is in POINTS, not the
    percentage that explodes on short windows (see HOLDOUT_DD_RATIO_MAX).
    Win/loss classification matches calculate_metrics: pnl > 0 wins,
    pnl ≤ 0 loses.
    """
    trades = list(trades or [])
    if not trades:
        return WindowStats()
    pnls = [int(getattr(t, "pnl", 0) or 0) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p <= 0))
    if gross_loss > 0:
        pf = gross_win / gross_loss
    else:
        pf = float("inf") if wins > 0 else 0.0
    peak = cum = mdd = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return WindowStats(n=len(trades), pnl=sum(pnls), wins=wins,
                       gross_win=gross_win, gross_loss=gross_loss,
                       profit_factor=pf, max_drawdown=mdd)


@dataclass
class Fold:
    """One rolling ``[start, end)`` window with both sides' results."""
    start: str                  # "YYYY-MM-DD HH:MM", inclusive
    end: str                    # "YYYY-MM-DD HH:MM", exclusive
    base: WindowStats
    cand: WindowStats
    shared: int
    base_only: int
    cand_only: int
    delta: int                  # cand.pnl - base.pnl


_FOLD_DT_FMT = "%Y-%m-%d %H:%M"


def _last_exit_dt(*trade_lists: list) -> str:
    """Latest exit_dt across the given lists ("" when none is dated).

    String max, not datetime max — exit_dt is zero-padded ISO so
    lexicographic ordering is chronological (same trick as
    ``design_cutoff_index``), and undated trades sort to "" harmlessly.
    """
    latest = ""
    for trades in trade_lists:
        for t in (trades or []):
            ed = getattr(t, "exit_dt", "") or ""
            if ed > latest:
                latest = ed
    return latest


def _bucket_trades(trades: list, start: str, end: str) -> list:
    """Trades whose exit_dt falls in ``[start, end)``. Undated ones are
    dropped — they can't be assigned to a fold, and guessing would
    silently credit the wrong window."""
    out = []
    for t in trades:
        ed = getattr(t, "exit_dt", "") or ""
        if ed and start <= ed < end:
            out.append(t)
    return out


def _any_trade_before(trades: list, cut: str) -> bool:
    for t in trades:
        ed = getattr(t, "exit_dt", "") or ""
        if ed and ed < cut:
            return True
    return False


def analyze_folds(base_trades: list, cand_trades: list, fold_days: int = 14,
                  max_folds: int = 10,
                  span_end: str | None = None) -> list[Fold]:
    """Slice both trade lists into consecutive rolling folds.

    Anchored at ``span_end`` (exclusive) or, by default, at the latest
    exit across both sides — the evidence timeline, not the wall clock,
    for the same reason ``compute_design_cut`` uses it. The derived
    anchor is rounded UP to the next midnight: windows are half-open, so
    anchoring exactly on the last exit would push that trade out of its
    own fold, and day-aligned boundaries read better in the report.

    Windows walk backward until the trades run out or ``max_folds`` is
    reached, then come back in CHRONOLOGICAL order — ``folds[-1]`` is the
    most recent window, i.e. the true holdout. Leading (oldest) folds
    where neither side traded are dropped; interior and trailing empty
    folds are kept, because "the candidate stopped trading" is evidence.
    """
    base_trades = list(base_trades or [])
    cand_trades = list(cand_trades or [])
    if span_end:
        anchor_s = span_end
    else:
        anchor_s = _last_exit_dt(base_trades, cand_trades)
    if not anchor_s:
        return []
    try:
        anchor = datetime.strptime(anchor_s[:16], _FOLD_DT_FMT)
    except ValueError:
        return []
    if span_end:
        end = anchor
    else:
        end = (datetime(anchor.year, anchor.month, anchor.day)
               + timedelta(days=1))

    bounds: list[tuple[str, str]] = []
    while len(bounds) < max_folds:
        start = end - timedelta(days=fold_days)
        s_str, e_str = (start.strftime(_FOLD_DT_FMT),
                        end.strftime(_FOLD_DT_FMT))
        bounds.append((s_str, e_str))
        if not (_any_trade_before(base_trades, s_str)
                or _any_trade_before(cand_trades, s_str)):
            break
        end = start

    folds: list[Fold] = []
    for s_str, e_str in reversed(bounds):
        bt = _bucket_trades(base_trades, s_str, e_str)
        ct = _bucket_trades(cand_trades, s_str, e_str)
        bs, cs = window_stats(bt), window_stats(ct)
        shared, base_only, cand_only = split_trade_overlap(bt, ct)
        folds.append(Fold(start=s_str, end=e_str, base=bs, cand=cs,
                          shared=shared, base_only=base_only,
                          cand_only=cand_only, delta=cs.pnl - bs.pnl))
    while folds and folds[0].base.n == 0 and folds[0].cand.n == 0:
        folds.pop(0)
    return folds


def _fmt_pf(pf: float) -> str:
    return "INF" if pf == float("inf") else f"{pf:.2f}"


def decide_multifold_verdict(
        folds: list[Fold],
        criteria: dict[str, float] | None = None) -> EvolutionVerdict:
    """PASS/FAIL from pooled, paired out-of-sample evidence.

    Gates (all ANDed, every one leaves a reason line): the most recent
    fold must be profitable in absolute terms (ABS_PF_FLOOR — the
    issue #99 loophole); the mutation must have expressed itself; the
    candidate must beat the baseline in at least half the folds where
    the two differ; and the paired total must be positive. Plan criteria,
    when given, apply to the candidate's AGGREGATE across all folds.
    """
    if not folds:
        return EvolutionVerdict(
            False, ["no folds — not enough dated trade history to validate"])
    cand_total = sum(f.cand.n for f in folds)
    if cand_total == 0:
        return EvolutionVerdict(
            False, [f"candidate produced 0 trades across {len(folds)} folds "
                    f"— nothing to validate"])

    reasons: list[str] = []
    passed = True

    # 1. Holdout floor — the most recent fold is the real out-of-sample
    #    window; a net-losing one can never pass, baseline or no baseline.
    holdout = folds[-1]
    if holdout.cand.n == 0:
        reasons.append(
            f"holdout fold ({holdout.start} → {holdout.end}): candidate made "
            f"no trades in the holdout fold ✗")
        passed = False
    else:
        pf_ok = holdout.cand.profit_factor >= ABS_PF_FLOOR
        note = ("✓" if pf_ok else
                f"✗ — candidate is net-losing in the most recent window "
                f"({holdout.start} → {holdout.end})")
        reasons.append(
            f"holdout fold PF {_fmt_pf(holdout.cand.profit_factor)} ≥ "
            f"{ABS_PF_FLOOR:g}: {note}")
        passed = passed and pf_ok

    # 2. No-op gate, pooled over every fold.
    divergent = sum(f.base_only + f.cand_only for f in folds)
    shared = sum(f.shared for f in folds)
    if divergent < MIN_EXPRESSED_TRADES:
        reasons.append(
            f"no-op mutation: only {divergent} divergent trades across "
            f"{len(folds)} folds ({shared} identical) — the change did not "
            f"express; nothing to judge ✗ (defer to next window)")
        passed = False
    else:
        reasons.append(
            f"mutation expressed: {divergent} divergent trades across "
            f"{len(folds)} folds ({shared} identical) ✓")

    # 3. Fold majority — consistency, not one lucky month. Folds where
    #    both sides scored identically carry no directional information
    #    and are excluded from the count.
    div_folds = [f for f in folds if f.delta != 0]
    if div_folds:
        wins = sum(1 for f in div_folds if f.delta > 0)
        ok = wins * 2 >= len(div_folds)
        reasons.append(
            f"fold majority: candidate wins {wins}/{len(div_folds)} divergent "
            f"folds (需 ≥ half): {'✓' if ok else '✗'}")
        passed = passed and ok
    else:
        reasons.append(
            "fold majority: no fold shows a P&L difference — no directional "
            "evidence ✗")
        passed = False

    # 4. Paired total across the identical fold set.
    total_delta = sum(f.delta for f in folds)
    ok = total_delta > 0
    reasons.append(
        f"paired total Δ {total_delta:+,} > 0 (candidate − baseline over the "
        f"same {len(folds)} folds): {'✓' if ok else '✗'}")
    passed = passed and ok

    # 5. Plan criteria on the pooled candidate aggregate.
    if criteria:
        agg_n = cand_total
        agg_pnl = sum(f.cand.pnl for f in folds)
        agg_wins = sum(f.cand.wins for f in folds)
        agg_gw = sum(f.cand.gross_win for f in folds)
        agg_gl = sum(f.cand.gross_loss for f in folds)
        if agg_gl > 0:
            agg_pf = agg_gw / agg_gl
        else:
            agg_pf = float("inf") if agg_gw > 0 else 0.0
        agg_wr = agg_wins / agg_n if agg_n else 0.0
        for key, val in criteria.items():
            if key == "max_drawdown_pct_max":
                # dd% is window-length sensitive (a 14-day fold's tiny
                # peak inflates it arbitrarily) — the long-window check
                # in decide_deep_verdict is where this criterion belongs.
                reasons.append(
                    f"MaxDD% ≤ {val:g} skipped here — dd% is window-length "
                    f"sensitive, checked on the long window instead")
                continue
            if key == "profit_factor_min":
                ok = agg_pf >= val
                reasons.append(
                    f"pooled PF {_fmt_pf(agg_pf)} ≥ {val:g}: "
                    f"{'✓' if ok else '✗'}")
            elif key == "win_rate_min":
                ok = agg_wr >= val
                reasons.append(
                    f"pooled WR {agg_wr * 100:.1f}% ≥ {val * 100:g}%: "
                    f"{'✓' if ok else '✗'}")
            elif key == "total_pnl_min":
                ok = agg_pnl >= val
                reasons.append(
                    f"pooled P&L {agg_pnl:+,} ≥ {val:+,.0f}: "
                    f"{'✓' if ok else '✗'}")
            else:
                continue
            passed = passed and ok

    if passed and cand_total < 30:
        reasons.append(
            f"⚠ OOS 樣本少 small pooled sample ({cand_total} trades across "
            f"{len(folds)} folds < 30) — PASS is PROVISIONAL; the rolling "
            f"weekly holdout and paper incubation provide the statistical "
            f"confirmation")
    return EvolutionVerdict(passed, reasons, used_criteria=bool(criteria))


def run_multifold_validation(baseline_cls, candidate_cls, bars,
                             point_value: int, fold_days: int = 14,
                             max_folds: int = 10
                             ) -> tuple[list[Fold], ABResult, ABResult]:
    """Backtest both sides ONCE over all bars, then slice into folds.

    Deliberately a single continuous run per side rather than one run per
    window (as ``run_deep_validation`` does): re-running each window
    cold-starts the strategy, so every window pays a warm-up penalty and
    loses positions open across its boundary. Slicing the trades of one
    continuous run keeps the folds contiguous and comparable.

    Returns ``(folds, baseline_ab, candidate_ab)``. On either side
    erroring, ``folds`` is empty — inspect ``.error`` on the ABResults.
    """
    bars = list(bars)
    base_ab = run_ab_backtest(baseline_cls, bars, point_value, "基準 baseline")
    cand_ab = run_ab_backtest(candidate_cls, bars, point_value, "候選 candidate")
    if (base_ab.error or cand_ab.error
            or base_ab.result is None or cand_ab.result is None):
        return [], base_ab, cand_ab
    folds = analyze_folds(base_ab.result.trades, cand_ab.result.trades,
                          fold_days=fold_days, max_folds=max_folds)
    return folds, base_ab, cand_ab


def format_multifold_block(folds: list[Fold], verdict: EvolutionVerdict,
                           data_desc: str, saved_as: str = "") -> str:
    """Human-readable multi-fold verdict for the chat log."""
    lines = [
        f"🧬 EVO VERDICT (multi-fold) — "
        f"{'PASS ✅' if verdict.passed else 'FAIL ❌'}",
        f"資料 Data: {data_desc}",
        f"滾動折數 Rolling folds: {len(folds)} "
        f"(最新在最後 newest last — that fold is the true holdout)",
    ]
    for i, f in enumerate(folds, 1):
        lines.append(
            f"  #{i} {f.start} → {f.end} | "
            f"base {f.base.n}/{f.base.pnl:+,} | "
            f"cand {f.cand.n}/{f.cand.pnl:+,} | "
            f"Δ {f.delta:+,} | shared {f.shared}")
    lines.append("依計畫標準 plan criteria + paired fold gates:"
                 if verdict.used_criteria
                 else "預設門檻 default paired fold gates:")
    lines.extend(f"  {r}" for r in verdict.reasons)
    lines.extend(_verdict_trailer(verdict, saved_as))
    return "\n".join(lines)
