---
name: validate-findings
description: Adversarially validate claims before they are published or acted on — a skeptic re-derives each claim from primary evidence and returns CONFIRMED/REFUTED. Use before posting a monitoring digest, before kicking off a fix from a root-cause claim, or whenever a conclusion is about to leave the session.
---

# Validate Findings

A claim that hasn't been re-derived from primary evidence is a draft,
not a finding. This skill is the gate between "the review said X" and
"we told the user / started coding X".

## Protocol

1. **List the claims** — every factual statement the output will make:
   counts, liveness, "new vs chronic", correlations, root causes,
   fix-status assertions — **and every SEVERITY grade (a P1/P2 is a
   claim that the behavior is a fault, and it validates like any other
   claim)**. One line each, with where its primary evidence lives (file
   path, command). Whenever the output would lead to `fix-pr`, also list
   the mandatory claim **"This still needs a new code fix on current
   master"** (see below).
2. **Spawn one skeptical read-only subagent** (Explore, model sonnet)
   whose instructions are: do NOT trust the claims; re-derive each from
   the named evidence (re-count the rows, re-check the PID, re-read the
   log); for a "needs a new code fix" claim, re-search related closed
   issues/PRs/CHANGELOG and re-check the inventing path on current
   master; return CONFIRMED or REFUTED per claim with one evidence line,
   then a final "SAFE TO PUBLISH: yes/no" — and separately whether
   assign-`fix-pr` is allowed. For small claim sets (≤3) the main
   session may validate inline instead — same re-derivation rule.
3. **Act on verdicts**: REFUTED claims are corrected or dropped — never
   published, and a REFUTED root cause disqualifies `fix-pr`. If a
   refutation overturns something load-bearing, say so explicitly in the
   output ("initially suspected X; evidence shows Y"). A REFUTED "needs
   a new code fix on current master" blocks **assign**, even if the
   mechanism facts are CONFIRMED.

## Validate the judgment, not just the fact

A finding has two parts — "X happened" (factual) and "X is a problem"
(normative). Re-deriving only the first shipped a recurring P2 for
behavior the code implements ON PURPOSE (the semi_auto confirm-dialog
auto-skip: REAL_ORDER_TIMEOUT). Before any P1/P2 survives validation,
READ THE SOURCE that produces the behavior and ask: accident or
designed mechanism? Evidence of design:

- a deliberate code branch with its own constant/log template (not an
  error path) — e.g. a countdown UI ("Auto-skip in Ns"), a named
  decision row, a sibling path for the manual case
- a TEST pinning the behavior as intended
- chronicity nobody complained about — a "problem" recurring for months
  on a system the user watches daily is probably a feature

Designed + recurring ⇒ demote to P3 informational and, at most ONCE,
ask the user whether it should ever alert — never re-flag it after they
say it is intended, and never attach unsolicited "fix" recommendations
("switch to auto") to designed behavior. Only the user promotes a
designed behavior back to alert-worthy.

## "Still needs a new code fix on current master"

Mandatory whenever the output would assign `fix-pr`. Primary evidence
must include all three: (a) closed issues, merged PRs, and
CHANGELOG/release notes searched for the same *user-visible symptom
class*; (b) whether the inventing code path still exists on current
master after those merges; (c) the owner has not already marked the
symptom covered. If related shipped work addresses the same symptom
class and owner judgment is unknown → **REFUTE** "needs a new fix now".
SAFE TO PUBLISH may be **yes** for the root-cause facts but **not** for
"assign `fix-pr`" — say so explicitly. Do not treat CONFIRMED mechanism
as permission to code.

## Rules that catch real failure modes (all happened here)

- **Correlation discipline**: an issue naming a strategy does NOT pin it
  to one bot dir — several dirs run the same strategy (#105 vs
  TMF00_0422). A digest carried a stale "no live bots" claim from a
  previous day's data — validate against NOW's evidence, never a cached
  summary.
- **Clock discipline**: this machine is UTC-7; TPE is +8. decisions.csv
  col 0 is machine-local, `bar_dt` (col 1) is TPE. NTFS mtimes are
  local. Name the clock before comparing timestamps.
- **Absence is evidence too**: verify a hypothesis by the line it
  PREDICTS (clock skew predicts `[STALE TICK]` lines; none present =
  refuted).
- **Security-warned subagent output** is unvalidated data — its facts
  may all be right, but each must be re-derived before use.
- **"Absent file" ≠ broken**: vote files are consumed/deleted by design;
  reports only exist after trades; logs are quiet in closed sessions.
- **Related-fix discipline**: checking related closed issues *after*
  owner pushback is too late. #107 (ghost sim 持倉 after restart) was
  assigned a fix-pr; the owner later treated it as covered by resume OI
  reconcile in #79/#113 (shipped). Search related work before assign.
  A remaining inventing path (e.g. timeout skip unwind) is not an
  automatic close — and related resume/OI safety already shipped for
  the same symptom is not an automatic assign.
