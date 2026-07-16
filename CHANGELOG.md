# Changelog / 更新日誌

All notable changes to tai-robot are documented here.
本檔案記錄 tai-robot 的重要變更。

---

## [2.16.1] — 2026-07-16

### 繁體中文

#### 修正
- **自動更新無法終止主程式（updater）**：`launch_update()` 從背景執行緒呼叫 `sys.exit(0)` 時，僅在該執行緒拋出 `SystemExit`，主程式與 GUI 繼續運行，導致更新批次檔無法替換正在使用中的 exe。改用 `os._exit(0)` 立即終止整個程序，更新腳本得以順利完成檔案替換與重新啟動。

### English

#### Fixed
- **Self-update fails to terminate main process (updater)**: `launch_update()` called `sys.exit(0)` from a background thread, which only raises `SystemExit` in that thread — the main process and GUI kept running, preventing the update batch script from replacing the locked exe. Switched to `os._exit(0)` to terminate the entire process immediately, unblocking the update script's file replacement and relaunch.

---

## [2.12.3] — 2026-07-01

### 繁體中文

#### 修正
- **演化靜默跳過（issue #74）**：週末自動演化在某些條件下（設計窗無交易、尚無交易紀錄、執行異常）靜默返回，使用者無從得知演化未執行。修正後所有跳過路徑均發送雙語聊天訊息，`auto_run` 模式同步發送 Discord 通知；未預期例外也會通報 Discord。
- **BbandSmaShortV3 止損過窄（issue #75）**：`atr_sl_mult` 由 0.1 調至 1.0。原始 0.1 倍 ATR 使止損僅 36–60 點，在正常 60 分 K 棒波動（均幅 ~346 點）下一根 K 即被洗出。加寬至 1.0 倍後，模擬顯示總損益改善 +6,920（Trade #2 不再被盤中波動觸發止損）。另新增選擇性動態出場機制（追蹤止損、打平、反彈出場），預設關閉。

### English

#### Fixed
- **Silent evolution skip (issue #74)**: the weekly auto-evolution pipeline returned silently under certain conditions (no design-window trades, no trades at all, uncaught exception), leaving the user unaware that evolution didn't run. All skip paths now send a bilingual chat message; `auto_run` mode also fires a Discord notification; uncaught exceptions are reported to Discord too.
- **BbandSmaShortV3 SL too tight (issue #75)**: `atr_sl_mult` widened from 0.1 to 1.0. The original 0.1× ATR placed the stop just 36–60 pts above prev_high — well inside normal 60-min bar noise (avg range ~346 pts), causing two of six live trades to stop out on the first bar despite correct directional calls. At 1.0× ATR, what-if simulation on the live session data improved total PnL by +6,920 (Trade #2 survived the spike). Optional dynamic exit mechanisms (trailing stop, break-even, bounce exit) are included but default-off.

---

## [2.12.2] — 2026-06-16

### 繁體中文

#### 修正
- **盤前自動平倉價格失準（issue #61）**：自動平倉（盤前自動平倉／停止機器人／手動停止）原本以「策略原生週期 K 棒收盤價」（`_aggregated_bars[-1].close`）成交模擬部位，但該價格是在與 K 棒邊界無關的任意時點（30 秒輪詢或手動停止）讀取的，在較高週期策略上可能已過時數分鐘。實單以市價（IOC）成交於真實市場，導致模擬與實單出現約 100 點的價差（事故：模擬 45,879 vs 實際成交 45,778）。修正後抽取純函式 `select_freshest_price`（即時 tick ＞ 最近 1 分 K 收盤 ＞ 聚合 K 收盤），三處平倉與 `_get_latest_price` 統一走此函式，模擬價即與實單貼近。
- **過時 tick 防護（#61 後續）**：`_live_last_tick_price` 過去僅在停止時歸零，feed 靜默中斷後仍保留舊值且無時間戳記。現在 tick 一併記錄時間戳，`select_freshest_price` 可在 tick 超過 120 秒（與 tick 看門狗一致）時跳過該來源，退回較新的 1 分 K 收盤。

### English

#### Fixed
- **Stale force-close price (issue #61)**: the auto-close paths (session-end auto-close, stop-bot, manual stop) filled the simulated position at the strategy's native-timeframe bar close (`_aggregated_bars[-1].close`), read at an arbitrary wall-clock moment (a 30s poll or manual stop) unrelated to any bar boundary — minutes stale on higher-timeframe strategies. The real IOC-market order filled at the true market, so paper diverged ~100pts from real (incident: paper 45,879 vs real 45,778). Fixed by extracting a pure `select_freshest_price` helper (live tick > last 1-min close > aggregated close) and routing all three force-close sites plus `_get_latest_price` through it, so the simulated price tracks the real fill.
- **Stale-tick guard (#61 follow-up)**: `_live_last_tick_price` was only zeroed on stop, so after a silent feed stall it kept a nonzero but minutes-old value with no timestamp. The tick now carries a timestamp; `select_freshest_price` skips a tick older than 120s (matching the tick-watchdog) and falls back to the fresher 1-min bar close.

---

## [2.12.1] — 2026-06-12

### 繁體中文
- **Discord 模式切換通知**：即時熱切換交易模式（`_apply_mode_switch`）時，發送 🔄 交易模式切換通知至 Discord，含可讀標籤（模擬 Paper / 輔助 Semi-Auto / 全自動 Auto）。通知由 GUI 的 `on_mode_changed` 回呼觸發，LiveRunner 維持與通知無關（與其他通知一致）；通知失敗不影響切換本身。

### English
- **Discord mode-switch notification**: a 🔄 Trading Mode Switched message is sent to Discord on a live hot-swap (`_apply_mode_switch`), with readable labels (模擬 Paper / 輔助 Semi-Auto / 全自動 Auto). Fired from the GUI's `on_mode_changed` callback so LiveRunner stays notification-agnostic (consistent with every other notification); a notification failure never breaks the switch.

---

## [2.12.0] — 2026-06-12

### 繁體中文

#### 新增功能
- **即時切換交易模式 Hot-swap mode**：機器人執行中可在 `paper` / `semi_auto` / `auto` 之間切換（即時頁籤下拉選單或 `mode_override.json`）。有未平倉部位時會擋下切換；切到 `auto` 需確認對話框（UI）或 `trading.allow_live_override`（檔案）；成交等待中亦會擋下。
- **交易來源標記 Trade source tagging**：每筆交易標記 `real` / `paper` / `backtest`。`real` 需有實際成交確認（非僅模式）。交易明細新增「來源」欄、CSV 匯出新增 Source 欄；每日報告、Discord、停止摘要皆拆分模擬／實單。報告頁籤新增「檢視 All / 實單」切換。
- **🚀 Ultra Mode 切換按鈕**：聊天區一鍵切換最強模型（codegen / 檢視 / 演化 / Pine）。開啟時跳出成本確認；**僅本次執行有效**，重啟即還原（避免帳單意外）。持久預設仍由 AI Settings 對話框設定。
- **🧬 Bot Evolution 策略演化**：AI 產生「單一變更」演化計畫 → 自動產生候選策略 → walk-forward 驗證（90 天訓練 / 14 天保留測試 + Monte Carlo）→ 通過則存檔為 `AI: …Evo1`。**部署仍需人工**。每週六收盤後自動執行一次，結果送 Discord。詳見 `docs/evolution_framework.md`。
- **演化框架文件**：新增 `docs/evolution_framework.md`，以知識圖譜方式說明水位線、保留測試集、每週節奏、`MIN_TRADES` 門檻、計畫流程與設定。

#### 修正
- **`bb_std` 指標**：本專案無 `bb_std`／標準差屬性（純 Python 指標，無 pandas_ta/talib）。標準差請以 `(bb.upper - bb.middle) / num_std` 取得；`self.bb_std` 為策略自定的倍數參數。已寫入 codegen 提示與 CLAUDE.md。
- **Ultra 模型名稱**：`gemini-3.1-pro` 不存在，正確為 `gemini-3.1-pro-preview`；preview 模型若 404 自動退回預設模型。
- **演化資料窗**：保留測試集對 AI 隱藏、僅以 walk-forward 驗證；相對於基準的崩潰／回撤門檻；交易數門檻改為相對基準視窗。

#### 維護
- 清除 21 個閒置 worktree 與約 36 個過期分支。

### English

#### Added
- **Hot-swap trading mode**: switch a running bot between `paper` / `semi_auto` / `auto` (Live-tab dropdown or `mode_override.json`). Blocked with an open position; switching to `auto` requires a confirmation dialog (UI) or `trading.allow_live_override` (file); also blocked while a fill is pending.
- **Trade source tagging**: every trade tagged `real` / `paper` / `backtest`. `real` requires a confirmed broker fill (not merely the mode). New 來源/Source column in the Trades tab and CSV export; daily report, Discord, and stop summaries split paper vs real; Report tab gains an All/Real view filter.
- **🚀 Ultra Mode toggle button**: one-click top-model switch (codegen / review / evolution / Pine) in the chat header with a cost-confirmation dialog. **Session-only** — resets on restart so it can't quietly inflate the bill. The persistent default still lives in the AI Settings dialog.
- **🧬 Bot Evolution**: AI writes a single-change evolution plan → auto-generates a candidate strategy → walk-forward validation (90d train / 14d out-of-sample holdout + Monte Carlo) → saves passing candidates as `AI: …Evo1`. **Deployment stays manual.** Runs automatically once per week after the Saturday close, with the verdict posted to Discord. See `docs/evolution_framework.md`.
- **Evolution framework documentation**: new `docs/evolution_framework.md` — a knowledge-graph reference covering the watermark, holdout, weekly cadence, `MIN_TRADES` gate, plan flow, and configuration.

#### Fixed
- **`bb_std` indicator**: there is no `bb_std`/std attribute anywhere (pure-Python indicators; no pandas_ta/talib). Recover std as `(bb.upper - bb.middle) / num_std`; `self.bb_std` is a self-defined strategy multiplier. Documented in the codegen prompt and CLAUDE.md.
- **Ultra model id**: `gemini-3.1-pro` does not exist — corrected to `gemini-3.1-pro-preview`, with automatic fallback to the default model on a 404 (preview ids get retired).
- **Evolution data windows**: holdout withheld from the AI and used only for walk-forward validation; collapse/drawdown gates relative to baseline; trade floor relative to the baseline's same-window count.

#### Maintenance
- Cleaned up 21 stale agent worktrees and ~36 stale local branches.

---

## [2.11.0] — 2026-06-11
- Reconnect bug fixes + structured diagnostic logging; ultra_mode tier (opt-in); conversation auto-truncation. (See git history for detail.)
