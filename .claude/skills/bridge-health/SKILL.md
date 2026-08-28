---
name: bridge-health
description: Check the news/vote bridge pipeline — are W2 cross-market, W3 RSS and W4 chips still alive, is a circuit-breaker signal reaching a bot, is the event calendar fresh. Use when a regime vote is missing, a news signal seems ignored, the bridge went quiet, or vote/signal files need auditing.
---

# Bridge Health

```bash
C:/Python313/python.exe -s scripts/monitor/check_bridge.py [--settings PATH]
```

Read-only over the paths in `settings.yaml` (`news.signal_path`,
`news.regime_vote_path`, `news.events_path`, `news.rss_state_file`) —
normally `C:\Users\eric8\n8n-bridge\`. Never consumes or deletes a vote.

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
  `chips_monitor.log`, not the presence of a vote. TAIFEX's OpenAPI lags
  the 16:15 publish by hours, which is why retry slots
  (18:15/20:15/22:15/04:15) exist — if a week of log lines shows only
  16:15, the deployed workflow predates those retries.
- A naive `issued_at` is rejected on purpose: n8n runs on its own host,
  and guessing the offset would turn an 8-hour-old signal into a fresh one.
- An empty `news.regime_vote_path` in settings means votes are written but
  never consumed — see `deploy-check`.
- Don't restart the n8n service casually while a night session is open
  (15:00–05:00 TPE): it interrupts W2's 2-minute night-tier polling.

## Next steps

- a workflow actually failed / a cron never fired → the `n8n-debug` skill
  (`status`, `execs --workflow W3 --errors`, `detail <id>`)
- votes arriving but the regime not moving → `regime-health`
