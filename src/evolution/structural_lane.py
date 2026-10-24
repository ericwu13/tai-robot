"""Monthly structural lane and Saturday propose-only mode (P6, Nova Q6).

The knob grid is a different lane. This module does not import it, does
not pick a grid point, and does not write settings, an allowlist, a
live or paper strategy, StrategyStore, or the strategy registry.
The lane defaults on and still only proposes. Boolean false turns it off.

A structural candidate is one named form from a fixed menu, at most
once per bot per calendar month, and at most ``max_in_paper``
outstanding paper *recommendations* at once. Graduation and retirement
are text. ``applied`` on every record this module writes is false.

PBO and DSR are inputs. They are not computed here (that trial-penalty
math is a separate change). A missing value does not count as a pass.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# One process-wide lock, shared by every bot in this process. It covers
# the ledger read-modify-write (reserve, record, save). It is not a file lock.
_LEDGER_LOCK = threading.RLock()

# Test seam. Production leaves this None. A callable runs after a ledger
# read and before the matching write, inside ``_LEDGER_LOCK``.
_ledger_write_pause = None


def _pause_ledger_write() -> None:
    hook = _ledger_write_pause
    if hook is not None:
        hook()

# Nova Q6 menu. The id is what the plan must name. The text is the
# only change that form is allowed to make.
STRUCTURAL_FORMS: tuple[tuple[str, str], ...] = (
    ("add_regime_filter", "add a regime filter"),
    ("remove_regime_filter", "remove a regime filter"),
    ("change_exit_type", "change the exit type"),
    ("add_session_filter", "add a time-of-day or session filter"),
    ("change_entry_trigger", "change the entry trigger"),
    ("side_split", "split the strategy into long-only or short-only"),
)

FORM_IDS: tuple[str, ...] = tuple(form for form, _desc in STRUCTURAL_FORMS)

MIN_NATIVE_BARS = 150
STRUCTURAL_CODEGEN_ATTEMPTS = 1

SATURDAY_PROMOTE = "promote"
SATURDAY_PROPOSE = "propose"
# Saturday's default records a proposal. "promote" is an explicit opt-in.
DEFAULT_SATURDAY_MODE = SATURDAY_PROPOSE

# Nova Q6 durations. The plan sketch's paper_days=14 is not this gate.
DEFAULT_PER_BOT_PER_MONTH = 1
DEFAULT_MAX_IN_PAPER = 3
DEFAULT_MIN_PAPER_SESSIONS = 21
DEFAULT_MIN_PAPER_TRADES = 20
DEFAULT_TIMEOUT_SESSIONS = 45
DEFAULT_GRAD_PF = 1.10
DEFAULT_GRAD_WEEKLY_WINS = 3
DEFAULT_GRAD_WEEKLY_OF = 5
DEFAULT_GRAD_MAJORITY = 0.60
DEFAULT_GRAD_DD_RATIO = 1.2
DEFAULT_GRAD_DECAY = 0.50
DEFAULT_PBO_MAX = 0.50
DEFAULT_DSR_MIN = 0.90

# Outstanding paper recommendations. A refusal does not occupy a slot.
_IN_PAPER_STATUSES = ("paper_recommended", "graduation_recommended")

_FORM_RE = re.compile(r"structural_form\s*[:=]\s*([A-Za-z_]+)")
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Admission:
    """Whether this bot may spend its structural slot this month."""

    admit: bool
    reason: str


@dataclass
class GraduationRecommendation:
    """Text for a human. ``recommend`` means "surface this", not "apply it"."""

    status: str
    recommend: bool
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


def form_ids() -> tuple[str, ...]:
    return FORM_IDS


def normalize_saturday_mode(value) -> str:
    """``propose`` (the default) or ``promote``. Anything else is propose.

    Promote is an explicit opt-in. It is not the default.
    """
    if isinstance(value, str):
        text = value.strip().lower()
        if text in (SATURDAY_PROMOTE, SATURDAY_PROPOSE):
            return text
    return DEFAULT_SATURDAY_MODE


def default_lane_config() -> dict:
    """Lane on. A missing ``enabled`` is boolean true.

    Saturday mode is not stored here; a missing mode is propose.
    The lane still only proposes. It does not promote the pool.
    """
    return {
        "enabled": True,
        "per_bot_per_month": DEFAULT_PER_BOT_PER_MONTH,
        "max_in_paper": DEFAULT_MAX_IN_PAPER,
        "min_paper_sessions": DEFAULT_MIN_PAPER_SESSIONS,
        "min_paper_trades": DEFAULT_MIN_PAPER_TRADES,
        "timeout_sessions": DEFAULT_TIMEOUT_SESSIONS,
        "grad_pf": DEFAULT_GRAD_PF,
        "grad_weekly_wins": DEFAULT_GRAD_WEEKLY_WINS,
        "grad_weekly_of": DEFAULT_GRAD_WEEKLY_OF,
        "grad_majority": DEFAULT_GRAD_MAJORITY,
        "grad_dd_ratio": DEFAULT_GRAD_DD_RATIO,
        "grad_decay": DEFAULT_GRAD_DECAY,
        "pbo_max": DEFAULT_PBO_MAX,
        "dsr_min": DEFAULT_DSR_MIN,
        "ledger_path": "",
        "warnings": [],
    }


def _warn_int(mapping: dict, key: str, default: int, lo: int, hi: int,
              warnings: list[str]) -> int:
    if key not in mapping:
        return default
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        parsed = None
    elif isinstance(value, float) and not value.is_integer():
        parsed = None
    else:
        parsed = int(value)
    if parsed is None or parsed < lo or parsed > hi:
        warnings.append(
            f"structural_lane.{key} {value!r} outside [{lo}, {hi}]; "
            f"using default {default}")
        return default
    return parsed


def _warn_float(mapping: dict, key: str, default: float, lo: float, hi: float,
                warnings: list[str]) -> float:
    if key not in mapping:
        return default
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        parsed = None
    else:
        parsed = float(value)
    if parsed is None or parsed != parsed or parsed < lo or parsed > hi:
        warnings.append(
            f"structural_lane.{key} {value!r} outside [{lo:g}, {hi:g}]; "
            f"using default {default:g}")
        return default
    return parsed


def parse_lane_config(raw) -> dict:
    """Fill defaults. A missing block leaves the lane on.

    Boolean false is the off switch. A non-boolean ``enabled`` stays
    off. ``paper_days`` is accepted and ignored. Nova Q6 replaced that
    14-day sketch with a session and trade floor.
    """
    cfg = default_lane_config()
    warnings: list[str] = []
    if raw is None:
        cfg["warnings"] = warnings
        return cfg
    if not isinstance(raw, dict):
        warnings.append(
            f"structural_lane {raw!r} is not a mapping; lane stays disabled")
        cfg["enabled"] = False
        cfg["warnings"] = warnings
        return cfg
    if "paper_days" in raw:
        warnings.append(
            "structural_lane.paper_days is not the incubation gate; "
            "Nova Q6 uses min_paper_sessions and min_paper_trades")
    if "enabled" in raw:
        flag = raw.get("enabled")
        if isinstance(flag, bool):
            cfg["enabled"] = flag
        else:
            warnings.append(
                f"structural_lane.enabled {flag!r} is not a boolean; "
                "lane stays disabled")
            cfg["enabled"] = False
    cfg["per_bot_per_month"] = _warn_int(
        raw, "per_bot_per_month", DEFAULT_PER_BOT_PER_MONTH, 1, 1, warnings)
    cfg["max_in_paper"] = _warn_int(
        raw, "max_in_paper", DEFAULT_MAX_IN_PAPER, 1, 3, warnings)
    cfg["min_paper_sessions"] = _warn_int(
        raw, "min_paper_sessions", DEFAULT_MIN_PAPER_SESSIONS, 1, 60, warnings)
    cfg["min_paper_trades"] = _warn_int(
        raw, "min_paper_trades", DEFAULT_MIN_PAPER_TRADES, 1, 500, warnings)
    cfg["timeout_sessions"] = _warn_int(
        raw, "timeout_sessions", DEFAULT_TIMEOUT_SESSIONS, 1, 120, warnings)
    if cfg["timeout_sessions"] < cfg["min_paper_sessions"]:
        warnings.append(
            "structural_lane.timeout_sessions is below min_paper_sessions; "
            f"using default {DEFAULT_TIMEOUT_SESSIONS}")
        cfg["timeout_sessions"] = DEFAULT_TIMEOUT_SESSIONS
    cfg["grad_pf"] = _warn_float(
        raw, "grad_pf", DEFAULT_GRAD_PF, 1.0, 5.0, warnings)
    cfg["grad_weekly_wins"] = _warn_int(
        raw, "grad_weekly_wins", DEFAULT_GRAD_WEEKLY_WINS, 1, 10, warnings)
    cfg["grad_weekly_of"] = _warn_int(
        raw, "grad_weekly_of", DEFAULT_GRAD_WEEKLY_OF, 1, 12, warnings)
    cfg["grad_majority"] = _warn_float(
        raw, "grad_majority", DEFAULT_GRAD_MAJORITY, 0.5, 1.0, warnings)
    cfg["grad_dd_ratio"] = _warn_float(
        raw, "grad_dd_ratio", DEFAULT_GRAD_DD_RATIO, 1.0, 3.0, warnings)
    cfg["grad_decay"] = _warn_float(
        raw, "grad_decay", DEFAULT_GRAD_DECAY, 0.0, 2.0, warnings)
    cfg["pbo_max"] = _warn_float(
        raw, "pbo_max", DEFAULT_PBO_MAX, 0.0, 1.0, warnings)
    cfg["dsr_min"] = _warn_float(
        raw, "dsr_min", DEFAULT_DSR_MIN, 0.0, 1.0, warnings)
    ledger = raw.get("ledger_path", "")
    cfg["ledger_path"] = str(ledger or "").strip()
    cfg["warnings"] = warnings
    return cfg


def settings_from_evolution(evo: dict | None) -> dict:
    """Keys ``_load_settings`` stores.

    A missing ``saturday_mode`` is propose. ``promote`` has to be set.
    ``paper_days`` is read only inside ``structural_lane``.
    """
    evo = evo if isinstance(evo, dict) else {}
    mode = normalize_saturday_mode(evo.get("saturday_mode", DEFAULT_SATURDAY_MODE))
    warnings: list[str] = []
    if "saturday_mode" in evo and mode != str(evo.get("saturday_mode") or "").strip().lower():
        warnings.append(
            f"saturday_mode {evo.get('saturday_mode')!r} is not "
            f"promote|propose; using {DEFAULT_SATURDAY_MODE}")
    lane = parse_lane_config(evo.get("structural_lane"))
    lane["warnings"] = list(lane.get("warnings") or []) + warnings
    return {
        "evolution_saturday_mode": mode,
        "evolution_structural_lane": lane,
    }


def lane_config_from_settings(settings) -> dict:
    """A partial settings dict still gets the lane defaults.

    A missing block leaves ``enabled`` true. A non-boolean value is off.
    """
    raw = {}
    if isinstance(settings, dict):
        candidate = settings.get("evolution_structural_lane")
        if isinstance(candidate, dict):
            raw = candidate
    cfg = default_lane_config()
    cfg.update({k: raw[k] for k in cfg if k in raw})
    if not isinstance(cfg.get("enabled"), bool):
        cfg["enabled"] = False
    if not isinstance(cfg.get("warnings"), list):
        cfg["warnings"] = []
    return cfg


def sanitize_bot_id(value: str) -> str:
    text = _SAFE_ID.sub("_", str(value or "").strip())
    text = text.strip("._")
    return (text[:80] or "bot")


def passes_native_bar_guard(n: int, minimum: int = MIN_NATIVE_BARS) -> bool:
    try:
        count = int(n)
    except (TypeError, ValueError):
        return False
    return count >= int(minimum)


def structural_plan_block() -> str:
    """Prompt trailer. Names the menu and forbids a knob-grid step."""
    lines = [
        "## Structural lane (MANDATORY on this run)",
        "This run is the monthly structural lane. It is not the Saturday "
        "knob grid. Do not propose a numeric knob step.",
        "Propose EXACTLY ONE change, and name exactly one of these forms:",
    ]
    for form, desc in STRUCTURAL_FORMS:
        lines.append(f"- {form}: {desc}")
    lines.append(
        "End the plan with one line: structural_form: <one form from the list>")
    lines.append(
        "One candidate. There is no retry this month if this one fails.")
    return "\n".join(lines) + "\n"


def structural_codegen_prompt(*, source: str, plan: str, candidate_name: str,
                              form: str, context: str = "") -> str:
    """Single codegen request. Callers do not retry it."""
    desc = dict(STRUCTURAL_FORMS).get(form, form)
    return (
        f"{context}\n\n"
        f"## Current strategy source\n```python\n{source}\n```\n\n"
        f"## Evolution plan\n{plan}\n\n"
        f"## Task\n"
        f"Apply EXACTLY one structural change: `{form}` ({desc}). "
        f"Do not change numeric knob defaults. Do not apply a second change. "
        f"The class MUST be named `{candidate_name}` and keep the same "
        f"kline_type / kline_minute. Output ONLY one ```python code block "
        f"containing the complete strategy file. This is the only attempt."
    )


def parse_structural_form(text: str) -> str | None:
    """The single menu id named in a plan, or None.

    Zero matches and several matches both refuse. A knob-only plan
    has no ``structural_form`` line, so it is None and cannot enter
    this lane.
    """
    found = [m.group(1).strip().lower() for m in _FORM_RE.finditer(text or "")]
    if len(found) != 1:
        return None
    token = found[0]
    for form in FORM_IDS:
        if form == token:
            return form
    return None


def pool_promotion_allowed(*, saturday_mode, structural_lane: bool,
                           eligible: bool, auto_run: bool) -> bool:
    """Pool promotion is the current Saturday promote path only.

    Propose mode and the structural lane never promote. A manual run
    (``auto_run`` false) keeps the promote path even when the Saturday
    flag is propose — the flag is the Saturday auto run.
    """
    if not eligible:
        return False
    if structural_lane:
        return False
    if auto_run and normalize_saturday_mode(saturday_mode) == SATURDAY_PROPOSE:
        return False
    return True


def empty_ledger() -> dict:
    return {"slots": [], "incubations": []}


def load_ledger(path) -> dict:
    """Read the lane ledger. A missing file is an empty ledger, not an error.

    Corrupt JSON fails closed (``unreadable``) so a bad file cannot
    reset the monthly cap.
    """
    if not path:
        return empty_ledger()
    p = Path(path)
    if not p.exists():
        return empty_ledger()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {"slots": [], "incubations": [], "unreadable": True}
    if not isinstance(data, dict):
        return {"slots": [], "incubations": [], "unreadable": True}
    slots = data.get("slots")
    incubations = data.get("incubations")
    return {
        "slots": list(slots) if isinstance(slots, list) else [],
        "incubations": list(incubations) if isinstance(incubations, list) else [],
    }


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    os.replace(tmp, path)


def save_ledger(path, ledger: dict) -> None:
    """Replace the ledger file under the process-wide ledger lock.

    Every bot in this process shares ``_LEDGER_LOCK``. Callers that
    already hold it (reserve, record, the Saturday follow-up) re-enter
    it. This does not take a file lock.
    """
    with _LEDGER_LOCK:
        body = {
            "slots": list(ledger.get("slots") or []),
            "incubations": list(ledger.get("incubations") or []),
        }
        _atomic_json(Path(path), body)


def count_in_paper(ledger: dict) -> int:
    n = 0
    for inc in ledger.get("incubations") or []:
        if isinstance(inc, dict) and inc.get("status") in _IN_PAPER_STATUSES:
            n += 1
    return n


def _has_slot(ledger: dict, bot_id: str, month: str) -> bool:
    for slot in ledger.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        if slot.get("bot_id") == bot_id and slot.get("month") == month:
            return True
    return False


def admission_decision(*, enabled: bool, ineligible: bool, fallback: str,
                       bot_id: str, month: str, ledger: dict,
                       config: dict | None = None) -> Admission:
    """Gate for one structural attempt. Does not write."""
    cfg = config or default_lane_config()
    if not enabled:
        return Admission(False, "structural lane disabled")
    if ledger.get("unreadable"):
        return Admission(False, "structural ledger unreadable; failing closed")
    if ineligible and str(fallback or "").strip().lower() != "structural_lane":
        return Admission(
            False, "knob-ineligible fallback is not structural_lane")
    if not bot_id or not month:
        return Admission(False, "missing bot id or month")
    cap = int(cfg.get("per_bot_per_month") or DEFAULT_PER_BOT_PER_MONTH)
    used = sum(
        1 for slot in (ledger.get("slots") or [])
        if isinstance(slot, dict)
        and slot.get("bot_id") == bot_id
        and slot.get("month") == month
    )
    if used >= cap:
        return Admission(
            False, "per_bot_per_month cap reached; no retry this month")
    limit = int(cfg.get("max_in_paper") or DEFAULT_MAX_IN_PAPER)
    if count_in_paper(ledger) >= limit:
        return Admission(False, "max in paper reached")
    return Admission(True, "admitted")


def ineligible_structural_divert(settings, *, fallback: str, bot_id: str,
                                 month: str, ledger_path) -> bool:
    """True when an excluded class may continue into the structural lane.

    False leaves the caller on today's exclusion return. The lane must
    be enabled; the fallback string alone does not admit.
    """
    cfg = lane_config_from_settings(settings)
    if not cfg.get("enabled"):
        return False
    decision = admission_decision(
        enabled=True,
        ineligible=True,
        fallback=fallback,
        bot_id=bot_id,
        month=month,
        ledger=load_ledger(ledger_path),
        config=cfg,
    )
    return bool(decision.admit)


def reserve_structural_slot(path, *, bot_id: str, month: str, config: dict,
                            now: datetime | None = None,
                            ineligible: bool = False,
                            fallback: str = "") -> Admission:
    """Consume this bot's month. A second call the same month refuses.

    The process-wide ledger lock is held from the read through the save,
    so two bots reserving together cannot drop a slot. Writes only
    ``path``. Returns the admission that was applied. This does not
    take a file lock.
    """
    with _LEDGER_LOCK:
        return _reserve_structural_slot_locked(
            path, bot_id=bot_id, month=month, config=config, now=now,
            ineligible=ineligible, fallback=fallback)


def _reserve_structural_slot_locked(path, *, bot_id: str, month: str,
                                    config: dict, now: datetime | None,
                                    ineligible: bool, fallback: str) -> Admission:
    ledger = load_ledger(path)
    _pause_ledger_write()
    decision = admission_decision(
        enabled=bool(config.get("enabled")),
        ineligible=ineligible,
        fallback=fallback,
        bot_id=bot_id,
        month=month,
        ledger=ledger,
        config=config,
    )
    if not decision.admit:
        return decision
    moment = now or datetime.now()
    slots = list(ledger.get("slots") or [])
    slots.append({
        "bot_id": bot_id,
        "month": month,
        "at": moment.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "reserved",
        "form": "",
        "applied": False,
    })
    ledger["slots"] = slots
    save_ledger(path, ledger)
    return Admission(True, "reserved")


def _touch_slot(ledger: dict, bot_id: str, month: str, form: str,
                status: str) -> None:
    for slot in ledger.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        if slot.get("bot_id") == bot_id and slot.get("month") == month:
            slot["status"] = status
            slot["form"] = form or slot.get("form") or ""
            slot["applied"] = False
            slot.pop("applied_at", None)


def _proposal_path(root, bot_id: str, month: str, lane: str, moment: datetime) -> Path:
    """One new file per attempt. A later week in the same month does not replace it.

    The stem carries the ISO week and a timestamp, so two Saturdays in
    one month never share a filename.
    """
    directory = Path(root) / "proposals"
    stamp = moment.strftime("%Y%m%dT%H%M%S%f")
    iso_week = moment.strftime("%G-W%V")
    stem = f"{sanitize_bot_id(bot_id)}-{month}-{lane}-{iso_week}-{stamp}"
    path = directory / f"{stem}.json"
    suffix = 2
    while path.exists():
        path = directory / f"{stem}-{suffix}.json"
        suffix += 1
    return path


def record_proposal(root, *, bot_id: str, month: str, lane: str, status: str,
                    evidence: dict | None = None, form: str = "",
                    ledger_path=None, touch_slot: bool = False,
                    incubation: dict | None = None,
                    now: datetime | None = None,
                    source_code: str = "",
                    promote=None, save_store=None, write_settings=None,
                    write_allowlist=None, mutate_strategies=None) -> dict:
    """Write the proposal artifact. The callable arguments are ignored.

    They are accepted so a propose-mode caller cannot apply a candidate
    by handing this function the pool, store, or settings writer.
    ``applied`` is forced false even if ``evidence`` says otherwise.
    ``source_code`` is the candidate source. A class name is not a
    substitute. The process-wide ledger lock covers this write and the
    ledger update that follows. This does not take a file lock.
    """
    with _LEDGER_LOCK:
        return _record_proposal_locked(
            root, bot_id=bot_id, month=month, lane=lane, status=status,
            evidence=evidence, form=form, ledger_path=ledger_path,
            touch_slot=touch_slot, incubation=incubation, now=now,
            source_code=source_code, promote=promote, save_store=save_store,
            write_settings=write_settings, write_allowlist=write_allowlist,
            mutate_strategies=mutate_strategies)


def _record_proposal_locked(root, *, bot_id: str, month: str, lane: str,
                            status: str, evidence, form: str, ledger_path,
                            touch_slot: bool, incubation, now, source_code: str,
                            promote, save_store, write_settings, write_allowlist,
                            mutate_strategies) -> dict:
    del promote, save_store, write_settings, write_allowlist, mutate_strategies
    moment = now or datetime.now()
    payload = dict(evidence or {})
    payload.pop("applied", None)
    payload.pop("applied_at", None)
    code_text = source_code if isinstance(source_code, str) else ""
    if not code_text and isinstance(payload.get("source_code"), str):
        code_text = payload["source_code"]
    payload.pop("source_code", None)
    dsr_trials = payload.get("dsr_trials")
    if not isinstance(dsr_trials, int) or isinstance(dsr_trials, bool):
        dsr_trials = 1 if lane == "structural" and status == "proposed" else 0
    body = {
        "ts": moment.strftime("%Y-%m-%dT%H:%M:%S"),
        "bot_id": bot_id,
        "month": month,
        "lane": lane,
        "form": form or "",
        "status": status,
        "applied": False,
        "settings_written": False,
        "allowlist_written": False,
        "strategy_store_saved": False,
        "pool_promoted": False,
        "dsr_trials": dsr_trials,
        "source_code": code_text,
        "evidence": payload,
    }
    path = _proposal_path(root, bot_id, month, lane, moment)
    _atomic_json(path, body)
    if touch_slot and ledger_path:
        ledger = load_ledger(ledger_path)
        if not ledger.get("unreadable"):
            _touch_slot(ledger, bot_id, month, form, status)
            if incubation is not None:
                _upsert_incubation(ledger, incubation)
            save_ledger(ledger_path, ledger)
    elif incubation is not None and ledger_path:
        ledger = load_ledger(ledger_path)
        if not ledger.get("unreadable"):
            _upsert_incubation(ledger, incubation)
            save_ledger(ledger_path, ledger)
    body["path"] = str(path)
    return body


def _upsert_incubation(ledger: dict, incubation: dict) -> None:
    """Insert or replace one incubation. ``applied`` stays false."""
    row = dict(incubation)
    row["applied"] = False
    row.pop("applied_at", None)
    incubations = [
        inc for inc in (ledger.get("incubations") or [])
        if not (
            isinstance(inc, dict)
            and inc.get("bot_id") == row.get("bot_id")
            and inc.get("month") == row.get("month")
        )
    ]
    incubations.append(row)
    ledger["incubations"] = incubations


def format_proposal_notice(body: dict) -> str:
    """Chat / Discord line. States that nothing was applied."""
    return (
        "EVO PROPOSAL only — nothing applied. No strategy, settings, "
        "allowlist, StrategyStore, or StrategyPool change. "
        f"lane={body.get('lane')} status={body.get('status')} "
        f"form={body.get('form') or '-'} "
        f"dsr_trials={body.get('dsr_trials')}."
    )


def structural_paper_decision(*, sandbox_ok: bool, native_bars: int,
                              q2_passed: bool, pbo, dsr,
                              config: dict | None = None) -> tuple[bool, list[str]]:
    """Whether a structural candidate may be *recommended* for paper.

    A pass here still does not promote. Missing PBO or DSR fails closed.
    """
    cfg = config or default_lane_config()
    reasons: list[str] = []
    ok = True
    if sandbox_ok:
        reasons.append("sandbox: pass")
    else:
        reasons.append("sandbox: FAIL")
        ok = False
    if passes_native_bar_guard(native_bars):
        reasons.append(f"native bars {native_bars} ≥ {MIN_NATIVE_BARS}")
    else:
        reasons.append(f"native bars {native_bars} < {MIN_NATIVE_BARS}")
        ok = False
    if q2_passed:
        reasons.append("Q2 relative checks: pass")
    else:
        reasons.append("Q2 relative checks: FAIL")
        ok = False
    pbo_max = float(cfg.get("pbo_max", DEFAULT_PBO_MAX))
    dsr_min = float(cfg.get("dsr_min", DEFAULT_DSR_MIN))
    if pbo is None or dsr is None:
        reasons.append(
            "PBO/DSR not supplied (trial-penalty change); "
            "not treated as a pass")
        ok = False
    else:
        try:
            pbo_v = float(pbo)
            dsr_v = float(dsr)
        except (TypeError, ValueError):
            pbo_v = dsr_v = float("nan")
        pbo_ok = pbo_v == pbo_v and pbo_v < pbo_max
        dsr_ok = dsr_v == dsr_v and dsr_v > dsr_min
        reasons.append(
            f"PBO {pbo_v} < {pbo_max:g}: {'pass' if pbo_ok else 'FAIL'}")
        reasons.append(
            f"DSR {dsr_v} > {dsr_min:g}: {'pass' if dsr_ok else 'FAIL'}")
        ok = ok and pbo_ok and dsr_ok
    reasons.append(
        "paper entry is a recommendation only; nothing is promoted")
    return ok, reasons


class _TradeView:
    def __init__(self, pnl: int, exit_dt: str):
        self.pnl = int(pnl)
        self.exit_dt = exit_dt


def _as_trades(items) -> list:
    out = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(_TradeView(
                int(item.get("pnl") or 0), str(item.get("exit_dt") or "")))
        else:
            out.append(item)
    return out


def _parse_exit(value: str) -> datetime | None:
    raw = str(value or "").strip()
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16),
                   ("%Y-%m-%d", 10)):
        if len(raw) < n:
            continue
        try:
            return datetime.strptime(raw[:n], fmt)
        except ValueError:
            continue
    return None


def _week_pnl(trades) -> dict[str, int]:
    out: dict[str, int] = {}
    for trade in trades:
        dt = _parse_exit(getattr(trade, "exit_dt", ""))
        if dt is None:
            continue
        year, week, _day = dt.isocalendar()
        key = f"{year}-W{week:02d}"
        out[key] = out.get(key, 0) + int(getattr(trade, "pnl", 0) or 0)
    return out


def _slippage_not_worse(profile) -> tuple[bool, str]:
    """Candidate slippage and timeouts must be ≤ the baseline's.

    A missing profile fails this criterion. There is no trading-review
    reader in this module; the caller supplies the two numbers.
    """
    if not isinstance(profile, dict):
        return False, "slippage/timeout profile missing — not recommended"
    cand = profile.get("candidate")
    base = profile.get("baseline")
    if not isinstance(cand, dict) or not isinstance(base, dict):
        return False, "slippage/timeout profile missing a side — not recommended"
    try:
        c_slip = float(cand.get("slippage"))
        b_slip = float(base.get("slippage"))
        c_to = int(cand.get("timeouts"))
        b_to = int(base.get("timeouts"))
    except (TypeError, ValueError):
        return False, "slippage/timeout profile is not numeric — not recommended"
    ok = c_slip <= b_slip and c_to <= b_to
    return ok, (
        f"slippage {c_slip:g}≤{b_slip:g} and timeouts {c_to}≤{b_to}: "
        f"{'pass' if ok else 'FAIL'}"
    )


def assess_graduation(*, paper_trades, baseline_trades, sessions: int,
                      backtest_mean_pnl, slippage,
                      config: dict | None = None) -> GraduationRecommendation:
    """Recommend graduation, inconclusive, or keep waiting. Applies nothing."""
    from .pipeline import window_stats

    cfg = config or default_lane_config()
    paper = _as_trades(paper_trades)
    base = _as_trades(baseline_trades)
    n_sessions = int(sessions or 0)
    n_trades = len(paper)
    min_sessions = int(cfg.get("min_paper_sessions") or DEFAULT_MIN_PAPER_SESSIONS)
    min_trades = int(cfg.get("min_paper_trades") or DEFAULT_MIN_PAPER_TRADES)
    timeout = int(cfg.get("timeout_sessions") or DEFAULT_TIMEOUT_SESSIONS)
    reasons = [
        "GRADUATION RECOMMENDATION only — no strategy, settings, or "
        "allowlist was changed. This is a CEO call.",
        f"thresholds: sessions≥{min_sessions} trades≥{min_trades} "
        f"timeout at {timeout} pf≥{float(cfg.get('grad_pf') or DEFAULT_GRAD_PF):g} "
        f"weekly≥{int(cfg.get('grad_weekly_wins') or DEFAULT_GRAD_WEEKLY_WINS)}"
        f"/{int(cfg.get('grad_weekly_of') or DEFAULT_GRAD_WEEKLY_OF)} "
        f"or {float(cfg.get('grad_majority') or DEFAULT_GRAD_MAJORITY):.0%} "
        f"dd≤{float(cfg.get('grad_dd_ratio') or DEFAULT_GRAD_DD_RATIO):g}× "
        f"decay≥{float(cfg.get('grad_decay') or DEFAULT_GRAD_DECAY):g}",
    ]
    paper_stats = window_stats(paper)
    base_stats = window_stats(base)
    criteria_ok, detail = _graduation_criteria(
        paper, base, paper_stats, base_stats, backtest_mean_pnl, slippage, cfg)
    reasons.extend(detail)
    ready = n_sessions >= min_sessions and n_trades >= min_trades
    if n_sessions > timeout:
        status = "inconclusive"
        reasons.append(
            f"window closed: {n_sessions} sessions > timeout {timeout}; "
            "close as inconclusive")
    elif ready and criteria_ok and n_sessions <= timeout:
        status = "graduation_recommended"
        reasons.append(
            "criteria met inside the paper window — recommend graduation; "
            "do not apply it")
    elif n_sessions >= timeout:
        status = "inconclusive"
        reasons.append(
            f"timeout at {n_sessions} sessions without the graduation "
            "criteria; close as inconclusive")
    else:
        status = "open"
        reasons.append(
            f"not ready: sessions {n_sessions}/{min_sessions} "
            f"trades {n_trades}/{min_trades}")
    evidence = {
        "applied": False,
        "sessions": n_sessions,
        "trades": n_trades,
        "paper_pf": paper_stats.profit_factor,
        "paper_pnl": paper_stats.pnl,
        "baseline_pnl": base_stats.pnl,
        "thresholds": {
            "min_paper_sessions": min_sessions,
            "min_paper_trades": min_trades,
            "timeout_sessions": timeout,
            "grad_pf": cfg.get("grad_pf"),
            "grad_dd_ratio": cfg.get("grad_dd_ratio"),
            "grad_decay": cfg.get("grad_decay"),
        },
    }
    return GraduationRecommendation(
        status=status,
        recommend=status in ("graduation_recommended", "inconclusive"),
        reasons=reasons,
        evidence=evidence,
    )


def _graduation_criteria(paper, base, paper_stats, base_stats,
                         backtest_mean_pnl, slippage, cfg) -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True
    pf_floor = float(cfg.get("grad_pf") or DEFAULT_GRAD_PF)
    pf = paper_stats.profit_factor
    pf_ok = pf == float("inf") or (pf == pf and pf >= pf_floor)
    pnl_ok = paper_stats.pnl > 0
    lines.append(
        f"paper PF {pf if pf != float('inf') else 'inf'} ≥ {pf_floor:g} "
        f"and net {paper_stats.pnl} > 0: "
        f"{'pass' if pf_ok and pnl_ok else 'FAIL'}")
    ok = ok and pf_ok and pnl_ok

    delta = paper_stats.pnl - base_stats.pnl
    weeks_c = _week_pnl(paper)
    weeks_b = _week_pnl(base)
    keys = sorted(set(weeks_c) | set(weeks_b))
    wins = sum(1 for key in keys if weeks_c.get(key, 0) > weeks_b.get(key, 0))
    need_n = int(cfg.get("grad_weekly_of") or DEFAULT_GRAD_WEEKLY_OF)
    need_wins = int(cfg.get("grad_weekly_wins") or DEFAULT_GRAD_WEEKLY_WINS)
    majority = float(cfg.get("grad_majority") or DEFAULT_GRAD_MAJORITY)
    if len(keys) < need_n:
        week_ok = False
    elif len(keys) == need_n:
        week_ok = wins >= need_wins
    else:
        week_ok = (wins / len(keys)) >= majority
    delta_ok = delta > 0
    lines.append(
        f"paired Δ {delta} > 0 and weekly wins {wins}/{len(keys)}: "
        f"{'pass' if week_ok and delta_ok else 'FAIL'}")
    ok = ok and week_ok and delta_ok

    ratio = float(cfg.get("grad_dd_ratio") or DEFAULT_GRAD_DD_RATIO)
    limit = ratio * base_stats.max_drawdown
    dd_ok = paper_stats.max_drawdown <= limit
    lines.append(
        f"paper max DD {paper_stats.max_drawdown} ≤ {ratio:g} × "
        f"baseline {base_stats.max_drawdown}: "
        f"{'pass' if dd_ok else 'FAIL'}")
    ok = ok and dd_ok

    slip_ok, slip_line = _slippage_not_worse(slippage)
    lines.append(slip_line)
    ok = ok and slip_ok

    decay = float(cfg.get("grad_decay") or DEFAULT_GRAD_DECAY)
    try:
        backtest_mean = float(backtest_mean_pnl)
    except (TypeError, ValueError):
        backtest_mean = float("nan")
    if paper_stats.n <= 0 or backtest_mean != backtest_mean or backtest_mean <= 0:
        decay_ok = False
        paper_mean = None
    else:
        paper_mean = paper_stats.pnl / paper_stats.n
        decay_ok = paper_mean >= decay * backtest_mean
    lines.append(
        f"paper per-trade {paper_mean} ≥ {decay:g} × backtest per-trade "
        f"{backtest_mean}: {'pass' if decay_ok else 'FAIL'}")
    ok = ok and decay_ok
    return ok, lines


def format_graduation_recommendation(rec: GraduationRecommendation) -> str:
    lines = list(rec.reasons)
    lines.append("action: none")
    return "\n".join(lines)


def emit_graduation_recommendation(rec: GraduationRecommendation, *,
                                   log=None, changelog=None,
                                   report: list | None = None,
                                   strategy_name: str = "",
                                   version: str = "unchanged") -> None:
    """Write the recommendation through the callables only.

    No settings path, no allowlist, no pool, no strategy file. When
    there is nothing to surface this is a no-op.
    """
    if not rec.recommend:
        return
    text = format_graduation_recommendation(rec)
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


def review_incubation(incubation: dict, observation: dict | None,
                      config: dict | None = None) -> GraduationRecommendation:
    """Judge one stored incubation from an explicit observation.

    ``observation`` is None when nobody supplied paper trades. Session
    counts already stored on the row are used, and missing paper trades
    cannot meet the graduation criteria.
    """
    obs = observation or {}
    sessions = obs.get("sessions", incubation.get("sessions") or 0)
    paper = obs.get("paper_trades")
    base = obs.get("baseline_trades")
    backtest_mean = obs.get(
        "backtest_mean_pnl", incubation.get("backtest_mean_pnl"))
    slippage = obs.get("slippage")
    return assess_graduation(
        paper_trades=paper,
        baseline_trades=base,
        sessions=int(sessions or 0),
        backtest_mean_pnl=backtest_mean,
        slippage=slippage,
        config=config,
    )


def structural_saturday_followup(ledger_path, *, observations=(),
                                 config: dict | None = None,
                                 log=None, changelog=None,
                                 report: list | None = None,
                                 strategy_name: str = "",
                                 version: str = "unchanged") -> list[str]:
    """Review incubations and emit recommend-only text.

    Writes the ledger only when a stored row's status or session count
    changes. Does not write settings, an allowlist, or a strategy.
    A missing ledger is a no-op. The process-wide ledger lock is held
    from the read through the save. This does not take a file lock.
    """
    with _LEDGER_LOCK:
        return _structural_saturday_followup_locked(
            ledger_path, observations=observations, config=config, log=log,
            changelog=changelog, report=report, strategy_name=strategy_name,
            version=version)


def _structural_saturday_followup_locked(ledger_path, *, observations=(),
                                         config: dict | None = None,
                                         log=None, changelog=None,
                                         report: list | None = None,
                                         strategy_name: str = "",
                                         version: str = "unchanged") -> list[str]:
    if not ledger_path or not Path(ledger_path).exists():
        return []
    ledger = load_ledger(ledger_path)
    _pause_ledger_write()
    if ledger.get("unreadable"):
        return []
    cfg = config or default_lane_config()
    obs_by = {}
    for obs in observations or []:
        if isinstance(obs, dict):
            obs_by[(obs.get("bot_id"), obs.get("month"))] = obs
    # No observation → do not invent a session count and do not rewrite
    # a recommendation that was stored earlier.
    if not obs_by:
        return []
    changed = False
    notices: list[str] = []
    for inc in ledger.get("incubations") or []:
        if not isinstance(inc, dict):
            continue
        obs = obs_by.get((inc.get("bot_id"), inc.get("month")))
        if obs is None:
            continue
        rec = review_incubation(inc, obs, cfg)
        if rec.status != inc.get("status"):
            inc["status"] = rec.status
            inc["applied"] = False
            inc.pop("applied_at", None)
            inc["recommendation"] = list(rec.reasons)
            changed = True
        if obs is not None:
            inc["sessions"] = int(obs.get("sessions") or 0)
            if obs.get("paper_trades") is not None:
                inc["trades"] = len(list(obs.get("paper_trades") or []))
            inc["applied"] = False
            inc.pop("applied_at", None)
            changed = True
        report_before = len(report) if report is not None else 0
        emit_graduation_recommendation(
            rec, log=log, changelog=changelog, report=report,
            strategy_name=strategy_name, version=version)
        if rec.recommend and report is not None and len(report) > report_before:
            notices.append(report[-1])
        elif rec.recommend and report is None:
            notices.append(format_graduation_recommendation(rec))
    if changed:
        save_ledger(ledger_path, ledger)
    return notices


def applied_graduation_stamps(ledger: dict | None) -> list[str]:
    """Stamps of graduations that were actually applied.

    A recommendation does not set ``applied_at``. This list stays empty
    until a human records an application, which this module never does.
    """
    stamps = []
    for inc in (ledger or {}).get("incubations") or []:
        if not isinstance(inc, dict):
            continue
        stamp = str(inc.get("applied_at") or "").strip()
        if stamp:
            stamps.append(stamp)
    return stamps


def make_incubation(*, bot_id: str, month: str, form: str, status: str,
                    backtest_mean_pnl=None, now: datetime | None = None) -> dict:
    """An incubation row. ``applied`` is false. No ``applied_at``."""
    moment = now or datetime.now()
    return {
        "bot_id": bot_id,
        "month": month,
        "form": form,
        "status": status,
        "sessions": 0,
        "trades": 0,
        "backtest_mean_pnl": backtest_mean_pnl,
        "at": moment.strftime("%Y-%m-%dT%H:%M:%S"),
        "applied": False,
    }
