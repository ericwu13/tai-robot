---
name: bridge-health
description: Check the news/vote bridge pipeline — are W2 cross-market, W3 RSS and W4 chips still alive, is a circuit-breaker signal reaching a bot, is the event calendar fresh. Use when a regime vote is missing, a news signal seems ignored, the bridge went quiet, or vote/signal files need auditing.
---

# Bridge Health

```bash
C:/Users/eric8/.venvs/tai-monitor/Scripts/python.exe -s scripts/monitor/check_bridge.py [--settings PATH]
```

Read-only over the paths in `settings.yaml` (`news.signal_path`,
`news.regime_vote_path`, `news.events_path`, `news.rss_state_file`) —
normally `C:\Users\eric8\n8n-bridge\`. Never consumes or deletes a vote.

## Where the bridge runs (since 2026-10-24: the second trading PC)

W1-W4 run as `\TaiRobot\Bridge-*` scheduled tasks on the host whose
`monitor.bridge_dir` is set in the gitignored tailscale hosts file
(`steven`). This PC's `C:\Users\eric8\n8n-bridge\` is no longer written,
so `check_bridge.py` above reports a frozen copy. Check the live one:

```bash
C:/Users/eric8/.venvs/tai-monitor/Scripts/python.exe -s scripts/monitor/remote.py --check bridge
C:/Users/eric8/.venvs/tai-monitor/Scripts/python.exe -s scripts/monitor/remote.py --check tasks
```

One ssh call fetches the bridge files (log tails only) into
`data/monitor/remote/<host>/bridge/` with remote mtimes kept, then runs
this same check over that copy — every rule below applies unchanged. The
"consumed by" lookup reads the remote bots' `news_ledger.json`, which is
where the consumer of that signal lives. `TASKS` adds: a task whose last
result is not 0/running/queued, a Disabled task, or a traceback in its
`task_<W>.last.log` = P2; host clock skew >5 min = P2; host unreachable
= P1 (bridge UNVERIFIED, not proven dead).

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
- This PC's n8n-service is stopped on purpose since the move — never
  start it (see `n8n-debug`). On the bridge host, don't re-run the W2
  task casually while a night session is open (15:00–05:00 TPE).

## Next steps

- a task actually failed / never fired → `remote.py --check tasks`, then
  the `n8n-debug` skill (its first section covers the scheduled tasks;
  `status`/`execs`/`detail` cover pre-2026-10-24 n8n history)
- votes arriving but the regime not moving → `regime-health`
