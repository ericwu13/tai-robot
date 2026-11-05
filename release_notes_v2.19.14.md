# v2.19.14

## EXE 變更 EXE changes

- **#236**（squash `5c98a3a3`，#231 回報連線卡住的審查後續修正）：
  - 移除不會被觸發的 `REPLY_CONNECT_ATTEMPT_CAP` 次數上限；每次斷線仍只呼叫一次 ConnectByID，90 秒逾時後暫停新進場並警示一次，政策不變。
  - 權益數更新（GetFutureRights）失敗時清除已送出旗標，至少間隔 5 秒（`EQUITY_REFRESH_RETRY_S`）才重試，避免連續重送。
  - 「權益數未更新，暫停新進場」警示每次暫停只發一次；新暫停開始或暫停解除時才重置，其他畫面更新不會重置。
  - ConnectByID 回傳非零代碼時於呼叫當下記為錯誤（error）日誌；回傳 0 仍為一般資訊日誌。
  - 出場、平倉與成交不受暫停影響。

  **#236** (squash `5c98a3a3`, #231 reply-hang review follow-ups):
  - Removed the unreachable `REPLY_CONNECT_ATTEMPT_CAP`. One ConnectByID per drop, then the 90 s timeout that pauses entries and alerts once, is unchanged.
  - A failed equity refresh (GetFutureRights) clears the sent flag and waits at least 5 s (`EQUITY_REFRESH_RETRY_S`) before retrying, so it cannot storm.
  - The "entries held: equity never refreshed" alert fires once per hold. It resets when a new hold starts or the hold ends, not on unrelated display updates.
  - A non-zero ConnectByID return code is logged as an error at the call; a zero code stays on the info log.
  - Exits, flattens and fills are never held.

## 操作影響 Operator impact

- 回報連線中斷時日誌與警示較少重複；不需修改設定檔。 / Fewer repeated log lines and alerts while the reply link is down. No settings change required.

## 風險 Risks

- 權益數更新失敗後，新進場最多多等約 5 秒才重試。出場不受影響。 / After a failed equity refresh, entries may wait up to about 5 s longer for the retry. Exits are unaffected.

## 升級注意 Upgrade / redeploy notes

- **換 EXE**：正常停止 bot，保留 v2.19.13 資料夾或 zip，解壓 v2.19.14 取代 `tai_backtest.exe` 與 `_internal\`（保留 `settings.yaml`、`data\`、`strategies\`、`taifex_closures.yaml`），重新啟動並確認標題為 v2.19.14。 / **EXE swap**: stop the bot cleanly, keep the v2.19.13 folder or zip, unzip v2.19.14 and replace `tai_backtest.exe` and `_internal\` (keep `settings.yaml`, `data\`, `strategies\`, `taifex_closures.yaml`), relaunch and check the title shows v2.19.14.

## 回退 Rollback

- EXE：停止 bot，換回 v2.19.13 release zip（sha256 `74b6bad5e1abed356dac90c6c5c4c5d7a00401d664588462b66120275250512c`）後重新啟動。 / EXE: stop the bot, swap back to the v2.19.13 release zip (sha256 `74b6bad5e1abed356dac90c6c5c4c5d7a00401d664588462b66120275250512c`) and relaunch.

## 已知問題 Known issues

- **Windows pytest 已知失敗（同 v2.19.13）**：`tests/test_backfill_bars.py::test_refuses_target_outside_backfill`（WinError 1314）與 `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress`（WinError 5）。 / **Known Windows pytest failures (same as v2.19.13)**: `tests/test_backfill_bars.py::test_refuses_target_outside_backfill` (WinError 1314) and `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress` (WinError 5).
- **儀表板停止僅限 headless**（同 v2.19.13）。 / **Dashboard stop is headless-only** (unchanged).
- 假日缺漏 [#209](https://github.com/ericwu13/tai-robot/issues/209) 同 v2.19.13。 / Holiday gap [#209](https://github.com/ericwu13/tai-robot/issues/209) unchanged.
