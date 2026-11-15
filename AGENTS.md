# AGENTS.md

Rules for coding agents (Cursor background agents, Reed QA, Codex, …) working
on this repo. Read `CLAUDE.md` first: its **"Change & review contract"** is the
single source for severity (P1/P2/P3), the evidence bar and the PR body
format. This file only adds how agents work together on a PR.

Goal: 我們不可能一直防呆，而是要訂好合理的規則 — reasonable rules, not
foolproofing. One clear rule beats a branch per scenario.

## The review loop

- Every push to a non-draft PR, or to a PR labeled `claude-review`, gets a
  Claude review. Its `review` check is required to merge, and so is "all
  conversations resolved". A push that leaves the PR's own diff unchanged
  (e.g. merging master) reuses the last verdict.
- Claude posts one summary comment (edited in place) and inline threads for
  P1/P2 only. P3s appear only in the summary.
- The owner (Eric) merges. Agents never merge, tag, release or deploy.

## If you wrote the PR

1. **Answer every Claude and Reed thread with exactly one of:**
   - `Fixed in <sha>` (after pushing the fix),
   - `Won't fix: <rule, data or owner decision>`,
   - `Needs owner` (a policy choice: what the bot should do when X).

   Claude reads these replies on its next review and closes threads that
   are fixed or answered with a valid `Won't fix`. Do not resolve Claude's
   threads yourself. Resolve your own.
2. **P3s get no commits.** List them under "Known gaps" in the PR body.
3. **No speculative special cases.** Add a branch for an edge case only
   when real logs, data or an incident show it happens, and cite the
   evidence. Otherwise keep the one rule and name the gap.
4. **Stop rule.** If the PR has the `needs-owner` label (Claude blocked it
   three times), stop pushing. Post ONE question to the owner with the
   options and your recommendation, then wait for the answer.
5. **Don't push only to refresh.** Merge master only to resolve a conflict
   or pick up a change you need. A no-op merge doesn't change the verdict.
6. **Docs that describe new behaviour ship in the same PR as the code**,
   not in a separate PR that documents code master doesn't have yet.
7. **PR body:** Root cause / The fix / Tests / Known gaps / Rollout (see
   CLAUDE.md). Rollout says which `settings.yaml` keys or saved state
   override a changed default, or "none".

## If you are Reed QA (the Cursor review automation)

Claude owns severity for code findings. Reed owns tests and probes.

- Don't give a severity verdict on findings Claude already raised, and don't
  repeat a Claude finding as a new thread.
- Your job is the parts Claude can't run: execute the tests, probes and
  replays, check for missing or vacuous tests, and confirm a claimed fix
  with a run.
- Post no P3 threads. Mention P3s in your review body only.
- When you APPROVE, resolve your own open threads.
- Skip a push that only merges master into the PR (the diff is unchanged).
- A red `review` check on a PR that edits `.github/workflows/claude-review.yml`
  is expected (the action refuses to run a workflow that differs from master).
  That alone is not a reason to withhold APPROVE.
