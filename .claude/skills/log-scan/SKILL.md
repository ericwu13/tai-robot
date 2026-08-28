---
name: log-scan
description: Scan tai-robot bot debug logs and the n8n bridge logs for known error signatures — tracebacks, corrupted state, failed Discord sends, refused swaps, n8n workflow failures. Use when something broke and the cause is unknown, after a crash, or when asking "is there anything scary in the logs?".
---

# Log Scan

```bash
C:/Python313/python.exe -s scripts/monitor/check_logs.py
```

Scans, per bot directory, the newest two `debug_YYYYMMDD.log` files from
the **last 7 days** (older logs are archaeology from retired test bots),
plus `n8n-service.log` and `~/.n8n/n8nEventLog.log`.

## Pattern → severity

The app's log handler prints **no levelname**, so severity comes from the
message body:

| Level | Pattern |
|---|---|
| P1 | `news framework DISABLED` |
| P1 | `Corrupted signal ledger` |
| P1 | `Corrupted state file` |
| P1 | `Unrecoverable apply error` |
| P1 | `Cannot restore leg` |
| P1 | `Traceback (most recent call last)` |
| P2 | `Event calendar unusable or stale` |
| P2 | `Discord send failed` |
| P2 | `Daily report generation failed` |
| P2 | `malformed vote file` |
| P2 | `could not remove vote file` |
| P2 | `swap_strategy refused` |
| P2 | `Discarded pending recommendation` |
| P3 | `Dropped out-of-order aggregated bar` |
| P3 | `Discarded recommendation` |
| P2 | any `n8n.workflow.failed` event in the last 24h |

Findings are deduped per (pattern, file) and capped at 5 example lines,
each redacted before it is printed.

## Gotchas

- **No levelnames in the app log** — `grep ERROR` finds nothing useful.
  Match message bodies (the table above) instead.
- **Debug logs are per-TPE-date and never rotated**; a busy bot's daily
  log reaches ~9 MB. The helper tails them (2 MB) rather than reading
  whole files — do the same if you go in manually.
- Log lines carry two clocks: `[<TPE> / <local>]`. The **first**
  timestamp is TPE. The OS clock is UTC-7, so the second one is 15 hours
  behind and must never be used for reasoning about sessions.
- `swap_strategy refused` is often legitimate (one of the 5 safety gates:
  not flat, veto, in replay, timeframe mismatch, insufficient bars) —
  read the reason before treating it as a bug.
- A `Traceback` in a log from months ago is a retired test bot, not an
  incident; the 7-day window exists for exactly that reason. If you widen
  it manually, check the bot is one that still runs.

## Next steps

- traceback in a live bot → the repo's `debug` skill (root-cause → fix →
  review → release)
- `n8n.workflow.failed` hits → the `n8n-debug` skill for the actual
  execution record (`execs --errors`, then `detail <id>`)
- `Discord send failed` → confirm `notifications.*` wiring with
  `deploy-check`
