---
name: weekly-review
description: Orchestrate the weekly tai-robot review — did the Saturday evolution pipeline actually run and how far did it get, plus a week-over-week rollup of daily-review verdicts — validated, then published to Discord. Use on/after TPE Saturday morning, or when asked whether evolution is working this week.
---

# Weekly Review — the orchestrator

Same shape as `daily-review` (which stays daily and does NOT cover
evolution): fetch → review → validate → publish, each stage delegated to
its skill. Keep it light — a healthy week costs one script run and zero
or one subagent. Hard rules are inherited from `daily-review` (main tree
read-only, no merges, redact everything).

## Stage 1 — fetch (no subagents)

```bash
C:/Python313/python.exe -s scripts/monitor/check_evolution.py
```

Plus a cheap week rollup: list `data\monitor\daily_review_*.md` for the
last 7 TPE days and extract each file's `**verdict: ...**` line (one
Grep). Note any day with no file (review didn't run).

**No P1/P2 from the evolution check and no RED day in the rollup → skip
to stage 3's inline validation, publish a one-line green digest.**

## Stage 2 — review (0–1 subagent)

For evolution findings, one read-only Explore agent (model sonnet)
follows `.claude/skills/evo-debug/SKILL.md` — pass it the findings and
the evidence map; it answers which gate blocked the Saturday run (or
which phase the run died in), whether that is by-design or an outage,
and the single operator action. RED days in the rollup are already
explained inside their daily report file — read that file, don't re-run
the checks.

## Stage 3 — validate

`.claude/skills/validate-findings/SKILL.md` on every digest claim.
Evolution-specific traps: ai_usage.csv timestamps are **UTC** (TPE −8);
watermark/baseline stamps are TPE; a frozen `evolution.json` and a
"no new trades" skip are both BY DESIGN, not outages; regime-bot silence
is by design.

## Stage 4 — publish

Digest ≤1900 chars: `🧬 tai-robot weekly review — week of <TPE Saturday
date> (Claude)`, then: whether Saturday's evolution ran and the phase it
reached (plan / codegen / verdict, PASS or FAIL), per-bot watermark
movement, next-Saturday eligibility (any non-regime bot running?), the
week's daily verdicts as a compact line (e.g. `Mon 🟢 Tue 🟡 …`), and one
recommended action if any. Delivery identical to `daily-review` stage 4
(monitor channel via bot token when configured, else the news webhook;
throwaway scratchpad script; never store/echo secrets).

## Stage 5 — fix kickoff (rare)

Only via `.claude/skills/fix-pr/SKILL.md` gates and only for CODE
defects. Verdict-gate tuning, cadence changes, and "should regime bots
evolve" are product decisions — present evidence, ask the user.

## Timing

Evolution finishes by ~05:10 TPE Saturday. Run this **Saturday
05:30–06:00 TPE = Friday ~14:30–15:00 local**. The scheduled task uses
local time.
