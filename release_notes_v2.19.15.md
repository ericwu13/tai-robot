# v2.19.15

## EXE 變更 EXE changes

- **#239**（squash `121e0bef`，修正 #234）：
  - 心跳新增頂層欄位 `loss_paused` / `loss_net` / `loss_limit`（`src/live/main_heartbeat.py`；由 `run_backtest.py` 的 `_publish_loss_heartbeat` 寫入）。不改動 `orders_paused`、`entries_allowed` 或連線快照語意。
  - 儀表板機隊列以心跳顯示紫色 **PAUSED** 徽章與 `net … / limit …`（`scripts/dashboard/snapshot.py` 的 `loss_status`；`scripts/dashboard/static/index.html`）。較舊心跳缺欄位時不顯示、不崩潰。limit 為 0 時顯示 `limit off`。
  - **實單／自動進場啟動閘門**：`auto` 與 `semi_auto` 在部署（`_deploy_live_from`）以及從非實單模式切到實單時，會掛上既有的 `_equity_refresh_owed` 暫停；新進場在第一次有效的 GetFutureRights 回覆實際跑過 `TradingGuard.update_pnl` 之前一律擋下。出場、平倉與成交輪詢不受影響。
  - 暫停逾時 90 秒（`EQUITY_ENTRY_HOLD_TIMEOUT_S`）與 GetFutureRights 失敗共用一個警示鎖存，每次暫停只警示一次；臂上與每次釋放都清除鎖存與重試時鐘。被拒的 GetFutureRights 至少間隔 5 秒（`EQUITY_REFRESH_RETRY_S`）才再送。連線中斷期間不發 90 秒警示；連線恢復時重啟暫停計時。
  - 日損限額為 0 時仍會跑 `update_pnl`（只記淨值、不暫停），有效回覆仍會解除啟動閘門。**本版不變更預設日損限額數值**（`TradingGuard` 預設仍為 10,000 NTD）；也不做交易日邊界重置（見未合併的 #242）。

  **#239** (squash `121e0bef`, fixes #234):
  - Heartbeat gains top-level `loss_paused` / `loss_net` / `loss_limit` (`src/live/main_heartbeat.py`; written by `_publish_loss_heartbeat` in `run_backtest.py`). `orders_paused`, `entries_allowed`, and the link snapshot are unchanged.
  - The dashboard fleet row shows a purple **PAUSED** badge and `net … / limit …` from those fields (`loss_status` in `scripts/dashboard/snapshot.py`; `scripts/dashboard/static/index.html`). Older heartbeats with none of the three keys show nothing and do not crash. A limit of 0 renders as `limit off`.
  - **Real/auto startup entry gate**: `auto` and `semi_auto` arm the existing `_equity_refresh_owed` hold on deploy (`_deploy_live_from`) and when switching from a non-real mode onto real orders. New entries stay shut until the first valid GetFutureRights reply has actually run `TradingGuard.update_pnl`. Exits, flattens and fill polls are never held.
  - The 90 s hold timeout (`EQUITY_ENTRY_HOLD_TIMEOUT_S`) and a failed GetFutureRights share one alert latch — one alert per hold. Arm and every release clear the latch and the retry clock. Rejected GetFutureRights waits at least 5 s (`EQUITY_REFRESH_RETRY_S`) before another send. The 90 s note is quiet while the reply link is down; the hold clock restarts when the link returns.
  - A loss limit of 0 still runs `update_pnl` (records the net, never pauses), so a valid reply still releases the startup hold. **This release does not change the default daily-loss-limit value** (`TradingGuard` default remains 10,000 NTD), and it does not add a trading-day boundary reset (see unmerged #242).

## 操作影響 Operator impact

- 部署或切到 `auto` / `semi_auto` 後，新進場通常多等約一次權益數輪詢；無需改設定檔。既有 bot 的日損限額數值不變。 / After deploy or a switch to `auto` / `semi_auto`, new entries typically wait about one rights poll. No settings change required. Existing bots keep their deployed loss-limit values.
- 儀表板可直接看到日損暫停狀態，不再誤把連線旗標當成日損暫停。 / The dashboard shows the daily-loss pause directly; link flags are no longer mistaken for it.

## 風險 Risks

- 啟動閘門 fail-closed：若權益數回覆一直不到，新進場會一直暫停（出場不受影響）。 / The startup gate fails closed: if no rights reply arrives, new entries stay held (exits unaffected).
- 日損觸發條件仍為既有的嚴格小於：`net < -limit`（`trading_guard.py` 的 `update_pnl`）。暫停後需重新部署（或之後合併的日界重置）才會解除。 / The trip condition is still the existing strict less-than: `net < -limit` (`TradingGuard.update_pnl`). Once paused, only a redeploy (or a future day-boundary reset) clears it.
- semi_auto 在閘門尚未刷新時仍可能跳出確認對話框，但確認後不會送單（已知外觀問題）。 / In semi_auto a confirm dialog can still open before the gate refreshes; confirming it does not send (known cosmetic).

## 升級注意 Upgrade / redeploy notes

- **換 EXE**：正常停止 bot，保留 v2.19.14 資料夾或 zip，解壓 v2.19.15 取代 `tai_backtest.exe` 與 `_internal\`（保留 `settings.yaml`、`data\`、`strategies\`、`taifex_closures.yaml`），重新啟動並確認標題為 v2.19.15。 / **EXE swap**: stop the bot cleanly, keep the v2.19.14 folder or zip, unzip v2.19.15 and replace `tai_backtest.exe` and `_internal\` (keep `settings.yaml`, `data\`, `strategies\`, `taifex_closures.yaml`), relaunch and check the title shows v2.19.15.

## 回退 Rollback

- EXE：停止 bot，換回 v2.19.14 release zip（sha256 `0d411fa7a37dd71b4fb0ac8211b2d520d4efe82fb0e01e4c394e21fa42eb8961`）後重新啟動。 / EXE: stop the bot, swap back to the v2.19.14 release zip (sha256 `0d411fa7a37dd71b4fb0ac8211b2d520d4efe82fb0e01e4c394e21fa42eb8961`) and relaunch.

## 已知問題 Known issues

- **Windows pytest 已知失敗（同 v2.19.14）**：`tests/test_backfill_bars.py::test_refuses_target_outside_backfill`（WinError 1314）與 `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress`（WinError 5）。 / **Known Windows pytest failures (same as v2.19.14)**: `tests/test_backfill_bars.py::test_refuses_target_outside_backfill` (WinError 1314) and `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress` (WinError 5).
- **儀表板停止僅限 headless**（同 v2.19.14）。 / **Dashboard stop is headless-only** (unchanged).
- 假日缺漏 [#209](https://github.com/ericwu13/tai-robot/issues/209) 同 v2.19.14。 / Holiday gap [#209](https://github.com/ericwu13/tai-robot/issues/209) unchanged.
- 未合併：#242（預設日損 20,000 與交易日邊界重置）、#244。 / Not in this release: #242 (20,000 default and trading-day boundary reset), #244.