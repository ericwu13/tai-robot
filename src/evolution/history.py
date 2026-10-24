"""Append-only record of one evolution attempt (D2).

One JSON object per line, at ``{bot_dir}/evolution_history.jsonl``.
Live bots keep that file next to ``evolution_watermark.json`` under
``data/live/{symbol}_{bot}/``. A run with no bot directory does not
write (there is no per-bot file to append to). Tests pass a temporary
directory. This module never reads settings, never writes a lock, a
vote, or session state, and never raises to its caller.

Schema (one object per attempt). Keys the plan named are stored under
these names:

* ``ts`` — ``YYYY-MM-DDTHH:MM:SS``, local time, the same form as a
  #188 ``WeekRecord.at``.
* ``bot`` — strategy display name.
* ``class`` — strategy class name.
* ``source_hash`` — sha256 of the strategy source. The source text is
  not stored.
* ``cut_idx`` — design-window cut, or null when the attempt ended
  before the cut existed.
* ``watermark`` — ``{"trade_count", "at"}`` or null. Other keys on the
  loaded watermark are dropped.
* ``plan_excerpt`` — first 300 characters of the plan (the same cap the
  PASS changelog uses). Empty when no plan was produced.
* ``directives`` — ``action``, ``criteria``, and ``knobs`` only.
* ``params_before`` / ``params_after`` / ``params_reason`` — the same
  snapshot ``evolution_changelog_params`` writes on a PASS. Null on
  skips and on any attempt that never built a candidate.
* ``gates.deep`` — the window verdict (walk-forward when deep
  validation ran, otherwise the single-window A/B). ``kind`` is
  ``deep`` or ``ab``. ``reasons`` are the verdict strings. ``values``
  holds the numeric side summaries. Null when that gate did not run.
* ``gates.multifold`` — multi-fold verdict strings plus fold numbers.
* ``gates.month`` — the #181 ``month_gates`` dict, verbatim (reasons
  and the per-month nets). Null when month gates did not run.
* ``gates.promotion_gate`` — mode, note, whether the absolute PF floor
  was required, the combined pass/fail/provisional, the combined
  reason lines, and (relative path only) ``relative_reasons``.
* ``final_status`` — ``skip``, ``no_change``, ``refused``, ``fail``,
  or ``pass``. A provisional month gate is ``fail`` with
  ``provisional`` true (the combined verdict is already not a clean
  PASS).
* ``provisional`` — copied from the combined verdict, else false.
* ``ai_calls`` — completed-or-started plan and codegen calls. Null
  when the count is not known.
* ``reason`` / ``reason_kind`` — why this attempt stopped. The kind is
  the machine key; ``reason`` is the operator-visible line when there
  is one.
* ``retire_week`` — ``{at, evaluated, passed}``, the #188 streak input.
  ``evaluated`` is true only when this attempt produced a validation
  verdict under ``promotion_gate.mode == relative``. Skips, absolute
  runs, and attempts that never reached a verdict store
  ``evaluated: false`` and do not count and do not reset the streak.
* ``thresholds`` — the resolved paper/real/relative/retire config in
  force for the attempt (defaults included). Unknown keys on the raw
  settings mapping are not copied.

``reason_kind`` values used by the pipeline:

* ``no_trades``, ``no_chat_client``, ``knob_ineligible``, ``watermark``,
  ``no_design_window``, ``min_design_trades`` (the brief's fitness
  sample skip: design window below ``MIN_TRADES``), ``plan_only``
  → ``final_status=skip``
* ``no_change`` → ``no_change``
* ``refused`` — Saturday knob plan refused on an eligible class
* ``plan_unusable``, ``no_source``, ``knob_apply``, ``codegen``,
  ``insufficient_bars``, ``validation``, ``pipeline_error`` → ``fail``
  or, for a clean validation pass, ``pass``

A write error is logged and swallowed. Readers skip a missing file,
an undecodable file, and any line that is not a JSON object.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict
from datetime import datetime

from .paper_gate import (
    WeekRecord,
    abs_pf_required,
    dedupe_weeks,
    evolution_gate_config_from_mapping,
)

HISTORY_FILENAME = "evolution_history.jsonl"
PLAN_EXCERPT_CHARS = 300
_REASON_CHARS = 4000
_STATUSES = ("skip", "no_change", "refused", "fail", "pass")
_GATE_KEYS = ("deep", "multifold", "month", "promotion_gate")


def evolution_history_path(bot_dir) -> str:
    """``{bot_dir}/evolution_history.jsonl`` (live: under ``data/live/``)."""
    return os.path.join(os.fspath(bot_dir), HISTORY_FILENAME)


def _report(log, message: str) -> None:
    if log is None:
        return
    try:
        log(message)
    except Exception:
        pass


def _finite(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _json_ready(value):
    """A JSON value. Non-finite floats become null. Unknown objects drop."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return None


def _status(value) -> str:
    text = str(value or "").strip().lower()
    if text in _STATUSES:
        return text
    return "fail"


def _cut_idx(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ai_calls(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _hash_source(source: str) -> str:
    if not source:
        return ""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _watermark(value):
    if not isinstance(value, dict):
        return None
    out = {}
    if "trade_count" in value:
        try:
            out["trade_count"] = int(value.get("trade_count") or 0)
        except (TypeError, ValueError):
            out["trade_count"] = 0
    if "at" in value:
        out["at"] = str(value.get("at") or "")
    return out or None


def _directives(value):
    if not isinstance(value, dict):
        return None
    out = {}
    if "action" in value:
        out["action"] = str(value.get("action") or "")
    if "criteria" in value:
        criteria = value.get("criteria")
        if isinstance(criteria, dict):
            cleaned = {}
            for key, raw in criteria.items():
                number = _finite(raw)
                if number is not None:
                    cleaned[str(key)] = number
            out["criteria"] = cleaned or None
        else:
            out["criteria"] = None
    if "knobs" in value:
        knobs = value.get("knobs")
        out["knobs"] = _json_ready(knobs) if isinstance(knobs, dict) else None
    return out or None


def _empty_gates() -> dict:
    return {key: None for key in _GATE_KEYS}


def _normalize_gates(gates) -> dict:
    base = _empty_gates()
    if isinstance(gates, dict):
        for key in _GATE_KEYS:
            if key in gates:
                base[key] = _json_ready(gates[key])
    return base


def thresholds_snapshot(raw) -> dict:
    """Resolved gate thresholds. Unknown raw keys are not copied."""
    cfg = evolution_gate_config_from_mapping(raw if isinstance(raw, dict) else None)
    return {
        "paper_gate": cfg.paper_gate,
        "real_gate": cfg.real_gate,
        "warnings": list(cfg.warnings),
        "relative": asdict(cfg.relative),
        "retire": asdict(cfg.retire),
    }


def _metrics_numbers(metrics) -> dict:
    if metrics is None:
        return {}
    pf = getattr(metrics, "profit_factor", None)
    return {
        "trades": getattr(metrics, "total_trades", None),
        "pnl": getattr(metrics, "total_pnl", None),
        "pf": _finite(pf),
        "max_drawdown": getattr(metrics, "max_drawdown", None),
        "max_drawdown_pct": _finite(getattr(metrics, "max_drawdown_pct", None)),
        "win_rate": _finite(getattr(metrics, "win_rate", None)),
    }


def _side_numbers(side) -> dict:
    """Numeric summary of an ``ABResult`` or a ``DeepResult`` test window."""
    if side is None:
        return {}
    test = getattr(side, "test", None)
    if test is not None and (hasattr(test, "metrics") or hasattr(test, "fitness")):
        metrics = getattr(test, "metrics", None)
        fitness = getattr(test, "fitness", None)
        out = _metrics_numbers(metrics)
        if fitness is not None:
            out["composite"] = _finite(getattr(fitness, "composite", None))
            out["gated"] = bool(getattr(fitness, "gated", False))
        if getattr(side, "mc_variance", None) not in (None, 0):
            out["mc_variance"] = _finite(getattr(side, "mc_variance", None))
        if getattr(side, "fragile", False):
            out["fragile"] = True
        error = getattr(test, "error", "") or ""
        if error:
            out["error"] = str(error)
        return _json_ready(out) or {}
    metrics = getattr(side, "metrics", None)
    fitness = getattr(side, "fitness", None)
    out = _metrics_numbers(metrics)
    if fitness is not None:
        out["composite"] = _finite(getattr(fitness, "composite", None))
        out["gated"] = bool(getattr(fitness, "gated", False))
    error = getattr(side, "error", "") or ""
    if error:
        out["error"] = str(error)
    return _json_ready(out) or {}


def _fold_numbers(folds) -> dict:
    folds = list(folds or [])
    if not folds:
        return {"n_folds": 0}
    holdout = folds[-1]
    divergent = [fold for fold in folds if getattr(fold, "delta", 0) != 0]
    wins = sum(1 for fold in divergent if getattr(fold, "delta", 0) > 0)
    holdout_pf = _finite(getattr(getattr(holdout, "cand", None), "profit_factor", None))
    rows = []
    for fold in folds:
        base = getattr(fold, "base", None)
        cand = getattr(fold, "cand", None)
        rows.append({
            "start": getattr(fold, "start", ""),
            "end": getattr(fold, "end", ""),
            "base_n": getattr(base, "n", None),
            "base_pnl": getattr(base, "pnl", None),
            "cand_n": getattr(cand, "n", None),
            "cand_pnl": getattr(cand, "pnl", None),
            "delta": getattr(fold, "delta", None),
            "shared": getattr(fold, "shared", None),
        })
    return {
        "n_folds": len(folds),
        "nonempty": sum(1 for fold in folds if getattr(getattr(fold, "cand", None), "n", 0) > 0),
        "divergent_folds": len(divergent),
        "divergent_wins": wins,
        "paired_total_delta": sum(getattr(fold, "delta", 0) for fold in folds),
        "holdout_start": getattr(holdout, "start", ""),
        "holdout_end": getattr(holdout, "end", ""),
        "holdout_cand_n": getattr(getattr(holdout, "cand", None), "n", None),
        "holdout_cand_pnl": getattr(getattr(holdout, "cand", None), "pnl", None),
        "holdout_pf": holdout_pf,
        "folds": rows,
    }


def _verdict_block(verdict, values, *, kind=None) -> dict | None:
    if verdict is None:
        return None
    block = {
        "passed": bool(getattr(verdict, "passed", False)),
        "provisional": bool(getattr(verdict, "provisional", False)),
        "reasons": [str(line) for line in (getattr(verdict, "reasons", None) or [])],
        "values": values or {},
    }
    if kind:
        block["kind"] = kind
    return block


def validation_gates(*, assembled, folds, baseline_res, candidate_res,
                     gate, gate_cfg) -> dict:
    """Per-gate blocks for one validation attempt.

    ``gates.month`` is the #181 observation dict, not a wrapper.
    ``gates.deep`` is the window verdict (deep walk-forward or A/B).
    ``gates.promotion_gate`` is the combined promotion decision.
    """
    window = getattr(assembled, "window_verdict", None)
    multifold = getattr(assembled, "multifold_verdict", None)
    relative = getattr(assembled, "relative_verdict", None)
    verdict = getattr(assembled, "verdict", None)
    deep_side = baseline_res if baseline_res is not None else candidate_res
    kind = "deep" if getattr(deep_side, "test", None) is not None else "ab"
    month = getattr(assembled, "month_gates", None)
    promo = None
    if gate is not None or verdict is not None:
        promo = {
            "mode": getattr(gate, "mode", "") or "",
            "note": getattr(gate, "note", "") or "",
            "require_abs_pf": bool(abs_pf_required(gate)) if gate is not None else True,
            "passed": bool(getattr(verdict, "passed", False)),
            "provisional": bool(getattr(verdict, "provisional", False)),
            "reasons": [str(line) for line in (getattr(verdict, "reasons", None) or [])],
            "relative_reasons": (
                [str(line) for line in relative.reasons] if relative is not None else None
            ),
        }
        if gate_cfg is not None and getattr(gate_cfg, "warnings", None):
            promo["warnings"] = [str(line) for line in gate_cfg.warnings]
    return {
        "deep": _verdict_block(
            window,
            {"baseline": _side_numbers(baseline_res),
             "candidate": _side_numbers(candidate_res)},
            kind=kind,
        ),
        "multifold": _verdict_block(multifold, _fold_numbers(folds)),
        "month": month,
        "promotion_gate": promo,
    }


def build_evolution_record(
        *,
        ts: str | None = None,
        bot: str = "",
        class_name: str = "",
        source: str = "",
        source_hash: str | None = None,
        cut_idx=None,
        watermark=None,
        plan_excerpt: str = "",
        directives=None,
        params_before=None,
        params_after=None,
        params_reason: str | None = None,
        gates=None,
        final_status: str = "fail",
        provisional: bool = False,
        ai_calls=0,
        reason: str = "",
        reason_kind: str = "",
        retire_evaluated: bool = False,
        retire_passed: bool = False,
        thresholds=None,
) -> dict:
    """One history object. Does not touch the filesystem."""
    stamp = ts or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    evaluated = bool(retire_evaluated)
    passed = bool(retire_passed) if evaluated else False
    digest = source_hash if source_hash is not None else _hash_source(source or "")
    excerpt = str(plan_excerpt or "")[:PLAN_EXCERPT_CHARS]
    reason_text = str(reason or "")[:_REASON_CHARS]
    record = {
        "ts": str(stamp),
        "bot": str(bot or "")[:200],
        "class": str(class_name or "")[:200],
        "source_hash": str(digest or ""),
        "cut_idx": _cut_idx(cut_idx),
        "watermark": _watermark(watermark),
        "plan_excerpt": excerpt,
        "directives": _directives(directives),
        "params_before": _json_ready(params_before) if isinstance(params_before, dict) else None,
        "params_after": _json_ready(params_after) if isinstance(params_after, dict) else None,
        "params_reason": (str(params_reason) if params_reason else None),
        "gates": _normalize_gates(gates),
        "final_status": _status(final_status),
        "provisional": bool(provisional),
        "ai_calls": _ai_calls(ai_calls),
        "reason": reason_text,
        "reason_kind": str(reason_kind or "")[:80],
        "retire_week": {
            "at": str(stamp),
            "evaluated": evaluated,
            "passed": passed,
        },
        "thresholds": _json_ready(thresholds) if isinstance(thresholds, dict) else None,
    }
    return record


def append_evolution_history(path, record, *, log=None) -> bool:
    """Append one JSON line. Never raises.

    The previous file is replaced only after the new contents are on
    disk (temp file in the same directory, fsync, ``os.replace``). A
    failure leaves the previous bytes in place. An existing file that
    cannot be decoded as UTF-8 is left untouched and the failure is
    logged. A missing file is created.
    """
    path = os.fspath(path)
    tmp = None
    try:
        payload = _json_ready(record)
        if not isinstance(payload, dict):
            raise TypeError("evolution history record must be an object")
        line = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        blob = line + "\n"
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        existing = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                existing = handle.read()
        if existing and not existing.endswith("\n"):
            existing += "\n"
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(existing)
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
        return True
    except Exception as exc:
        _report(log, f"evolution history append failed: [{type(exc).__name__}] {exc}")
        return False
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_evolution_history(path) -> list[dict]:
    """Records in file order.

    A missing file, an unreadable file, and a non-UTF-8 file yield
    ``[]``. Lines that are not a JSON object are skipped. Valid lines
    around a corrupt line are kept.
    """
    path = os.fspath(path)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeError):
        return []
    found = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    return found


def record_evolution_attempt(bot_dir, *, log=None, thresholds_raw=None,
                             thresholds=None, source: str = "",
                             source_hash: str | None = None,
                             capture_params: bool = False,
                             knob_apply=None, baseline_cls=None,
                             baseline_source: str = "",
                             candidate_source: str = "",
                             candidate_name: str = "",
                             baseline_name: str = "",
                             params_before=None, params_after=None,
                             params_reason: str | None = None,
                             **fields) -> bool:
    """Build one record and append it under ``bot_dir``. Never raises.

    ``bot_dir`` empty → False, no log (no per-bot file). Write failures
    are logged through ``log`` and return False.
    """
    if not bot_dir:
        return False
    try:
        if (capture_params and params_before is None and params_after is None
                and (knob_apply or baseline_source or candidate_source)):
            from .changelog_params import evolution_changelog_params
            params_before, params_after, params_reason = evolution_changelog_params(
                knob_apply=knob_apply,
                baseline_cls=baseline_cls,
                baseline_source=baseline_source or "",
                candidate_source=candidate_source or "",
                candidate_name=candidate_name or "",
                baseline_name=baseline_name or "",
            )
        if thresholds is None and thresholds_raw is not None:
            thresholds = thresholds_snapshot(thresholds_raw)
        elif thresholds is None and "thresholds" not in fields:
            thresholds = thresholds_snapshot(None)
        record = build_evolution_record(
            source=source,
            source_hash=source_hash,
            params_before=params_before,
            params_after=params_after,
            params_reason=params_reason,
            thresholds=thresholds,
            **fields,
        )
        return append_evolution_history(
            evolution_history_path(bot_dir), record, log=log)
    except Exception as exc:
        _report(log, f"evolution history append failed: [{type(exc).__name__}] {exc}")
        return False


def _strict_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "true":
            return True
        if text == "false":
            return False
    return None


def weeks_from_history(records, bot: str | None = None) -> list[WeekRecord]:
    """Evaluated relative-gate weeks, chronological, one per calendar day.

    Rows with ``retire_week.evaluated`` false are omitted, so a skip
    neither counts nor resets the streak (the same rule as #188, which
    does not write skipped weeks into the changelog). A later record
    on the same calendar day replaces the earlier one. Malformed rows
    and other bots are skipped. ``bot`` None keeps every bot.
    """
    found: list[WeekRecord] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if bot is not None and str(record.get("bot") or "") != bot:
            continue
        week = record.get("retire_week")
        if not isinstance(week, dict):
            continue
        evaluated = _strict_bool(week.get("evaluated"))
        passed = _strict_bool(week.get("passed"))
        if evaluated is None or passed is None or not evaluated:
            continue
        at = str(week.get("at") or "")
        if len(at) < 19:
            continue
        found.append(WeekRecord(at=at, evaluated=True, passed=passed))
    return dedupe_weeks(found)


def retire_streak_from_history(records, *, consecutive_weeks: int = 4,
                               bot: str | None = None) -> dict:
    """The #188 not-improvable streak, from history alone.

    The last ``consecutive_weeks`` evaluated relative-gate rows must
    all be failures. Skips and absolute-mode rows are not in that
    list. ``evaluated_failures_tail`` is empty unless the streak holds,
    matching ``assess_retirement`` evidence.
    """
    try:
        need = int(consecutive_weeks)
    except (TypeError, ValueError):
        need = 4
    if need < 1:
        need = 1
    weeks = weeks_from_history(records, bot=bot)
    evaluated = [week for week in weeks if week.evaluated]
    tail = evaluated[-need:]
    streak = len(tail) >= need and all(not week.passed for week in tail)
    return {
        "streak": streak,
        "consecutive_weeks": need,
        "evaluated_count": len(evaluated),
        "evaluated_failures_tail": [week.at for week in tail] if streak else [],
        "weeks": [
            {"at": week.at, "evaluated": week.evaluated, "passed": week.passed}
            for week in weeks
        ],
    }
