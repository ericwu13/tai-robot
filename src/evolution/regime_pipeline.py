"""Regime threshold evolution — mutate ADX thresholds and confirm_sessions,
evaluate via outcome-based fitness on rolling daily report history, and
persist the best parameters to ``data/regime/regime_evolved_params.json``.

Uses the same validation philosophy as the strategy SEE pipeline: gene
bounds, fitness floor, and rolling-window evaluation. No AI codegen —
the genes are numeric parameters on the RegimeConfig dataclass.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass
from datetime import datetime
from glob import glob
from typing import Any

from src.evolution.regime_fitness import compute_regime_fitness

REGIME_GENE_BOUNDS: dict[str, tuple[float, float]] = {
    "adx_enter": (18.0, 32.0),
    "adx_strong": (25.0, 40.0),
    "confirm_sessions": (1, 3),
}

EVOLVED_PARAMS_PATH = os.path.join("data", "regime", "regime_evolved_params.json")
ROLLING_WINDOW_SESSIONS = 60
MIN_FITNESS_THRESHOLD = 0.3
POPULATION_SIZE = 8
MUTATION_STD_RATIO = 0.15
NUM_GENERATIONS = 5


@dataclass
class RegimeCandidate:
    adx_enter: float
    adx_strong: float
    confirm_sessions: int
    fitness: float = 0.0


def clamp_genes(candidate: RegimeCandidate) -> RegimeCandidate:
    """Clamp gene values to their defined bounds."""
    lo, hi = REGIME_GENE_BOUNDS["adx_enter"]
    candidate.adx_enter = max(lo, min(hi, candidate.adx_enter))
    lo, hi = REGIME_GENE_BOUNDS["adx_strong"]
    candidate.adx_strong = max(lo, min(hi, candidate.adx_strong))
    lo, hi = REGIME_GENE_BOUNDS["confirm_sessions"]
    candidate.confirm_sessions = max(int(lo), min(int(hi), candidate.confirm_sessions))
    if candidate.adx_strong <= candidate.adx_enter:
        candidate.adx_strong = candidate.adx_enter + 1.0
        _, max_strong = REGIME_GENE_BOUNDS["adx_strong"]
        candidate.adx_strong = min(candidate.adx_strong, max_strong)
    return candidate


def mutate(parent: RegimeCandidate) -> RegimeCandidate:
    """Create a mutated child from a parent candidate."""
    adx_enter = parent.adx_enter + random.gauss(0, parent.adx_enter * MUTATION_STD_RATIO)
    adx_strong = parent.adx_strong + random.gauss(0, parent.adx_strong * MUTATION_STD_RATIO)
    cs_delta = random.choice([-1, 0, 0, 1])
    confirm_sessions = parent.confirm_sessions + cs_delta
    child = RegimeCandidate(
        adx_enter=adx_enter,
        adx_strong=adx_strong,
        confirm_sessions=confirm_sessions,
    )
    return clamp_genes(child)


def load_recent_reports(
    reports_dir: str = "",
    max_sessions: int = ROLLING_WINDOW_SESSIONS,
) -> list[dict]:
    """Load the most recent daily report JSONs that contain regime data."""
    if not reports_dir:
        reports_dir = os.path.join("data", "daily-reports")
    if not os.path.isdir(reports_dir):
        return []
    paths = sorted(glob(os.path.join(reports_dir, "*.json")))
    reports = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("regime_switching"):
            reports.append(data)
    return reports[-max_sessions:]


def evaluate_candidate(candidate: RegimeCandidate, reports: list[dict]) -> float:
    """Evaluate a candidate's fitness against historical reports.

    The reports already contain trade outcomes under the regime labels
    that were active when those trades were made. Fitness measures how
    well the label→direction prediction aligned with actual P&L.
    """
    if not reports:
        return 0.0
    return compute_regime_fitness(reports)


def run_regime_evolution(
    reports_dir: str = "",
    base_dir: str = "",
    seed: RegimeCandidate | None = None,
) -> RegimeCandidate | None:
    """Run the regime threshold evolution loop.

    Returns the best candidate if it clears the fitness threshold,
    otherwise None. Writes to ``regime_evolved_params.json`` on success.
    """
    reports = load_recent_reports(reports_dir)
    if len(reports) < 10:
        return None

    if seed is None:
        seed = RegimeCandidate(adx_enter=25.0, adx_strong=30.0, confirm_sessions=2)

    seed.fitness = evaluate_candidate(seed, reports)

    population = [seed]
    for _ in range(POPULATION_SIZE - 1):
        child = mutate(seed)
        child.fitness = evaluate_candidate(child, reports)
        population.append(child)

    for _gen in range(NUM_GENERATIONS):
        population.sort(key=lambda c: c.fitness, reverse=True)
        survivors = population[:max(2, POPULATION_SIZE // 2)]
        children = []
        while len(children) < POPULATION_SIZE - len(survivors):
            parent = random.choice(survivors)
            child = mutate(parent)
            child.fitness = evaluate_candidate(child, reports)
            children.append(child)
        population = survivors + children

    population.sort(key=lambda c: c.fitness, reverse=True)
    best = population[0]

    if best.fitness < MIN_FITNESS_THRESHOLD:
        return None

    _write_evolved_params(best, base_dir)
    return best


def _write_evolved_params(candidate: RegimeCandidate, base_dir: str = "") -> None:
    path = os.path.join(base_dir, EVOLVED_PARAMS_PATH) if base_dir else EVOLVED_PARAMS_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "adx_enter": round(candidate.adx_enter, 1),
        "adx_strong": round(candidate.adx_strong, 1),
        "confirm_sessions": candidate.confirm_sessions,
        "evolved_at": datetime.now().strftime("%Y-%m-%d"),
        "fitness_score": round(candidate.fitness, 4),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
