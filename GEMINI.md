# GEMINI.md — tai-robot 新聞訊號模組上下文與工程鐵則

> 本檔案規範 `tai-robot` **W3 新聞訊號四層漏斗管線**的領域脈絡與工程原則。
> 全專案通用安全限制、變更與審查合約（P1/P2/P3、證據門檻、PR 格式、發布流程）一律以 [CLAUDE.md](CLAUDE.md) 與 [AGENTS.md](AGENTS.md) 為單一準則來源。

## 1. 專案定位與三大核心痛點

- **專案定位**：`tai-robot` 為串接群益 API（SKCOM）之台指期（TXF / MTX / TMF）交易機器人。台指期高度連動台積電（2330 / TSM ~30% 權重）、聯發科（2454）、台灣 AI 伺服器代工、美股 AI 權值股（NVDA、AVGO、MU、SK 海力士、MSFT、GOOGL、AMZN、META、ORCL）、費城半導體指數（SOX）及美國總經利率與信用環境。
- **新聞訊號在系統中的角色**：W3（`scripts/news_bridge/rss_scorer.py`）在交易主機 Steven 以排程 `--once` 每 30 分鐘執行，透過檔案合約（`regime_vote_w3.json` 與可選 opt-in `signal.json`）與 `RegimeSwitchingRunner` 協作；**任何新聞訊號皆不可繞過 SKCOM 即時報價與 `TradingGuard` 獨立下單**。
- **既有 v1 新聞訊號三大痛點（由 Steven 8,088 篇真實日誌驗證）**：
  1. **訊號重複**：跨媒體改寫、中英文翻譯改寫、多段 `|` 後綴繞過精確標題比對，導致同一事件在 1–4 小時內重複灌入 EMA（v1 僅濾掉 3% 重複）。
  2. **訊號太雜**：缺乏事件白名單與來源分級；Investing.com（`All News`）微盤股 Form 4／法說逐字稿／分析師目標價佔逾半數篇幅，Google News 更帶入散戶論壇（`股市爆料同學會`、`Facebook`）與八卦／業配文。
  3. **純報漲跌點數（無預測力）**：大量「台股重挫 N 點」、「美股收黑」、「盤中速報」、「三大法人買賣超」等回顧性價格摘要（Price Recap Only），在 v1 中高達 95% 中文純漲跌稿被評為非中性，污染訊號且落後 SKCOM 即時報價。

## 2. 高價值催化劑白名單（Event Taxonomy）

僅放行以下 **4 類具定價權的實質催化劑**，其餘一律標記為 `DROP` 並丟棄：

1. **`MACRO_RATES`（總經與利率驚喜）**：Fed FOMC 決議與點陣圖、主席談話（Kevin Warsh）、美國 CPI／PCE／非農就業意外值、美國 10 年期公債殖利率突破或回落關鍵門檻（如 5.0%）、原油／能源衝擊（伊朗／Brent／WTI）、台灣央行政策。
2. **`AI_SUPPLY_CHAIN`（AI 權值與供應鏈催化）**：
   - 晶圓與晶片：台積電（月營收、法說會、毛利率、2nm／CoWoS 產能交期、16nm 出口限制）；NVDA、AVGO、MU、SK 海力士、Dell 財報、財測、毛利率與產品爬坡（Vera Rubin、NVL72、HBM4）。
   - 雲端與電力瓶頸：雲端巨頭（MSFT、GOOGL、AMZN、META、ORCL）CapEx 指引上修／下修、自由現金流轉折、RPO／ARR；實體電力與散熱瓶頸（GE Vernova、Vertiv、Eaton、電網併網）；模型層商業化（Anthropic、OpenAI）。
   - **TXF 專屬判讀原則**：必須區分「供應商轉單（Mix Shift）」與「算力需求崩壞」（例如 Google 將 TPU 訂單由博通轉向聯發科時仍由台積電代工，應以對台灣半導體權值與 TXF 之淨影響評分）。
3. **`GOVERNANCE_REGULATORY`（治理黑天鵝與監管政策）**：核心權值股高層突發異動、SEC 會計審查、10-Q 季報延遲或補交釐清、司法傳票；美國商務部 BIS 晶片與遠端算力出口管制；美國聯邦或關鍵州（德州 ERCOT／TCEQ）資料中心暫停令（Moratorium）或電網許可凍結／解封；台灣市場防禦機制（金管會限空令／禁空令、國安基金、TAIFEX／TWSE／KOSPI 熔斷或暫停交易、大規模違約交割）。
4. **`CREDIT_FLOWS`（信用債市警訊與機構籌碼異動）**：科技巨頭停買庫藏股、大規模發債或 ATM 股權增資（GOOGL、META、ORCL、MSFT）；AI 基礎設施債市壓力（CoreWeave 無擔保債殖利率與 GPU 擔保貸款 DDTL 分化、Oracle 與巨頭 5 年期 CDS 利差劇變或收斂）；機構部位極端切換（美銀 FMS 調查、SOXX／SMH／SOXL／DRAM／QQQ 異常鉅額流向）。
5. **`DROP`（丟棄）**：純行情點數漲跌回顧、技術面事後解盤、例行三大法人／外資買賣超（由 W4 籌碼橋接負責）、非權值中小型個股、分析師目標價／評級、論壇貼文與無新事實數據的評論稿。

## 3. 四層漏斗架構（Filter → Deduplicate → Classify → Route）

- **Layer 1（[prefilter.py](src/news/pipeline/prefilter.py)）**：URL 正規化、多輪剝除媒體前後綴、來源分級、硬規則 Regex 攔截論壇來源／微盤股模板噪音／期交所行政公告／純行情回顧（具明確因果催化劑保護詞除外）與非核心實體離題稿。
- **Layer 2（[dedup.py](src/news/pipeline/dedup.py)）**：三段式即時增量去重——前置 URL／標題指紋／64-bit SimHash → 1–4 小時滑動時間窗 $O(k)$ 增量 Centroid 餘弦聚類（嚴禁全量歷史重跑 DBSCAN；預設 Gemini `batchEmbedContents` 跨語系對齊，離線自動降級純 Python Lexical 向量）→ 單一代表稿與 `mention_count` 累加，並將 4–48 小時過期群組歸檔以提供 Layer 3 跨日脈絡判別 `has_new_fact_delta`。聚類與向量狀態獨立存放於旁車檔 `rss_pipeline_v2_state.json`，避免膨脹主狀態檔 `rss_scorer_state.json`。
- **Layer 3（[classifier.py](src/news/pipeline/classifier.py)）**：僅對新事件代表稿（或未路由 Tier 3 升格為 Tier 1/2 者）呼叫 LLM，嚴格驗證 12 欄位 JSON Schema（`event_id`, `is_duplicate`, `is_price_recap_only`, `has_new_fact_delta`, `category`, `entities`, `direction`, `materiality`, `horizon`, `source_tier`, `mention_count`, `summary_one_line`），強制以 Layer 2 鎖定權威 `source_tier`；解析失敗一律 fail-closed 不觸發任何投票或下單。
- **Layer 4（[router.py](src/news/pipeline/router.py)）**：
  - 嚴格 5 條件放行閘門：`!is_price_recap_only && !is_duplicate && has_new_fact_delta && category != "DROP" && materiality >= 4`。
  - 來源分級權重（Tier 1 > Tier 2 > Tier 3；Tier 3 單獨不可觸發投票或斷路器）。
  - **多空非對稱門檻與投票遲滯**：BEARISH `materiality >= 4`（Tier 1/2）立即放行防禦投票／快訊（門檻 `-0.85`）；BULLISH 須 `materiality == 5` 且通過 W2／SKCOM 新鮮量價與區域股指矛盾檢查（門檻 `+0.85`）；搭配 10 小時 EMA 半衰期與 `0.45` 撤票遲滯下限（含零新稿輪次衰減撤票）。
  - 預設維持投票制（`regime_vote_w3.json`），`signal.json (risk_off, severity: "high")` 需明確設定 `news.rss_emit_risk_off: true` 才啟用。

## 4. 工程鐵則

- **外部化設定**：所有門檻、Regex 規則、白名單、實體表與來源權重一律集中於 [config/news_filter.yaml](config/news_filter.yaml)，禁止寫死於業務邏輯。
- **零重型依賴**：管線維持純 Python stdlib + `pyyaml`，相容 Steven CPU 短命 `--once` 排程與既有儀表板（`scripts/dashboard/news_*.py`）與監控腳本（`scripts/monitor/check_bridge.py`）。
- **預設啟用與一鍵回退**：`settings.yaml` 的 `news.rss_pipeline_v2` 預設為 `"live"`（正式啟用四層漏斗），保留 `"off"`（緊急一鍵退回 v1）與 `"shadow"` 模式；既有歷史日誌可透過 [backfill_news_v2.py](scripts/news_bridge/backfill_news_v2.py) 重建以對齊儀表板顯示。詳見 [docs/plans/news-signal-refactor-plan.md](docs/plans/news-signal-refactor-plan.md)。
