---
name: n8n-debug
description: HISTORY ONLY (local n8n stopped since 2026-10-24; current W1-W4 are Steven scheduled tasks — use bridge-health). Archived diagnosis of the retired local n8n news-bridge (W1-W5): why a historical workflow failed, never fired, or was not imported. A live bridge-script error, a missing vote or signal file, or a Discord alert that did not arrive is bridge-health, not this skill.
---

# n8n News-Bridge Debugging

**History only — do not run.** Local n8n is retired; restarting it can double-run W1–W4 (see `bridge-health`). Current bridges are Steven scheduled tasks. The notes below are a record of that retired instance. Read-only DB and event-log queries may stay, and they are history-only.

These notes describe the retired instance **without the n8n UI, without the REST API key, and
without asking the user** — via the n8n SQLite DB (read-only), the event
log, and the Windows service state, as it was. Historical reads start with the helper script; fall
back to raw paths only if it can't answer.

## Rule zero — evidence before code

The failure evidence is ALWAYS on disk. Do not read source, guess, or ask
the user to paste errors until you have pulled the actual execution
record. (The 2026-08-16 yaml incident: the full `ModuleNotFoundError`
traceback sat in `execution_data` for 12 hours of half-hourly failures
while the audit only read code and ran pytest in the dev shell.)

## Rule zero-b — probe the upstream API before fixing around it

When a bridge fix depends on an external feed's schedule or semantics,
probe the endpoint's ACTUAL contract first (does it honor parameters?
what exactly does it serve? when does it flip?), and treat the fix as
unverified until one real cycle confirms it. (The 2026-08-25 W4 retry
ladder was designed on the assumption that the TAIFEX OpenAPI lagged
"hours" and honored a date — it takes no date parameter, serves exactly
one dataset, and lags >12h; the first live night falsified the fix and
it had to be redesigned as the walk-back.) Also compare live data
magnitudes against every threshold in the decision path: a reading that
permanently saturates a threshold (外資 net OI −80k vs a 5k "strong"
band) means the threshold was calibrated for a different quantity
(delta vs level), not that the signal is strong.

## The helper (history-only reads of the retired DB and event log)

```bash
C:/Python313/python.exe .claude/skills/n8n-debug/scripts/n8n_query.py status
```

| Command | Answers |
|---|---|
| `status` | service RUNNING/PAUSED, healthz, every workflow's imported/active/ARCHIVED state + last execution |
| `execs --workflow W3 --errors` | which runs failed, when (UTC), trigger vs manual |
| `detail <exec_id>` | the actual stderr/traceback/exit evidence from one execution |
| `detail <exec_id> --grep stdout` / `--all` | stdout or full dump of a (successful) run |
| `events --pattern failed` | did the cron fire at all, in TPE timestamps |

## Symptom → where to look (history-only; a live bridge silence or missing vote → `bridge-health`)

1. **"Workflow X failed" / Discord went quiet** →
   `status` → `execs --workflow X --errors` → `detail <newest id>`.
   Failure duration ~150 ms = spawn/import error (read the traceback);
   30 s+ = network timeout inside the script.
2. **Vote/signal file missing but no failure visible** → the bridge
   scripts' own liveness logs FIRST — `C:\Users\eric8\n8n-bridge\`
   `monitor.log` / `rss_scorer.log` / `chips_monitor.log` — one line per
   pass with the no-vote reason. A missing W4 vote is usually a
   *decision* (hysteresis/contradiction/no-data), not a failure.
3. **"Cron never fired"** → `events --pattern <workflow>` (no
   `workflow.started` events = trigger not registered) → `status`
   (workflow INACTIVE? = imported but never published) → wrong firing
   HOUR = `GENERIC_TIMEZONE`/`TZ` envs lost from the service (memory:
   the OS clock is US Pacific, n8n needs Asia/Taipei pinned).
4. **"Workflow not in the list at all"** → `status` shows every
   `workflow_entity` row including ARCHIVED. Not there = never imported (historical fix: `n8n import:workflow --input=<repo>/scripts/news_bridge/n8n/<file>.json`; do not run).
5. **Service dead / everything failing instantly** → `status` (PAUSED =
   NSSM crash-loop throttle), then `C:\Users\eric8\n8n-bridge\n8n-service.log` (do not restart).

## Known failure signatures (all previously seen live)

| Evidence in `detail` / logs | Root cause → historical fix (do not run) |
|---|---|
| `ModuleNotFoundError: No module named 'yaml'/'feedparser'/'tzdata'` | Do not install into system site-packages. The monitor venv recipe is in `.claude/skills/regime-health/SKILL.md`: `C:\Users\eric8\.venvs\tai-monitor\Scripts\python.exe -m pip install holidays==0.105 python-dateutil PyYAML==6.0.3 six`. Checkers that import `src.market_data.holidays` run as `C:/Users/eric8/.venvs/tai-monitor/Scripts/python.exe -s`. `feedparser` and `tzdata` are not in that venv. n8n W4/W5 executeCommand nodes stay on `C:\Python313\python.exe` (LocalSystem, no `-s`). |
| `'python' is not recognized` | executeCommand has no user PATH → absolute `C:\Python313\python.exe` in the workflow command |
| Every executeCommand fails in ~7 ms, empty stderr | n8n's child-process spawning died → do not restart the service |
| `Unrecognized node type: n8n-nodes-base.executeCommand` | `NODES_EXCLUDE="[]"` env lost |
| Cron fires at wrong TPE hour | `GENERIC_TIMEZONE` + `TZ` = Asia/Taipei missing |
| `port 5678 is already in use` loop in n8n-service.log | orphan n8n process → do not kill it, do not restart service |
| `No encryption key found ... .n8n\.n8n\config` | `N8N_USER_FOLDER` must be `C:\Users\eric8` (parent), not `...\.n8n` |
| Fresh empty DB / login rejects known account | same `.n8n\.n8n` folder bug |

## Environment parity (the audit gap this skill closes)

pytest and your shell run as `eric8` **with** user site-packages; the
service runs as LocalSystem **without** them. Code reading + green tests
prove nothing about the deployment env. Therefore, after ANY change to
`scripts/news_bridge/*`:

```bash
C:/Python313/python.exe -s <script> --dry-run   # or --once
```

`-s` on `C:/Python313/python.exe` replicates LocalSystem's package
visibility for news-bridge scripts. Those scripts do not import
`src.market_data.holidays`. `feedparser` and `tzdata` are outside the
tai-monitor venv (regime-health). W4/W5 executeCommand nodes stay on
`C:\Python313\python.exe`.

## Paths & facts (history-only)

- DB: `C:\Users\eric8\.n8n\database.sqlite` — **read-only URI only**
  (`file:...?mode=ro`); never write while the service runs. Execution
  timestamps are UTC; event-log timestamps are TPE.
- Event log: `C:\Users\eric8\.n8n\n8nEventLog.log` (+ rotated `-1/-2/-3`).
- Service log (NSSM stdout): `C:\Users\eric8\n8n-bridge\n8n-service.log`.
- Workflow sources of truth: `scripts/news_bridge/n8n/*.json` (repo) —
  W1 calendar, W2 crossmarket, W3 RSS, W4 chips, W5 manual tap.
- `execution_data.data` is n8n's flattened JSON. Windows paths inside it
  contain literal `\n` sequences (`\news_bridge`, `\n8n-bridge`) — never
  "unescape" them; the helper already handles this.
- REST API exists but the `X-N8N-API-KEY` is not stored anywhere Claude
  can read — do not plan around it. UI actions (Publish, Archive) need
  the user logged in; everything diagnostic works without.
- Current W2 is a Steven scheduled task (`bridge-health`).
