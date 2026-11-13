---
name: log-scan
description: Scan tai-robot bot debug logs and the news-bridge logs for known error signatures — tracebacks, corrupted state, failed Discord sends, refused swaps. Use when something broke and the cause is unknown, after a crash, or when asking "is there anything scary in the logs?". Retired local n8n logs are history-only (`n8n-debug`); live W1–W4 failures are `bridge-health`.
---

# Log Scan

> **Where the data is (since 2026-10-24/25):** the live fleet (12 headless
> `tai_backtest.exe` bots), the bot dashboard and the W1–W4 bridge tasks
> run on **Steven**, not this PC. This PC's `data\live` holds only
> retired/test bots and its n8n is stopped. Read live state from the
> dashboard API (`http://<Steven tailnet IP>:8765/api/fleet`,
> `/api/bot?id=<SYMBOL>_<bot>`; address in `hosts.local.yaml`). A script
> on that host needs the permission in `start-here` §7. A run of a
> checker on this PC describes this PC only — never publish it as fleet
> health. Host map → `start-here`. Run checkers from an up-to-date checkout
> (`git rev-list --count HEAD..origin/master` = 0); the main tree may be
> weeks behind.

Data host → `start-here` §7; name the host in the output.

Default is the read-only API log tail, not this PC's `data/live`.

```bash
curl -s "http://<Steven tailnet IP>:8765/api/bot?id=<SYMBOL>_<bot>"
```

Address in `hosts.local.yaml`. The API log is a partial tail; a full scan is `check_logs.py` on that host (permission per `start-here` §7). That scan covers, per
bot directory, the newest two `debug_YYYYMMDD.log` files from the **last
7 days** (older logs are archaeology from retired test bots).

Fallback, this PC only:

```bash
C:/Python313/python.exe -s scripts/monitor/check_logs.py
```

`n8n-service.log` and `~/.n8n/n8nEventLog.log` are history-only (retired
local n8n, stopped 2026-10-24). Do not treat them as live bridge health —
live W1–W4 are `bridge-health`.

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
| history | `n8n.workflow.failed` — retired local n8n; if check_logs.py prints it, ignore (not live) |

Findings are deduped per (pattern, file) and capped at 5 example lines,
each redacted before it is printed.

## Gotchas

- **No levelnames in the app log** — `grep ERROR` finds nothing useful.
  Match message bodies (the table above) instead.
- **Debug logs are per-TPE-date and never rotated**; a busy bot's daily
  log reaches ~9 MB. The helper tails them (2 MB) rather than reading
  whole files — do the same if you go in manually.
- Log lines carry two clocks: `[<TPE> / <local>]`. The **first**
  timestamp is TPE. The second is the host's local clock (clocks → `start-here` §4)
  and must never be used for reasoning about sessions.
- `swap_strategy refused` is often legitimate (one of the 5 safety gates:
  not flat, veto, in replay, timeframe mismatch, insufficient bars) —
  read the reason before treating it as a bug.
- A `Traceback` in a log from months ago is a retired test bot, not an
  incident; the 7-day window exists for exactly that reason. If you widen
  it manually, check the bot is one that still runs.
- `n8n.workflow.failed` in the retired local n8n event log is history-only,
  not a live P2. See `n8n-debug` (history only — do not run). A live bridge
  failure or a missing vote → `bridge-health`.

## Next steps

- traceback in a live bot → the repo's `debug` skill (root-cause → fix → review); release → `CLAUDE.md` § Release Process
- historical `n8n.workflow.failed` on the retired local n8n → `n8n-debug`
  only (history only — do not run). A live bridge silence or missing vote
  → `bridge-health`
- `Discord send failed` → confirm `notifications.*` wiring with
  `deploy-check`
