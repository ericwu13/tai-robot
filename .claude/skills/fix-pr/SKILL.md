---
name: fix-pr
description: Implement a validated bug fix in an isolated git worktree and ship it as a draft PR — gated on a confirmed root cause, with failing-first tests, never touching the main working tree (live bots run from it) and never merging. Use after issue-triage + validate-findings confirm a bounded fix, or when asked to turn a root cause into a PR.
---

# Fix → Draft PR

Turns a CONFIRMED root cause into a reviewable draft PR. This is the
autonomous sibling of the interactive `debug` skill (which ends in a
release); this one ends at a draft PR and a comment on the issue.

## Gates — ALL must hold, else stop and report why

1. Root cause was produced by `issue-triage` (or equivalent evidence)
   AND confirmed by `validate-findings`. A plausible-but-unverified
   cause never reaches code.
2. The fix is bounded: a handful of files, no architecture change, no
   change to live order-sending semantics, never settings/credentials.
3. No duplicate work: `gh pr list --state open --json number,headRefName`
   and `git branch --list "fix/issue-<N>*"` show nothing covering it.

## Execution

- Spawn ONE implementation agent, **model opus** (CLAUDE.md model
  preference), **isolation: worktree** — the main working tree is
  off-limits: live bots run python processes out of it, and even a
  branch checkout churns files under them.
- The agent's mandate: follow CLAUDE.md conventions; write unit tests
  **designed to FAIL against the pre-fix code** (state which ones fail
  pre-fix); grep for existing tests that PINNED the bug and rewrite them
  with an issue reference; run `python -m pytest tests/ -x -q` — full
  suite green (worktree note: `tests/test_session_replay.py` skips there
  because gitignored `data/` is absent — expected); commit on branch
  `fix/issue-<N>` (conventional message, ending with
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`); push;
  `gh pr create --draft` with Root cause / The fix / Tests / "Fixes #N"
  and the footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- After it returns: verify with `gh pr view`, review the diff YOURSELF
  (`git fetch origin fix/issue-<N>; git diff master...origin/fix/issue-<N>`)
  — scrutinize any deviation from the spec, especially glue-code changes
  (`run_backtest.py` — the repo's bugs live in glue); then
  `gh issue comment <N>` with the root cause, the PR link, and the
  user-facing workaround.

## Hard limits

- **NEVER merge.** The PR stays a draft; human review + merge is the
  only path to master. Releases (so EXE users get the fix) go through
  the `debug` skill's release phase, separately and human-initiated.
- Never commit `data/`, settings files, or anything the fix didn't
  change. Redact reporter-bundle content in PR/issue text.
- One fix per pipeline run — depth over breadth.
