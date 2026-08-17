---
name: n8n-debug
description: Self-serve diagnosis of the local n8n news-bridge instance (W1-W5 workflows) — find why a workflow failed, never fired, or isn't imported/active, without the user in the loop. Use when a bridge script errors, a vote/signal file is missing, a Discord alert didn't arrive, or an n8n workflow/execution needs inspecting.
---

# n8n News-Bridge Debugging

Everything here works **without the n8n UI, without the REST API key, and
without asking the user** — via the n8n SQLite DB (read-only), the event
log, and the Windows service state. Start with the helper script; fall
back to raw paths only if it can't answer.

## Rule zero — evidence before code

The failure evidence is ALWAYS on disk. Do not read source, guess, or ask
the user to paste errors until you have pulled the actual execution
record. (The 2026-08-16 yaml incident: the full `ModuleNotFoundError`
traceback sat in `execution_data` for 12 hours of half-hourly failures
while the audit only read code and ran pytest in the dev shell.)

## The helper (tested — prefer it over ad-hoc queries)

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

## Symptom → where to look

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
   the OS clock is UTC-7, n8n needs Asia/Taipei pinned).
4. **"Workflow not in the list at all"** → `status` shows every
   `workflow_entity` row including ARCHIVED. Not there = never imported:
   `n8n import:workflow --input=<repo>/scripts/news_bridge/n8n/<file>.json`
   imports it **inactive**; activation is UI "Publish" (hot-reloads the
   trigger, needs user login) — CLI/DB activation does NOT register the
   trigger until a service restart.
5. **Service dead / everything failing instantly** → `status` (PAUSED =
   NSSM crash-loop throttle), then `C:\Users\eric8\n8n-bridge\n8n-service.log`.

## Known failure signatures (all previously seen live)

| Evidence in `detail` / logs | Root cause → fix |
|---|---|
| `ModuleNotFoundError: No module named 'yaml'/'feedparser'/'tzdata'` | Service runs as **LocalSystem** → user-site packages invisible. Install system-wide: `sudo cmd /c "set PYTHONNOUSERSITE=1&& C:\Python313\python.exe -s -m pip install <pkg>"` (UAC prompt; plain elevated pip says "already satisfied" and installs nothing). Verify: `python -s -c "import <pkg>"` |
| `'python' is not recognized` | executeCommand has no user PATH → absolute `C:\Python313\python.exe` in the workflow command |
| Every executeCommand fails in ~7 ms, empty stderr | n8n's child-process spawning died → restart the service |
| `Unrecognized node type: n8n-nodes-base.executeCommand` | `NODES_EXCLUDE="[]"` env lost |
| Cron fires at wrong TPE hour | `GENERIC_TIMEZONE` + `TZ` = Asia/Taipei missing |
| `port 5678 is already in use` loop in n8n-service.log | orphan n8n process → kill it, restart service |
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

`-s` replicates LocalSystem's package visibility. A clean dry-run under
`-s` is the merge gate for bridge scripts, alongside pytest. New
third-party imports must be installed system-wide (recipe above) — as of
2026-08-17 that set is: pyyaml, feedparser, tzdata.

## Paths & facts

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
- Service control needs admin: `sudo` is Force-New-Window mode → wrap
  with `sudo cmd /c "... > log 2>&1"` and read the log.
- REST API exists but the `X-N8N-API-KEY` is not stored anywhere Claude
  can read — do not plan around it. UI actions (Publish, Archive) need
  the user logged in; everything diagnostic works without.
- Restarting the service interrupts W2's 2-min night-tier polling — avoid
  casual restarts while a night session is open (15:00–05:00 TPE).
