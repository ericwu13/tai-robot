---
name: start-here
description: Orientation for any tai-robot agent task — which checkout is current (main tree vs worktree vs origin/master), which host runs the live bots, dashboard and news bridges (Steven, not this PC), how to read live state over the dashboard API, and which skill handles what. Use FIRST when starting cold on research, backtest, ops, monitoring or debugging work, or when asked "where do the bots run?", "is this tree up to date?", "where is the live data?", "which skill do I use?".
---

# Start here

Read this before trusting any file you open. The three most expensive
mistakes so far were reading stale code, reading stale data, and looking
for live bots on the wrong machine.

## 1. Which checkout am I reading?

| Tree | What it is | Rules |
|---|---|---|
| `C:\Users\eric8\gitRepos\tai-robot` (main tree) | the owner's working tree on this PC | **can be far behind origin/master** (69 commits on 2026-11-08). Never run pytest, `checkout`, `reset`, `pull`, `stash pop` or anything that moves HEAD there. Read-only. |
| `<main>\.claude\worktrees\<name>` | per-session worktrees | branch new work from `origin/master`, not from the main tree's HEAD |
| `origin/master` | the truth for "what the code does now" | compare before you conclude anything |

First commands in any session:

```bash
git rev-parse --show-toplevel        # am I where I think I am?
git fetch origin -q
git rev-list --count HEAD..origin/master   # >0 = this tree is stale
```

- If your cwd looks empty, or `git ls-tree HEAD` prints nothing, you are
  in a plain folder inside the main tree (for example a fresh
  `.claude/worktrees/<x>` that was never `git worktree add`-ed), not in a
  checkout. Create a real worktree: `git worktree add <path> -b <branch> origin/master`.
- To read origin/master without a worktree: `git show origin/master:path/to/file`.
- CLAUDE.md and the memory index describe origin/master behaviour; the
  main tree may not have it yet.

## 2. Which host runs what

| Thing | Where | How to reach |
|---|---|---|
| all live bots (12, headless `tai_backtest.exe deploy … --resume`) | **Steven** (2nd Windows PC) | `tailscale-ssh` skill |
| bot data dirs (`session.json`, `bars_1m_*.csv`, `debug_*.log`, `.lock`) | Steven `E:\tai_robot_v<version>\tai_backtest\data\live\<SYMBOL>_<bot>` | dashboard API (§3). An ssh read needs permission per §7 |
| bot dashboard (read-only + opt-in controls) | Steven, port 8765, bound to loopback + Steven's tailnet IP | `http://<steven tailnet ip>:8765/` |
| news bridges W1–W4 | Steven scheduled tasks `\TaiRobot\Bridge-W*` | `bridge-health` skill |
| local n8n | stopped (Manual) since 2026-10-24 | only for history |
| this PC's `data\live` | retired / test bots only | do not treat as the fleet |

- The install folder is versioned and moves with each release
  (`v2199` → `v21910` → `v21916` …). Do not hard-code it: read the
  dashboard task's `--base-dir`, or the `steven.install_dir` field of
  your local `hosts.local.yaml` (see `tailscale-ssh`).
- On Steven, **never move the HEAD of `C:\Users\Traderbot\tai-robot-src`**
  (the bridge tasks run from it) or `C:\Users\Traderbot\tai-robot-dashboard`
  (the dashboard runs from it). Put other code in a new worktree of that clone.
- The tailnet IP, user and key path live only in gitignored
  `hosts.local.yaml` / `%USERPROFILE%\.ssh\tailscale-hosts.local.yaml`.
  Never write them into a committed file.

## 3. Read live state without ssh: the dashboard API

All GET, JSON, no auth (the server only answers loopback + tailnet clients,
and checks the `Host` header — use the IP it is bound to, not a made-up name).

| Endpoint | Gives |
|---|---|
| `/api/fleet` | every bot: liveness class, strategy, mode, position, P&L, market state |
| `/api/bot?id=<bot dir name>` | one bot: trades, decisions, log tail (redacted), regime/vote detail |
| `/api/bars?id=<bot>&before=YYYYMMDD&days=N` | that bot's recorded 1-min bars (TPE) — real live data for research |
| `/api/news`, `/api/news/pulse`, `/api/workflows` | bridge/news state |
| `/position-log/<bot>` | per-bot real-order position log |

```bash
curl -s http://<steven tailnet ip>:8765/api/fleet | python -m json.tool | head -50
```

Bot ids are the data-dir names (they carry the symbol prefix, e.g. `TMF00_<bot>`).
POST `/api/control` exists but is a real control channel — never use it
without the owner's explicit say-so.

## 4. Time and clocks

- Every bot timestamp (`bar_dt`, trades, logs) is **TPE (UTC+8)**. This PC's
  OS clock is not TPE. Never compare a TPE stamp to naive `datetime.now()`.
- Before converting TPE to local, check `date +%z` (or on Windows
  `Get-Date -Format K`). TPE stamps are UTC+8. This PC is US Pacific
  with DST; Steven's zone is separate (`tzutil /g` there). Do not
  hard-code a Pacific clock time.
- `decisions.csv` column 0 is the bot machine's local clock — use `bar_dt`.
- TradingView CSV exports downloaded by hand are stamped in the
  exporter's browser zone (US-Pacific for the owner's exports).
  `load_bars_from_csv` does **no** timezone detection — it takes text
  dates as TPE, so such a file loads silently shifted. Only the live
  tvDatafeed download path (`tv_dataframe_to_bars`) detects the source
  zone. Prefer downloading over hand exports; see `strategy-research`.

## 5. Models for subagents

CLAUDE.md prefers Fable for debugging. If a Fable subagent fails with a
usage-credit / "requires usage credits" error, re-run it on Opus and say so
in your report — do not stall the task waiting for Fable.

## 6. Which skill for what

| Task | Skill |
|---|---|
| build / test a strategy, run backtests, pull TradingView or KLine data, drive the AI chat headlessly | `strategy-research` |
| is a bot alive? | `bot-status` (local dirs) — or `/api/fleet` for the Steven fleet |
| deploy / restart / stop a bot | `deploy`, then `deploy-check` |
| regime didn't switch / bot idle | `regime-health` |
| votes / news bridge silent | `bridge-health` |
| pre-2026-10-24 local n8n history | `n8n-debug` (history only — do not run) |
| scary logs, crash | `log-scan`, then `debug` |
| weekly evolution never improves anything | `evo-debug` |
| GitHub issue → root cause → PR | `issue-triage` → `validate-findings` → `fix-pr` |
| daily / weekly health digest | `daily-review`, `weekly-review` |
| shell on Steven | `tailscale-ssh` (not by default; §7) |

## 7. Data host

Host map is §2. Data host = the host `/api/fleet` reports as running the
bot (today: Steven). The output must name the host the data came from.
A run on this PC describes this PC only — never publish it as fleet health.

The fleet path is read-only HTTP (`/api/fleet`, `/api/bot`; address in
`hosts.local.yaml`). No script runs on Steven by default.

`daily_review.py` and `check_logs.py` on Steven are not supported until
those scripts take a base-dir flag (tracked follow-up). Both always read
`REPO_ROOT/data/live` (`scripts/monitor/common.py` `default_base_dir`).
`daily_review.py` writes `data/monitor/*.md` unless `--no-report-file`
(`scripts/monitor/daily_review.py`) and would write that on a live host.
Do not run either there under any permission.
