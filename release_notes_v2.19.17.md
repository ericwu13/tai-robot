# v2.19.17

## EXE 變更 EXE changes

- **#242**（squash `6cdad0d2`）日損暫停在交易日邊界解除；預設日損上限 20,000 NTD：
  - 過去 `TradingGuard.paused` 一旦觸發就一直鎖住，只有重新部署才會清除；長時間執行的 bot 隔天仍會擋進場。現在 `update_pnl` 帶入交易日鍵值，鍵值變更時先解除暫停，再用同一筆帳戶淨損益重新檢查（`src/live/trading_guard.py`）。
  - 交易日鍵值以台北時間 15:00 開盤日期切日（非午夜），純日期運算，不使用假日曆。只在鍵值「變更」時解除，同一盤中不會被清除。
  - 預設實單日損上限 `DEFAULT_DAILY_LOSS_LIMIT_NTD = 20000`，供 TradingGuard、LiveRunner、GUI 部署預填與 CLI `--loss-limit` 使用（CLI 原為 1,000、GUI 原為 10,000）。新增設定鍵 `trading.daily_loss_limit_ntd`。與回測用的 `risk.max_daily_loss` 不同。

  **#242** (squash `6cdad0d2`) the daily loss pause releases at the trading-day boundary; default limit 20,000 NTD:
  - `TradingGuard.paused` used to latch until a fresh deploy, so a long-running bot kept refusing entries the next day. `update_pnl` now takes a trading-day key; on a key change it releases the pause and immediately rechecks the same account net (`src/live/trading_guard.py`).
  - The key is the opening date rolling at 15:00 Asia/Taipei (not midnight), pure date arithmetic with no holiday calendar. It releases only on a key change, never mid-session.
  - Default real-order daily loss limit `DEFAULT_DAILY_LOSS_LIMIT_NTD = 20000`, shared by TradingGuard, LiveRunner, the GUI deploy pre-fill and CLI `--loss-limit` (previously CLI 1,000 and GUI 10,000). New settings key `trading.daily_loss_limit_ntd`. Separate from the backtest-only `risk.max_daily_loss`.

- **#244**（squash `e533e0cf`）補齊其餘 20,000 預設與 heartbeat 顯示：
  - `DeployRequest.loss_limit` 預設由 `"1000"` 改為 20,000；部署欄位空白或非數字時改回落 20,000（`parse_deploy_loss_limit`）。明確輸入 0 仍代表停用；GUI 恢復部署保留已儲存的值（包括 0）。
  - 模式切換時的 `update_pnl` 也帶入當日交易日鍵值。
  - Headless heartbeat 的 `entries_blocked` 改為綜合狀態，並標示是哪個閘門擋住（`link`、`loss_limit`）。
  - 儀表板部署對話框提示改為「例如 20000」（儀表板頁面，非 EXE）。

  **#244** (squash `e533e0cf`) the remaining 20,000 defaults and heartbeat reporting:
  - `DeployRequest.loss_limit` default `"1000"` → 20,000; a blank or non-numeric deploy field falls back to 20,000 (`parse_deploy_loss_limit`). An explicit 0 still disables the limit; a GUI resume keeps the saved value, including 0.
  - The mode-switch `update_pnl` call also passes today's trading-day key.
  - The headless heartbeat's `entries_blocked` is now the composite and names the gate holding it (`link`, `loss_limit`).
  - Dashboard deploy dialog placeholder now reads 例如 20000 (dashboard page, not the EXE).

## 操作注意 Operator caveats

- **CLI `--resume` 未指定 `--loss-limit` 時一律使用 20,000**，不沿用該 bot 已儲存的上限（包括自訂值或 0）。要保留原上限，請明確指定 `--loss-limit`。 / **A CLI `--resume` without `--loss-limit` always uses 20,000**, regardless of the bot's saved limit (custom or 0). Pass `--loss-limit` explicitly to keep a prior value.
- **Q-L1**：日損判斷使用 Capital `GetFutureRights` 的帳戶累計值（已實現－手續費－稅＋浮動損益），不是個別 bot 的成交損益。 / **Q-L1**: the loss check uses the Capital `GetFutureRights` account running total (realized minus fees minus tax plus floating P&L), not per-bot fills.
- **新交易日第一次檢查若仍超限，會重新暫停並維持到下一個交易日邊界**；同日稍後帳戶數值回落也不會解除。 / **If the first check of a new trading day is still beyond the limit, the pause re-trips and holds until the next trading-day boundary**; a later recovery within that day does not release it.
- 仍虧損的帳戶在切日時可能再發一則日損 Discord 警示。週末／假日鍵值照日期推進。 / A still-losing account may post another daily-loss Discord alert at the boundary. Weekend/holiday keys advance by date.

## 升級注意 Upgrade / redeploy notes

- **換 EXE**：正常停止 bot，保留 v2.19.16 資料夾或 zip，解壓 v2.19.17 取代 `tai_backtest.exe` 與 `_internal\`（保留 `settings.yaml`、`data\`、`strategies\`、`taifex_closures.yaml`），重新啟動並確認標題為 v2.19.17，並核對恢復後的日損上限。 / **EXE swap**: stop the bot cleanly, keep the v2.19.16 folder or zip, unzip v2.19.17 and replace `tai_backtest.exe` and `_internal\` (keep `settings.yaml`, `data\`, `strategies\`, `taifex_closures.yaml`), relaunch, check the title shows v2.19.17, and verify the resumed daily loss limit.
- 持倉中的 bot 請待平倉後再換版。 / Swap a bot holding a position only after it is flat.

## 回退 Rollback

- EXE：停止 bot，換回 v2.19.16 release zip（sha256 `f0ae00bba1577abaab6a3b342cb1e82c09f956d28aaa5312b950f01505b15c1f`）後重新啟動，並明確核對日損上限；回退會恢復舊的跨日鎖定行為。 / EXE: stop the bot, swap back to the v2.19.16 release zip (sha256 `f0ae00bba1577abaab6a3b342cb1e82c09f956d28aaa5312b950f01505b15c1f`), relaunch and explicitly verify the loss limit; rollback restores the old cross-day latch.

## 已知問題 Known issues

- **Windows pytest 已知失敗（同 v2.19.16）**：`tests/test_backfill_bars.py::test_refuses_target_outside_backfill`（WinError 1314）與 `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress`（WinError 5）。 / **Known Windows pytest failures (same as v2.19.16)**: `tests/test_backfill_bars.py::test_refuses_target_outside_backfill` (WinError 1314) and `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress` (WinError 5).
- 假日缺漏 [#209](https://github.com/ericwu13/tai-robot/issues/209) 不變。 / Holiday gap [#209](https://github.com/ericwu13/tai-robot/issues/209) unchanged.
