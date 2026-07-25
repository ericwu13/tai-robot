# v2.17.7

## 繁體中文

### 修正
- **reload_1m_bars DataStore 缺口修復 (#96)**：在盤前時段（05:00–08:45）重新部署的機器人若帶有已儲存的 CSV 歷史資料，現在能正確將 1 分 K 重新聚合至策略時間週期（H1/H4），修復了重啟後零訊號的問題
- **每日報告通知去重 (#95)**：去重鍵從僅日期改為日期+時段（DAY/NIGHT），讓日盤與夜盤機器人都能正確收到通知；Discord 發送錯誤現在會記錄至日誌而非靜默丟棄
- **Evolution Discord 通知錯誤記錄 (#94)**：進化策略的 Discord 通知失敗現在會記錄 HTTP 狀態碼、回應內容及例外詳情；缺少 channel ID 會在啟動時發出警告

---

## English

### Fixed
- **reload_1m_bars DataStore hole (#96)**: bots redeployed during the pre-open window (05:00–08:45) with saved CSV history now correctly re-aggregate 1-min bars into the strategy timeframe (H1/H4), fixing zero signals after restart
- **Daily report dedup key (#95)**: dedup key changed from date-only to date+session so DAY and NIGHT bots both receive their notifications; Discord send errors are now logged instead of silently dropped
- **Evolution Discord notification logging (#94)**: Discord notification failures in the evolution module now log HTTP status, response body, and exception details; missing channel ID warns at startup
