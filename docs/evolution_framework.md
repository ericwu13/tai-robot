# Strategy Evolution Framework

> Knowledge-graph reference for the 🧬 Bot Evolution engine in tai-robot.
> Last updated for **v2.12.0**.

---

## Concept map (entities & relationships)

```
                         ┌──────────────────────┐
                         │  🧬 Bot Evolution    │  trigger: manual button OR weekly auto
                         └──────────┬───────────┘
                                    │ reads
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
     ┌────────────────┐   ┌──────────────────┐   ┌──────────────────┐
     │  watermark     │   │  design window   │   │  holdout (14d)   │
     │ (already seen) │──▶│ trades[wm:cut]   │   │ trades[cut:end]  │
     └────────────────┘   │  → AI sees these │   │  → WITHHELD      │
              ▲           └────────┬─────────┘   └────────┬─────────┘
              │ advances to cut_idx │ designs ONE change   │ becomes the
              │                     ▼                      │ verdict's test
              │            ┌──────────────────┐            │ window
              │            │  AI plan (advice)│            ▼
              │            └────────┬─────────┘   ┌──────────────────┐
              │                     │ apply (human │  walk-forward    │
              │                     ▼  or auto-gen)│  validation      │
              │            ┌──────────────────┐    │  train 90d /     │
              └────────────│  candidate       │───▶│  test = holdout  │
                  on PASS  │  strategy        │    │  + Monte Carlo   │
                           └──────────────────┘    └────────┬─────────┘
                                                            ▼
                                                   ┌──────────────────┐
                                                   │  verdict         │
                                                   │  PASS → save     │
                                                   │  FAIL → discard  │
                                                   └──────────────────┘
```

Relationships in words:
- The **watermark** marks trades already consumed as design evidence; it advances to the **design cut** after each run.
- The **design window** (between watermark and cut) is the only trade evidence the **AI** sees when it writes a **plan**.
- The **holdout** (most recent 14 days of trades) is withheld from the AI and reappears as the **test window** of the **walk-forward validation**.
- The **verdict** is decided on the unseen test window; a PASS saves a **candidate strategy**; deployment is always a separate human step.

---

## 1. Overview

The Evolution Engine is a **FunSearch / AlphaEvolve-inspired** loop: an LLM proposes a change to a trading strategy, the change is mechanically backtested on data the LLM never saw, and only changes that survive out-of-sample validation are kept. It is **human-in-the-loop by design** — the AI is a *strategy advisor*, never an autonomous trader:

- The AI **proposes** exactly one change per cycle (a parameter tweak or a single structural change) plus its own pass/fail criteria.
- The pipeline **generates and validates** a candidate, but **never deploys** it. A PASS produces a saved, selectable strategy; pointing real money at it is always a separate human click.
- The loop is intentionally **slow and conservative**: one change at a time, validated out-of-sample, with the rolling holdout and (recommended) paper incubation guarding against the over-fitting that kills auto-tuned strategies.

What it is **not**: there is no live parameter optimizer and no automatic redeploy. The genetic "gene pool" (`pool.py`) is scaffolding for a future phase and is not wired into the live loop today.

---

## 2. Data windows

Two independent splits cooperate. The **trade evidence** split decides what the AI sees; the **backtest validation** split decides the verdict. They are aligned so the AI's blind spot (recent 14 days) is exactly the window the verdict is graded on.

```
TRADE EVIDENCE (the bot's own closed trades, chronological)
                watermark                       cut = last_exit − 14d
                   │                                  │
  #1 ............. │ ===== design window ===========  │ ░░ holdout (14d) ░░  #N
   already         │   AI SEES these trades            │   WITHHELD from AI
   analyzed        │   (writes the plan from them)     │   (validation grades here)
  (omitted)        ▼                                  ▼

BACKTEST VALIDATION (≈194d of native-timeframe bars, fetched via Capital API)
   [────────── train 90d (in-sample) ──────────][──── test 14d (out-of-sample) ────]
                                                  ▲ SAME 14-day window as the holdout
                                                  └ the verdict is decided HERE
```

- **Design window** = `trades[watermark : cut_idx]`, where `cut_idx` is the first trade whose exit is within the last `holdout_days` (14) of the most recent trade's exit. These trades, their metrics, and their fitness are the *only* evidence in the AI prompt.
- **Holdout** = `trades[cut_idx : end]` — the most recent ~14 days of trades. Never shown to the AI, not even as aggregates.
- **Train window (90d)** and **test window (14d)** are the walk-forward split of the *bars* (not trades). The test window coincides with the holdout period, so a change that secretly chases the recent trades cannot pass — the AI never saw them, and they decide the verdict.
- **Why the cut rolls** — the cut is anchored at the **last trade's exit minus 14 days**, not the wall clock. Each week the latest trade is newer, so the cut advances and last week's holdout ages into this week's design window. An idle bot (no new trades) keeps a real holdout instead of collapsing it to zero.

---

## 3. Watermark

| | |
|---|---|
| **What** | A per-bot high-water mark: `{"trade_count": N, "at": "<TPE timestamp>"}`. Trades `#1..#N` are considered "already used as design evidence." |
| **Where** | `data/live/{symbol}_{bot_name}/evolution_watermark.json` — alongside `session.json`. Gitignored (per-bot runtime state). |
| **When updated** | At the end of every evolution run, in `_bot_evolution` → `save_watermark(bot_dir, cut_idx)`. |
| **Why `cut_idx`, not `len(trades)`** | The held-out trades (`cut_idx..end`) were never shown to the AI, so they must remain available as **design evidence in the next cycle**. Advancing to `len(trades)` would consume trades the AI never analyzed, permanently skipping them. Advancing to `cut_idx` is what makes the holdout *roll forward* week over week. |
| **Reading it** | `load_watermark()` returns `None` for a missing/corrupt file or a non-positive count (treated as "never run"). |

The watermark is a high-water mark rather than per-trade flags because `broker.trades` is append-only within a session, so a single integer identifies the consumed set exactly.

---

## 4. Weekly cadence

The automatic run is pinned to the **end of the trading week**, which in Taiwan futures is the **Friday night session that closes Saturday ~05:00 TPE**.

- **Session-end fitness check** (`should_run_evolution_check`, cadence `"weekly"`) fires on **TPE Saturday** at the ~04:58 session-end poll. This is the cheap fitness scoring + Discord improvement notification — *no AI*.
- **Post-close evolution pipeline** (`is_post_close_evolution_window`) fires on **TPE Saturday ≥ 05:05** via `_check_weekly_evolution` on the 30-second status poll. The 05:05 buffer guarantees the session-end force-close and final daily report have settled, the market is quiet, and the week's data is complete. It is **latched per ISO week** so it runs exactly once; a bot deployed later on Saturday still gets its run on the first post-deploy poll.
- Both require `evolution.auto_pipeline: true`, a `RUNNING` bot, and (for the pipeline) a closed market.

**"Skipped" is correct, not a bug.** If the run finds no *new design-window trades* (`cut_idx <= watermark`), it logs `🧬 EVO (weekly auto): … skipped` and stops. This happens whenever every trade since the last run is still inside the 14-day holdout — i.e., the bot has traded recently but nothing has aged across the cut yet. The design deliberately makes the AI work on evidence that is **14–21 days old**; right after a run, or right after a fresh deploy, that older-evidence pool has nothing new, so skipping is the intended same-sample-protection behavior. It self-heals: as trades age past the rolling cut, the next Saturday fires normally. When that skip happens and the last exit is at least `evolution.idle_warn_days` calendar days ago (default 7), the same message gains a line `idle N days since last exit <date>` so a quiet book is visible next to a holdout wait.

---

## 5. The `MIN_TRADES` gate

`fitness.py` forces the composite fitness score to **0.0 when there are fewer than `MIN_TRADES = 30` trades** in the scored window. Small samples score well by luck; gating prevents a lucky 5-trade week from propagating as a "good" strategy.

**Practical implication:** a freshly deployed bot needs roughly **2–4 weeks of live trading** (enough closed trades to cross 30 in the design window) before evolution produces a *meaningful* plan. Before that:
- The **Saturday/automatic** plan call is skipped when the design window has fewer than `evolution.min_design_trades` trades (default 30, the same number as `MIN_TRADES`). Chat and Discord get `waiting for sample: N/30`, no AI call is made, and the watermark does not advance. This skip is an approved behavior change for sub-30 samples on the automatic path only. The manual Evolve button is exempt: it logs `below min_design_trades (N/30), manual override` and still calls the AI. Set the key to `0` to disable the check.
- The fitness composite is still `0.000 (gated)` below `MIN_TRADES`. That math is unchanged. With the check disabled, the AI is told the sample is low-confidence, as before.
- The walk-forward verdict also notes when both sides are gated (`OOS composite gated on both sides`) and leans on the absolute/relative metric gates instead. Those gates are unchanged. Bots with at least `min_design_trades` design-window trades keep the previous plan-call path.

This is why the weekly cadence (not daily) is the default — a single trading day almost never crosses 30 trades, so daily scoring would be pure gated noise.

---

## 6. Evolution plan flow

The plan is **advisory**. The full path from plan to deployed strategy:

1. **Plan** — the AI returns a 5-section plan (summary / what's working / diagnosis / one change / validation+revert) ending in a machine-readable `json` directives block (`action`, and the plan's own pass criteria such as `max_drawdown_pct_max`, `profit_factor_min`).
2. **Candidate codegen** — the plan + current source go to the codegen persona, which applies *exactly one* change and renames the class `…EvoN`. Sandbox-validated (forbidden imports/indicators rejected), one automatic retry on failure.
3. **Walk-forward validation** — baseline vs candidate backtested on the train (90d) and test (14d=holdout) windows; Monte Carlo robustness on the candidate (±10% jitter of numeric `__init__` defaults).
4. **Verdict** — the candidate must clear, on the **out-of-sample test window**:
   - Monte Carlo not fragile (CV ≤ 0.30),
   - OOS composite ≥ baseline (skipped when both gated),
   - design-window non-collapse (candidate train PF ≥ 0.8× baseline, train MaxDD ≤ 1.5× baseline),
   - the plan's own criteria (drawdown checked on the long train window; others on the holdout; holdout drawdown gated *relative* to baseline ≤ 1.2×),
   - a trade floor of `max(5, 25% of the baseline's same-window count)`.
   A PASS with < 30 holdout trades is flagged **PROVISIONAL**.
5. **On PASS** — the candidate is saved to the StrategyStore, appears in the dropdown as `AI: …EvoN`, and is logged to `data/changelog.json` with `initiated_by="ai"`. **Deployment is NOT automatic.**

**Applying a change manually** (the human-in-the-loop path, also valid for plan-only mode): the plan lives in the chat, so ask the chat to apply it and click **Generate Strategy**, backtest the new version against the plan's own criteria, then **Save** + redeploy, or discard per the plan's revert condition. Either way, redeploy on the **same bot session** so the broker history, watermark, and fitness baseline carry over (the deploy dialog asks before reverting your strategy selection — answer "deploy selected").

**Forward incubation (recommended).** A PASS is an *in-sample-adjacent* result, not proof of live edge. Deploy the candidate in **paper mode** on the same session for **≥14 days / ≥30 trades** before switching to `semi_auto` — the future is the only true out-of-sample test (mirrors the pool's `eligible_for_live` thresholds).

---

## 7. The 🧬 manual button

The toolbar **🧬 進化 Evolution** button runs the same pipeline on demand. Use it when you want to evolve *now* rather than waiting for Saturday — e.g. after a notably bad week, or to test the pipeline.

Differences from the weekly auto run:
- **No-new-design-trades handling.** Weekly auto **skips silently** (with a Discord notice). The manual button instead shows a **re-run dialog**: "no new design-window trades since the last evolution — re-run on the full design history anyway? (holdout stays withheld)." Answering *yes* re-analyzes the whole design window (`omitted` reset to 0); the 14-day holdout is still withheld from the AI either way. When that skip's last exit is at least `evolution.idle_warn_days` calendar days ago, both messages gain `idle N days since last exit <date>`. `idle_warn_days: 0` leaves the line off.
- **Small design window.** `evolution.min_design_trades` (default 30) gates the Saturday/automatic plan call only. The manual button bypasses it and logs `below min_design_trades (N/30), manual override` before the plan call.
- **Verdict visibility.** Both the manual and weekly runs post the verdict block to Discord (manual included, for testing/visibility) and to the chat log.
- **Ultra mode.** Whichever model the 🚀 session toggle / settings default selects is used for the plan and codegen (heavy tier).

What is identical: the design/holdout split, the walk-forward validation, the verdict gates, the auto-save-on-PASS, and the manual-only deployment.

---

## 7.1 Gate-decision history

Every evolution attempt appends one JSON line to `data/live/{symbol}_{bot}/evolution_history.jsonl` (next to `evolution_watermark.json`). The line covers `skip`, `no_change`, `refused`, `fail`, and `pass`, including the deep, multifold, month, and promotion-gate reasons. `retire_week` stores `{at, evaluated, passed}` so a #188 streak can be derived with `weeks_from_history`.

The file is append-only and **not size-capped or rotated**. Growth is unbounded on purpose: one line per attempt is small, and trimming the tail would drop weeks the streak derivation still needs. Delete or archive the file by hand if it ever needs a bound. There is no settings key for a cap. A write failure (including a Windows `os.replace` sharing violation after the heartbeat writer's bounded retry) is logged as a warning and does not stop the evolution run.

`assess_retirement` keeps reading the changelog (`weeks_from_changelog`). Switching that live reader to this history file is a later follow-up, so an empty history file does not reset an existing changelog streak.

---

## 8. Key files

| File | Role |
|---|---|
| `src/evolution/fitness.py` | Pure scoring: composite from Sharpe/Sortino/drawdown/PF/win-rate/consistency/regime; `MIN_TRADES=30` gate; drawdown stored in **percent** units. |
| `src/evolution/pipeline.py` | The auto-pipeline logic: `parse_plan_directives`, `compute_design_cut` / `design_cutoff_index`, `split_train_test`, `run_deep_validation`, `decide_deep_verdict`, candidate naming, verdict formatting. Pure & unit-tested. |
| `src/evolution/notify.py` | Watermark (`load_watermark`/`save_watermark`), fitness baseline (`evolution.json`), cadence gates (`should_run_evolution_check`, `is_post_close_evolution_window`), Discord improvement notification. |
| `src/evolution/evaluator.py` | Standalone two-phase batch evaluator (screen/deep) for the future GA. The pipeline borrows only `DEEP_TRAIN_DAYS` and `monte_carlo_robustness` from it. |
| `src/evolution/pool.py` | SQLite "gene pool" with lineage + status pipeline and `eligible_for_live` promotion gate. **Scaffolding — not wired into the live loop yet.** |
| `run_backtest.py` | GUI glue: `_bot_evolution` (orchestration), `_start_evolution_pipeline` (worker thread, AI calls, API fetch), `_check_weekly_evolution` (post-close trigger), `_build_evolution_context`. |
| `data/live/{symbol}_{bot}/evolution_watermark.json` | Per-bot design-cut high-water mark (gitignored). |
| `data/live/{symbol}_{bot}/evolution.json` | Per-bot fitness baseline (best composite so far). |
| `data/live/{symbol}_{bot}/evolution_history.jsonl` | Append-only gate-decision log, one JSON line per attempt. Not size-capped or rotated (see §7.1). |
| `data/changelog.json` | Audit trail of applied changes (`initiated_by` = `ai` or `human`). `assess_retirement` still reads this file. |

---

## 9. Configuration (`settings.yaml`)

```yaml
evolution:
  cadence: "weekly"        # "weekly" (default) | "daily" — when the fitness check runs
  auto_pipeline: true      # run the full plan→codegen→validate pipeline; also enables the weekly auto run
  validation: "deep"       # "deep" (walk-forward + Monte Carlo) | "simple" (single-window A/B)
  holdout_days: 14         # most-recent-N-days withheld from the AI and used as the OOS test window
  min_design_trades: 30    # automatic path only: skip the plan call below this count; 0 disables
  idle_warn_days: 7        # watermark-skip idle line; 0 disables the line
  knob_ineligible_fallback: "none"  # "none" (default) | "structural_lane"
                                    # neither value enables weekly codegen
  month_gate:
    latest_def: merge_stub       # merge_stub (default) | max_exit_month (opt out)
    min_sessions: 12             # fold the newest month below this many TAIFEX dates
    slack:
      mode: scaled               # scaled (default) | flat (opt out: flat_twd, gate 3 Δ ≥ 0)
      flat_twd: 5000
      k: 1.0
      gate3_k: 0.5               # strict check uses this fraction of the scaled allowance
      floor: 3000
      cap: 15000

trading:
  allow_live_override: false   # NOTE: lives under `trading:`, not `evolution:`
```

| Key | Default | Meaning |
|---|---|---|
| `evolution.cadence` | `"weekly"` | `"weekly"` = fitness check only on the Saturday end-of-week session; `"daily"` = every session end (pre-2.12, mostly gated noise). |
| `evolution.holdout_days` | `14` | Days of most-recent trade evidence withheld from the AI; also the walk-forward test-window length. |
| `evolution.auto_pipeline` | `true` | Enables the one-click pipeline *and* the weekly post-close auto run. `false` = plan-only (no codegen/validation/auto-save). |
| `evolution.validation` | `"deep"` | Validation method (see §6). `"simple"` skips the walk-forward split and Monte Carlo. |
| `evolution.min_design_trades` | `30` | Saturday/automatic path only: skip the AI plan call while the design window has fewer trades than this (`waiting for sample: N/<threshold>`, 0 AI calls, watermark unchanged). The manual Evolve button is exempt and logs `below min_design_trades (N/30), manual override`. `0` disables the check. Does not change the fitness `MIN_TRADES` gate. |
| `evolution.idle_warn_days` | `7` | On a watermark skip, append `idle N days since last exit <date>` when the last exit is at least this many calendar days ago. `0` disables the line. |
| `evolution.knob_ineligible_fallback` | `"none"` | Weekly handling of a knob-grid class with fewer than 2 rewritable knobs. `"none"` excludes it and logs `excluded: <class> — <reason>; fallback=none` every Saturday. `"structural_lane"` only changes that logged mode; it does not enable weekly codegen. |
| `evolution.month_gate.latest_def` | `"merge_stub"` | Fold the newest exit month into the previous month when it has fewer than `min_sessions` TAIFEX trading dates in the price bars. A night session (15:00–05:00) belongs to the next trading day, so one night is one date and Friday night maps to Monday. `"max_exit_month"` opts out and keeps the newest exit month. |
| `evolution.month_gate.min_sessions` | `12` | Session-count threshold for the stub fold. |
| `evolution.month_gate.slack.mode` | `"scaled"` | Gate 1 allowance is `clamp(k × σ × sqrt(n), floor, cap)` TWD. σ is the baseline's prior-block per-trade sample standard deviation; n is the candidate's prior-block trade count. Gate 3 uses `gate3_k` of that allowance (half by default). `"flat"` opts out: gate 1 uses `flat_twd` and gate 3 requires Δ ≥ 0. |
| **`trading.allow_live_override`** | `false` | **Not an evolution key.** It gates the live **hot-swap mode switch** — whether a `mode_override.json` file (or the UI) may switch the running bot to fully-automatic `auto` mode. Documented here because it governs how much autonomy a live bot has; evolution itself never deploys or switches modes. |

---

### See also
- `CLAUDE.md` → "AI Code Generation" and indicator notes (incl. the `bb_std` recovery formula).
- `src/evolution/*` docstrings for the authoritative per-function contracts.
