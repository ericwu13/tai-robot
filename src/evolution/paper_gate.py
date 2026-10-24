"""P1: relative paper-promotion gate and a recommend-only retire flag.

The real-money path keeps ``ABS_PF_FLOOR`` and every current gate. The
paper path uses those same rules until ``evolution.paper_gate`` is
``relative``. Retirement never disables a bot, never writes settings or
an allowlist, and never changes a pool status — it only returns text
for a log line, a changelog entry, and a report.

PBO, DSR, scaled month-slack, and the structural lane are other changes.
This module does not compute them. A missing post-backfill span (no
start date configured) defers that one check instead of failing it.
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .fitness import MIN_TRADES
from .pipeline import (
    EvolutionVerdict,
    Fold,
    combine_evolution_verdicts,
    decide_month_gates,
    decide_multifold_verdict,
    window_stats,
)

# Approved defaults (Nova Q2 / Q3). ``paper_gate`` stays absolute so a
# deploy that does not opt in keeps today's verdicts.
DEFAULT_BOOTSTRAP_RESAMPLES = 10_000
DEFAULT_BOOTSTRAP_SEED = 20261024
DEFAULT_MIN_NONEMPTY_FOLDS = 6
DEFAULT_MIN_DIVERGENT_FOLDS = 3
DEFAULT_FOLD_MAJORITY = 0.60
DEFAULT_BOOTSTRAP_ALPHA = 0.10
DEFAULT_EFFECT_SIZE_SIGMA = 0.25
DEFAULT_MIN_POST_BACKFILL_FOLDS = 2
DEFAULT_RETIRE_MIN_TRADES = 150
DEFAULT_RETIRE_PF_BELOW = 0.95
DEFAULT_RETIRE_UPPER_Q = 0.90
DEFAULT_RETIRE_CONSECUTIVE_WEEKS = 4
DEFAULT_RETIRE_IDLE_DAYS = 7
DEFAULT_RETIRE_STRUCTURAL_QUIET_DAYS = 60
DEFAULT_FOLD_DAYS = 14

_PAPER_GATE_VALUES = ("absolute", "relative")
_REAL_MONEY_MODES = ("semi_auto", "auto", "live", "full_auto")

# Changelog marker for one evaluated Saturday. Skipped weeks are not
# written, so they cannot count toward the retire streak. This is a
# record of a verdict, not a strategy change.
RELATIVE_EVAL_MARKER = "EVO_RELATIVE_GATE"
# Anchored at the start of the summary, and case-sensitive. A search
# anywhere in the line would treat the same text inside an AI plan
# excerpt as a real Saturday record. A different case is not the marker.
_EVAL_RE = re.compile(
    rf"^{re.escape(RELATIVE_EVAL_MARKER)}\b.*?"
    r"\bpassed=(true|false)\b.*?\bat="
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
)
_RELATIVE_MODES = ("paper", "backtest")


@dataclass(frozen=True)
class RelativeGateConfig:
    """Thresholds for the paper relative gate. Logged on every use."""

    min_nonempty_folds: int = DEFAULT_MIN_NONEMPTY_FOLDS
    min_divergent_folds: int = DEFAULT_MIN_DIVERGENT_FOLDS
    fold_majority: float = DEFAULT_FOLD_MAJORITY
    bootstrap_alpha: float = DEFAULT_BOOTSTRAP_ALPHA
    bootstrap_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED
    effect_size_sigma: float = DEFAULT_EFFECT_SIZE_SIGMA
    min_post_backfill_folds: int = DEFAULT_MIN_POST_BACKFILL_FOLDS
    # Empty: the ≥2 post-backfill check is deferred (backfill is separate).
    post_backfill_start: str = ""
    fold_days: int = DEFAULT_FOLD_DAYS


@dataclass(frozen=True)
class RetireConfig:
    """Thresholds for the recommend-only retire rule."""

    min_trades: int = DEFAULT_RETIRE_MIN_TRADES
    pf_below: float = DEFAULT_RETIRE_PF_BELOW
    upper_q: float = DEFAULT_RETIRE_UPPER_Q
    consecutive_weeks: int = DEFAULT_RETIRE_CONSECUTIVE_WEEKS
    min_eligible_trades: int = MIN_TRADES
    idle_days: int = DEFAULT_RETIRE_IDLE_DAYS
    structural_quiet_days: int = DEFAULT_RETIRE_STRUCTURAL_QUIET_DAYS
    bootstrap_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED
    fold_days: int = DEFAULT_FOLD_DAYS


@dataclass(frozen=True)
class EvolutionGateConfig:
    paper_gate: str = "absolute"
    real_gate: str = "absolute"
    relative: RelativeGateConfig = field(default_factory=RelativeGateConfig)
    retire: RetireConfig = field(default_factory=RetireConfig)
    # Empty when every *present* threshold was in range. A missing key
    # stays at its default and does not warn. An out-of-range key falls
    # back to the default and leaves one string here.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromotionGate:
    """Which rule set a promotion is judged under.

    ``mode`` is ``absolute`` or ``relative``. ``note`` is empty when the
    request matched the config; otherwise it says why real money stayed
    on the absolute floor.
    """

    mode: str
    note: str = ""


@dataclass(frozen=True)
class WeekRecord:
    """One Saturday. ``evaluated`` False is a skip and does not count."""

    at: str
    evaluated: bool
    passed: bool = False


@dataclass
class RetireRecommendation:
    """A suggestion. Nothing in here is an instruction to change a bot."""

    recommend: bool
    reasons: list[str]
    evidence: dict


def _int_or(value, default: int, *, minimum: int | None = None) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and n < minimum:
        return default
    return n


def _float_or(value, default: float, *, lo: float, hi: float) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    if x < lo or x > hi or x != x:  # NaN
        return default
    return x


def _present_open_float(mapping: dict, key: str, default: float, *,
                        lo: float, hi: float, warnings: list[str]) -> float:
    """Open interval ``(lo, hi)``. A missing key keeps the default quietly."""
    if key not in mapping:
        return default
    value = mapping[key]
    label = f"relative.{key}"
    if isinstance(value, bool):
        x = None
    else:
        try:
            x = float(value)
        except (TypeError, ValueError):
            x = None
    if x is None or not math.isfinite(x) or not (lo < x < hi):
        warnings.append(
            f"{label} {value!r} outside ({lo:g}, {hi:g}); "
            f"using default {default}")
        return default
    return x


def _present_closed_int(mapping: dict, key: str, default: int, *,
                        lo: int, hi: int, warnings: list[str]) -> int:
    """Closed interval ``[lo, hi]``. A missing key keeps the default quietly."""
    if key not in mapping:
        return default
    value = mapping[key]
    label = f"relative.{key}"
    n = None
    if isinstance(value, bool):
        n = None
    elif isinstance(value, float) and not value.is_integer():
        n = None
    else:
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = None
    if n is None or n < lo or n > hi:
        warnings.append(
            f"{label} {value!r} outside [{lo}, {hi}]; using default {default}")
        return default
    return n


def _choice(value, allowed: tuple[str, ...], default: str) -> str:
    if isinstance(value, str) and value.strip().lower() in allowed:
        return value.strip().lower()
    return default


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def evolution_gate_config_from_mapping(evo: dict | None) -> EvolutionGateConfig:
    """Parse ``evolution:`` . Missing or junk keys fall back to the defaults.

    ``real_gate`` other than ``absolute`` is stored as ``absolute``.
    The real-money path has no relative mode.
    """
    evo = _mapping(evo)
    rel = _mapping(evo.get("relative"))
    ret = _mapping(evo.get("retire"))
    paper = _choice(evo.get("paper_gate", "absolute"), _PAPER_GATE_VALUES,
                    "absolute")
    # Any non-absolute value, including "relative", is coerced.
    real = "absolute"
    warnings: list[str] = []
    # 0 or 1 makes majority / alpha / effect size trivial. A fold floor
    # of 1 (or above 30) is not the approved sample. Rejected values
    # fall back; absent keys do not warn.
    min_nonempty = _present_closed_int(
        rel, "min_nonempty_folds", DEFAULT_MIN_NONEMPTY_FOLDS,
        lo=6, hi=30, warnings=warnings)
    fold_majority = _present_open_float(
        rel, "fold_majority", DEFAULT_FOLD_MAJORITY,
        lo=0.0, hi=1.0, warnings=warnings)
    bootstrap_alpha = _present_open_float(
        rel, "bootstrap_alpha", DEFAULT_BOOTSTRAP_ALPHA,
        lo=0.0, hi=1.0, warnings=warnings)
    effect_size_sigma = _present_open_float(
        rel, "effect_size_sigma", DEFAULT_EFFECT_SIZE_SIGMA,
        lo=0.0, hi=1.0, warnings=warnings)
    return EvolutionGateConfig(
        paper_gate=paper,
        real_gate=real,
        warnings=tuple(warnings),
        relative=RelativeGateConfig(
            min_nonempty_folds=min_nonempty,
            min_divergent_folds=_int_or(
                rel.get("min_divergent_folds"), DEFAULT_MIN_DIVERGENT_FOLDS,
                minimum=1),
            fold_majority=fold_majority,
            bootstrap_alpha=bootstrap_alpha,
            bootstrap_resamples=_int_or(
                rel.get("bootstrap_resamples"), DEFAULT_BOOTSTRAP_RESAMPLES,
                minimum=1),
            bootstrap_seed=_int_or(
                rel.get("bootstrap_seed"), DEFAULT_BOOTSTRAP_SEED),
            effect_size_sigma=effect_size_sigma,
            min_post_backfill_folds=_int_or(
                rel.get("min_post_backfill_folds"),
                DEFAULT_MIN_POST_BACKFILL_FOLDS, minimum=0),
            post_backfill_start=str(
                rel.get("post_backfill_start") or "").strip(),
            fold_days=_int_or(
                rel.get("fold_days"), DEFAULT_FOLD_DAYS, minimum=1),
        ),
        retire=RetireConfig(
            min_trades=_int_or(
                ret.get("min_trades"), DEFAULT_RETIRE_MIN_TRADES, minimum=1),
            pf_below=_float_or(
                ret.get("pf_below"), DEFAULT_RETIRE_PF_BELOW, lo=0.0, hi=100.0),
            upper_q=_float_or(
                ret.get("upper_q"), DEFAULT_RETIRE_UPPER_Q, lo=0.0, hi=1.0),
            consecutive_weeks=_int_or(
                ret.get("consecutive_weeks"),
                DEFAULT_RETIRE_CONSECUTIVE_WEEKS, minimum=1),
            min_eligible_trades=_int_or(
                ret.get("min_eligible_trades"), MIN_TRADES, minimum=1),
            idle_days=_int_or(
                ret.get("idle_days"), DEFAULT_RETIRE_IDLE_DAYS, minimum=0),
            structural_quiet_days=_int_or(
                ret.get("structural_quiet_days"),
                DEFAULT_RETIRE_STRUCTURAL_QUIET_DAYS, minimum=0),
            bootstrap_resamples=_int_or(
                ret.get("bootstrap_resamples"), DEFAULT_BOOTSTRAP_RESAMPLES,
                minimum=1),
            bootstrap_seed=_int_or(
                ret.get("bootstrap_seed"), DEFAULT_BOOTSTRAP_SEED),
            fold_days=_int_or(
                ret.get("fold_days"), DEFAULT_FOLD_DAYS, minimum=1),
        ),
    )


def resolve_promotion_gate(trading_mode: str, paper_gate: str,
                           real_gate: str) -> PromotionGate:
    """Relative only for the exact strings ``paper`` and ``backtest``.

    ``semi_auto``, ``auto``, ``live``, and ``full_auto`` are real money
    and stay absolute, with a note that says so. ``""``, ``None``, and
    any other value — including a different case or surrounding space —
    stay absolute and are not treated as paper. Comparison is exact:
    no strip and no case fold.
    """
    paper = _choice(paper_gate, _PAPER_GATE_VALUES, "absolute")
    real_note = ""
    if (real_gate or "").strip().lower() not in ("", "absolute"):
        real_note = (
            f"real_gate {real_gate!r} ignored; real-money path stays absolute"
        )
    mode_key = trading_mode if isinstance(trading_mode, str) else ""
    if mode_key in _REAL_MONEY_MODES:
        note = (
            f"trading mode {mode_key} is real money; "
            f"paper_gate={paper} does not apply"
        )
        if real_note:
            note = f"{note}; {real_note}"
        return PromotionGate("absolute", note)
    if mode_key in _RELATIVE_MODES:
        if paper == "relative":
            return PromotionGate("relative", real_note)
        return PromotionGate("absolute", real_note)
    label = mode_key if mode_key else "(empty)"
    return PromotionGate(
        "absolute",
        f"trading mode {label} is not paper; absolute gate",
    )


def evolution_promotion_mode(*, runner, runner_idle: bool, configured_mode):
    """Mode the promotion gate should read at decision time.

    A running runner contributes its own ``trading_mode``, so a mid-run
    switch is the mode that is judged. An idle or missing runner
    contributes only the bot's configured mode. This never invents
    ``backtest`` or ``paper`` — an unknown configured mode stays
    unknown, and the resolver then keeps the absolute gate.
    """
    if runner is not None and not runner_idle:
        return getattr(runner, "trading_mode", None)
    return configured_mode


def gate_provenance(mode: str) -> str:
    """Tag a PASS record with the gate that actually cleared it.

    Anything other than the two real modes is recorded as absolute, so
    a later real-money deploy can see that the absolute floor was the
    one that passed.
    """
    label = mode if mode in ("absolute", "relative") else "absolute"
    return f"promotion_gate={label}"


def abs_pf_required(gate: PromotionGate) -> bool:
    """False only on the paper relative path."""
    return gate.mode != "relative"


def _as_stamp(value: str) -> str:
    s = (value or "").strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s + " 00:00"
    return s[:16]


def _parse_dt(value: str) -> datetime | None:
    s = (value or "").strip()
    for fmt, n in (
        ("%Y-%m-%dT%H:%M:%S", 19),
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
    ):
        if len(s) < n:
            continue
        try:
            return datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    return None


def _sample_stdev(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    mu = sum(values) / n
    var = sum((x - mu) ** 2 for x in values) / (n - 1)
    return math.sqrt(var)


def paired_fold_bootstrap_p(deltas: list[float], *, resamples: int,
                            seed: int) -> tuple[float, float]:
    """One-sided bootstrap p for H1: mean fold Δ > 0.

    p is the fraction of resample means that are ≤ 0. The seed is part
    of the logged evidence so a later run can replay the same draw.
    """
    if not deltas:
        return 1.0, 0.0
    # NaN and ±inf fail closed. ``nan <= 0`` is False, so leaving them
    # in the resample loop would report p=0 and pass the alpha check.
    try:
        finite = all(math.isfinite(d) for d in deltas)
    except TypeError:
        finite = False
    if not finite:
        return 1.0, float("nan")
    n = len(deltas)
    observed = sum(deltas) / n
    if resamples <= 0:
        return 1.0, observed
    rng = random.Random(seed)
    leq = 0
    for _ in range(resamples):
        total = 0.0
        for _j in range(n):
            total += deltas[rng.randrange(n)]
        if total / n <= 0.0:
            leq += 1
    return leq / resamples, observed


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        raise ValueError("empty sample")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = int(math.floor(q * (len(sorted_vals) - 1)))
    if idx < 0:
        idx = 0
    if idx >= len(sorted_vals):
        idx = len(sorted_vals) - 1
    return sorted_vals[idx]


def _fold_pnl_groups(trades: list, fold_days: int) -> list[list[int]]:
    """Non-empty 14-day groups of per-trade PnL, oldest first."""
    if fold_days <= 0:
        return []
    dated: list[tuple[datetime, int]] = []
    for t in trades or []:
        dt = _parse_dt(getattr(t, "exit_dt", "") or "")
        if dt is None:
            continue
        dated.append((dt, int(getattr(t, "pnl", 0) or 0)))
    if not dated:
        return []
    dated.sort(key=lambda item: item[0])
    last = dated[-1][0]
    end = datetime(last.year, last.month, last.day) + timedelta(days=1)
    first = dated[0][0]
    groups: list[list[int]] = []
    while end > first and len(groups) < 10000:
        start = end - timedelta(days=fold_days)
        pnls = [p for dt, p in dated if start <= dt < end]
        if pnls:
            groups.append(pnls)
        end = start
    groups.reverse()
    return groups


def mean_pnl_upper_bound(trades: list, *, q: float, resamples: int,
                         seed: int, fold_days: int) -> float | None:
    """Fold-bootstrap upper ``q`` bound on mean per-trade PnL.

    Folds are resampled with replacement. The statistic is the
    trade-weighted mean inside the resampled folds. None when fewer
    than two non-empty folds exist — a missing bound does not satisfy
    the retire rule.
    """
    groups = _fold_pnl_groups(trades, fold_days)
    if len(groups) < 2 or resamples <= 0:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    n_groups = len(groups)
    for _ in range(resamples):
        total = 0
        count = 0
        for _j in range(n_groups):
            group = groups[rng.randrange(n_groups)]
            total += sum(group)
            count += len(group)
        if count:
            means.append(total / count)
    if not means:
        return None
    means.sort()
    return _quantile(means, q)


def baseline_pnls_in_folds(trades: list, folds: list[Fold]) -> list[int]:
    """Baseline per-trade PnL whose exit falls inside the fold span."""
    if not folds:
        return []
    start, end = folds[0].start, folds[-1].end
    out: list[int] = []
    for t in trades or []:
        ed = getattr(t, "exit_dt", "") or ""
        if ed and start <= ed < end:
            out.append(int(getattr(t, "pnl", 0) or 0))
    return out


def nonempty_fold_count(folds: list[Fold] | None) -> int:
    """Folds in which the candidate actually traded."""
    return sum(1 for f in (folds or []) if f.cand.n > 0)


def meets_min_folds(n_folds: int, minimum: int = DEFAULT_MIN_NONEMPTY_FOLDS) -> bool:
    """True when the relative gate has enough non-empty folds (default ≥ 6).

    Fewer than ``minimum`` is not eligible: the relative verdict FAILs.
    """
    return int(n_folds) >= int(minimum)


def decide_relative_paper_checks(
        folds: list[Fold],
        base_trade_pnls: list[float],
        config: RelativeGateConfig | None = None) -> EvolutionVerdict:
    """Fold floor, bootstrap, effect size, and the post-backfill count.

    ``meets_min_folds`` (≥ 6 non-empty folds) is required here. A miss
    is a FAIL with its own reason line. Majority, collapse guards, and
    month gates are applied by the callers. PBO and DSR are named in
    the reason list and are not treated as a pass — they belong to a
    later change, which is why ``paper_gate`` stays ``absolute`` until
    that change lands.
    """
    cfg = config or RelativeGateConfig()
    reasons = [
        f"paper relative thresholds: nonempty≥{cfg.min_nonempty_folds} "
        f"divergent≥{cfg.min_divergent_folds} "
        f"majority≥{cfg.fold_majority:.0%} "
        f"bootstrap p<{cfg.bootstrap_alpha:g} "
        f"effect≥{cfg.effect_size_sigma:g}×σ "
        f"post-backfill≥{cfg.min_post_backfill_folds} "
        f"seed={cfg.bootstrap_seed} resamples={cfg.bootstrap_resamples}",
        "PBO and DSR not applied on this path (separate trial-penalty "
        "change); they are not treated as a pass",
    ]
    passed = True

    n_nonempty = nonempty_fold_count(folds)
    fold_ok = meets_min_folds(n_nonempty, cfg.min_nonempty_folds)
    if fold_ok:
        reasons.append(
            f"meets_min_folds: {n_nonempty} ≥ {cfg.min_nonempty_folds} "
            f"non-empty folds: eligible ✓")
    else:
        reasons.append(
            f"meets_min_folds: {n_nonempty} ≥ {cfg.min_nonempty_folds} "
            f"non-empty folds: FAIL ✗ — not enough folds for the "
            f"relative paper gate")
        passed = False

    deltas = [float(f.delta) for f in folds]
    p_value, observed = paired_fold_bootstrap_p(
        deltas, resamples=cfg.bootstrap_resamples, seed=cfg.bootstrap_seed)
    p_ok = p_value < cfg.bootstrap_alpha
    reasons.append(
        f"paired fold bootstrap: one-sided p {p_value:.4f} < "
        f"{cfg.bootstrap_alpha:g} on mean fold Δ {observed:.2f} "
        f"(resamples={cfg.bootstrap_resamples} seed={cfg.bootstrap_seed}): "
        f"{'✓' if p_ok else '✗'}")
    passed = passed and p_ok

    pnls = [float(p) for p in (base_trade_pnls or [])]
    sigma = _sample_stdev(pnls)
    total_delta = sum(deltas)
    if sigma is None or not pnls:
        reasons.append(
            "effect size: baseline per-trade σ undefined "
            f"(n={len(pnls)}) — need ≥2 baseline trades ✗")
        passed = False
    else:
        mean_delta = total_delta / len(pnls)
        floor = cfg.effect_size_sigma * sigma
        ok = mean_delta >= floor
        reasons.append(
            f"effect size: mean Δ/trade {mean_delta:.2f} ≥ "
            f"{cfg.effect_size_sigma:g} × σ {sigma:.2f} ({floor:.2f}) "
            f"of baseline per-trade PnL (n={len(pnls)}, sample σ): "
            f"{'✓' if ok else '✗'}")
        passed = passed and ok

    start = _as_stamp(cfg.post_backfill_start)
    if not start:
        reasons.append(
            "post-backfill folds: span not configured — check deferred "
            "(backfill is a separate change); not applied")
    else:
        n_post = sum(
            1 for f in folds
            if f.cand.n > 0 and f.start >= start)
        ok = n_post >= cfg.min_post_backfill_folds
        reasons.append(
            f"post-backfill folds: {n_post} ≥ {cfg.min_post_backfill_folds} "
            f"non-empty with start ≥ {start}: {'✓' if ok else '✗'}")
        passed = passed and ok

    return EvolutionVerdict(passed, reasons)


@dataclass(frozen=True)
class PromotionAssembly:
    """Combined verdict plus the month-gate observation.

    ``month_verdict.month_gates`` is always an attribute (None until a
    later change fills the per-month dict). A merge can pass
    ``month_gates=month_verdict.month_gates`` without NameError or
    AttributeError.

    ``window_verdict``, ``multifold_verdict``, and ``relative_verdict``
    are the same objects that were ANDed into ``verdict``. They do not
    change the combined decision. History persistence reads them so each
    gate's reasons can be stored separately. ``relative_verdict`` is
    None on the absolute path.
    """

    verdict: EvolutionVerdict
    month_verdict: EvolutionVerdict
    window_verdict: EvolutionVerdict | None = None
    multifold_verdict: EvolutionVerdict | None = None
    relative_verdict: EvolutionVerdict | None = None

    @property
    def month_gates(self):
        return self.month_verdict.month_gates


def assemble_promotion_verdict(
        *,
        gate: PromotionGate,
        gate_cfg: EvolutionGateConfig,
        folds: list[Fold],
        base_trades: list,
        cand_trades: list,
        holdout_start: str | None,
        criteria: dict | None,
        window_verdict: EvolutionVerdict,
        bars=None,
        month_gate=None,
) -> PromotionAssembly:
    """AND month gates, multi-fold, the walk-forward window, and (paper
    relative only) the bootstrap / effect-size checks.

    ``window_verdict`` must already have been built with
    ``require_abs_pf=abs_pf_required(gate)`` so the holdout PF floor
    matches this mode. This function does not rewrite ``window_verdict``.

    On the relative path the multi-fold divergent floor is
    ``relative.min_divergent_folds``, the same number the threshold
    line logs. The absolute path keeps ``MIN_DIVERGENT_FOLDS``.

    ``bars`` and ``month_gate`` are forwarded to ``decide_month_gates``.
    This module does not compute the scaled slack. Omitting both keeps
    the pre-P5 month gates.
    """
    relative = gate.mode == "relative"
    rel_cfg = gate_cfg.relative
    month = decide_month_gates(
        base_trades, cand_trades,
        holdout_start=holdout_start,
        drop_prior_pf_floor=relative,
        bars=bars,
        month_gate=month_gate,
    )
    mf = decide_multifold_verdict(
        folds, criteria,
        require_abs_pf=not relative,
        min_nonempty=(rel_cfg.min_nonempty_folds if relative else None),
        min_divergent=(rel_cfg.min_divergent_folds if relative else None),
        majority=(rel_cfg.fold_majority if relative else None),
    )
    parts = [month, mf, window_verdict]
    relative_verdict = None
    if relative:
        relative_verdict = decide_relative_paper_checks(
            folds, baseline_pnls_in_folds(base_trades, folds), rel_cfg)
        parts.append(relative_verdict)
    return PromotionAssembly(
        verdict=combine_evolution_verdicts(*parts),
        month_verdict=month,
        window_verdict=window_verdict,
        multifold_verdict=mf,
        relative_verdict=relative_verdict,
    )


def _dated_trades(trades: list) -> list:
    dated = []
    for t in trades or []:
        ed = getattr(t, "exit_dt", "") or ""
        if ed and _parse_dt(ed) is not None:
            dated.append(t)
    dated.sort(key=lambda t: getattr(t, "exit_dt", "") or "")
    return dated


def _fmt_pf(pf: float) -> str:
    if pf == float("inf"):
        return "INF"
    return f"{pf:.3f}"


def _last_exit(trades: list) -> datetime | None:
    last = None
    for t in _dated_trades(trades):
        dt = _parse_dt(getattr(t, "exit_dt", "") or "")
        if dt is not None and (last is None or dt > last):
            last = dt
    return last


def structural_graduation_recent(stamps: list[str] | None, now: datetime,
                                days: int) -> bool:
    """True when any supplied graduation stamp falls inside the window.

    An empty list means nothing has graduated on record. This module
    does not read a structural-lane store.
    """
    if days < 0:
        return False
    cutoff = now - timedelta(days=days)
    for stamp in stamps or []:
        dt = _parse_dt(str(stamp))
        if dt is not None and dt >= cutoff:
            return True
    return False


def dedupe_weeks(weeks: list[WeekRecord]) -> list[WeekRecord]:
    """One record per calendar day. The later record wins."""
    by_day: dict[str, WeekRecord] = {}
    for week in weeks or []:
        day = (week.at or "")[:10]
        if len(day) < 10:
            continue
        by_day[day] = week
    return [by_day[k] for k in sorted(by_day)]


def relative_eval_summary(passed: bool, at: str) -> str:
    """Changelog line for one evaluated Saturday. Not a strategy edit."""
    flag = "true" if passed else "false"
    return (
        f"{RELATIVE_EVAL_MARKER} record only; no strategy change; "
        f"passed={flag} at={at}"
    )


def weeks_from_changelog(entries: list[dict] | None,
                         strategy_name: str) -> list[WeekRecord]:
    """Read prior relative-gate results for this strategy.

    Entries for other strategies are ignored. Malformed lines are
    skipped. The result is chronological.
    """
    found: list[WeekRecord] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("strategy") or "") != strategy_name:
            continue
        summary = str(entry.get("change_summary") or "").lstrip()
        # match, not search: the marker has to lead the entry. The same
        # words inside an AI plan excerpt must not count as a Saturday.
        match = _EVAL_RE.match(summary)
        if match is None:
            continue
        found.append(WeekRecord(
            at=match.group(2),
            evaluated=True,
            passed=match.group(1).lower() == "true",
        ))
    return dedupe_weeks(found)


def assess_retirement(
        trades: list,
        *,
        weeks: list[WeekRecord] | None = None,
        structural_graduations: list[str] | None = None,
        now: datetime | None = None,
        config: RetireConfig | None = None,
) -> RetireRecommendation:
    """Flag a retire *candidate*. Does not modify ``trades`` or any bot.

    (a) ≥ ``min_trades`` dated closes, PF of the most recent window
        < ``pf_below``, and the fold-bootstrap upper bound on mean
        per-trade PnL < 0.
    (b) eligible (≥ ``min_eligible_trades``, not idle) and the relative
        gate failed on ``consecutive_weeks`` evaluated Saturdays, and
        no supplied structural graduation sits inside the quiet window.
        Skipped weeks (``evaluated`` False) do not count and do not reset.
    """
    cfg = config or RetireConfig()
    moment = now or datetime.now()
    dated = _dated_trades(trades)
    n_closed = len(dated)
    window = dated[-cfg.min_trades:] if n_closed else []
    stats = window_stats(window) if len(window) >= cfg.min_trades else None
    last_pf = None if stats is None else stats.profit_factor
    bound = None
    if stats is not None:
        bound = mean_pnl_upper_bound(
            window, q=cfg.upper_q, resamples=cfg.bootstrap_resamples,
            seed=cfg.bootstrap_seed, fold_days=cfg.fold_days)
    pf_low = (
        last_pf is not None
        and last_pf != float("inf")
        and last_pf < cfg.pf_below
    )
    bound_low = bound is not None and bound < 0
    condition_a = n_closed >= cfg.min_trades and pf_low and bound_low

    last = _last_exit(trades)
    if last is None:
        idle = True
    else:
        idle = (moment - last) >= timedelta(days=cfg.idle_days)
    eligible = n_closed >= cfg.min_eligible_trades and not idle
    evaluated = [
        w for w in dedupe_weeks(list(weeks or []))
        if w.evaluated
    ]
    tail = evaluated[-cfg.consecutive_weeks:]
    streak = (
        len(tail) >= cfg.consecutive_weeks
        and all(not w.passed for w in tail)
    )
    graduated = structural_graduation_recent(
        structural_graduations, moment, cfg.structural_quiet_days)
    condition_b = eligible and streak and not graduated

    thresholds = {
        "min_trades": cfg.min_trades,
        "pf_below": cfg.pf_below,
        "upper_q": cfg.upper_q,
        "consecutive_weeks": cfg.consecutive_weeks,
        "min_eligible_trades": cfg.min_eligible_trades,
        "idle_days": cfg.idle_days,
        "structural_quiet_days": cfg.structural_quiet_days,
        "bootstrap_seed": cfg.bootstrap_seed,
        "bootstrap_resamples": cfg.bootstrap_resamples,
    }
    evidence = {
        "n_closed": n_closed,
        "last_n": cfg.min_trades,
        "last_pf": last_pf,
        "mean_pnl_upper_bound": bound,
        "bootstrap_seed": cfg.bootstrap_seed,
        "bootstrap_resamples": cfg.bootstrap_resamples,
        "condition_a": condition_a,
        "eligible": eligible,
        "idle": idle,
        "evaluated_failures_tail": [w.at for w in tail] if streak else [],
        "condition_b": condition_b,
        "structural_graduation_recent": graduated,
        "thresholds": thresholds,
    }
    reasons: list[str] = []
    if condition_a:
        reasons.append(
            f"performance: last-{cfg.min_trades} PF {_fmt_pf(last_pf)} "
            f"< {cfg.pf_below:g} and upper {cfg.upper_q:.0%} bound "
            f"{bound:.2f} < 0 "
            f"(seed={cfg.bootstrap_seed} resamples={cfg.bootstrap_resamples})")
    else:
        reasons.append(
            f"performance rule not met: n={n_closed} "
            f"pf={_fmt_pf(last_pf) if last_pf is not None else 'n/a'} "
            f"upper_bound={bound if bound is not None else 'n/a'}")
    if condition_b:
        reasons.append(
            f"not improvable: relative gate failed {cfg.consecutive_weeks} "
            f"consecutive evaluated Saturdays "
            f"{[w.at for w in tail]}; no structural-lane graduation in the "
            f"last {cfg.structural_quiet_days} days on record")
    else:
        reasons.append(
            "not-improvable rule not met: "
            f"eligible={eligible} idle={idle} "
            f"evaluated={len(evaluated)} streak={streak} "
            f"structural_graduation_recent={graduated}")
    return RetireRecommendation(
        recommend=bool(condition_a or condition_b),
        reasons=reasons,
        evidence=evidence,
    )


def format_retire_recommendation(rec: RetireRecommendation) -> str:
    """Report text. States that no bot state was changed."""
    ev = rec.evidence
    th = ev.get("thresholds") or {}
    lines = [
        "RETIRE RECOMMENDATION only — no bot was disabled, retired, "
        "or removed from paper or live. This is a CEO call.",
    ]
    if ev.get("condition_a"):
        bound = ev.get("mean_pnl_upper_bound")
        lines.append(
            f"performance: last-{ev.get('last_n')} PF "
            f"{_fmt_pf(ev.get('last_pf'))} < {th.get('pf_below')} and upper "
            f"{float(th.get('upper_q') or 0):.0%} bound on mean per-trade "
            f"PnL {bound:.2f} < 0 "
            f"(seed={ev.get('bootstrap_seed')} "
            f"resamples={ev.get('bootstrap_resamples')})")
    if ev.get("condition_b"):
        lines.append(
            f"not improvable: relative gate failed "
            f"{th.get('consecutive_weeks')} consecutive evaluated Saturdays "
            f"{ev.get('evaluated_failures_tail')}; no structural-lane "
            f"graduation in the last {th.get('structural_quiet_days')} days "
            f"on record")
    lines.append("action: none")
    return "\n".join(lines)


def emit_retire_recommendation(
        rec: RetireRecommendation,
        *,
        log=None,
        changelog=None,
        report: list | None = None,
        strategy_name: str = "",
        version: str = "unchanged",
) -> None:
    """Write the recommendation to log, changelog, and report.

    When ``recommend`` is false this is a no-op. The callable arguments
    are the only outputs. There is no bot, settings, or pool handle.
    """
    if not rec.recommend:
        return
    text = format_retire_recommendation(rec)
    if log is not None:
        log(text)
    if report is not None:
        report.append(text)
    if changelog is not None:
        changelog(
            strategy_name=strategy_name or "bot",
            version_before=version,
            version_after=version,
            change_summary=text,
            initiated_by="ai",
        )
