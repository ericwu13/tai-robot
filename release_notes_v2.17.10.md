# v2.17.10

## 繁體中文

### 修正
- **多空切換機器人重啟不再誤報 regime-idle**：先前 regime-idle 的 Discord 警告與 GUI 狀態更新在 `restore_session()` 之前就執行，導致即使 `session.json` 中已存有作用中的多／空腳位，每次重啟仍會顯示「regime-idle」。現在兩項檢查都移到還原流程之後，只在真正閒置時才發出警告。
- **還原作用中腳位時顯示確認訊息**：成功還原作用中腳位後，會新增一行即時記錄 `[RESUME] Active leg restored: SHORT (BbandSmaShortV3)`，讓使用者看到明確確認而非一片靜默。
- **機器人啟動通知合併還原資訊**：多空切換機器人的「機器人啟動 Bot Deployed」Discord 訊息延後至 `restore_session()` 之後發送，並將還原的腳位資訊（`✅ 已恢復 Restored: 做空 SHORT · 策略名稱`）併入同一則訊息，取代先前分成兩則的通知。
- **報告日期改用台北時區當日日期**：每日報告的日期改用 `datetime.now(TPE_TZ).date()` 決定，不再沿用最後一筆交易的出場日期，避免跨日或無交易時顯示錯誤日期。
- **「今日 Today」在無交易時顯示 0**：當日若無平倉交易，「今日 Today」現在正確顯示 0 筆交易／0 損益，移除先前退回到「最近交易日」的錯誤 fallback。

---

## English

### Fixed
- **Regime-switching restart no longer false-flags regime-idle**: the regime-idle Discord warning and GUI status update previously fired *before* `restore_session()` ran, so every restart showed "regime-idle" even when an active long/short leg was saved in `session.json`. Both checks now run after the restore block, so the warning fires only when the bot is genuinely idle.
- **Restored active leg is confirmed in the log**: after an active leg is successfully restored, a live log line `[RESUME] Active leg restored: SHORT (BbandSmaShortV3)` is emitted so the user sees explicit confirmation instead of silence.
- **Deploy notification folds in restore info**: the regime-switching "Bot Deployed" Discord message is now deferred until after `restore_session()`, with the restored-leg info (`✅ 已恢復 Restored: 做空 SHORT · strategy name`) folded into that single message instead of two separate notifications.
- **Report date uses the Taipei-timezone current date**: the daily report date now comes from `datetime.now(TPE_TZ).date()` instead of the last trade's exit date, avoiding a wrong date across day boundaries or when there were no trades.
- **"今日 Today" shows 0 when no trades closed today**: with no closed trades for the day, "今日 Today" now correctly shows 0 trades / 0 P&L, removing the previous fallback that reverted to the most recent trading day.
