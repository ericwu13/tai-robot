"""Saturday allowlisted knob mode (issue #153).

Weekend auto-evolution must not codegen an off-grid stretch such as
EMA 50→80. DynamicExitPullback* may change at most two knobs, and only
to the discrete values below. Structural edits escalate; they are not
applied on the Saturday path.

Issue #164: the class name is only a candidate. Knob mode applies when
the class instantiates with no arguments, exposes every grid knob, and
each knob has a literal ``kwargs.get`` default that
``rewrite_knob_source`` can replace. An ineligible class is recorded
once as ``not applicable``, keyed on class name plus source hash, and
the watermark advances (option iii). The same key is not retried or
re-alerted; a new hash is re-evaluated. There is no codegen fallback.
Off-grid proposals on an eligible class still stop before the watermark.
The substring grid match stays; an explicit class opt-in is a follow-up.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
from datetime import datetime, timedelta, timezone

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


def knob_mode_ineligible(strategy_cls, grid, source) -> str:
    """``""`` when Saturday knob mode can read and rewrite ``strategy_cls``.

    Otherwise a reason: no-arg instantiation failed, a grid knob is
    missing on the instance, reading a grid knob raised (anything other
    than ``AttributeError``), or a grid knob has no literal
    ``kwargs.get("key", <num>)`` default.
    """
    name = getattr(strategy_cls, "__name__", None) or str(strategy_cls)
    try:
        inst = strategy_cls()
    except Exception as e:
        return (f"knob mode not applicable: {name} no-arg instantiation "
                f"failed ({type(e).__name__}: {e}); skipping knob mode")
    missing = []
    for key in grid:
        try:
            getattr(inst, key)
        except AttributeError:
            missing.append(key)
        except Exception as e:
            # Outside the instantiation try on purpose: a property that
            # raises RuntimeError must become a refusal, not an EVO ERROR
            # that escapes saturday_knob_decision (#164).
            return (f"knob mode not applicable: {name} knob {key} "
                    f"raised {type(e).__name__}")
    if missing:
        return (f"knob mode not applicable: {name} lacks knobs "
                f"{_knob_list(missing)}; skipping knob mode")
    nonliteral = [
        key for key in grid
        if not _has_literal_knob_default(source, key)
    ]
    if nonliteral:
        return (f"knob mode not applicable: {name} lacks a literal "
                f"kwargs.get default for knobs {_knob_list(nonliteral)}; "
                f"skipping knob mode")
    return ""


def saturday_knob_directives_if_eligible(strategy_cls, source: str) -> str:
    """MANDATORY knob block, or ``""`` when the class is not knob-eligible.

    The Saturday prompt must not demand a grid the class cannot satisfy
    (issue #164).
    """
    if strategy_cls is None:
        return ""
    grid = knob_grid_for(getattr(strategy_cls, "__name__", None))
    if not grid:
        return ""
    if knob_mode_ineligible(strategy_cls, grid, source or ""):
        return ""
    return saturday_knob_directives()


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
    A grid class that cannot be instantiated, is missing a grid knob, or
    has no literal ``kwargs.get`` default returns ``(reason, None)``.
    The caller records that once and advances the watermark (issue #164,
    option iii: no codegen fallback, no weekly retry of the same source).
    A valid grid proposal returns ``("", {knob: value})``.
    A refused Saturday plan on an eligible class returns ``(reason, None)``
    and the caller stops before the watermark.

    ``source`` is the strategy text ``rewrite_knob_source`` would edit.
    ``None`` falls back to ``inspect.getsource`` so existing callers keep
    working; pass ``""`` when the source was looked up and is missing.
    """
    grid = knob_grid_for(class_name)
    if grid is None:
        return "", None
    if source is None:
        source = _class_source(strategy_cls)
    # Before read_strategy_knobs: a missing knob used to raise KeyError
    # and the cryptic refusal repeated every Saturday (#164).
    reason = knob_mode_ineligible(strategy_cls, grid, source)
    if reason:
        return reason, None
    try:
        baseline = read_strategy_knobs(strategy_cls, grid)
    except Exception as e:
        return (f"could not read baseline knobs "
                f"({type(e).__name__}: {e}) — {_ESCALATE}", None)
    reason = knob_proposal_rejection(proposal, baseline, grid)
    if reason:
        return reason, None
    return "", changed_knobs(proposal, baseline, grid)


def is_knob_not_applicable_reason(reason: str | None) -> bool:
    """True for an eligibility refusal, not an off-grid proposal refusal."""
    return (reason or "").startswith("knob mode not applicable:")


def knob_applicability_key(class_name: str, source: str) -> dict[str, str]:
    """Cache key: class name plus the sha256 of the strategy source."""
    digest = hashlib.sha256((source or "").encode("utf-8")).hexdigest()
    return {"class_name": class_name or "", "source_hash": digest}


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
                         source: str) -> bool:
    if not record:
        return False
    key = knob_applicability_key(class_name, source)
    return (record.get("class_name") == key["class_name"]
            and record.get("source_hash") == key["source_hash"])


def save_knob_not_applicable(bot_dir, class_name: str, source: str,
                             reason: str, at: str) -> dict:
    """Write the one not-applicable outcome for this class and source."""
    os.makedirs(os.fspath(bot_dir), exist_ok=True)
    key = knob_applicability_key(class_name, source)
    payload = {
        "outcome": KNOB_NOT_APPLICABLE,
        "class_name": key["class_name"],
        "source_hash": key["source_hash"],
        "reason": reason,
        "at": at,
    }
    path = knob_not_applicable_path(bot_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    return payload


def settle_knob_ineligibility(bot_dir, strategy_cls, source: str,
                              trade_count: int, at: str | None = None):
    """``(action, reason)`` for one Saturday look at a live class.

    ``action`` is ``not_grid`` (codegen stays available), ``eligible``
    (knob mode proceeds; nothing is written), ``skip`` (this class and
    source hash were already recorded — caller returns with no alert),
    or ``recorded`` (first time, or a new hash: outcome stored, watermark
    advanced, caller alerts once). No codegen fallback.
    """
    if strategy_cls is None:
        return "not_grid", ""
    name = getattr(strategy_cls, "__name__", "") or ""
    grid = knob_grid_for(name)
    if not grid:
        return "not_grid", ""
    reason = knob_mode_ineligible(strategy_cls, grid, source or "")
    if not reason:
        return "eligible", ""
    source = source or ""
    if bot_dir and _same_not_applicable(
            load_knob_not_applicable(bot_dir), name, source):
        return "skip", ""
    if at is None:
        at = datetime.now(_TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    if bot_dir:
        save_knob_not_applicable(bot_dir, name, source, reason, at)
        from src.evolution.notify import save_watermark
        save_watermark(bot_dir, int(trade_count), at=at)
    return "recorded", reason


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
