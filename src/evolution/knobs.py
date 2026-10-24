"""Saturday allowlisted knob mode (issue #153, #164, D1).

Weekend auto-evolution must not codegen an off-grid stretch such as
EMA 50→80. DynamicExitPullback* may change at most two knobs, and only
to the discrete values below. Structural edits escalate; they are not
applied on the Saturday path.

Issue #164: the class name is only a candidate. Knob mode applies when
the class instantiates with no arguments and each knob it rewrites has
a literal ``kwargs.get`` default. An ineligible class is recorded as
``not applicable`` and the watermark advances (option iii).

D1: the Saturday grid is per class. It is the intersection of the master
grid with the knobs that class exposes as literal ``kwargs.get``
defaults, and the class needs at least ``MIN_CLASS_KNOBS`` (2) of them.
A class that exposes ``ema_period``, ``atr_period``, and
``atr_stop_mult`` (and not ``atr_buffer_mult``) gets that 3-knob grid.
A class with fewer than 2 rewritable grid knobs is excluded from weekly
codegen.

The not-applicable cache key is class name + source hash + grid hash.
The same key does not re-record or move the watermark, but every
Saturday still emits ``excluded: <class> — <reason>; fallback=<mode>``.
A new source hash or a new grid hash is re-evaluated, so widening the
grid cannot leave a class stale-excluded. Eligibility is computed
before the cache is consulted.

``evolution.knob_ineligible_fallback`` is ``none`` (default) or
``structural_lane``. Neither value selects the weekly codegen path.
The structural lane itself is a later change; this module only records
the configured mode on the exclusion line. Off-grid proposals on an
eligible class still stop before the watermark. The substring grid
match stays; an explicit class opt-in is a follow-up.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

MAX_AUTO_KNOBS = 2
# Intersection of the master grid with the knobs a class can rewrite.
# Fewer than this and the class is excluded from weekly codegen (D1).
MIN_CLASS_KNOBS = 2

# Weekly codegen stays off. ``structural_lane`` is recorded on the
# exclusion line for a later monthly lane; it is not a Saturday path.
FALLBACK_NONE = "none"
FALLBACK_STRUCTURAL_LANE = "structural_lane"
KNOB_INELIGIBLE_FALLBACK_DEFAULT = FALLBACK_NONE

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

# Option (iii): one persisted outcome per (class name, source hash).
KNOB_NOT_APPLICABLE = "not applicable"
KNOB_NOT_APPLICABLE_FILENAME = "knob_not_applicable.json"
_TZ_TAIPEI = timezone(timedelta(hours=8))


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


def _literal_kwarg_default_re(key: str) -> str:
    """The ``kwargs.get("key", <num>)`` shape ``rewrite_knob_source`` replaces."""
    return (
        rf'(kwargs\.get\(\s*["\']{re.escape(key)}["\']\s*,\s*)'
        rf'([0-9]+(?:\.[0-9]+)?)(\s*\))'
    )


def _has_literal_knob_default(source: str, key: str) -> bool:
    return re.search(_literal_kwarg_default_re(key), source or "") is not None


def _class_source(strategy_cls) -> str:
    try:
        return inspect.getsource(strategy_cls)
    except (OSError, TypeError):
        return ""


def _knob_list(keys) -> str:
    return "[" + ", ".join(keys) + "]"


def normalize_knob_ineligible_fallback(value) -> str:
    """``structural_lane`` or ``none``.

    Any other spelling, including the retired ``codegen`` value, is
    ``none``. This setting never selects weekly Saturday codegen.
    """
    text = str(value or "").strip().lower()
    if text == FALLBACK_STRUCTURAL_LANE:
        return FALLBACK_STRUCTURAL_LANE
    return FALLBACK_NONE


def format_knob_exclusion_line(class_name: str, reason: str,
                               fallback: str | None = None) -> str:
    """One Saturday line for a class excluded from weekly evolution."""
    mode = normalize_knob_ineligible_fallback(fallback)
    return f"excluded: {class_name} — {reason}; fallback={mode}"


def knob_grid_hash(grid: dict | None) -> str:
    """Stable id of a grid's keys and discrete values, in grid order."""
    items = []
    for key, values in (grid or {}).items():
        items.append([
            str(key),
            [render_knob_default(str(key), v) for v in values],
        ])
    blob = json.dumps(items, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _few_knobs_reason(name: str, rewritable: list[str],
                      not_rewritable: list[str]) -> str:
    n = len(rewritable)
    noun = "knob" if n == 1 else "knobs"
    text = (f"knob mode not applicable: {name} has {n} "
            f"rewritable grid {noun} {_knob_list(rewritable)} "
            f"(need >= {MIN_CLASS_KNOBS})")
    if not_rewritable:
        text += f"; not rewritable {_knob_list(not_rewritable)}"
    return text + "; skipping knob mode"


def class_knob_eligibility(strategy_cls, grid, source) -> tuple[dict, str]:
    """``(effective_grid, reason)`` for one class against ``grid``.

    ``reason == ""`` when the class exposes at least ``MIN_CLASS_KNOBS``
    rewritable grid knobs. ``effective_grid`` is those knobs, in master
    grid order, with the master value tuples. A missing attribute is
    not in the intersection. A knob with no literal ``kwargs.get``
    default is not rewritable. No-arg instantiation failure, or a knob
    whose getter raises, excludes the class (the refusal must not escape
    ``saturday_knob_decision``).
    """
    name = getattr(strategy_cls, "__name__", None) or str(strategy_cls)
    try:
        inst = strategy_cls()
    except Exception as e:
        return {}, (f"knob mode not applicable: {name} no-arg instantiation "
                    f"failed ({type(e).__name__}: {e}); skipping knob mode")
    exposed: list[str] = []
    for key in grid:
        try:
            getattr(inst, key)
        except AttributeError:
            continue
        except Exception as e:
            # Outside the instantiation try on purpose: a property that
            # raises RuntimeError must become a refusal, not an EVO ERROR
            # that escapes saturday_knob_decision (#164).
            return {}, (f"knob mode not applicable: {name} knob {key} "
                        f"raised {type(e).__name__}; skipping knob mode")
        exposed.append(key)
    text = source or ""
    rewritable = [key for key in exposed if _has_literal_knob_default(text, key)]
    not_rewritable = [key for key in exposed if key not in rewritable]
    effective = {key: grid[key] for key in rewritable}
    if len(rewritable) >= MIN_CLASS_KNOBS:
        return effective, ""
    return effective, _few_knobs_reason(name, rewritable, not_rewritable)


def knob_mode_ineligible(strategy_cls, grid, source) -> str:
    """``""`` when Saturday knob mode can read and rewrite ``strategy_cls``.

    Otherwise a reason: no-arg instantiation failed, a grid knob's getter
    raised (anything other than ``AttributeError``), or the class has
    fewer than ``MIN_CLASS_KNOBS`` literal ``kwargs.get`` grid knobs.
    """
    _grid, reason = class_knob_eligibility(strategy_cls, grid, source)
    return reason


def saturday_knob_directives_if_eligible(strategy_cls, source: str) -> str:
    """MANDATORY knob block for this class's grid, or ``""``.

    The Saturday prompt lists only the knobs the class can rewrite
    (D1). A class with fewer than ``MIN_CLASS_KNOBS`` gets no block.
    """
    if strategy_cls is None:
        return ""
    grid = knob_grid_for(getattr(strategy_cls, "__name__", None))
    if not grid:
        return ""
    effective, reason = class_knob_eligibility(
        strategy_cls, grid, source or "")
    if reason or len(effective) < MIN_CLASS_KNOBS:
        return ""
    return saturday_knob_directives(effective)


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


def saturday_knob_decision(class_name, proposal, strategy_cls,
                           source: str | None = None):
    """``(rejection, knobs_to_apply)``.

    Non-grid strategies return ``("", None)`` so the caller keeps codegen.
    A grid class that cannot be instantiated, whose knob getter raises,
    or that has fewer than ``MIN_CLASS_KNOBS`` rewritable grid knobs
    returns ``(reason, None)``. The caller excludes it from weekly
    codegen (D1; no Saturday codegen fallback). A valid proposal on the
    class's own grid returns ``("", {knob: value})``. A refused Saturday
    plan on an eligible class returns ``(reason, None)`` and the caller
    stops before the watermark.

    ``source`` is the strategy text ``rewrite_knob_source`` would edit.
    ``None`` falls back to ``inspect.getsource`` so existing callers keep
    working; pass ``""`` when the source was looked up and is missing.
    """
    grid = knob_grid_for(class_name)
    if grid is None:
        return "", None
    if source is None:
        source = _class_source(strategy_cls)
    # Per-class grid: only knobs this class can rewrite. A missing
    # attribute used to KeyError inside read_strategy_knobs (#164).
    effective, reason = class_knob_eligibility(strategy_cls, grid, source)
    if reason:
        return reason, None
    try:
        baseline = read_strategy_knobs(strategy_cls, effective)
    except Exception as e:
        return (f"could not read baseline knobs "
                f"({type(e).__name__}: {e}) — {_ESCALATE}", None)
    reason = knob_proposal_rejection(proposal, baseline, effective)
    if reason:
        return reason, None
    return "", changed_knobs(proposal, baseline, effective)


def is_knob_not_applicable_reason(reason: str | None) -> bool:
    """True for an eligibility refusal, not an off-grid proposal refusal."""
    return (reason or "").startswith("knob mode not applicable:")


def knob_applicability_key(class_name: str, source: str,
                           grid: dict | None = None) -> dict[str, str]:
    """Cache key: class name, source sha256, and the grid hash.

    ``grid`` defaults to the master grid for ``class_name``. A change
    to that grid must not reuse a not-applicable record (D1).
    """
    if grid is None:
        grid = knob_grid_for(class_name) or {}
    digest = hashlib.sha256((source or "").encode("utf-8")).hexdigest()
    return {
        "class_name": class_name or "",
        "source_hash": digest,
        "grid_hash": knob_grid_hash(grid),
    }


def knob_not_applicable_path(bot_dir) -> str:
    return os.path.join(os.fspath(bot_dir), KNOB_NOT_APPLICABLE_FILENAME)


def load_knob_not_applicable(bot_dir) -> dict | None:
    """The persisted not-applicable record, or None if absent or unreadable."""
    if not bot_dir:
        return None
    path = knob_not_applicable_path(bot_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("outcome") != KNOB_NOT_APPLICABLE:
        return None
    return data


def _same_not_applicable(record: dict | None, class_name: str,
                         source: str, grid: dict | None) -> bool:
    if not record:
        return False
    key = knob_applicability_key(class_name, source, grid)
    return (record.get("class_name") == key["class_name"]
            and record.get("source_hash") == key["source_hash"]
            and record.get("grid_hash") == key["grid_hash"])


def save_knob_not_applicable(bot_dir, class_name: str, source: str,
                             reason: str, at: str,
                             grid: dict | None = None,
                             fallback: str | None = None) -> dict:
    """Write the one not-applicable outcome for this class, source, and grid."""
    os.makedirs(os.fspath(bot_dir), exist_ok=True)
    key = knob_applicability_key(class_name, source, grid)
    payload = {
        "outcome": KNOB_NOT_APPLICABLE,
        "class_name": key["class_name"],
        "source_hash": key["source_hash"],
        "grid_hash": key["grid_hash"],
        "reason": reason,
        "fallback": normalize_knob_ineligible_fallback(fallback),
        "at": at,
    }
    path = knob_not_applicable_path(bot_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    return payload


def settle_knob_ineligibility(bot_dir, strategy_cls, source: str,
                              trade_count: int, at: str | None = None,
                              fallback: str | None = None):
    """``(action, line)`` for one Saturday look at a live class.

    ``action`` is ``not_grid`` (codegen stays available), ``eligible``
    (knob mode proceeds; nothing is written), ``skip`` (this class,
    source hash, and grid hash were already recorded — caller does not
    re-record or move the watermark), or ``recorded`` (first time, or a
    new source/grid hash: outcome stored, watermark advanced).

    ``skip`` and ``recorded`` both return the weekly exclusion line and
    log it. There is no weekly codegen fallback. ``fallback`` is only
    the mode written on that line (``none`` or ``structural_lane``).
    """
    mode = normalize_knob_ineligible_fallback(fallback)
    if strategy_cls is None:
        return "not_grid", ""
    name = getattr(strategy_cls, "__name__", "") or ""
    grid = knob_grid_for(name)
    if not grid:
        return "not_grid", ""
    source = source or ""
    # Eligibility first. A stale not-applicable file must not keep a
    # class excluded after its grid (or its exposed knobs) changes.
    _effective, reason = class_knob_eligibility(strategy_cls, grid, source)
    if not reason:
        return "eligible", ""
    same = bool(bot_dir) and _same_not_applicable(
        load_knob_not_applicable(bot_dir), name, source, grid)
    if same:
        stored = load_knob_not_applicable(bot_dir) or {}
        reason = stored.get("reason") or reason
        action = "skip"
    else:
        if at is None:
            at = datetime.now(_TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
        if bot_dir:
            save_knob_not_applicable(
                bot_dir, name, source, reason, at, grid=grid, fallback=mode)
            from src.evolution.notify import save_watermark
            save_watermark(bot_dir, int(trade_count), at=at)
        action = "recorded"
    line = format_knob_exclusion_line(name, reason, mode)
    logger.info(line)
    return action, line


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
        pattern = _literal_kwarg_default_re(key)
        text2, n = re.subn(pattern, rf"\g<1>{rendered}\g<3>", text, count=1)
        if n != 1:
            raise ValueError(f"could not rewrite default for {key}")
        text = text2
    return text


def saturday_knob_directives(grid: dict | None = None) -> str:
    """Prompt trailer for the Saturday plan. No numeric holdout criteria.

    ``grid`` defaults to the full master grid, which keeps the V2 prompt
    unchanged. A per-class grid omits knobs the class cannot rewrite.
    """
    g = DYNAMIC_EXIT_PULLBACK_GRID if grid is None else grid
    if not g:
        return ""

    def _vals(key: str) -> str:
        return ", ".join(render_knob_default(key, v) for v in g[key])

    bullets = []
    for key in g:
        if key == "ema_period":
            bullets.append(
                f"- ema_period: {_vals('ema_period')} "
                "(80 and 90 are out of auto)")
        else:
            bullets.append(f"- {key}: {_vals(key)}")
    if "ema_period" in g:
        example = '{"action": "change", "knobs": {"ema_period": 60}}'
        ema_ban = "Do NOT propose ema_period 80 or 90.\n"
    else:
        example_key = next(iter(g))
        example_val = render_knob_default(
            example_key, g[example_key][-1])
        example = (
            '{"action": "change", "knobs": {'
            f'"{example_key}": {example_val}'
            "}}"
        )
        ema_ban = ""
    return (
        "## Saturday auto knob mode (MANDATORY on this weekly run)\n"
        "Apply an allowlisted discrete grid only. Change at most "
        f"{MAX_AUTO_KNOBS} knobs (prefer exactly one). "
        "Do not invent values between the grid points.\n"
        + "\n".join(bullets) + "\n"
        "Put the change in the directives JSON, for example:\n"
        "```json\n"
        f"{example}\n"
        "```\n"
        "Structural changes are not applied on Saturday: "
        f"{_ESCALATE}.\n"
        f"{ema_ban}\n"
    )
