---
name: debug
description: Structured 4-phase debug workflow for tai-robot — investigate a live-bot symptom (Fable), implement a fix on a fix/ branch (Opus), review the diff (Fable), then ship a versioned GitHub release with Discord notification. Use when a deployed bot misbehaves or a bug needs root-causing end to end.
---

# tai-robot Debug Workflow

A repo-specific, four-phase workflow for taking a live-bot symptom to a shipped fix.
Each phase names the model to use. Do not skip phases — the review phase catches
non-regime bot regressions, and shipping without the full test suite is not allowed.

## Security reminder (applies to EVERY phase)
- Never hardcode channel IDs or role IDs in source files. They live only in `settings.yaml` (gitignored). `settings.example.yaml` keeps empty placeholders.
- Never push channel IDs, role IDs, or any Discord credentials to git.
- Never paste real user IDs / account numbers into logs, tests, or commit messages.

---

## Phase 1 — Investigate (model: `claude-fable-5`)
Goal: trace the symptom to a **root cause**, not a plausible guess.

**ALWAYS START WITH THE LOGS — before reading any code.** The log evidence, not a
hunch about the code, sets the triage direction. Read the debug log for the
relevant bot and date, reconstruct the history of what actually happened (trades,
fills, `decisions.csv`, the prior `session.json`), and follow where that evidence
points. The logs often reveal the root cause faster than reading source — do not
jump to code assumptions before you have read them.

1. Read the bot's debug log FIRST:
   `data/live/<symbol>_<bot-name>/debug_YYYYMMDD.log`
   (e.g. `data/live/TMF00_07-27-空單/debug_20260727.log`). Then read the history in
   the same directory: `decisions.csv`, the current and prior `session.json`, and
   `bars_1m_YYYYMMDD.csv`. Build the timeline of trades/fills/decisions before
   opening any source file.
2. Reproduce or pin down the exact sequence of events. Cross-reference the log
   timestamps against the trade/decision rows and the bar CSVs. Let the log
   evidence — not an assumption — decide which code path to inspect next.
3. Do **not** assume — follow the actual call chain. When adding a new branch,
   check what state it inherits. Trace ALL mutation points before concluding.
4. If the logs lack the detail needed, **add logging to the bot** first, redeploy/
   re-run, and gather more data before proposing a fix. Diagnosing on insufficient
   data is how surface-level patches happen.
5. Output of this phase (all three, logged in the diagnosis before handing off to Opus):
   - **Root-cause statement** — what, where, why. Not a fix yet.
   - **Fix confidence rating (0–100%)** — state the confidence level in the proposed
     root cause / fix direction, plus what evidence would *raise* it and what would
     *lower* it (e.g. "70% — a repro against the known-bad code would raise to 90%;
     finding the same symptom on a code path this fix doesn't touch would lower it").
     This must be visible in the diagnosis output so Opus sees it before implementing.
   - **Solid test plan** — a concrete plan that will produce *visible fix evidence*
     with an unambiguous pass/fail. Prefer tangible artifacts: a screenshot of a
     rendered chart, a log line that was previously missing, a passing assertion that
     fails against the known-bad code. Be specific enough that anyone can run it and
     tell pass from fail. (Example evidence standard — the k-chart fix: a frozen
     smoke-test EXE fed synthetic bars, with a screenshot showing candles rendering.
     Aim for that level of concreteness.)

## Phase 2 — Implement fix (model: `claude-opus-4-8`)
Goal: fix the root cause cleanly, with the full test suite green.

**Handoff criteria — do not start implementing until Phase 1 delivered all three:**
the root-cause statement, the **fix confidence rating (0–100%)** with its raise/lower
factors, and the **solid test plan**. If the confidence rating or test plan is
missing, loop back to Phase 1 rather than guessing. Implement the fix so it satisfies
that test plan, then execute the plan and capture its evidence (screenshot, log line,
or assertion) as proof the fix works — not just a green suite.

1. Branch: `git checkout -b fix/<issue-slug>` off `master`.
2. Fix the fundamental problem identified in Phase 1 — no surface patches. Remove
   any dead code you touch; match surrounding style.
3. Add/adjust tests that would fail against the known-bad code.
4. Run the full suite:
   ```bash
   python -m pytest tests/ -x -q
   ```
   All 1600+ tests must pass. A failing or skipped test is a blocker, not a footnote.

## Phase 3 — Review (model: `claude-fable-5`)
Goal: an independent read of the diff before it merges.

Review `git diff master...fix/<issue-slug>` for:
- **Correctness** — does it actually fix the Phase 1 root cause?
- **Edge cases** — session boundaries, gaps/weekends/holidays, fill races, restarts.
- **Regressions** — does it break any existing code path or timeframe?
- **Non-regime bot safety** — verify plain `LiveRunner` / paper bots are unaffected,
  not just `RegimeSwitchingRunner`. Changes to shared broker/aggregator/live glue
  must be checked against every bot type.
- **Security** — no hardcoded channel/role IDs, no credentials in the diff.

If the review surfaces problems, loop back to Phase 2.

## Phase 4 — Ship
Goal: a versioned release with a Discord notification.

1. Merge `fix/<issue-slug>` to `master`.
2. Bump `version.py`.
3. Build the exe via `build_release.py`.
4. Write bilingual release notes (繁體中文 + English).
5. Create the GitHub release and upload assets.
6. Send the Discord notification — it reads from the GitHub release body (no local
   file lookup), so the release body must be complete before notifying.
