"""Saturday allowlisted knob mode (issue #153).

Weekend auto-evolution must not codegen an off-grid stretch such as
EMA 50→80. DynamicExitPullback* may change at most two knobs, and only
to the discrete values below. Structural edits escalate; they are not
applied on the Saturday path.
"""

from __future__ import annotations

import re

MAX_AUTO_KNOBS = 2

# 80 and 90 are intentionally absent — the #153 evo1 failure.
DYNAMIC_EXIT_PULLBACK_GRID: dict[str, tuple] = {
    "ema_period": (40, 50, 60, 70),
    "atr_period": (10, 14, 21),
    "atr_stop_mult": (2.0, 2.5, 3.0),
    "atr_buffer_mult": (0.3, 0.5, 0.8),
}

_EMA_OUT_OF_AUTO = (80, 90)
_ESCALATE = "structural/codegen escalate, not auto"
_FLOAT_KNOBS = ("atr_stop_mult", "atr_buffer_mult")


def knob_grid_for(class_name: str | None) -> dict[str, tuple] | None:
    if class_name and "DynamicExitPullback" in class_name:
        return DYNAMIC_EXIT_PULLBACK_GRID
    return None


def _same(a, b) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


def _in_options(value, options) -> bool:
    return any(_same(value, opt) for opt in options)


def render_knob_default(key: str, value) -> str:
    fv = float(value)
    if key in _FLOAT_KNOBS:
        return f"{fv:.1f}"
    if abs(fv - round(fv)) < 1e-9:
        return str(int(round(fv)))
    return f"{fv:g}"


def read_strategy_knobs(strategy_cls, grid: dict) -> dict:
    inst = strategy_cls()
    out = {}
    for key in grid:
        if not hasattr(inst, key):
            raise KeyError(key)
        out[key] = getattr(inst, key)
    return out


def knob_proposal_rejection(proposal, baseline: dict, grid: dict,
                            max_knobs: int = MAX_AUTO_KNOBS) -> str:
    """Empty string when ``proposal`` is an allowlisted change of at most
    ``max_knobs`` keys. Otherwise the operator-visible refusal.
    """
    if not isinstance(proposal, dict) or not proposal:
        return f"no allowlisted knobs in the plan — {_ESCALATE}"
    changed = 0
    for key, raw in proposal.items():
        if key == "ema_period" and any(_same(raw, x) for x in _EMA_OUT_OF_AUTO):
            return f"ema_period 80/90 is out of auto — {_ESCALATE}"
        if key not in grid:
            return f"knob {key} is not allowlisted — {_ESCALATE}"
        if not _in_options(raw, grid[key]):
            allowed = ", ".join(render_knob_default(key, v) for v in grid[key])
            return (f"{key}={raw} is outside the discrete grid ({allowed}) — "
                    f"{_ESCALATE}")
        base = baseline.get(key)
        if base is None or not _same(raw, base):
            changed += 1
    if changed == 0:
        return "knobs match the baseline — nothing to apply"
    if changed > max_knobs:
        return f"{changed} knobs changed (max {max_knobs}) — {_ESCALATE}"
    return ""


def changed_knobs(proposal, baseline: dict, grid: dict) -> dict:
    """Snapped knob changes, or ``{}`` when the proposal is rejected."""
    if knob_proposal_rejection(proposal, baseline, grid):
        return {}
    out = {}
    for key, raw in proposal.items():
        if key not in grid:
            continue
        base = baseline.get(key)
        if base is None or not _same(raw, base):
            out[key] = next(v for v in grid[key] if _same(v, raw))
    return out


def saturday_knob_decision(class_name, proposal, strategy_cls):
    """``(rejection, knobs_to_apply)``.

    Non-grid strategies return ``("", None)`` so the caller keeps codegen.
    A valid grid proposal returns ``("", {knob: value})``.
    A refused Saturday plan returns ``(reason, None)``.
    """
    grid = knob_grid_for(class_name)
    if grid is None:
        return "", None
    try:
        baseline = read_strategy_knobs(strategy_cls, grid)
    except Exception as e:
        return (f"could not read baseline knobs "
                f"({type(e).__name__}: {e}) — {_ESCALATE}", None)
    reason = knob_proposal_rejection(proposal, baseline, grid)
    if reason:
        return reason, None
    return "", changed_knobs(proposal, baseline, grid)


def iter_knob_candidates(baseline: dict, grid: dict | None = None,
                         max_knobs: int = MAX_AUTO_KNOBS) -> list[dict]:
    """Discrete grid points that differ from ``baseline`` in 1..max_knobs keys.

    80/90 are not in the ema grid, so they are never yielded.
    """
    grid = grid or DYNAMIC_EXIT_PULLBACK_GRID
    options = []
    for key, values in grid.items():
        base = baseline.get(key)
        alts = [v for v in values if not _same(v, base)]
        options.append((key, alts))
    found: list[dict] = []
    for key, alts in options:
        for value in alts:
            found.append({key: value})
    if max_knobs >= 2:
        for i in range(len(options)):
            for j in range(i + 1, len(options)):
                k1, a1 = options[i]
                k2, a2 = options[j]
                for v1 in a1:
                    for v2 in a2:
                        found.append({k1: v1, k2: v2})
    return found


def rewrite_knob_source(source: str, base_name: str, candidate_name: str,
                        knobs: dict) -> str:
    """Rename the class and replace ``kwargs.get`` defaults for ``knobs``."""
    if not knobs:
        raise ValueError("no knobs to apply")
    needle = f"class {base_name}"
    if needle not in source:
        raise ValueError(f"{needle} not in strategy source")
    text = source.replace(needle, f"class {candidate_name}", 1)
    for key, value in knobs.items():
        rendered = render_knob_default(key, value)
        pattern = (
            rf'(kwargs\.get\(\s*["\']{re.escape(key)}["\']\s*,\s*)'
            rf'([0-9]+(?:\.[0-9]+)?)(\s*\))'
        )
        text2, n = re.subn(pattern, rf"\g<1>{rendered}\g<3>", text, count=1)
        if n != 1:
            raise ValueError(f"could not rewrite default for {key}")
        text = text2
    return text


def saturday_knob_directives() -> str:
    """Prompt trailer for the Saturday plan. No numeric holdout criteria."""
    g = DYNAMIC_EXIT_PULLBACK_GRID

    def _vals(key: str) -> str:
        return ", ".join(render_knob_default(key, v) for v in g[key])

    return (
        "## Saturday auto knob mode (MANDATORY on this weekly run)\n"
        "Apply an allowlisted discrete grid only. Change at most "
        f"{MAX_AUTO_KNOBS} knobs (prefer exactly one). "
        "Do not invent values between the grid points.\n"
        f"- ema_period: {_vals('ema_period')} "
        "(80 and 90 are out of auto)\n"
        f"- atr_period: {_vals('atr_period')}\n"
        f"- atr_stop_mult: {_vals('atr_stop_mult')}\n"
        f"- atr_buffer_mult: {_vals('atr_buffer_mult')}\n"
        "Put the change in the directives JSON, for example:\n"
        "```json\n"
        '{"action": "change", "knobs": {"ema_period": 60}}\n'
        "```\n"
        "Structural changes are not applied on Saturday: "
        f"{_ESCALATE}.\n"
        "Do NOT propose ema_period 80 or 90.\n\n"
    )
