# v2.19.20

## EXE 變更 EXE changes

- **#282**（squash `b7a7abfb`）部署／恢復中止時停止 bot 但不平倉（Eric 2026-11-13 決定）：
  - 部署或 `--resume` 在開始接收即時報價前失敗（暖機無資料來源、COM 暖機查詢失敗或錯誤、30 秒暖機逾時、TV 不支援週期、TV 暖機失敗或錯誤、未連線、Tick 訂閱失敗或錯誤，共 10 處）時，改走 `_abort_deploy(reason)`：**不送單、不強制平倉**，`session.json` 保留持倉與出場價位，可手動平倉或 `--resume`。
  - 通知：debug log `[DEPLOY ABORT]`、即時頁訊息、GUI 彈窗（headless 只記 log），Discord 啟用時發送優先警報「🚨 部署中止 Deploy aborted [模式]: 原因…」，列出商品、方向、口數。
  - v2.19.19 及更早版本：同樣的中止在實單已確認持倉時會送出停止平倉單。Paper 與 regime-switching bot 同樣處理；headless 若在暖機設定階段中止（無資料來源、COM 暖機查詢失敗或錯誤、TV 不支援週期）以 exit code 4（deploy refused）結束，之後的中止（暖機逾時、TV 暖機錯誤、Tick 訂閱）以 exit code 5（bot stopped）結束；兩者都會記 `[DEPLOY ABORT]` 並發 Discord 警報，heartbeat 檔如一般停止般移除。
  - 不變：「停止機器人」按鈕、headless STOP／Ctrl+C、儀表板停止仍會平倉。中止時不產生每日報告，之後平倉的報告仍會送出。

  **#282** (squash `b7a7abfb`) a deploy/resume abort stops the bot without flattening (Eric's 2026-11-13 decision):
  - A deploy or `--resume` that fails before live ticks (no warmup source, COM warmup fetch failure or error, 30 s warmup timeout, TV unsupported interval, TV warmup failure or error, not connected, tick-subscribe failure or error — 10 sites) now goes through `_abort_deploy(reason)`: **no order is sent and the position is not force-closed**; `session.json` keeps the position and exit levels, so the operator can close by hand or `--resume`.
  - Signals: debug log `[DEPLOY ABORT]`, a Live-tab line, a GUI popup (headless logs it only), and, when Discord is enabled, a priority alert "🚨 部署中止 Deploy aborted [mode]: reason…" naming symbol, side and quantity.
  - On v2.19.19 and earlier the same abort sent a stop-close order when a confirmed real position was open. Paper and regime-switching bots are handled the same way; headless exits with code 4 (deploy refused) when the abort happens during warmup setup (no source, COM fetch failure or error, TV unsupported interval) and with code 5 (bot stopped) for later aborts (warmup timeout, TV warmup error, tick subscribe); both log `[DEPLOY ABORT]` and send the Discord alert, and the heartbeat file is removed as on a normal stop.
  - Unchanged: the Stop Bot button, headless STOP / Ctrl+C and the dashboard stop still flatten. The abort skips the daily report, so the later exit is still reported.

- **#292**（squash `d06d5cff`）週六演化網格（knob grid）：進度心跳、依規模調整時限、每個安裝資料夾一次一個網格：
  - 網格子行程每完成一組設定回報進度；超過 10 分鐘沒有進度判定為 `stalled`。總時限改為 max(20 分鐘, 90 秒 × 次數)，上限 3 小時（原固定 20 分鐘）。
  - 同一安裝資料夾一次只跑一個網格：以 `data/evo_grid.slot` 排隊（每 5 秒輪詢、可被 STOP 中斷，最多等 9 小時後以 `queue_timeout` 失敗）；Windows 上網格子行程以 BELOW_NORMAL 優先權執行。
  - 失敗仍記為 `grid_failed`（#262 重試規則不變）；`evolution_history.jsonl` 另記 `grid_failure_kind`（timeout／stalled／crash／stopped／queue_timeout）、`grid_progress`、`grid_elapsed_s`、`grid_queue_s`。
  - 「已開始」時段保護由 45 分鐘延長為 3 小時 25 分鐘，期間重啟不會重跑同週演化。部分結果仍丟棄；選擇邏輯、門檻、holdout 不變；無新設定鍵。log 新增「EVO grid waiting / acquired host slot」「EVO grid progress k/n」「EVO grid finished …」。

  **#292** (squash `d06d5cff`) Saturday evolution knob grid: progress heartbeat, scaled budget, one grid per install folder:
  - The grid child reports progress after each setting; 10 min without progress is `stalled`. The overall budget is max(20 min, 90 s × runs), capped at 3 h (was a flat 20 min).
  - One grid runs at a time per install folder: bots queue on `data/evo_grid.slot` (5 s poll, STOP honoured, bounded at 9 h, then `queue_timeout`); on Windows the grid child runs at BELOW_NORMAL priority.
  - Failures stay `grid_failed` (the #262 retry set is unchanged); `evolution_history.jsonl` also records `grid_failure_kind` (timeout / stalled / crash / stopped / queue_timeout), `grid_progress`, `grid_elapsed_s`, `grid_queue_s`.
  - The "started" slot guard grows from 45 min to 3 h 25 min, so a restart inside that window does not re-run that week's evolution. Partial results are still discarded; selection, gates and holdout are unchanged; no new settings key. New log lines: "EVO grid waiting / acquired host slot", "EVO grid progress k/n", "EVO grid finished …".

## 非 EXE Non-EXE (bridge / dashboard / monitor)

- **#283**（squash `773c7e67`）W3／W4 後續修正：W3 RSS 去重新增 `guid_v2_at` 時間戳，同連結但較新發布的公告（如期交所結算／保證金）不再被誤略；W4 期交所在本盤投票已決定後回傳空結果時視為「無資料」，保留該投票與 `last_decided_date`；證交所回應成功但零筆資料也視為「無資料」，exit 0 且不標記當日已決，磁碟上的投票除非來自同一份資料集否則仍會刪除；儀表板籌碼面板顯示「證交所現貨資料尚無內容，不投票（稍後重試）」；`check_bridge` 的連續無票計數對「fetch failed」行既不累加也不中斷。 / **#283** (squash `773c7e67`) W3/W4 follow-up: W3 RSS dedup stamps `guid_v2_at`, so newer notices sharing a link (e.g. TAIFEX settlement/margin) are no longer dropped; W4 treats an empty TAIFEX result, after this session's vote was already decided, as no-data and keeps that vote and `last_decided_date`; a TWSE OK body with zero rows is also no-data, exits 0 and leaves the day undecided, but the on-disk vote is still deleted unless it came from the same dataset; the dashboard chips panel shows 證交所現貨資料尚無內容，不投票（稍後重試） for that case; `check_bridge`'s voteless streak neither counts nor ends on "fetch failed" lines.

## 操作注意 Operator caveats

- **#282 中止後持倉無人管理**：中止的 bot 已停止，持倉仍在券商端，沒有策略或出場管理，直到操作員處理。看到 🚨 部署中止、headless exit 4 或 5、或 bot 從機隊消失時，請先確認該商品券商持倉，再修正原因後 `--resume` 或手動平倉。watchdog 不會回報 HUNG（heartbeat 已移除）。 / **#282 an aborted bot leaves the position unmanaged**: the bot is stopped while the position stays at the broker with no strategy or exit management until an operator acts. On a 🚨 Deploy aborted alert, a headless exit 4 or 5, or a bot missing from the fleet, check the broker position for that symbol, then fix the cause and `--resume`, or close by hand. The watchdog will not report HUNG (the heartbeat is removed).
- **#282 換版**：以 `--resume` 重啟的 bot 若中止，會停止並保留持倉；換版時請逐一確認重啟的 bot 確實在執行（無 exit 4／5、無 `[DEPLOY ABORT]`、無 🚨）。 / **#282 swaps**: a bot relaunched with `--resume` that aborts stops and keeps its position; during a swap, confirm each relaunched bot is actually running (no exit 4 or 5, no `[DEPLOY ABORT]`, no 🚨) before moving on.
- **#292 週六網格改為依序執行**：共用同一 EXE 資料夾的 bot 會逐一執行網格，排在後面的 bot 可能晚數小時完成，或等滿 9 小時以 `queue_timeout`（可重試）失敗。`data/evo_grid.slot` 是作業系統檔案鎖，持有的行程結束即自動釋放；殘留的檔案本身不會擋住下一個網格，執行中請勿刪除。 / **#292 Saturday grids now run one at a time**: bots sharing one EXE folder run their grids in turn, so later bots may finish hours later or fail with `queue_timeout` (retryable) after 9 h. `data/evo_grid.slot` is an OS file lock that is released when the holding process exits; a leftover file does not block the next grid by itself. Do not delete it while a grid runs.

## 升級注意 Upgrade / redeploy notes

- **換 EXE**：正常停止 bot，保留 v2.19.19 資料夾或 zip，解壓 v2.19.20 取代 `tai_backtest.exe` 與 `_internal\`（保留 `settings.yaml`、`data\`、`strategies\`、`taifex_closures.yaml`；不要用 zip 內的範本 `settings.yaml` 覆蓋），重新啟動並確認標題為 v2.19.20。持倉中的 bot 請待平倉後再換版。 / **EXE swap**: stop cleanly, keep the v2.19.19 folder or zip, unzip v2.19.20 and replace `tai_backtest.exe` and `_internal\` (keep `settings.yaml`, `data\`, `strategies\`, `taifex_closures.yaml`; do not copy the zip's template `settings.yaml` over yours), relaunch and confirm the title shows v2.19.20. Swap a bot holding a position only after it is flat.
- **#282、#292 需換 EXE** 才生效。 / **#282 and #292 need an EXE swap** to take effect.
- **#283 不在 EXE 內**：需把 W3／W4 news_bridge 所用 checkout 與儀表板 worktree 更新到含 #283 的 master；交易 EXE 無需重啟。 / **#283 is not in the EXE**: it takes effect once the W3/W4 news_bridge checkouts and the dashboard worktree move to a master that includes #283; trading EXEs need no restart.

## 回退 Rollback

- EXE：停止 bot，換回 v2.19.19 release zip（sha256 `29c4a0f64482998e5c8cc768b3ac2b227e7a19227a6df6e14ac43dbc450095e2`）後重新啟動。`data/evo_grid.slot` 與歷史中的新欄位 v2.19.19 不會讀取，可保留。 / EXE: stop the bot, swap back to the v2.19.19 release zip (sha256 `29c4a0f64482998e5c8cc768b3ac2b227e7a19227a6df6e14ac43dbc450095e2`), relaunch. v2.19.19 does not read `data/evo_grid.slot` or the new history fields; they may stay.

## 已知問題 Known issues

- **Windows pytest**：v2.19.19 的兩個已知 Windows 失敗（`test_backfill_bars.py::test_refuses_target_outside_backfill` WinError 1314、`test_hung_heartbeat.py::test_windows_reader_writer_stress` WinError 5）已由 #311 修正，本版 Windows 全套測試 0 失敗。#312 修正 #292 鎖測試在 Windows spawn 下的執行，#313 修正 grid 子程序結束檢查在 Windows 高負載下的逾時（皆只改測試）。 / **Windows pytest**: the two known Windows failures from v2.19.19 (`test_backfill_bars.py::test_refuses_target_outside_backfill` WinError 1314 and `test_hung_heartbeat.py::test_windows_reader_writer_stress` WinError 5) are fixed by #311; the full Windows suite has 0 failures on this release. #312 makes the #292 lock tests run under Windows spawn, and #313 makes the grid child-exit check Windows-safe under load (both tests only).
- [#306](https://github.com/ericwu13/tai-robot/issues/306) 05:00 暫停延續（同 v2.19.19）。 / [#306](https://github.com/ericwu13/tai-robot/issues/306) 05:00 pause carry (same as v2.19.19).
- [#285](https://github.com/ericwu13/tai-robot/issues/285) headless 卡住尚未宣稱解決。 / [#285](https://github.com/ericwu13/tai-robot/issues/285) headless hang not claimed solved.
- 假日缺漏 [#209](https://github.com/ericwu13/tai-robot/issues/209) 不變。 / Holiday gap [#209](https://github.com/ericwu13/tai-robot/issues/209) unchanged.
