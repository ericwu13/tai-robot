"""Strategy-evolution pipeline health (read-only).

Answers "did the weekly evolution actually run, and can it run next
Saturday?".

Three facts drive everything:

- the pipeline is GUI-driven and per-bot.  It fires from the Tk app's
  status poll on TPE Saturdays: ~04:58 the fitness check may rewrite
  ``evolution.json``, and from 05:05 the AI pipeline runs.  No GUI, no
  run — there is no service, no cron, nothing to restart.
- ``evolution_watermark.json`` is rewritten at the END of every attempt,
  even a failed one, so its ``at`` stamp is the honest "last attempt"
  clock.  ``data/ai_usage.csv`` is the only record of how FAR an attempt
  got: a ``bot_evolution`` row means the plan phase reached the AI, an
  ``evolution_codegen_*`` row means codegen did.
- an empty ledger does NOT mean the slot never fired.  Every early
  return in ``_bot_evolution`` — no trades, and the holdout skip that
  fires whenever the week's trades all sit inside the withheld window —
  happens before the first AI call, so a run that completed exactly as
  designed leaves ai_usage.csv untouched and the watermark frozen.  The
  bot's debug log is what separates "fired and self-limited" from "never
  fired": the Tk app writes a start line the moment the slot fires.  Do
  NOT infer a cause from the ledger alone.  It reported "GUI closed" for
  the 2026-09-05 slot, when both bots had in fact run and taken the
  holdout skip — identified from the Discord message lengths (203/207
  chars) matching that branch's template to the character.
- regime bots are excluded from evolution BY DESIGN, and a run with no
  new trades outside the holdout window silently skips.  Neither is a
  failure, so neither may raise a finding on its own.

Nothing here mutates state: watermarks, baselines and the usage ledger
are only ever read.
"""

from __future__ import annotations

import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import csv  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

from scripts.monitor.common import (  # noqa: E402
    TZ_TPE, Finding, bot_name, debug_log_open_date, default_base_dir,
    discover_bot_dirs, first_ts_in_line, guard_stdout, last_activity,
    newest_debug_logs, pid_alive, print_report, read_json, read_lock_pid,
    to_tpe,
)

# The AI pipeline runs from this Saturday slot (TPE); the fitness check
# that rewrites evolution.json fires ~7 min earlier.
DUE_WEEKDAY = 5          # Saturday, matching datetime.weekday()
DUE_HOUR = 5
DUE_MINUTE = 5

# A watermark older than TWO due-Saturdays means the bot skipped a whole
# cycle, not just the most recent one.
STALE_WATERMARK_DAYS = 7.0

# "Recent activity" for the stale-watermark gate: data/live is full of
# retired test-bot directories whose frozen watermarks would otherwise
# report forever.
RECENT_ACTIVITY_DAYS = 7.0

PLAN_CALL_SITE = "bot_evolution"
CODEGEN_PREFIX = "evolution_codegen"

# The ASCII half of the start line the Tk app logs when the slot fires
# ("🧬 週末自動演化 Weekly auto-evolution (post-close) starting...").
# Matching the English keeps this independent of the log's encoding, and
# carrying "(post-close) starting" keeps it clear of the lower-cased
# "weekly auto-evolution failed" the failure path writes.
START_MARKER = "Weekly auto-evolution (post-close) starting"


def last_due(now: datetime) -> datetime:
    """The most recent Saturday 05:05 TPE that is already in the past.

    On a Saturday BEFORE 05:05 the answer is the previous Saturday — the
    week's run has not had its chance yet, so grading it as missed would
    fire a false P2 every Saturday morning.
    """
    now = to_tpe(now)
    anchor = now.replace(hour=DUE_HOUR, minute=DUE_MINUTE, second=0, microsecond=0)
    anchor -= timedelta(days=(now.weekday() - DUE_WEEKDAY) % 7)
    if anchor > now:
        anchor -= timedelta(days=7)
    return anchor


def read_usage(path: str):
    """``{TPE date -> {"plan": n, "codegen": n}}`` from data/ai_usage.csv.

    ``None`` when the ledger is missing or unreadable.  Timestamps are
    UTC ISO (``2026-08-28T21:05:49+00:00``) and MUST be converted before
    the date is taken: the 05:05 TPE Saturday slot lands on the previous
    UTC calendar day.
    """
    if not path or not os.path.isfile(path):
        return None
    runs = {}
    try:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.reader(f):
                if len(row) < 2:
                    continue
                site = (row[1] or "").strip()
                is_plan = site == PLAN_CALL_SITE
                is_codegen = site.startswith(CODEGEN_PREFIX)
                if not (is_plan or is_codegen):
                    continue
                try:
                    stamp = datetime.fromisoformat((row[0] or "").strip())
                except ValueError:
                    continue
                day = to_tpe(stamp).date()
                bucket = runs.setdefault(day, {"plan": 0, "codegen": 0})
                bucket["plan" if is_plan else "codegen"] += 1
    except OSError:
        return None
    return runs


def _spans_due(path: str, due: datetime) -> bool:
    """Could this debug log contain ``due``'s slot?

    True when the deploy opened the log on or before the due day AND was
    still writing to it at or after the slot.  Skipping the rest is what
    keeps a "never started" answer cheap on a tree full of retired bots
    whose logs run to tens of megabytes.
    """
    opened = debug_log_open_date(path)
    if opened is None or opened > due.date():
        return False
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path), TZ_TPE)
    except OSError:
        return False
    return mtime >= due


def find_start(bot_dir: str, due: datetime):
    """TPE timestamp of this bot's evolution start line for ``due``'s day.

    ``None`` when the slot left no start line — the bot was not running,
    the GUI was closed, or auto_pipeline is off.  Scans forward rather
    than tailing: the slot can sit far from the end of a log that kept
    growing for days afterwards.

    Every log is offered to ``_spans_due``; capping the list would drop
    the slot's file on a bot redeployed often enough that day (one bot
    here has 33).  The cap was never what made this cheap — the filter
    is, at one ``stat`` per file.
    """
    due = to_tpe(due)
    for path in newest_debug_logs(bot_dir, None):
        if not _spans_due(path, due):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if START_MARKER not in line:
                        continue
                    ts = first_ts_in_line(line)
                    if ts is not None and ts.date() == due.date():
                        return ts
        except OSError:
            continue
    return None


def due_slot_starts(base_dir: str, due: datetime):
    """``[(bot_name, started_at)]`` for every bot that logged a start."""
    due = to_tpe(due)
    starts = []
    for bot_dir in discover_bot_dirs(base_dir):
        ts = find_start(bot_dir, due)
        if ts is not None:
            starts.append((bot_name(bot_dir), ts))
    starts.sort(key=lambda row: row[1])
    return starts


def _check_usage(now, due, usage, feature_in_use, path, starts,
                 lines, findings) -> None:
    lines.append("--- usage evidence")
    started = ", ".join(f"{name} {ts.strftime('%H:%M:%S')}"
                        for name, ts in starts)
    lines.append(f"start line in debug logs: {started or 'none for this slot'}")
    if usage is None:
        lines.append(f"ai_usage.csv: missing or unreadable ({path})")
        findings.append(Finding(
            "P3", "evolution",
            "data/ai_usage.csv missing/unreadable — cannot tell how far any "
            "evolution attempt got (the AI ledger is the only record)",
            path))
        return

    if not usage:
        lines.append("ai_usage.csv: no evolution rows ever")
    else:
        last_day = max(usage)
        counts = usage[last_day]
        saturdays = sorted(d for d in usage if d.weekday() == DUE_WEEKDAY)
        lines.append(
            f"last evolution run: {last_day} TPE "
            f"(plan x{counts['plan']}, codegen x{counts['codegen']} — "
            f"codegen {'reached' if counts['codegen'] else 'NOT reached'})")
        lines.append(f"distinct run dates: {len(usage)} "
                     f"({len(saturdays)} on a Saturday)")

    due_day = due.date()
    counts = usage.get(due_day, {"plan": 0, "codegen": 0})
    lines.append(f"last due slot {due.strftime('%Y-%m-%d %H:%M')} TPE: "
                 f"plan x{counts['plan']}, codegen x{counts['codegen']}")

    if not counts["plan"]:
        if starts:
            # The slot fired and returned before the AI was called.  In
            # practice that is the holdout skip: the bot's recent trades
            # all sit inside the withheld window, so there is nothing new
            # to design on.  That branch is deliberate, it notifies
            # Discord itself, and this module's own contract says it may
            # not raise a finding on its own.  Report it and stop —
            # calling it a stall sends the operator hunting a failure
            # that did not happen.
            lines.append(f"  (fired and returned before the AI call — normally "
                         f"the by-design holdout skip; the bot's own 🧬 Discord "
                         f"message says which branch)")
        elif not feature_in_use:
            lines.append("  (no watermark anywhere — evolution has never been "
                         "used in this tree, so a silent Saturday is expected)")
        else:
            findings.append(Finding(
                "P2", "evolution",
                f"weekly evolution did not start on {due_day} — no start line "
                f"in any bot debug log (GUI closed, no eligible non-regime bot "
                f"RUNNING at 05:05 TPE Sat, or auto_pipeline off)",
                path))
        return

    if not counts["codegen"]:
        findings.append(Finding(
            "P3", "evolution",
            f"evolution ran on {due_day} but stopped at plan phase (plan said "
            f"no_change, or fitness-gated) — no candidate was generated",
            path))


def _watermarks(base_dir: str):
    """``[(bot_dir, watermark_or_None, baseline_or_None)]`` for every bot
    directory that has at least one evolution artefact."""
    rows = []
    for bot_dir in discover_bot_dirs(base_dir):
        mark = read_json(os.path.join(bot_dir, "evolution_watermark.json"))
        base = read_json(os.path.join(bot_dir, "evolution.json"))
        mark = mark if isinstance(mark, dict) else None
        base = base if isinstance(base, dict) else None
        if mark is None and base is None:
            continue          # regime bot or never evolved — not a failure
        rows.append((bot_dir, mark, base))
    return rows


def _parse_stamp(raw):
    """A watermark/baseline ``"YYYY-MM-DD HH:MM:SS"`` stamp (TPE), or None."""
    try:
        return datetime.strptime(str(raw).strip(),
                                 "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_TPE)
    except (TypeError, ValueError):
        return None


def _check_watermarks(now, due, rows, lines, findings) -> None:
    lines.append("--- watermarks / baselines")
    if not rows:
        lines.append("no evolution artefacts under the bot tree "
                     "(regime bots produce none BY DESIGN)")
        return

    cutoff = due - timedelta(days=STALE_WATERMARK_DAYS)
    for bot_dir, mark, base in rows:
        name = bot_name(bot_dir)
        if mark is None:
            mark_txt = "watermark none"
        else:
            mark_txt = (f"watermark at={mark.get('at', '?')} "
                        f"trade_count={mark.get('trade_count', '?')}")
        if base is None:
            base_txt = "baseline none"
        else:
            base_txt = (f"baseline composite={base.get('best_composite', '?')} "
                        f"n_updates={base.get('n_updates', '?')}")
        lines.append(f"{name}: {mark_txt} | {base_txt}")

        if mark is None:
            continue
        at = _parse_stamp(mark.get("at"))
        if at is None:
            lines.append("  (watermark 'at' unparsable — staleness not checked)")
            continue
        if at >= cutoff:
            continue
        seen = last_activity(bot_dir)
        if seen is None:
            lines.append("  (stale watermark, but the directory shows no recent "
                         "activity — archaeology)")
            continue
        idle_days = (to_tpe(now) - to_tpe(seen)).total_seconds() / 86400.0
        if idle_days > RECENT_ACTIVITY_DAYS:
            lines.append(f"  (stale watermark, last activity {idle_days:.0f} "
                         f"days ago — archaeology)")
            continue
        findings.append(Finding(
            "P2", "evolution",
            f"{name}: evolution has not attempted for 2+ weeks despite recent "
            f"activity (watermark {mark.get('at', '?')} TPE, last activity "
            f"{seen.strftime('%Y-%m-%d %H:%M')} TPE) — the Saturday slot is "
            f"not reaching this bot",
            os.path.join(bot_dir, "evolution_watermark.json")))


def _check_eligibility(base_dir, pid_alive_fn, lines, findings) -> None:
    """Could next Saturday's run fire at all?

    Eligibility = a RUNNING deploy that is not a regime bot.  Regime bots
    are excluded from evolution by design, so a tree full of live regime
    bots still cannot evolve anything.
    """
    lines.append("--- eligibility now")
    eligible = []
    for bot_dir in discover_bot_dirs(base_dir):
        has_lock, pid = read_lock_pid(bot_dir)
        if not has_lock or pid is None or not pid_alive_fn(pid):
            continue
        data = read_json(os.path.join(bot_dir, "session.json"))
        if isinstance(data, dict) and data.get("regime_mode"):
            continue
        eligible.append(bot_name(bot_dir))
    if eligible:
        lines.append(f"eligible (running, non-regime): {', '.join(eligible)}")
        return
    lines.append("eligible (running, non-regime): none")
    findings.append(Finding(
        "P2", "evolution",
        "no eligible (non-regime, running) bot — next Saturday's evolution "
        "cannot fire",
        base_dir))


def _check_zero_pass(repo_root, lines, findings) -> None:
    """Has the pipeline ever PASSed a candidate?

    A PASS writes data/changelog.json and registers an "Evo"-suffixed
    class in strategies/index.json.  Neither existing says something
    structural about the verdict gates rejecting every candidate — it is
    not an outage, so it is P3 and never gates the verdict.
    """
    lines.append("--- pass history")
    changelog = os.path.join(repo_root, "data", "changelog.json")
    index_path = os.path.join(repo_root, "strategies", "index.json")
    has_changelog = os.path.isfile(changelog)

    entries = read_json(index_path)
    evo = []
    if isinstance(entries, list):
        evo = [e.get("class_name") for e in entries
               if isinstance(e, dict) and "Evo" in str(e.get("class_name", ""))]

    lines.append(f"changelog.json: {'present' if has_changelog else 'absent'}")
    lines.append(f"Evo-suffixed strategies in index.json: "
                 f"{', '.join(evo) if evo else 'none'}")
    if has_changelog or evo:
        return
    findings.append(Finding(
        "P3", "evolution",
        "pipeline has never PASSed a candidate — verdict gates have rejected "
        "every candidate to date (structural, not an outage)",
        changelog))


def check_evolution(now: datetime, base_dir: str, repo_root: str = _REPO,
                    pid_alive_fn=pid_alive):
    """``(findings, report_lines)`` for the weekly evolution pipeline."""
    findings = []
    lines = []

    due = last_due(now)
    lines.append(f"now: {now.strftime('%Y-%m-%d %H:%M:%S')} TPE")
    lines.append(f"last due slot: {due.strftime('%Y-%m-%d %H:%M')} TPE "
                 f"(Saturday 05:05, GUI-driven)")
    lines.append("")

    rows = _watermarks(base_dir)
    usage_path = os.path.join(repo_root, "data", "ai_usage.csv")
    usage = read_usage(usage_path)
    feature_in_use = any(mark is not None for _, mark, _ in rows)

    starts = due_slot_starts(base_dir, due)

    _check_usage(now, due, usage, feature_in_use, usage_path, starts,
                 lines, findings)
    lines.append("")
    _check_watermarks(now, due, rows, lines, findings)
    lines.append("")
    _check_eligibility(base_dir, pid_alive_fn, lines, findings)
    lines.append("")
    _check_zero_pass(repo_root, lines, findings)
    return findings, lines


def main() -> int:
    guard_stdout()
    from scripts.monitor.common import now_tpe
    base_dir = sys.argv[1] if len(sys.argv) > 1 else default_base_dir()
    repo_root = sys.argv[2] if len(sys.argv) > 2 else _REPO
    findings, lines = check_evolution(now_tpe(), base_dir, repo_root)
    print_report("evolution health", lines, findings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
