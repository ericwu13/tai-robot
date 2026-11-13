---
name: bot-status
description: Check whether a deployed tai-robot bot process is actually alive and still ticking — lock/PID liveness, debug-log freshness, saved session state. Use when the user asks "is the bot running?", "are the bots up?", "fleet status", "is Steven OK?", a bot went quiet, the dashboard shows HUNG/NO_HEARTBEAT/CRASHED, Discord stopped reporting trades, or a deploy needs confirming. Covers local dirs (script) and the Steven fleet (dashboard /api/fleet).
---

# Bot Status

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

## The Steven fleet

First command for "is the fleet alive?":

```bash
curl -s http://<Steven tailnet IP>:8765/api/fleet
```

Read `bots[].state`. The dashboard and
`check_bots.classify_heartbeat` share:
RUNNING = alive; LAGGING = heartbeat stale; HUNG = main loop stopped or a
COM call stuck; NO_HEARTBEAT = PID alive but no usable heartbeat (needs
attention, never "running"); CRASHED = dead lock PID on a recently
active bot; STOPPED = no lock. `mode` ≠ `paper` (page badge 實單) is a
real-order bot — say so before suggesting any stop/restart.

## Fallback, this PC only

For this PC's dirs, not the fleet:

```bash
C:/Users/eric8/.venvs/tai-monitor/Scripts/python.exe -s scripts/monitor/check_bots.py
```

(`-s` ignores user-site packages, so this interpreter sees the
tai-monitor pins from the regime-health skill. Optional arg: a different
`data/live` base directory.)

## What it reads

| Artefact | Fact it establishes |
|---|---|
| `data/live/<bot>/.lock` | the PID that owns the bot |
| Windows `OpenProcess`/`GetExitCodeProcess` | whether that PID is alive |
| newest `debug_YYYYMMDD.log` | last TPE line = last sign of life |
| `session.json` | strategy, trading_mode, cumulative P&L, open position |
| `heartbeat.json` | main-loop tick + in-flight COM call: age >45 s = STALE, >120 s or in-flight >60 s = HUNG (alive PID, frozen loop — issue #157) |
| `dist/tai_backtest/BUILD_INFO.json` (local build; Steven: `<install>\BUILD_INFO.json`) | which version/commit the EXE is |

## Reading the verdict

| Level | Meaning | Do |
|---|---|---|
| P1 | dead-PID lock on a recently active bot, a HUNG heartbeat, or a log frozen >10 min mid-session | act now — the bot is down or hung |
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
- **Liveness is the lock PID, not the image name.** A packaged bot (GUI
  or headless) is `tai_backtest.exe`. A source headless bot is
  `python.exe` with `run_bot_cli.py` on the command line. Do not require
  one name or the other.
- **Log silence in a closed gap is normal** — 13:46–14:59 and 05:01–08:44
  TPE, plus weekends and TAIFEX holidays. The helper only applies the
  10-minute freshness rule when `current_session(now)` is not None.
- **Log stamps are TPE (+8); the OS clock is not** — clocks → `start-here` §4. Never
  compare a log timestamp against a naive `datetime.now()`.
- The check is strictly read-only: it re-implements PID liveness rather
  than calling `LiveRunner.check_lock`, which DELETES stale locks.

## Next steps

- bot alive but not trading → `regime-health` (idle leg? news gate?)
- bot dead → read the tail of its newest `debug_*.log`, then `log-scan`
- needs restarting (paper/auto) → `deploy` (on Steven, via `tailscale-ssh`, permission per `start-here` §7); semi_auto → `deploy-check` workbench recipe
- host map → `start-here`
