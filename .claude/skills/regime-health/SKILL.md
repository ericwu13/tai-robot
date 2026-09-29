---
name: regime-health
description: Verify the regime-switching brain — did it classify last night, did the recommendation actually apply, is a flip pause or news suppression freezing it. Use when the regime didn't change, a strategy swap never happened, the bot sits idle, or a regime classification/vote needs auditing.
---

# Regime Health

```bash
C:/Users/eric8/.venvs/tai-monitor/Scripts/python.exe -s scripts/monitor/check_regime.py
```

The monitor runs from the `tai-monitor` venv, not a system-site install.
Create it once from the system interpreter, then install only what
`check_regime` imports:

```bat
C:\Python313\python.exe -m venv C:\Users\eric8\.venvs\tai-monitor
C:\Users\eric8\.venvs\tai-monitor\Scripts\python.exe -m pip install holidays
```

`pip install holidays` is the install. It pulls `python-dateutil` (the
only requirement declared by `holidays` 0.105). Do not use
`pip install -e .`: `pyproject.toml` also depends on `pyyaml`, `keyring`,
`httpx`, `lightweight-charts`, and `pandas`, and `check_regime` never
imports those. The script inserts the repo root on `sys.path` itself, so
an editable install is unnecessary.

Executed chain (a `check_regime(now, base_dir)` call):
`scripts.monitor.check_regime` → `src.regime.switch_logic.holiday_calendar_degraded`
/ `latest_night_session` → `src.market_data.holidays` → `import holidays`
→ `dateutil`. `yaml` lives only in `scripts.monitor.common.load_settings`,
which this checker does not call. `src.live.live_runner` is imported only
by settlement-day helpers in `holidays.py`, which this checker does not
call.

`C:/Users/eric8/.venvs/tai-monitor/Scripts/python.exe -s -c "import holidays"`
is the env gate for this path. A missing `holidays` install makes the
checker report a degraded calendar instead of a missed night.

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
| P1 | `last_assessed` older than the last completed night (classification MISSED) — only when the TW holiday calendar is healthy and that night is not `is_taifex_holiday`; corrupt `regime_state.json`; news suppression latched on a stale session key |
| P2 | never classified (placeholder state); pre-v2.16 `key_format`; flip-counter pause active; recommendation never applied; session P&L never recorded; >20 `NEWS_SUPPRESSED` bars in 24h; any `REAL_ORDER_TIMEOUT`; TW holiday calendar degraded (miss P1 suppressed) |
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
- **Holiday gaps are not residual #151 misses.** With
  `last_assessed=2026-09-24|NIGHT`, a working calendar on Teachers' Day
  morning (reproduced at 2026-09-28 05:17 TPE) still has last completed
  night `2026-09-24|NIGHT`, and `classification_due` returns None. Live
  logs `Classification not due`. Mid-Autumn **2026-09-25** and Teachers'
  Day **2026-09-28** did not open nights. The next real night is Tuesday
  **2026-09-29 15:00** TPE. Do not restart the bot and do not
  hand-advance `last_assessed`.
  The phantom `classification_due → 2026-09-25|NIGHT` happens only when
  the TW `holidays` package has fallen back to weekends. In that state
  `is_taifex_holiday(2026-09-25)` is also false, so a holiday check does
  not refute the tip. Call `holiday_calendar_degraded(now)` first. If it
  is true, refuse the miss escalation: `check_regime` emits a
  degraded-calendar finding (P2) instead of a miss P1. Use
  `is_taifex_holiday` only after that health check says the calendar is
  intact.
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
