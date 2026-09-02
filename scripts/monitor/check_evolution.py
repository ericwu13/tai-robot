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
    TZ_TPE, Finding, bot_name, default_base_dir, discover_bot_dirs,
    guard_stdout, last_activity, pid_alive, print_report, read_json,
    read_lock_pid, to_tpe,
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


def _check_usage(now, due, usage, feature_in_use, path, lines, findings) -> None:
    lines.append("--- usage evidence")
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
        if feature_in_use:
            findings.append(Finding(
                "P2", "evolution",
                f"weekly evolution did not start on {due_day} (GUI closed, no "
                f"eligible non-regime bot RUNNING at 05:05 TPE Sat, or "
                f"auto_pipeline off)",
                path))
        else:
            lines.append("  (no watermark anywhere — evolution has never been "
                         "used in this tree, so a silent Saturday is expected)")
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

    _check_usage(now, due, usage, feature_in_use, usage_path, lines, findings)
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
