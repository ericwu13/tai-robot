# v2.19.1

## 修正 Fixes

### 演化水位線：延後到計畫 action=change 才前進（#125 / #127）

**問題**：水位線在演化 worker 啟動時就前進，不論計畫結果。`no_change` / FAIL 的週六仍燒掉本週設計窗差異，下週只看到過 holdout 切點後的少數新交易（例如 09-12：3/130）。

**變更**：水位線僅在「完成的計畫且 action=change」後才寫入。啟動時、`no_change`、計畫無法解析 action 時皆不前進。閘門門檻不變（ABS_PF_FLOOR、MIN_EXPRESSED_TRADES、HOLDOUT_DD_RATIO_MAX、MIN_TRADES=30 等）。

---

## Fixes (English)

### Evolution watermark: advance only after plan action=change (#125 / #127)

**Problem**: Watermark advanced at evolution worker launch, before the plan returned. A Saturday that ended `no_change` or FAIL still consumed that design-window delta, so the next week's plan saw only the few trades that aged across the holdout cut.

**What changed**: Watermark saves only after a completed plan with `action=change`. Launch, `no_change`, and unparseable plan-only fallback leave it frozen. Gate thresholds unchanged.

---

## 亦含 Also in this tag

- docs(skills): related-work gate before fix-pr assign (#124) — agent workflow only; no runtime change for operators.

---

## 升級注意 Upgrade notes

- 需重新啟動機器人才會採用新演化水位線邏輯（週六 05:05 演化使用執行中程式）。Restart running bots to adopt the watermark deferral (Saturday 05:05 evolution runs whatever code is loaded).
