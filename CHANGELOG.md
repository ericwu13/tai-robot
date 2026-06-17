# Changelog / 更新日誌

All notable changes to tai-robot are documented here.
本檔案記錄 tai-robot 的重要變更。

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
