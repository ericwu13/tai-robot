"""Parameter snapshots for an evolution PASS changelog entry.

Two sources, chosen by the path that produced the candidate:

* Knob path — the allowlisted defaults read off the baseline instance,
  and those defaults with the applied knob dict merged on top.
* Codegen path — public class-body assignments whose values are
  literals. ``__init__`` signature defaults and ``kwargs.get`` defaults
  are not class-level; they are left out. When a side cannot be parsed,
  that side is ``None`` and the returned reason says why.

Neither path changes a verdict. Extraction never raises: a failure
becomes ``None`` plus a reason so the changelog write still happens.
"""

from __future__ import annotations

import ast
from typing import Any

_MISSING = object()


def evolution_changelog_params(
    *,
    knob_apply: dict | None,
    baseline_cls: type | None,
    baseline_source: str,
    candidate_source: str,
    candidate_name: str,
    baseline_name: str = "",
) -> tuple[dict | None, dict | None, str | None]:
    """``(params_before, params_after, params_reason)``.

    ``params_reason`` is ``None`` when both sides produced a dict.
    A non-empty knob dict selects the knob path. Anything else is the
    codegen path, including a manual run that never entered knob mode.
    """
    try:
        if isinstance(knob_apply, dict) and knob_apply:
            return _knob_params(baseline_cls, knob_apply)
        name = baseline_name or getattr(baseline_cls, "__name__", "") or ""
        return _codegen_params(
            baseline_source or "",
            name,
            candidate_source or "",
            candidate_name or "",
        )
    except Exception as e:
        return None, None, f"unparseable: {type(e).__name__}: {e}"


def _knob_params(
    baseline_cls: type | None,
    knob_apply: dict,
) -> tuple[dict | None, dict | None, str | None]:
    if baseline_cls is None:
        return None, None, "unparseable: knob path has no baseline class"
    from src.evolution.knobs import knob_grid_for, read_strategy_knobs

    class_name = getattr(baseline_cls, "__name__", "") or ""
    grid = knob_grid_for(class_name)
    if not grid:
        return None, None, (
            f"unparseable: no knob grid for {class_name or 'baseline'}")
    try:
        raw = read_strategy_knobs(baseline_cls, grid)
    except Exception as e:
        return None, None, (
            f"unparseable: could not read baseline knobs "
            f"({type(e).__name__}: {e})")
    before = _json_dict(raw)
    if before is None:
        return None, None, "unparseable: baseline knobs are not JSON values"
    applied = _json_dict(knob_apply)
    if applied is None:
        return None, None, "unparseable: applied knobs are not JSON values"
    after = dict(before)
    after.update(applied)
    return before, after, None


def _codegen_params(
    baseline_source: str,
    baseline_name: str,
    candidate_source: str,
    candidate_name: str,
) -> tuple[dict | None, dict | None, str | None]:
    before, why_before = _class_level_literals(baseline_source, baseline_name)
    after, why_after = _class_level_literals(candidate_source, candidate_name)
    reasons = []
    if why_before:
        reasons.append(f"unparseable baseline: {why_before}")
    if why_after:
        reasons.append(f"unparseable candidate: {why_after}")
    return before, after, ("; ".join(reasons) or None)


def _class_level_literals(
    source: str, class_name: str,
) -> tuple[dict | None, str | None]:
    """Public literal assignments in the named class body.

    ``(dict, None)`` when at least one literal was found. ``(None, reason)``
    when the source, the class, or its assignments cannot be read as literals.
    """
    if not class_name:
        return None, "class name missing"
    if not (source or "").strip():
        return None, "no strategy source"
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        detail = e.msg or "invalid syntax"
        return None, f"syntax error: {detail}"
    except Exception as e:
        return None, f"cannot parse: {type(e).__name__}: {e}"

    class_def = _find_class(tree, class_name)
    if class_def is None:
        return None, f"class {class_name} not found"

    literals: dict[str, Any] = {}
    skipped: list[str] = []
    for names, value in _class_assignments(class_def):
        public = [name for name in names if _keep_name(name)]
        if not public:
            continue
        lit = _literal(value)
        if lit is _MISSING:
            skipped.extend(public)
            continue
        for name in public:
            literals[name] = lit
    if literals:
        return literals, None
    if skipped:
        listed = ", ".join(skipped)
        return None, f"class-level assignments are not literals: {listed}"
    return None, "no class-level literal assignments"


def _find_class(tree: ast.AST, class_name: str) -> ast.ClassDef | None:
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _class_assignments(class_def: ast.ClassDef):
    for stmt in class_def.body:
        if isinstance(stmt, ast.Assign):
            names: list[str] = []
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
            if names:
                yield names, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.value is not None:
                yield [stmt.target.id], stmt.value


def _keep_name(name: str) -> bool:
    return bool(name) and not name.startswith("_")


def _literal(node: ast.AST):
    """JSON-safe literal, or ``_MISSING`` when the node is not one."""
    if isinstance(node, ast.Constant):
        return _json_scalar(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _literal(node.operand)
        if isinstance(inner, bool) or not isinstance(inner, (int, float)):
            return _MISSING
        return -inner if isinstance(node.op, ast.USub) else inner
    if isinstance(node, (ast.List, ast.Tuple)):
        items = []
        for elt in node.elts:
            lit = _literal(elt)
            if lit is _MISSING:
                return _MISSING
            items.append(lit)
        return items
    if isinstance(node, ast.Dict):
        out: dict[str, Any] = {}
        for key_node, val_node in zip(node.keys, node.values):
            if key_node is None:
                return _MISSING
            key = _literal(key_node)
            val = _literal(val_node)
            if not isinstance(key, str) or val is _MISSING:
                return _MISSING
            out[key] = val
        return out
    return _MISSING


def _json_scalar(value):
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return _MISSING


def _json_dict(raw: dict) -> dict | None:
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            return None
        scalar = _json_scalar(value)
        if scalar is _MISSING:
            return None
        out[key] = scalar
    return out
