# v2.18.0

## 新功能 Features

### 新聞／事件交易框架：逐機器人啟用，不再是全域開關

**問題**:`news.enabled` 原本是全域設定,一旦開啟,每一台 regime 機器人都會掛上斷路器 —— 一次 `risk_off` 會把對照組（baseline）機器人也一起平倉,A/B 測試無從進行。

**變更**:啟用權移到部署對話框,和 regime switching 的作法一致。「多空切換 Regime Switching」區塊下新增三個核取方塊:

- 📰 新聞斷路器 News circuit breaker（此機器人 this bot only）
- Tier 2 強制進場（斷路器關閉時為灰階不可選）
- 方向性暫停 directional risk_off —— 只擋衝突方向

`settings.yaml` 的 `news.enabled` / `news.tier2_enabled` / `news.suppress_scope` 從此只是**對話框的預設值**;路徑類設定（`signal_path` / `events_path` / `ledger_path` / `regime_vote_path`）仍為所有機器人共用,因為它們描述的是 n8n 產生的檔案本身。每台機器人自己的選擇記在它的 `session.json`,續跑時以 `session.json` 為準。

**使用者看到**:同一台電腦可以一邊跑開啟新聞框架的機器人,一邊跑完全未受影響的對照組。

---

### News / event trading framework: per-bot enablement, no longer a global switch

**Problem**: `news.enabled` used to be a global gate — turning it on gave every regime bot the circuit breaker, so a single `risk_off` flattened the baseline bot too and destroyed the A/B comparison.

**What changed**: Enablement moved into the deploy dialog, following the regime-switching pattern. Three checkboxes now live inside the 「多空切換 Regime Switching」 reveal:

- 📰 新聞斷路器 News circuit breaker (this bot only)
- Tier 2 forced entry (greyed out until the breaker is on)
- 方向性暫停 directional risk_off — gate only the conflicting leg

`news.enabled` / `news.tier2_enabled` / `news.suppress_scope` in `settings.yaml` are now **dialog defaults only**; the path settings (`signal_path` / `events_path` / `ledger_path` / `regime_vote_path`) are still shared by every news-enabled bot, because they describe the n8n files themselves. Each bot's own choice is recorded in its `session.json` and wins on resume.

**What the user sees**: A news-enabled bot and an untouched baseline bot can now run side by side on the same machine.

---

### 新聞斷路器：risk_off 平倉、行事曆閘門、Tier 2 事件強制進場

**變更**:外部 n8n 工作流程寫出兩個 JSON 檔,機器人每 30 秒輪詢一次:

- `signal.json` —— 未排程風險。`risk_off` 立即以 `FORCE_CLOSE`（tag `news_risk_off`）平倉並封鎖進場;`clear` 解除;`deploy_short` / `deploy_long` 為 Tier 2,切換到「事件強制放空／做多」策略。透過 `SignalLedger`（`ledger_path`）保證**恰好執行一次**,重開機也不會重複觸發。
- `events.json` —— 排程風險行事曆（CPI、FOMC 等）。在場次開盤前的收盤空檔判斷是否整場退出,門檻由 `news.calendar_min_severity` 決定。

所有決策邏輯集中在 `src/news/circuit_breaker.py` 的純函式規劃器(planner),runner 只是執行回傳的動作清單。兩個獨立的封鎖來源:`clear` 只解除訊號封鎖、行事曆只解除事件封鎖,所以在 FOMC 場次手動按下 `clear` 不會解開行事曆閘門。行事曆閘門在**有部位時延後生效**,等該筆交易出場後的第一次輪詢才啟動 —— 封鎖絕不可以凍結一個開著的部位的停損管理。

事件強制進場策略(`src/strategy/examples/news_event.py`)不判斷型態:暖機完成後無條件進場一次,之後只做 ATR 停損 + 時間停損,出場後永不再進場。它只能由斷路器部署,不會出現在 regime 多空腳的下拉選單裡。

所有檔案都視為**不受信任的輸入**:解析失敗、版本不符、內容為垃圾,一律降級為「沒有訊號」而不是拋出例外 —— 壞掉的自動化流程絕對不能讓交易永久停擺。

---

### News circuit breaker: risk_off flatten, calendar gate, Tier 2 forced entry

**What changed**: An external n8n workflow writes two JSON files that the bot polls every 30 seconds:

- `signal.json` — unscheduled risk. `risk_off` flattens immediately via `FORCE_CLOSE` (tag `news_risk_off`) and blocks entries; `clear` releases it; `deploy_short` / `deploy_long` are Tier 2 and swap in the news-event forced-entry strategy. `SignalLedger` (`ledger_path`) gives **exactly-once** semantics across both the 30s poll loop and process restarts.
- `events.json` — the scheduled-event calendar (CPI, FOMC, ...). Evaluated during the closed gap before a session opens, so a whole session can be sat out; `news.calendar_min_severity` sets the bar.

Every branch lives in the pure planner in `src/news/circuit_breaker.py`; the runner just executes the returned action list. Two independent suppression sources: `clear` releases only the signal source and the calendar releases only the event source, so a `clear` tapped during an FOMC session cannot un-gate the event. The calendar gate **defers while a position is open** and engages on the first poll after that trade exits — suppression must never freeze an open position's stop management.

The forced-entry strategy (`src/strategy/examples/news_event.py`) hunts no setup: once warmed up it enters unconditionally, exactly once, then manages only an ATR stop plus a time stop, and never re-enters. It is breaker-only and is filtered out of the regime leg dropdowns.

Every file is treated as **untrusted input**: a missing file, bad JSON, wrong version or junk entries all degrade to "no signal" instead of raising — a broken automation must never be able to halt trading indefinitely.

---

### n8n 新聞橋接 W1–W5：五條資料管線

**變更**:新增 `scripts/news_bridge/`（純標準函式庫為主）與可直接匯入本機 n8n 的工作流程 JSON。

| # | 腳本 | 頻率 | 產出 |
|---|------|------|------|
| W1 | `calendar_bridge.py` | 每日 | 經濟數據行事曆風險視窗 |
| W2 | `crossmarket_monitor.py` | 美股時段每 2 分鐘,其餘每 10 分鐘 | `signal.json` 斷路器 + `regime_vote_w2.json` |
| W3 | `rss_scorer.py` | 每 30 分鐘 | `regime_vote_w3.json`（RSS + Gemini 情緒） |
| W4 | `chips_monitor.py` | 每日 16:15 TPE（含 4/18/20/22 補跑） | `regime_vote_w4.json`（法人資金流） |
| W5 | `n8n/W5_manual_tap.json` | 人工點擊 | `signal.json`（Discord 連結觸發 webhook） |

- **W2 跨市場監控**:從 3 檔美股標的擴充到 11 檔全球標的,分成 signal / alert 兩層。每檔標的有自己的新鮮度容許值（美股 600 秒、CME 900 秒、亞歐延遲報價 1500 秒）—— 舊版單一 600 秒的門檻會默默丟棄所有非美股報價。新增 `^HSI` / `^TWII` / `000001.SS` 後,台股白天時段的有效投票者從 2/8 提升到 5-6 檔。
- **W3 RSS + Gemini 評分**:抓取國際與台灣新聞來源（Reuters、MarketWatch、Yahoo Finance、CNBC、天下雜誌、商業週刊、TechNews、Google News TW、SCMP）,逐則標題由 Gemini 評分,單則分數裁切在 ±0.5,以 **2 小時半衰期的 EMA** 累加成場次分數,超過門檻才寫票。單一嘈雜的 30 分鐘視窗不再能決定當晚的票。週末暫停(Sat 05:00 – Mon 05:00 TPE):不抓取、不呼叫 Gemini,心跳 embed 照常。
- **W4 籌碼投票**:TAIFEX 外資臺股期貨淨未平倉 + TWSE BFI82U 外資現貨買賣超 → `regime_vote_w4.json`。遲滯帶、矛盾檢查、過期票自動刪除。
- **W5 人工確認**:Discord 連結開啟 n8n webhook,寫出的是**訊號**而非票。

投票檔案的統一規則:方向只有 `trending-up` / `trending-down`（沒有中立 —— 不確定的來源就不寫）,`expires_after_session` 必須等於正在分類的那一晚,且**每次分類後一律刪除**。

---

### n8n news bridge W1–W5: five data lanes

**What changed**: New `scripts/news_bridge/` (stdlib-first) plus importable n8n workflow JSON.

| # | Script | Cadence | Output |
|---|--------|---------|--------|
| W1 | `calendar_bridge.py` | daily | economic-calendar risk windows |
| W2 | `crossmarket_monitor.py` | 2 min during US hours, 10 min otherwise | `signal.json` breaker + `regime_vote_w2.json` |
| W3 | `rss_scorer.py` | every 30 min | `regime_vote_w3.json` (RSS + Gemini sentiment) |
| W4 | `chips_monitor.py` | daily 16:15 TPE (+ 4/18/20/22 retry slots) | `regime_vote_w4.json` (institutional flow) |
| W5 | `n8n/W5_manual_tap.json` | on human tap | `signal.json` via a Discord webhook link |

- **W2 cross-market monitor**: expanded from 3 US symbols to 11 global symbols across a signal and an alert tier. Per-symbol freshness allowances (600s US, 900s CME, 1500s delayed Asia/EU) replace the single 600s guard that was silently discarding every non-US quote. Adding `^HSI` / `^TWII` / `000001.SS` lifts TW-daytime voter coverage from 2/8 to 5–6 symbols.
- **W3 RSS + Gemini scorer**: polls international and Taiwanese feeds (Reuters, MarketWatch, Yahoo Finance, CNBC, 天下雜誌, 商業週刊, TechNews, Google News TW, SCMP), scores each headline with Gemini, clips per-article scores to ±0.5, and accumulates them into a session score with a **2-hour-half-life EMA**. One noisy 30-minute window can no longer set the standing vote for the night. Weekend pause (Sat 05:00 – Mon 05:00 TPE): no fetch, no Gemini calls; the heartbeat embed still posts.
- **W4 chips vote**: TAIFEX foreign net OI in TX futures plus TWSE BFI82U foreign equity flow → `regime_vote_w4.json`, with a hysteresis band, a contradiction check, and stale-vote deletion.
- **W5 manual tap**: a Discord link opens the n8n webhook, which fires a *signal*, not a vote.

Shared vote-file rules: directions are only `trending-up` / `trending-down` (no neutral — an uncertain source writes nothing), `expires_after_session` must equal the night being classified, and votes are **deleted after every classification pass**.

---

### Regime：外部票進入分類與盤整決策

**問題**:外部訊號原本只能加速「趨勢」確認,當技術面判讀為盤整時就被結構性地丟棄。8/05–8/07 那週指數收盤累積 +743 點、ADX 卻始終低於 20,框架手上握有的外部證據全部被扔掉。

**變更**:融合發生在**決策層(selector)**,而不是分類層 —— regime 標籤維持誠實的技術面判讀,每個票源都可稽核。

- 分類時(~04:58)讀取所有有效票,同意 raw 判讀的票可略過一晚的遲滯確認(2 晚 → 1 晚 + 票)。
- 盤整分支:方向明確的外部票若與 `ema_slope` 漂移方向一致,不論 `range_bias_action` 設定為何,都以**半口(qty_scale)**部署對應腳。互相衝突的票會互相抵銷;與漂移相反的票直接忽略。ADX < `adx_exit` 時 DI 方向訊噪比太低,故不納入判斷。（**這條盤整探路單在 PR #111 之後預設關閉** —— 需 `regime.vote_range_probe: true` 才會啟用,見下方 PR #111 項目。分類階段的票加速不受影響。）
- 波動急升(vol-spike)與事件風險兩道閘門仍優先於票。
- 新增動作 `deploy_long_half`（與 `deploy_short_half` 對稱）。
- `range_bias_action` 擴充為 `sit_out` / `short_half` / `long_half` / `both_half`,而且**真的接上了 settings.yaml** —— 先前 `_load_settings` 沒讀、`config_loader` 也沒映射,是一個死掉的設定。
- 每個票源寫自己的檔案（`regime_vote_w2.json` / `_w3` / `_w4`）,不再互相覆蓋。
- 分類結果持久化 `_vote_sources` 與 `_vote_accelerated`,`regime_history.csv` 新增 `votes` 欄位。

---

### Regime: external votes now feed classification and range-bound decisions

**Problem**: External votes could only accelerate a *trending* confirmation — when the raw technical read was range-bound they were structurally ignored. The 08-05→08-07 grind-up (close +743 pts while ADX stayed under 20) showed the framework discarding external evidence it already had.

**What changed**: Fusion happens at the **decision layer (selector)**, not at classification — the regime label stays an honest technical read, auditable per source.

- At classification (~04:58) every valid vote is read; a vote agreeing with the raw label skips one night of hysteresis (2 nights → 1 night + vote).
- Range-bound branch: an unambiguous external vote agreeing with the `ema_slope` drift deploys the matching leg at **half size**, regardless of `range_bias_action`. Conflicting votes cancel; a vote against the drift is ignored. DI direction is not required (low-signal when ADX < `adx_exit`). (**This range-bound probe ships OFF by default as of PR #111** — it needs `regime.vote_range_probe: true`; see the PR #111 item below. Vote acceleration at classification is unaffected.)
- The vol-spike and event-risk gates still outrank votes.
- New action `deploy_long_half`, symmetric to `deploy_short_half`.
- `range_bias_action` extended to `sit_out` / `short_half` / `long_half` / `both_half` — and actually wired from `settings.yaml`, which it was not before (`_load_settings` never read it and `config_loader` never mapped it: dead config).
- Each source writes its own file (`regime_vote_w2.json` / `_w3` / `_w4`); they no longer overwrite each other.
- The classification persists `_vote_sources` and `_vote_accelerated`, and `regime_history.csv` gains a `votes` column.

---

### GUI：票源狀態 + 分段(episode)式 regime 分頁、卡片式報告分頁

**變更**:

- Regime 分頁重建:狀態卡片、W2/W3/W4 三列票源狀態（fresh / stale / unknown,含各來源不同的過期門檻:W2 24h、W3 2h、W4 72h）,以及以 regime **episode 分段**的 Treeview —— 同一段 regime 併成彩色區塊、DAY 列淡化、閒置期收合、票源與加速標記直接顯示在列上。
- Report 分頁改為同一套卡片版型:標題卡（好／壞配色）、交易與風險明細區塊、實單子集重算面板、逐策略 Treeview,全部放在可捲動畫布中。既有的「檢視 View」篩選保留;`format_report` 的純文字輸出未動,Discord 與匯出格式不變。
- 兩者都建立在無 Tkinter 依賴的純資料模組上（`src/news/vote_status.py`、`src/regime/episodes.py`、`src/backtest/report_view.py`）。

---

### GUI: per-source vote status, episode-grouped regime tab, card-based Report tab

**What changed**:

- The regime tab was rebuilt: status cards, one row per vote source (W2/W3/W4) showing fresh / stale / unknown against per-source thresholds (W2 24h, W3 2h, W4 72h), and a Treeview grouped into **regime episodes** — coloured bands per episode, dimmed DAY rows, collapsed idle stretches, and vote markers including acceleration.
- The Report tab moved to the same card pattern: headline cards with good/bad tones, trade and risk detail grids, a real-order subset panel, and a per-strategy Treeview, all in a scrollable canvas. The 檢視 View filter is kept; `format_report`'s text output is untouched, so Discord and exports are unchanged.
- Both are built on Tk-free pure view models (`src/news/vote_status.py`, `src/regime/episodes.py`, `src/backtest/report_view.py`).

---

### Regime 分類門檻可從 settings.yaml 調整

**問題**:所有分類門檻都寫死在 `RegimeConfig` 內,調參必須改程式碼。

**變更**:`regime:` 區塊新增 `adx_enter` / `adx_exit` / `adx_strong` / `confirm_sessions` / `flip_pause_sessions` / `max_flips_in_window` / `flip_window_sessions` / `classify_interval`,由 `build_regime_config()` 組成 `RegimeConfig`,並可由 `data/regime/regime_evolved_params.json` 疊加（有 90 天新鮮度閘門）。同時新增 regime 基因演化管線:以「方向預測正確率 0.6 + 正規化損益 0.4」為適應度,對 `adx_enter` / `adx_strong` / `confirm_sessions` 做有界突變,適應度未達標則不寫回。

---

### Regime classification thresholds are now tunable from settings.yaml

**Problem**: Every classification threshold was a hard-coded `RegimeConfig` default; tuning meant editing code.

**What changed**: The `regime:` block gained `adx_enter` / `adx_exit` / `adx_strong` / `confirm_sessions` / `flip_pause_sessions` / `max_flips_in_window` / `flip_window_sessions` / `classify_interval`. `build_regime_config()` builds `RegimeConfig` from them, with an optional overlay from `data/regime/regime_evolved_params.json` behind a 90-day freshness gate. A regime gene pipeline was added alongside: fitness is directional label accuracy weighted 0.6 plus normalized P&L 0.4, mutating `adx_enter` / `adx_strong` / `confirm_sessions` with bounds clamping and a fitness gate on the write.

---

### W2 分層重整、夜間車道票齡閘門、非對稱票數門檻 (PR #110)

**問題**:W2 的優勢集中在開火後約 **4 小時內**。但夜間 regime 車道是在 ~04:58 分類時才讀這張票 —— 那已經是這張票所指定場次的**結束**時刻,而它換上的腳要到隔天 08:45 開盤才真正開始交易,距離美股時段開火約 14–20 小時。在那個時間差上,W2 的方向 10 次對 3 次。另外,`ASML.AS` 單獨開火的勝率只有 20%。

**變更**:

- **W2 分層重整**:`risk_off` 現在必須通過票數門檻 —— 至少 2 檔新鮮標的向下突破各自的投票門檻,且沒有任何標的向上突破。單一 signal 層突破（或被一個新鮮的反向突破抵銷）只會發出 alert 層 Discord 訊息,附上「未達票數門檻 / quorum not met — no risk_off」的雙語註記,**不寫任何訊號檔**。這條 alert 路徑使用獨立的去重鍵 `signal-alert-down:<us_date>`,永遠不會佔用 `down:<us_date>` 而壓掉同一個美股時段稍後的真實開火。
- `ASML.AS` 從 signal 層降為 **alert 層**:保留投票門檻、仍計入票數,但不能單獨開火。
- **夜間車道票齡閘門**:`write_regime_vote` 現在為每張票蓋上 `fired_at`(ISO-8601,+08:00);schema 維持 version 1,因為欄位是純新增而讀取端本來就忽略未知鍵。`RegimeVote` 新增 `fired_at` 與 `age_sec`（無法解析時退回檔案 mtime,且鉗制在 0 以上,橋接端時鐘走快不會讀成負值）。新設定 `news.nightly_vote_max_age_h`（預設 `{W2: 4.0}`）超齡的票會記錄 `expired-by-age (source, age)` 並**不交給分類器**,但仍然照常被消費刪除（留在磁碟上只會更老）。未列在對應表中的來源沒有年齡上限;`{}` 完全關閉這道閘門。
- **非對稱票數門檻**:`regime.vote_quorum_up`（預設 2）/ `regime.vote_quorum_down`（預設 1）。這是刻意的不對稱 —— 依外部證據做多是昂貴的錯誤,而提早確認向下的判讀是便宜且具保護性的方向。盤整探路單的「衝突取消」規則不變且**絕對**:任何一張反向票都會取消探路單,不論票數是否達標（PR #111 之後探路單本身預設關閉,票數門檻仍作用於分類階段的加速）。設為 0 可停用該方向的票。
- `regime_history.csv` 新增 `vote_rule` 欄位（`"up:1/2"` 形式,實得/所需）,在有票的每一個已評估場次都會寫入,包含過渡與暫停的提前結束路徑。`votes` 欄位格式完全未動。
- 新增 `scripts/score_fires.py`（純唯讀,`--out` 除外）:把 W2 alert 表匯出檔與機器人自己的 `regime_history.csv`、`bars_1m_*.csv` 對接,逐筆開火輸出它指定的夜間 key、對應的分類列、實際上線的腳、以及 `leg_window_move`（帶正負號,正值代表這次開火方向是對的）。連接邏輯本身是 `src/news/fire_scoring.py` 的純函式。

---

### W2 re-tier, nightly-lane vote age gate, asymmetric vote quorum (PR #110)

**Problem**: W2's edge peaks in roughly the **4 hours after a fire**. The nightly regime lane reads that vote at the ~04:58 classification — the moment that *ends* the session the vote named — and the leg it swings only goes live at the next 08:45 open, ~14–20 h after a US-session fire. Over that lag W2 was right 3 times in 10. Separately, `ASML.AS` fires on its own were 20 % win.

**What changed**:

- **W2 re-tier**: `risk_off` now requires the vote **quorum** — at least 2 fresh symbols breaching their vote thresholds down, and none breaching up. A lone signal-tier breach, or one contradicted by a fresh up-breach, posts the alert-tier Discord text plus a bilingual "未達票數門檻 / quorum not met — no risk_off" note and writes **no** signal file. That alert path uses a distinct dedup key `signal-alert-down:<us_date>`, so it can never consume the `down:<us_date>` slot and suppress a real fire later in the same US session.
- `ASML.AS` moves **signal → alert**: it keeps its vote thresholds and still counts toward the quorum, but can no longer fire on its own.
- **Nightly-lane age gate**: `write_regime_vote` stamps `fired_at` (ISO-8601, `+08:00`); the schema stays version 1 because the field is additive and readers already ignore unknown keys. `RegimeVote` gains `fired_at` and `age_sec` (falling back to file mtime when `fired_at` is absent or unparseable, clamped at 0 so a fast bridge clock cannot read as negative). The new `news.nightly_vote_max_age_h` (default `{W2: 4.0}`) makes an over-age vote log `expired-by-age (source, age)` and be **withheld from the classifier**, while still being consumed with the rest — leaving it on disk only makes it older. Sources absent from the map have no limit; `{}` disables the gate.
- **Asymmetric quorum**: `regime.vote_quorum_up` (default 2) / `regime.vote_quorum_down` (default 1). Deliberately asymmetric — going long on external evidence is the expensive mistake, while confirming a down read early is the cheap, protective direction. Conflict-cancel in the range-bound probe is unchanged and **absolute**: ANY opposing vote kills the probe, quorum or not (as of PR #111 the probe itself is off by default; the quorum still governs vote acceleration at classification). Setting a value to 0 disables votes for that direction.
- `regime_history.csv` gains a `vote_rule` column (`"up:1/2"`, got/needed), stamped on every assessed session that saw votes — including the transitional and paused early-exit paths. The `votes` column format is untouched.
- New `scripts/score_fires.py` (read-only apart from `--out`): joins the W2 alerts-sheet export against the bot's own `regime_history.csv` and `bars_1m_*.csv`, emitting per fire the night key it targeted, the classification row that consumed it, the leg that actually ran, and `leg_window_move` — signed so a positive number always means the fire was right. The join itself is pure functions in `src/news/fire_scoring.py`.

---

### 監控套件與診斷 skills

**變更**:新增 `scripts/monitor/`（`check_bots` / `check_regime` / `check_bridge` / `check_logs` / `check_deploy` / `check_evolution` 與 `daily_review.py` 協調器）以及對應的 Claude Code skills（bot-status、regime-health、bridge-health、log-scan、deploy-check、n8n-debug、issue-triage、validate-findings、fix-pr、evo-debug、daily-review、weekly-review）。這是開發／維運用的**唯讀**工具,一般使用者不需要執行 —— 它從不修改機器人或橋接的任何狀態,所有摘錄都會做敏感資訊遮蔽。

---

### Monitoring suite and diagnostic skills

**What changed**: New `scripts/monitor/` (`check_bots` / `check_regime` / `check_bridge` / `check_logs` / `check_deploy` / `check_evolution` plus the `daily_review.py` orchestrator) and the matching Claude Code skills (bot-status, regime-health, bridge-health, log-scan, deploy-check, n8n-debug, issue-triage, validate-findings, fix-pr, evo-debug, daily-review, weekly-review). This is a **read-only** developer/ops tool that end users do not need to run — it never mutates bot or bridge state, and every excerpt is redacted.

---

## 修正 Fixes

### 方向性暫停與場次邊界自動到期（8/18→8/20 卡死事件）

**問題**:8/18 一個 bearish 的 `risk_off` 讓機器人的**放空**腳靜默了大約兩天,直到 8/20。原因有兩層:(1) 封鎖只有在收到 `clear` 訊號時才會解除,而那個 `clear` 從來沒有送出;(2) 一個看空的訊號沒有理由去封鎖一個看空的策略。

**修正**:

- `plan_suppression_maintenance` 現在會在訊號的 `signal_session_key` 不再對應到目前或下一個場次時,**自動解除封鎖(fail open)**。沒有這個欄位的舊 `session.json` 會在第一次輪詢時解除。重新開火的 `risk_off` 會刷新這個時間戳,所以持續的風險仍然持續封鎖。
- 新增 `news.suppress_scope`:`conflicting_leg` 模式下,`BreakerState.gates_leg` 只封鎖這個方向性訊號會傷害到的腳 —— bearish 擋多方腳、bullish 擋空方腳;同方向的部位會被保留,並繼續正常的出場管理。severity 為 `critical`、或訊號沒有 `direction` 欄位時,一律兩邊都擋。預設維持舊行為 `both`。
- 每個場次一次的 Discord「仍在封鎖中」提醒,避免再次無聲卡死。
- 每一根被封鎖的 K 棒都會在 `decisions.csv` 寫入一列 `NEWS_SUPPRESSED` 稽核紀錄。
- 新增 `scripts/replay_bars.py`:用機器人自己錄下的 1 分 K CSV 把策略重跑一次,離線計算「如果沒被擋,策略會做什麼」—— 讓斷路器累積自己的成績單,而不是默默地讓實盤行為偏離回測。

---

### Direction-aware suppression and session-boundary expiry (the 08-18 → 08-20 latch)

**Problem**: On 8/18 a bearish `risk_off` silenced the bot's **short** leg for roughly two days, until 8/20. Two causes: the suppression could only be released by a `clear` signal that never arrived, and a bearish signal had no business gating a bearish strategy in the first place.

**Fix**:

- `plan_suppression_maintenance` now **releases (fails open)** a signal suppression whose `signal_session_key` no longer matches the current or next session. A legacy `session.json` without the key releases on the first poll. A re-fired `risk_off` refreshes the stamp, so genuine ongoing risk keeps gating.
- New `news.suppress_scope`: under `conflicting_leg`, `BreakerState.gates_leg` gates only the leg a directional signal would hurt — bearish gates long legs, bullish gates short legs — and a same-direction position is kept with its exit management intact. Severity `critical` or a signal with no `direction` field always gates both. The default stays the legacy `both`.
- A once-per-session Discord "still suppressed" reminder, so this cannot latch silently again.
- Every gated bar writes a `NEWS_SUPPRESSED` audit row to `decisions.csv`.
- New `scripts/replay_bars.py`: replays a bot's own recorded 1-min CSVs through a strategy offline to price what a suppression cost — the breaker earns a track record instead of silently filtering live behavior away from the backtest.

---

### 逐筆行情重連後機器人變殭屍 (issue #105)

**問題**:2026-09-01 部署的一台機器人整場沒有交易。08:47:52 報價重連後,策略被永久壓制,同時有 24,666 筆新鮮 tick 以每秒約 20 筆的速度湧入。

**根本原因**:COM 把重連後的**整條即時流**都透過 `OnNotifyHistoryTicksLONG`（`is_history=True`）送出,從未切換回 `OnNotifyTicksLONG`。這正是 issue #50 的**鏡像**（#50 是歷史 tick 被標成即時,已由時間差防護擋下）。`classify_tick` 的過渡前分支條件是 `not is_history_flag`,所以一筆被標成歷史的新鮮 tick 永遠無法回傳 "transition",`suppress_strategy` 與 `_is_reloading` 就此永久鎖死 True。600 秒的安全閥只清除 `_is_reloading`;而整個 repo 裡 `suppress_strategy = False` 只有一個寫入點,就在那個過渡區塊裡。結果:K 棒照常產生、策略永不執行,只能重啟。

**修正**:

- `classify_tick` 在過渡前**只看時間差**:新鮮度在門檻內的 tick 一律過渡,不管旗標;過期的 tick 保留原行為（靜靜地建 K 棒）。過渡後的行為完全未變。代價是一次合法的歷史回補在最後 ≤120 秒時會稍微提早過渡 —— 那是在本質上已經是當前的資料上,遠優於一台永久壓制的機器人。
- `_on_com_tick` 的看門狗存活判定原本也信任旗標。改為時間差判定後,#105 這種流會餓死看門狗、每 5 分鐘觸發一次沒有意義的重新訂閱（而重新訂閱會再次壓制策略）。現在它同樣接受任何新鮮 tick。
- 600 秒安全閥維持只清除回補視窗,訊息文字改為如實描述:在這次修正之後,它只有在資料流真的死掉、或所有 tick 都過期時才會被觸發 —— 那些情況下維持壓制才是正確的。

---

### Bot became a zombie after a quote reconnect (issue #105)

**Problem**: A bot deployed on 2026-09-01 never traded. After a quote reconnect at 08:47:52 the strategy stayed suppressed for the whole session while 24,666 fresh ticks streamed in at ~20/s.

**Root cause**: COM delivered the entire post-reconnect stream via `OnNotifyHistoryTicksLONG` (`is_history=True`) and never switched to `OnNotifyTicksLONG`. This is the **mirror image** of issue #50 (history ticks flagged live, which the tick-age guard already covers). `classify_tick`'s pre-transition branch was gated on `not is_history_flag`, so a fresh tick flagged history could never return "transition" — `suppress_strategy` and `_is_reloading` latched True permanently. The 600s safety valve cleared only `_is_reloading`, and repo-wide `suppress_strategy = False` has exactly one site: that transition block. Bars kept flowing, the strategy never ran, and only a restart recovered it.

**Fix**:

- `classify_tick`: pre-transition, **age alone decides**. A tick within the staleness threshold transitions regardless of the flag; a stale tick is kept (bars build silently). Post-transition behavior is unchanged. The trade-off is that the final ≤120s of a legitimate replay now transitions slightly early, on essentially-current data — benign versus a permanently suppressed bot.
- `_on_com_tick`: the watchdog liveness gate also trusted the flag. With age-based transition, a #105-style feed would have starved the watchdog and forced a pointless resubscribe every 5 min (which re-suppresses the strategy). It now also accepts any fresh tick.
- The 600s valve still clears only the reload window, and its message now says so: after this fix it is reachable only when the feed is genuinely dead or all ticks are stale, where staying suppressed is the correct outcome.

---

### AI API 暫時性失敗會終止整個演化排程 (issue #108)

**問題**:一次 Gemini 503（"This model is currently experiencing high demand"）在 EVO 2/3 直接殺掉整個每週演化排程,而且沒有任何回復路徑。

**根本原因**:兩個缺陷。(1) `src/ai/chat_client.py` 對任何非 200 狀態一律拋出 `RuntimeError`,全域沒有任何重試 —— 只有 `_post_gemini` 針對 404 舊模型有 fallback,沒有東西處理暫時性的 429/5xx。(2) `run_backtest.py` 那個標榜「候選碼產生（重試一次）」的迴圈,把 `one_shot()` 寫在 `try` **外面**,所以傳輸層的 `RuntimeError` 直接跳出 `for attempt in (1, 2)` 迴圈 —— 廣告中的重試對 API 失敗根本沒有發生,而錯誤落到外層處理器,只寫 log 不發 Discord。

**修正**:

- 新增 `_post_with_retry()`:對 429/500/502/503/504 做最多 3 次、1 秒與 2 秒退避的重試,沿用 `DiscordNotifier._send` 既有的慣例。`one_shot` 與 `send_message` 兩條路徑（Anthropic 與 Google）全部接上。永久性錯誤（400/401/404）仍在第一次回應就快速失敗;成功路徑、回傳形狀與 404 舊模型 fallback 皆未改變,而且 fallback 在重試之間維持黏著。
- 候選碼產生迴圈把 `one_shot()` 移進 `try`,API 失敗會消耗一次嘗試,最終失敗則走既有的 EVO FAIL / Discord 通知路徑。
- `_start_evolution_pipeline` 最外層的 `except Exception` 原本只寫 log、不發 Discord —— 也就是回報者遇到的那個「無聲死亡」。現在會送出 `🧬 EVO ERROR:` 通知（以 `auto_run` 為條件,手動執行維持安靜）。

---

### A transient AI API failure killed the whole evolution run (issue #108)

**Problem**: A Gemini 503 ("This model is currently experiencing high demand") killed an entire weekly evolution run at EVO 2/3 with no recovery path.

**Root cause**: Two defects. (1) `src/ai/chat_client.py` raised a bare `RuntimeError` for any non-200 status with no retry anywhere — only `_post_gemini`'s 404 stale-model fallback existed; nothing handled transient 429/5xx. (2) `run_backtest.py`'s "candidate codegen (one retry)" loop called `one_shot()` **outside** the try, so a transport `RuntimeError` escaped the `for attempt in (1, 2)` loop entirely — the advertised retry never happened for API failures, and the error landed in the outer handler which logs but sends no Discord notification.

**Fix**:

- New `_post_with_retry()`: bounded 3 attempts with 1s/2s backoff on 429/500/502/503/504, matching `DiscordNotifier._send`'s idiom, wired into both the `one_shot` and `send_message` paths for both providers. Permanent errors (400/401/404) still fail fast on the first response; the success path, return shape and the 404 stale-model fallback are unchanged, and the fallback stays sticky across retries.
- The codegen loop now runs `one_shot()` inside the try, so an API failure consumes an attempt and a final failure is reported through the existing EVO FAIL / Discord path.
- `_start_evolution_pipeline`'s outermost `except Exception` logged but never notified Discord — precisely the reported silent-death symptom. It now sends a `🧬 EVO ERROR:` notice, guarded by `auto_run` so manual runs stay Discord-quiet.

---

### 部署時明確宣告新聞斷路器狀態

**問題**:8/04-rss 那台機器人的新聞斷路器在 8/7–8/14 之間整整一週處於「靜默關閉」狀態 —— 一次續跑掉了那個旗標,而沒有任何地方會把它顯示出來。

**修正**:每一次 regime 部署都會在三個地方陳述新聞啟用狀態:Discord `bot_deployed_regime` 訊息的 news-breaker ON/OFF 行（含 Tier2 標記）、GUI/debug 部署 log 的 `News=ON+Tier2/ON/OFF` 後綴,以及 runner 啟動 log 中經過 ledger 失敗路徑後的**最終生效狀態**（那條路徑可能在對話框說 ON 之後強制關閉新聞）。

---

### Breaker state is announced on every regime deploy

**Problem**: The news circuit breaker ran silently disabled on the 08-04-rss bot for a week (Aug 7–14) after one resume dropped the flag, and nothing surfaced it.

**Fix**: Every regime deploy now states news enablement in three places: a news-breaker ON/OFF line (with a Tier2 marker) in the Discord `bot_deployed_regime` message, a `News=ON+Tier2/ON/OFF` suffix on the GUI/debug deploy log line, and the runner startup log's **final effective state** after the ledger-failure path, which can force-disable news after the dialog said ON.

---

### 投票的場次 key 指向錯誤的一晚

**問題**:`night_session_key` 以 15:00 為分界,所以在 TPE 05:00–14:59 之間寫出的票會被蓋上**前一晚**的 key —— 那一晚早在 ~04:58 就分類完了,票一寫出就已經過期。而 W2 的票正是在 12:00–14:00 TPE 左右因為亞洲日盤的走勢而開火,剛好落在這個死區,所以它白天的票從來沒有被消費過。

**修正**:分界改為 05:00（夜盤收盤）—— 早上或白天寫出的票會指向今晚的分類。新增邊界測試:04:59 → 昨晚,05:00 / 10:00 / 14:00 → 今晚。

---

### Votes were stamped with the wrong night's session key

**Problem**: `night_session_key` used 15:00 as its boundary, so votes written between 05:00 and 14:59 TPE were stamped with the **previous** night's key — a session already classified at ~04:58, making the vote expired on arrival. W2's votes fire off Asian day-session moves around 12:00–14:00 TPE, exactly that dead window, so its daytime votes could never be consumed.

**Fix**: The boundary is now 05:00 (night close), so a morning or day vote targets tonight's classification. Boundary tests added: 04:59 → last night; 05:00 / 10:00 / 14:00 → tonight.

---

### 部署從未把 `news.regime_vote_path` 接進 NewsConfig

**問題**:GUI 部署的機器人從來沒有消費過任何 W2/W3/W4 的票 —— 投票檔就這樣一路過期、無人讀取。橋接端一切正常,只是那條線根本沒接上。

**修正**:`_resolve_news_config` 現在會傳遞 `regime_vote_path`,並加上迴歸測試釘住這個欄位確實到達 `NewsConfig`。PR #110 的 `nightly_vote_max_age_h` 也附了同一類的迴歸測試,因為這是同一種缺陷。

---

### The deploy path never wired `news.regime_vote_path` into NewsConfig

**Problem**: GUI-deployed bots never consumed any W2/W3/W4 vote — the vote files simply expired unread. The bridges were fine; the wire was missing.

**Fix**: `_resolve_news_config` now passes `regime_vote_path` through, with a regression test pinning that the field actually reaches `NewsConfig`. PR #110's `nightly_vote_max_age_h` ships with the same class of regression test, because it is the same defect.

---

### W4 籌碼投票：走查回補 + 使用 ΔOI 而非水位

**問題**:W4 從 8/19 到 8/31 完全沒有投出任何一票。

**根本原因**:兩個獨立問題。(1) TAIFEX OpenAPI **不接受日期參數**,永遠只提供一份（落後的）資料集,而腳本卻預先挑好「今天」的日期去比對,結果每天都不匹配。(2) `decide()` 讀的是外資臺股期貨淨未平倉的**水位**。外資長期持有結構性避險空單（約 -80,000 口）,所以水位每天都把 5,000 口的門檻塞爆在空方,`trending-up` 在結構上永遠不可能達成。

**修正**:

- 改為**資料集驅動的走查回補**:從實際送來的資料列上讀出交易日期,只要該日尚未決策且在 3 個交易日內就直接決策。週末的執行會**延後**（週六／週日的夜間 key 不會開盤,提前決策反而會用去重機制擋掉週一的票）。加上補跑排程（TPE 15 分,4/16/18/20/22 時）與以 `.chips_state.json` 的 `last_decided_date` 做的逐日去重,所以 TWSE 中斷造成的「沒投票」仍然可以重試。
- **改用日對日的 ΔOI**:1,000 口以下為遲滯帶不投票;1,000–5,000 的弱帶必須有同方向的現貨資金流佐證;5,000 以上直接依 ΔOI 正負投票;基準值超過 5 個日曆天則丟棄（跨越假期時,過期的 Δ 會無聲地灌水）。移除斜率分支;第一次執行只記錄不投票。投票內容新增 `data_date`,所有介面一律顯示 `ΔOI ...（水位 ..., 前值 ...）`。
- 去重／延後的執行仍會蓋上存活時間戳並記錄自己的排程時段,所以從磁碟上可以分辨「橋接掛了」與「TAIFEX 休市」。

（帶寬對 TEJ 的重新校準仍待進行。）

---

### W4 chips vote: dataset-driven walk-back, and ΔOI instead of the level

**Problem**: W4 produced no vote at all from 08-19 to 08-31.

**Root cause**: Two independent issues. (1) The TAIFEX OpenAPI takes **no date parameter** and serves exactly one (lagging) dataset, but the script pre-picked "today" and compared against it, so the date never matched. (2) `decide()` read the **level** of foreign net OI in TX futures. Foreign institutions carry a permanent structural hedge short (~-80,000 contracts), so the level saturates the 5,000-contract band on the short side every single day and `trending-up` was structurally unreachable.

**Fix**:

- **Dataset-driven walk-back**: read the trade date off the served rows and decide it when undecided and within 3 trading days. Weekend passes **defer** (a Sat/Sun night key never opens and would dedup-block Monday's vote). Retry-slot crons (15 past the hour at 4, 16, 18, 20, 22 TPE) plus per-date dedup via `last_decided_date` in `.chips_state.json` keep TWSE-outage no-votes retryable.
- **Day-over-day ΔOI semantics**: below 1,000 contracts is a hysteresis band (no vote); the weak band 1,000–5,000 needs same-direction equity flow; at or above 5,000 the sign of ΔOI decides; a baseline older than 5 calendar days is rejected (a stale Δ silently inflates across a gap). The slope branch is removed and the first pass records only. The vote payload gains `data_date` and every surface reports `ΔOI ... (level ..., prev ...)`.
- Deduped and deferred passes still stamp liveness and log their slot, so a dead bridge is distinguishable from a TAIFEX holiday from disk alone.

(Band re-calibration against TEJ is still pending.)

---

### Regime 分頁的票源摺疊必須挑選有效的檔案

**問題**:一次路徑打錯的手動執行留下了一個一週前的 `regime_vote_w3_w3.json`,它的 `source` 欄位也寫 W3。因為 glob 順序排在後面,它蓋掉了真正的票,導致分頁上在一張今晚有效的票旁邊顯示「已過期」。

**修正**:碰撞規則明確化 —— 今晚有效的檔案勝過已過期的,平手時取目標場次較新的;glob 順序永遠不決定結果。分類器本身不受影響（它逐一獨立驗證每個檔案）。

---

### The regime tab's per-source vote collapse must prefer the valid file

**Problem**: A mis-pathed manual run left a week-old `regime_vote_w3_w3.json` stray whose `source` field also said W3. Globbed last, it shadowed the real vote and the tab showed "expired" beside a live vote for tonight.

**Fix**: The collision rule is now explicit — valid-for-tonight beats expired, latest target breaks ties, and glob order never decides. The classifier was unaffected (it validates each file independently).

---

### Regime 引擎：暫停只凍結趨勢進場，外部票不再從盤整開倉 (PR #111)

**問題**:機器人從 8/17 到 8/28 整整卡在一個過期的 `trending-down` 判讀上,只做空。

**根本原因**:兩層。(1) 三次翻轉（8/06、8/12、8/14）觸發了 `max_flips_in_window=3`,引擎進入 5 個場次的暫停 —— 而舊規則下的暫停會**凍結一切**,包含那些其實在**降低**曝險的變更。被凍結的那 5 晚裡有 3 晚的原始判讀已經是盤整（range-bound）,引擎看得到、卻不准離開空方腳。(2) 暫停結束後,一次過渡性(transitional)的判讀又把待確認的連續計數歸零,退出因此再往後拖到 8/28。

**變更**:四條規則,每一條都是 `settings.yaml` 中 `regime:` 底下的一個開關,設為 `true` 即可還原舊行為:

- `pause_freezes_exits: false` —— 暫停只凍結**趨勢腳的進場**;切換到盤整的**退出**仍然生效,selector 隨後跑它正常的盤整分支而不是 `hold`。離開一個趨勢腳是減少曝險,不是增加。
- `exits_count_as_flips: false` —— 只有趨勢進場計入 `max_flips_in_window`。一次防禦性的退場不會把引擎推得更深地卡在暫停裡,「平靜→趨勢→平靜」的序列也不再自己武裝一次暫停。
- `transitional_resets_streak: false` —— 一次過渡性判讀既不增加、也不重置待確認計數;確認中的 regime 變更不會因為中間插進一晚模稜兩可的讀數而從頭來過。
- `vote_range_probe: false` —— 外部票**不得**再從一個空手的盤整場次開出半倉探路單。它們只能**加速確認技術面本來就已經顯示的趨勢**(這正是「在既有方向上加碼」)。使用者的規則:目前策略不做空時就不要做空,反之亦然。趨勢確認的票加速(`step()`)不受這道閘門影響。

**驗證**:新增 `scripts/replay_regime_rules.py`,用機器人自己的 `regime_history.csv` 離線重跑整個切換引擎,並列印「舊規則 vs 新規則」的逐場次差異(舊規則模式同時自我檢查,必須重現檔案裡記錄的 `effective_regime`)。以該機器人 26 個場次的歷史重播:新規則在 **8/21** 就離開那筆過期的空單,而不是 8/28;整段歷史**一次暫停都沒有武裝**;四次從盤整開出的票探路單（8/28、8/31、9/07、9/08）**全部不開**;8/28 之後兩套規則的生效 regime 完全一致。

---

### Regime engine: the pause freezes trend entries only, and votes no longer open from a flat range (PR #111)

**Problem**: The bot sat short on a stale `trending-down` read from 08-17 all the way to 08-28.

**Root cause**: Two layers. (1) Three flips (08-06, 08-12, 08-14) tripped `max_flips_in_window=3` and armed a 5-session pause — and under the old rules a pause **froze everything**, including the changes that *reduce* exposure. On 3 of those 5 frozen nights the raw label was already range-bound: the engine could see it and was not allowed to leave the short leg. (2) After the pause lifted, a transitional read zeroed the pending confirmation streak, pushing the exit out further to 08-28.

**What changed**: Four rules, each a `settings.yaml` knob under `regime:` whose legacy value is still available (set it to `true`):

- `pause_freezes_exits: false` — a pause freezes **trend-leg entries only**; a confirmed **exit** to range-bound still applies, and the selector then runs its normal range-bound branch instead of `hold`. Leaving a trend leg reduces exposure rather than adding to it.
- `exits_count_as_flips: false` — only trend entries count toward `max_flips_in_window`. A defensive exit can no longer push the engine deeper into a pause, and a calm→trend→calm sequence no longer self-arms one.
- `transitional_resets_streak: false` — a transitional read neither increments nor resets the pending streak, so a regime change under confirmation is not sent back to zero by one ambiguous night in the middle.
- `vote_range_probe: false` — external votes may **no longer** open a half-size probe out of a flat range-bound session. They may only **accelerate confirmation of a trend the technicals already show**, which is adding in the direction the regime leg already holds. The user's rule: never go short when the current strategy is not short, and vice versa. Vote acceleration of a trend confirmation in `step()` is not gated by this knob.

**Verification**: New `scripts/replay_regime_rules.py` replays any bot's own `regime_history.csv` through the switching engine offline and prints the session-by-session legacy-vs-current diff (the legacy run self-checks: it must reproduce the recorded `effective_regime` column). Replayed over that bot's 26-session history, the new rules exit the stale short on **08-21** instead of 08-28, **never arm a pause** at all, and open **none** of the four flat-range vote probes (08-28, 08-31, 09-07, 09-08); from 08-28 onward the two rule sets agree on the effective regime.

---

## 改進 Improvements

### 投票與無投票的 Discord 稽核

W2/W3/W4 現在會為**每一次寫票**以及每一次有意義的**不投票**（矛盾、斜率不同意、未達票數門檻）發出 Discord 訊息,W2 的矛盾警示以美股時段去重。W3 的 embed 同時顯示 Run Net 與 Session Net 兩個欄位,並且只在有變化時發送（有評分的執行,或剛進入週末暫停時發一次）,避免灌爆頻道。三個橋接都在自己的 log 檔（`monitor.log` / rss state / `chips_monitor.log`）留下每次執行一行的存活紀錄,並且會自我修剪。

---

### Discord audit for votes and no-votes

W2/W3/W4 now post to Discord on **every vote write** and on every meaningful **no-vote** (contradiction, slope disagreement, quorum not met), with W2's contradiction alert deduped per US session. W3's embed carries both a Run Net and a Session Net field and only posts on change — a scored run, or once on entering the weekend pause — instead of flooding the channel. All three bridges append a one-line-per-pass liveness record to their own self-trimming log (`monitor.log` / the rss state file / `chips_monitor.log`).

---

### W2 分頻執行

W2 的 n8n 觸發改為分層排程:TPE 21:00–05:00 美股時段每 2 分鐘,其餘時間每 10 分鐘。Yahoo 的請求量減少約 70%,而 regime 票每天只被讀一次,離峰時段的警示延遲最差也只增加約 8 分鐘。

---

### W2 tiered cadence

W2's n8n trigger is now tiered: a 2-minute cron during US hours (21:00–05:00 TPE) and 10 minutes otherwise. Yahoo load drops ~70 % with no loss — the regime vote is read once daily, and off-hours alert latency worsens by at most ~8 minutes.

---

### n8n 執行環境的實務修正

n8n 服務以 LocalSystem 身分執行,沒有使用者的 PATH、也沒有使用者的 site-packages。所有 Execute Command 節點改用**絕對 python 路徑**並帶上 `--settings`。人工確認的 tap 從 W4 更名為 **W5**（W4 的編號歸屬籌碼監控）—— 如果你的 n8n 實例裡還有一個叫「W4 Manual tap」的工作流程,請在 n8n 中改名或刪除它。`rss_scorer` 強制 stdout/stderr 使用 UTF-8。W3 的 n8n 排程改為 24 小時執行,而非僅限台股盤中時段。

---

### n8n runtime fixes

The n8n service runs as LocalSystem: no user PATH, no user site-packages. Every Execute Command node now uses an **absolute python path** plus `--settings`. The human-confirmation tap was renumbered from W4 to **W5** (the W4 slot belongs to the chips monitor) — if your n8n instance still shows a workflow named "W4 Manual tap", rename or delete it there. `rss_scorer` forces UTF-8 on stdout/stderr. W3's n8n schedule now runs 24/7 rather than only during TPE market hours.

---

### 建置流程強制要求發行說明

`create_github_release` 原本在 `release_notes_v{VERSION}.md` 不存在時,會默默退回一個只有「Release vX.Y.Z」的空白 body。由於 Discord 的發行通知是直接讀 GitHub release body,一次忘記寫說明檔就等於發出一則空公告。現在 `verify_release_notes()` 會在開始建置**之前**就中止（檔案不存在或內容過短皆然）,而且 release 一律使用 `--notes-file`。

---

### The build now requires release notes

`create_github_release` silently fell back to a bare "Release vX.Y.Z" body when `release_notes_v{VERSION}.md` was missing. Since the Discord release notification reads the GitHub release body, a forgotten notes file meant an empty announcement. `verify_release_notes()` now aborts **before** building if the file is missing or trivially short, and the release always uses `--notes-file`.

---

## 升級注意事項 Upgrade notes

### 新增的設定鍵（全部為選用,未填即維持舊行為）

把下列區塊從 `settings.example.yaml` 複製到你的 `settings.yaml`。**留空的路徑代表整個功能關閉**,所以不做任何修改就直接升級是安全的。

```yaml
news:
  enabled: false                  # 僅為部署對話框的預設值
  signal_path: ""                 # 斷路器訊號 JSON
  events_path: ""                 # 排程事件行事曆 JSON
  ledger_path: ""                 # 已消費 signal_id 帳本
  regime_vote_path: ""            # regime 投票基礎路徑
  max_signal_age_sec: 900
  tier2_enabled: false            # 僅為對話框預設值
  calendar_min_severity: "high"   # high | medium | low
  suppress_scope: "both"          # both | conflicting_leg（僅為對話框預設值）
  nightly_vote_max_age_h:         # 新增於 PR #110
    W2: 4.0
  regime_vote_thresholds:
    soxx_pct: 2.5
    tsm_pct: 2.0
    qqq_pct: 2.0
  rss_feeds: [...]                # 見 settings.example.yaml 的完整清單
  rss_interval_minutes: 30
  rss_state_file: "data/rss_scorer_state.json"
  discord_webhook: ""             # W2/W3/W4 橋接警示用的 Discord webhook

regime:
  adx_enter: 25.0
  adx_exit: 20.0
  adx_strong: 30.0                # 必須大於 adx_enter
  confirm_sessions: 2
  flip_pause_sessions: 5
  max_flips_in_window: 3
  flip_window_sessions: 10
  classify_interval: 3600
  range_bias_action: sit_out      # sit_out | short_half | long_half | both_half
  vote_quorum_up: 2               # 新增於 PR #110
  vote_quorum_down: 1             # 新增於 PR #110
  vote_range_probe: false         # 新增於 PR #111：票不得從空手盤整開探路單
  pause_freezes_exits: false      # 新增於 PR #111：暫停只凍結趨勢進場
  exits_count_as_flips: false     # 新增於 PR #111：只有趨勢進場計入翻轉
  transitional_resets_streak: false  # 新增於 PR #111：過渡判讀不重置連續計數
```

---

### New settings keys (all optional; omitting them keeps the previous behaviour)

Copy the blocks below from `settings.example.yaml` into your `settings.yaml`. **An empty path means the feature is off**, so upgrading without touching anything is safe.

```yaml
news:
  enabled: false                  # deploy-dialog default only
  signal_path: ""                 # circuit-breaker signal JSON
  events_path: ""                 # scheduled-event calendar JSON
  ledger_path: ""                 # consumed signal_id ledger
  regime_vote_path: ""            # regime-vote base path
  max_signal_age_sec: 900
  tier2_enabled: false            # dialog default only
  calendar_min_severity: "high"   # high | medium | low
  suppress_scope: "both"          # both | conflicting_leg (dialog default only)
  nightly_vote_max_age_h:         # new in PR #110
    W2: 4.0
  regime_vote_thresholds:
    soxx_pct: 2.5
    tsm_pct: 2.0
    qqq_pct: 2.0
  rss_feeds: [...]                # full list in settings.example.yaml
  rss_interval_minutes: 30
  rss_state_file: "data/rss_scorer_state.json"
  discord_webhook: ""             # Discord webhook for W2/W3/W4 bridge alerts

regime:
  adx_enter: 25.0
  adx_exit: 20.0
  adx_strong: 30.0                # must be greater than adx_enter
  confirm_sessions: 2
  flip_pause_sessions: 5
  max_flips_in_window: 3
  flip_window_sessions: 10
  classify_interval: 3600
  range_bias_action: sit_out      # sit_out | short_half | long_half | both_half
  vote_quorum_up: 2               # new in PR #110
  vote_quorum_down: 1             # new in PR #110
  vote_range_probe: false         # new in PR #111: no probe from a flat range
  pause_freezes_exits: false      # new in PR #111: pause freezes entries only
  exits_count_as_flips: false     # new in PR #111: only trend entries flip
  transitional_resets_streak: false  # new in PR #111: transitional keeps the streak
```

---

### 已經在跑的 regime 機器人升級後會看到的行為變化

- **超過 4 小時的 W2 票不再計入。** `news.nightly_vote_max_age_h` 預設 `{W2: 4.0}`。W2 通常在美股時段開火,而夜間車道要到 ~04:58 分類才讀它,所以**大部分的 W2 票升級後會被判定超齡**,記錄為 `expired-by-age` 而不交給分類器（仍然照常被消費刪除）。這是刻意的:在那段時間差上 W2 的方向 10 次只對 3 次。W3 與 W4 是日級訊號,沒有年齡上限。要恢復舊行為,把這個對應表設成 `{}`。
- **單一 ASML/SOXX 的下跌不再觸發 `risk_off`。** W2 現在需要至少 2 檔新鮮標的同向突破投票門檻、且沒有反向突破。單獨的突破只會發 Discord alert。`ASML.AS` 已降為 alert 層,單獨不能開火（單獨開火時勝率僅 20%）。
- **依外部票做多現在需要兩個來源。** `regime.vote_quorum_up` 預設 2、`vote_quorum_down` 預設 1。原本是「任何一票同意就加速」的對稱規則。盤整探路單的衝突取消規則不變且絕對:任何一張反向票都會取消探路單（探路單本身現在預設關閉,見下一則）。
- **`regime_history.csv` 會自動長出新欄位。** v3 新增 `votes`、v4 新增 `vote_rule`,採用既有的表頭原地升級機制:既有欄位位置完全不變,舊資料列維持較短（讀取端以表頭名稱查欄位並做長度保護）。**不需要手動編輯,也不需要刪除既有檔案。**
- **`news.suppress_scope` 預設維持 `both`**,也就是舊行為。要啟用只擋衝突方向的暫停,請在部署對話框勾選「方向性暫停 directional」——`settings.yaml` 的值只是對話框的預設值。
- **盤整場次不再因為外部票而開探路單,而暫停中的引擎現在可以出場走平。** `regime.vote_range_probe` 預設 `false`:外部票只能加速確認技術面本來就已經顯示的趨勢,不能從空手的盤整狀態開倉。同時 `pause_freezes_exits` / `exits_count_as_flips` / `transitional_resets_streak` 都預設 `false`,所以翻轉暫停只凍結趨勢進場,退出到盤整仍然生效。要還原舊行為,把對應的鍵設成 `true`。
- **新聞框架的啟用是逐機器人的。** 即使 `settings.yaml` 裡 `news.enabled: true`,已經在跑的機器人續跑時仍以它自己的 `session.json` 為準;新部署則由對話框的核取方塊決定。

---

### Behaviour changes a running regime bot will see after upgrading

- **W2 votes older than 4 hours no longer count.** `news.nightly_vote_max_age_h` defaults to `{W2: 4.0}`. W2 typically fires during the US session, and the nightly lane only reads votes at the ~04:58 classification, so **most W2 votes will now be judged over-age**, logged as `expired-by-age` and withheld from the classifier (they are still consumed and deleted). This is deliberate: over that lag W2 was right 3 times in 10. W3 and W4 are daily-cadence signals and have no age limit. Set the mapping to `{}` to restore the old behaviour.
- **A single ASML/SOXX drop no longer fires `risk_off`.** W2 now needs at least 2 fresh symbols breaching their vote thresholds in the same direction with none breaching the other way. A lone breach posts a Discord alert only. `ASML.AS` is now alert tier and cannot fire on its own (20 % win as a lone trigger).
- **Going long on votes now needs two sources.** `regime.vote_quorum_up` defaults to 2, `vote_quorum_down` to 1. The old rule was symmetric — any single agreeing vote accelerated. Conflict-cancel in the range-bound probe is unchanged and absolute: any opposing vote kills the probe (and the probe itself is now off by default — see the next bullet).
- **`regime_history.csv` gains new columns automatically.** v3 adds `votes`, v4 adds `vote_rule`, both through the existing additive header upgrade: existing column positions are unchanged and old data rows stay short (readers look columns up by header name and length-guard). **No manual edit and no file deletion is required.**
- **`news.suppress_scope` still defaults to `both`**, the legacy behaviour. To get conflicting-leg-only suppression, tick 「方向性暫停 directional」 in the deploy dialog — the `settings.yaml` value is only that checkbox's default.
- **A range-bound regime no longer opens vote probes, and a paused engine can now go flat.** `regime.vote_range_probe` defaults to `false`: external votes may only accelerate confirmation of a trend the technicals already show, never open a position out of a flat range. Alongside it, `pause_freezes_exits` / `exits_count_as_flips` / `transitional_resets_streak` all default to `false`, so a flip pause freezes trend entries only and an exit to range-bound still applies. Set the matching key to `true` to restore the legacy behaviour.
- **News-framework enablement is per bot.** Even with `news.enabled: true` in `settings.yaml`, a resumed bot follows its own `session.json`; a fresh deploy follows the dialog checkboxes.
