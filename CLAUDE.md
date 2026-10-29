# tai-robot - Taiwan Futures Trading Bot

## Project Overview
Trading bot for Taiwan futures using Capital API (SKCOM) v2.13.57.
Two API approaches: DLL-based (ctypes via SKDLLPython.py) and COM-based (comtypes for KLine).

## Security Constraints (ALWAYS FOLLOW — non-negotiable)
- Never hardcode channel IDs or role IDs in source files. They belong only in `settings.yaml` (which is gitignored). `settings.example.yaml` has empty placeholder fields.
- Never push channel IDs, role IDs, or any Discord credentials to git.

## Model Preferences
- **Debugging / investigation / root-cause tasks**: use `claude-fable-5`
- **Implementation / code writing / bug fixes**: use `claude-opus-4-8`
- When a task involves both, use Fable to diagnose first, then hand off to Opus to implement.

## Testing
- Run tests with: `python -m pytest tests/ -x -q`
- All tests must pass before merging or shipping a release.

## Release Process
- Bump `version.py`, write bilingual release notes (繁體中文 + English), build the exe via `build_release.py`, create the GitHub release, upload assets, and send the Discord notification.
- The Discord notification reads from the GitHub release body — no local file lookup.

## Build & Test
```bash
pip install pyyaml keyring comtypes holidays
pytest tests/ -x          # run full test suite
python test_connection.py  # DLL-based quote connection GUI
python test_kline.py       # COM-based KLine history GUI
```

## Key Paths
- SDK wrapper: `CapitalAPI_2.13.57/CapitalAPI_2.13.57_PythonExample/SKDLLPythonTester/SKDLLPython.py`
- SDK DLLs: `...SKDLLPythonTester/libs/SKCOM.dll`
- SDK reference examples: `CapitalAPI_2.13.57/CapitalAPI_2.13.57_PythonExample/PythonExample/`
- App source: `src/` (config, gateway, market_data, strategy, execution, risk, logging_, utils)
- Tests: `tests/`
- Config: `settings.yaml` (gitignored, has credentials), `settings.example.yaml` (template)

## Architecture
- EventBus: thread-safe queue pub/sub, DLL callbacks enqueue, main thread dispatches
- ConnectionManager: Login -> Reply(0) -> Quote(1) -> ProxyOrder(4) -> LoadCommodity
- BarBuilder: ticks -> time-aligned OHLCV bars
- Strategy: AbstractStrategy plugin interface with on_bar() returning Signal
- ExecutionEngine: paper/semi_auto/full_auto modes with risk gate
- Indicators: pure functions (sma, ema, rsi, macd, bollinger_bands)

## Capital API - DLL Approach (test_connection.py)
- SK class is fully static, callbacks via OnXxx(handler) pattern
- Connection sequence: Login -> ManageServerConnection(id,0,0) -> (id,0,1) -> (id,0,4)
- 77 exported DLL functions - KLine functions are NOT exported
- authority_flag: 0=prod, 1=test+prod, 2=test only

## Capital API - COM Approach (test_kline.py)
- Required for KLine historical data (RequestKLineAMByDate, OnNotifyKLineData)
- COM registration: `regsvr32 SKCOM.dll` and `regsvr32 CTSecuritiesATL.dll` (run as admin)
- Must use `comtypes` package (not win32com - ProgIDs not registered)
- MUST use `SKCenterLib_LoginSetQuote(uid, pwd, "Y")` instead of plain Login - the "Y" flag enables quote service
- Create COM objects at MODULE level before Tkinter mainloop (not inside methods)
- Register event handlers at module level with `comtypes.client.GetEvents(obj, handler)` - do NOT specify event interface param
- Event handler methods must NOT include `this` parameter - comtypes `without_this` strips it automatically
- OnReplyMessage `[out]` param sConfirmCode: return -1 from handler (comtypes handles as return value, not pointer write)
- Connection takes ~15s: Login(~7s) -> Reply(3001) -> Ready(3003)
- KLine types: 0=minute, 4=daily, 5=weekly, 6=monthly. For N-minute bars, use type=0 with minuteNumber=N
- Trade sessions: 0=Full Session, 1=AM Session
- KLine data format: "MM/DD/YYYY HH:MM, Open, High, Low, Close, Volume"
- Data arrives synchronously within RequestKLineAMByDate call
- **Bar timestamp convention**: COM API returns intraday N-min bars labeled by their CLOSE time (e.g. an AM 60-min bar covering 12:45–13:45 arrives as `13:45`). Everywhere else in this codebase (BarBuilder/BarAggregator/`is_last_bar_of_session`/strategies) `bar.dt` is the bar OPEN time. `parse_kline_strings()` auto-detects close-time labels and shifts them to open-time so downstream code sees a single convention. Do NOT undo this normalization without rewriting every consumer.

## Headless CLI (run_bot_cli.py / tai_backtest.exe)
- `run_bot_cli.py deploy|stop|list|status|strategies|backtest` — same live machinery as the GUI: `HeadlessBotApp(BacktestApp)` on a withdrawn Tk root (`src/live/headless_app.py`), so tick/fill/reconnect/session-end code is byte-identical. Pure helpers (parser, DeployRequest builder, STOP file, bot scan) in `src/live/headless.py`. A headless deploy writes `headless.json` (`pid`, `started_at`) in the bot dir and removes it in a `finally` on the way out; OSError on that write or remove is logged and does not crash the bot. The GUI live loop does not poll STOP and does not write the marker.
- Packaged EXE (no Python on PATH): the same commands are `tai_backtest.exe <command>` from the install folder. `run_backtest.py` routes a known subcommand to `run_bot_cli.main`; no args, `-h`/`--help`, or an unknown first token stays on the GUI (double-click unchanged). `--detach` child argv is `[exe, subcommand, …]` with no `.py` token. `python run_bot_cli.py` stays the source/oreopie form. Data dirs (`data/live`, STOP, lock, `cli_stdout.log`) use `src/paths.py` `project_root()` — the EXE's install dir, the same place the GUI writes. v2.19.2 shipped the source CLI only; the release note that said headless "ships in this tag" did not make the EXE route those commands.
- `multiprocessing.freeze_support()` runs before Tk import. `src.paths.skips_gui_and_com` (one helper, shared with `runtime_hook_comtypes.py`) is true for a spawn child (`argv[1] == --multiprocessing-fork`) and for `--grid-subprocess-smoke`. Those two exit before `route_argv`, Tk, and COM. A CLI `deploy` still installs the comtypes gen cache.
- Dialog seam: `_deploy_live()` = dialog only → `_deploy_live_from(DeployRequest)`; every prompt on that path goes through `self._confirm(title, msg)` / `self._alert(title, msg)` (GUI: messagebox; headless: `HeadlessPolicy.answer(title)`, unknown title = NO). Adding a new prompt to the deploy/backtest path = use the seam and add its title to `src/live/headless.py`, or the CLI will decline it.
- `semi_auto` is refused headless (needs the order-confirm dialog). Stop = `STOP` file in the bot dir or Ctrl+C → normal `_stop_live()`.
- `deploy --detach --wait-ready N` spawns the bot detached (stdout → `{bot_dir}/cli_stdout.log`) and returns 0 on `Tick subscription active` under the child's own lock, the child's exit code if it died, 6 if still starting. The `deploy` skill (`.claude/skills/deploy`) is the agent-facing procedure with its safety gates; it runs from the MAIN tree (worktrees have no settings/SDK/data).

## Bot Dashboard (scripts/dashboard)
- `python -m scripts.dashboard.server [--tailscale] [--base-dir <data/live>] [--port 8765]` — read-only zh-TW web page over every bot dir: fleet table + per-bot candles/trades/decisions/log. Stdlib only. Binds loopback by default; `--tailscale` adds the tailnet IP; clients outside loopback/100.64.0.0/10/fd7a:115c:a1e0::/48 get 403.
- **Never writes to a bot dir.** Liveness reuses `check_bots.classify_heartbeat` + `common.pid_alive` (never `LiveRunner.check_lock`, which deletes stale locks), so page and monitor agree.
- **All reads go through `fileio.open_shared` (FILE_SHARE_DELETE).** A plain `open()` reader on Windows makes `save_session` raise (its `os.replace` fails against ANY open handle, and the remove-then-rename fallback needs share-delete) and blocks `.lock` removal on clean stop. `tests/test_dashboard.py` pins both directions.
- Log lines pass `redact_line` (monitor `redact` + `log_redact.scrub_ids` + Discord snowflakes + TW-ID shapes) before leaving the box. Requests whose `Host` is not localhost/a bound address/the machine name (or its MagicDNS `*.ts.net`) get 403 — DNS-rebinding guard.
- `session.json` is re-read only when its stat changes (`read_session`): every open turns the bot's next `os.replace` into the non-atomic remove+rename fallback. A live PID with no usable heartbeat is NO_HEARTBEAT (orange, needs attention), never "running" — the monitor treats it as UNKNOWN. Times use `bar_dt`/trade strings (TPE); chart times are TPE wall-clock encoded as UTC.
- Candles: `/api/bars?id=&before=YYYYMMDD&days=N` stitches daily `bars_1m_*.csv` files into one series (a night session spans two files); the page prepends older windows as the viewer scrolls left, and its 10 s refresh asks for every file from its newest loaded day (`after=`) so a tab asleep across midnight fills the gap. lightweight-charts fires range events asynchronously (also after the page's own `setData`), so what bounds older-day loading is the view shift after a prepend: it stops once the visible range is filled.
- Regime bots (`session.json` `regime_mode`; a plain deploy can leave an old `regime_state.json` behind): `regime_detail` reads `regime_state.json` + `regime_history.csv`, and its health flags come from `check_regime._check_state` itself (P1 for a corrupt/missing state file), demoted with the monitor's own rule (`_was_running`: no activity since the last completed night opened → P3 `[stopped bot]`). `regime_state.json` / `regime_history.csv` / `session.json` are re-read only when their stat changes: every open makes the bot's next `os.replace` fail, and the regime store writers have no fallback. `next_session.date` is the night the plan was based on, not the day it starts. The chart band places each NIGHT classification at that night's close (open 15:00 + 14 h).
- Controls are opt-in (`--enable-control`; off = read-only) and limited to `stop` (the headless `STOP` file via `src.live.headless.request_stop`) and `paper` (`mode_override.json`, applied by `LiveRunner._check_mode_override` at the next strategy bar). **Nothing in the dashboard may write semi_auto/auto**: a runtime escalation skips the deploy-time real-position check (#204) and loss limit, and `--resume` restores the command-line mode anyway. `control.check` and `control.apply` each refuse unknown actions on their own. `POST /api/control` needs `X-Dashboard-Control: 1` + JSON + same-origin (no OPTIONS handler, so cross-site preflights fail), stop needs the typed bot name, stop also needs the position the dialog warned about (`position_ack`, refused if it changed), a live `headless.json` marker whose pid is still alive (re-checked in `apply` immediately before any write; a GUI bot, a stale pid, or a malformed marker is refused and no STOP / mode_override.json is written) and a non-HUNG bot, and every request (including 400/403 and unknown bots) is logged, formula-neutralised, to `dashboard_control_log.csv` next to the bot root. Stopping a real-money bot that holds a position closes it at market.
- Real-order P&L (`real_pnl.py`): trade `pnl` is SIMULATED; real P&L comes from `real_entry_price`/`real_exit_price` on trades with `source=="real"` (empty source = pre-field trades, judged by their prices; "paper" never counts). A recorded real price is trusted only from #92 on (`REAL_PRICES_TRUSTED_FROM` = 2026-07-26; earlier exits can be the GetFulfillReport day average or a parsed non-price) and only within 2% of the sim price (bigger = data error, not slippage). Both trusted = real; one side never recorded = estimated (sim used); anything else is excluded and counted as unreliable/missing. Gross, before fees and futures transaction tax.
- Votes (regime bots): `snapshot.vote_detail` reuses `src.news.vote_status.collect_vote_status` (the desktop regime tab) with its `read_json` seam set to the share-delete reader, so the dashboard never blocks the bot deleting a consumed vote file. Vote paths = only the `news.regime_vote_path`/`signal_path`/`rss_state_file` keys of the install settings.yaml (`--settings`, default two levels above `--base-dir`), resolved against that file's folder; paths never reach a client. Labels are zh-TW only (the desktop tab is bilingual). Vote/bridge files are re-read only when their stat changes: the bridges replace them with a bare `os.replace` (no retry), so any open handle risks a lost vote. Votes show as used only for `--news` bots (the runner reads none otherwise), and a W2 vote past `nightly_vote_max_age_h` at the ~04:58 classify without `admitted_at` is marked 將失效. The 新聞抑制中 chip uses `BreakerState.gates_leg` with the bot's `news_suppress_scope`.
- Colors follow 紅漲綠跌 (red = profit/up). Health states use blue/orange so they never read as P&L. `trading_mode != "paper"` shows a 實單 badge.

## Conventions
- Python 3.13+ on Windows 11
- Snake_case for functions/variables, PascalCase for classes
- No pandas/numpy - indicators use pure Python
- settings.yaml is NEVER committed (contains credentials)
- SDK directory (CapitalAPI_2.13.57/) is gitignored (large binaries)

## COM Tick History Replay (CRITICAL — issue #50)
- After `RequestTicks`, COM replays historical ticks before sending live ticks
- History ticks SHOULD come via `OnNotifyHistoryTicksLONG` (is_history=True)
- **COM sometimes sends history via `OnNotifyTicksLONG` (is_history=False)** — this mislabeling caused the strategy to run on stale data
- Defense: `_on_com_tick` compares tick datetime vs `_taipei_now()`. If tick > 120s old, treat as history regardless of is_history flag
- `suppress_strategy` must be True during ALL history replay. Do NOT trust the is_history flag alone.

## TradingGuard Fill-Pending Gate
- `BLOCK_FILL_PENDING` blocks entries AND exits while waiting for fill confirmation
- `FORCE_CLOSE` bypasses the gate (user emergency exit)
- **Deferred close (issue #50)**: when `TRADE_CLOSE` is blocked by fill_pending, the decision is stored via `guard.defer_close()`. After `_on_fill_confirmed("entry")`, it's popped and replayed automatically. Without this, rapid bar replay can permanently lose exit orders.
- Never rebind `self._trading_guard` — `_fill_poller` holds a reference to it (issue #43)

## Live Next-Open Entry Fill (backtest parity)
- Backtest runs `fill_mode="next_open"` (signal on bar N fills at bar N+1's open). Live/paper used to fill the placeholder at bar N's CLOSE — at a session boundary that close is the PREVIOUS session's last price, 75+ min stale (incident: sim 44,930 = day close vs real IOC fill 44,774 = night open, 156pt phantom gap in Discord 模擬價)
- Fix: `LiveRunner._entry_fill_price()` — the next bar's open is ALREADY KNOWN at signal time (the aggregator finalizes bar N only when the next window's first 1-min bar arrives, so `get_partial_bar().open` IS the next-open). Fallbacks: fresh tick ≤120s (`latest_tick_price`, mirrored from `_on_com_tick` before the BarBuilder feed — 1-min pass-through has no partial) → `bar.close` (stop-flush edge)
- Passed to `broker.on_bar_close(..., entry_fill_price=)` — overrides the ENTRY fill only. Market closes (`broker.close()`) still fill at bar.close, identical to backtest close-fill semantics. Do NOT route entry_fill_price into market closes
- Fill timing/entry_bar_index unchanged (synchronous placeholder for the real-fill race, issue #45); only the placeholder PRICE changed. Real fills still overwrite via try_set_real_entry_price
- ENTRY_FILL decision reason now says "filled at next-open" / "filled at tick" / "filled at bar close" (source audit trail)

## Real Entry Price Tracking (issue #45)
- `SimulatedBroker.real_entry_price`: set by `_on_fill_confirmed("entry")` from OpenInterest avg_cost
- `try_set_real_entry_price()`: guarded write with 4 checks (price>0, position>0, not already set, entry_bar_index matches). Prevents late callbacks from planting stale values on the next trade.
- `effective_entry_price()`: returns real if confirmed, else simulated. Strategies use this for slippage-aware stops.
- Belt-and-braces: `on_bar_close` resets `real_entry_price=0` on every new entry

## Real Exit Price Tracking (issue #92)
- `SKReplyLib.OnNewData` is the ONLY per-fill price source: type "D" (deal) rows carry the actual fill price for ONE order, keyed by the 13-digit seq no `SendFutureOrderCLR` returned
- **`GetFulfillReport(..., 4)` ("合併同商品") merges fills by commodity — its price is a DAY-CUMULATIVE AVERAGE per side, never a fill price.** Display-only. Using it as a price source is what reported exit @44,794 (day average) when the bill said 44,622.
- `src/live/fill_report.py`: `RealFillTracker.register_order(seq, action_type, bar_index, sim_price)` on order OK; `on_new_data(raw)` matches deal rows by seq — unmatched rows (another bot / manual order on the same account) are ignored
- `broker.try_set_real_exit_price()`: guarded write mirroring the entry guard (no trade / already set / exit_bar_index mismatch drops late ROD fills for previous trades)
- Discord: `fill_confirmed` uses the real price when the deal row beat the OpenInterest confirm (normal case); else "(est.)" + `fill_price_correction` follow-up when OnNewData lands
- Entry keeps OpenInterest avg_cost as fallback; OnNewData writes first when faster (identical for 1-lot)

## BarAggregator 1-min Pass-Through (issue #44)
- For `target_interval == 60`, `on_bar()` returns the bar immediately (no accumulation delay)
- Without this, 1-min strategies had a permanent 1-bar lag (chart and strategy both 1 bar behind)
- H1/H4 aggregation is unchanged (accumulate until boundary cross)

## CSV Reload for 1-min Strategies (issue #47)
- `reload_1m_bars()` merges CSV bars into `_aggregated_bars` for `target_interval == 60`
- `feed_warmup_bars()` seeds `_seen_1m_dts` for 1-min strategies to prevent duplicates
- COM warmup API does NOT return bars for the currently in-progress trading session
- Without the merge, the live chart had a ~10-hour gap between warmup end and first live bar

## Regime Switching (Phase 3)
- `RegimeSwitchingRunner(LiveRunner)`: bot type 2, classifies regime per NIGHT session, swaps long/short strategies at session boundaries
- **Session identity = OPEN date** (`current_session`/`latest_night_session` in switch_logic): the midnight-straddling night keeps ONE key ("2026-07-09|NIGHT" for the night opening 07-09 15:00). All regime keys, history rows, and P&L windows use it. `session_slot(now)` (calendar-date at poll time) survives ONLY for the daily-report key — do NOT use it for session identity.
- `current_session` returns None on gaps/weekends/TAIFEX holidays → no phantom rows/classifications on closed days (a Sat 13:43 poll once wrote a "2026-07-11,DAY" row)
- `classification_due`: fires in the night's last 2 min OR any time after close while unassessed (catch-up after app sleep/hang, and lets a fresh deploy classify immediately); 30-min backoff when bars are insufficient
- Poll order is classify → record → apply: `record_session_result` UPDATES the row for (date,session) in place (idempotent re-record; stop()/restart can't double-count), so the classification row carries its own P&L
- `_compute_session_pnl` matches trade exits inside the session's [open_dt, close_dt] window — never by calendar date (date-matching lost pre-midnight night P&L and double-counted post-midnight into DAY)
- `stop()` records AFTER `super().stop()` so the force-close trade is included in the session row
- Classifier bars come from `_classifier_bars` (default provider): in-memory `_aggregated_bars` (pre-seeded by the warmup KLine fetch → classifies from night one) vs CSV 1-min history, whichever aggregates to more classify-interval bars. Bound method — never capture the list object (`_rebuild_timeframe` rebinds it, issue #43 family)
- Stale pending recommendations (older than the last completed night) are discarded, not applied; unknown-strategy applies are discarded instead of retried every 30s forever
- Flip-counter pause uses a true sliding window (`flip_sessions`); `adx_strong` fast-track is disabled when `adx_strong <= adx_enter` (raising adx_enter must not silently reduce confirm_sessions to 1)
- **Pause/streak rules** (`RegimeConfig.pause_freezes_exits` / `exits_count_as_flips` / `transitional_resets_streak`, all default False = current behaviour; True restores the legacy behaviour that knob replaced): (1) a flip pause freezes trend ENTRIES only — a confirmed EXIT to `range-bound` still applies while paused, and the selector then runs its normal range-bound branch instead of `hold`; (2) only trend entries append to `flip_history`/`flip_sessions`, so an exit can never arm a pause; (3) a `transitional` read neither increments nor resets `pending_count`. Legacy freeze-everything + reset-on-transitional is what kept TMF00 short-only from 08-17 to 08-28 on stale `trending-down`
- **External votes may only ADD in the direction the regime leg already holds, never open a position from flat.** `RegimeConfig.vote_range_probe` (default False) gates the selector's range-bound half-size vote probe; vote acceleration of a trend *confirmation* in `step()` is NOT gated (that is adding in the held direction). Set `regime.vote_range_probe: true` to restore the probe — note "half" is still 1 lot today
- `in_closed_gap(now)`: True during gaps between sessions (when swaps can be applied)
- `swap_strategy(new_strategy, display_name)`: 5 safety gates (flat, no veto, not in replay, same timeframe, sufficient bars)
- `regime_idle`: separate from `suppress_strategy` — suppresses trading but keeps DataStore/CSV flowing
- On-disk dedup: `state.last_assessed = "open_date|NIGHT"` prevents double-classification across restarts. Pre-v2.16 files stored the CLOSE date; `load_state` auto-migrates it once (translates to the covered night's open-date key, gated by the `key_format` marker `save_state` stamps) — no manual edit, no skipped night, no duplicate assessment
- `record_session_result()`: writes P&L from switching runner's own broker (replaces retired `backfill_pnl`)
- History v2 schema: 4 new columns (strategy_active, applied, applied_at, trading_mode)
- Deploy: `_deploy_live` branches on `regime.enabled` to construct `RegimeSwitchingRunner` vs `LiveRunner`
- Evolution disabled for regime bots (`hasattr(runner, 'on_status_poll')` duck-typing gate)
- Report files namespaced: `{date}_{bot_name}.json` to avoid collision between concurrent bots
- Daily report includes `per_strategy` breakdown and `regime_switching` section for Discord

## AI Code Generation
- Indicators return named tuples: `BollingerResult(upper, middle, lower)`, `MacdResult(macd_line, signal_line, histogram)`, `StochasticResult(k, d)` — both `bb.middle` and `upper, mid, lower = bb` work
- **No `bb_std`/std attribute exists anywhere** (pure-Python indicators; no pandas_ta/talib in this repo). BB std-dev is not returned — recover it as `(bb.upper - bb.middle) / num_std` (population std, ddof=0, matches TradingView `ta.stdev`). `self.bb_std` in strategies is a self-defined constructor param (the σ multiplier): `self.bb_std = float(kwargs.get("bb_std", 2.0))`. Referencing it without that line → runtime AttributeError (the BbandMonthShortV5 crash).
- `broker.queue_exit()` auto-rounds float limit/stop prices to int (TAIFEX prices are integers)
- `extract_python_code()` strips trailing markdown that leaks into code fences
- Trade.entry_dt and exit_dt are STRINGS ("YYYY-MM-DD HH:MM"), not datetime objects
- Code gen prompt uses task+constraint framing, NOT expertise persona (research: personas hurt code accuracy)
- AI Review prompt limits to 1 change per iteration, requires identifying what works first, warns about overfitting
- Python 3.13: exception variables (`e` in `except ... as e:`) are deleted after the block — use `lambda err=e:` not `lambda: ...e...`
