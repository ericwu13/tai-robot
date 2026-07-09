<!-- Phase 3 design DRAFT (pre-critique). Extracted from workflow wf_da0902d8-5f2 draft agent a9ae03e2665290109. The critique panel + final synthesis may supersede this. -->

# Phase 3 Design: RegimeSwitchingRunner

**Status:** Approved design, ready for implementation
**Branch base:** `feat/regime-trade-attribution` (v2.13.0 + per-trade strategy attribution)
**Directive (2026-07-08):** Regime mode becomes a **deploy-time mode of the bot itself**. Deploying with regime enabled means the deployed bot IS the switching bot (paper `trading_mode` = Shadow #2, real = Live #2). The v2.13.0 advisory sidecar is **retired**, not preserved as a parallel mode. Its classifier / state machine / selector are kept as the switching engine's brain.

---

## 1. Executive summary

### What v2.13.0 shipped (and why it's insufficient)

v2.13.0 "shadow mode" is an **advisory sidecar**: a `RegimeManager` created alongside a normal single-strategy deployment (`run_backtest.py:5384-5406`) that classifies market regime once per NIGHT session when the bot's daily report fires (`run_backtest.py:6395-6409`), logs a recommendation, and backfills P&L from the **deployed bot's** report (`src/regime/manager.py:44-51` → `src/regime/store.py:79-91`).

This is confirmed broken as an evaluation tool (**verdict C1, CONFIRMED**):

- The P&L written into `regime_history.csv` is the deployed single strategy's P&L — the recommended `long_strategy`/`short_strategy` are never instantiated, simulated, or executed anywhere (`grep`: only hits are `src/regime/selector.py:31,46,48,53` and the config dataclass). Shadow mode therefore **cannot measure whether switching is profitable**.
- Sit-out sessions produce **no data at all**: `generate_session_report` returns `None` on a trade-free broker (`src/daily_report/report_generator.py:240-242`), so no report → no backfill → blank rows.
- The trigger is fragile: classification only fires from the trade-gated, poll-fired daily report; stop-time reports never reach the manager (`_regime_manager` is nulled before the trampolined callback runs, `run_backtest.py:7487-7488`).

### What Phase 3 builds

**Bot type 2**: a bot that *is* the switching logic. It classifies regime per NIGHT session, holds a pending recommendation, and swaps between the configured `long_strategy` / `short_strategy` (or sits out) at session boundaries, only when flat. Its P&L comes from its **own** `SimulatedBroker`, so `regime_history.csv` finally measures the right quantity.

- **Shadow #2** = this bot with `trading_mode="paper"` — genuine simulated P&L of the switching logic, standalone, no real bot required.
- **Live #2** = the same engine with `trading_mode="semi_auto"|"auto"` — the existing real-order mirror activates (`run_backtest.py:6361-6363`); nothing else changes (**verdict C3, CONFIRMED**: paper is the base pipeline; real trading is a one-gate bolt-on).

### Retired / refactored / kept

| Component | Fate |
|---|---|
| `RegimeManager.on_daily_report` (daily-report-triggered classification) | **RETIRED** — replaced by a direct `classify_session()` call from the 30s poll |
| `store.backfill_pnl` + P&L-from-deployed-bot's-report | **RETIRED** — replaced by `record_session_result()` fed from the switching bot's own broker |
| Regime hook in `_on_live_daily_report` (`run_backtest.py:6395-6409`) | **RETIRED** (delete the block) |
| `RegimeConfig.dry_run` | **RETIRED** — shadow vs live is the deploy dialog's `trading_mode`, not a regime config flag |
| Advisory sidecar on plain Bot #1 (`RegimeManager` construction at `run_backtest.py:5384-5406` for non-regime deploys) | **RETIRED** — a plain deploy creates no RegimeManager at all |
| `regime_classifier.classify_regime`, `RegimeStateMachine`, `StrategySelector`, `store.save_state/append_history/load_state` | **KEPT** as the brain, with additive changes only |
| `regime_history.csv` v1 rows | **ARCHIVED** — renamed to `regime_history_legacy.csv` on first Phase-3 deploy (old rows measure a different quantity; mixing corrupts evaluation) |
| `_classify_regime_now` manual button | **KEPT but made read-only** (currently it permanently advances the real state machine and appends fake NIGHT rows — `run_backtest.py:5076-5082`) |
| Weekly auto-evolution | **DISABLED when regime mode is active** (baseline = `type(runner.strategy)` at `run_backtest.py:4216` would chase whichever leg is deployed on Saturday) |

---

## 2. Architecture

### Decision: extend LiveRunner with two small primitives + a thin subclass. Not a wrapper, not a new runner.

Justification from the maps:

- **New standalone runner: rejected.** The paper pipeline (ticks → BarBuilder → LiveRunner → SimulatedBroker → CSVs → reports → chart) is already complete and mode-blind (C3). LiveRunner is COM-free; all COM/tick plumbing is GUI-owned (`run_backtest.py:5365-5765`) and cannot be re-hosted cheaply. Rewriting it duplicates ~1000 lines of battle-tested glue (issues #43/#44/#45/#47/#50/#61/#78/#79 all live there).
- **Wrapper (composition): rejected.** `self._live_runner` has ~100 read sites in the GUI accessing LiveRunner attributes directly (broker, get_bars, get_result, suppress_strategy, `_session_key`, …). A wrapper must proxy all of them.
- **Extend: chosen.** The swap itself needs only two mode-agnostic primitives on `LiveRunner` (they mutate state that only LiveRunner owns), plus orchestration that fits naturally in a subclass. GUI changes reduce to one construction branch and three poll hooks.

### New / changed classes

```
src/live/live_runner.py          (CHANGED — 2 new primitives, 1 bug fix)
  LiveRunner.swap_strategy(new_strategy, display_name) -> tuple[bool, str]
  LiveRunner.regime_idle: bool           # sit_out flag, ANDed at the line-818 gate
  LiveRunner.restore_session(...)        # FIX: re-set broker.trade_source after from_dict rebind

src/live/regime_switching_runner.py (NEW, ~250 lines)
  class RegimeSwitchingRunner(LiveRunner)
    - owns: RegimeManager (moved out of the GUI), pending recommendation,
      active_leg ("long"|"short"|"idle"), stable bot identity
    - on_status_poll(now) -> list[str]   # called from the GUI 30s poll; returns status lines
    - _maybe_classify(now)               # NIGHT-end trigger, decoupled from daily report
    - _maybe_apply_pending(now)          # closed-gap swap application
    - _record_session_result(...)        # own-broker P&L into history
    - stop() override                    # classify/record before teardown if in window

src/regime/switch_logic.py (NEW, pure functions — per the "test untestable glue" lesson)
  session_slot(now) -> (date_str, "DAY"|"NIGHT")     # extracted from LiveRunner._session_key (live_runner.py:1178-1190)
  should_classify(now, minutes_to_close, slot, last_assessed_key) -> bool
  in_closed_gap(now) -> bool                          # after 13:45/05:00 close, before next open
  decide_boundary_action(pending_rec, sim_flat, fill_pending, in_replay, ctor_ok) -> SwapDecision
  validate_leg_strategies(cfg, strategies_registry) -> list[str]   # exist + same timeframe

src/regime/manager.py (CHANGED)
  RegimeManager.classify_session(session_date, session_slot) -> Recommendation | None
      # the on_daily_report pipeline minus report parsing; NIGHT-only; ON-DISK dedup
      # via state.last_assessed (currently saved but never checked)
  RegimeManager.record_session_result(date, session, pnl, trades, strategy_active)
  RegimeManager.bars_provider: Callable[[], list[Bar]]   # injectable; default = seed file + bot_dir CSVs
  RegimeManager.on_daily_report  # DELETED

src/regime/store.py (CHANGED)
  record_session_result(...)     # replaces backfill_pnl (delete backfill_pnl)
  append_history(...)            # new columns, see §6
  migrate_legacy_history(path)   # rename v1 file → regime_history_legacy.csv

run_backtest.py (CHANGED — deploy branch, poll hooks, seed fetch, UI; see §5)
```

### Data flow (Shadow #2 and Live #2 are identical except the one gate)

```
COM ticks ──GUI _on_com_tick (6127-6232)──► BarBuilder ──► runner.feed_1m_bar
   ──► _ingest_1m_bar (dedup, bars_1m CSV, aggregate)                 [unchanged]
   ──► _process_aggregated_bar (801-894): DataStore/HTF/_bar_index always;
        strategy+broker skipped when suppress_strategy OR regime_idle  [1-line change]
   ──► on_decision ──► real-order mirror ONLY when trading_mode in (semi_auto, auto)
                       (run_backtest.py:6361-6363)                     [unchanged]

GUI 30s poll (_schedule_status_update, run_backtest.py:5756-5765)
   ──► runner.on_status_poll(now):
         1. _maybe_classify(now)        # NIGHT end, ~2 min before 05:00 close
         2. _record_session_result(...) # at session end, own broker, incl. pnl=0 sit_out
         3. _maybe_apply_pending(now)   # in closed gap, gates pass → swap/idle
```

---

## 3. The swap state machine

### 3.1 Leg states (runner-level; distinct from the existing regime hysteresis machine, which is unchanged)

| State | Meaning | `regime_idle` | `active_leg` |
|---|---|---|---|
| `WARM_IN` | Classifier lacks ≥52 HTF bars and seed fetch failed; no recommendation yet | True | `"idle"` |
| `IDLE` | sit_out / hold-with-no-position applied | True | `"idle"` |
| `LONG_ACTIVE` | `long_strategy` instance live | False | `"long"` |
| `SHORT_ACTIVE` | `short_strategy` instance live | False | `"short"` |
| (orthogonal) `PENDING` | A recommendation exists, not yet applied | — | — |

**Construction:** `RegimeSwitchingRunner` is always constructed with an instance of `long_strategy` as the *timeframe donor* (LiveRunner requires a non-None strategy: `__init__` reads `strategy.kline_type/kline_minute` unconditionally at `live_runner.py:339-341`; **verdict C7**: `strategy=None` crashes at 6+ sites — never support it). It starts with `regime_idle=True` until the first recommendation is applied. Because long/short must share `(kline_type, kline_minute)` (§5 validation), either leg is a valid donor.

### 3.2 Trigger points (exact anchors)

**T1 — Classification (NIGHT end).** New call inside the existing 30s poll, next to `_check_session_end_report` (`run_backtest.py:5762-5763`). Fires when `session_slot(now)[1] == "NIGHT"` and `runner.minutes_until_session_close() <= 2` (same threshold as `_SESSION_END_CLOSE_MINUTES`, `run_backtest.py:5767`). **Unconditional on trades** — this fixes the C7/C8 bootstrap deadlock where a sitting-out bot never classifies because `generate_session_report` returns `None` on an empty broker (`report_generator.py:240-242`). Dedup is **on-disk**: `classify_session` writes `state.last_assessed = f"{date}|NIGHT"` via `store.save_state` and refuses to re-classify the same key (the in-memory `_last_session_key` at `manager.py:33` resets on restart — a same-night restart currently double-classifies).

Result: `machine.step` → `selector.select` → persist state + history row → set `pending_recommendation` + write it into the `next_session` block of `regime_state.json` with `executed=False` (consuming the dead Phase-2 placeholder fields at `store.py:39-43` rather than inventing a parallel mechanism, per C1).

**T2 — Session P&L recording (every session end, DAY and NIGHT).** Same poll tick: when `minutes_until_session_close() <= 2` and the `(date, slot)` result key isn't recorded yet, compute the session's P&L from **own** `broker.trades` (filter `exit_dt` within the session window; the session-end force-close at `run_backtest.py:5823+` guarantees all trades are closed by now) and call `record_session_result(date, slot, pnl, n_trades, active_leg_strategy_name)`. **Zero trades → record `pnl=0`** — sit_out sessions become evaluable (C1 implication 3).

**T3 — Swap application (closed gap).** Every poll tick, if `pending_recommendation` exists and `in_closed_gap(now)` (after 05:00 NIGHT close / after 13:45 DAY close, before next open — the NIGHT→DAY gap is guaranteed ≥3h45m, weekends ~51h47m per C4), attempt to apply. Do **not** use a `mode_override.json`-style file trigger (per-aggregated-bar latency + silent deletion during `_is_reloading`, `live_runner.py:476-518` — C4 implication 5) and do **not** piggyback TickWatchdog `session_resubscribe` (requires an active prior subscription and immediately re-enters the `suppress_strategy=True` replay window, `run_backtest.py:1163-1170`).

There is **no session-start hook in the tick path today** (C4) — applying in the closed gap via the 30s poll deliberately avoids needing one: the market is closed, the bot is flat (T2 force-close already ran), no ticks are flowing, and the new leg is warm when 08:45 arrives. The poll keeps running while closed (`_schedule_status_update` reschedules unconditionally while RUNNING, `run_backtest.py:5765`). Holiday-blindness of `is_market_open` (`live_runner.py:54-95`) is harmless here: applying a swap on a holiday just means the leg idles until ticks eventually arrive.

### 3.3 Apply-time gates (ALL must pass; retry on next poll tick otherwise)

Reuses the exact `_apply_mode_switch` gate pattern (`live_runner.py:536-545`) plus two more:

1. `broker.has_open_position() == False` (`broker.py:480-481`)
2. `mode_switch_veto()` returns falsy — the GUI wires this to `TradingGuard.fill_pending` (`run_backtest.py:5376-5379`); reuse the same callable as the swap veto
3. `self._fill_poller` not active (GUI supplies via the same veto)
4. **Live #2 only:** real account signed position == 0 via `AccountMonitor` (the issue-#79 resume pattern, `run_backtest.py:5449-5476`) — sim-flat does not imply real-flat after SKIP_EXIT or fill-timeout "assume filled" (C4)
5. Not inside the `suppress_strategy`/`_is_reloading` replay window (`run_backtest.py:6155-6162` clears it; a swap during replay runs on stale data — C6)

### 3.4 Applying each action

| `Recommendation.action` | Effect |
|---|---|
| `deploy_long` | `swap_strategy(STRATEGIES[cfg.long_strategy](), cfg.long_strategy)`; `regime_idle=False`; `active_leg="long"` |
| `deploy_short` | same with `short_strategy`; `active_leg="short"` |
| `deploy_short_half` | **treated as `deploy_short`** — order qty is hard-capped at `nQty=1` (`run_backtest.py:6775-6795`); log the intended `qty_scale=0.5` in history but do not attempt fractional sizing. Documented limitation. |
| `sit_out` | 1) position already flat (session-end force-close); if somehow open, `broker.force_close(...)` at freshest price; 2) **clear `broker._pending_entries/_pending_exits/_pending_market_closes`** — `on_bar_close` fills leftover pending entries even when `on_bar` never runs (`broker.py:320-349` via `live_runner.py:888`, C7); 3) `regime_idle=True`; keep the outgoing strategy object in place as the logging-identity donor (C7 hybrid: flag + real object; `strategy.name` is read at `live_runner.py:923/965/1046`) |
| `hold` | no-op (keep current leg/idle) |

After any apply: mark `next_session.executed=True, executed_at=now` in `regime_state.json`, `_auto_save_session()`, emit `on_status`, Discord notify (now phrased "switched", not "manual switch required" — `manager.py:110` text changes).

### 3.5 `swap_strategy()` — the LiveRunner primitive

```python
def swap_strategy(self, new_strategy, display_name: str) -> tuple[bool, str]:
```

Refuses (returns `(False, reason)`) unless gates 1–3/5 of §3.3 pass, plus:
- `(new_strategy.kline_type, new_strategy.kline_minute) == ` current `(kline_type, kline_minute)` — `target_interval`, the primary aggregator, and HTF registrations are frozen at `__init__` (`live_runner.py:339-362`); cross-timeframe swap-in-place is FORBIDDEN (C6)
- `new_strategy.required_bars() <= len(self.data_store)` — otherwise `on_bar` silently never fires (`live_runner.py:848-849`)

On success, atomically (single method, no awaits):
1. `self.strategy = new_strategy`; `self.strategy_display_name = display_name`
2. `self.broker.strategy_label = display_name` (per-trade attribution then flows through `entry_strategy` snapshot at entry fill, `broker.py:336`, into `Trade.strategy` at close, `broker.py:461` — reuse, don't reinvent)
3. Clear the three broker pending lists (stale orders from the dying strategy must not fill under the new label)
4. Ensure every interval in `new_strategy.htf_intervals` is registered; if any is missing, register + replay `self._aggregated_bars` through a fresh HTF aggregator (the `reload_1m_bars` rebuild pattern at `live_runner.py:1353-1363`) — an unregistered interval makes `_htf_warmup_satisfied` gate `on_bar` **forever with no error** (`data_store.py:107-109`, C6)
5. `self._auto_save_session()`

Never touched: `_bar_index`, `_seen_1m_dts`, `_1m_bars`, `_aggregated_bars`, `data_store`, the aggregator, the guard, the poller (full table in §8).

**Indicator warmup is free** (C6): strategies recompute indicators from `data_store` on every `on_bar` via pure functions (`h4_bollinger_long.py:46-47`, `mtf_macd_bb.py:60-76`). Same timeframe ⇒ carrying `data_store` IS the warmup. No seeding code exists or is needed.

### 3.6 Boundary policy decisions (resolved)

- **Swap boundary:** recommendations are computed once per NIGHT (~04:58) and applied in the following closed gap; the applied leg **carries through the DAY→NIGHT boundary** (no fresh classification exists at 13:45; the 75-minute gap is a no-op unless an unapplied pending recommendation is still retrying).
- **Weekend staleness:** a Saturday ~04:58 recommendation applies as-is during the weekend gap and takes effect Monday 08:45. No pre-open re-classification (no new data exists to classify).
- **Mid-position at boundary:** moot by design — Phase 3 **keeps** the existing session-end force-close (`_check_session_end_close`, always flat at boundaries). If real-side `fill_pending` lingers, the swap simply retries each poll tick; the recommendation never expires until superseded by the next classification.
- **Cold start (`WARM_IN`):** see §9.3 — deploy-time HTF seed fetch makes this rare; if seeding fails, the bot idles (builds bars, logs CSVs, classifies as soon as ≥52 HTF bars exist).

---

## 4. Broker parameterization: shadow vs live

**Verdict C3 (CONFIRMED): build no broker abstraction.** The plan's "dry_run selects the broker" is reframed as "**deploy-time `trading_mode` selects whether the existing real-order mirror is active**":

- `SimulatedBroker` is the fill engine in ALL modes, including real ones (in live, `on_close` fills are "a synchronous placeholder"; the real fill overwrites the price ~50ms later via `try_set_real_entry_price` — `broker.py:167-181`). Do **not** switch `fill_mode` off `on_close`.
- The ONLY paper/real difference is one gate: `run_backtest.py:6362` forwards ENTRY_FILL/TRADE_CLOSE/FORCE_CLOSE to `_handle_semi_auto_order` iff `_trading_mode in ("semi_auto", "auto")`. Shadow #2 = that call skipped. Live #2 = that call active, with TradingGuard verdicts, FillPoller target-state confirmation, `try_set_real_entry_price/exit_price` — all unchanged.
- `broker.trade_source` tags trades `"paper"` vs `"real"` via `_mode_to_source` (`broker.py:14-20`); `Trade.source` degrades `"real"→"paper"` when no real fill confirmed (`broker.py:439-441`). This split already feeds `paper_summary`/`real_summary` in reports.

What shadow P&L means (label it in UI/docs, per C3): **gross** (no fees/commission/tax anywhere in `SimulatedBroker`), entries and TP fill with **zero slippage** at bar close / limit price; SL tick-exits fill at actual tick price (`live_runner.py:710-719`) so stop slippage is partially realistic. No cost model is added in Phase 3.

Loss limits: Shadow #2 does **not** enforce the daily loss limit (the guard's `update_pnl` is fed from the REAL account and gated to semi_auto/auto, `run_backtest.py:7208-7223`); Live #2 inherits the existing enforcement unchanged.

`RegimeConfig.dry_run` is deleted; `Recommendation.dry_run` is repurposed to mirror `runner.trading_mode == "paper"` at notify time (Discord phrasing only).

---

## 5. Config schema + UI changes

### 5.1 `settings.yaml` regime block (target schema)

```yaml
regime:
  enabled: false            # deploy-time: enabled → deploying creates the switching bot
  long_strategy: ""         # STRATEGIES display name — MUST exist, MUST share timeframe with short
  short_strategy: ""        # STRATEGIES display name
  range_bias_action: sit_out   # sit_out | short_half (short_half executes as short, qty capped 1)
  adx_enter: 25.0
  adx_exit: 20.0
  confirm_sessions: 2
  vol_spike_ratio: 1.5
  max_flips: 3              # NEW in dialog/yaml round-trip (previously yaml-only)
  flip_window: 10           # NEW in round-trip
  pause_sessions: 5         # NEW in round-trip
  classify_interval: 3600   # NEW in round-trip
  # REMOVED: dry_run (shadow vs live = trading_mode at deploy)
  # manual_override remains state-file-resident (RegimeState), not config
```

Changes:
- Extend `_regime_yaml_block` / `_regime_block_to_flat_settings` (`run_backtest.py:442-475`) from 9 to 13 keys; drop `dry_run`.
- `_validate_regime_values` (`run_backtest.py:411-440`) gains two hard checks (currently it only checks non-empty): **(a)** `long_strategy` and `short_strategy` each resolve in `STRATEGIES` (deleting/renaming an AI strategy currently leaves regime config dangling); **(b)** `STRATEGIES[long].kline_type/kline_minute == STRATEGIES[short].…` — read as class attrs, no instantiation needed (the pattern at `run_backtest.py:2731-2735`). Same validation re-runs at deploy time (registry may have changed since save).
- `RegimeConfig.manual_override` field (dead code — never read; the live override is `RegimeState.manual_override`, `selector.py:28-32`) is deleted.

### 5.2 Deploy flow — three states

`_deploy_live` (`run_backtest.py:5211-5523`) branches on `regime.enabled` **after** the bot-session dialog returns `(bot_name, resume, trading_mode, loss_limit)`:

| State | Construction | Strategy dropdown |
|---|---|---|
| **Plain #1** (regime disabled) | `LiveRunner(strategy_cls(), ...)` — today's path, unchanged; **no RegimeManager is created** (delete `run_backtest.py:5384-5406` from this path) | drives the deployed strategy |
| **Shadow #2** (regime enabled + `trading_mode="paper"`) | `RegimeSwitchingRunner(STRATEGIES[cfg.long_strategy](), ..., regime_cfg)` | **disabled**, shows "Regime 切換 Switching" |
| **Live #2** (regime enabled + semi_auto/auto) | same class; the deploy confirmation dialog states real orders will follow regime switches | disabled |

Validation failure (missing/mismatched leg strategies) aborts deploy with a messagebox before any bot_dir side effects. Warmup, tick subscription, guard reconfiguration, resume dialogs: unchanged (the runner is deployed with the long leg as timeframe donor, so warmup depth/COM fetch work as today via `get_warmup_params`, `live_runner.py:602-626`).

**Additional deploy step for regime mode (cold-start seed, per C8):** after strategy warmup completes and before tick subscription, issue a second `RequestKLineAMByDate` at `kline_type=0, minute=classify_interval//60` (~30 days back), parse via `parse_kline_strings`, and write the HTF bars to `bot_dir/regime_seed_htf.csv`. Do **not** write them into `bars_1m_*.csv` — those have documented "what the bot saw live" semantics and interact with `reload_1m_bars`/`_seen_1m_dts` dedup. The manager's `bars_provider` merges seed + CSV-aggregated bars, dedup by `dt`, live-CSV wins. Fetch failure is non-fatal → `WARM_IN` idle (~2.7 trading days at 1h to reach 52 bars: TAIFEX yields ~19 hourly bars/day).

**Evolution:** `_check_weekly_evolution` (`run_backtest.py:5914-5935`) and `_run_evolution_check_after_report` early-return when `isinstance(self._live_runner, RegimeSwitchingRunner)`.

**Stop:** `_stop_live` unchanged except the regime manager is now owned by the runner (delete `self._regime_manager` GUI slot, `run_backtest.py:1259/5384/7488`); `RegimeSwitchingRunner.stop()` records the in-progress session result before `super().stop()` (the daily-report trampoline running after teardown is no longer a regime problem because classification/recording no longer ride on it — C4 finding).

### 5.3 Regime dialog + status/history panel

- Dialog (`_show_regime_dialog`, `run_backtest.py:4798-4933`): remove the dry-run checkbox; add the 4 new tunables; long/short comboboxes gain live validation (existence + timeframe match shown inline); manual override save no longer **silently drops** when no bot is deployed (`run_backtest.py:5162` gate) — instead show a warning "no switching bot deployed; override not stored".
- `_classify_regime_now` (`run_backtest.py:4997-5107`): becomes a **preview** — runs classify + a *copy* of the state machine step and displays the would-be recommendation; deletes the `save_state`/`append_history` calls at `5076-5082` (manual clicks currently pollute history and advance hysteresis for real — C2/C7 hazard).
- Status panel (`_populate_regime_status` / `_update_regime_status`): shows **mode** (Shadow/Live/off), effective regime, **active leg**, pending recommendation + `executed` flag, next boundary time, and — **the C1 fix** — switching-bot P&L: total from `runner.broker` (`_summary()` `paper_pnl`/`real_pnl`, `live_runner.py:1138-1154`) and **per-leg P&L** grouped by `Trade.strategy` (the attribution field already stamped per trade). History Treeview reads the v2 columns (§6), whose `pnl` is now the switching bot's own session P&L.
- Live chart title uses the leg display name; on swap, the GUI closes/reopens `LiveChart` (pattern at `run_backtest.py:3378-3380`) via a new `on_strategy_swapped` runner event — data continuity is free since the chart is re-seeded from `runner.get_bars()`.

### 5.4 Daily-report filename collision (small, in scope per C5)

`generate_daily_report` writes `data/daily-reports/{date}.json` keyed only by date (`report_generator.py:19, 172-175`); two bots on the same date overwrite each other. Change to `{date}_{bot_name}.json` (thread `bot_name` through; keep a compatibility read of the old name in any consumer).

---

## 6. Persistence

### 6.1 `session.json` (via `_auto_save_session`, `live_runner.py:1158-1176`)

Additive keys written by `RegimeSwitchingRunner`:

```json
{
  "regime_mode": true,
  "active_leg": "long|short|idle",
  "strategy": "<active leg display name or donor name when idle>"
}
```

Resume: the bot-session dialog detects `regime_mode: true` and resumes as a `RegimeSwitchingRunner` **regardless of the dropdown** (the strategy-mismatch resume dialog at `run_backtest.py:5246-5294` is skipped for regime bots). Restored open positions keep their persisted `entry_strategy` (already handled, `live_runner.py:1232-1235`).

**Bug fix folded in (C6):** `restore_session` rebinds the broker via `from_dict` but neither `from_dict` nor `restore_session` restores `trade_source`, and the GUI sets it at `run_backtest.py:5372` *before* restore at `5438` — resumed real-mode sessions record `Trade.source=""`. Fix: `restore_session` re-sets `self.broker.trade_source = _mode_to_source(self.trading_mode)` after the rebind. Applies to Plain #1 too.

### 6.2 `regime_state.json`

Unchanged shape; two semantics change:

- `next_session.executed / executed_at` — currently written and **never read** (`store.py:39-43`): now the swap lifecycle. Written `False` at classification; flipped `True` at apply. On restart, if `next_session.executed == False` and `(next_session.date, session)` is still the current/next slot, the pending recommendation is **re-armed** (survives crash mid-pending-swap).
- `last_assessed` — currently saved and never checked: now the **on-disk classification dedup key** (`"{date}|{slot}"`), fixing the restart double-classify (in-memory `_last_session_key` resets, `manager.py:33`).

### 6.3 `regime_history.csv` — v2

Migration: on first Phase-3 manager init, if the existing file's header lacks the v2 columns, rename it to `regime_history_legacy.csv` and start fresh (old `pnl` measured the deployed Bot #1's strategy — a different quantity; mixing corrupts evaluation. Resolved per directive: full replacement, no compatibility mode).

v2 columns = the 17 v1 columns **plus**:

| New column | Meaning |
|---|---|
| `strategy_active` | what ACTUALLY ran this session (may differ from `strategy_deployed`, which keeps meaning "recommended", when a swap was vetoed/retrying) |
| `applied` | `true/false` — recommendation applied at boundary |
| `applied_at` | timestamp of apply (blank if not) |
| `trading_mode` | `paper` / `semi_auto` / `auto` at record time (shadow vs live provenance) |

**What `pnl` now means:** the switching bot's **own** simulated (paper) session P&L — sum of own `broker.trades` closed within the `(date, session)` window, gross, TAIFEX points × `point_value`. Recorded by `record_session_result()` at session end (T2), keyed by exact `(date, session)` match against the classification row — no more date-string + blank-pnl heuristics (`backfill_pnl`'s brittleness at `store.py:85-89` is deleted with it). Sit-out sessions record `pnl=0, trades=0` (counterfactual "what the leg would have made" is explicitly **out of scope** for Phase 3). NIGHT rows are written at classification (T1); their P&L was already recorded by that same tick's T2 (T2 runs before T1 in the poll), so the classification row carries the just-ended session's result directly — DAY sessions get standalone result rows with `decision=""`.

---

## 7. Concurrency stance (per C5, CONFIRMED)

**Shadow #2 / Live #2 / Plain #1 are mutually exclusive within one app instance.** The three deploy states are alternatives for the single `self._live_runner` slot (set/nulled at exactly `run_backtest.py:5365` / `7487`). This is a hard scope boundary, not a TODO:

- The tick pipeline is single-subscription with no symbol routing (`run_backtest.py:659-671`, one `_live_bar_builder`, one `_live_tick_symbol`); the 30s poll, reconnect replay flags, Discord global, debug-log handle, TradingGuard/FillPoller pair are all singletons. Concurrent dual-runner = a broad refactor of the 7,500-line monolith where bugs concentrate (project memory).
- A user who insists on watching real Bot #1 **and** Shadow #2 simultaneously runs **two app processes**: the app layer permits it (no process mutex; distinct `bot_name` avoids the `.lock`), but this is *supported-with-caveats*, not designed-for: (i) Capital API dual-login behavior for the same account is **not determinable from the repo** — needs an empirical test or a second account; (ii) the daily-report filename fix (§5.4) is prerequisite; (iii) both processes share the hard-coded SKCOM log dir `CapitalLog_Backtest` (`run_backtest.py:2645-2647`) — acceptable log interleaving, or make the path per-process later.

Shadow #2 still requires a quote login (paper deploy is gated on `_quote_connected or _tv_available`, `run_backtest.py:5211-5216`) — "standalone" means *no real Bot #1 required*, not *offline*.

---

## 8. Swap-state checklist (per C6 — the implementation table for `swap_strategy()` and `sit_out`)

| State | Verdict | Detail + anchors |
|---|---|---|
| Strategy instance | **RESET** (fresh zero-arg `STRATEGIES[name]()`) | Instantiation is uniformly zero-arg everywhere (`run_backtest.py:5362`, `3086`); no kwargs machinery needed. Per-instance state (`_sl_price` h4_bollinger_long.py:40, `_prev_hist` mtf_macd_bb.py:50) starts cold — harmless only because swap is flat-gated. Must also update `self.strategy`, `strategy_display_name` (read at live_runner.py:848/854/923/965/1031/1046/1162/1211), `broker.strategy_label` (live_runner.py:393; precedent 1235), and the GUI display identity. |
| `target_interval` / primary BarAggregator | **CARRY** — identical `(kline_type, kline_minute)` only; cross-TF swap-in-place **FORBIDDEN** | Frozen at `__init__` (live_runner.py:339-346); aggregator holds the partial bar + issue-#78 guard (bar_aggregator.py:30-41). Enforced by §5.1 validation. |
| HTF aggregators / `_htf_required` | **CARRY if subset; REBUILD missing** | Unregistered interval ⇒ `on_bar` gated forever, silently (data_store.py:107-109). Rebuild pattern: live_runner.py:1353-1363 (register + replay `_aggregated_bars`). |
| `_seen_1m_dts` / `_1m_bars` | **CARRY, mandatory** | Clearing double-ingests bars on the next reconnect replay (run_backtest.py:1162-1177) and double-writes CSVs (dedup at live_runner.py:767-770). |
| `data_store` | **CARRY** (same TF) | It IS the indicator warmup. Precondition: new `required_bars() <= len(data_store)` (gate live_runner.py:848). |
| `_bar_index` | **CARRY — never reset** | Pre-incremented (live_runner.py:814-815); `check_tick_exit` uses `_bar_index-1` (722-728); TRADE_CLOSE/ENTRY_FILL compare against it (898, 911); persisted/restored (1171, 1236). Reset breaks close/fill detection and the issue-#45 race guards. |
| Broker instance | **CARRY same object** | Rebind only via existing `restore_session` (holders must re-read `runner.broker`, live_runner.py:1231). Trades/equity/`_cumulative_pnl` carry — attribution safe via `entry_strategy` snapshot (broker.py:336, 461). `strategy_label` → RESET to new name. `trade_source` carries (plus §6.1 restore fix). |
| Broker pending lists | **RESET (clear all three)** | `on_bar_close` fills leftover pending entries even when `on_bar` never ran (broker.py:320-349 via live_runner.py:888) and would stamp them with the NEW label. |
| Broker position / `real_entry_price` / `entry_bar_index` | **FORBIDDEN-while-open** | Gate on `position_size == 0`. When flat these are already zeroed by `_close_position` (broker.py:467-476) and `try_set_real_entry_price` rejects while flat (broker.py:523-524) — no reconciliation needed. |
| TradingGuard | **CARRY same object; NEVER rebind; never `reset()` in a swap** | FillPoller captured the reference at construction (run_backtest.py:1303; issue #43). `reset()` wipes `real_entry_confirmed` + `_deferred_close` (trading_guard.py:35-44; issue #79). A flat, non-pending guard is quiescent across a swap. |
| `fill_pending` / FillPoller | **FORBIDDEN while pending/active** | Deferred close replays in the CURRENT context (trading_guard.py:114-128; run_backtest.py:7048-7060); `_entry_bar_index` snapshot is trade-scoped (fill_poller.py:62-66). The existing `mode_switch_veto` (run_backtest.py:5376-5379) encodes this — reuse it as the swap veto. |
| `suppress_strategy` / `_is_reloading` window | **FORBIDDEN during** | Precedent: mode overrides are deleted, not queued, during the window (live_runner.py:476-518); cleared only at history→live transition (run_backtest.py:6159-6162). Swap retries on the next poll. Do NOT reuse `suppress_strategy` as the sit_out flag — reconnects clear it unconditionally, resurrecting the strategy mid-idle (C7); `regime_idle` is a separate flag ANDed at the live_runner.py:818 gate. |
| Chart | **RESET-or-relabel** | Title + BB params baked at creation (run_backtest.py:3391-3400); close+reopen on swap (pattern 3378-3380); data re-seeds from the carried runner. |
| CsvLogger | **CARRY — no action** | Bar files rotate by `bar.dt` date; `decisions.csv` rows carry the per-call strategy name (csv_logger.py:53-72), correct the moment `self.strategy`/`strategy_display_name` update. |
| session.json | **CARRY file; save immediately after swap** | Update `strategy_display_name` first (resume matching reads it, live_runner.py:1162; run_backtest.py:5278-5294); then `_auto_save_session()` so a crash resumes with the correct leg. |
| Daily-report accumulators (`_last_report_session`) | **CARRY** | Reset re-fires a duplicate report within the same slot (live_runner.py:1200-1204). Known residual distortion: the session report header labels ALL trades with the CURRENT display name/params (live_runner.py:1207-1216) while per-trade `Trade.strategy` stays correct — acceptable because swaps happen at session boundaries (never intra-session), so a session's trades all belong to one leg; the regime history (per-leg via `Trade.strategy`) is the authoritative evaluation source, not the report header. |
| Evolution baseline | **DISABLED in regime mode** | `type(runner.strategy)` as baseline (run_backtest.py:4216) would chase whichever leg is deployed on Saturday. |

---

## 9. Failure modes & mitigations

1. **Strategy constructor throws at apply time** (AI strategy file changed/deleted since config save): catch in `_maybe_apply_pending`; keep the current leg (or stay idle), mark the history row `applied=false` with the error in a status log, Discord alert, retry **not** attempted for ctor errors (deterministic failure) — next NIGHT classification supersedes. Deploy-time validation (§5.1) makes this rare.
2. **Swap gates never clear during the gap** (e.g., stuck `fill_pending` from a timed-out real order): the pending recommendation retries every 30s poll tick indefinitely; if still unapplied when the next classification runs, the new recommendation **replaces** it (write new `next_session`, old row keeps `applied=false`). No queue — exactly one pending recommendation exists at a time.
3. **Classifier insufficient bars** (`<52` HTF, `manager.py:97`): `classify_session` returns `None`; runner stays in `WARM_IN`/current state; a history row is NOT written (avoid noise); status panel shows "warm-in: N/52 bars". Seed fetch at deploy (§5.2) is the primary mitigation; the fetch failing is non-fatal.
4. **Restart mid-pending-swap:** `next_session.executed=false` persists in `regime_state.json`; on resume, `RegimeSwitchingRunner` re-arms the pending recommendation iff its `(date, session)` target hasn't passed (else drop, log). On-disk `last_assessed` prevents the restarted process from re-classifying the same NIGHT (§6.2).
5. **Restart / resume interplay with the guard (issue #79):** unchanged reconciliation — restored open position confirms the guard only when the REAL account also shows a position (`run_backtest.py:5441-5476`); the swap veto then naturally holds any pending swap until reconciliation resolves. The `trade_source` restore bug is fixed (§6.1). `guard.reset()` at deploy/stop stays; swaps never call it.
6. **Reconnect during idle/pending:** reconnect sets `suppress_strategy=True` and re-enters replay (`run_backtest.py:1163-1170`); `regime_idle` is untouched by the history→live transition (it only clears `suppress_strategy`, `run_backtest.py:6159`), so sit_out survives reconnects — the specific failure C7 flagged for flag-reuse.
7. **Holiday false session-start:** `is_market_open` is holiday-blind (`live_runner.py:54-95`); a swap applied before a holiday just idles until ticks arrive (watchdog handles silence). Never treat a tickless post-swap session as an error.
8. **Zero-trade sessions:** no longer a starvation vector — classification (T1) and result recording (T2) are unconditional on trades. The trade-gated daily report (`report_generator.py:240-242`) remains as-is for its other consumers; regime no longer depends on it.
9. **Stop→redeploy within one session slot:** duplicate daily reports remain possible (dedupe key is per-runner-instance, `live_runner.py:412`) — pre-existing, out of scope; but duplicate **classification** is now impossible (on-disk `last_assessed`).

---

## 10. Test plan

Style: pytest under `tests/`, pure-Python, no GUI/Tk, no COM — matching `tests/test_regime_selector.py` / `test_regime_state_machine.py` / `test_regime_ui_settings.py`. Per the project lesson (*feedback_test_untestable_glue*), all branching decision logic lives in `src/regime/switch_logic.py` pure functions and runner methods that take injected inputs; the GUI poll hook stays a 5-line pass-through.

**`tests/test_switch_logic.py`** (pure functions)
- `session_slot`: boundary minutes 08:44/08:45/13:45/13:46/05:00 (must match `_session_key` semantics, live_runner.py:1178-1190 — extract, then have `_session_key` delegate so they can't diverge)
- `should_classify`: NIGHT-only; fires once per on-disk key; restart simulation (fresh object, same key) → no re-fire; DAY session → never
- `in_closed_gap`: 13:46–14:59, 05:01–08:44, weekend Sat 05:01→Mon 08:44, mid-session → False
- `decide_boundary_action`: full truth table over (pending × flat × fill_pending × in_replay) — designed to fail against the known-bad behavior (e.g., a case that would swap during replay)
- `validate_leg_strategies`: missing name; timeframe mismatch (build two fake classes with differing `kline_minute`); pass case

**`tests/test_live_runner_swap.py`** (runner primitive; LiveRunner is COM-free and already constructible with fakes)
- Swap succeeds when flat: strategy/label/display-name updated, pending lists cleared, `_bar_index`/`_seen_1m_dts`/`data_store` untouched, session auto-saved
- Swap refused: open position; veto returns reason; `required_bars() > len(data_store)`; timeframe mismatch
- HTF rebuild: swap to a strategy declaring an unregistered `htf_interval` → interval registered and warmed by replaying `_aggregated_bars`; then `on_bar` actually fires (fails against known-bad silent forever-gate)
- Stale pending entry from old strategy does NOT fill under new label after swap (fails against known-bad: skip step 3 and `on_bar_close` fills it, broker.py:320-349)
- Attribution: leg A trade then swap then leg B trade → `Trade.strategy` differs per trade; both under one broker
- `regime_idle=True`: feed aggregated bars → `data_store`/`_bar_index` grow, zero broker calls, CSV log still invoked; reconnect-style `suppress_strategy` toggle does NOT clear `regime_idle`

**`tests/test_regime_manager_v2.py`**
- `classify_session`: on-disk dedup across two manager instances (restart); NIGHT-only; `next_session` written with `executed=false`; injectable `bars_provider` with synthetic 52+ bars; `<52` → None, no history row
- `record_session_result`: exact `(date, session)` keying; pnl=0 sit_out row; v2 columns; result recorded even with zero trades
- History migration: v1-header file renamed to `regime_history_legacy.csv`, fresh v2 file created; v2 file left in place
- `on_daily_report` no longer exists / `backfill_pnl` deleted (import-level assertion)

**`tests/test_regime_switching_runner.py`**
- Cold start: no seed, <52 bars → `WARM_IN`, no crash, no recommendation
- Full cycle with faked clock: classify at NIGHT end → pending persisted → apply in gap → `executed=true` + leg active; second poll tick is a no-op (idempotent)
- Restart mid-pending: rebuild runner from persisted state → pending re-armed; expired pending dropped
- `restore_session` re-sets `broker.trade_source` (fails against current code — designed per *design-tests-to-fail-against-known-bad*)
- `deploy_short_half` executes as short, history logs `qty_scale`

**`tests/test_regime_ui_settings.py` (extend)**
- 13-key yaml round-trip; `dry_run` absent; validation rejects nonexistent/mismatched-TF legs

Run: `pytest tests/ -x`. Update the CLAUDE.md test count when done.

---

## 11. Phased implementation order (reviewable commits)

| # | Commit scope | Size | Contents |
|---|---|---|---|
| 1 | `refactor(regime): extract pure switch logic + fix trade_source restore` | S (~200 LOC + tests) | `src/regime/switch_logic.py` (`session_slot` extracted from `_session_key`, `in_closed_gap`, `should_classify`, `decide_boundary_action`, `validate_leg_strategies`); `restore_session` trade_source fix. No behavior change to shipped paths except the bug fix. |
| 2 | `feat(live): LiveRunner.swap_strategy + regime_idle primitive` | M (~150 LOC + tests) | Gates, atomic swap steps 1–5, pending-list clear, HTF rebuild, idle flag at the 818 gate. Inert until a caller exists. |
| 3 | `refactor(regime): manager v2 — classify_session, record_session_result, retire on_daily_report/backfill_pnl` | M (~250 LOC + tests) | On-disk dedup via `last_assessed`; `next_session.executed` lifecycle; injectable `bars_provider`; history v2 columns + legacy migration; delete `backfill_pnl`; delete the regime hook block at `run_backtest.py:6395-6409`. |
| 4 | `feat(live): RegimeSwitchingRunner` | M (~250 LOC + tests) | Subclass: owns manager, pending recommendation, leg states, `on_status_poll`, apply/sit_out/hold handling, stop() override, session.json `regime_mode` keys, resume re-arm. |
| 5 | `feat(deploy): regime deploy branch + poll hooks + seed fetch` | L (GUI glue, ~300 LOC) | `_deploy_live` branch (three states), deploy-time validation, HTF seed fetch via existing `RequestKLineAMByDate` machinery, `on_status_poll` call in `_schedule_status_update`, swap veto wiring, evolution disable, chart relabel on swap event, resume detection of `regime_mode`. |
| 6 | `feat(ui): regime dialog v2 + status/history panel + report filename` | M (~250 LOC) | 13-key dialog, drop dry_run, inline leg validation, read-only Classify-Now preview, override-drop warning, per-leg P&L status, v2 history Treeview, `{date}_{bot_name}.json` daily reports. |
| 7 | `docs+chore: CLAUDE.md, settings.example.yaml, memory notes, test count` | S | Document the three deploy states, `regime_history.csv` v2 semantics ("pnl = switching bot's own gross paper P&L"), and the shadow-P&L fidelity caveats. |

Dependency order is strict 1→2→3→4→5; 6 and 7 can follow 5 in either order. Commits 1–4 are fully testable headless; commit 5 is the only one touching the untestable GUI glue and deliberately contains no decision logic (all decisions were extracted in commits 1–4).

**Out of scope (explicitly):** fee/slippage cost model; fractional qty for `short_half`; counterfactual sit-out P&L; concurrent dual-runner in one process; Capital API dual-login verification; modularization of `run_backtest.py`.