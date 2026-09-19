# v2.19.3

## 修正 Fixes

### 夜盤 P&L 補記：停電／重啟後空白列回填（#131 / #122）

**問題**：分類路徑本就有 catch-up，但記錄路徑只看「當前時段」，重啟後夜盤列常永遠空白。

**變更**：新增 `_record_night_catch_up`：僅在歷史列已存在且 pnl 空白時回填；無列不寫幽靈列；已有 pnl 為重啟安全去重。下單語意、settings、history schema 不變。

### W2 熱通道 Option C：美股晚間票可進今晚 classify（#135 / #132）

**問題**：預設 `nightly_vote_max_age_h[W2]=4h` 會在 ~04:58 丟掉多數美股晚間 W2。

**變更**：寫入時若開啟熱通道且仍在 admit 窗內，於 `regime_vote_w2.json` 戳 `admitted_at`；classify 可在超齡時仍保留該票。功能旗標預設 **關閉**（`news.w2_hot_lane_enabled: false`）。Admit ≠ 部署；不改 quorum／ADX／W3/W4。

---

## Fixes (English)

### Night P&L catch-up after outage/restart (#131 / #122)

**Problem**: Classify already catch-up'd; record only looked at `current_session(now)`, leaving blank night rows after restart.

**What changed**: New `_record_night_catch_up` backfills only when a history row exists with blank pnl (no phantoms; filled pnl is restart-proof dedup). Order path / settings / schema unchanged.

### W2 hot-lane Option C — US-evening votes for tonight's classify (#135 / #132)

**Problem**: Default 4h W2 TTL drops most US-evening votes by ~04:58.

**What changed**: When enabled, a young W2 write stamps `admitted_at`; classify keeps that vote past wall-clock age. Flag defaults **off**. Admit ≠ deploy; no quorum/ADX/W3/W4 changes.

---

## 升級注意 Upgrade notes

- 需重啟 regime 機器人才會套用 #131 補記與 #135 熱通道程式碼。Restart regime bots to load both.
- 熱通道預設關閉；要生效另在 live `settings.yaml` 設 `news.w2_hot_lane_enabled: true`。Hot-lane stays off until explicitly enabled on live settings.
- Discord admit 文案只表示「進今晚 classify 佇列」，不代表已 deploy。
