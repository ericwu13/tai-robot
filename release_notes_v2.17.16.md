# v2.17.16

## 繁體中文

### 修正

**盤末實際出場價未寫入每日報告與交易頁籤（issue #92 再現）**
- 症狀：Discord 交易頻道顯示正確的實際出場價，但「每日報告」與 App「交易紀錄」頁籤仍顯示錯誤（模擬）出場價，三者不一致
- 根本原因：盤末強制平倉的實際成交回報（`OnNewData` 成交列）是非同步送達的，約在平倉後 1～6 秒才到。但同一個 30 秒輪詢在觸發強制平倉的當下，就同步產生了每日報告並存檔 `session.json`——兩者都在實際成交價寫入之前。每日報告以 `{日期}_{時段}` 去重，後續輪詢不會再重新產生，於是報告 JSON 與 `session.json` 都把 `real_exit_price` 凍結成 0；Discord 交易通知另有一條較晚才觸發的成交確認計時器，所以只有它是對的
- 進場實際價之所以正確，是因為部位開著好幾個小時，期間的存檔會補上；出場是整個時段的最後一個事件，之後沒有任何存檔
- 實測案例（SHORT TM0000，2026-08-07 13:43）：`session.json` 於 13:43:16 存檔、每日報告於 13:43:17 產生、實際成交 44,286 於 13:43:17.921 才寫入——太晚
- 修正方式：
  1. 盤末報告在「該時段強制平倉的實際出場成交」尚未寫入前先延後產生，等下一次輪詢實際價到位後再產生；為避免遺失報告，收盤前最後一分鐘即使成交未到也照常產生（退回模擬價，與修正前行為相同）。純模擬（paper）機器人不受影響
  2. 當較晚的實際出場價寫入時，立即重新存檔 `session.json` 並刷新交易頁籤（同時涵蓋退回情況與 `stop()` 手動停止，並即時更新 UI）
- 確認無第二個問題：更早那些 `real_exit_price=0` 的交易全都早於 v2.17.8（issue #92 修正），當時尚無此功能；所有生產日誌中從未出現 `accepted=False`，寫入防護從未誤擋

---

## English

### Fixed

**Real exit price not written into the daily report / Trades tab (issue #92 recurrence)**
- Symptom: the Discord trade channel shows the correct real exit price, but the daily report and the app's Trades tab still show the wrong (simulated) exit price — the three disagree
- Root cause: the session-end force-close's real fill (an `OnNewData` deal row) arrives asynchronously ~1–6s after the sim position closes. But the same 30s poll that triggers the force-close also generates the daily report and saves `session.json` synchronously — both before that write. The report is debounced by `{date}_{session}`, so no later poll regenerates it, and both the report JSON and `session.json` freeze `real_exit_price=0`. Discord is correct only because its per-trade fill-confirm runs on a separate, later timer
- The real entry price survives because the position is open for hours and later saves catch it; the exit is the session's last event, so nothing re-persists it
- Proven incident (SHORT TM0000, 2026-08-07 13:43): session saved 13:43:16, report generated 13:43:17, real fill 44,286 written at 13:43:17.921 — too late
- Fix:
  1. Defer the session-end report while that force-close's real exit fill is still in flight, generating it on a later poll once the real price is recorded. Fallback: within the final minute before close, generate anyway so the report is never lost (a stuck fill degrades to the sim price, exactly as before). Paper bots are unaffected
  2. On the accepted late write, immediately re-save `session.json` and refresh the Trades tab (covers the fallback and the `stop()` force-close, and updates the UI promptly)
- Confirmed no second bug: the earlier `real_exit_price=0` trades all predate the v2.17.8 issue #92 fix (the feature didn't exist yet); `accepted=False` never appears in any production log, so the guarded write has never wrongly rejected
