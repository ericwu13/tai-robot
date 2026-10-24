---
name: bot-status
description: Check whether a deployed tai-robot bot process is actually alive and still ticking — lock/PID liveness, debug-log freshness, saved session state. Use when the user asks "is the bot running?", a bot went quiet, Discord stopped reporting trades, or a deploy needs confirming.
---

# Bot Status

One read-only command answers "is it alive":

```bash
C:/Users/eric8/.venvs/tai-monitor/Scripts/python.exe -s scripts/monitor/check_bots.py
```

(`-s` ignores user-site packages, so this interpreter sees the
tai-monitor pins from the regime-health skill. Optional arg: a different
`data/live` base directory.)

## Bots on another PC

A bot listed under a host's `monitor.bots` in the gitignored tailscale
hosts file runs THERE (since 2026-10-24: `TMF00_08-04-rss-機器人` on
`steven`). The directory of the same name in this tree's `data/live` is
a stale copy left by the move — the daily review ignores it, and so must
you. Check the real one:

```bash
C:/Users/eric8/.venvs/tai-monitor/Scripts/python.exe -s scripts/monitor/remote.py --check bots
```

One ssh call copies `.lock`, `heartbeat.json`, `session.json`, the
regime/decision files and the newest two debug-log tails into
`data/monitor/remote/<host>/live/<bot>/` (remote mtimes kept) and probes
the lock PID with `Get-Process` on that host — same rules as below. Two
differences: a configured remote bot with **no `.lock` is P2** (it was
put there to run; locally "no lock" is P3 because `data/live` is mostly
retired bots), and a missing bot directory is P2. Host unreachable = P1
"UNVERIFIED". To restart it, use the `deploy` skill on that host over
`tailscale-ssh` (that host also has a `\TaiRobot\Launch-08-04-rss`
scheduled task — `remote.py --check tasks` shows its last run).

## What it reads

| Artefact | Fact it establishes |
|---|---|
| `data/live/<bot>/.lock` | the PID that owns the bot |
| Windows `OpenProcess`/`GetExitCodeProcess` | whether that PID is alive |
| newest `debug_YYYYMMDD.log` | last TPE line = last sign of life |
| `session.json` | strategy, trading_mode, cumulative P&L, open position |
| `dist/tai_backtest/BUILD_INFO.json` | which version/commit the EXE is |

## Reading the verdict

| Level | Meaning | Do |
|---|---|---|
| P1 | dead-PID lock on a recently active bot, or a log frozen >10 min mid-session | act now — the bot is down or hung |
| P2 | unparsable lock, unreadable `session.json` | investigate today; a resume would start flat |
| P3 | no lock (not running), abandoned stale lock, dirty build | informational |

Healthy vs unhealthy:

| Situation | Healthy? |
|---|---|
| lock present, PID alive, log line <10 min old, market open | yes |
| lock present, PID alive, log silent for hours, market CLOSED | yes — no ticks flow in a gap |
| lock present, PID **dead**, bot was active in the last 3 days | NO — it crashed |
| no lock at all | depends: only the user knows if that bot should be running |

## Gotchas

- **A clean stop deletes the lock.** So a lock file whose PID is dead is
  never "leftover from a tidy shutdown" — it is the fingerprint of a
  crash. (`data/live` also holds retired test bots whose locks were never
  cleaned; the helper demotes those to P3 by checking last activity, so
  only recent crashes are graded P1.)
- **Never delete a `.lock` by hand.** The app self-heals a stale lock on
  the next deploy of that bot. Deleting one manually can let a second
  instance attach to the same account.
- **Log silence in a closed gap is normal** — 13:46–14:59 and 05:01–08:44
  TPE, plus weekends and TAIFEX holidays. The helper only applies the
  10-minute freshness rule when `current_session(now)` is not None.
- **The OS clock is UTC-7 while log stamps are TPE (+8).** Never compare a
  log timestamp against a naive `datetime.now()`.
- The check is strictly read-only: it re-implements PID liveness rather
  than calling `LiveRunner.check_lock`, which DELETES stale locks.

## Next steps

- bot alive but not trading → `regime-health` (idle leg? news gate?)
- bot dead → read the tail of its newest `debug_*.log`, then `log-scan`
- needs restarting headlessly (paper/auto) → `deploy`; semi_auto → `deploy-check` for the workbench recipe
- needs restarting → `deploy-check` for the manual redeploy recipe
