---
name: weekly-review
description: Orchestrate the weekly tai-robot review — did the Saturday evolution pipeline actually run and how far did it get, plus a week-over-week rollup of daily-review verdicts — validated, then published to Discord. Use on/after TPE Saturday morning, or when asked whether evolution is working this week.
---

# Weekly Review — the orchestrator

> **Where the data is (since 2026-10-24/25):** the live fleet (12 headless
> `tai_backtest.exe` bots), the bot dashboard and the W1–W4 bridge tasks
> run on **Steven**, not this PC. This PC's `data\live` holds only
> retired/test bots and its n8n is stopped. Read live state from the
> dashboard API (`http://<Steven tailnet IP>:8765/api/fleet`,
> `/api/bot?id=<SYMBOL>_<bot>`; address in `hosts.local.yaml`). No script
> runs on Steven by default (`start-here` §7). A run of a
> checker on this PC describes this PC only — never publish it as fleet
> health. Host map → `start-here`. Run checkers from an up-to-date checkout
> (`git rev-list --count HEAD..origin/master` = 0); the main tree may be
> weeks behind.

Data host → `start-here` §7; name the host in the output.

Same shape as `daily-review` (which stays daily and does NOT cover
evolution): fetch → review → validate → publish, each stage delegated to
its skill. Keep it light — a healthy week costs one script run and zero
or one subagent. Hard rules are inherited from `daily-review` (main tree
read-only, no merges, redact everything).

## Stage 1 — fetch (no subagents)

`check_evolution.py` writes nothing (`scripts/monitor/check_evolution.py:40-41`;
pool open is `mode=ro` at :341; `main()` only prints, :627-634). With
explicit permission in the brief, read-only, on the data host, using that
host's own interpreter (not a path from this PC):

```bash
python -s scripts/monitor/check_evolution.py <install_dir>\data\live <install_dir>
```

`<install_dir>` is `steven.install_dir` in `hosts.local.yaml`. `argv[1]`
is base_dir and `argv[2]` is repo_root (`check_evolution.py:630-631`).

Daily reports are written only by `daily_review.py`, to
`<repo>/data/monitor/daily_review_<date>.md`, and only when that script
runs without `--no-report-file` (`scripts/monitor/daily_review.py:269-270`,
`:285-289`). The repo does not put those files on the data host.

Fallback, this PC only:

```bash
C:/Python313/python.exe -s scripts/monitor/check_evolution.py
```

**Plus the GitHub issue sweep (MANDATORY every run):**

```bash
gh issue list --state all --limit 20 --json number,title,createdAt,updatedAt,author,state
```

Classify every issue created or updated in the last 7 days. An issue is
**EVO-related** when its title/body/comments mention 演化 / evolution /
EVO / 🧬, when it was filed on a Saturday after the 05:05 TPE slot, or
when it attaches `bug_report_*.zip` bundles whose names carry strategy
names (the issue #114 pattern: 7 bundles, one per bot). For each
EVO-related issue read the body AND comments (`gh issue view N --json
body,comments`) — other users paste the 工作臺 chat log there (issue #94
shows the full plan prompt; #99 the verdict/backtest report), which is
evidence this machine never has. Bundles: reading them needs an explicit
download OK in chat (filename, source, size) — in an unattended run do
NOT download; classify from the issue text + local evidence and mark
"bundle not fetched (needs permission)". Every EVO-related issue gets a
line in the digest (bot count, phase reached per bot, whether it changes
this week's finding) and feeds stage 2b. Non-EVO issues are the daily
review's job — list them in one line, don't triage.

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

## Stage 2b — evolution EFFICACY audit (every week, cheap; 2 agents when triggered)

"Did the slot fire" (stage 1) is not "is evolution working". Every week
also answer, from primary evidence only. `data/ai_usage.csv` and
`data/live/<bot>/session.json` are on the data host, not this PC. A read
of this PC's copies is this PC only.

1. **Attempt history** — the data host's `data/ai_usage.csv` (UTC; TPE =
   +8): per past Saturday, plan rows (`bot_evolution`) vs candidate rows
   (`evolution_codegen_*`). A Saturday with a plan but no codegen row =
   plan said `no_change`; no rows at all = skip or app closed (check the
   start line). Report the streak of Saturdays with no candidate.
2. **Verdict outcome** — the verdict block is NOT persisted anywhere
   local (the Tk chat auto-save only fires at window close, and the bot
   token cannot read Discord history: HTTP 403). The only on-disk trace is
   the Discord send in the data host's bot debug log ~60-90s after the
   start line with `msg_len` ≈ 1000-1500 (FAIL/PASS verdict); 112-130 = no_change,
   ~200-300 = holdout / young-session skip. A PASS would also create
   `data/changelog.json` and an `...EvoN` entry in the strategy store.
3. **P&L vs evolution** — weekly realized P&L (by exit date) of every
   running non-regime bot for the last 8 weeks from the data host's
   `data/live/<bot>/session.json` broker.trades. Losing streak + zero
   candidates = the evolution loop is not doing its job, whatever the
   reason.
4. **Cross-user evidence** — the stage-1 issue sweep's EVO-related
   issues. Their bundles' `debug_YYYYMMDD.log` carry the same start line
   / Discord `msg_len` signature as ours (see the evo-debug skill's
   signature table), so classify each bot the same way; pasted chat logs
   in the issue give the plan prompt / verdict block directly. Another
   user's bots post to the SAME evolution channel, so their verdicts sit
   next to ours in Discord.

**Trigger for the deep audit** (any one): ≥4 consecutive Saturdays with
no candidate; ≥8 weeks with candidates but zero PASS; a running bot is
net-negative over the last 4 weeks while evolution stays silent; the
owner asks "is the EVO algorithm correct". When triggered, launch TWO
independent agents in parallel (model fable — fall back to opus on a usage-credit error and say so; read-only, scripts in the
scratchpad only):

- **Pipeline-correctness reviewer** (Explore): `parse_plan_directives`
  default/format sensitivity, plan-prompt bias toward `no_change`, gate
  reachability on a 14-day holdout (trade floor, MIN_EXPRESSED_TRADES,
  ABS_PF_FLOOR, plan criteria, Monte Carlo sub-verdict, AND-chain),
  design-cut starvation (cut anchored on the LAST exit — a low-frequency
  bot's design window can stop growing), silent exception swallowing,
  determinism, and whether any test pins a realistic PASS.
- **Empirical gate-reachability check** (general-purpose): replicate the
  pipeline's `run_deep_validation` + `decide_deep_verdict` on a real
  bot's 1-min CSV history with candidate = baseline (must fail only on
  divergence), a sliding-holdout table (how often the baseline itself
  clears the floors), a single-parameter perturbation family (does ANY
  neighbour pass; which gate kills most), and a determinism re-run.

Both reports go through stage 3. Their conclusions separate **code
defect** (→ stage 5 fix-pr) from **design limitation** (→ present
evidence, the owner decides gate/holdout/cadence changes — never tune
autonomously).

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
week's daily verdicts as a compact line (e.g. `Mon 🟢 Tue 🟡 …`), the
stage-2b efficacy line (candidate streak, PASS count, P&L trend, deep-audit
verdict when it ran), and one recommended action if any. Delivery identical to `daily-review` stage 4
(monitor channel via bot token when configured, else the news webhook;
throwaway scratchpad script; never store/echo secrets).

## Stage 5 — fix kickoff (rare)

Only via `.claude/skills/fix-pr/SKILL.md` gates and only for CODE
defects. Verdict-gate tuning, cadence changes, and "should regime bots
evolve" are product decisions — present evidence, ask the user.

## Timing

Evolution finishes by ~05:10 TPE Saturday. Run this **Saturday
05:30–06:00 TPE**; convert with `start-here` §4 (check `date +%z` / `Get-Date -Format K`).
The scheduled task uses local time.
