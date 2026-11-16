---
name: fix-pr
description: Implement a validated bug fix in an isolated git worktree (via the fixer agent) and ship it as a draft PR — gated on a confirmed root cause, with failing-first tests, never touching the owner's main working tree and never merging. Use after issue-triage + validate-findings confirm a bounded fix, or when asked to turn a confirmed root cause into a PR.
---

# Fix → Draft PR

Turns a CONFIRMED root cause into a reviewable draft PR. This is the
autonomous sibling of the interactive `debug` skill (which ends in a
release); this one ends at a draft PR and a comment on the issue.
Severity, evidence bar, finding replies and the PR body follow CLAUDE.md
"Change & review contract".

## Gates — ALL must hold, else stop and report why

1. Root cause was produced by `issue-triage` (or equivalent evidence)
   AND confirmed by `validate-findings`. A plausible-but-unverified
   cause never reaches code.
2. The fix is bounded: a handful of files, no architecture change, no
   change to live order-sending semantics, never settings/credentials.
   If the fix needs a policy choice (what the bot should do when X), stop
   and post the options to the owner as ONE question on the issue.
3. No duplicate or already-covered work: `gh pr list --state open --json
   number,headRefName` and `git branch --list "fix/issue-<N>*"` show
   nothing covering it, and `issue-triage`'s related-work search (merged
   PRs, closed issues, same *user-visible symptom class*) says `STILL
   OPEN` — or the owner explicitly chose continue after `LIKELY COVERED` /
   `OWNER JUDGMENT NEEDED`. (#107: a merged resume/OI fix for "ghost 持倉
   after restart" counted as covering work.)

## Execution

- Spawn ONE `fixer` agent (`.claude/agents/fixer.md`: **model opus**,
  **isolation: worktree** pinned in its definition). If that agent is not
  available, spawn a general-purpose agent with model opus and
  isolation worktree, and include the mandate below in its brief. The
  main working tree is off-limits: it is the owner's tree, usually behind
  origin/master, and a checkout there churns their files. The brief gives
  the CONFIRMED root cause, the test plan, branch `fix/issue-<N>`, and
  "push and open a draft PR". The agent's first steps: `git fetch
  origin`, branch from `origin/master` (`git switch -c fix/issue-<N>
  origin/master`), confirm `git rev-list --count HEAD..origin/master` = 0.
  Never branch from the main tree's HEAD.
- The agent's mandate: follow CLAUDE.md conventions and its change &
  review contract — state the fix as one rule ("use X when valid,
  otherwise existing behaviour"), add a special-case branch only for an
  edge case real logs/data/an incident show (cite it), and list the rest
  under "Known gaps" with how the rule covers them; write unit tests
  **designed to FAIL against the pre-fix code** (state which ones fail
  pre-fix); grep for existing tests that PINNED the bug and rewrite them
  with an issue reference; run `python -m pytest tests/ -x -q` — full
  suite green (worktree note: `tests/test_session_replay.py` skips there
  because gitignored `data/` is absent — expected); commit on branch
  `fix/issue-<N>` (conventional message, ending with
  the `Co-Authored-By:` line the session's attribution instructions give —
  it names the model that actually wrote the commit); push;
  `gh pr create --draft` with Root cause / The fix / Tests / Known gaps /
  Rollout / "Fixes #N"
  and the footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- After it returns: verify with `gh pr view`, review the diff YOURSELF
  (`git fetch origin master fix/issue-<N>; git diff origin/master...origin/fix/issue-<N>`
  — local `master` can be weeks behind and would pull unrelated commits into the diff)
  — scrutinize any deviation from the spec, especially glue-code changes
  (`run_backtest.py` — the repo's bugs live in glue) — and check it is
  one rule, at least as safe as master, with every special case backed by
  cited evidence (unbacked ones move to "Known gaps"); then
  `gh issue comment <N>` with the root cause, the PR link, and the
  user-facing workaround.

## Hard limits

- **NEVER merge.** The PR stays a draft; human review + merge is the
  only path to master. Releases (so EXE users get the fix) go through
  the `debug` skill's release phase, separately and human-initiated.
- Never commit `data/`, settings files, or anything the fix didn't
  change. Redact reporter-bundle content in PR/issue text.
- One fix per pipeline run — depth over breadth.
