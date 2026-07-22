# v2.16.10

## 繁體中文

### 新功能
- **新增「多空 Regime」分頁**：多空切換機器人的趨勢資訊移至專屬分頁（切到分頁時自動刷新，亦可手動刷新），內容包括：
  - **目前趨勢**：原始/有效趨勢（上升趨勢、下降趨勢、盤整、過渡期）、ADX、分類指標(+DI/-DI/ATR比/EMA斜率)、待確認進度 — 與 Discord 通知相同格式
  - **最新建議**：決策內容與真實套用狀態（讀取 `regime_state.json` 的 `next_session.executed`，不再顯示永遠為 false 的 CSV `applied` 欄位）
  - **切換紀錄**：最近 30 個交易時段，每列包含該時段判定的趨勢（例如 `trending-up→transitional (ADX 27.3)`）、決策、現行策略與損益

### 修正
- **績效報告不再無限增長**：移除 Report 分頁的「多空切換紀錄」附加區塊 — 新分頁每次檢視時從磁碟重建內容，不再累加
- **v2.16.9 標籤修正**：GitHub 上的 v2.16.9 tag 原先指向 2.16.8 的 commit（來源未推送）；已重新指向實際建置的 commit `20e3380`（EXE 本身正確，見 BUILD_INFO.json）

---

## English

### New
- **New "多空 Regime" tab**: regime-switching bot info moved to a dedicated tab (auto-refreshes on select, plus a manual Refresh button):
  - **Current Regime**: raw/effective trend (trending-up/down, range-bound, transitional), ADX, classifier features (+DI/-DI/ATR ratio/EMA slope), pending-confirmation progress — same format as the Discord notification
  - **Latest Recommendation**: decision with its real applied status (read from `regime_state.json`'s `next_session.executed`, replacing the always-false CSV `applied` column)
  - **Switching Log**: last 30 sessions, each row showing the session's classified trend (e.g. `trending-up→transitional (ADX 27.3)`), decision, active strategy, and P&L

### Fixed
- **Report tab no longer grows unboundedly**: removed the appended "Regime Switching Log" section — the new tab rebuilds its content from disk on every view instead of appending
- **v2.16.9 tag corrected**: the GitHub v2.16.9 tag pointed at the 2.16.8 commit (source was unpushed); it now points at the actual build commit `20e3380` (the shipped EXE itself was correct, per BUILD_INFO.json)
