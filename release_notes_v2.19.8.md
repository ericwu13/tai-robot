# v2.19.8

## 修復 Fixes

### 週末進化：多折＋月度閘門、僅寫入候選池（#154，#153）

**變更**：週六自動進化路徑（與 deep／短窗 A/B）的判定改為既有 walk-forward／簡單 A/B **且** 多折閘門（holdout ABS_PF≥1、有效交易≥3、≥4 非空折、多數決前≥3 分歧折、多數決、paired Δ＞0）**且** 先前月份淨利 ≥ 基準−5k（排除最新曆月；未過＝`prior-months collapse`）**且** 若全窗優勢＞0 則最新月占比 ≤50%（未過＝`calendar-month dominated`）。PASS 只呼叫 `record_validated_candidate`（pool → validated），**不** `StrategyStore.save`、**不** 註冊 `STRATEGIES`；部署仍手動。DynamicExitPullback* 週六計畫改 allowlist 旋鈕（EMA 不含 80/90）。持續下單路徑不變。

### 盤勢分類：失敗退避寫入日誌、存檔失敗回滾記憶體（#152，#151）

**變更**：`_maybe_classify` 每一次提前返回都寫日誌（重試／未到期＝INFO；`classify_session` 回 None＝WARN 並標 session）。`save_state` 失敗時從磁碟重載記憶體（或回預步驟狀態），`last_assessed` 不會超前磁碟，該夜不寫入 history／不廣播。分類例外同樣回滾。持續下單路徑不變。

---

## Fixes (English)

### Weekend evo: multifold + month gates, pool-only PASS (#154, #153)

**What changed**: Saturday auto path (and deep / short-span A/B) verdict is existing walk-forward / simple A/B **AND** multifold gates (holdout ABS_PF≥1, expressed trades≥3, ≥4 non-empty folds, ≥3 divergent before majority, majority, paired Δ > 0) **AND** prior-months net ≥ baseline−5k (latest calendar month excluded → `prior-months collapse`) **AND** if full-window edge > 0, latest-month share ≤ 50% (`calendar-month dominated`). PASS only calls `record_validated_candidate` (pool → validated); does **not** `StrategyStore.save` or register `STRATEGIES`. Operator deploy stays manual. DynamicExitPullback* Saturday plans use allowlisted knobs (EMA excludes 80/90). Continuous order path unchanged.

### Regime classify: log bails + roll back a failed save (#152, #151)

**What changed**: Every `_maybe_classify` early return is logged (retry / not-due = INFO; null `classify_session` = WARN naming the session). On `save_state` failure, memory reloads from disk (or pre-step state) so `last_assessed` cannot lead disk; that night is not appended to history or announced. Classifier exceptions also restore pre-step state. Continuous order path unchanged.

---

## 升級注意 Upgrade notes

- GUI 需換包才能拿到週末演化閘門／pool-only PASS 與盤勢分類日誌修正。Restart/resync GUI for the new EXE.
- Paper／live 若跑週六 `auto_run` 進化：需 pull／重啟以套用新閘門。Paper/live bots with Saturday auto_run: pull/restart for the new gates.
- Paper／live 若跑盤勢分類：需 pull／重啟以套用 #152 日誌與存檔回滾。Paper/live bots that classify: pull/restart for #152.
- 持續下單、不跑演化／分類的路徑：無強制重啟。Continuous trading without evo/classify: no forced restart.
- PASS 僅寫入候選池 validated，不會自動進 STRATEGIES。PASS is pool validated only (no STRATEGIES autosave).
