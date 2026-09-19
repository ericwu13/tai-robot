# v2.19.2

## 修正 Fixes

### 演化計畫：不再請模型填 holdout 數值門檻（#133）

**問題**：計畫 prompt 範例 JSON 含 `PF≥1.2` / `WR≥45%`，模型常抄進 `directives["criteria"]`，使已過 #125 硬門檻的 Path A 候選仍因「計畫門檻」FAIL（例：TMF00_0422 2026-09-19）。

**變更**：機器可讀指令改為僅 `{"action":"change"|"no_change"}`；明確要求勿輸出數值 holdout 條件，改走預設 not-worse-than-baseline。#125 硬門檻不變（ABS_PF_FLOOR、MIN_EXPRESSED_TRADES、HOLDOUT_DD_RATIO_MAX、MC CV）。

### 演化設計報告 MaxDD% 與 fitness 資本基數一致（#128）

設計窗 `format_report` 改以 `_evolution_capital_base()` 種子，避免 `initial_balance=0` 印出錯誤 MaxDD%。

### Monte Carlo：發現 `kwargs.get` / instance 數值參數（#129）

AI `**kwargs` 策略先前 MC 參數發現為空（no-op）。現合併 signature、`kwargs.get` 字面量與（必要時）instance 數值屬性。

---

## 新功能 Features

### Headless CLI：無 GUI 部署／停止／查詢／回測（#126）

`run_bot_cli.py`（deploy | stop | list | status | strategies | backtest）在隱藏 Tk 上跑與 workbench 相同的 live 路徑；含 `--detach --wait-ready` 與 `.claude/skills/deploy`。`semi_auto` 仍須 workbench。

---

## Fixes (English)

### Evolution plan: omit numeric holdout criteria from the prompt (#133)

**Problem**: Example JSON bars (PF≥1.2 / WR≥45%) were copied into `directives["criteria"]`, FAILing Path A candidates that already cleared #125 floors (e.g. TMF00_0422 2026-09-19).

**What changed**: Machine-readable trailer is action-only; model must not emit numeric holdout criteria so the default not-worse-than-baseline path applies. #125 hard floors unchanged.

### Design-report MaxDD% uses capital_base (#128)

AI design-window report seeds `calculate_metrics` from `_evolution_capital_base()` so MaxDD% matches fitness.

### Monte Carlo discovers kwargs.get / instance numeric params (#129)

`**kwargs` AI strategies no longer get empty MC discovery (was a no-op). Merges signature defaults, `kwargs.get` literals, and instance attrs when needed.

---

## Features (English)

### Headless CLI for deploy/stop/inspect/backtest (#126)

`run_bot_cli.py` runs the same live machinery as the GUI on a withdrawn Tk root, with detached ready-wait and the `deploy` skill. `semi_auto` still requires the workbench.

---

## 亦含 Also in this tag

- test(evolution): pin realistic deep AND-chain PASS e2e fixture (#130) — tests only; no runtime change for operators.

---

## 升級注意 Upgrade notes

- 需重新啟動機器人才會採用 #133/#128/#129 演化邏輯（週六 05:05 演化使用執行中程式）。Restart running bots to adopt evolution plan/MC/report fixes (Saturday 05:05 evolution runs whatever code is loaded).
- Headless CLI 隨此 tag 進 release 包；既有 GUI 部署路徑行為不變。Headless CLI ships in this tag; existing GUI deploy path unchanged.
