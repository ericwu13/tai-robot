# v2.19.12

> 草稿 / DRAFT — 尚未打 tag 或發布 / not tagged or published. Base: master `fc1943b6` (v2.19.11 `00e67b4d` + #229). #231 待合併 / pending merge.

## EXE 變更 EXE changes

- **#231（待合併 / pending merge）**：修正 #230 回報連線中斷後卡住的問題。
  - 依程式順序：回報連線輪詢第一次讀到中斷（0）時，立即暫停新進場；接著在連續 0 讀數涵蓋回報穩定時間後，嘗試一次 `ConnectByID`；若該次呼叫後 90 秒連線仍未恢復，bot 降級、新進場維持暫停，並送出一次警示：`[CONN] reply link still down 90s after one ConnectByID; entries paused; no further automatic reconnect; operator reconnect or restart needed`。之後需由操作人員手動重連或重啟；沒有自動重試。
  - 中斷期間暫停定期帳戶查詢（`GetOpenInterestGW`／`GetFutureRights`），但成交確認的部位查詢照常執行，出場成交仍可確認，不會因 10 秒成交逾時而降級。
  - 連線恢復後，全自動／半自動會先查詢一次帳戶權益（`GetFutureRights`）並套用，之後才允許新進場；虧損上限與保證金檢查使用新數值。
  - 回報連線中斷時，儀表板切換為全自動（實單）的請求會被拒絕，且不查詢券商；bot 維持原模式。
  - 另含：SKCOM 事件參數個數相容、輪詢例外後重新啟用回報閘門、卡住警示註明模式與部位、卡住證據依修改時間封存、時戳寫入失敗與看門狗時間跳躍均記錄。
  **#231 (pending merge)**: fixes the #230 hang after a reply-link drop.
  - In the code's order: entries pause on the first down (0) reply-link poll reading; after consecutive 0 readings that span the reply settle, one `ConnectByID` is tried; if the link is not back 90 s after that call, the bot degrades, keeps entries paused and sends one alert: `[CONN] reply link still down 90s after one ConnectByID; entries paused; no further automatic reconnect; operator reconnect or restart needed`. An operator reconnect or restart is then needed; there are no automatic retries.
  - During the drop the periodic account poll (`GetOpenInterestGW`/`GetFutureRights`) is held, but the fill-confirmation position query still runs, so exit fills confirm and the 10 s fill timeout does not downgrade the bot.
  - After recovery, auto/semi-auto bots read and apply equity (`GetFutureRights`) once before new entries reopen; the loss-limit and margin checks use the fresh value.
  - While the reply link is down, a dashboard request to switch to auto (real) is refused without any broker query; the bot stays on its current mode.
  - Also: SKCOM event-sink arity tolerance, the reply gate re-armed after a poll error, the hung alert names mode and position, hang evidence archived by mtime, and stamp-write failures and watchdog wake gaps are logged.

## 開發／維運 Developer / ops

- **#229**（squash `fc1943b6`）：儀表板機器人總覽新增「本交易日實單」欄位（自最近一次 15:00 夜盤開盤起平倉、進出場皆有可信成交價的實單損益，不含估算，未扣手續費及期交稅）；明細交易清單依交易日分為「本交易日」（含實單小計）與「先前」兩組。原有「本交易日」與「實單累計」意義不變。 / **#229** (squash `fc1943b6`): dashboard fleet table gains a 本交易日實單 column (real P&L of trades closed since the last 15:00 night open, counting only trades with trusted entry and exit fills, estimates excluded, before fees and tax); the bot detail trade list is grouped into the current trading day (with a real-only subtotal) and earlier trades. The existing 本交易日 and 實單累計 figures keep their meaning.

## 操作影響 Operator impact

- **#229**：更新並重啟儀表板後才會出現新欄位；不影響 bot。 / **#229**: the new column appears after the dashboard is updated and restarted; bots are unaffected.
- **#231**：回報連線中斷時新進場會暫停，恢復後先更新權益才重新開放；若 `ConnectByID` 後 90 秒仍未恢復，會收到一次警示 `[CONN] reply link still down 90s after one ConnectByID; entries paused; no further automatic reconnect; operator reconnect or restart needed`，且不再自動重連，需人工重連或重啟。 / **#231**: new entries pause during a reply-link drop and reopen only after an equity refresh; if the link is not back 90 s after the one `ConnectByID` you get one alert, `[CONN] reply link still down 90s after one ConnectByID; entries paused; no further automatic reconnect; operator reconnect or restart needed`, and the bot does not reconnect on its own, so reconnect or restart it.

## 風險 Risks

- **#231 單次重連**：每次中斷只重連一次；90 秒內未恢復即需人工處理，新進場維持暫停（出場仍可成交確認）。 / **#231 single reconnect**: one reconnect per drop; if it is not back within 90 s an operator must act, and new entries stay paused (exits still confirm).
- **#231 恢復後進場延遲**：新進場要等權益查詢回覆後才開放，查詢未回覆時會持續擋住新進場。 / **#231 entry delay after recovery**: entries wait for the equity reply; if it never arrives, entries stay held.
- **#229**：僅儀表板顯示，無交易行為變更。 / **#229**: dashboard display only, no trading change.

## 升級注意 Upgrade / redeploy notes

- 將監控與儀表板的程式碼更新到 master 並重新啟動。 / Update the monitor and dashboard checkouts to master and restart them.
- **換 EXE**（#231 屬 EXE 變更）：正常停止 bot，保留 v2.19.11 資料夾或 zip，解壓 v2.19.12 取代 `tai_backtest.exe` 與 `_internal\`（保留 `settings.yaml`、`data\`、`strategies\`、`taifex_closures.yaml`），重新啟動並確認標題為 v2.19.12。不需修改設定檔。 / **EXE swap** (#231 changes the EXE): stop the bot cleanly, keep the v2.19.11 folder or zip, unzip v2.19.12 and replace `tai_backtest.exe` and `_internal\` (keep `settings.yaml`, `data\`, `strategies\`, `taifex_closures.yaml`), relaunch and check the title shows v2.19.12. No settings change required.

## 回退 Rollback

- EXE：停止 bot，換回 v2.19.11 release zip（sha256 `555507588b57ee2f1a650afadf443ccd67004a596cbd991c0d944dbde6c09235`）後重新啟動。 / EXE: stop the bot, swap back to the v2.19.11 release zip (sha256 `555507588b57ee2f1a650afadf443ccd67004a596cbd991c0d944dbde6c09235`) and relaunch.
- 監控與儀表板：將程式碼重設到 tag `v2.19.11` 並重新啟動（若只需保留 #229，可停在 `fc1943b6`）。 / Monitor and dashboard: reset the checkouts to tag `v2.19.11` and restart them (or `fc1943b6` to keep #229 only).

## 已知問題 Known issues

- **Windows pytest 已知失敗（同 v2.19.11）**：`tests/test_backfill_bars.py::test_refuses_target_outside_backfill`（WinError 1314）與 `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress`（WinError 5）。 / **Known Windows pytest failures (same as v2.19.11)**: `tests/test_backfill_bars.py::test_refuses_target_outside_backfill` (WinError 1314) and `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress` (WinError 5).
- **儀表板停止僅限 headless**：GUI bot 仍不讀 `STOP`（同 v2.19.11）。 / **Dashboard stop is headless-only**: GUI bots still do not poll `STOP` (unchanged from v2.19.11).
- **#219 根因**：回報事件分派根因仍待查。 / **#219 root cause**: reply event dispatch is still under investigation.
- 假日缺漏 [#209](https://github.com/ericwu13/tai-robot/issues/209) 同 v2.19.11。 / Holiday gap [#209](https://github.com/ericwu13/tai-robot/issues/209) unchanged from v2.19.11.
