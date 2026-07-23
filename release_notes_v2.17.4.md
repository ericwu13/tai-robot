# v2.17.4

## 繁體中文

### 修正
- **進場模擬價改用下一根K棒開盤價（與回測一致）**：即時／模擬模式的進場成交價原本使用訊號K棒的收盤價。在時段交界（日盤收盤 → 夜盤開盤），該收盤價其實是**上一個時段的最後成交價**，可能過時 75 分鐘以上——實際案例：15 分 K 策略在 13:30 K 棒（日盤收盤 44,930）觸發訊號，該 K 棒直到夜盤第一根 1 分 K 於 15:01 抵達才收斂完成，真實 IOC 市價單成交在夜盤開盤價 44,774，Discord 通知卻顯示模擬價 44,930，出現 156 點的假性價差：
  - 回測使用 `next_open` 成交模式（訊號 K 棒 N → 於 K 棒 N+1 開盤價成交），修正後即時模式與回測完全一致
  - 關鍵洞察：訊號處理當下「下一根 K 棒的開盤價」其實已經存在——聚合器必須收到下一個時段窗口的第一根 1 分 K 才會收斂訊號 K 棒，該局部 K 棒的開盤價即為 next-open 成交價
  - 遞補順序：局部聚合 K 棒開盤價 → 最新 tick（≤120 秒，1 分 K 策略專用）→ K 棒收盤價（永不以 0 成交）
  - 市價平倉（`broker.close()`）維持以 K 棒收盤價成交，與回測平倉語意一致
  - 實單成交價回報（issue #45/#92 的 OnNewData／OpenInterest 機制)不受影響，仍會覆寫模擬價
  - 模擬模式（含 regime 影子機器人）的交易紀錄與時段損益從此與回測同一套成交邏輯，時段交界進場不再出現整段跳空誤差
  - `ENTRY_FILL` 決策記錄現在標註成交價來源（next-open / tick / bar close），方便稽核

### 測試
- 新增 8 個測試（`tests/test_live_next_open_fill.py`），涵蓋事故情境：13:30 日盤收盤 K 棒觸發訊號、15:00 夜盤第一根 1 分 K 抵達時必須以夜盤開盤價成交，以及 1 分 K tick 遞補、tick 過時回退、市價平倉不受影響——總計 1609 個測試

---

## English

### Fixed
- **Live entry sim price now fills at the next bar's open (backtest parity)**: live/paper entry fills used the signal bar's CLOSE. At a session boundary (day close → night open) that close is the **previous session's last trade**, up to 75+ minutes stale — real incident: a 15-min strategy signaled on the 13:30 bar (day close 44,930), which only finalized when the first night 1-min bar arrived at 15:01; the real IOC order filled at the night open 44,774 while Discord showed sim price 44,930, a 156-point phantom gap:
  - Backtest runs `fill_mode="next_open"` (signal on bar N → fill at bar N+1's open); live now matches it exactly
  - Key insight: the next bar's open is ALREADY KNOWN at signal time — the aggregator can only finalize the signal bar when the next window's first 1-min bar arrives, and that partial bar's open IS the next-open fill price
  - Fallback chain: partial aggregated bar's open → freshest tick (≤120s, for 1-min strategies) → bar close (never fills at 0)
  - Market closes (`broker.close()`) still fill at bar close, identical to backtest close-fill semantics
  - Real fill price tracking (issues #45/#92, OnNewData/OpenInterest) is unaffected and still overwrites the placeholder
  - Paper-mode trade records and regime shadow-bot session P&L now share the exact fill model as backtest — no more inter-session gap error on boundary entries
  - `ENTRY_FILL` decision rows now record the fill source (next-open / tick / bar close) for auditability

### Tests
- 8 new tests (`tests/test_live_next_open_fill.py`) covering the exact incident scenario — signal on the 13:30 day-close bar must fill at the night open when the 15:00 bar arrives — plus 1-min tick fill, stale-tick fallback, and market-close isolation — 1609 tests total
