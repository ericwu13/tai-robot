# v2.17.12

## 繁體中文

### 修正
- **移除 30 秒帳戶輪詢中的 GetFulfillReport 呼叫，消除幽靈「實成交」日誌**：`_query_real_account` 每 30 秒呼叫 `GetFulfillReport(1)`，但因欄位索引與格式不符（format-1 vs format-4），每筆資料均被解析為 `SELL平 x0.0000 @0.0`；加上去重計數器每次輪詢歸零，導致同一批過期資料在整個交易時段內被反覆記錄為「新成交」。此呼叫僅供顯示用途 — 真正的成交偵測使用 GetOpenInterestGW（FillPoller），成交價格來自 OnNewData（RealFillTracker），皆不受影響。「今日成交」計數器現改由 OnNewData 成交事件驅動。

---

## English

### Fixed
- **Remove GetFulfillReport from 30-second account poll — eliminates phantom 實成交 log entries**: `_query_real_account` called `GetFulfillReport(1)` every 30 seconds, but field indices were wrong (format-1 vs format-4 mismatch), so every row parsed as `SELL平 x0.0000 @0.0`. The dedup counter also reset between polls, causing the same stale rows to be re-logged as "new fills" repeatedly for the entire session. This call was display-only — real fill detection uses GetOpenInterestGW (FillPoller) and real fill prices come from OnNewData (RealFillTracker), both unaffected. The "今日成交 Trades" counter now derives from OnNewData fill events instead.
