# W3 新聞訊號管線重構計畫與實證驗證報告（Pipeline v2）

> 本文件記錄 `tai-robot` W3 新聞訊號模組（`scripts/news_bridge/rss_scorer.py`）由 v1 全面重構為**四層漏斗管線（Filter → Deduplicate → Classify → Route）**的診斷證據、架構設計、Steven 實機校正項目、離線重播基準與分階段上線手冊。
>
> 領域原則與工程鐵則同步收錄於 [GEMINI.md](../../GEMINI.md)；通用變更與審查合約以 [CLAUDE.md](../../CLAUDE.md) 與 [AGENTS.md](../../AGENTS.md) 為單一準則。

---

## 1. Phase 0 — 現況診斷與 Steven 實機資料實證（8,088 篇真實日誌 + 632 次排程執行）

透過分析實機主機（Steven）匯出之完整橋接日誌（`news_log.jsonl` 7,311 筆 + `news_log_202611.jsonl` 777 筆 = **8,088 篇**、`runs/W3.jsonl` **632 次**執行紀錄、`regime_history.csv` **68 次**夜盤分類與實盤 `monitor.log`），確認既有 v1 管線存在以下結構性缺陷：

| 痛點類別 | Steven 實證數據 | 對台指期（TXF/TMF）交易的實質危害 |
|---|---|---|
| **1. 訊號重複** | v1 精確標題比對僅濾除約 **3%** 重複；同一事件（如限空令、台積電 16nm 管制、Anthropic IPO）跨 5–70 篇媒體重複改寫灌入 EMA | 2026-11-19 單一 Anthropic IPO 題材被 76 篇改寫稿灌爆，將 W3 推至 `+5.15`，隨後市場重挫 `-10.95%` |
| **2. 訊號雜訊** | Investing.com（`All News`）微盤股 Form 4 內部人申報、法說逐字稿、分析師目標價與離題個股佔輸入量逾半；Google News 帶入 `股市爆料同學會`、`Facebook` 等論壇來源 | 每輪呼叫 Gemini 40–80 次（p95 耗時 135.3s，最高 296s／77 篇），浪費逾 75% API 額度並稀釋高權重事件 |
| **3. 純報漲跌（回顧性噪音）** | 95% 中文純漲跌稿（`【台股開盤】`、`盤中速報`、`外資買賣超`）在 v1 被評為非中性 | `regime_history.csv` 68 次夜盤實證顯示 `W3 trending-up` 同步命中率 64.7%，但 **次日預測命中率僅 35.3%（平均 `-662` 點）**、2 日命中率 25.0%，證實為落後追高陷阱 |
| **4. 排程可靠性** | `runs/W3.jsonl` 632 次執行中 630 次成功，僅有 2 次失敗皆為 Gemini `HTTP 503` 暫時性錯誤 | 無重試機制且失敗時若未回滾聚類狀態，會導致復原後同事件誤判為重複 |
| **5. W2 報價新鮮度盲區** | `monitor.log` 1,680 次平日輪詢顯示 08:00–23:59 TPE 100% 新鮮，但每日 CME 休市 06:20–07:10 TPE 為 0/11 新鮮；另 14:00 TPE `^TWII` 收盤後港股 `^HSI` 仍可能大跌 `-3.03%` | 僅讀 `fresh` 旗標不檢查檔案 `asof` 年齡，若 W2 中斷將放行過期報價；未檢查區域股指矛盾可能誤放多頭確認 |

---

## 2. 四層漏斗架構設計（Phases 1–4）

所有規則、來源權重、白名單、實體表與路由門檻皆外部化於 [config/news_filter.yaml](../../config/news_filter.yaml)，實作位於 [src/news/pipeline/](../../src/news/pipeline/__init__.py)（純 Python stdlib + `pyyaml`，零重型 ML 依賴）。

```
RSS Fetch (13 default feeds)
   │
   ▼
[Layer 1] NewsPreFilter (prefilter.py)
   • URL 正規化（去 UTM/fbclid/hash）與多輪剝除媒體前後綴
   • 來源分級（Tier 1 權威 / Tier 2 主流 / Tier 3 社群次級；優先比對媒體名稱）
   • 硬規則攔截：論壇黑名單、微盤股模板（Form 4/逐字稿/分析師目標價）、期交所行政公告、
     純漲跌回顧（具明確因果催化劑保護詞除外）、離題非核心實體
   │
   ▼
[Layer 2] IncrementalEventClusterer (dedup.py)
   • Stage 1：正規化 URL / 標題指紋 / 64-bit SimHash（Hamming ≤ 6）即時比對
   • Stage 2：1–4 小時滑動時間窗 O(k) 增量 Centroid 餘弦聚類
     - 預設 Gemini Embedding REST API（batchEmbedContents，256-dim，支援跨語系 EN↔ZH 對齊）
     - 離線或 API 異常自動無縫降級純 Python Lexical 向量
   • Stage 3：單一代表稿 + mention_count 累加；未路由 Tier 3 群組升格 Tier 1/2 觸發補分類；
     過期群組歸檔 4–48 小時提供跨日事件脈絡（PriorEventContext）
   • 狀態隔離：群組與向量單獨存放於旁車檔 `rss_pipeline_v2_state.json`（不膨脹 `rss_scorer_state.json`）
   │
   ▼
[Layer 3] GeminiEventClassifier (classifier.py)
   • 僅對新事件代表稿（與升格代表稿）呼叫 LLM，內建 429/5xx 暫時性重試
   • 嚴格驗證 12 欄位 JSON Schema + 強制以 Layer 2 鎖定權威 source_tier
   • 四類高價值催化白名單：MACRO_RATES / AI_SUPPLY_CHAIN / GOVERNANCE_REGULATORY / CREDIT_FLOWS（其餘 DROP）
   • 任何 JSON 解析失敗或 Schema 違規一律 fail-closed（丟棄且不投票、不觸發風控）
   │
   ▼
[Layer 4] SignalRouter (router.py)
   • 嚴格 5 條件放行閘門：!is_price_recap_only && !is_duplicate && has_new_fact_delta && category != "DROP" && materiality >= 4
   • 來源權威：Tier 3 單獨不可觸發投票或斷路器（僅 ADVISORY_LOW_TIER）
   • 非對稱多空門檻：
     - BEARISH：materiality >= 4（Tier 1/2）立即放行防禦投票（門檻 -0.85）
     - BULLISH：materiality == 5 且通過 W2/SKCOM 量價確認（含 asof ≤ 1800s 新鮮度檢查與 ^HSI/^N225/^KS11/ASML.AS 區域矛盾檢查，門檻 +1.00）
   • 遲滯與衰減（Hysteresis）：10h EMA 半衰期 + 0.45 撤票下限，確保單一去重催化事件可存活至 ~04:58 TPE 夜盤分類；無新文章時亦照常衰減並在跌破下限時撤票
   • 預設僅輸出 `regime_vote_w3.json`；opt-in `news.rss_emit_risk_off: true` 時寫入 `severity: "high"` 之 `signal.json`（保留 `conflicting_leg` 不誤砍獲利空單之特性）
```

---

## 3. Steven 實機資料驅動之 P0 / P1 關鍵校正

分析完整 Steven 封包後，對初始設計做出以下 7 項實證校正（全數已實作並納入自動化測試）：

1. **[P0] 單一去重催化事件過早自毀（EMA 2h → 10h + 0.45 撤票遲滯）**：`RegimeSwitchingRunner` 每日僅於 ~04:58 TPE 讀取一次 `regime_vote_w3.json`，而 v2 去重使單一衝擊在 4–48h 內僅計分一次。原 2h 半衰期會使 T2 M4 衝擊在 1 小時內自刪；改為 `ema_half_life_hours: 10.0` + `vote_withdraw_threshold: 0.45` 後，T2 M4 可穩定存活 ~9.2h、T1 M5 ~14.7h，且於 `guid_new == []` 靜默輪次仍正常衰減撤票，杜絕殭屍票。
2. **[P0] 狀態檔膨脹隔離（旁車檔 `rss_pipeline_v2_state.json`）**：壓測顯示滿載 400 active + 600 archived 之 256-dim 群組 JSON 約 ~2.0 MB；抽離至旁車檔並對歸檔群組採用精簡格式與 3 位小數向量後，`rss_scorer_state.json` 維持精簡，不影響 Dashboard / GUI / `check_bridge` 輪詢。
3. **[P0] Dashboard 門檻同步（`scripts/dashboard/news_pulse.py`）**：當 `pipeline_v2_mode == "live"` 時，儀表板自動改用 v2 非對稱門檻（`-0.85` / `+1.00`）與 `v2_active_vote` 遲滯狀態，避免 `-0.90` 已投票卻在畫面顯示「中性・距門檻 0.60」；`off` / `shadow` 模式行為 100% 不變。
4. **[P1] W2 報價 `asof` 新鮮度與區域股指矛盾檢查**：`load_market_snapshot` 驗證 `crossmarket_quotes.json` 之 `asof` 年齡（上限 `1800s`），並新增 `bullish_contra_symbols`（`^N225`、`^KS11`、`^HSI`、`ASML.AS`，跌幅 `<= -1.5%` 即擋下多頭確認）。
5. **[P1] Layer 1 白名單與純漲跌校正**：自 `causal_protection_whitelist` 移除裸 `\bIPO\b`（防止 Anthropic IPO 行情稿繞過攔截），增補 `台積電ADR\d+日(上漲|下跌)` 與台股純報漲跌模板，並補齊 BIS 遠端算力封鎖、美國非農就業、關稅、台幣匯率、BOJ、Neocloud 等核心關鍵字。
6. **[P1] Shadow 模式可觀測性**：`[v2 shadow]` 輸出 `emb=`、`fb=`、`schema_fail=`、`err=` 至標準輸出日誌，並將 API／Embedding 降級訊息寫入 `pipeline_v2_shadow_error`。
7. **[P1] Opt-in `risk_off` 嚴重度改為 `"high"`**：實盤曾因 `suppress_scope: "both"` / `"critical"` 在暴跌前強制平倉獲利空單；`write_risk_off_signal` 改發 `"severity": "high"`，使 `conflicting_leg` 僅封鎖衝突多單而不平倉空單（在 `both` 模式下依然雙向保護）。

---

## 4. Phase 5 — 離線重播基準與測試驗證

使用 [scripts/news_bridge/replay_news_pipeline.py](../../scripts/news_bridge/replay_news_pipeline.py)（離線 Lexical 模式，無網路呼叫）對真實資料與測試樣本進行定量回測：

| 指標 | Steven 真實日誌（8,088 篇 / 523 批次） | Repo XML 測試樣本（170 篇） | 目標基準 |
|---|---:|---:|---:|
| **Layer 1 攔截率** | **78.5%**（6,351 / 8,088 篇） | **80.6%**（137 / 170 篇） | ≥ 60% |
| **純漲跌回顧攔截數** | **317 篇**（3.9% 輸入量） | **14 篇**（8.2% 輸入量） | 有效攔截 |
| **Layer 2 離線去重率** | **7.7%**（134 篇；跨日 48h 脈絡命中 66 次） | **3.0%**（1 篇） | 實盤搭配 Gemini Embedding 跨語系進一步收斂 |
| **總 LLM 呼叫節省率** | **80.2%**（節省 6,485 / 8,088 次） | **81.2%**（節省 138 / 170 次） | ≥ 65% |
| **L1+L2 平均延遲** | **1.290 ms / 篇**（總計 10.43s） | **0.285 ms / 篇**（48.5 ms） | < 50 ms / 篇 |
| **單元與整合測試** | **901 passed / 0 failed**（11.63s） | 含 `test_news_pipeline.py` 與全部既有 W3/Bridge/Dashboard/Regime 測試 | 100% 通過 |

---

## 5. 上線步驟、歷史回補與一鍵回退（Rollout）

### 新增設定鍵（均位於 `settings.yaml` 的 `news:` 區塊）

| 設定鍵 | 預設值 | 說明 |
|---|---|---|
| `news.rss_pipeline_v2` | `"live"` | `"live"`（預設，直接啟用四層漏斗）/ `"shadow"`（平行紀錄不改寫投票）/ `"off"`（緊急一鍵退回 v1） |
| `news.rss_filter_config` | `""` | 覆寫過濾設定檔路徑；留空則載入 repo 內 `config/news_filter.yaml` |
| `news.rss_embedding_provider` | `""` | 覆寫 Layer 2 向量來源（`gemini` / `lexical` / `none`）；留空依 YAML 預設 `gemini`（自動降級 `lexical`） |
| `news.rss_emit_risk_off` | `false` | 明確 opt-in 才允許高衝擊利空（Tier 1/2 M4+ 且通過市場確認）寫入 `signal.json`（`severity: "high"`）；預設維持投票制 |

### Steven 上線與 Dashboard 歷史資料回補步驟

1. **直接啟用 Live**：部署後無需改動 Steven 現有 `settings.yaml`（`rss_pipeline_v2` 預設 `"live"`，既有 `rss_feeds: []` 與其他鍵均無覆蓋衝突）；下一輪排程起即以四層漏斗寫票並建立旁車檔 `rss_pipeline_v2_state.json`。
2. **歷史資料回補（讓 Dashboard 完整呈現 v2 歷史曲線與事件表）**：
   - 先免費預覽需要呼叫幾次 Gemini（不花額度、不改檔）：
     ```bash
     python scripts/news_bridge/backfill_news_v2.py --settings settings.yaml --dry-run
     ```
   - 執行正式回補並同步更新 Dashboard 與 W3 狀態（自動先將舊 v1 日誌備份至 `backup_v1_<timestamp>/`，且內建 JSONL 快取支援中斷續跑）：
     ```bash
     python scripts/news_bridge/backfill_news_v2.py --settings settings.yaml --bots-dir data/live --in-place --update-state
     ```
3. **一鍵回退（Rollback）**：
   - 任何時候於 `settings.yaml` 設定 `news.rss_pipeline_v2: "off"` 即可立即切回 v1；`rss_scorer.py` 會自動更新 `pipeline_v2_mode: "off"` 並還原 Dashboard `±1.5` 顯示，歷史 v1 備份亦完整保留於 `backup_v1_*` 目錄。
