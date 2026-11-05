# v2.19.16

## EXE 變更 EXE changes

- **#245**（squash `080cca7d`，取代 #241）Discord 通知改為單一背景送出執行緒：
  - 不再每則訊息開一條執行緒；一個 worker 依序送出（`src/live/discord_notify.py`）。一般訊息佇列上限 200（`SEND_QUEUE_MAX`），優先訊息另有 200 則 backlog（`PRIORITY_BACKLOG_MAX`），worker 永遠先送優先訊息。佇列滿時先丟一般訊息；恢復後送一則「Discord was stalled: … dropped, … delivered late」彙總。
  - 列為優先：委託、委託失敗、成交確認、出場、日損、模式切換、連線中斷／恢復、權益數暫停警示、結算日平倉、新聞斷路器動作、HUNG 警示（`run_backtest.py` 呼叫處改為 `priority=True`）。HTTP 逾時明定為 10 秒（連線 5 秒）；429 重試留在 worker 內。
  - 只改通知送出方式，不改下單、進場、出場邏輯。

  **#245** (squash `080cca7d`, supersedes #241) Discord notifications move to one background send worker:
  - No more one thread per message; a single worker sends in order (`src/live/discord_notify.py`). Routine queue capped at 200 (`SEND_QUEUE_MAX`), plus a separate 200-message priority backlog (`PRIORITY_BACKLOG_MAX`) that the worker always drains first. When full, routine messages are dropped first; after recovery one "Discord was stalled: … dropped, … delivered late" summary is posted.
  - Priority: orders, order failures, fill confirmations, exits, daily loss, mode switches, link loss/recovery, equity-hold alerts, settlement flatten, news-breaker actions and HUNG alerts (call sites in `run_backtest.py` now pass `priority=True`). HTTP timeout is explicit at 10 s (5 s connect); 429 retries stay inside the worker.
  - Changes how notifications are sent only; order, entry and exit logic are unchanged.

- **#237**（squash `9dea7a33`，修正 #233）掛起偵測補強：
  - HUNG 結束時送一則 `[HANG] <bot> RECOVERED after N s`，並把 `recovered_at`、`stall_s` 寫入 hang.json（`src/live/main_heartbeat.py`）。
  - 同一部署根目錄下 120 秒內有 3 個以上 bot 進入 HUNG 時，額外送一則主機停頓彙總與一則恢復彙總（新檔 `src/live/host_stall.py`）。各 bot 自己的 HUNG 警示不會被壓掉。
  - 台灣國定假日每年快取一次；`taifex_closures.yaml` 最多每 60 秒檢查一次（`src/market_data/holidays.py`）。查詢失敗不快取。
  - 只改監控與警示，不改下單、進場、出場邏輯。

  **#237** (squash `9dea7a33`, fixes #233) hang-detection follow-ups:
  - When a HUNG episode ends, one `[HANG] <bot> RECOVERED after N s` alert is sent and `recovered_at` / `stall_s` are written to hang.json (`src/live/main_heartbeat.py`).
  - When 3 or more bots under the same deploy root go HUNG within 120 s, one extra host-stall rollup and one recovery rollup are sent (new `src/live/host_stall.py`). Per-bot HUNG alerts are never suppressed.
  - TW public holidays are cached per year, and `taifex_closures.yaml` is checked at most every 60 s (`src/market_data/holidays.py`). Lookup failures are not cached.
  - Monitoring and alerts only; order, entry and exit logic are unchanged.

## 非 EXE 變更 Non-EXE changes

- **#247**（squash `3412e471`）儀表板「新聞訊號」頁：
  - 中文：儀表板新增「新聞訊號」頁面，列出 W3 RSS 新聞評分（來源、標題連結、分數、盤別累計與 ±1.5 投票門檻），可依盤別／分數／來源篩選。W3 評分程式會把每篇已評分文章寫入 news_log.jsonl；歷史資料從新版 rss_scorer.py 開始執行後才累積，不回補。投票邏輯不變。
  - English: The dashboard adds a 新聞訊號 (news signals) view of W3 RSS news scores (source, linked title, score, session running total vs the ±1.5 vote threshold), filterable by session, score and source. The W3 scorer now appends each scored article to news_log.jsonl; history starts only once the new rss_scorer.py runs, with no backfill. Vote logic is unchanged.

## 操作影響 Operator impact

- 換 EXE 後 Discord 訊息改由單一執行緒送出；網路停頓時一般訊息可能被丟棄並於恢復後彙總，交易相關訊息會延後送達而不丟。 / After the EXE swap, Discord sends go through one worker; during a network stall routine messages may be dropped and summarized afterwards, while trading messages are delayed rather than dropped.
- 新增 RECOVERED 與主機停頓彙總警示訊息。 / New RECOVERED and host-stall rollup alert lines.
- #247 不在 EXE 內：儀表板與 W3 排程需更新到此版程式碼；若 W3 的 state 路徑與儀表板 `news.rss_state_file` 不同，兩邊需指定相同的 `--news-log`（或 `news.rss_news_log`）。 / #247 is not in the EXE: the dashboard and the W3 scheduled scorer must run this commit; if the W3 state path differs from the dashboard's `news.rss_state_file`, point both at the same `--news-log` (or `news.rss_news_log`).

## 風險 Risks

- Discord 通知架構改寫：若 worker 異常，通知可能延遲；單一送出失敗只重試一次後放棄，worker 不會因此停止。 / The Discord notifier is rewritten: a worker problem could delay notifications; a raised send is retried once then abandoned, and does not kill the worker.
- 主機停頓彙總依賴部署根目錄下的共用檔案（`host_stall.*`）；只影響警示。 / The host-stall rollup uses shared files (`host_stall.*`) under the deploy root; alerts only.

## 升級注意 Upgrade / redeploy notes

- **換 EXE**：正常停止 bot，保留 v2.19.15 資料夾或 zip，解壓 v2.19.16 取代 `tai_backtest.exe` 與 `_internal\`（保留 `settings.yaml`、`data\`、`strategies\`、`taifex_closures.yaml`），重新啟動並確認標題為 v2.19.16。 / **EXE swap**: stop the bot cleanly, keep the v2.19.15 folder or zip, unzip v2.19.16 and replace `tai_backtest.exe` and `_internal\` (keep `settings.yaml`, `data\`, `strategies\`, `taifex_closures.yaml`), relaunch and check the title shows v2.19.16.

## 回退 Rollback

- EXE：停止 bot，換回 v2.19.15 release zip（sha256 `05a30ca07207859134edeba11cac323f5074dc8c4073c8b517a83c35478d3a5e`）後重新啟動。 / EXE: stop the bot, swap back to the v2.19.15 release zip (sha256 `05a30ca07207859134edeba11cac323f5074dc8c4073c8b517a83c35478d3a5e`) and relaunch.

## 已知問題 Known issues

- **Windows pytest 已知失敗（同 v2.19.15）**：`tests/test_backfill_bars.py::test_refuses_target_outside_backfill`（WinError 1314）與 `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress`（WinError 5）。 / **Known Windows pytest failures (same as v2.19.15)**: `tests/test_backfill_bars.py::test_refuses_target_outside_backfill` (WinError 1314) and `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress` (WinError 5).
- **儀表板停止僅限 headless**（不變）。 / **Dashboard stop is headless-only** (unchanged).
- 假日缺漏 [#209](https://github.com/ericwu13/tai-robot/issues/209) 不變。 / Holiday gap [#209](https://github.com/ericwu13/tai-robot/issues/209) unchanged.
- 未包含：#242（預設日損 20,000 與交易日邊界重置）、#244；#241 已關閉未合併。預設日損仍為 10,000 NTD。 / Not in this release: #242 (20,000 default and trading-day boundary reset), #244; #241 was closed unmerged. The default daily loss limit remains 10,000 NTD.
