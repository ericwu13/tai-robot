"""Outcome-based fitness for regime threshold genes.

Evaluates how well a set of regime thresholds (adx_enter, adx_strong,
confirm_sessions) predicted market direction by looking at actual trade
outcomes under each effective regime label in daily report JSONs.
"""

from __future__ import annotations

from typing import Any


def _label_direction(label: str) -> str | None:
    """Map effective_regime label → expected trade direction.

    Returns "long", "short", or None (label doesn't predict direction).
    """
    if label in ("trending-up",):
        return "long"
    if label in ("trending-down",):
        return "short"
    return None


def compute_label_accuracy(reports: list[dict]) -> float:
    """Fraction of sessions where the regime label predicted the right
    trade direction.

    For each report with an ``effective_regime`` label that implies a
    direction (trending-up → long, trending-down → short), check whether
    trades made in that session had positive P&L in the implied direction.
    Range-bound / unknown labels are skipped (no directional prediction).
    """
    correct = 0
    total = 0
    for r in reports:
        regime_block = r.get("regime_switching") or {}
        label = regime_block.get("effective_regime", "")
        direction = _label_direction(label)
        if direction is None:
            continue
        trades = r.get("trades") or []
        if not trades:
            continue
        session_pnl = sum(float(t.get("pnl") or 0) for t in trades)
        total += 1
        if session_pnl > 0:
            correct += 1
    if total == 0:
        return 0.0
    return correct / total


def compute_weighted_pnl(reports: list[dict]) -> float:
    """Sum of P&L made under each directional regime label.

    Only counts sessions where effective_regime implies a direction.
    The "weighting" is inherent: longer-held labels contribute more
    sessions and thus more P&L to the sum.
    """
    total_pnl = 0.0
    for r in reports:
        regime_block = r.get("regime_switching") or {}
        label = regime_block.get("effective_regime", "")
        if _label_direction(label) is None:
            continue
        trades = r.get("trades") or []
        for t in trades:
            total_pnl += float(t.get("pnl") or 0)
    return total_pnl


def compute_regime_fitness(
    reports: list[dict],
    accuracy_weight: float = 0.6,
    pnl_weight: float = 0.4,
    pnl_normalization_cap: float = 100_000.0,
) -> float:
    """Combined regime fitness score in [0, 1].

    ``label_accuracy * accuracy_weight + normalized_weighted_pnl * pnl_weight``

    The P&L component is normalized by capping at ``pnl_normalization_cap``
    and clipping to [0, 1].
    """
    accuracy = compute_label_accuracy(reports)
    raw_pnl = compute_weighted_pnl(reports)

    normalized_pnl = max(0.0, min(1.0, raw_pnl / pnl_normalization_cap)) if pnl_normalization_cap > 0 else 0.0

    return accuracy * accuracy_weight + normalized_pnl * pnl_weight
