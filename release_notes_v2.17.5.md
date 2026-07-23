# v2.17.5

## 繁體中文

### 修正
- **出場成交價改用 GetFulfillReport(1) 逐筆明細輪詢（issue #92 後續修正）**：先前版本以 `GetFulfillReport(format=4)`（合併同商品）作為出場回報價格來源，但 format 4 回傳的是**當日該方向的累積加權均價**，不是單筆成交價——實際案例：對帳單顯示出場價 44,622，但 Discord 通知報 44,794（當日賣出均價），導致損益偏差
  - 改用 `GetFulfillReport(format=1)`（逐筆明細），以委託序號比對找到對應的出場成交紀錄，取得真實單筆成交價
  - 輪詢採指數退避（1s, 2s, 4s, ...），最多重試至確認成交或逾時
  - OnNewData（`SKReplyLib` deal 回報）仍為主要來源；format 1 輪詢為備援路徑，先到者寫入
  - 進場價維持使用 OpenInterest 均價（單口等價於成交價），不受此修正影響

---

## English

### Fixed
- **Exit fill price now polls GetFulfillReport(1) per-fill detail (issue #92 follow-up)**: previous versions used `GetFulfillReport(format=4)` ("merge by commodity") as exit price source, but format 4 returns a **day-cumulative weighted average per side**, not the individual fill price — real incident: brokerage statement showed exit at 44,622 but Discord reported 44,794 (day sell-side average), causing P&L drift
  - Switched to `GetFulfillReport(format=1)` (per-fill detail), matching the exit fill by the 13-digit order sequence number returned from `SendFutureOrderCLR`
  - Polling uses exponential backoff (1s, 2s, 4s, ...) until the fill is confirmed or timeout
  - OnNewData (`SKReplyLib` deal callback) remains the primary source; format 1 polling is the fallback — whichever arrives first writes the price
  - Entry fill price continues using OpenInterest avg_cost (equivalent for single-lot), unaffected by this fix
