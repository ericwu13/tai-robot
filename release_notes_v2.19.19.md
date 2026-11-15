# v2.19.19

## EXE 變更 EXE changes

- **#293**（squash `b3c95b4a`）實單每日虧損上限改為夜盤另計（Eric 選項 B）：
  - 新增 `src/live/loss_baseline.py` 的 `DailyLossBaseline`。快照 = 當日（已知 TAIFEX 交易日）13:45–15:00 TPE 之間最後一筆有效的帳戶權益讀數（FutureRights 淨損益）。
  - **夜盤（15:00–05:00 TPE）**：有同一交易日的有效快照時，計算損益 L = 帳戶淨損益 − 快照，日盤的盈虧不再計入夜盤額度。**其他時間（05:00–15:00，含日盤）以及沒有有效快照時**：L = 完整帳戶淨損益，與 v2.19.18 相同。讀數無效時也用帳戶淨損益。
  - 實際效果：日盤觸發上限而暫停的 bot，15:00 換日釋放後若快照有效，不會再被日盤虧損立即重新暫停，夜盤可以交易（v2.19.18 會因帳戶淨損益仍含日盤虧損而重新暫停）。夜盤有自己的上限額度。
  - 暫停閂鎖本身不變（`trading_guard.py` 與 v2.19.18 相同）：只在 15:00 換日釋放並立即重新檢查。夜盤觸發的暫停仍會持續到下一個日盤結束（次日 15:00）。
  - 快照存於各 bot 目錄的 `loss_baseline.json`（原子寫入，帳號只存雜湊）。重啟後只有同一交易日且讀取時間落在 13:45–15:00 的檔案才會還原；15:00 後才啟動且沒有該檔的 bot，當晚使用完整帳戶淨損益。
  - Paper bot 也會記錄快照（供之後切換全自動使用），但不會因此暫停。Paper → 全自動切換的檢查使用同一個 L。
  - 顯示：heartbeat 的 `loss_net` 改為計入上限的 L，並新增 `loss_account_net`、`loss_day`、`loss_basis`（`snapshot`／`master`）；儀表板顯示「today X (acct Y)」；Discord 每日虧損上限訊息同時列出「今日損益」與「帳戶淨損益」。OpenInterest 回報只更新持倉文字，不再用快取權益重算。
  - 新增 `tests/test_loss_limit_simple.py`。`settings.example.yaml` 只更新說明；`trading.daily_loss_limit_ntd` 預設值不變。

  **#293** (squash `b3c95b4a`) the REAL daily loss limit now gives the night session its own budget (Eric's option B):
  - New `DailyLossBaseline` in `src/live/loss_baseline.py`. The snapshot is the last valid account-rights read (FutureRights net) between 13:45 and 15:00 TPE on a known TAIFEX trading day.
  - **Night session (15:00–05:00 TPE)**: with a valid snapshot for that trading day, the counted loss L = account net − snapshot, so the day session's P&L no longer uses up the night budget. **At all other times (05:00–15:00, including the day session) and whenever there is no valid snapshot**: L = the full account net, exactly as in v2.19.18. An unusable read also falls back to the account net.
  - In practice: a bot paused by a day-session loss is released at the 15:00 roll and, with a valid snapshot, is not re-paused by that day loss, so it can trade the night session (v2.19.18 re-paused it because the account net still carried the day loss). The night session has its own limit.
  - The pause latch itself is unchanged (`trading_guard.py` is identical to v2.19.18): it releases only at the 15:00 roll and re-checks at once. A pause set in the night session still holds through the next day session, until the following 15:00.
  - The snapshot is stored as `loss_baseline.json` in each bot dir (atomic write, account kept only as a hash). After a restart it is restored only from a same-trading-day file whose read time is inside 13:45–15:00; a bot started after 15:00 without that file uses the full account net for that night.
  - Paper bots also record the snapshot (for a later switch to auto) but never pause on it. The paper → auto switch check uses the same L.
  - Display: heartbeat `loss_net` now carries the gated L, plus new `loss_account_net`, `loss_day`, `loss_basis` (`snapshot` / `master`); the dashboard shows "today X (acct Y)"; the Discord daily-loss-limit alert lists both 今日損益 and 帳戶淨損益. An OpenInterest row now updates the position text only and no longer re-renders from cached rights.
  - New `tests/test_loss_limit_simple.py`. `settings.example.yaml` comment only; the `trading.daily_loss_limit_ntd` default is unchanged.

- **#286**（squash `49ef90b2`）Headless 模式：無視窗執行時，不再重建看不見的「報告」分頁卡片（約 70 個元件）。這項工作原本在處理 COM 報價與 tick 出場的同一執行緒上執行，在 #285 的 4 次卡住樣本中有 3 次出現在此處。只覆寫 `src/live/headless_app.py` 的 `_render_report_view`；交易列表、績效指標、session 儲存及所有下單／出場流程皆不變。GUI（有視窗）模式不受影響。此為強化措施，可降低風險，但尚未宣稱已解決 #285。 / **#286** (squash `49ef90b2`) headless mode no longer rebuilds the invisible Report tab cards (~70 widgets) while running without a window. That work ran on the same thread that drains COM ticks and runs tick exits, and it was the sampled frame in 3 of 4 #285 hang dumps. Only `_render_report_view` is overridden in `src/live/headless_app.py`; the trade list, metrics, session save and every order/exit path are unchanged, and windowed GUI mode is not affected. Hardening that reduces exposure; #285 is not claimed solved.

## 操作注意 Operator caveats

- **#293 夜盤上限較寬**：日盤虧損時，夜盤額度從快照重新起算，因此同一帳戶在一個日盤加夜盤內的總虧損可能超過單一上限（日盤最多約一個上限，夜盤再一個上限）。這是 Eric 選定的規則。 / **#293 the night limit is looser after a losing day**: the night budget restarts from the snapshot, so one account can lose more than one limit across a day session plus the following night (up to about one limit each). This is the rule Eric chose.
- **#293 05:00 至券商重置**：05:00 到券商每日重置（約 05:22–05:36）之間 L 為完整帳戶淨損益，此時觸發的暫停會延續到下一個日盤；與 v2.19.18 相同，追蹤於 [#306](https://github.com/ericwu13/tai-robot/issues/306)。 / **#293 05:00 to the broker reset**: from 05:00 until the broker's daily reset (~05:22–05:36) L is the full account net, and a pause latched then carries into the next day session; same as v2.19.18, tracked in [#306](https://github.com/ericwu13/tai-robot/issues/306).
- **#293 同帳戶兩個實單 bot**：快照是每個 bot 各自記錄帳戶淨損益，同帳戶多個實單 bot 的分攤尚未處理（與 v2.19.18 相同，皆看整個帳戶）。 / **#293 two REAL bots on one account**: each bot snapshots the account net on its own; splitting one account across several REAL bots is deferred (both see the whole account, as in v2.19.18).
- **#293 監控讀值**：讀取 heartbeat `loss_net` 的監控或報表，換版後拿到的是計入上限的 L（夜盤可能不等於帳戶淨損益）；帳戶淨損益改看 `loss_account_net`。 / **#293 monitoring reads**: anything reading heartbeat `loss_net` gets the gated L after the swap (at night it can differ from the account net); use `loss_account_net` for the account figure.

## 升級注意 Upgrade / redeploy notes

- **換 EXE**：正常停止 bot，保留 v2.19.18（或目前版本）資料夾或 zip，解壓 v2.19.19 取代 `tai_backtest.exe` 與 `_internal\`（保留 `settings.yaml`、`data\`、`strategies\`、`taifex_closures.yaml`），重新啟動並確認標題為 v2.19.19。持倉中的 bot 請待平倉後再換版。 / **EXE swap**: stop cleanly, keep the v2.19.18 (or current) folder or zip, unzip v2.19.19 and replace `tai_backtest.exe` and `_internal\` (keep `settings.yaml`, `data\`, `strategies\`, `taifex_closures.yaml`), relaunch and confirm the title shows v2.19.19. Swap a bot holding a position only after it is flat.
- **#293 需換 EXE**：新規則需含 #293 的建置並重啟。重啟會清除記憶體中的暫停；實單／全自動 bot 在第一筆權益回報更新上限前不會開新倉。第一個夜盤要用快照，bot 必須在當日 13:45–15:00 TPE 之間運行並讀到權益；在 TPE 13:45 之前完成換版即可。 / **#293 needs an EXE swap**: the new rule needs a build with #293 plus restart. A restart clears the in-memory pause; real/auto bots open no new entry until the first rights reply updates the gate. For the first night to use a snapshot, the bot must be running with a rights read between 13:45 and 15:00 TPE that day; swapping before 13:45 TPE is enough.
- **#286 需換 EXE**：只影響以 headless 模式執行的 bot。 / **#286 needs an EXE swap**: affects bots run in headless mode only.
- **儀表板**：`today X (acct Y)` 顯示需把儀表板主機的 `tai-robot-dashboard` worktree 更新到含 #293 的 master；未更新時仍顯示 `net X`（數值為 heartbeat 的 `loss_net`）。 / **Dashboard**: the `today X (acct Y)` text needs the dashboard host's `tai-robot-dashboard` worktree on a master with #293; until then it shows `net X` (the heartbeat `loss_net`).

## 回退 Rollback

- EXE：停止 bot，換回 v2.19.18 release zip（sha256 `02fef43fc74580df6efc16572d38d76f3ece60d54d89bf7b13f1192af3e93dcf`）後重新啟動。v2.19.18 不讀 `loss_baseline.json`，檔案可保留或刪除。 / EXE: stop the bot, swap back to the v2.19.18 release zip (sha256 `02fef43fc74580df6efc16572d38d76f3ece60d54d89bf7b13f1192af3e93dcf`), relaunch. v2.19.18 does not read `loss_baseline.json`; the file may be kept or deleted.

## 已知問題 Known issues

- [#306](https://github.com/ericwu13/tai-robot/issues/306) 05:00 暫停延續（同 v2.19.18）。 / [#306](https://github.com/ericwu13/tai-robot/issues/306) 05:00 pause carry (same as v2.19.18).
- [#285](https://github.com/ericwu13/tai-robot/issues/285) headless 卡住尚未宣稱解決。 / [#285](https://github.com/ericwu13/tai-robot/issues/285) headless hang not claimed solved.
- **Windows pytest 已知失敗（同 v2.19.18）**：`tests/test_backfill_bars.py::test_refuses_target_outside_backfill`（WinError 1314）與 `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress`（WinError 5）。 / **Known Windows pytest failures (same as v2.19.18)**: `tests/test_backfill_bars.py::test_refuses_target_outside_backfill` (WinError 1314) and `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress` (WinError 5).
- 假日缺漏 [#209](https://github.com/ericwu13/tai-robot/issues/209) 不變。 / Holiday gap [#209](https://github.com/ericwu13/tai-robot/issues/209) unchanged.
