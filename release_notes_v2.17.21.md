# v2.17.21

## 修正 Fixes

### 成交確認改用 OnNewData 回報，不再單靠未平倉查詢 (issue #103)

**問題**:8/18 早盤進單後,IOC 委託在 1 秒內成交,但機器人回報「成交超時」並降級為半自動,使用者因此手動補單,造成部位重複(空單 2 口而非 1 口)。

**根本原因**:成交確認只監聽 `OnOpenInterest` 未平倉回報。事發當時 `GetOpenInterestGW` 連續回傳 API 錯誤(`RequestOpenInterest result JSON data error`)長達 40 秒,而 `OnNewData` 成交回報(type-D deal row)在下單後 1 秒內就已抵達,卻只被用來記錄成交價格,不會觸發成交確認。10 秒輪詢逾時後即誤判為未成交。

**修正**:
- `OnNewData` 成交回報現在直接確認等待中的成交輪詢——它是 API 的逐筆委託回報流,以 13 碼委託序號精準對應本機器人送出的委託,是成交的權威證據
- 確認動作排在 broker 的防護寫入(guarded write)之後:被拒絕的過期回報(前一筆交易的延遲成交)永遠無法確認當前委託
- Discord 成交通知在此路徑下直接帶出實際成交價
- `OnOpenInterest` 輪詢保留為備援;10 秒逾時維持不變,現在僅在兩條回報通道都靜默時觸發(最後防線警報)

---

### Fill confirmation now uses OnNewData deal reports, not only OpenInterest (issue #103)

**Problem**: On the 8/18 morning session, an IOC entry order filled within 1 second, but the bot reported "fill timeout", downgraded to semi-auto, and the user manually re-entered — doubling the position (2 lots short instead of 1).

**Root cause**: Fill confirmation only listened to `OnOpenInterest` position callbacks. During the incident, `GetOpenInterestGW` returned API errors (`RequestOpenInterest result JSON data error`) for 40+ seconds, while the `OnNewData` deal row (type "D") arrived within 1 second of the order — but was only used for price tracking, never for fill confirmation. The 10-second poll window expired and the bot wrongly assumed the fill was unconfirmed.

**Fix**:
- `OnNewData` deal rows now directly confirm the pending fill poll — they are the API's per-order report stream, matched by the 13-digit seq no of orders this bot sent, and are definitive proof of fill
- Confirmation runs after the broker's guarded real-price write: a rejected stale row (late fill from a previous trade) can never confirm the current order
- The Discord fill notification carries the actual fill price on this path
- `OnOpenInterest` polling remains as fallback; the 10s timeout is unchanged and now only fires when both report channels are silent (last-resort alarm)
