# v2.19.9

> 草稿 / DRAFT. Base: master `cad71dd9` (#194, #195 merged). #177 is **pending merge**; #172 awaits a merge-or-known-issue decision. No build, tag or publish yet.

## 修復與功能 Fixes & features

### 重連 Reconnect / 連線

- **#178**：`LinkStateMachine` 已接入實際重連路徑（#175 只加入、未接線）。重連不再重新登入（不再跑 LeaveMonitor → LoginSetQuote → ConnectByID → EnterMonitorLONG），程序啟動時的登入只跑一次。回報連線 READY 且 OnComplete 對帳完成前，封鎖新的實單進場；平倉與強制平倉不受限。進入 DEGRADED 時：狀態列、連線燈號、Discord 通知、heartbeat `link` 快照、封鎖進場、每 60 秒 `IsConnected` 探測；需手動按 Reconnect 或重啟程式恢復，不會自動重啟。
  **#178**: `LinkStateMachine` is now wired into the live reconnect path (#175 added it unwired). Reconnect no longer logs in again (no LeaveMonitor → LoginSetQuote → ConnectByID → EnterMonitorLONG); process-start login runs once. New real entries are blocked until the reply link is READY and OnComplete reconciles; TRADE_CLOSE / FORCE_CLOSE are not link-gated. DEGRADED shows on the status line, conn dot, Discord, heartbeat `link` snapshot, blocks entries, and probes `IsConnected` every 60 s. Recovery is the manual Reconnect button or an operator restart; no auto-restart.
- **#175**：新增 #157 P1 `LinkStateMachine` 與 fake SKCOM（重現 10-06、10-12、10-22 當機序列）；由 #178 接線。
  **#175**: adds the #157 P1 `LinkStateMachine` plus a fake SKCOM that faults on the 10-06 / 10-12 / 10-22 crash sequences; wired by #178.
- **#183**：緊急休市（如颱風）時，連續 3 次零 tick 重連後改為每 30 分鐘重試一次直到收到 tick 或下個時段；收到 tick 即恢復快速重連。可選的本機 `taifex_closures.yaml` 可列出休市日期，列出的日期不會重連。檔案不是有效 UTF-8／YAML 時視為無日期。
  **#183**: on an emergency TAIFEX closure (e.g. typhoon), after 3 consecutive zero-tick reconnects the watchdog slows to one retry every 30 min; a live tick restores the fast ladder. An optional local `taifex_closures.yaml` lists closure dates, which cost no reconnects. An invalid UTF-8/YAML file yields no dates.
- **#174**：還原 #170（BrokerPort）與 #158 的半連線 LeaveMonitor 保護，以便調查 #157。保留 #158 的診斷：faulthandler、`[RECONNECT] about to call …` 日誌、`[COM] {name} returned in N ms` 耗時；`SKCenterLib_LogOut` 已移除（SKCOM 2.13.57 無此方法）。
  **#174**: reverts #170 (BrokerPort) and the #158 half-up LeaveMonitor guard so #157 can be investigated. Keeps #158 diagnostics: faulthandler, `[RECONNECT] about to call …` logs, and `[COM] {name} returned in N ms` timings; `SKCenterLib_LogOut` is gone (not in SKCOM 2.13.57).
- **#158**：僅保留診斷日誌（其重連保護已被 #174 還原）。
  **#158**: logging/diagnostics only remain (its reconnect guard was reverted by #174).

### 實盤 Live

- **#171**：重啟後還原未完成的出場、停損價位與成交等待狀態，並重新啟動成交輪詢；空倉時丟棄殘留停損；實際部位方向不符時不確認成交、改為警示（#168）。
  **#171**: on resume, restore pending exits, stop levels and the in-flight fill gate, and restart the fill poller; drop leftover exits when flat; never confirm a resumed fill against an opposite/missing book — alert instead (#168).
- **#166**：Windows 上 `heartbeat.json` 取代／刪除遇 `PermissionError`（WinError 5）時以 5/10/20 ms 重試；`check_bots` 顯示 `busy` 而非 `corrupt`。
  **#166**: retry Windows `PermissionError` (WinError 5) on `heartbeat.json` replace/delete with 5/10/20 ms backoff; `check_bots` shows `busy` instead of `corrupt`.

### 監控 Monitor

- **#159**：偵測「卡住但仍存活」的 bot（Tk 主執行緒卡在 COM 呼叫）並警示；只偵測，不重啟、不修改狀態。
  **#159**: detect hung-but-alive bots (Tk main thread stuck in a COM call) and alert; detection only, no kill/restart.
- **#156**：台灣假日套件失效時，不再誤發漏分類 P1，改發一則 P2（`holiday calendar degraded`）。
  **#156**: when the TW holidays calendar is degraded, refuse the phantom missed-night P1 and emit one P2 instead.

### 新聞 News

- **#160**：RSS 評分不再傳送 Gemini `temperature`。 / **#160**: RSS scorer no longer sends Gemini `temperature`.
- **#163**：RSS 評分改用 `gemini-3.6-flash`。 / **#163**: RSS scorer now uses `gemini-3.6-flash`.

### 週末進化 Weekend evolution

- **#193**：週六自動進化預設改為 `saturday_mode: propose`：只寫提案檔，不提升 pool、不寫 StrategyStore、不改 bot；`promote` 為明確選用。新增每月結構通道 `structural_lane`（預設開啟、僅提案；每 bot 每月一個候選，DSR > 0.95，需在選擇折數勝過基準；PBO 僅記錄）。該月的結構週六取代當天的 knob grid。手動 Evolve 按鈕不變。
  **#193**: Saturday auto run now defaults to `saturday_mode: propose` — writes a proposal file only; no pool promotion, no StrategyStore write, no bot change. `promote` is an explicit opt-in. Adds a monthly `structural_lane` (on by default, propose-only; one candidate per bot per month, DSR > 0.95, must win the selection folds; PBO logged, not a pass condition). The month's structural Saturday replaces that day's knob grid. Manual Evolve unchanged.
- **#192**：週六 knob 搜尋預設 `search: grid_batch`：預先登錄網格（最多 48 組、最多 2 個 knob），以 DSR、PBO 與高原規則挑選；模型只解釋結果。holdout 不參與挑選。`llm_one` 為舊行為。同時 paper／backtest 晉升預設改為 `paper_gate: relative`。
  **#192**: Saturday knob search defaults to `search: grid_batch` — a pre-registered grid (≤48 settings, ≤2 knobs) picked by DSR, PBO and a plateau rule; the model only explains the winner; the holdout stays out of selection. `llm_one` keeps the old plan. Paper/backtest promotion now defaults to `paper_gate: relative`.
- **#188**：新增相對晉升門檻（配對折 bootstrap、60% 多數、效果量、≥6 非空折），僅適用 paper／backtest；實單維持絕對 PF 門檻；無效值退回 absolute 並警告。新增退役建議：只寫建議，不停用 bot。（預設值由 #192 改為 relative。）
  **#188**: relative promotion gate (paired-fold bootstrap, 60% majority, effect size, ≥6 non-empty folds) for paper/backtest only; real-money modes keep the absolute PF floor; invalid values fall back to absolute with a warning. Adds a recommend-only retirement check (never disables a bot). (Default flipped to relative by #192.)
- **#191**：每次進化嘗試（skip／no_change／refused／FAIL／PASS）寫一行到各 bot 目錄的 `evolution_history.jsonl`（只附加、無大小上限、不輪替）。同一 bot 已有進化在跑時，拒絕第二次（例如週六自動跑時按 Evolve）。Windows 寫入遇 PermissionError 會重試。
  **#191**: one line per evolution attempt (skip/no_change/refused/FAIL/PASS) appended to `evolution_history.jsonl` in each bot dir (append-only, unbounded, no rotation). A second run on the same bot is refused while one is active (e.g. Evolve during the Saturday auto run). Windows PermissionError is retried.
- **#190**：月門檻：最新月份若少於 12 個 TAIFEX 交易日，併入前一月（`latest_def: merge_stub`）；門檻 1 寬限改為一個標準誤，夾在 3000–15000 TWD（`slack.mode: scaled`），門檻 3 用一半。**會改變 PASS／FAIL 判定。** 可用 `max_exit_month`／`flat` 回退。
  **#190**: month gates — a newest month with <12 TAIFEX trading dates folds into the previous month (`latest_def: merge_stub`); gate 1 slack becomes one standard error clamped to 3000–15000 TWD (`slack.mode: scaled`), gate 3 uses half. **This changes verdicts.** Opt out with `max_exit_month` / `flat`.
- **#186**：每個類別的 knob 集合 = 主網格 ∩ 類別實際可改寫的 knob，至少 2 個；不足者排除於每週 codegen，每週六 Discord 顯示一行「excluded」。網格變更會重新檢查。
  **#186**: per-class knob sets (master grid ∩ the class's rewritable knobs, need ≥2); classes below that are excluded from weekly codegen with an "excluded" line every Saturday. A grid change triggers a recheck.
- **#185**：自動進化在設計窗交易少於 30 筆時跳過 AI 計畫呼叫，顯示 `waiting for sample: N/30`（不呼叫 AI、watermark 不變）；手動 Evolve 不受限。watermark 跳過時若距上次出場 ≥7 天，附加 `idle N days since last exit` 行。
  **#185**: the automatic run skips the AI plan call below 30 design-window trades (`waiting for sample: N/30`; 0 AI calls, watermark unchanged); manual Evolve is exempt. On a watermark skip, an `idle N days since last exit` line is added after 7 days.
- **#184**：PASS changelog 記錄套用的參數（`params`，無法解析時 `params_reason`）。判定與 Discord 文字不變。
  **#184**: PASS changelog entries record the applied `params` (or `params_reason`). Verdicts and Discord text unchanged.
- **#181**：記錄月門檻輸入（各月淨值、比例）到 debug.log 與 changelog `month_gates`；僅觀測。
  **#181**: month-gate inputs (per-month nets, share) logged to debug.log and stored on changelog `month_gates`; observability only.
- **#165**：不符資格的週六 knob 類別在 KeyError 前即被拒絕，只警示一次並推進 watermark（#164）。
  **#165**: ineligible Saturday knob classes are refused before `KeyError`, alerted once, watermark advances (#164).
- **#194**（merge `747d4dcd`）：週六網格改在 spawn 子程序中執行，不再在 live bot 程序內佔用 GIL 卡住 Tk（tick、成交輪詢、STOP 檔）。上限 20 分鐘；逾時／當機／停止時殺掉子程序、不挑選、watermark 不變。新增 `--grid-subprocess-smoke`。
  **#194** (merged `747d4dcd`): the Saturday grid runs in a spawned subprocess instead of the live process, so it no longer holds the GIL and stalls Tk (ticks, fill polling, STOP file). Bounded at 20 min; timeout/crash/stop kills the child, picks nothing, watermark unchanged. Adds `--grid-subprocess-smoke`.
- **#177（pending merge）**：發行版 `tai_backtest.exe` 直接支援無介面指令（`deploy`／`stop`／`list`／`status`／`strategies`／`backtest`），不另出第二個 EXE。雙擊、沒有參數、或無法辨識的第一個參數仍開 GUI。`--mode` 只接受 `paper` 與 `auto`；`semi_auto` 請用 GUI。不改下單、成交、還原或重連。
  **#177 (pending merge)**: the release `tai_backtest.exe` routes headless commands (`deploy` / `stop` / `list` / `status` / `strategies` / `backtest`); no second EXE. Double-click, no args, or an unknown first token still opens the GUI. `--mode` accepts `paper` and `auto` only; `semi_auto` stays on the GUI. No order, fill, restore or reconnect changes.
- **#172（pending：merge 或列為已知問題，待決定）**：#171 的後續修正。被 `BLOCK_FILL_PENDING` 擋下的延遲平倉（deferred close）在成交逾時或重啟後可能留下，之後在下一筆 auto 進場確認時被重播（新空單多一筆賣出，或同方向進場反被平倉）。修正：僅在進場 `fill_pending` 中才保存／還原 `deferred_close`；`FillPoller.timeout()` 在任何釋放成交閘門的逾時（進場與出場）都清除它。不改 BrokerPort／`broker.py`。進場逾時後不對帳實際部位仍是追蹤中的後續問題。
  **#172 (pending — merge or known issue, decision pending)**: fix-forward for #171. A deferred close (stored when `TRADE_CLOSE` is blocked by `BLOCK_FILL_PENDING`) could survive a fill timeout or a restart and be replayed on the next auto entry confirm (an extra sell on a new short, or a same-side entry closed instead). Fix: `deferred_close` is saved/restored only while an entry `fill_pending` is in flight, and `FillPoller.timeout()` clears it on every timeout that drops the fill gate (entry and exit). No BrokerPort / `broker.py` changes. Not reconciling the real book after an entry timeout remains a tracked follow-up.

### 打包 Build

- **#195**（merge `cad71dd9`）：修正乾淨 clone 的 EXE 建置（不再打包 gitignored 的 `strategies/`，改用空白佔位）與 `comtypes.gen` 啟動當機（runtime hook 在 `_comtypes_cache` 建立 `comtypes.gen`）；固定 PyInstaller 6.19.0、comtypes 1.4.15。
  **#195** (merged `cad71dd9`): fixes fresh-clone EXE builds (bundles an empty placeholder instead of the gitignored `strategies/`) and the `comtypes.gen` startup crash (runtime hook creates `comtypes.gen` in `_comtypes_cache`); pins PyInstaller 6.19.0 and comtypes 1.4.15.

## 升級注意 Upgrade notes

- 所有 GUI／bot 需換新 EXE 並重啟以取得 #178 重連與 #183。 / All GUIs/bots need the new EXE + restart for the #178 reconnect path and #183.
- 有週六 `auto_run` 的 bot 需新版並重啟。注意預設行為改變：`saturday_mode: propose`（不再自動提升 pool）、`search: grid_batch`、`paper_gate: relative`、月門檻 `merge_stub` + `scaled`。 / Bots with Saturday `auto_run` need the new build + restart. Default behaviour changes: `saturday_mode: propose` (no automatic pool promotion), `search: grid_batch`, `paper_gate: relative`, month gates `merge_stub` + `scaled`.
- `settings.example.yaml` 新增 `evolution` 鍵 / new `evolution` keys: `min_design_trades`, `idle_warn_days`, `knob_ineligible_fallback`, `paper_gate`, `real_gate`, `search`, `saturday_mode`, `relative.*`, `trial_penalty.*`, `retire.*`, `month_gate.*` (`latest_def`, `min_sessions`, `slack.*`), `structural_lane.*`. 可選本機檔 / optional local file: `taifex_closures.yaml`.
- 無介面操作（#177，pending merge）：在安裝資料夾執行 `tai_backtest.exe deploy`（部署）、`tai_backtest.exe stop`（停止）、`tai_backtest.exe list`（列出）、`tai_backtest.exe status`（狀態）。`--mode` 只有 `paper`／`auto`；`semi_auto` 請用 GUI。需在已登入的桌面工作階段執行（不支援 Windows 服務／LocalSystem）。 / Headless (#177, pending merge): from the install folder run `tai_backtest.exe deploy`, `tai_backtest.exe stop`, `tai_backtest.exe list`, `tai_backtest.exe status`. `--mode` is `paper` or `auto` only; `semi_auto` stays on the GUI. Requires a logged-in desktop session (not a Windows service / LocalSystem).
- `evolution_history.jsonl` 不會自動截斷，需要時手動封存。 / `evolution_history.jsonl` is never trimmed; archive by hand if needed.
- DEGRADED 時新進場被封鎖，請按 Reconnect 或重啟。 / In DEGRADED, new entries are blocked: press Reconnect or restart.

## 已知問題／待定關卡 Known issues / pending gates

**已知問題（#157）：** 券商連線中斷後重新連線時，機器人偶爾可能當機或凍結。最常發生在開盤前幾分鐘。
- **已確認：** 所有記錄到的當機或停止回應，都發生在斷線後於同一程序內重新連線的過程中；新啟動的機器人從未當機。故障是券商原生 SKCOM 函式庫內的 Windows 存取違規（access violation），程式無法攔截。
- **尚未確認：** SKCOM 內部的確切缺陷。SKCOM 為閉源，且每次當機的故障位置不同。
- **本版變更：** 重新連線改由新的連線狀態機處理（#178）。重新連線時不再呼叫 LeaveMonitor，只在關閉時呼叫。在部位核對完成前會阻擋新的進場。多次失敗後會維持斷線狀態、發出警示，並等待手動 Reconnect。尚未經長時間測試證實能消除此當機。
- **本版未包含：** 將券商連線移至獨立程序（#169）未包含在本版，因此重新連線仍在同一程序內進行。
- **對使用者的影響：** 若發生此問題，機器人會結束或停止回應，且不會自動重啟。任何未平倉部位在重啟機器人前將失去自動出場管理。收到斷線警示後，請檢查機器人，若已停止或凍結請重新啟動。

**Known issue (#157):** Rarely, a bot can crash or freeze while it reconnects after the broker connection drops. This is most likely in the few minutes before a session opens.
- **Confirmed:** Every recorded crash or hang happened during an in-process reconnect after a disconnect, and a freshly started bot has never crashed. The fault is a Windows access violation inside the broker's native SKCOM library, which the app can't catch.
- **Not confirmed:** The exact defect inside SKCOM. It's closed source, and the faulting location varies between crashes.
- **What changed in this build:** Reconnects now run through a new connection state machine (#178). It no longer calls LeaveMonitor during a reconnect, only at shutdown. It blocks new entries until positions are reconciled. After repeated failures it stays disconnected, alerts, and waits for a manual Reconnect. It hasn't yet been proven in a soak to eliminate the crash.
- **Not in this build:** Moving the broker into its own process (#169) isn't included, so a reconnect still happens inside the same process.
- **User impact:** If it happens, the bot exits or stops responding and does not restart itself. Any open position loses automatic exit management until you restart the bot. After a disconnect alert, check the bot and restart it if it's down or frozen.

- **#192 GIL 卡頓**：已由 #194 解決（網格改在 spawn 子程序執行）。 / **#192 GIL stall**: resolved by #194 (the grid now runs in a spawned subprocess).
- **#195**：已合併（`cad71dd9`），建置需要的修正已在 master。 / merged (`cad71dd9`); the build fix is on master.

## 開發者 Developer

- **#187**：離線 D7 backfill 腳本（預設 dry-run，只寫 `data/backfill/`）；不在 EXE 內，對操作者無影響。 / offline D7 backfill script (dry-run default, writes only `data/backfill/`); not in the EXE, no operator impact.
- **#176**：新增 tailscale-ssh skill 文件。 / docs: tailscale-ssh skill.
- **#170**：已被 #174 還原。 / reverted by #174.
