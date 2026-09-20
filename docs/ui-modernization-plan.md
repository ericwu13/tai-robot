# tai-robot UI Modernization Plan

Status: **DRAFT — for scoping** · Author: research pass over `run_backtest.py` (8,752 lines), `src/live/`, `build_release.py`, `tai_backtest.spec` · Date: 2026-09-19

This document is the implementation-scoping plan for modernizing the tai-robot UI ("fluid and user-friendly", not just prettier). All line references are against master @ `a18c155`.

---

## 1. Current state assessment

### What exists today

The entire GUI is one Tkinter application, `BacktestApp` (`run_backtest.py:906–8722`), started maximized at 1400x850 (`:912–918`). Layout is a horizontal `PanedWindow`:

- **Left pane (weight 2)** — AI 策略工作台 chat panel (`_build_chat_panel`, `:1542`): dark `ScrolledText` transcript (`#1e1e1e`), 3-line `tk.Text` input, Send / Generate Strategy / Export Pine / Save Strategy buttons, saved-strategy combobox row.
- **Right pane (weight 3)** — control panel (`_build_control_panel`, `:1621`): symbol/strategy combos, plaintext login row, a 12-button toolbar with a *hand-rolled flow layout* (`_reflow_toolbar`, `:3010`), a gray one-line status label, a collapsible backtest-settings LabelFrame; below it a 5-tab `ttk.Notebook` (`:1786`):

  | Tab | Content |
  |---|---|
  | 績效報告 Report | metric cards + per-strategy Treeview, rebuilt from scratch on every update (`_render_report_view`, `:3649`) |
  | 交易明細 Trades | 13-column sortable Treeview, full clear+reinsert per update (`:3814–3847`) |
  | 即時 Live | bot name/mode row, 5 status StringVars, manual-order buttons, 10-field real-account panel, dark event log |
  | 多空 Regime | 3 status cards, external-votes grid with liveness dots, episode-grouped switching-log Treeview — rebuilt from disk on tab select |
  | 紀錄 Log | one bare `ScrolledText`, no filter/search/level/cap |

Charts are **not** in the main window: `lightweight-charts` opens separate OS windows via pywebview/WebView2 (`src/backtest/chart.py`), fed live through thread-safe `push_bar`/`push_partial`/`push_trade` queues.

There are 7 `tk.Toplevel` dialogs (deploy/session picker `:5516`, order confirm `:7594`, AI settings `:2555`, update progress `:2940`, Pine export `:2381`, source viewer `:3073`, tooltip helper `:877`) and **25 modal `messagebox` call sites**.

### What works (keep these properties)

- **The COM threading discipline is correct.** COM callbacks only `queue.put_nowait`; two queues (`_tick_queue`, `_ui_queue`, `:674–683`) are drained on the Tk thread by `root.after` pumps. No cross-thread widget access.
- **The headless seam is a real architectural asset.** `HeadlessBotApp(BacktestApp)` on a withdrawn root (`src/live/headless_app.py`) runs the live glue byte-identical to the GUI; every human prompt goes through `_confirm(title, msg)`/`_alert(title, msg)` (`:5895–5906`), answered headlessly by title (`src/live/headless.py:41–68`, fail-closed). Enforced by `tests/test_bot_cli.py:80–82`.
- **Two surfaces were already modernized in the right pattern**: the Report tab (pure view model `src/backtest/report_view.py` → dumb renderer) and the Regime tab (`src/news/vote_status.py`, `src/regime/episodes.py` → renderer). This *view-model-first* pattern is the template for everything below.
- **A Tk-free event surface already exists**: `LiveRunner.on(event, handler)` (`src/live/live_runner.py:830–840`) emits `on_status`, `on_bar`, `on_decision`, `on_tick_exit`, `on_daily_report`, with exception isolation per subscriber. The GUI subscribes at exactly one splice point (`run_backtest.py:6352–6355`), wrapping each handler in `root.after(0, …)`.
- **A JSON-ready read model already exists**: `src/live/headless.py` — `BotInfo.as_dict()` (`:281–299`), `list_bots()`, `bots_as_json()` (`:410`), `tail_lines()`, `read_lock()` (explicitly non-destructive). `scripts/monitor/common.py` is a second, richer read layer with health verdicts and a `redact()` helper.
- Trades tab sorting, Ctrl-C copy, real-vs-sim price `--` sentinels, bilingual labels — the *information design* is largely right; the delivery is what's dated.

### What doesn't work — concrete pain points

**Visual / structural**

1. **No theme exists at all.** Zero `ttk.Style` usage in the repo. Windows default `vista` theme + ~40 hardcoded hex literals. The window is a light/dark *hybrid*: dark chat (`#1e1e1e`) and live log (`#1a1a2e`) sit next to white LabelFrames and a white Log tab. Only color constant: `_TONE_COLORS` (`:3634`).
2. **~50/50 raw-`tk` vs `ttk` widget mix** (~186 raw tk widgets). Raw `tk` was used wherever a color was needed — the direct consequence of having no `ttk.Style`. This is the single structural blocker to theming.
3. **Status feedback is one gray line.** `status_var` (`:1739–1741`) has ~90 call sites; "已連線 Connected", "圖表錯誤 Chart error", and progress messages all render identically as 9pt gray text. No color, no severity, no history.
4. Hand-rolled infrastructure duplicated: the Canvas+Scrollbar+inner-Frame+mousewheel scroll rig appears 3× (`:1806–1821`, `:5529–5557`); the toolbar has a manual reflow layout; the chat panel deletes "Thinking…" lines by *string search* (`_remove_last_system_line`, `:2161`).
5. Hardcoded fonts (16 distinct tuples, 3 families), no DPI awareness.

**Blocking / freezing**

6. **`_fetch_tradingview_live` (`:3436–3442`) blocks the entire main thread** on a network download (with a `root.update()` right before — dispatching queued input mid-handler).
7. **Deploy pre-check spins the event loop manually** (`:6072–6078`): `while … root.update_idletasks(); self._drain_ui_queue(); time.sleep(0.1)` for up to 5 s on the Tk thread.
8. `reload_1m_bars` pumps `update_idletasks` with a documented re-entrancy landmine (`:6544–6564`).
9. **All tick processing runs on the Tk thread**: `_on_com_tick` (`:7053–7250`) does classification + BarBuilder + strategy dispatch, up to 500 ticks/frame. Strategy `on_bar` cost lands directly on UI latency.
10. Every live trade close triggers a **full destroy-and-rebuild** of the Report tab and a full clear+reinsert of the trades Treeview.
11. 25 modal messageboxes; the deploy dialog blocks on `wait_window`. (The order-confirm dialog is *deliberately* non-modal with a 10 s countdown — that's correct and must be preserved.)

**Missing surfaces**

12. **No daily-report viewer.** `_on_live_daily_report` (`:7335`) writes one log line and posts to Discord; the user reads the JSON on disk or reads Discord. `data/daily-reports` appears in the GUI exactly once, inside a log string.
13. **No evolution view.** All evolution output is green `system`-tagged lines in the chat transcript; a PASS's only persistent GUI trace is a new entry in the strategy dropdown.
14. **The Log tab is nearly useless** and `_log_debug` output (`[CONN]`/`[RECONNECT]`/`[TICKS]`/`[WATCHDOG]`) never reaches any widget — users open `debug_YYYYMMDD.log` in a text editor to diagnose anything. Worse, `_log()` lines from non-main threads are silently dropped from the widget (`:711`).
15. **No remote/mobile visibility.** Discord is the de-facto remote dashboard: order/fill/report/regime/evolution events are pushed there via ~45 hard-wired call sites in `run_backtest.py` (module-global `_discord`, `:642`, constructed `:6188–6198`).
16. The 350-line deploy dialog (`:5496–5843`) is procedural, returns a positional **10-tuple**, and mixes load/create/mode/risk/regime/news concerns in one scrolling window.
17. Regime tuning (14 `RegimeConfig` knobs) is settings.yaml-only, and evolved-param overlays (`regime_evolved_params.json`, `config_loader.py:79–88`) are **invisible** — the bot may be running different ADX thresholds than settings.yaml says, with no UI hint.

---

## 2. Design principles

What "fluid and user-friendly" means for *this* app — a real-money trading bot that runs unattended for weeks:

1. **Glanceable state, always.** The #1 question is "is my bot alive and what's my position?" That answer must be visible in <1 second, from across the room, and from a phone. Green/amber/red status hierarchy: 🟢 healthy (ticks flowing, position known), 🟡 degraded (reconnecting, stale votes, pending fill), 🔴 action needed (disconnected, loss limit, fill timeout).
2. **The UI never blocks the trading loop.** The Tk thread drains COM ticks; anything that can stall it (network fetches, modal dialogs during market hours, full-widget rebuilds) is a trading risk, not just a UX flaw. Non-blocking is a *safety* property here.
3. **Push, don't poll-freeze.** Live data flows through the existing `LiveRunner.on()` events; renderers apply deltas (insert one Treeview row / append one DOM row), never rebuild whole surfaces per event.
4. **Read paths are free; write paths are guarded.** Viewing status, reports, logs, regime history must never require confirmation or risk mutation (the `read_lock` non-destructive contract in `headless.py:314` generalizes to the whole UI). Deploy/stop/mode-switch/manual orders keep explicit, deliberate confirmation — through the `_confirm`/`_alert` seam so headless parity survives.
5. **One design language, bilingual by design.** 中文 English dual labels are a product feature (keep them), but they belong in one label module, not 200 inline literals. One palette, dark-first (the chat, live log and charts are already dark; the market runs at night).
6. **Discord and the UI are views of the same event stream.** Anything worth a Discord push is worth a dashboard row, and vice versa. That implies extracting the ~45 direct `_discord.*` calls into a small notifier bus both subscribe to.
7. **Don't destabilize what's proven.** The live glue survived issues #43/#44/#45/#47/#50/#92/#98. Modernization must be additive/extractive — the byte-identical-headless property and the COM queue discipline are non-negotiable invariants.

---

## 3. Technology options

### Option A — Tkinter + CustomTkinter

Swap widgets for `customtkinter` equivalents: dark theme, rounded corners, stays pure Python.

- **For:** No new runtime; smallest conceptual change; PyInstaller-friendly.
- **Against — and these are disqualifying for a *full* migration:**
  - It is **not** drop-in. `CTkButton`/`CTkFrame` are different classes with different options; ~380 widget instantiations (186 raw tk + ~197 ttk) would each be touched, inside an 8.7k-line file where UI code interleaves with live-trading glue. High-churn, low-leverage.
  - **No CTk Treeview, Notebook, or PanedWindow.** The app's core surfaces (trades table, regime log, 5-tab notebook, split panes) stay ttk anyway, so you end up theming ttk regardless — at which point CustomTkinter added little.
  - Ceiling: still Tk. No web embeds, no mobile, no rich charts in-window.
- **Verdict:** Reject as a migration target. **But** the underlying idea — a real `ttk` theme — is the correct Phase 1 win *without* CustomTkinter.
- **Revision (2026-09-19, after PR #136):** the first implementation recolored the stock `clam` theme, as this plan originally suggested. It did not look good, for two reasons recoloring cannot fix: (1) `clam`'s elements are hard 1-px rectangles — buttons, fields, tabs, scrollbar arrows and check glyphs keep their 2005 geometry whatever the palette; (2) the process was DPI-unaware, so on a scaled display (the owner's 4K panel runs at 175 %) Windows rendered the window at 96 DPI and bitmap-stretched it — everything was blurry. The shipped approach is therefore **an image-based ttk theme drawn at runtime at the display's real DPI scale** (`src/ui/raster.py` + `src/ui/theme.py`): anti-aliased 9-slice rounded chrome, pill scrollbars, underline tabs, and `SetProcessDpiAwareness` before any window exists. It is pure Python — no Pillow, no bitmap assets, no new dependency in the PyInstaller bundle — and headless bots skip it entirely (`init_theme(minimal=True)`). `sv-ttk` was rejected because its fixed-size bitmap elements do not scale with DPI, which is exactly the failure being fixed.

### Option B — PyQt6 / PySide6

- **For:** Genuinely rich native widgets (QTableView with models beats Treeview outright), signal/slot maps 1:1 onto the existing event flow, QWebEngineView could host the charts in-window, excellent HiDPI.
- **Against:**
  - **It's a rewrite of the monolith, not a port.** The live glue (~3k lines: tick drain, watchdog, reconnect ladder, fill polling, session-end close) is interwoven with Tk idioms (`root.after` timers ×85, two Tk-thread queue pumps, the withdrawn-root headless trick). Re-plumbing that onto QTimer/QThread means re-validating every incident fix (#43–#98) — exactly the risk principle 7 forbids.
  - The headless CLI (`HeadlessBotApp` on a withdrawn Tk root) and the `_confirm` seam contract would need a Qt equivalent; the Tk mainloop also currently doubles as the **COM message pump** (`main()`, `:8740–8746`) — Qt has its own COM/STA story that would need proving against SKCOM.
  - +100 MB of Qt binaries in the onedir bundle; another framework for a Python-first team of one to master.
  - And after all that: still a desktop-only window. No mobile. The stated goal (fluid + phone-viewable) isn't reached.
- **Verdict:** Reject. Cost is rewrite-sized; payoff is a nicer desktop app, which is not the bottleneck.

### Option C — Embedded web UI (FastAPI + browser dashboard)

The bot process (or a sidecar) serves a local HTTP API + static dashboard; any browser — including the phone — renders it.

- **For:**
  - **The stack is already shipped.** Since commit `0bd146b` (issue #97), the frozen EXE renders the k-chart via **pywebview's local HTTP server inside WebView2** (`README.md:207–211`, `src/backtest/chart.py:107–146`). Users already install the WebView2 Runtime; `httpx` is already bundled; JS assets are already packaged via the `lightweight_charts/js` datas precedent (`tai_backtest.spec:36–42`). This is an *incremental use of an existing dependency class*, not a new one.
  - **The backend halves already exist**: `LiveRunner.on()` for push, `headless.py` `bots_as_json()`/`tail_lines()` + `scripts/monitor/common.py` (verdicts, `redact()`) for pull. A read-only dashboard is mostly transport, not new logic.
  - Highest ceiling: real charts (lightweight-charts is *itself* a browser library — used natively instead of through pywebview windows), regime timelines, mobile, multi-bot overview, zero-install viewing from any device on the LAN/Tailscale.
  - **Risk-gradable**: a read-only sidecar process that only reads disk state cannot destabilize a live bot at all. Write actions can reuse the proven headless machinery (`spawn_detached`, STOP file) instead of poking the Tk app.
- **Against (honest):**
  - Most new surface area: an HTTP API, a frontend, auth for LAN exposure, a `dashboard:` settings section (greenfield — none exists today), spec `hiddenimports` care (FastAPI/uvicorn import dynamically → `collect_submodules`, the exact issue-#58 class of bug), and extending `build_release.py`'s `verify_bundle_matches_source()` (currently `src/`-only, `:77–106`) to cover web assets.
  - A JS build toolchain would be a real burden for a Python-first team — so the frontend must be deliberately chosen to avoid one (see §4).
  - Two UIs coexist for a while (Tk workbench + web dashboard). That's a feature (risk gradient) but also a maintenance tax until Tk surfaces are retired.
- **Verdict:** **Recommended** — as the *monitoring/dashboard* layer first, not a big-bang replacement. Details in §4.

### Option D — Tauri / Electron wrapper

- **For:** Native-feeling window around a web frontend; Tauri uses the same WebView2 already required.
- **Against:** Adds a Rust (Tauri) or Node (Electron) toolchain and a second packaged runtime alongside the PyInstaller EXE, plus IPC between the wrapper and Python. Everything it provides — a native window hosting the web UI — **pywebview already provides and already ships in this EXE**. If a "native window" for the dashboard is ever wanted, `webview.create_window("tai-robot", "http://127.0.0.1:<port>")` is ~3 lines with zero new dependencies.
- **Verdict:** Reject. Strictly dominated by pywebview given what's already bundled.

---

## 4. Recommended approach

**Two-track: polish the Tk workbench in place (cheap, immediate), and build the web dashboard as the new monitoring surface (the actual "fluid" win) — converging on the web UI as the primary live-monitoring interface while Tk remains the desktop workbench for backtesting/AI until it's optionally retired.**

Rationale, tied to the constraints:

1. **The bot is a frozen Windows EXE and the team is Python-first** → no Qt rewrite, no Node-required frontend. Backend: **FastAPI + uvicorn** (typed, WebSocket support, team-familiar Python idioms), with the spec/`collect_submodules` cost paid once and documented. Frontend: **no-build-step stack** — a single-page dashboard using [htmx or Alpine.js] + the standalone `lightweight-charts` UMD bundle, served as static files checked into `web/static/`. No npm, no bundler, no lockfile. (If the UI later outgrows this, a Vite/Vue upgrade is additive — the API doesn't change.)
2. **The EXE already proves localhost HTTP + WebView2 works in the frozen build** (issue #97 fix) → the riskiest unknown of Option C is already retired in production.
3. **"Fluid" is primarily a live-monitoring problem** (freezes, gray status line, Discord-as-dashboard, no phone view). The web dashboard attacks exactly that. The backtest/AI workbench is *functionally* fine and only needs the Phase 1 theme + non-blocking fixes.
4. **Safety gradient**: Phase 3a's dashboard is a **separate read-only process** (`python -m src.webui` / `tai_dashboard.exe`) that reads bot dirs the same way `run_bot_cli.py list|status` does — it cannot crash, block, or mutate a live bot. In-process event streaming (3b) comes only after the read-only tier is proven. Write actions (deploy/stop) go through the already-hardened headless functions, never through Tk.
5. **The Discord problem and the dashboard problem are the same problem.** Extracting the ~45 `_discord.*` call sites into a `NotifierBus` (Phase 2) gives Discord, the web UI, and the Tk Live log identical event feeds — `src/regime/manager.py` and `src/evolution/notify.py` already use exactly this callback-not-transport pattern, so this extends an existing convention.

**Explicit non-goals:** no Qt; no Electron/Tauri; no rewrite of the live glue for its own sake; no removal of the Tk GUI until the web dashboard has demonstrably covered live monitoring for multiple weeks; the headless CLI contract (`_confirm` titles, `HEADLESS_MODES`, STOP file) is preserved verbatim throughout.

---

## 5. Phased implementation plan

### Phase 1 — Quick wins in Tk (small PRs, no architecture change)

Goal: kill the worst visual and blocking problems without touching live logic.

1. **`src/ui/theme.py`** (+ `src/ui/raster.py`) — single module owning: the image-based `tairobot` ttk theme (see the Option A revision note: runtime-rasterized, DPI-scaled rounded chrome — not a recolored stock theme), the palette tokens from §7, named styles (`Card.TFrame`, `Status.Ok.TLabel`, `Status.Warn.TLabel`, `Status.Err.TLabel`, `Tone.Good/Bad`), and font objects (with `tk.call('tk', 'scaling', …)` DPI handling). Retire `_TONE_COLORS` and the ~40 inline hex literals into it. Convert the raw-`tk`-for-color widgets (the 186) to styled ttk *mechanically, surface by surface* — chat panel, control panel, then tabs. `HeadlessBotApp` check: theme init must run fine on a withdrawn root (it does — it's all Tcl option setting; add a test that constructs `HeadlessBotApp` with the theme active, alongside `tests/test_bot_cli.py`).
2. **Status bar → status strip** (replaces `:1739–1741`): move to the window bottom; three zones — connection dot + text (🟢/🟡/🔴 driven by the existing conn events in `_drain_ui_queue` `:978–983`), transient task text (current `status_var` content), and a click-to-open last-20-messages popover (backed by a deque; solves "the message vanished before I read it"). Severity via named styles, not `foreground=`. Introduce `set_status(msg, level="info")` and migrate the ~90 `status_var.set` call sites with default level `info` (mechanical), upgrading error sites (`Chart error`, reconnect failures, order failures) to `warn`/`error` as found.
3. **Un-block the main thread** (each an isolated fix):
   - `_fetch_tradingview_live` (`:3436`): move `fetch_tv_dataframe` to a worker thread + `root.after(0, …)` completion, same idiom as `_backtest_worker` (`:3585`). Delete the `root.update()`.
   - Deploy OI wait (`:6072–6078`): replace the sleep-spin with an `after(100, poll)` chain that re-enters `_deploy_live_from`'s continuation, or keep it synchronous but move it before widget lock-in with a progress label. (Keep the 5 s `OI_SNAPSHOT_TIMEOUT_S` semantics identical — it feeds the Existing-Position confirm.)
   - Trades tab: incremental insert (`_display_results` `:3814` keeps a rendered-count watermark; on live updates only insert new trades, re-sort only if a sort is active). Report tab: only re-render cards whose values changed, or cheapest viable: throttle `_render_report_view` to ≥1 s coalescing.
4. **Consolidate hand-rolled UI helpers** into `src/ui/widgets.py`: one `ScrollableFrame` (replaces the 3 copies of the Canvas rig, with correct `<Enter>`/`<Leave>` wheel unbinding), `_attach_tooltip` moved and styled, a `TagTextLog` wrapper (read-only ScrolledText + tag palette + max-line cap) used by chat display, live log, and Log tab.
5. **Log tab minimum viability**: line cap (e.g. 5,000), level filter (All / Info / Debug), search-as-you-type highlight, pause-scroll toggle, and — the big one — route `_log_debug` into the widget behind the Debug filter *and* fix the dropped non-main-thread `_log` lines by routing them through `_ui_queue` (the queue exists; `:711` just doesn't use it).
6. Small dead-code cleanups while touching each surface: `_on_strategy_changed` pass-hook (`:3006`), `_on_live_bar` stale comment (`:7297`), `_remove_last_system_line` string-search → mark ranges with a Tk text mark when inserting transient lines.

Deliverable: same app, dark, coherent, status you can read at a glance, and no known main-thread stall during market hours. All changes PR-sized and individually revertable.

### Phase 2 — Core redesign of the Tk workbench + event-bus extraction

Goal: fix the structural UX debts and create the seam the web UI will plug into.

1. **`NotifierBus` extraction** (the load-bearing item): a small pub/sub in `src/live/notifier_bus.py` with typed topics mirroring `DiscordNotifier`'s methods (`order_sent`, `fill_confirmed`, `fill_price_correction`, `daily_report`, `regime_swap`, `mode_switched`, `evolution_verdict`, `bot_stopped`, …). `DiscordNotifier` becomes one subscriber; the Tk Live log becomes another; the ~45 `_discord.*` call sites in `run_backtest.py` become `bus.emit(...)`. Semantics must be preserved exactly (including the manual-vs-auto evolution Discord asymmetry at `:5420` — encode it as an event attribute, let the Discord subscriber filter). This is refactor-only: byte-identical Discord output is the acceptance test.
2. **Deploy dialog rebuild** (see §6.2): 2-step flow, returns a `DeployRequest` directly (killing the 10-tuple; `_DIALOG_FIELDS` in `src/live/deploy_request.py` already defines the shape). All prompts stay on the `_confirm` seam; no new titles without registering them in `src/live/headless.py`.
3. **Daily-report viewer**: new content in the Report tab — a date/bot picker over `list_reports()` (`src/daily_report/report_generator.py:362`) rendering the JSON through the existing card renderer, including the `per_strategy` and `regime_switching` sections. Removes "open the JSON in an editor" from the daily loop.
4. **Evolution results panel** (see §6.5): persist pipeline runs to `data/evolution/runs/{date}.json` at the stages that today only emit chat lines (`:5162–5439`), and render a run-history list + verdict detail view. Chat keeps the live progress feed; the panel is the durable record.
5. **Regime config transparency** (see §6.3): read-only "effective config" view — `RegimeConfig` as constructed (`build_regime_config`), with evolved-overlay values visually marked (source: `regime_evolved_params.json`, age). Editing stays in settings.yaml this phase; the win is *seeing the truth*.
6. **Log viewer completion**: per-bot debug-log file viewer (reuse `tail_lines`) with follow mode — the in-GUI answer to "open debug_YYYYMMDD.log in Notepad".

### Phase 3 — Web dashboard (the mobile/fluid win)

**3a — Read-only sidecar (zero risk to live bots).**

- New package `src/webui/`: FastAPI app + `web/static/` frontend. Runs as a separate process: `python -m src.webui` (dev) / built into the onedir bundle with a launcher (`tai_dashboard.exe` or a `--dashboard` flag on the CLI). Binds `127.0.0.1:8410` by default; `settings.yaml` gains a greenfield `dashboard:` section (`enabled`, `host`, `port`, `token`). Non-localhost bind requires a bearer token; settings rendered through `redact()` (`scripts/monitor/common.py:141`) — settings.yaml holds live credentials including a Discord webhook.
- Endpoints (all straight wrappers over existing code):
  - `GET /api/bots` → `bots_as_json()` (`headless.py:410`)
  - `GET /api/bots/{name}` → `bot_info()` + `session.json` broker subset
  - `GET /api/bots/{name}/decisions?limit=` → `decisions.csv` tail
  - `GET /api/bots/{name}/log?file=&lines=` → `tail_lines` over debug logs
  - `GET /api/bots/{name}/regime` → `regime_state.json` + `regime_history.csv` (reuse `src/regime/episodes.py` shaping)
  - `GET /api/reports?date=&bot=` → `load_report`/`list_reports`
  - `GET /api/votes` → vote files via the same resolution as `_regime_vote_paths` (`run_backtest.py:3905`) refactored into `src/news/vote_status.py`
  - `GET /api/health` → the `scripts/monitor` check verdicts (import, don't shell out)
- Frontend: one dark dashboard page (§6.1 "web" column) polling `/api/bots` every 5 s — plenty for 30 s-cadence data — plus per-bot detail pages. Phone-usable layout from day one (this is mostly "cards stack vertically").
- Packaging: `web/static` added to spec `datas` (the `lightweight_charts/js` precedent); `fastapi`/`uvicorn` via `collect_submodules`; extend `SOURCE_PATHS` + `verify_bundle_matches_source()` in `build_release.py` to cover `web/` (the guard is currently `src/`-only — shipping unverified frontend assets would recreate the v2.13.0 stale-EXE failure mode for the dashboard).

**3b — Live event streaming (in-process).**

- Add a `webui` subscriber to the Phase 2 `NotifierBus` + the `LiveRunner.on()` events inside the bot process, forwarding to the dashboard over a local channel. Two viable transports, pick after a spike: (i) the bot process itself hosts a `/ws` WebSocket (uvicorn on a daemon thread, events crossing via `queue.Queue` → asyncio `loop.call_soon_threadsafe`) or (ii) the bot appends to an `events.ndjson` in the bot dir and the sidecar tails it (crash-safe, transport-free, replayable — likely the better fit for this codebase's file-centric style; it also gives the Tk Live log a persistence upgrade for free).
- Dashboard goes from 5 s polling to live ticking status, fill events as toasts, and the 🟢/🟡/🔴 header driven by tick freshness (`READY_MARKER` / debug-log age / watchdog state).

**3c — Guarded write actions.**

- `POST /api/bots/{name}/stop` → write the STOP file (`headless.py:161–175`) — already the sanctioned control channel.
- `POST /api/deploy` → `build_deploy_request` + `spawn_detached` + `wait_for_ready` (`headless.py:215–276`) — i.e., the dashboard deploys exactly like `run_bot_cli.py deploy --detach`, inheriting `HEADLESS_MODES` (`paper`/`auto` only; `semi_auto` stays GUI-only because it *requires* the interactive order-confirm dialog) and the fail-closed `HeadlessPolicy`. Confirmation UX in the browser mirrors the `_confirm` titles.
- Auth mandatory for these routes even on localhost (token header); every action logged to the bot dir.

### Phase 4 — Advanced surfaces

1. **In-dashboard charts**: lightweight-charts UMD directly in the browser page, fed by `GET /api/bots/{name}/bars` (CSV-backed) + the 3b event stream for partial-bar updates — replacing the separate pywebview chart windows for live monitoring (backtest charts can stay pywebview). Apply the `_ensure_ascending` sanitization server-side (issue #97's blank-pane trap applies identically in the browser).
2. **Regime visualization**: episode timeline (colored bands per leg, swap markers, vote annotations, news-suppression spans) rendered from `regime_history.csv` + `NEWS_SUPPRESSED` audit rows — the visual answer to "why was the bot short all week?" (the 08-17→08-28 class of question).
3. **Evolution timeline**: runs from Phase 2's `data/evolution/runs/` as a vertical timeline — per Saturday: data window, plan summary, candidate, verdict block, A/B or walk-forward numbers; PASS candidates link to their strategy source.
4. **Multi-bot fleet header**: all bots across symbols on one row of cards (the monitor suite's verdicts inline) — the "one glance, whole system" view.
5. Optional: workbench features (AI chat, backtest launch) in the web UI — only if by then the Tk workbench is the last reason to sit at the desktop; explicitly a decision for later, not a commitment.

---

## 6. Specific screens to redesign

### 6.1 Main window / dashboard

- **Current**: chat pane + control panel + 5-tab notebook (§1). Live status = 5 monospace StringVars + gray status line; connection state is inferable only from button enablement.
- **Problems**: no visual hierarchy (a P&L number and a label have near-equal weight); status severity invisible; hybrid light/dark; toolbar of 12 undifferentiated buttons; the most important state (connection, position, P&L) has the least visual priority.
- **Redesign (Tk, Phases 1–2)**: dark theme throughout; bottom status strip with connection dot (🟢🟡🔴) + last-message popover; Live tab promoted to a card layout — big Position/P&L cards with tone colors, State/Bars/Market as secondary chips, regime line as a colored badge row; toolbar grouped (Backtest | Live | AI | App) with separators.
- **Redesign (web, Phase 3)**: the dashboard *leads* with the fleet: one card per bot — name, mode badge, 🟢🟡🔴 health, position, session P&L sparkline, last-event time; click-through to the detail page (status, decisions tail, log follow, regime strip, chart in Phase 4). Phone layout = stacked cards.

### 6.2 Deploy dialog (`_show_bot_session_dialog`, `:5496–5843`)

- **Current**: one 680x600 scrolling modal mixing an existing-bot table, create-new row, 3 mode radios, loss-limit entry, and a regime section that expands inline with nested news checkboxes; returns a positional 10-tuple; up to 3 sequential confirms behind it.
- **Problems**: two workflows (resume vs create) share one cramped window; the regime/news reveal makes the dialog jump; mode radios encode a real-money decision with the same visual weight as a name field; 10-tuple is fragile; blocking modal.
- **Redesign (Phase 2)**: two-step flow. **Step 1 — pick**: existing-bot list as cards (name, strategy, mode, P&L, last-saved, *running* badge) + "New bot" card. **Step 2 — configure**: name; mode as three large option cards with explicit risk copy (paper = gray, semi-auto = amber, auto = red + typed-confirmation "AUTO" for real accounts); loss limit; regime section as a proper sub-panel (enable → leg pickers + news flags, using `validate_leg_strategies` inline before OK, not as a post-hoc `_alert`). Returns `DeployRequest`. Post-dialog confirms unchanged in title and semantics (`策略更換 Strategy Change`, `帳戶有持倉 Existing Position`) — seam contract intact. Web (3c) reuses step 2's layout minus semi-auto.

### 6.3 Regime mode config + status

- **Current**: deploy dialog sets `enabled`+legs only; 14 tuning knobs live in settings.yaml; evolved overlay invisible; status = one `#8ab4f8` line + the Regime tab (cards, votes grid, episode Treeview — already decent).
- **Problems**: no single place shows the *effective* config; overlay opacity (a bot can trade on evolved ADX thresholds the user can't see); the status line packs pending/suppression into one string.
- **Redesign**: Phase 2 adds an "Effective Config" panel to the Regime tab: every `RegimeConfig` field with value + source badge (`settings` / `evolved (age 34d)` / `default`), plus the three behaviour knobs (`pause_freezes_exits` etc.) with their one-line meaning. Status line becomes badges: leg (LONG green / SHORT red / IDLE gray), pending ⏳, suppression 🚫 with reason tooltip. Phase 4's episode timeline supersedes the Treeview as the primary history view (Treeview stays as the data grid).

### 6.4 Report / performance tab

- **Current**: card renderer over `report_view.py` (good bones), All/Real filter, per-strategy Treeview; full rebuild per update; **no daily-report viewer** (§1 pain 12).
- **Problems**: rebuild churn on every live trade close; the daily JSON — the thing the user actually reviews each morning — is only in Discord/disk; no equity-curve visual anywhere in the GUI despite `equity_curve` sitting in session.json.
- **Redesign**: Phase 1 fixes rebuild churn (diff or throttle). Phase 2 adds the report browser (date × bot over `list_reports()`), rendering `per_strategy` and `regime_switching` sections with the same cards. Phase 3 serves the identical view model as JSON (`/api/reports`) — the web report page and Tk tab render the same shape, keeping one source of truth. Phase 4 adds the equity-curve chart (browser).

### 6.5 Evolution results view

- **Current**: no view. Stage messages + verdict blocks as green text in the chat transcript (`:5117–5439`); PASS silently registers `AI: X` in the strategy dropdown; changelog row on disk with no reader.
- **Problems**: results are ephemeral (chat scrollback), invisible when headless (chat is Tk-only — the exact reason the monitor suite has to forensically reconstruct evolution runs from ai_usage.csv and watermarks); no history ("has evolution *ever* passed?" required an audit).
- **Redesign**: Phase 2 persists each run — `data/evolution/runs/{ISO-week}.json`: trigger (manual/auto), data window, plan digest, candidate name, stage reached, verdict block, metrics — written at the existing stage sites; a new Evolution panel (sub-tab of Report or its own tab) lists runs with PASS/FAIL/SKIP badges and a detail view. Chat keeps live progress. Phase 4 renders the same files as a timeline in the web UI. This also hands the weekly-review skill structured evidence instead of watermark archaeology.

### 6.6 Log viewer

- **Current**: bare ScrolledText Log tab; debug diagnostics never shown; non-main-thread lines dropped; dark Live-tab event log with 4 tags; real triage happens in a text editor on `debug_YYYYMMDD.log`.
- **Problems**: no filter/search/cap; two disconnected log surfaces; the richest signal (`[RECONNECT]`, `[WATCHDOG]`) invisible.
- **Redesign**: Phase 1 = `TagTextLog` (cap, level filter incl. Debug, search, pause-follow) + queue-route all producer threads. Phase 2 = file viewer over `tail_lines` with follow-mode for any bot's debug log. Phase 3 = `/api/bots/{name}/log` + a browser log-follow page (phone-readable incident triage). Severity coloring by prefix (`[WATCHDOG]`/`[RECONNECT]` amber, tracebacks red) — reuse the signature list from the `log-scan` skill.

---

## 7. Component library / design system

One module owns all of this: `src/ui/theme.py` (Tk) and `web/static/theme.css` (web), generated from the same token table (checked in as `docs/ui-tokens.md` or a small YAML; hand-sync is acceptable at this scale, but keep the token *names* identical).

**Palette (dark-first).** Derived from the surfaces users already accept (chat `#1e1e1e`, chart background, live log):

| Token | Value | Use |
|---|---|---|
| `bg` | `#16161a` | window background |
| `bg-raised` | `#1e1e24` | cards, panels |
| `bg-inset` | `#121216` | text/log/chart wells |
| `border` | `#2e2e36` | card & panel borders |
| `text` | `#d6d6dc` | primary text |
| `text-dim` | `#8a8a94` | labels, secondary |
| `accent` | `#4f8cff` | interactive, links, active tab |
| `ok` | `#2ecc8f` | 🟢 healthy, wins, LONG |
| `warn` | `#f0b429` | 🟡 degraded, pending, semi-auto |
| `err` | `#ef5350` | 🔴 errors, losses, SHORT, auto-mode risk |
| `info` | `#8ab4f8` | regime/status accents (existing) |
| `up` / `down` | `#26a69a` / `#ef5350` | candles & P&L (matches chart.py today) |

Rules: **status colors are reserved for status** (a green *button* is banned; green means "healthy/profit" only); LONG/SHORT always ok/err-tinted badges, never bare text; every color pairs ≥4.5:1 contrast on its background.

**Typography.** UI: `Segoe UI` 10 (the two modern surfaces already use it) — CJK falls back to Microsoft JhengHei automatically on Win11, verify mixed-string metrics once. Numeric/log: `Consolas` (keep; tabular alignment matters more than fashion). Scale: 16 bold (page title) / 13 bold (section) / 11 bold (card value: P&L, position) / 10 (body) / 9 (secondary, log). Kill the 16 ad-hoc font tuples for four named fonts: `font_title`, `font_section`, `font_value`, `font_body`, `font_mono`.

**Spacing.** 4 px base unit; 8 px intra-card padding; 12 px between cards; 16 px section gutters. (Today: a mix of `padx=4/5/10` — pick the scale and apply mechanically while converting widgets.)

**Components** (each exists once, both stacks):

- **StatusDot** — 🟢🟡🔴 + label; drives connection, bot health, vote liveness (replaces the ad-hoc `●` labels at `:3947–3955`).
- **Card** — raised panel, label / value / sub, optional tone (formalizes `_report_card` `:3636` and the regime cards).
- **Badge** — small colored chip: mode (`模擬 Paper` gray / `半自動 Semi` amber / `自動 Auto` red), leg, `⏳ pending`, `🚫 suppressed`.
- **TagTextLog** — capped, filterable log pane (§6.6).
- **ScrollableFrame** (Tk-only) — the one blessed scroll rig.
- **DataTable** — Treeview (Tk) / `<table>` (web) with the shared conventions: sortable headers, numeric right-aligned, `+`-signed thousands-separated P&L, `--` sentinel for missing real prices, win/loss row tinting.
- **ConfirmPanel** — the deliberate-action pattern: message + risk copy + typed confirmation for real-money scopes; Tk implementation routes through `_confirm` (seam-safe), web implementation mirrors the same titles.

**Bilingual labels**: move to `src/ui/labels.py` as constants (`DEPLOY = "部署機器人 Deploy Bot"`); both stacks import the same strings (web via a generated `labels.json`). No translation framework — just one place instead of 200 literals.

---

## 8. Implementation notes

### Keeping the EXE build working

- Spec: onedir `COLLECT` (`tai_backtest.spec:132`) — new static assets are plain files under `_internal/`, servable from disk; no `sys._MEIPASS` extraction issues. Add `('web/static', 'web/static')` to `datas` next to the `lightweight_charts/js` entry.
- **Every new dynamically-imported module must be declared**: FastAPI/uvicorn/starlette need `collect_submodules` (and uvicorn's `collect_data_files` for its logging config) — this repo has already been bitten by exactly this (issue #58 hang; memory: pyinstaller-dynamic-imports). Add a smoke test that boots the server inside the frozen build in CI-adjacent checks.
- **Extend the integrity guard**: `build_release.py` `SOURCE_PATHS` (`:39`) and `verify_bundle_matches_source()` (`:77–106`) currently cover only `src/`, `run_backtest.py`, `version.py`. Add `web/` when Phase 3 lands, or the frontend can silently ship stale (v2.13.0 failure mode).
- `pyproject.toml` says `requires-python >= 3.10` but the project is documented 3.13+ — bump while adding the new deps; there is no `requirements.txt`, and per the CI-workflow-deps rule, new pip deps must also be added to all three `.github/workflows/*.yml` install lines.
- Dashboard sidecar as an EXE: either a second PyInstaller entry in the same spec (shared `_internal/`) or a `--dashboard` mode on the existing CLI EXE — decide in the Phase 3a spike; the second-entry route keeps process isolation obvious.

### COM callbacks and the threading model

- **Nothing in this plan touches the COM rules**: callbacks enqueue-only (`:674–683`), Tk thread drains, the Tk mainloop remains the COM message pump (`main()`, `:8740`). The web server must live on daemon threads (uvicorn `Server.run` in a thread) or in the sidecar process; it never calls into COM or Tk.
- Event flow to the browser: producer threads → `queue.Queue` → the server's asyncio loop via `loop.call_soon_threadsafe` → WebSocket broadcast. Identical shape to the existing chart feeder (`chart.py:400–413, 527`) — this pattern is already proven in-process.
- If 3b chooses the `events.ndjson` tail route instead: the bot's `NotifierBus` gets a file-appending subscriber (append + flush, same as `decisions.csv` discipline at `csv_logger.py:103`), and no server thread enters the bot process at all — prefer this if the uvicorn-thread spike shows any interference with tick latency.
- The Tk-side renderers keep the `root.after(0, …)` marshalling idiom for bus subscriptions — the bus emits on whatever thread the producer runs on, mirroring `LiveRunner._emit`'s exception isolation so a broken UI subscriber can never kill trading.
- Strategy/tick work staying on the Tk thread (§1 pain 9) is *out of scope* for this plan — moving it is a live-machinery change with incident-class risk; the plan only removes the *other* sources of jank so the tick path has the thread to itself.

### Headless / CLI invariants (regression checklist for every phase)

- `HeadlessBotApp` must still construct every widget on a withdrawn root — any new widget must not require a mapped window (no `winfo_width()`-at-build patterns beyond the existing `_reflow_toolbar` guard).
- No new `messagebox.*` on the deploy/backtest path — new prompts go through `_confirm`/`_alert` with titles registered in `src/live/headless.py` (`TITLE_*` constants). `tests/test_bot_cli.py:80–82` enforces this; keep it green.
- `semi_auto` remains GUI-only; the order-confirm dialog remains **non-modal** with its countdown (`:7594–7658`) — do not "fix" its modality.
- STOP-file stop, `.lock` liveness, `READY_MARKER` stdout contract, and `read_lock`'s never-mutate rule are load-bearing for the CLI, the monitor suite, *and* the future dashboard — treat them as public API.

### Security

- Dashboard binds `127.0.0.1` by default; any other bind requires the `dashboard.token`. Write routes require the token always.
- Settings rendering goes through `redact()` — `settings.yaml` contains Discord tokens/webhooks and Capital credentials; channel/role IDs must never appear in served pages' source defaults (same rule as the repo's git hygiene constraint).
- No external exposure guidance beyond LAN/Tailscale; the dashboard is not designed for the public internet and the plan should say so in its README.

### Suggested sequencing & effort (single developer, honest ranges)

| Phase | Content | Effort |
|---|---|---|
| 1 | theme + status strip + un-blocking + log tab + helpers | 1.5–2.5 wks across ~6 PRs |
| 2 | NotifierBus + deploy dialog + report viewer + evolution persistence + regime config view | 2–3 wks |
| 3a | read-only sidecar + dashboard page + packaging | 1.5–2 wks |
| 3b/3c | event streaming + guarded actions | 1.5–2 wks |
| 4 | charts, regime timeline, evolution timeline, fleet view | incremental, per-feature |

Order within phases is chosen so every PR ships user-visible value and nothing depends on a big-bang merge. Phase 3a can start in parallel with late Phase 2 (it only needs `headless.py`, which is stable today).
