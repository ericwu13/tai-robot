# v2.19.0

## 修正 Fixes

### 演化流程：計畫幾乎永遠回答「不修改」的根本原因（#114）

**問題**：演化提示詞的規則「交易少於 30 筆 → 繼續收集數據，不修改」，被模型套用在**只包含上次演化之後新增交易**的清單上（例如 09-12 的清單只有 3 筆，但設計窗其實有 130 筆；issue #94 是 1 筆對 14 筆）。而水位線（watermark）在工作執行緒啟動後立即前進，不論計畫結果為何，所以這些交易永遠不會再被送出。結果：13 週的週六演化只在 4 個週六產生過候選策略，全部 FAIL。

**變更**：樣本大小規則改為明確以**設計窗總筆數**判定，並說明下方清單只是「差異清單」，清單短不是「不修改」的理由。門檻值（30）不變。

---

### 演化驗證閘門：回撤比較改用絕對值（#114）

**問題**：演化回測沒有起始資金，最大回撤百分比是用「累積損益高點」當分母 —— 一路虧損的窗口高點為 0，回撤被記成 0.00%（最好的分數）；高點很小的窗口則爆到 100–226%。以此比較基準與候選策略，等於在基準虧損的那兩週把所有候選一律否決，其他週則幾乎不設防。

**變更**：所有「基準 vs 候選」的回撤閘門（預設守門、設計窗崩潰比、保留測試集比）改比較**絕對回撤金額**（同一批 K 棒、永遠有定義）。計畫自訂的 `max_drawdown_pct_max` 準則仍用百分比。驗證報告同時顯示絕對值與百分比。

---

### 演化評分：新增資金基準 `evolution.capital_base_twd`（#119）

**問題**：適應度評分（fitness composite）的回撤分項同樣使用「高點為分母」的回撤百分比：一路虧損的策略拿到滿分回撤分數，勉強獲利的策略反而拿 0 分，排序完全顛倒。

**變更**：`settings.yaml` 新增 `evolution.capital_base_twd`（預設 100,000 新台幣），作為演化評分與驗證回測的**起始權益**，回撤百分比從此是真正的「佔資金比例」。填 0 或負數退回預設值。一般回測不受影響。既有 `evolution.json` 的 best_composite 是舊尺度，下次執行會重新建立基準一次。

---

### 演化候選策略：強制類別名稱（#114）

**問題**：程式碼沙箱回傳原始碼中第一個策略類別，不檢查名稱；若模型沿用原策略的類別名稱，PASS 時會以該名稱儲存，**覆寫正在運行策略的原始碼**與下拉選單登錄。

**變更**：候選類別名稱不符時視為產生失敗，用既有的一次重試要求模型修正。

---

### 續跑對帳：等待未平倉快照並要求方向一致（#113）

**問題**：部署前的實際持倉查詢只等 0.5 秒，OnOpenInterest 回呼卻約 4 秒後才到，導致「帳戶有持倉」警示未跳出、續跑對帳誤判為空倉（2026-09-11 實際發生：帳戶持有空單 1 口）。

**變更**：等待未平倉快照真正抵達後再對帳，且對帳要求方向一致。

---

### 監控腳本：check_evolution 誤報「週六排程沒有到達此機器人」（#115）

**變更**：水位線過期的判定會先查看該機器人是否有當週的演化啟動行；有啟動行（保留測試集跳過，屬設計行為）只列為資訊，不再發 P2。

---

### 每週檢視技能：納入 GitHub issue 掃描與演化成效稽核（#116）

**變更**：`weekly-review` 每次執行必掃 `gh issue list`，分類與演化相關的 issue（含其他使用者附上的 bug bundle 與貼上的工作臺紀錄），並新增 Stage 2b 演化**成效**稽核（嘗試紀錄、判決訊號、8 週損益、跨使用者證據；連續無候選或持續虧損時觸發兩個獨立稽核 agent）。`evo-debug` 新增從 debug log 的 `msg_len` 直接判定一次演化到達哪個階段的對照表。

---

## Fixes (English)

### Evolution: the real reason plans almost always said "no change" (#114)

**Problem**: The plan prompt's rule "fewer than 30 trades → continue collecting data, no change" was applied by the model to a trade list that contains ONLY trades new since the previous run (3 of 130 on 09-12; 1 of 14 in issue #94). The watermark advances as soon as the worker thread starts, whatever the plan says, so those trades are never shown again. Result: 13 weeks of Saturday runs produced a candidate on only 4 Saturdays, all FAIL.

**What changed**: The sample-size rule is now stated against the DESIGN-WINDOW total and explains that the list below is a delta; a short list is no longer a reason for no_change. The threshold (30) is unchanged.

---

### Evolution gates: drawdown compared in absolute terms (#114)

**Problem**: The evolution backtests ran with no starting equity, so drawdown % used the peak of cumulative P&L as its denominator — 0.00% (the best possible value) for a window that never went positive, 100–226% for a tiny peak. Comparing baseline vs candidate on that number rejected every candidate in a losing-baseline fortnight and was trivially loose otherwise.

**What changed**: Every baseline-vs-candidate drawdown gate (default guard, design-window collapse ratio, holdout ratio) now compares ABSOLUTE drawdown (same bars, always defined). The plan's own `max_drawdown_pct_max` criterion stays on percent. Reports show both.

---

### Evolution fitness: new capital base `evolution.capital_base_twd` (#119)

**Problem**: The fitness composite's drawdown sub-score used the same peak-based percentage: a sinking strategy scored a perfect drawdown, a barely-profitable one scored zero — the ordering was inverted.

**What changed**: New `evolution.capital_base_twd` (default 100,000 TWD) is the starting equity for the fitness composite and the validation backtests, so drawdown % is a real fraction of capital. Zero/negative falls back to the default. Ordinary backtests are unchanged. Stored `evolution.json` best_composite values are on the old scale and re-baseline once on the next run.

---

### Evolution candidates: class name enforced (#114)

**Problem**: The code sandbox returned the first strategy class in the generated source regardless of its name; a model that kept the base class name would, on PASS, overwrite the LIVE strategy's stored source and registry entry.

**What changed**: A name mismatch counts as a generation failure and spends the existing retry correcting it.

---

### Resume reconcile waits for the OpenInterest snapshot and requires direction match (#113)

**Problem**: The pre-deploy real-position query waited 0.5 s while the OnOpenInterest callback arrived ~4 s later, so the "existing position" warning did not fire and the resume reconcile logged "real account flat" while the account held SHORT 1 (2026-09-11).

**What changed**: Reconcile waits for the snapshot to actually land and requires the direction to match.

---

### Monitor: check_evolution false P2 "slot is not reaching this bot" (#115)

**What changed**: The stale-watermark rule now consults the bot's start line for the due slot; a fired-then-skipped bot (holdout skip, by design) is informational, not a P2.

---

### Weekly-review skill: GitHub issue sweep + evolution efficacy audit (#116)

**What changed**: `weekly-review` now always runs `gh issue list`, classifies evolution-related issues (including other users' bug bundles and pasted chat logs), and adds Stage 2b — an evolution EFFICACY audit (attempt history, verdict signature, 8-week P&L, cross-user evidence) that launches two independent review agents when evolution stays silent or bots keep losing. `evo-debug` gains a table to classify a run from the debug log's Discord `msg_len` alone.

---

## 升級注意 Upgrade notes

- 需重新啟動機器人才會採用新程式碼（週六 05:05 的演化使用執行中的程式）。Restart running bots to adopt this release (the Saturday 05:05 evolution slot runs whatever code is loaded).
- `settings.yaml` 可選新增 `evolution.capital_base_twd`；未設定即為 100000。Optional new key `evolution.capital_base_twd`; defaults to 100000 when absent.
