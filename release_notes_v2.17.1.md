# v2.17.1

## 繁體中文

### 修正
- **實際出場價格追蹤 (issue #92)**:Discord 成交通知與交易紀錄先前可能顯示錯誤的出場價 — 原本的價格來源 `GetFulfillReport(合併同商品)` 回傳的是當日同方向成交的**累計平均價**,不是該筆委託的實際成交價(例:實際成交 44,622 卻通知 44,794)。現改用 `SKReplyLib.OnNewData` 即時逐筆成交回報,以 13 碼委託序號比對,每筆委託取得自己的真實成交價:
  - 同帳戶多個機器人/手動下單互不干擾 — 序號不符的成交一律忽略
  - **Discord**:成交確認直接顯示真實成交價;若回報延遲,先送「(est.)」估計價,回報到達後自動補發「🔁 成交價更正 Fill Price Correction」通知
  - **交易明細**新增「實出場 Real Exit」欄位,與「實進場 Real Entry」對稱
  - 進場價亦優先採用逐筆回報(OpenInterest 平均成本保留為備援)
- **移除「實委託(已成)」記錄行**:`GetOrderReport(5)` 的欄位排列與成交回報不同,共用解析器導致每行都顯示 `x0 @`(無價格/數量);OnNewData 現已記錄每筆委託事件的原始資料,故直接移除該查詢

### 測試
- 新增 30 個測試(`tests/test_fill_report.py`),含 issue #92 實際情境:同日第二筆出場必須取得自己的成交價、他人委託隔離、延遲回報防護 — 總計 1600 個測試

---

## English

### Fixed
- **Real exit price tracking (issue #92)**: Discord fill notifications and the trade log could show a wrong exit price — the old source, `GetFulfillReport` (merged-by-commodity), returns the **day-cumulative average** of all same-side fills, not the order's own fill price (e.g. actual fill 44,622 notified as 44,794). Real fill prices now come from `SKReplyLib.OnNewData` real-time per-order deal reports, matched by the 13-digit order seq no:
  - Multiple bots / manual orders on the same account can no longer cross-contaminate — deal rows with an unknown seq no are ignored
  - **Discord**: fill confirmations carry the real price; if the deal report lags, an "(est.)" price goes out first and a "🔁 Fill Price Correction" follow-up is sent automatically when the real fill arrives
  - **Trade list** gains a "實出場 Real Exit" column, mirroring "實進場 Real Entry"
  - Entries also prefer the per-order deal price (OpenInterest avg_cost kept as fallback)
- **Removed the "實委託(已成)" log line**: `GetOrderReport(5)` has a different CSV layout than the fulfill report, so the shared parser rendered every row as `x0 @` (no price/qty); OnNewData now logs each raw order event, so the query was dropped

### Tests
- 30 new tests (`tests/test_fill_report.py`) covering the exact issue #92 scenario: a second same-day exit must get its own fill price, foreign-order isolation, and late-fill guards — 1600 tests total
