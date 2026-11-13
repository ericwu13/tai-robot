# Weekly evolution ("evo") redesign plan

Status: **owner decisions N1–N8 decided 2026-11-09 (TPE): every recommended
default below was adopted** ("go with your recommendations"). Phase 1 ships in
the PR that adds this file and changes no threshold, holdout, cadence, or
promotion rule. The adopted numbers take effect only in the phase that
implements each one (§4), each in its own PR. Every policy choice is in
[Owner decisions](#owner-decisions).

Line references are to `origin/master` at 865b910 unless marked "this PR".

## 1. Problem

The owner's complaint: evo does not produce better strategies week over week.
The record agrees.

- **39 verdicts, 2 PASSes, 2026-09-05 → 2026-11-07.** Steven: 36 verdicts, 1
  PASS. Local: 3 verdicts, 1 PASS.
- **Both PASSes lost money forward.**
  - `DynamicExitPullbackStrategyV2Evo1` (EMA 50→80, Steven, 2026-09-26). The
    A/B promised a move from −1,250 to +18,020 (PF 0.94 → 3.33). Paper, 09-29 →
    11-07: −22,020, against −21,210 for its parent. **Δ −810.** It looked
    +9.8k better after 2 weeks, then went −10.7k worse over the next 4.
  - Local `…V2Evo1` (EMA 60, 2026-10-10). Never deployed. Backtest-forward
    **Δ −10.8k.**
  - `V3Evo1`: Δ −2.0k on paper.
- **FAIL neighbours lost about as much as the incumbent.** Codegen and
  structural FAILs cannot be scored at all, because only `source_hash` was
  stored.
- **Cost:** about 9 Gemini calls and 69k tokens per Steven Saturday.
- **The family effect swamps the knob effect.** Since 09-01, pullback longs
  are −22k to −28k each, while breakout and short bots made +15k to +35k.
  Knob deltas are ±10k.
- **Conclusion:** pass/fail carried no measurable forward information
  (n = 3). Evo searches the wrong space (knobs of a decaying incumbent). It
  judges candidates with a gate that "less bad than a loser" passes. It never
  checks its own record.

A manual exercise on 2026-11-08 (Gemini Pro with the repo's prompts, Claude
playing the owner) produced `DonchianBreakoutStrategy` (30m). It held up on a
holdout, on 23 months of TradingView data and on 11 months of Capital data,
and it is being paper-deployed now. Lessons from it that this plan builds in:

1. **The idea came from a market/regime brief, not from the incumbent's
   trades.**
2. **Regenerating code from a summary silently drifts the spec.** v3 lost
   its 2×ATR stop and its ratchet, and PF fell from 1.65 to 1.29. Only a
   source diff caught it.
3. **The LLM reviewer missed dead code that a 14-second parameter sweep
   caught.** `atr_sma_period` 30/50/100 gave identical trades.
4. **An LLM filter picked from in-sample losing trades flipped the holdout**
   from +2.75k to −6.4k.
5. **The degradation guard ("don't change, PF > 1.3") worked.** Keep it.

## 2. Defects, ranked

| # | Defect | Where | Effect |
|---|---|---|---|
| D1 | **No forward loop.** `evolution_history.jsonl` is write-only. The incubation review is called with `observations=()`. Nothing re-scores a PASS after it is proposed | `run_backtest.py:8033-8042`; `src/evolution/history.py` has no reader besides the retire streak | V2Evo1 is −22k forward and evo would never know. "Is evo working?" cannot be answered from evo's own data |
| D2 | **Retire check skipped on a grid no-pick.** The grid's early return comes before `assess_retirement`, so that week is never an evaluated failure and the #188 "not improvable" streak cannot build | `run_backtest.py:7212-7224` returns, `:8012` never reached | A decaying incumbent whose whole knob neighbourhood fails is never flagged. **Fixed in this PR** |
| D3 | **Search is local to the incumbent.** `DynamicExitPullback*` gets a ≤48-point grid on its own knobs. Other classes get one structural self-mutation per month. No new family can ever be proposed | `src/evolution/grid_search.py:1-32`, `:612` `grid_search_applies`; `src/evolution/structural_lane.py` | Grid neighbours decay together. The 10-24 dry run had 16/16 variants FAIL, with PBO 0.83 |
| D4 | **The LLM sees only the incumbent's own trades since the watermark.** No market/regime context, no other bots, no past attempts | `run_backtest.py:6211-6265` (payload, watermark window), `:6296-6305` (design cut) | Ideas are patches to one bot's recent losers. Lesson 4: filters picked from in-sample losers overfit |
| D5 | **Relative-only gates on paper.** The absolute PF floors are dropped on the relative path: month gates `drop_prior_pf_floor=relative`, multi-fold `require_abs_pf=not relative`, holdout floor dropped | `src/evolution/paper_gate.py:966-980`; `src/evolution/pipeline.py:1113-1121` | "Less bad than a losing incumbent" passes. A net-losing candidate can be promoted |
| D6 | **One 14-day out-of-sample fold.** Bootstrap and effect-size reuse the 9 selection folds. The holdout rolls into next week's design window | `paper_gate.py:71,74` (`DEFAULT_FOLD_DAYS`, `DEFAULT_HOLDOUT_DAYS` = 14); `run_backtest.py:6296` | 11-07 holdout candidate trade counts were 8, 2, 4, 10 and 3. For a 30–60 min strategy, 14 days is noise |
| D7 | **DSR trial count is per run.** N = this Saturday's grid columns. Weeks overlap 8/9 folds and earlier trials are never counted. Structural uses N = attempts this turn (usually 1) | `grid_search.py:484`; `trial_penalty.py:495-510`; `run_backtest.py:7834-7840` | Repeated weekly looks at nearly the same data are not penalised. The multiple-testing correction is too weak |
| D8 | **Validation fill model ≠ live.** `run_ab_backtest` and the evaluator build `BacktestEngine` without `fill_mode`, so they use the engine default `on_close`. The GUI and live paper fill entries at the next open | `src/evolution/pipeline.py:406-407`, `src/evolution/evaluator.py:336`; `src/backtest/engine.py:105`; GUI `run_backtest.py:4961` | Gate numbers are not the numbers the bot would have produced. Not fixed here: it changes every verdict, so it ships on its own (Phase 2) |
| D9 | **Objective is mean fold Δ-TWD against the incumbent.** It is not risk-adjusted, not regime-robust, and blind to portfolio correlation | `grid_search.py:19-32`, `:363-394` | Rewards "loses less in a bad regime" over "earns in a different regime" |
| D10 | **Candidate source and verdict text were not persisted.** History kept `source_hash` only, `reason` was capped at 4,000 chars, and Discord cuts the block at 1,700 | `history.py:20-21,112`; `src/live/discord_notify.py:650-651` | FAILs cannot be re-scored, and the post-mortem had to infer verdicts from `msg_len`. **Fixed in this PR** |
| D11 | **A restart re-runs the Saturday slot.** The ISO-week latch was in memory only | `run_backtest.py:9943-9946`, init `:2568` | Steven 10-31 08:08–08:25: every bot re-ran, and one made a second AI-backed attempt. 11-07 06:14: all re-ran as skips. **Fixed in this PR** |
| D12 | **Low-frequency bots never evolve.** `min_design_trades` = 30 counts live paper trades only | `run_backtest.py:6446-6460` | BB-ShortOnly (best paper bot, +35k since 09-01) is stuck at 14–18/30. A backtest of the same window gives 62 trades |
| D13 | V3 knob `KeyError: 'atr_buffer_mult'` (#164), three Saturdays running | — | **Already fixed on master** by #165 (81b8529, closed 2026-10-22). V3 has not hit it since 10-24. No change in this PR |
| D14 | **Fallback validation bars are 1-min for single-TF strategies.** The normal weekly path fetches API bars at the strategy's own timeframe (`run_backtest.py:8166`, `start_api_fetch(symbol, kt, km, …)`). When the quote session is down or the fetch fails, `_fallback_bars` (`:6963-6980`) passes the bot's 1-min CSVs. `BacktestEngine._prepare_bars` (`src/backtest/engine.py:138-165`) aggregates only for strategies with `htf_intervals`, so a 15m single-TF strategy is validated on 1-min bars there (the comments at `:6968` and `:7540` assume otherwise) | `run_backtest.py:6963-6980`, `:7540` | A fallback Saturday scores the wrong timeframe. The forward ledger already aggregates (`forward_ledger.bars_for_strategy`). Not fixed here: it changes verdicts on fallback days, so it ships with D8 and its replay |

## 3. Target architecture

Evo becomes a weekly **measurement** loop with a monthly **search** step.
Promotion is decided on paired forward evidence, never on one backtest.

```
            ┌──────────── weekly (cheap, no LLM) ─────────────┐
 bars ──▶ forward ledger ──▶ family roster re-score ──▶ report + memory
            │  every past proposal / PASS / incumbent, on bars after its date
            │
            └─▶ feeds: "what failed and why" into the brief
            ┌──────────── monthly search (LLM) ───────────────┐
 market/regime brief ──▶ propose NEW family or patch ──▶ invariance check
   ──▶ mechanical probes (sweep + dead-condition) ──▶ LLM review
   ──▶ gates (absolute floors + long untouched holdout + cumulative DSR)
   ──▶ paired paper-shadow vs parent ──▶ owner promotes on forward Δ
```

### 3.1 Forward ledger (D1). Phase 1 ships the read-only part
- `src/evolution/forward_ledger.py` + `python -m scripts.evo_forward_ledger`
  (this PR). It reads every pass/fail attempt in `evolution_history.jsonl`
  plus the pre-history PASS rows in `data/changelog.json`. It backtests the
  incumbent and the candidate over all bars, so indicators are warm, and
  keeps only trades that **enter after the proposal timestamp**. It reports:
  - per row: incumbent and candidate forward n / P&L and Δ;
  - **PASS-vs-incumbent forward hit rate**, FAIL-vs-incumbent hit rate, and
    their difference ("gate lift"). Gate lift ≤ 0 means the gate carries no
    forward information;
  - incumbent-only rows (grid no-pick, refused plans): whether the
    incumbent that was left in place kept losing.
- Sources come from `{bot_dir}/evolution_sources/` (this PR), extra folders
  (the `strategies/` store), a read-only StrategyPool db, or, for old
  knob-mode rows, the incumbent source plus `directives.knobs`.
- Report only. No gating, no writes into a bot directory, no model call. The
  default fill model is `next_open` (live parity); `--fill-mode on_close`
  reproduces the gate's own numbers.
- Later (Phase 2): run it on the Saturday slot, post the summary, and feed
  the last N failed ideas (one line each: family, change, forward Δ) into
  the brief as memory.

### 3.2 Family-level search driven by a brief (D3, D4, D9)
- **Roster** of families: pullback long, breakout long (Donchian),
  multi-timeframe trend, short (BB). Every week, backtest all families on the
  same folds plus the holdout, and report family P&L side by side.
- **The LLM's job changes.** It stops patching the incumbent from its own
  trades. It proposes a new family, or a change to one, **from a market/regime
  brief**: regime history (`regime_history.csv`), per-family trailing P&L,
  volatility and trend state, and the ledger's failed ideas. This is the
  Session A setup that produced Donchian.
- Allocation across families by trailing family P&L. In the 10-24 dry run it
  beat equal weight at every lookback from 30 to 80 days. It starts as a
  paper shadow only; see the decisions table.

### 3.3 Patch-based codegen + invariance check (lesson 2)
- The LLM returns a **unified diff against the parent source**, never a
  regeneration from `build_summary`.
- Before any scoring, the harness applies the patch and checks that **logic
  the patch did not touch reproduces the parent's trades**. Method: disable
  the patched branch with a feature flag, or run both on a window where the
  new condition never fires; trades must match exactly. A mismatch rejects
  the candidate without spending a backtest budget on it.

### 3.4 Mechanical probes before the LLM review (lesson 3)
- **One-at-a-time sensitivity sweep** on every numeric `kwargs.get` default,
  ±1 catalog step (the grid already has the menus).
- **Dead-condition detector:** a perturbation that leaves the trade list
  byte-identical marks that parameter or condition as dead. Report it, and
  reject a candidate whose *new* code is dead.
- **Spike rejection:** require a fraction of one-step neighbours to also
  beat the parent (decision N3). The grid's plateau rule
  (`grid_search.py:19-25`, "at least 2 good neighbours") extends to every
  candidate, not only grid points.
- The probe results go into the LLM review prompt as facts. The review is
  told what is dead, not asked to find it.

### 3.5 Absolute floors, a longer untouched holdout, cumulative DSR (D5–D7)
- **Absolute floor on the paper path too:** candidate holdout P&L > 0 AND ≥
  max(incumbent holdout P&L, 0) (decision N6). The relative checks stay as
  additional conditions.
- **A longer holdout the LLM never sees** (decision N1). It is fixed per
  proposal and does not roll into the next week's design window until the
  proposal is resolved.
- **DSR trials counted cumulatively per bot** across Saturdays whose
  selection folds overlap (decision N7). Structural and LLM proposals count
  as trials too.
- Fix D8 (validation `fill_mode="next_open"`) in its own PR, with a
  before/after replay of the last 10 Saturdays so the shift is visible.

### 3.6 Paired paper-shadow before promotion (D1)
- A PASS is deployed as a **paper shadow next to its parent** for N2 weeks.
  Promotion uses the paired forward Δ (same bars, same sessions) from the
  ledger, never the backtest A/B.
- Under that rule V2Evo1 would have been held at week 2 (+9.8k) and rejected
  at week 6 (Δ −0.8k).
- The owner promotes. Evo only recommends.

### 3.7 Persist everything needed to re-score (D10). Done in this PR
- `evolution_sources/{sha256}.py` is a content-addressed, write-once store
  for the incumbent and candidate sources.
- The history line gains `candidate_class`, `candidate_source_hash` and
  `verdict_text` (the full block, capped at 64k).

## 4. Phased rollout

| Phase | What | Owner decision needed? | Status |
|---|---|---|---|
| **1** | (a) retire check on the grid no-pick exit; (b) candidate source + full verdict text in history; (c) persisted Saturday slot; (d) #164 verified already fixed; (e) read-only forward ledger + CLI | No (but see N8 on how (a) counts a no-pick week) | **This PR (draft)** |
| 1.5 | Run the ledger once on Steven's bot dirs + `strategies/` + changelog, attach the report to the PR | No (read-only). It needs a Steven session; it was not run from here | Pending |
| 2 | Ledger on the Saturday slot + Discord summary; failed-idea memory in the prompt; validation `fill_mode` parity (D8) with a replay; backtest trades as the design sample for low-frequency bots (D12); confirm Steven's EXE runs the current gates | Decided: D8 ships in its own PR with a before/after replay (owner ack 2026-11-09); N5 | Next |
| 3 | Family roster re-score; brief-driven proposals; patch codegen + invariance check; mechanical probes + dead-condition gate | Decided: N3, N5 | After 2 |
| 4 | Absolute floor on paper; long untouched holdout; cumulative DSR; paired paper-shadow promotion; family allocator shadow | Decided: N1, N2, N4, N6, N7 | After 3, and after the ledger shows the current gate lift |

Exit criterion for the whole plan: **the ledger's PASS-vs-incumbent forward
hit rate beats the FAIL hit rate** over at least 8 scored PASSes, and
promoted strategies' paired forward Δ is positive in aggregate. Until then, evo
recommends only.

## Owner decisions

These are policy, not code. **Decided 2026-11-09 (TPE): the owner adopted
every recommended default in this table, including N8.** A later change to any
row is an owner decision again; agents never retune them. Nothing here is
changed by the Phase 1 PR; each row takes effect in the phase that implements it.

| # | Decision | Today | Adopted default (2026-11-09) | Evidence |
|---|---|---|---|---|
| N1 | **Holdout length** (untouched by the LLM, fixed per proposal) | 14 days, rolls into next week's design window (`paper_gate.py:74`) | **8 weeks (56 days)** | 11-07 holdout candidate trade counts were 8/2/4/10/3. Donchian 30m had 15 holdout trades in 14 days. V2Evo1 flipped sign between week 2 (+9.8k) and week 6 (−0.8k). Cost: minute KLine history only reaches back to 2025-11-28 (~11 months), so 8 weeks leaves ~9 months of design data. 6 weeks is the floor if that is too much |
| N2 | **Paper-shadow weeks** before promotion | none (a PASS goes to the pool; deploy is manual) | **6 weeks**, with a 4-week minimum before any early reject | V2Evo1: +9.8k at week 2, −10.7k over weeks 3–6. Two weeks would have promoted a loser |
| N3 | **Neighbour fraction** for spike rejection (share of ±1-step neighbours that must also beat the parent) | grid only: ≥ 2 good neighbours within 50 % band (`grid_search.py:19-25`) | **≥ 60 %** of one-step neighbours with Δ > 0. Keep the 50 % band. Any dead neighbour (identical trades) is reported, not counted | 60 % matches the existing `DEFAULT_FOLD_MAJORITY` (`paper_gate.py:61`), so no new constant. The 10-24 dry run had 16/16 neighbours FAIL together (decay is shared, so a spike is the common failure). The Donchian sweep found `atr_sma_period` dead in 14 s |
| N4 | **Family allocation lookback and sizing** | none (each bot 1 lot, equal weight) | **Paper shadow only.** Lookback 60 trading days. Weight ∝ max(0, trailing family P&L), equal within a family, rounded to lots 0–2 per family. No live sizing change before 8 shadow weeks | The 10-24 dry run: family momentum beat equal weight at every lookback from 30 to 80 days (+585k…+678k vs +517k) and was positive Sep 12–Oct (+22k…+50k vs −6k). But maxDD was mostly worse, L = 40 was a peak and not a plateau (hence 60, mid-range), and PBO was 0.38 over 16 rules. Per-strategy momentum was worse than equal weight. Calibration showed single-bot P&L kill-switches cannot be calibrated on 20–50 trades |
| N5 | **Cadence** | weekly plan + grid every Saturday; structural monthly | **Weekly:** ledger + family re-score (no LLM). **Monthly** (first Saturday): one brief-driven proposal per family slot. Knob grid only when ≥ 1 new non-overlapping fold exists since the last grid for that bot | Weeks overlap 8/9 folds. Four bots repeated the same ~1,805–1,819-char FAIL verdict weekly. ~9 Gemini calls / 69k tokens per week bought 2 PASSes that both lost forward |
| N6 | **Absolute floor on the paper path** | dropped on relative (`paper_gate.py:966-980`) | Candidate **holdout P&L > 0 AND ≥ max(incumbent holdout P&L, 0)**, on top of the relative checks | "Less bad than a losing incumbent" is how a −22k strategy passes. Real money already requires the absolute floor |
| N7 | **DSR trial window** | this Saturday's grid columns; structural N ≈ 1 | **Cumulative per bot** over all Saturdays whose selection folds overlap the current design window, all lanes counted | Overlapping weekly looks at the same data are repeated trials. The current N understates the multiple-testing penalty |
| N8 | **Does a grid no-pick count as a failed week for the #188 retire streak?** (implemented in this PR) | it recorded no week | **Yes**, relative gate only, and only when the grid actually scored its settings (`GridOutcome.evaluated`). No bars, too few folds, a failed baseline backtest, unreadable knobs, or a grid timeout/crash count as no week | Without it, a class on the knob grid can never reach "not improvable". The output is recommend-only text, and `retire.consecutive_weeks` (4) is unchanged. If the owner prefers the old rule, revert commit "run the retire check when the knob grid selects nothing" |

## 5. Phase 1 details (this PR)

- **(a)** `src/evolution/retire_review.py` holds the retire block, moved out
  of the Tk worker. The validation exit and the grid no-pick exit both call
  it. A relative-gate no-pick from a grid that scored its settings
  (`GridOutcome.evaluated`, set only by `select_grid` with ≥ 1 scored
  point) writes the `EVO_RELATIVE_GATE` changelog marker, stores
  `retire_week.evaluated = true, passed = false`, and appends any RETIRE
  RECOMMENDATION to the chat/Discord message. Data exits (no bars, too few
  folds, baseline backtest failed, knobs unreadable, off the grid) record no
  week. Tests: `tests/test_evolution_grid_no_pick_retire.py`.
- **(b)** `history.store_source` / `load_stored_source`. A line names a
  digest only after its text is stored. Tests:
  `tests/test_evolution_history_sources.py` (including a 7.8k-char block
  whose last line must survive).
- **(c)** `{bot_dir}/evolution_weekly_slot.json` has two states. `started`
  is written at launch. `done` is written with the attempt's history line
  (`_persist_evolution_history` → `notify.finish_weekly_slot_for`), for any
  terminal status including skips, except the retryable infrastructure ends
  `pipeline_error` (e.g. an exhausted Gemini 503), `grid_failed`, and
  `no_chat_client`, which leave `started`. `weekly_slot_taken` is true for
  `done`, or for `started` younger than `SLOT_STARTED_GUARD_S` = 45 min (the
  20-min grid cap + API fetch + model calls with retries + validation;
  observed runs are 1–4 min per bot). So a finished week is never re-run, a
  dead run (crash, hang-kill, failed fetch) is retried after the window, and
  a quick restart never stacks a second AI-backed attempt on a live one. A
  failed write falls back to the in-memory latch, so a disk error never
  silences a run. Tests: `tests/test_evolution_weekly_slot.py`.
- **(d)** #164 was closed by #165 (81b8529). `tests/test_evolution_issue164.py`
  passes on this branch. No change.
- **(e)** Forward ledger, above. Bot-dir bars are 1-min, so
  `bars_for_strategy` aggregates them to each single-TF strategy's primary
  interval first (the engine only does that for `htf_intervals` strategies).
  History rows now carry `ts_utc_offset_min`; older rows use
  `--ts-offset-hours` (default 0 = TPE, correct for Steven, whose Saturday
  rows read 05:05 local for a 05:05-TPE trigger). Tests:
  `tests/test_evolution_forward_ledger.py`. The end-to-end test uses the
  real engine on bars that fall before the proposal and rise after it, so a
  short candidate that "won" in-sample must lose forward.

Ledger usage (run against a copy of the bot dirs, or on Steven):

```bash
python -m scripts.evo_forward_ledger "<install>/data/live/TMF00_<bot>" --source-dir "<install>/strategies" --changelog "<install>/data/changelog.json" --json ledger.json
```

Smoke-tested here against a copy of Steven's v2.19.16 history files. Those
contain 9 pass/fail rows from 10-31 and 11-07, but the copy had no bars, so
every row reported `no_bars`. A real run needs the bot dirs' `bars_1m_*.csv`
and the `strategies/` folder. It has not been run on Steven from this session.
