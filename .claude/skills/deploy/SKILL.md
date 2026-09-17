---
name: deploy
description: Deploy, restart or stop a tai-robot live bot headlessly with run_bot_cli.py — resolve the strategy name, gate real-order modes on explicit user consent, launch the bot as a detached process, and confirm it reached tick subscription. Use when asked to deploy/start/restart/stop a bot, to bring a bot back after a release or crash, or to run a bot without the Tk workbench.
---

# Deploy (headless)

`run_bot_cli.py` runs the SAME live machinery as the workbench on a hidden
Tk window, so a CLI bot behaves exactly like a GUI bot. Everything below
runs from the **main working tree on `master`**:

```
cd C:\Users\eric8\gitRepos\tai-robot
python run_bot_cli.py <command> ...
```

Running python out of the main tree is what every bot does; *editing* it
is still forbidden (see `fix-pr`). A git worktree cannot deploy — it has
no `settings.yaml`, no `CapitalAPI_2.13.57/`, and a different `data/live`.

## Commands

| Command | Does |
|---|---|
| `strategies` | display names the deploy accepts (incl. saved `AI: …` ones) |
| `status --symbol S --bot B --json` | lock/PID liveness + `session.json` summary (read-only) |
| `list [--symbol S]` | every `data/live/{symbol}_{bot}` |
| `deploy --symbol S --bot B … --detach --wait-ready 240` | spawn the bot detached, wait until it is live |
| `stop --symbol S --bot B --wait 120` | STOP file → force-close, session save, daily report, exit |

Deploy flags: `--strategy "<name or unique substring>"`, `--mode paper|auto`
(default paper), `--loss-limit NTD`, `--regime --long-strategy … --short-strategy …`,
`--news [--news-tier2] [--news-directional]`, `--new` / `--resume`,
`--allow-existing-position`, `--point-value N`.

## Gates — check ALL before the deploy command

1. **Mode is the user's call, never inferred.** Default is `paper`.
   Pass `--mode auto` only when the user asked for real orders for THIS
   bot in THIS conversation. A `session.json` whose `trading_mode` is
   `auto`/`semi_auto` does NOT carry over — the CLI restarts it as paper
   unless told otherwise, so when restarting a real-order bot, ask:
   "resume as paper, or auto (real orders)?". `semi_auto` cannot run
   headless at all (10-second order-confirm dialog) — those bots restart
   from the workbench (`deploy-check` has the recipe).
2. **Not already running.** `status` must show `running: false`. If it is
   running, deploying again is refused (exit 4, lock conflict); a
   restart is `stop` first (below). `.lock` is never deleted by hand.
3. **Strategy name resolves to exactly one entry** — run `strategies`
   and pass the display name (or a substring only it contains; the CLI
   errors on ambiguity, exit 1). Regime legs likewise.
4. **Resume vs strategy switch.** If `session.json` exists the bot
   resumes automatically (trades, watermark, open position). Passing
   `--strategy` that differs from the saved one is a deliberate
   *strategy switch on the same history* (the evolution hand-off path)
   — do it only when the user asked for the switch; otherwise omit
   `--strategy` and let the saved one load.
5. **`--allow-existing-position` only on explicit instruction.** It means
   the real account already holds a position the bot did not open; the
   bot will never send exits for it.

## Procedure

```bash
python run_bot_cli.py status --symbol TMF00 --bot night1 --json     # gate 2, read saved strategy/mode
python run_bot_cli.py strategies                                    # gate 3
python run_bot_cli.py deploy --symbol TMF00 --bot night1 --strategy "H4 Bollinger Long" \
    --detach --wait-ready 240
```

The launcher spawns the bot as a detached process (stdout →
`data/live/{symbol}_{bot}/cli_stdout.log`) and blocks until one of:

| Launcher prints | Exit | Meaning | Do |
|---|---|---|---|
| `READY: … tick subscription active` | 0 | logged in, warmup done, `RequestTicks` OK, `.lock` owned by the child | report PID + bot dir; done |
| `EXITED: … code N` | N | the bot died before going live; the last 15 stdout lines are printed | read them: 2 = login/connection, 4 = deploy refused (lock, unknown strategy, position on account), 5 = warmup/subscription failed — see `debug_YYYYMMDD.log` |
| `TIMEOUT: PID … still running` | 6 | still warming up (long CSV reload on resume) | **the bot is still running** — poll `status` and tail `cli_stdout.log`, do not relaunch |

Then confirm: `status` shows `running: true` with that PID, and the
stdout tail has `已訂閱 Tick subscription active`. A paper bot posts the
same Discord "bot deployed" message the GUI would.

**Stop:**

```bash
python run_bot_cli.py stop --symbol TMF00 --bot night1 --wait 120
```

Only CLI-started bots poll the STOP file (their bot dir has a fresh
`cli_stdout.log` with `[HEADLESS]` lines). A workbench bot ignores it —
tell the user to press 停止 in that window. After a clean stop the lock is
gone, `session.json` `saved_at` is fresh, and a daily report was written.

**Restart** = `stop --wait 120` → verify lock gone → `deploy … --detach`
(resume is automatic; apply gate 1 to the mode).

## Gotchas

- Each bot process logs into the Capital API on its own — identical to
  one workbench window per bot, which is how the account already runs
  several bots at once.
- Outside a session (13:46–14:59, 05:01–08:44 TPE, weekends, TAIFEX
  holidays) the bot still goes READY, then sits quiet until the next
  open; "no ticks" there is normal. Deploying mid-session replays
  history ticks first (`suppress_strategy` until the first live tick).
- A leftover `STOP` file is cleared by the next deploy; a stale `.lock`
  (dead PID) is self-healed by the deploy — never delete either by hand.
- The bot needs an interactive desktop session (hidden Tk window). Do
  not wrap it in a LocalSystem service or a scheduled task set to "run
  whether user is logged on or not".
- Stdout timestamps are TPE (with local time appended when they differ);
  the OS clock is UTC-7. `[AI] ultra_mode=…` on the first line is normal.
- Never print credentials, channel IDs, or the account number — the CLI
  redacts them; keep it that way in reports.
- Releases do not touch a running CLI bot; after merging a change the bot
  needs a `stop` + `deploy` to pick it up.

## Next steps

- confirm liveness later → `bot-status`
- wiring doubts (news paths, Discord, regime legs) → `deploy-check`
- regime bot idle after deploy → `regime-health`
