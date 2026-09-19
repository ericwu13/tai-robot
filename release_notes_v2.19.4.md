# v2.19.4

## 修正 Fixes

### 進化計畫空白／截斷：中止而非從空計畫演化（#134 / #108）

**問題**：週六 EVO 1/3 計畫呼叫曾回傳零可見文字（Gemini 思考 token 佔滿 maxOutputTokens，finishReason=MAX_TOKENS）。HTTP 200 不觸發重試；`parse_plan_directives` 寬容預設變成 action=change，watermark 消耗交易增量，codegen 從空計畫突變。

**變更**：新增 `plan_unusable_reason`：剝除 client 註解後若空白或截斷且無 directives 區塊則拒絕。evolution worker 在 watermark／codegen 前中止，雙語 Discord／聊天通知（未消耗 watermark；下次重跑同一批）。計畫呼叫改用與 codegen 相同的 `_CODE_GEN_MAX_TOKENS` 下限。下單路徑不變；settings 檔未改。

---

## Fixes (English)

### Evolution: refuse empty/truncated plan (#134 / #108)

**Problem**: Weekly EVO 1/3 plan sometimes returned zero visible text (thinking tokens exhausted maxOutputTokens). HTTP 200 skipped retry; tolerant parse default became action=change, burned the watermark, and codegen mutated from nothing.

**What changed**: New `plan_unusable_reason` refuses empty or truncated-without-directives plans. Worker aborts before watermark/codegen with bilingual notice (nothing consumed; next run retries same trades). Plan call gets the same `_CODE_GEN_MAX_TOKENS` floor as codegen. Order path unchanged; no settings file edits.

---

## 升級注意 Upgrade notes

- 有週六 auto_run 進化的機器人需重啟／換包後才套用此閘門。Restart bots that run weekly auto_run evolution to load the gate.
- 下單／regime 交易路徑未改。Trading / regime order path unchanged.
- 可選：live `settings.yaml` 將 `ai.max_tokens` 調到 ≥16384（模板仍是 4096）；計畫呼叫已有程式內下限，但其他 send_message 路徑仍用設定值。Optional: raise live `ai.max_tokens` ≥16384 (template still 4096); plan call has an in-code floor, other paths still use the setting.
