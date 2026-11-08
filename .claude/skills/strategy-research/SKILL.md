---
name: strategy-research
description: Research, build and backtest a tai-robot trading strategy headlessly — where historical bars come from (TradingView download, Capital COM KLine, the live bots' 1-min CSVs on Steven), how to run the same backtest the workbench runs, how to drive the AI chat → Generate Strategy → AI Review loop without the GUI, and the holdout discipline that keeps results honest. Use when asked to "build/design/test a new strategy", "backtest X", "pull TradingView data", "get historical bars", "act as the owner in the AI chat", "why doesn't evolution improve strategies", or to compare strategy variants.
---

# Strategy research

Start with `start-here`: work in a worktree cut from origin/master, and
remember the live fleet's data is on Steven.

## 1. Pick a data source (never `data/*.csv`)

The committed `data/*.csv` exports stop at 2026-03 and mix time zones; no
code reads them. Do not use them and do not ask the owner to export by hand.

| Source | Use for | How | Limits |
|---|---|---|---|
| **Live bots' 1-min bars** | recent months, real live data (TPE) | Steven: `python -m scripts.research.data dashboard http://<Steven tailnet IP>:8765 TMF00_<bot> --days 120 --out bars.pkl` (read-only GET of `/api/bars`, pages 10 days per call). Local dirs: `… data dirs <bot_dir>…` | only since that bot's first deploy; gaps where it was down — `describe` prints gaps >4 days |
| **TradingView** (tvDatafeed, no login) | longer history at 30m and above | `python -m scripts.research.data tv TMF00 1H --out tv_1h.pkl` → `kchart_fetcher.fetch_tv_dataframe` + `tv_dataframe_to_bars` (detects the source zone, converts to TPE) | **5,000 bars per request**: 30m ≈ 6 months, 1H ≈ 13 months, 4H ≈ 3.4 years, 1D ≈ back to 2012. 1-min ≈ 4–5 trading days (≈1,140 bars per day+night) |
| **Capital COM KLine** | exchange-official history; same feed as the live bots (Capital 30m = our 1-min-derived 30m on overlap) | `run_bot_cli.py backtest --source api …` (logs in, chunked `RequestKLineAMByDate`) — or a script on the `test_kline.py` pattern | **Minute KLine (1–60m) only reaches back to 2025-11-28 (~11 months); daily (type 4) from 2010.** Earlier ranges return code 0 with 0 rows — silently, not an error. Needs settings.yaml credentials + the SDK (main tree or an install, not a worktree). |

Gotchas:
- **A headless COM pull needs a Tk message pump**: create `root = tk.Tk(); root.withdraw()` and loop
  `root.update()` while waiting. With `comtypes.client.PumpEvents` no `OnConnection` event is
  delivered and `SKQuoteLib_IsConnected()` stays at 2 forever — it looks like a weekend/service
  outage but is not. Login → ready (3001 → 3003) takes ~13 s. `run_bot_cli.py backtest --source api`
  already pumps Tk. A second login from this PC with the same account did not disturb the Steven
  fleet (verified 2026-11-09, incl. the real-order bot) — still, ask the owner before logging in.
- **Symbol mapping:** `SYMBOL_CONFIG` (`src/market_data/kline_config.py`)
  maps TMF00→`IMF1!` and MTX00→`TMF1!` on origin/master — both wrong. The
  fix (TMF00→`TMF1!`, MTX00→`MXF1!`) is draft PR #260; until it merges,
  pass the raw TradingView symbol (`… data tv TMF1! 1H`). Do not re-fix it here.
- **Time zones:** every Bar in this repo is naive TPE, labelled by bar
  OPEN time. Hand-exported TradingView CSVs are stamped in the exporter's
  browser zone (US-Pacific for the owner), and `load_bars_from_csv` does
  NOT detect that — such a file loads shifted by 15–16 h. Only the
  tvDatafeed path (`tv_dataframe_to_bars`) detects the zone.
- **TradingView vs live bars differ slightly** (session boundaries, volume).
  Stitching TV history to live 1-min bars is fine for a long design window;
  keep the holdout on one source.
- TMF (微台) and MTX (小台) track the same index; TMF's own history is
  short. For long horizons TX/MTX history is the usual proxy — say so.

## 2. Backtest exactly like the workbench

Quick, named strategy (registered or saved `AI:` one), the GUI path itself:

```bash
python run_bot_cli.py backtest --symbol TMF00 --strategy "<name>" \
    --start 20260101 --end 20261031 --source tv [--export trades.csv]
```

A strategy file, variants, holdout split, folds:

```bash
python -m scripts.research.bt bars.pkl cand/a.py cand/b.py --symbol TMF00 \
    --design 2026-03-17:2026-09-15 --holdout 2026-09-16:2026-11-08 --cost 70
```

`scripts/research/bt.run` builds `BacktestEngine(strategy, point_value=pv,
fill_mode="next_open")` — what `_execute_backtest` in `run_backtest.py`
does. **The engine's own default is `on_close`; never omit `fill_mode`.**
Point value comes from `SYMBOL_CONFIG` (TMF00 = 10, MTX00 = 50, TX00 = 200).
Feed 1-min bars; the engine aggregates to the strategy's `kline_minute`
itself. `--cost` subtracts NT$ per round trip (≈70 for 1 TMF lot: fees + tax
+ ~2 pts slippage) — the simulator's `pnl` is gross.

## 3. The AI loop without the GUI

`scripts/research/chat.py` sends the same prompts, tiers and message text
as the workbench (the AI Review text comes from `src/ai/review_payload.py`,
which the GUI also calls; a test pins the codegen wording to `run_backtest.py`):

```bash
python -m scripts.research.chat say    s1 msg1.txt   --settings C:\Users\eric8\gitRepos\tai-robot\settings.yaml
python -m scripts.research.chat gen    s1 cand/a.py  --settings …
python -m scripts.research.chat review s1 cand/a.py  --settings … --bars bars.pkl --design 2026-03-17:2026-09-15
```

| Step | GUI equivalent | Prompt / tier |
|---|---|---|
| `say` | 傳送 in the chat panel | `STRATEGY_SYSTEM_PROMPT`, light tier (flash) |
| `gen` | Generate Strategy (+2 auto-retries) | one-shot `build_summary` + `STRATEGY_CODE_CONTEXT` with `CODE_GEN_SYSTEM_PROMPT`, heavy tier |
| `review` | AI Review | `review_payload` preamble (7 analysis rules + REVERT-FIRST degradation gate), heavy tier |

- Heavy tier = gemini-3.8-flash, or gemini-3.1-pro-preview when
  `ai.ultra_mode` / `ULTRA_MODE=1`; `--ultra` forces the ultra model for
  every call. Anthropic users get their configured model.
- A worktree has no `settings.yaml` — pass the main tree's with
  `--settings` (read only; never print the key). Set
  `TAI_AI_USAGE_LOG=<scratch>/ai_usage.csv` so research calls don't land in
  the bot's usage log.
- "Pretend to be the owner": write each `say` message as the owner would
  (goal, constraints, what they dislike), one idea per turn; let the
  review's ONE-change rule drive iterations.

## 4. Holdout discipline

- Fix DESIGN and HOLDOUT windows **before** the first run; the holdout is
  never sent to the model and never used to choose parameters.
- Report both windows, monthly P&L and the 14-day folds (`folds+ x/y`,
  worst fold). A variant that only wins one fold is luck.
- Report net of `--cost`, number of trades, and long/short split.
- A sweep over many parameters is a multiple-comparison test: keep the
  best 2–3 and re-check them on the holdout once.
- Stop when the review says metrics are acceptable (PF > 1.3, DD < 25%,
  WR > 40%) — more tuning is overfitting.

## 5. Relation to evolution

The weekly evolution pipeline (`src/evolution/`, see `evo-debug`) runs the
same engine on each bot's own live CSV history with its own gates
(multifold, month gates, knob mode). A candidate from this skill reaches a
bot only by the owner saving it as a strategy and redeploying (`deploy`).

## Never

- Never deploy a research candidate yourself; hand it to the owner.
- Never write to a Steven bot dir, `POST /api/control`, or the main tree.
- Never commit `settings.yaml`, API keys, session JSONs containing keys,
  or bar dumps.
