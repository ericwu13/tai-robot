---
name: daily-review
description: Run the full tai-robot health sweep — bots, regime brain, news bridge, logs, deploy config and n8n — and grade it GREEN/YELLOW/RED, optionally posting a summary to Discord. Use for a daily/morning check, a general "is everything OK?", or before shipping a release.
---

# Daily Review

```bash
C:/Python313/python.exe -s scripts/monitor/daily_review.py
C:/Python313/python.exe -s scripts/monitor/daily_review.py --discord   # notify
```

Flags: `--settings PATH`, `--no-report-file`. Always exits 0 — a
monitoring run that crashes is worse than one that reports RED.

Runs all five checks in-process plus an n8n quick check (service state,
`healthz`, failed executions in the last 24h), writes the full report to
`data/monitor/daily_review_<TPE date>.md`, and prints a Discord-sized
digest.

## Interpreting the verdict

| Verdict | Meaning | Do |
|---|---|---|
| 🟢 GREEN | no P1/P2 | note it and stop |
| 🟡 YELLOW | at least one P2 | degraded — investigate today with the named skill |
| 🔴 RED | at least one P1 | act now |

Then drill down by the finding's area:

| Area | Skill |
|---|---|
| `bots` | `bot-status` |
| `regime` | `regime-health` |
| `bridge` | `bridge-health` |
| `logs` | `log-scan` (then the repo's `debug` skill) |
| `deploy` | `deploy-check` |
| `n8n` | `n8n-debug` |

Report what the findings say, not just the colour — the summary names the
bot/source and the reason for every P1/P2.

## When to run it

**~05:10–05:30 TPE** is the sweet spot: the night session has closed
(05:00), the regime classification and P&L recording pass has landed, and
the DAY session hasn't opened yet.

**The OS clock here is UTC-7 while everything semantic is TPE (+8).**
05:10 TPE is **14:10 LOCAL on the PREVIOUS calendar day**. Any scheduler
entry must be written in local time with that shift applied, or it will
fire 15 hours off.

## Gotchas

- **Never echo secrets to Discord.** The digest is redacted (webhooks,
  API keys, long token runs), but never paste raw log lines or settings
  values into a message yourself.
- `--discord` is a no-op without `news.discord_webhook` in `settings.yaml`.
  Do NOT pass `--discord` while testing.
- Everything is READ-ONLY: no lock is deleted, no vote consumed, no state
  rewritten. Keep it that way.
- `data/live` holds ~30 retired test-bot directories. The helper demotes
  their findings to P3 (a stopped bot cannot classify or crash), so a
  wall of P3 rows is expected and not actionable. The digest lists only
  live bots and P1/P2 findings.
- `data/` is gitignored, so the report file is never committed.
