---
name: regime-health
description: Verify the regime-switching brain — did it classify last night, did the recommendation actually apply, is a flip pause or news suppression freezing it. Use when the regime didn't change, a strategy swap never happened, the bot sits idle, or a regime classification/vote needs auditing.
---

# Regime Health

```bash
C:/Python313/python.exe -s scripts/monitor/check_regime.py
```

Read-only over `data/live/<bot>/regime_state.json`, `regime_history.csv`,
`decisions.csv`, and the `news` block of `session.json`.

## Session identity (get this right first)

A session is identified by its **OPEN date**. The night opening Mon 15:00
and closing Tue 05:00 is `2026-08-24|NIGHT` on BOTH sides of midnight.
Every key the checker compares — `last_assessed`, history rows, P&L
windows, vote `expires_after_session` — uses that convention.
`session_slot()` (calendar date at poll time) survives only for the
daily-report key; never use it for session identity.

## Reading the verdict

| Level | Meaning |
|---|---|
| P1 | `last_assessed` older than the last completed night (classification MISSED); corrupt `regime_state.json`; news suppression latched on a stale session key |
| P2 | never classified (placeholder state); pre-v2.16 `key_format`; flip-counter pause active; recommendation never applied; session P&L never recorded; >20 `NEWS_SUPPRESSED` bars in 24h; any `REAL_ORDER_TIMEOUT` |
| P3 | suppression that is current and by-design; findings on a stopped/retired bot (demoted wholesale — a bot that isn't running cannot classify) |

Healthy vs unhealthy:

| `regime_state.json` | Healthy? |
|---|---|
| `last_assessed` == last completed night's key, `key_format: open-date` | yes |
| `next_session.executed: true` | yes — the swap applied |
| `next_session.executed: false` after the gap ended | NO — blocked by position/veto/replay |
| `paused_until_session >= session_count` | NO — flips froze the regime |
| `{"regime": "unknown", "classified_at": null}` | not yet — fresh deploy or insufficient bars |

## Gotchas

- **`applied` / `applied_at` in `regime_history.csv` are DEAD columns** —
  always `false`/empty by construction. Never read them, never quote them
  as evidence. Application evidence lives in
  `regime_state.json` → `next_session.executed` / `executed_at`.
- **The 30-minute insufficient-bars backoff is invisible.** When the
  classifier lacks bars it silently backs off; the only trace is the
  ABSENCE of a "Classified" line in the debug log. Don't read "no error"
  as "it classified".
- `classification_due` fires in the night's last 2 minutes OR any time
  after close while unassessed — so a catch-up after an app hang is
  normal and its features may include following-DAY bars.
- Poll order is classify → record → apply. `record_session_result`
  UPDATES the row in place, so re-recording never double-counts.
- Votes: `last_features._vote_sources` is the audit trail of what the
  LAST classification consumed. A missing key just means the state
  predates vote-source persistence.
- Bilingual reasons in state/history are expected (`下降趨勢確認
  trending-down confirmed`) — not corruption.

## Next steps

- votes look wrong or absent → `bridge-health`
- classification missed but the bot was alive → `log-scan`, then the
  repo's `debug` skill to root-cause end to end
- n8n side of the vote pipeline → the `n8n-debug` skill
