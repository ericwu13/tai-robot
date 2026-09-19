# UI Modernization — Per-File Implementation Details

Companion to [`docs/ui-modernization-plan.md`](ui-modernization-plan.md). For every file touched or created across Phases 1–4: what exactly changes and why. Line references are against master @ `a18c155`.

Conventions used below:
- **Action**: CREATE / MODIFY / DELETE / RENAME.
- "the seam" = the `_confirm(title, msg)` / `_alert(title, msg)` headless contract (`run_backtest.py:5895–5906`, `src/live/headless.py:41–68`, enforced by `tests/test_bot_cli.py:80–82`).
- "the queue discipline" = COM callbacks enqueue-only into `_tick_queue`/`_ui_queue` (`run_backtest.py:674–683`), drained on the Tk thread by `_drain_ui_queue` (`:956`) and `_drain_tick_queue` (`:7026`).

---

## Phase 1 — Tk quick wins

### 1.1 `src/ui/__init__.py`

- **Action**: CREATE
- **Why**: new package for extracted UI infrastructure; `src/` is already the bundled package root (`tai_backtest.spec:36` bundles all of `src`), so nothing spec-side is needed for a pure-Python subpackage *except* hiddenimports (see 1.7).
- **What changes**: empty (docstring only), matching the other `src/*/__init__.py` files.
- **Dependencies**: precedes every other `src/ui/*` file.

### 1.2 `src/ui/theme.py`

- **Action**: CREATE
- **Why**: the repo has zero `ttk.Style` usage and ~40 inline hex literals; this module becomes the single owner of palette, styles, and fonts.
- **What changes** (exports):
  - `PALETTE: dict[str, str]` — the §7 token table from the plan (`bg #16161a`, `bg_raised #1e1e24`, `bg_inset #121216`, `border #2e2e36`, `text #d6d6dc`, `text_dim #8a8a94`, `accent #4f8cff`, `ok #2ecc8f`, `warn #f0b429`, `err #ef5350`, `info #8ab4f8`, `up #26a69a`, `down #ef5350`).
  - Constants that absorb existing literals: `_TONE_COLORS` moves here from `run_backtest.py:3634` (as `TONE = {"good": PALETTE["ok"], "bad": PALETTE["err"]}` — note the *values* change from `#0f6e56`/`#a32d2d` to the dark-theme pair; the Report/Regime card renderers pick the change up automatically). Chat tag colors (`run_backtest.py:1565–1569`: `#569cd6`, `#d4d4d4`, `#ce9178`, `#f44747`, `#6a9955`) move here as `CHAT_TAGS`. Live-log tag colors (`:1975–1978`: `#4caf50`, `#f44336`, `#90caf9`, `#ffc107`) as `LOG_TAGS`. Vote-liveness dot colors (`:3949–3953`: `#9e9e9e`, `#c62828`, `#2e7d32`) as `DOT_COLORS` (remapped to `text_dim`/`err`/`ok`). Regime episode band colors (`:2037–2045`) as `EPISODE_TAGS` (dark-theme equivalents). Order-dialog `#228B22`/`#DC143C` (`:7605`) → `ok`/`err`.
  - `FONTS` — four named `tkfont.Font` objects (`title` 16b, `section` 13b, `value` 11b, `body` 10, plus `mono` Consolas 10 and `mono_small` Consolas 9), created in `init_theme`; replaces the 16 ad-hoc font tuples counted in the inventory (`("Consolas", 10, "bold")` ×6, `("Consolas", 9)` ×5, etc.).
  - `init_theme(root) -> ttk.Style` — `style.theme_use("clam")`; configures `TFrame/TLabel/TButton/TEntry/TCombobox/TNotebook/Treeview/TLabelframe/TPanedwindow` against `PALETTE`; defines named styles `Card.TFrame`, `CardValue.TLabel`, `Status.Ok.TLabel`, `Status.Warn.TLabel`, `Status.Err.TLabel`, `Dim.TLabel`; sets `root.configure(bg=PALETTE["bg"])` and Tk scaling (`root.tk.call('tk', 'scaling', …)` from `winfo_fpixels('1i')/72`).
- **Dependencies**: must land before any widget conversion in `run_backtest.py`; no imports from `run_backtest.py` (one-way dependency only, or `HeadlessBotApp` construction would cycle).

### 1.3 `src/ui/widgets.py`

- **Action**: CREATE
- **Why**: three hand-rolled copies of the same scroll rig, a one-off tooltip, and three ad-hoc read-only log Texts need a single home.
- **What changes** (exports):
  - `class ScrollableFrame(ttk.Frame)` — replaces the Canvas+Scrollbar+inner-frame+mousewheel rigs at `run_backtest.py:1806–1821` (Report tab body) and `:5529–5557` (deploy dialog); binds `<MouseWheel>` on `<Enter>`, unbinds on `<Leave>`, and unbinds on `destroy()` (the current rigs leak `bind_all` across tab switches).
  - `attach_tooltip(widget, text)` — moves `_attach_tooltip` from `run_backtest.py:877–903`; drops the `#ffffe0` Win95 yellow for `PALETTE["bg_raised"]` + `border`.
  - `class TagTextLog(ttk.Frame)` — wraps a read-only `ScrolledText` with: tag palette injection, `append(line, tag)` that flips NORMAL→insert→DISABLED internally, a `max_lines` cap (default 5,000; trims from the top), a level-filter combobox (All/Info/Debug), incremental search with highlight, and a pause-follow toggle. Consumers: chat display (`:1557–1562`), live log (`:1971–1978`), Log tab (`:2054`).
  - `class StatusDot(ttk.Label)` — `set_state("ok"|"warn"|"err"|"off")` rendering a colored `●`; replaces the ad-hoc vote-dot labels (`:3947–3955`) and powers the Phase 1 status strip.
- **Dependencies**: imports `src/ui/theme.py`; precedes the `run_backtest.py` conversions that consume it.

### 1.4 `src/ui/labels.py`

- **Action**: CREATE (skeleton in Phase 1, populated opportunistically through Phase 2)
- **Why**: ~200 inline `中文 English` literals; one module so Tk and (later) the web UI share strings.
- **What changes**: plain constants (`DEPLOY_BOT = "部署機器人 Deploy Bot"`, `STOP_BOT = "停止機器人 Stop Bot"`, `READY = "就緒 Ready"`, tab titles, dialog titles…). **The three seam titles are imported from `src/live/headless.py` (`TITLE_STRATEGY_CHANGE`, `TITLE_EXISTING_POSITION`, `TITLE_DATA_RANGE`), not redefined** — they are a byte-exact contract. Migration rule: any Phase 1/2 diff that touches a line containing a bilingual literal moves it here; no dedicated migration PR.
- **Dependencies**: none; consumed incrementally.

### 1.5 `run_backtest.py` (Phase 1 scope)

- **Action**: MODIFY (largest and riskiest file; see Risk flags)
- **Why**: it is the UI. All Phase 1 changes are inside it or extract from it.
- **What changes**, grouped by PR-sized chunk:
  1. **Theme bootstrap**: in `BacktestApp.__init__` (`:907`), call `theme.init_theme(root)` before `_build_ui()` (`:931`). Then a mechanical surface-by-surface conversion of raw-`tk`-for-color widgets (~186: `tk.Label` ×67, `tk.Frame` ×43, `tk.Button` ×42, `tk.Entry` ×11, others) to ttk + named styles — order: chat panel (`_build_chat_panel` :1542), control panel (`_build_control_panel` :1621), Report tab card renderer (`_report_card` :3636 → uses `Card.TFrame`/`CardValue.TLabel` and `theme.TONE`), Live tab (`:1855–1978`), Regime tab (`:1984–2046`), dialogs. Delete `_TONE_COLORS` (`:3634`) and every inline hex as each surface converts.
  2. **Status strip**: replace the gray in-panel status label (`:1739–1741`) with a bottom-packed strip: `StatusDot` (connection) + message label + history button. New method `set_status(self, msg, level="info")` appends to a `collections.deque(maxlen=20)` and styles by level. The ~90 `self.status_var.set(...)` call sites become `self.set_status(...)` — default level mechanical; upgraded sites: chart error (`:4218`) → `error`; reconnect-ladder messages (`_drain_ui_queue` disconnect branch near `:978–1061`) → `warn`; order/fill failures → `error`. Connection dot is driven from the existing `conn` events in `_drain_ui_queue` (`:978–983` ready/quote/disconnected) — no new state, just a second consumer.
  3. **Un-block TV fetch**: `_fetch_tradingview_live` (`:3436–3442`) — delete the `self.root.update()` at `:3438`; move the blocking `fetch_tv_dataframe(...)` call into a `threading.Thread(daemon=True)` worker that posts completion via `self.root.after(0, …)`, cloning the `_backtest_worker` idiom (`:3585`). Buttons disable during fetch, re-enable in the completion callback.
  4. **Un-block deploy OI wait**: `_deploy_live_from` (`:6072–6078`) — delete the `while … update_idletasks(); _drain_ui_queue(); time.sleep(0.1)` spin (the file's only `time.sleep` on the Tk thread). Split `_deploy_live_from` at that point: everything after the position check moves to a continuation method `_deploy_live_continue(self, req, oi_snapshot)`; the wait becomes an `after(100, poll)` chain that checks the same `OI_SNAPSHOT_TIMEOUT_S = 5.0` deadline (`:142`) and then calls the continuation. **Headless caution**: `HeadlessBotApp` calls `_deploy_live_from` synchronously and its Tk loop isn't running — the poll chain must fall back to a plain blocking loop *without* `update_idletasks` when `self.root.winfo_viewable()` is false / a new `self._headless` flag is set (headless has no UI to freeze). Return-value semantics of `_deploy_live_from` (bool) must be preserved for `headless_app.py:250–252`.
  5. **Incremental Trades render**: `_display_results` (`:3814–3847`) — add `self._rendered_trade_count`; on live updates (`_update_live_results` :8538 path) only `tree.insert` trades beyond the watermark instead of `delete(*get_children())` + full reinsert; full rebuild still used when a new backtest result replaces the trade list (watermark reset in `_on_backtest_done` :3597). `_sort_trade_tree` (`:4113`) resets the watermark to force full rebuild while a sort is active.
  6. **Report render throttle**: `_render_report_view` (`:3649`) — wrap in a 1 s coalescer (`self._report_render_pending` + `after(1000, …)`); `_on_live_decision` (`:7327`) may fire it per trade close today.
  7. **Log routing fix**: `_log` (`:691–717`) — the `:711` main-thread check currently *drops* widget output from other threads; change to `_ui_queue.put_nowait(("log", line))` (the `log` handler already exists in `_drain_ui_queue`). `_log_debug` (`:729–755`) additionally enqueues as `("log_debug", line)` — also an existing handler — so the Log tab's Debug filter can show `[CONN]`/`[RECONNECT]`/`[TICKS]`/`[WATCHDOG]` lines.
  8. **Log tab → `TagTextLog`**: `:2052–2056` — replace the bare `ScrolledText` with `TagTextLog(levels=("info","debug"))`; same for the Live log (`:1971–1978`, keep its 4 tags) and chat display (`:1557–1562`, keep its 5 tags).
  9. **Transient-line marks**: `_append_chat` (`:2142–2159`) sets a `tk.Text` mark pair around transient "Thinking…" system lines; `_remove_last_system_line` (`:2161–2190`) deletes by mark range instead of string search.
  10. **Dead code**: delete `_on_strategy_changed` (`:3006–3008`) and its `trace_add` (`:1641`); fix the stale comment in `_on_live_bar` (`:7297–7299`); delete `_attach_tooltip` (`:877–903`) in favor of the widgets import.
- **Dependencies**: 1.2/1.3 first. Chunks 1–2 and 7–8 are independent of 3–6; land smallest-risk-first (7, 10, 3, 4, 8, 2, 5, 6, 1, 9).

### 1.6 `tests/test_ui_theme.py`

- **Action**: CREATE
- **Why**: the headless invariant — every widget must construct on a withdrawn root with the theme active.
- **What changes**: (a) `init_theme` on a withdrawn `tk.Tk` root defines the named styles and doesn't raise; (b) constructing `HeadlessBotApp` (via the same harness `tests/test_bot_cli.py` uses) succeeds post-theme; (c) `set_status` levels map to the right style names; (d) `TagTextLog` cap trims from the top and `append` from a non-main thread raises (documenting that producers must go through the queue). Skip-guard the Tk tests the same way existing GUI-adjacent tests do (Tk unavailable in some CI shells).
- **Dependencies**: after 1.2/1.3/1.5-chunk-2.

### 1.7 `tai_backtest.spec`

- **Action**: MODIFY (small, Phase 1)
- **Why**: `hiddenimports` (`:43–103`) is an explicit hand-maintained list; `src.ui.*` are imported statically by `run_backtest.py` so PyInstaller *should* find them, but the spec's own comment (`:14–18`, issue #58) documents the policy of listing `src.*` modules explicitly.
- **What changes**: append `'src.ui', 'src.ui.theme', 'src.ui.widgets', 'src.ui.labels'` to `hiddenimports`.
- **Dependencies**: same release as the first `src/ui` import lands.

---

## Phase 2 — Workbench redesign + NotifierBus extraction

### 2.1 `src/live/notifier_bus.py`

- **Action**: CREATE
- **Why**: the ~45 `_discord.*` call sites hard-wired in `run_backtest.py` are the single reason Discord is the only remote view; the bus makes Discord, the Tk Live log, and (Phase 3) the web UI equal subscribers.
- **What changes** (exports):
  - `@dataclass(frozen=True) UiEvent` — `topic: str`, `payload: dict`, `ts: str` (TPE ISO), `origin: str` (`"manual"|"auto"|"live"`) — `origin` exists specifically to encode the manual-vs-auto evolution Discord asymmetry (`run_backtest.py:5420–5421`) as data instead of call-site branching.
  - `class NotifierBus` — `subscribe(fn: Callable[[UiEvent], None])`, `emit(topic, payload, origin="live")`; per-subscriber try/except that logs and continues, copied from `LiveRunner._emit` (`src/live/live_runner.py:834–840`) so a broken UI subscriber can never kill trading. Thread-agnostic: emits on the caller's thread; subscribers own their marshalling.
  - `TOPICS` — frozen set mirroring `DiscordNotifier`'s method surface: `order_sent`, `order_failed`, `fill_confirmed`, `fill_price_correction`, `fill_timeout_downgrade`, `bot_deployed`, `bot_deployed_regime`, `bot_stopped`, `mode_switched`, `daily_loss_limit`, `regime_swap`, `regime_idle_warning`, `regime_notify`, `daily_report`, `evolution_verdict`, `evolution_progress`, `strategy_improved`, `force_close_failed`, `status`. `emit` asserts membership (fail-loud on typos).
- **Dependencies**: precedes 2.2 and every call-site migration in 2.3. No imports from Tk or Discord.

### 2.2 `src/live/discord_notify.py`

- **Action**: MODIFY
- **Why**: `DiscordNotifier` (class at `:33`, `_send` at `:76–96`, per-type methods `order_sent:155` … `daily_report:380`) becomes one bus subscriber instead of the direct call target.
- **What changes**: add `attach_to_bus(self, bus)` registering `self._on_event(event)`; `_on_event` dispatches `topic → existing method` via a dict, passing payload fields as today's kwargs. Filtering moves *into this subscriber*: `evolution_verdict` always sends; `evolution_progress` never sends (stage messages stay Tk-only — today's behavior); early-stop/exception events send only when `event.origin == "auto"` (the `:5420` asymmetry). Channel routing (`_resolve_channel` `:72`, evolution channel `:36–59`) is untouched. The public methods stay callable directly so existing tests keep passing during migration.
- **Dependencies**: after 2.1; before the `run_backtest.py` call-site migration.

### 2.3 `run_backtest.py` (Phase 2 scope)

- **Action**: MODIFY
- **What changes**, by chunk:
  1. **Bus wiring**: replace the module-global `_discord = None` (`:642`) with a module-global `_bus = NotifierBus()` created at import; `DiscordNotifier` is constructed exactly where it is today (`:6188–6198`, the repo's only ctor call) and attached via `attach_to_bus`. A Tk subscriber (registered in `__init__`) marshals via `root.after(0, …)` and feeds the Live log + status strip.
  2. **Call-site migration** (mechanical, one PR per group; acceptance = byte-identical Discord output): deploy/lifecycle `:6205–6207, 6328–6347`; regime `:6217–6227, 6308–6321`; status `:6742–6744`; mode switch `:7291–7293`; daily report `:7353–7355`; evolution `:4758–4847, 5130–5423, 6905–6907, 7412–7415` (evolution `ui(self._append_chat, "system", …)` progress sites additionally emit `evolution_progress`); orders `:7802–7861`; fills `:8070–8159`. Each `_discord.X(a, b)` → `_bus.emit("X", {...}, origin=…)`. The `if _discord and _discord.enabled` guards are deleted (the Discord subscriber checks `enabled` itself).
  3. **Deploy dialog replacement**: `_show_bot_session_dialog` (`:5496–5843`, ~350 lines incl. closures `on_load/on_delete/on_select/on_create/_toggle_regime_widgets/_toggle_news_widgets`) is deleted; `_deploy_live` (`:5875–5891`) calls `src/ui/deploy_dialog.open_deploy_dialog(root, ctx) -> DeployRequest | None` instead, then passes the request straight to `_deploy_live_from` — the tuple hop and `DeployRequest.from_dialog_tuple` call at `:5891` disappear. The dialog-context object carries what the closure web reads today: symbol, `base_dir`, `STRATEGIES`/`NEWS_ONLY_STRATEGIES` keys, current strategy, settings defaults (`regime_long_strategy`/`regime_short_strategy`), `self._logged_in and self._futures_account` (gates semi_auto/auto radios, today at `:5737–5741`).
  4. **Daily-report browser**: Report tab gains a header row (bot × date combos over `list_reports()` / `load_report()` from `src/daily_report/report_generator.py:350–366`) that switches `_render_report_view`'s source between the live/backtest result and a loaded report dict shaped by `src/ui/report_browser.py`.
  5. **Evolution persistence + panel**: the pipeline's terminal sites write a run record via `src/evolution/run_log.py` — verdict `:5403–5413`, plan-unusable `:5162–5178`, no-change `:5181–5187`, source-unavailable `:5199–5205`, codegen-failed `:5266–5270`, exception `:5422–5425`, plus stage-reached breadcrumbs at `:5210, :5315, :5338, :5464`. A new "🧬 進化 Evolution" sub-view (list + verdict detail) renders `run_log.list_runs()`.
  6. **Regime effective-config panel**: Regime tab adds a grid rendered from `config_loader.effective_config_rows()` (see 2.6).
  7. **Log-file viewer**: Log tab gains a file mode — bot-dir picker + `tail_lines(path, n)` (`src/live/headless.py:243–252`) with a 2 s `after` follow loop, severity coloring by prefix (`[WATCHDOG]`/`[RECONNECT]` → warn, `Traceback` → err).
- **Dependencies**: chunk 1 before 2; chunk 3 needs 2.4+2.5; chunk 5 needs 2.7.

### 2.4 `src/ui/deploy_dialog.py`

- **Action**: CREATE
- **Why**: the deploy dialog is the workbench's worst UX and its 10-tuple return is the workbench's most fragile interface; extraction also finally makes the dialog unit-testable apart from `BacktestApp`.
- **What changes** (exports): `open_deploy_dialog(parent, ctx: DeployDialogContext) -> DeployRequest | None` implementing the plan §6.2 two-step flow:
  - Step 1: existing-bot cards built from the same scan `_show_bot_session_dialog` does today (`run_backtest.py:5502–5513`: `data/live/{symbol}_*` dirs + `load_session`), plus a New-bot card. Delete keeps the `askyesno` + `shutil.rmtree` behavior (`:5626–5641`) — this is a user-initiated destructive action and stays behind an explicit confirm.
  - Step 2: name entry (default = strategy name, spaces→underscores, dup/empty validation as `:5702–5706`); mode as three option cards (paper/semi_auto/auto) with the same enable gate (login + futures account) and a typed-"AUTO" confirmation for auto on a real account; loss-limit entry (default `"10000"`, `:5748`); regime sub-panel (enable → leg combos filtered by `NEWS_ONLY_STRATEGIES`, running `validate_leg_strategies` inline and disabling OK on failure — replacing the post-hoc `_alert` at `:6034`); the three news checkboxes with the same nesting/graying rules (`:5777–5818`).
  - Returns a `DeployRequest` built via the new named constructor (2.5). Modal via `wait_window` — acceptable: deploy happens before ticks matter, and the headless path never opens it.
- **Dependencies**: 2.5 first; consumed by 2.3-chunk-3.

### 2.5 `src/live/deploy_request.py`

- **Action**: MODIFY
- **Why**: it already defines the named shape (`_DIALOG_FIELDS`) the 10-tuple maps onto.
- **What changes**: add a keyword-only classmethod `DeployRequest.build(*, bot_name, resume_session, trading_mode, loss_limit, regime_enabled, regime_long, regime_short, news_enabled, news_tier2_enabled, news_directional, origin)`. `from_dialog_tuple` and `_DIALOG_FIELDS` are kept until the CLI builder (`src/live/headless.py` `build_deploy_request`) and tests migrate to `build`, then deleted in a follow-up — not in the same PR as the dialog swap.
- **Dependencies**: before 2.4.

### 2.6 `src/regime/config_loader.py`

- **Action**: MODIFY
- **Why**: `build_regime_config` is the only code that knows a field's provenance (settings key via `_SETTINGS_KEY_MAP:36–53`, evolved overlay `:79–88` from `data/regime/regime_evolved_params.json` with the 90-day age gate, or dataclass default) — the effective-config panel must not re-derive it.
- **What changes**: add `effective_config_rows(settings, dialog_choices) -> list[ConfigRow]` where `ConfigRow = (field, value, source, detail)`, `source ∈ {"dialog","settings","evolved","default"}`, `detail` = overlay age for evolved rows. Implemented by instrumenting the existing build path (record provenance while assembling), *not* by a parallel reimplementation — `build_regime_config`'s output must remain bit-identical (assert in tests).
- **Dependencies**: before 2.3-chunk-6. `tests/test_regime_ui_settings.py` extends here.

### 2.7 `src/evolution/run_log.py`

- **Action**: CREATE
- **Why**: evolution outcomes currently exist only as chat scrollback; the monitor suite reconstructs runs forensically from `ai_usage.csv` + watermarks. Durable run records fix the GUI view, the web timeline (Phase 4), *and* the weekly-review evidence problem at once.
- **What changes** (exports): `record_stage(week_key, stage, detail)`, `record_verdict(week_key, passed, verdict_block, metrics, origin)`, `record_abort(week_key, reason, origin)` — all append/update `data/evolution/runs/{ISO-week}.json` (schema: `trigger`, `data_window`, `plan_digest`, `candidate`, `stages: [...]`, `verdict`, `metrics`, `finished_at`); `list_runs()`, `load_run(week_key)`. Write style: read-modify-write with `os.replace` atomicity, mirroring `session_store` discipline. Callers: the seven `run_backtest.py` sites in 2.3-chunk-5.
- **Dependencies**: before 2.3-chunk-5. New test `tests/test_evolution_run_log.py` (CREATE): stage accumulation, abort vs verdict exclusivity, idempotent re-record for the same week.

### 2.8 `src/ui/report_browser.py`

- **Action**: CREATE
- **Why**: keeps the daily-report shaping out of `run_backtest.py`, following the `report_view.py` pure-view-model precedent.
- **What changes** (exports): `report_to_view(report: dict) -> ReportView` mapping a daily-report JSON (incl. `per_strategy` and `regime_switching` sections) onto the same card/section shapes `src/backtest/report_view.py` produces, so `_render_report_view` renders both without branching. Pure module, no Tk import.
- **Dependencies**: before 2.3-chunk-4; test `tests/test_report_browser.py` (CREATE) from a fixture report JSON.

### 2.9 `tests/test_notifier_bus.py`

- **Action**: CREATE
- **What changes**: subscriber isolation (raising subscriber doesn't stop others — clone of `LiveRunner._emit` semantics), unknown-topic rejection, `origin` filtering in the Discord adapter (evolution asymmetry: auto sends aborts, manual doesn't; progress never sends), and a golden test that replays a scripted event sequence through `DiscordNotifier._on_event` against the pre-migration direct-call output (byte-identical message bodies).
- **Dependencies**: with 2.1/2.2. The existing `tests/test_discord_*.py` files (deploy_regime, evolution, mode_switch, retry_and_dedup) **stay green untouched** during migration because the public methods remain; once all call sites emit via bus, extend — do not rewrite — them to go through the adapter.

---

## Phase 3a — Read-only web dashboard (sidecar)

### 3.1 `src/webui/__init__.py`

- **Action**: CREATE — empty package init.

### 3.2 `src/webui/app.py`

- **Action**: CREATE
- **Why**: the FastAPI application; a transport wrapper over already-existing read models, deliberately containing no business logic.
- **What changes** (exports `create_app(settings) -> FastAPI`), routes → existing code:
  - `GET /api/bots` → `list_bots` / `bots_as_json` (`src/live/headless.py:363–378, 410–411`)
  - `GET /api/bots/{name}` → `bot_info` (`headless.py:333–360`) + a whitelisted subset of `session.json` broker keys (`position_size, position_side, entry_price, trades, equity_curve, _cumulative_pnl`)
  - `GET /api/bots/{name}/decisions` → tail of `decisions.csv` (header contract at `src/live/csv_logger.py:53–73`)
  - `GET /api/bots/{name}/log` → `tail_lines` (`headless.py:243–252`) over `debug_*.log` / `cli_stdout.log` only (filename whitelist — no arbitrary paths)
  - `GET /api/bots/{name}/regime` → `regime_state.json` + `regime_history.csv` shaped by `src/regime/episodes.py`
  - `GET /api/reports` → `list_reports` / `load_report` (`src/daily_report/report_generator.py:350–366`) through `report_browser.report_to_view`
  - `GET /api/votes` → `src/news/vote_status.py` (after 3.6)
  - `GET /api/health` → imported check functions from `scripts/monitor/` (`check_bots`, `check_regime`, `check_logs` — imported as the package they already are, never shelled out)
  - Static mount of `web/static` at `/` (path resolved frozen-aware via the same `sys.frozen` pattern as `run_backtest.py:30–37`).
  - All responses pass through a serializer that applies `redact()` (`scripts/monitor/common.py:141`) to any settings-derived strings.
- **Dependencies**: after 3.5 (settings section), 3.6 (vote paths); before 3.3/3.4.

### 3.3 `src/webui/auth.py`

- **Action**: CREATE
- **What changes**: `require_token` FastAPI dependency — no-op when bound to `127.0.0.1` with no token configured; otherwise compares `Authorization: Bearer` against `dashboard.token` (constant-time compare). Applied to *all* routes when bind ≠ localhost, and to write routes (Phase 3c) always.

### 3.4 `run_dashboard.py`

- **Action**: CREATE
- **Why**: the sidecar entry point (plan §5 picked the second-EXE route over a CLI flag for obvious process isolation).
- **What changes**: ~40 lines — frozen-aware `sys.path` bootstrap copied from `run_backtest.py:30–42`, load settings (via `scripts.monitor.common.load_settings`), refuse to start if `dashboard.enabled` is false, `uvicorn.run(create_app(settings), host, port)`. **No COM imports, no Tk imports, no `src.live.live_runner` import** — the sidecar reads disk only.
- **Dependencies**: 3.2 first; referenced by 3.7/3.8.

### 3.5 `settings.example.yaml`

- **Action**: MODIFY
- **What changes**: append a new top-level `dashboard:` section (greenfield — the file's 11 existing sections have no HTTP config): `enabled: false`, `host: "127.0.0.1"`, `port: 8410`, `token: ""` with comments stating the LAN/token rule and "not for public internet". Placeholder-only per the security constraint (no real values, ever).
- **Dependencies**: before 3.2/3.4.

### 3.6 `src/news/vote_status.py`

- **Action**: MODIFY
- **Why**: vote-file path resolution currently lives in the Tk app (`_regime_vote_paths`, `run_backtest.py:3905` — prefer the running runner's `NewsConfig`, fall back to settings.yaml) and the web UI needs the identical resolution.
- **What changes**: move that resolution in as `resolve_vote_paths(settings, news_config=None)`; `run_backtest.py` `_regime_vote_paths` becomes a two-line delegate (MODIFY, same PR). Existing shaping functions unchanged; `tests/test_vote_status.py` extends with the fallback-order cases.
- **Dependencies**: before 3.2.

### 3.7 `tai_backtest.spec`

- **Action**: MODIFY (Phase 3 scope)
- **What changes**:
  - `datas`: add `('web/static', 'web/static')` — same pattern as the `(_lwc_js, 'lightweight_charts/js')` entry at `:36–42`.
  - `hiddenimports`: add `src.webui.*`, plus `collect_submodules('fastapi')`, `collect_submodules('starlette')`, `collect_submodules('uvicorn')` and `collect_data_files('uvicorn')` (uvicorn's logging config is data + dynamic imports — exactly the issue-#58 / pyinstaller-dynamic-imports failure class).
  - Second `EXE` block for `run_dashboard.py` (`console=True`), added to the existing `COLLECT` so both EXEs share one `_internal/`.
- **Dependencies**: with 3.4; before any Phase 3 release.

### 3.8 `build_release.py`

- **Action**: MODIFY
- **Why**: the bundle-integrity guard is `src/`-scoped and would let stale frontend assets ship silently (the v2.13.0 failure mode, re-created for `web/`).
- **What changes**: `SOURCE_PATHS` (`:39`) gains `"web"` and `"run_dashboard.py"`; `verify_bundle_matches_source()` (`:77–106`) walks `dist/tai_backtest/_internal/web/static` against the working tree with the same byte-compare; the zip step packages the second EXE. Dirty-check (`:50–63`) covers the new paths automatically once in `SOURCE_PATHS`.
- **Dependencies**: same release as 3.7.

### 3.9 `pyproject.toml`

- **Action**: MODIFY
- **What changes**: `requires-python = ">=3.10"` → `">=3.13"` (aligns with CLAUDE.md/README, currently stale); `dependencies` gains `fastapi>=0.115`, `uvicorn>=0.30`.
- **Dependencies**: with 3.2.

### 3.10 `.github/workflows/test.yml`, `.github/workflows/autofix.yml`, `.github/workflows/pr-review.yml`

- **Action**: MODIFY (all three)
- **Why**: standing rule — every locally-added pip dep must be added to all three workflow install lines (`feedback_ci_workflow_deps`), or CI fails on the new imports.
- **What changes**: append `fastapi uvicorn httpx` (httpx is already a runtime dep; needed in CI for FastAPI's `TestClient`) to each `pip install` line.
- **Dependencies**: same PR as 3.9.

### 3.11 `web/static/index.html`, `web/static/theme.css`, `web/static/app.js`

- **Action**: CREATE (3 files; no build step, no npm)
- **What changes**:
  - `theme.css` — CSS custom properties mirroring `src/ui/theme.py`'s `PALETTE` token-for-token (names identical: `--bg`, `--ok`, `--warn`, `--err`, …); card/badge/dot/table component classes matching plan §7.
  - `index.html` — fleet page: one card per bot (name, mode badge, health dot, position, session P&L, last-event age) + per-bot detail view (status fields, decisions tail, log follow, regime strip). Phone-first: cards stack under a single media query.
  - `app.js` — vanilla JS (or Alpine.js vendored as a single file if reactive state gets noisy): 5 s `fetch('/api/bots')` poll, hash-routed detail views, health-dot logic from `log_age_min` / `running` fields (`BotInfo.as_dict` keys, `headless.py:281–299`).
- **Dependencies**: 3.2's static mount; `labels.json` generation is a ~10-line script inside `src/ui/labels.py` (`python -m src.ui.labels > web/static/labels.json`) invoked manually and verified by the 3.8 byte-compare.

### 3.12 `tests/test_webui_api.py`

- **Action**: CREATE
- **What changes**: FastAPI `TestClient` against `create_app` with `base_dir` pointed at a tmp fixture bot dir (fabricate `session.json`/`.lock`/`decisions.csv` with the obvious-placeholder rule — `F1111…`-style IDs, never real ones); asserts: bots list shape matches `BotInfo.as_dict`, log route rejects non-whitelisted filenames, token required on non-localhost config, redaction applied to settings-derived strings, and **no route ever writes** (fixture dir mtimes unchanged after a full GET sweep — the `read_lock` non-mutation contract, promoted to the API).

---

## Phase 3b/3c — Event streaming + guarded actions

### 3.13 `src/live/event_log.py`

- **Action**: CREATE
- **Why**: the plan's preferred 3b transport — the bot appends events to a file; the sidecar tails it. No server thread enters the bot process; crash-safe; replayable.
- **What changes** (exports): `class NdjsonEventLog` — `subscriber(event: UiEvent)` appending one JSON line to `{bot_dir}/events.ndjson` (open-append-flush per event, the `csv_logger.py:103–105` discipline), with daily size-based rotation (`events.ndjson.1`); `tail_events(bot_dir, after_offset) -> (events, new_offset)` for the sidecar. Registered on the bus at deploy.
- **Dependencies**: 2.1; before 3.14. Test `tests/test_event_log.py` (CREATE): append/rotate/tail-offset round-trip, malformed-line tolerance.

### 3.14 `src/webui/app.py` (3b/3c scope)

- **Action**: MODIFY
- **What changes**:
  - `GET /api/bots/{name}/events` — SSE endpoint (starlette `EventSourceResponse`-style generator) polling `tail_events` every 1 s and pushing deltas; browser gets live fills/status without page polling.
  - `POST /api/bots/{name}/stop` — writes the STOP file exactly as `request_stop` does (`src/live/headless.py:161–175`); token-required always; action appended to the bot's `events.ndjson`.
  - `POST /api/deploy` — body maps 1:1 onto the CLI deploy args; calls `build_deploy_request` + `spawn_detached` (`headless.py:215–231`) + `wait_for_ready` (`:255–276`, `READY_MARKER = "Tick subscription active"` at `:191`); inherits `HEADLESS_MODES = ("paper","auto")` (`headless.py:25`) so semi_auto is impossible by construction; returns the CLI's own exit-code semantics (0 ready / child-exit / 6 starting).
- **Dependencies**: 3.13; `src/live/headless.py` is **reused unmodified** — if any signature there needs changing, stop and re-scope (it is contract surface for the CLI and the deploy skill).

### 3.15 `run_backtest.py` (3b scope)

- **Action**: MODIFY (small)
- **What changes**: in `_deploy_live_from`'s success path (adjacent to the Discord construction at `:6188–6198`), register `NdjsonEventLog(bot_dir).subscriber` on the bus; unregister in `_stop_live` (next to the debug-log close at `:8697`).
- **Dependencies**: 3.13.

### 3.16 `web/static/app.js` (3b/3c scope)

- **Action**: MODIFY
- **What changes**: `EventSource` subscription per bot detail view; toast component for `fill_confirmed`/`order_failed`/`daily_loss_limit`; health dot goes live (event-driven freshness). Stop/deploy buttons behind a ConfirmPanel that mirrors the seam titles' copy and sends the bearer token; auto-mode deploy requires typing "AUTO" (mirrors 2.4).

---

## Phase 4 — Advanced surfaces (all additive)

### 4.1 `src/webui/app.py`

- **Action**: MODIFY — `GET /api/bots/{name}/bars?tf=` reading `bars_1m_*.csv` (writer contract `src/live/csv_logger.py:87–92`), aggregated via `src/live/bar_aggregator.py`'s pure aggregation, sanitized with `_ensure_ascending` imported from `src/backtest/chart.py:35` (importable without pywebview — the lightweight-charts import there is guarded, `chart.py:17–21`; the existing `tests/test_chart_sanitize.py` proves it imports headless). One out-of-order bar silently blanks a lightweight-charts pane (issue #97) — identical trap in the browser.

### 4.2 `web/static/` additions

- **Action**: CREATE `web/static/vendor/lightweight-charts.standalone.js` (vendored UMD build, pinned version noted in a `VENDOR.md` line) + `chart.js`, `regime-timeline.js`, `evolution-timeline.js`; MODIFY `index.html`/`app.js` to mount them.
- **What changes**: chart page = candles from `/api/bars` + trade markers from `/api/decisions` + partial-bar updates from the SSE stream; regime timeline = colored leg bands + swap markers + `NEWS_SUPPRESSED` spans from `/api/bots/{name}/regime`; evolution timeline = `data/evolution/runs/*.json` via a new `GET /api/evolution/runs` route (5-line addition to 4.1's scope); fleet header = all bots + `/api/health` verdict chips.

### 4.3 `src/regime/episodes.py`

- **Action**: MODIFY (optional, small) — add a `to_json_rows()` shaping helper if the existing episode structures aren't directly serializable; no logic change. Skip if `dataclasses.asdict` suffices.

---

## New files summary

| Path | Exports | Imported by |
|---|---|---|
| `src/ui/__init__.py` | — | package |
| `src/ui/theme.py` | `PALETTE`, `TONE`, `CHAT_TAGS`, `LOG_TAGS`, `DOT_COLORS`, `EPISODE_TAGS`, `FONTS`, `init_theme` | `run_backtest.py`, `src/ui/widgets.py`, `src/ui/deploy_dialog.py` |
| `src/ui/widgets.py` | `ScrollableFrame`, `TagTextLog`, `StatusDot`, `attach_tooltip` | `run_backtest.py`, `src/ui/deploy_dialog.py` |
| `src/ui/labels.py` | bilingual string constants; `main()` emitting `labels.json` | `run_backtest.py`, `src/ui/deploy_dialog.py`, `web/static` (generated) |
| `src/ui/deploy_dialog.py` | `open_deploy_dialog`, `DeployDialogContext` | `run_backtest.py` |
| `src/ui/report_browser.py` | `report_to_view` | `run_backtest.py`, `src/webui/app.py` |
| `src/live/notifier_bus.py` | `NotifierBus`, `UiEvent`, `TOPICS` | `run_backtest.py`, `src/live/discord_notify.py`, `src/live/event_log.py` |
| `src/live/event_log.py` | `NdjsonEventLog`, `tail_events` | `run_backtest.py`, `src/webui/app.py` |
| `src/evolution/run_log.py` | `record_stage`, `record_verdict`, `record_abort`, `list_runs`, `load_run` | `run_backtest.py`, `src/webui/app.py` |
| `src/webui/__init__.py` / `app.py` / `auth.py` | `create_app`, `require_token` | `run_dashboard.py`, tests |
| `run_dashboard.py` | `main()` (sidecar EXE entry) | `tai_backtest.spec` |
| `web/static/index.html`, `theme.css`, `app.js` (+ Phase 4: `vendor/lightweight-charts.standalone.js`, `chart.js`, `regime-timeline.js`, `evolution-timeline.js`, `labels.json`) | — | served by `src/webui/app.py` |
| `tests/test_ui_theme.py`, `test_notifier_bus.py`, `test_evolution_run_log.py`, `test_report_browser.py`, `test_webui_api.py`, `test_event_log.py` | — | pytest |

## Risk flags

- **`run_backtest.py` (8,752 lines) — touched in Phases 1, 2, 3b.** Highest-risk file in the repo: UI interleaved with live glue; `HeadlessBotApp` subclasses `BacktestApp`, so *every* change must construct on a withdrawn root; the deploy path must stay `messagebox`-free (`tests/test_bot_cli.py:80–82`). Mitigation: PR-per-chunk order in 1.5/2.3, `tests/test_ui_theme.py`'s headless-construction test in CI from Phase 1 on, and no chunk mixes UI conversion with live-glue edits.
- **`tai_backtest.spec` + `build_release.py` — touched in Phases 1 and 3, release-critical.** A missed hiddenimport ships an EXE that hangs at runtime (issue #58); an unguarded `web/` ships stale assets (v2.13.0 mode). Every spec change needs a frozen-build smoke run (`build → launch → hit /api/bots`) before release, per the release-exe-matches-commit rule.
- **`src/live/discord_notify.py` + the four `tests/test_discord_*.py` files** — the bus migration must be output-byte-identical; the golden replay test in 2.9 is the gate. Do not rewrite the existing Discord tests during migration.
- **`src/live/deploy_request.py`** — consumed by `src/live/headless.py`'s CLI builder and `tests/test_bot_cli.py`; the 10-tuple path is deleted only in a dedicated follow-up PR after both consumers migrate.
- **Multi-phase files**: `run_backtest.py` (1/2/3b), `tai_backtest.spec` (1/3a), `src/webui/app.py` (3a/3b/3c/4), `web/static/app.js` (3a/3b/4), `settings.example.yaml` (3a). Each phase's edit must not assume the later phase's shape.

## Do-not-touch list

Files this effort must not modify (reuse-only or fully out of scope):

- **COM/SDK surface**: `CapitalAPI_2.13.57/**` (gitignored SDK incl. `SKDLLPython.py`, `SKCOM.dll`), the COM event handler classes and module-level COM init in `run_backtest.py` (`_init_com` `:193`, `SKQuoteLibEvents` `:760`, `SKReplyLibEvent` `:842`, `SKOrderLibEvents` `:859`) and the two queue declarations `:674–683` — the queue discipline and the Tk-mainloop-as-COM-pump arrangement (`main()` `:8740–8746`) are invariants.
- **Live trading core** (reused via `.on()` / callbacks only, never edited for UI reasons): `src/live/live_runner.py`, `src/live/regime_switching_runner.py`, `src/live/trading_guard.py`, `src/live/fill_poller.py`, `src/live/fill_report.py`, `src/live/session_store.py`, `src/live/tick_classifier.py`, `src/live/tick_watchdog.py`, `src/live/bar_aggregator.py` (Phase 4 imports its pure aggregation), `src/live/account_monitor.py`, `src/live/connection_monitor.py`.
- **Headless contract**: `src/live/headless.py` and `src/live/headless_app.py` — Phase 3c *calls* `spawn_detached`/`wait_for_ready`/`request_stop` as-is; any needed signature change is a re-scope, not a drive-by. `tests/test_bot_cli.py` may gain assertions but existing ones must not be weakened.
- **Packaging plumbing**: `runtime_hook_comtypes.py`; `version.py` changes only via the normal release bump.
- **SDK tester GUIs**: `test_connection.py`, `test_kline.py` — deliberately mirror vendor examples for API debugging; restyling them adds risk with zero user value.
- **Heavily-mocked / contract tests**: `tests/mocks/**`, `tests/conftest.py`, and the regression suites for live incidents (`test_live_runner*.py`, `test_regime_switching_runner.py`, `test_trading_safety.py`, `test_reconnect_regression.py`, `test_fill_*.py`, `test_session_*.py`) — new UI tests are additive files; incident regressions are never edited to make UI work pass.
- **Strategy/AI/backtest engines**: `src/strategy/**`, `src/ai/**` (UI reads `chat_client` as today), `src/backtest/engine.py`, `broker.py`, `metrics.py` — `src/backtest/report_view.py` and `chart.py` are imported, not modified (except nothing: `_ensure_ascending` is imported in place).
