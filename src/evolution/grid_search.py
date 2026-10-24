"""Saturday knob search: a pre-registered grid, then DSR, PBO, and a plateau.

The model does not pick the setting. Eligible knobs come from
``class_knob_eligibility``. The value menu is wider than the LLM
allowlist (ema 80 and 90 are reachable here only). The grid is built
before any result is looked at.

Caps, hard even if a caller asks for more:

- at most 48 settings
- at most 2 knobs changed together
- a 2-knob setting uses at most 4 values per knob (the ones nearest
  the baseline). The single-knob axis still lists the full menu, so
  80 and 90 are settings.

Singles are registered first, then pairs, and the list is cut at 48.
That order does not depend on performance.

A point is on the plateau when its mean fold Δ beats the baseline and
enough immediate neighbours (catalog step ±1 on one knob) are also
good: at least 2, or every neighbour when it has only one. "Good"
means the neighbour's mean fold Δ is > 0 and within 50% of the chosen
point's Δ (``|neighbour − chosen| ≤ band × |chosen|``). A point with
no evaluated neighbour is not on the plateau. The pick is the centre
of that set (closest to the median coordinate), not the maximum Δ.
The centre must also clear DSR > 0.90 and the trial set must clear
PBO < 0.50. Missing either one selects nothing. There is no fallback
to the best Sharpe and no fallback to an LLM pick.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .knobs import (
    class_knob_eligibility,
    knob_grid_for,
    read_strategy_knobs,
    rewrite_knob_source,
)
from .paper_gate import (
    TrialPenaltyConfig,
    evolution_gate_config_from_mapping,
)
from .trial_penalty import (
    TrialEvidence,
    choose_partitions,
    effective_trial_count,
    mean_pairwise_correlation,
    probability_of_backtest_overfitting,
    score_configuration,
    sharpe_ratio,
    trial_sharpe_std,
)

# Hard caps. The config parser also refuses anything above these.
HARD_MAX_SETTINGS = 48
HARD_MAX_JOINT_KNOBS = 2
HARD_MAX_VALUES_PER_KNOB = 4

# Wider than DYNAMIC_EXIT_PULLBACK_GRID. 80 and 90 stay off the LLM
# allowlist; this menu is the penalised sweep only.
SEARCH_KNOB_CATALOG: dict[str, tuple] = {
    "ema_period": (30, 40, 50, 60, 70, 80, 90, 100),
    "atr_period": (10, 14, 21),
    "atr_stop_mult": (1.5, 2.0, 2.5, 3.0, 4.0),
    "atr_buffer_mult": (0.3, 0.5, 0.8),
}

SEARCH_GRID_BATCH = "grid_batch"
SEARCH_LLM_ONE = "llm_one"

_EXPLAIN_ONLY = (
    "The grid has already selected this setting. "
    "Do NOT propose a different setting. The selection is final."
)


@dataclass(frozen=True)
class GridPoint:
    """One evaluated coordinate. The baseline has empty ``knobs``."""

    knobs: tuple
    coords: tuple
    index: tuple
    fold_pnls: tuple
    mean_delta: float
    is_baseline: bool = False


@dataclass(frozen=True)
class GridOutcome:
    """What the Saturday search decided.

    ``selected`` is the knob dict to apply, or None. ``settings`` is the
    pre-registered list (built before evaluation). ``evidence`` is the
    chosen point's trial penalty when a point was chosen, else None.
    ``rationale`` is the text used when the model does not explain.
    """

    selected: dict | None
    reasons: tuple
    settings: tuple
    evidence: TrialEvidence | None
    rationale: str


def _same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return a == b


def _coord_key(coords) -> tuple:
    out = []
    for value in coords:
        try:
            out.append(round(float(value), 6))
        except (TypeError, ValueError):
            out.append(value)
    return tuple(out)


def _index_of(axis: tuple, value) -> int | None:
    for i, item in enumerate(axis):
        if _same(item, value):
            return i
    return None


def catalog_values(key: str, eligibility_values) -> tuple:
    """Search menu for one knob. Known keys use the widened catalog."""
    if key in SEARCH_KNOB_CATALOG:
        return SEARCH_KNOB_CATALOG[key]
    return tuple(eligibility_values)


def nearest_values(values, baseline, limit: int) -> tuple:
    """Up to ``limit`` menu values nearest ``baseline``, menu order.

    The baseline is included when it sits on the menu. Equal distance
    keeps the lower value. ``limit`` is already clamped by the caller.
    """
    vals = list(values)
    if not vals:
        return ()
    if len(vals) <= limit:
        return tuple(vals)
    if any(_same(baseline, v) for v in vals):
        anchor = float(baseline)
    else:
        anchor = float(min(vals, key=lambda v: (abs(float(v) - float(baseline)), float(v))))
    ranked = sorted(vals, key=lambda v: (abs(float(v) - anchor), float(v)))
    chosen = ranked[:limit]
    return tuple(v for v in vals if any(_same(v, c) for c in chosen))


def _clamp_caps(max_settings: int, max_joint_knobs: int,
                max_values_per_knob: int) -> tuple[int, int, int]:
    settings = min(HARD_MAX_SETTINGS, max(0, int(max_settings)))
    joint = min(HARD_MAX_JOINT_KNOBS, max(1, int(max_joint_knobs)))
    values = min(HARD_MAX_VALUES_PER_KNOB, max(1, int(max_values_per_knob)))
    return settings, joint, values


def iter_search_settings(
        baseline: dict,
        eligibility_grid: dict,
        *,
        max_joint_knobs: int = HARD_MAX_JOINT_KNOBS,
        max_values_per_knob: int = HARD_MAX_VALUES_PER_KNOB) -> list[dict]:
    """Every single-knob setting, then every pair, with no 48-cap.

    The pair menu for each knob is the nearest
    ``max_values_per_knob`` values. The single-knob axis uses the full
    menu. Order is eligibility-key order, then menu order.
    """
    _settings_cap, joint_cap, value_cap = _clamp_caps(
        HARD_MAX_SETTINGS, max_joint_knobs, max_values_per_knob)
    keys = [key for key in eligibility_grid if key in baseline]
    full = {key: catalog_values(key, eligibility_grid[key]) for key in keys}
    singles: list[dict] = []
    for key in keys:
        for value in full[key]:
            if not _same(value, baseline.get(key)):
                singles.append({key: value})
    if joint_cap < 2:
        return singles
    capped = {
        key: nearest_values(full[key], baseline.get(key), value_cap)
        for key in keys
    }
    joints: list[dict] = []
    for i, key_a in enumerate(keys):
        for key_b in keys[i + 1:]:
            for value_a in capped[key_a]:
                if _same(value_a, baseline.get(key_a)):
                    continue
                for value_b in capped[key_b]:
                    if _same(value_b, baseline.get(key_b)):
                        continue
                    joints.append({key_a: value_a, key_b: value_b})
    return singles + joints


def build_search_settings(
        baseline: dict,
        eligibility_grid: dict,
        trial: TrialPenaltyConfig | None = None) -> list[dict]:
    """The pre-registered list. Never more than 48, never 3 knobs."""
    cfg = trial or TrialPenaltyConfig()
    cap, joint_cap, value_cap = _clamp_caps(
        cfg.max_settings, cfg.max_joint_knobs, cfg.max_values_per_knob)
    return iter_search_settings(
        baseline, eligibility_grid,
        max_joint_knobs=joint_cap,
        max_values_per_knob=value_cap,
    )[:cap]


def _axes(baseline: dict, eligibility_grid: dict) -> tuple[list[str], list[tuple]]:
    keys = [key for key in eligibility_grid if key in baseline]
    axes = []
    for key in keys:
        menu = list(catalog_values(key, eligibility_grid[key]))
        if not any(_same(baseline.get(key), v) for v in menu):
            menu.append(baseline[key])
        menu.sort(key=lambda v: float(v))
        axes.append(tuple(menu))
    return keys, axes


def _coords_of(knobs: dict, keys: list[str], baseline: dict) -> tuple:
    coords = []
    for key in keys:
        if key in knobs:
            coords.append(knobs[key])
        else:
            coords.append(baseline[key])
    return tuple(coords)


def _index_tuple(coords, axes: list[tuple]) -> tuple:
    indexes = []
    for value, axis in zip(coords, axes):
        idx = _index_of(axis, value)
        indexes.append(-1 if idx is None else idx)
    return tuple(indexes)


def _mean(values) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def within_plateau_band(neighbour_delta: float, chosen_delta: float,
                        band: float) -> bool:
    """Neighbour Δ > 0 and within ``band`` of the chosen Δ.

    ``band`` 0.50 means ``|neighbour − chosen| ≤ 0.50 × |chosen|``.
    A non-positive chosen Δ has no band.
    """
    if not math.isfinite(neighbour_delta) or not math.isfinite(chosen_delta):
        return False
    if chosen_delta <= 0.0 or neighbour_delta <= 0.0:
        return False
    if band <= 0.0:
        return False
    return abs(neighbour_delta - chosen_delta) <= band * abs(chosen_delta) + 1e-9


def immediate_neighbours(point: GridPoint, evaluated: dict,
                         axes: list[tuple]) -> list[GridPoint]:
    """Evaluated points one catalog step away on exactly one knob.

    A step that was not evaluated (and is not the baseline) is not a
    neighbour. The baseline is in ``evaluated`` with mean Δ 0, so a
    point sitting next to the baseline has that neighbour, and Δ 0
    does not count as agreement.
    """
    found: list[GridPoint] = []
    for dim, axis in enumerate(axes):
        idx = point.index[dim]
        if idx < 0:
            continue
        for step in (-1, 1):
            nxt = idx + step
            if nxt < 0 or nxt >= len(axis):
                continue
            coords = list(point.coords)
            coords[dim] = axis[nxt]
            other = evaluated.get(_coord_key(coords))
            if other is not None and other is not point:
                found.append(other)
    return found


def on_plateau(point: GridPoint, neighbours: list[GridPoint],
               band: float) -> bool:
    """Beat the baseline, and enough neighbours agree.

    Zero neighbours is not agreement. One neighbour must itself agree.
    Two or more require at least two that agree.
    """
    if point.is_baseline or point.mean_delta <= 0.0:
        return False
    n = len(neighbours)
    if n == 0:
        return False
    good = sum(
        1 for nb in neighbours
        if within_plateau_band(nb.mean_delta, point.mean_delta, band)
    )
    need = n if n < 2 else 2
    return good >= need


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def plateau_centre(members: list[GridPoint],
                   baseline_index: tuple) -> GridPoint | None:
    """The member closest to the median coordinate, not the max Δ.

    Ties break toward the baseline (smaller change), then by index.
    Mean Δ is not in the ordering.
    """
    if not members:
        return None
    width = len(members[0].index)
    median = tuple(
        _median([float(member.index[dim]) for member in members])
        for dim in range(width)
    )

    def _key(member: GridPoint):
        dist_med = sum(
            abs(float(a) - b) for a, b in zip(member.index, median))
        dist_base = sum(
            abs(float(a) - float(b))
            for a, b in zip(member.index, baseline_index))
        return (dist_med, dist_base, member.index, member.knobs)

    return min(members, key=_key)


def _fmt_knobs(knobs) -> str:
    if isinstance(knobs, dict):
        items = knobs.items()
    else:
        items = knobs
    parts = [f"{key}={value}" for key, value in items]
    return "{" + ", ".join(parts) + "}"


def _point_knobs(point: GridPoint) -> dict:
    return dict(point.knobs)


def select_grid(
        points: list[GridPoint],
        baseline_point: GridPoint,
        evaluated: dict,
        axes: list[tuple],
        trial: TrialPenaltyConfig,
        *,
        preregistered: list[dict]) -> GridOutcome:
    """Score the pre-registered points and pick the plateau centre.

    DSR is computed for every point (same N_eff, that point's Sharpe).
    PBO is one number for the whole fold × configuration matrix.
    """
    band = trial.plateau_band
    reasons: list[str] = [
        f"pre-registered {len(preregistered)} settings "
        f"(cap {min(trial.max_settings, HARD_MAX_SETTINGS)}; "
        f"joint knobs ≤ {min(trial.max_joint_knobs, HARD_MAX_JOINT_KNOBS)}; "
        f"pair values/knob ≤ {min(trial.max_values_per_knob, HARD_MAX_VALUES_PER_KNOB)}; "
        f"plateau band {band:g}; DSR > {trial.dsr_min:g}; "
        f"PBO < {trial.pbo_max:g})",
    ]
    columns = [list(point.fold_pnls) for point in points]
    n_trials = len(points)
    rho = mean_pairwise_correlation(columns) if n_trials else 0.0
    n_eff = effective_trial_count(n_trials, rho)
    sharpes = [sharpe_ratio(col) for col in columns]
    sharpe_std = trial_sharpe_std(
        [s for s in sharpes if s is not None])
    dsrs: list[float | None] = []
    for point in points:
        dsrs.append(score_configuration(
            list(point.fold_pnls), n_eff=n_eff, sharpe_std=sharpe_std))
    matrix = [list(row) for row in zip(*columns)] if columns else []
    pbo, partitions = probability_of_backtest_overfitting(matrix)
    reasons.append(
        f"trials N={n_trials} ρ̄={rho:.4f} N_eff={n_eff:.4f} "
        f"SR dispersion={sharpe_std:.4f}")
    if pbo is None:
        folds = len(matrix)
        reasons.append(
            f"PBO not computed: {folds} folds, need ≥ 8 for CSCV "
            f"S=8 or 10 — not a pass")
    else:
        pbo_ok = pbo < trial.pbo_max
        reasons.append(
            f"PBO {pbo:.4f} < {trial.pbo_max:g} "
            f"(CSCV S={partitions}): {'✓' if pbo_ok else '✗'}")

    members: list[GridPoint] = []
    for point, dsr in zip(points, dsrs):
        neighbours = immediate_neighbours(point, evaluated, axes)
        plateau = on_plateau(point, neighbours, band)
        if plateau:
            members.append(point)
        dsr_txt = "n/a" if dsr is None else f"{dsr:.4f}"
        reasons.append(
            f"setting {_fmt_knobs(point.knobs)} meanΔ={point.mean_delta:.4f} "
            f"DSR={dsr_txt} neighbours={len(neighbours)} "
            f"plateau={'yes' if plateau else 'no'}")

    centre = plateau_centre(members, baseline_point.index)
    if centre is None:
        reasons.append(
            "DECISION: no plateau — nothing selected "
            "(centre is not the maximum, and there is no LLM pick)")
        return GridOutcome(
            None, tuple(reasons), tuple(preregistered), None,
            "Grid selected nothing. No plateau.")
    centre_dsr = dsrs[points.index(centre)]
    dsr_ok = (
        centre_dsr is not None
        and math.isfinite(centre_dsr)
        and centre_dsr > trial.dsr_min
    )
    pbo_ok = pbo is not None and pbo < trial.pbo_max
    reasons.append(
        f"plateau centre {_fmt_knobs(centre.knobs)} "
        f"(not the max Δ) DSR "
        f"{'n/a' if centre_dsr is None else f'{centre_dsr:.4f}'} "
        f"> {trial.dsr_min:g}: {'✓' if dsr_ok else '✗'}")
    if not (dsr_ok and pbo_ok):
        reasons.append(
            "DECISION: plateau centre did not clear DSR and PBO — "
            "nothing selected, no fallback to the maximum")
        return GridOutcome(
            None, tuple(reasons), tuple(preregistered), None,
            "Grid selected nothing. Plateau centre missed DSR or PBO.")
    selected = _point_knobs(centre)
    evidence = TrialEvidence(
        dsr=centre_dsr,
        pbo=pbo,
        n_trials=n_trials,
        n_eff=n_eff,
        rho_bar=rho,
        partitions=partitions,
    )
    reasons.append(
        f"DECISION: selected {_fmt_knobs(selected)} "
        f"DSR={centre_dsr:.4f} PBO={pbo:.4f} N_eff={n_eff:.4f}")
    rationale = (
        "Grid selection (the model did not pick). "
        f"Selected knobs: {selected}. "
        f"DSR={centre_dsr:.4f} PBO={pbo:.4f} N={n_trials} N_eff={n_eff:.4f}."
    )
    return GridOutcome(
        selected, tuple(reasons), tuple(preregistered), evidence, rationale)


def explanation_prompt(selected: dict, reasons) -> str:
    """Ask the model to explain a setting it is not allowed to change."""
    body = "\n".join(str(line) for line in reasons)
    return (
        f"{_EXPLAIN_ONLY}\n"
        f"Selected knobs: {selected}\n\n"
        f"Evidence:\n{body}\n"
    )


def grid_search_applies(strategy_cls, source: str, evo) -> bool:
    """True when this class should be searched by the grid, not the model.

    ``evolution.search: llm_one`` keeps the old one-change plan. Any
    other config, including a missing key, uses the grid when the class
    has at least two rewritable grid knobs. Non-grid classes stay on
    the plan path. This does not look at results.
    """
    if strategy_cls is None or not source:
        return False
    cfg = evolution_gate_config_from_mapping(evo if isinstance(evo, dict) else {})
    if cfg.search != SEARCH_GRID_BATCH:
        return False
    name = getattr(strategy_cls, "__name__", "") or ""
    master = knob_grid_for(name)
    if not master:
        return False
    _effective, reason = class_knob_eligibility(strategy_cls, master, source)
    return reason == ""


def _fold_sum(trades, start: str, end: str) -> float:
    total = 0
    for trade in trades or []:
        exit_dt = getattr(trade, "exit_dt", "") or ""
        if exit_dt and start <= exit_dt < end:
            total += int(getattr(trade, "pnl", 0) or 0)
    return float(total)


def _baseline_bounds(base_trades, fold_days: int, max_folds: int):
    from .pipeline import analyze_folds
    folds = analyze_folds(
        base_trades, [], fold_days=fold_days, max_folds=max_folds)
    return [(fold.start, fold.end) for fold in folds]


def run_grid_on_bars(
        *,
        strategy_cls,
        source: str,
        bars,
        point_value: int,
        capital_base: int,
        evo,
        fold_days: int = 14,
        max_folds: int = 10) -> GridOutcome:
    """Backtest the pre-registered grid on ``bars`` and select.

    The setting list is fixed before the first candidate backtest.
    A setting that fails to load is logged and is not a trial. Fold
    windows come from the baseline only, so every column shares rows.
    """
    from src.ai.code_sandbox import load_strategy_from_source

    from .pipeline import run_ab_backtest

    cfg = evolution_gate_config_from_mapping(evo if isinstance(evo, dict) else {})
    name = getattr(strategy_cls, "__name__", "") or ""
    master = knob_grid_for(name) or {}
    effective, reason = class_knob_eligibility(strategy_cls, master, source or "")
    if reason or len(effective) < 2:
        text = reason or f"{name} is not on the Saturday knob grid"
        return GridOutcome(None, (text,), (), None, text)
    try:
        baseline = read_strategy_knobs(strategy_cls, effective)
    except Exception as exc:
        text = f"could not read baseline knobs ({type(exc).__name__}: {exc})"
        return GridOutcome(None, (text,), (), None, text)
    settings = build_search_settings(baseline, effective, cfg.trial)
    keys, axes = _axes(baseline, effective)
    reasons_head = (
        f"pre-registered settings before evaluation: {len(settings)}",
    )
    if not bars:
        return GridOutcome(
            None, reasons_head + ("no validation bars — nothing selected",),
            tuple(settings), None, "Grid selected nothing. No bars.")
    base_ab = run_ab_backtest(
        strategy_cls, bars, point_value, "基準 baseline",
        capital_base=capital_base)
    if base_ab.error or base_ab.result is None:
        text = f"baseline backtest failed: {base_ab.error or 'no result'}"
        return GridOutcome(
            None, reasons_head + (text,), tuple(settings), None, text)
    base_trades = list(base_ab.result.trades or [])
    bounds = _baseline_bounds(base_trades, int(fold_days), int(max_folds))
    if len(bounds) < 2:
        return GridOutcome(
            None,
            reasons_head + (
                f"only {len(bounds)} baseline folds — nothing selected",),
            tuple(settings), None,
            "Grid selected nothing. Not enough folds.")
    base_pnls = tuple(_fold_sum(base_trades, start, end) for start, end in bounds)
    base_coords = _coords_of({}, keys, baseline)
    baseline_point = GridPoint(
        knobs=(),
        coords=base_coords,
        index=_index_tuple(base_coords, axes),
        fold_pnls=base_pnls,
        mean_delta=0.0,
        is_baseline=True,
    )
    evaluated = {_coord_key(base_coords): baseline_point}
    points: list[GridPoint] = []
    skipped: list[str] = []
    for i, knobs in enumerate(settings):
        try:
            code = rewrite_knob_source(source, name, f"{name}Grid{i}", knobs)
            candidate_cls = load_strategy_from_source(code)
            ab = run_ab_backtest(
                candidate_cls, bars, point_value, f"grid {i}",
                capital_base=capital_base)
            if ab.error or ab.result is None:
                skipped.append(
                    f"setting {_fmt_knobs(knobs)} not evaluated: "
                    f"{ab.error or 'no result'}")
                continue
            trades = list(ab.result.trades or [])
            pnls = tuple(_fold_sum(trades, start, end) for start, end in bounds)
            deltas = tuple(c - b for c, b in zip(pnls, base_pnls))
            coords = _coords_of(knobs, keys, baseline)
            point = GridPoint(
                knobs=tuple(knobs.items()),
                coords=coords,
                index=_index_tuple(coords, axes),
                fold_pnls=pnls,
                mean_delta=_mean(deltas),
                is_baseline=False,
            )
            evaluated[_coord_key(coords)] = point
            points.append(point)
        except Exception as exc:
            skipped.append(
                f"setting {_fmt_knobs(knobs)} not evaluated: "
                f"{type(exc).__name__}: {exc}")
    outcome = select_grid(
        points, baseline_point, evaluated, axes, cfg.trial,
        preregistered=settings)
    if skipped:
        outcome = GridOutcome(
            outcome.selected,
            tuple(skipped) + outcome.reasons,
            outcome.settings,
            outcome.evidence,
            outcome.rationale,
        )
    return outcome
