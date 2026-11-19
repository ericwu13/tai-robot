---
name: bridge-health
description: Check the news/vote bridge pipeline — are W2 cross-market, W3 RSS and W4 chips still alive, is a circuit-breaker signal reaching a bot, is the event calendar fresh. Use when a regime vote is missing, a news signal seems ignored, the bridge went quiet, vote/signal files need auditing, "W1/W2/W3/W4 down", "news bridge", "市場脈動", or the Steven bridge tasks.
---

# Bridge Health

> **Where the data is (since 2026-10-24/25):** the live fleet (12 headless
> `tai_backtest.exe` bots), the bot dashboard and the W1–W4 bridge tasks
> run on **Steven**, not this PC. This PC's `data\live` holds only
> retired/test bots and its n8n is stopped. Read live state from the
> dashboard API (`http://<Steven tailnet IP>:8765/api/fleet`,
> `/api/bot?id=<SYMBOL>_<bot>`; address in `hosts.local.yaml`) or run the
> checker on Steven via `tailscale-ssh`. A run of a checker on this PC
> describes this PC only — never publish it as fleet health. Host map →
> `start-here`. Run checkers from an up-to-date checkout
> (`git rev-list --count HEAD..origin/master` = 0); the main tree may be
> weeks behind.

```bash
C:/Users/eric8/.venvs/tai-monitor/Scripts/python.exe -s scripts/monitor/check_bridge.py [--settings PATH]
```

Read-only over the paths in the bridge host's `settings.yaml`
(`news.signal_path`, `news.regime_vote_path`, `news.events_path`,
`news.rss_state_file`). Never consumes or deletes a vote.

**Since 2026-10-24 the bridges are Steven scheduled tasks**
`\TaiRobot\Bridge-W1..W4` writing `C:\Users\Traderbot\n8n-bridge\` (W1/W2/W4
from `tai-robot-src`, W3 from `tai-robot-dashboard`, all with the src venv
and `tai-robot-src\settings.yaml`). Quick remote view: dashboard
`/api/workflows` (per-W status, last run, next due) and `/api/news/pulse`.
Full check on Steven: `<venv python> -s scripts\monitor\check_bridge.py
--settings C:\Users\Traderbot\tai-robot-src\settings.yaml`. Per-task
evidence: `task_<W>.last.log`, `runs\W*.jsonl`, `Get-ScheduledTaskInfo
-TaskPath '\TaiRobot\'`. A local run reports every source stale (local files
stop 2026-10-23). The unconsumed-signal P1 only means something on the host
whose `data\live` holds the `--news` bot.

## Rule zero — absence is not failure

A missing vote file is the **normal resting state**:

- W4 deletes its vote file when it has no opinion (hysteresis band,
  contradiction, no TAIFEX data).
- The classifier consumes and deletes every pending vote after each pass.

So liveness comes from the bridges' own sidecar state files, never from
"is there a vote on disk".

| Source | Liveness file | Stale after |
|---|---|---|
| W2 cross-market | `monitor_state.json` (next to `signal_path`) | 24 h |
| W3 RSS scorer | `news.rss_state_file` (`rss_scorer_state.json`) | 2 h |
| W4 chips | `.chips_state.json` (next to `regime_vote_path`) | 72 h |

A pending vote only counts when its `expires_after_session` matches
tonight's key exactly (`upcoming_night_session(now).key`).

## Reading the verdict

| Level | Meaning |
|---|---|
| P1 | a fresh, valid `signal.json` older than 10 min that NO bot's `news_ledger.json` has consumed — the poll loop or the news wiring is dead |
| P2 | a source past its staleness threshold; a signal REJECTED for a writer bug; stale/unreadable event calendar; W2 monitor silent >60 min; ≥3 W2 fetch-fails; W4 voteless streak ≥3; only the 16:15 slot in 7 days of chips log |
| P3 | liveness unknown (no sidecar found); vote contradiction; force-fire; a fresh signal already consumed |

Signal reject reasons split cleanly:

| Reject reason | Verdict |
|---|---|
| `stale signal: …` / `signal file not found` | healthy — nothing is firing |
| `issued_at has no timezone` / `unsupported signal schema version` / `malformed signal file` / `unknown action` | P2 — the WRITER is broken |

## Gotchas

- **`rss_scorer.log` is never written in production.** W3 runs the
  `--once` path, which doesn't log. Judging W3 by that file always looks
  dead — use `rss_scorer_state.json` (`last_check`, `session_net`).
- **W4's real health signal is the trailing "no TAIFEX data" streak** in
  `chips_monitor.log`, not the presence of a vote. TAIFEX publishes the prior
  day late morning TPE, so Steven's 16:15 slot is the one that advances
  `.chips_state.json` `history`; 04:15 is usually a no-op. (The checker's
  P2 text still names the old 18:15/20:15/22:15 retry ladder.)
- A naive `issued_at` is rejected on purpose: n8n runs on its own host,
  and guessing the offset would turn an 8-hour-old signal into a fresh one.
- An empty `news.regime_vote_path` in settings means votes are written but
  never consumed — see `deploy-check`.
- Don't trigger or restart Steven's W2 task casually during a night
  session (15:00–05:00 TPE). Never restart the retired local n8n — W1–W4
  would double-run.

## Next steps

- a bridge task failed / never fired → Steven Task Scheduler history +
  `task_<W>.last.log` (via `tailscale-ssh`); `n8n-debug` only for
  pre-2026-10-24 history
- votes arriving but the regime not moving → `regime-health`
