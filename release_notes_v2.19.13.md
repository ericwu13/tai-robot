# v2.19.13

Base: master `550c19c2` (v2.19.12 `8bfb2a6a` + #238).

## EXE 變更 EXE changes

- **#238**（squash `550c19c2`，修正 #235）：收盤強制平倉、停止機器人及新聞 `risk_off` 平倉時，交易改記在最後一根已處理的K棒（`max(_bar_index - 1, entry_bar_index)`），不再記到下一根。先前下一根K棒收完時會重複記錄一筆 `TRADE_CLOSE` 並再跑一次決策；若實單仍標記為已進場，可能送出第二筆平倉單。現在每筆平倉只記錄一次（依交易識別去重），損益從未重複計算。
  **#238** (squash `550c19c2`, fixes #235): session-end force-close, bot stop and news `risk_off` flatten now book the trade on the last processed bar (`max(_bar_index - 1, entry_bar_index)`) instead of the next one. Before, when the next bar finalized it logged a duplicate `TRADE_CLOSE` and re-ran the decision path, which could send a second real exit order while the real entry was still marked confirmed. Each close is now logged once (deduplicated by trade identity). P&L was never double-counted.

## 操作影響 Operator impact

- 強制平倉後不會再出現第二筆 `TRADE_CLOSE` 或第二筆實單平倉單。不需修改設定檔。 / After a force-close there is no second `TRADE_CLOSE` and no second real exit order. No settings change required.

## 風險 Risks

- 去重集合僅存在記憶體中；重啟後由修正後的平倉K棒索引避免重複。 / The dedup set is in memory only; after a restart the corrected exit bar index prevents the repeat.

## 升級注意 Upgrade / redeploy notes

- **換 EXE**：正常停止 bot，保留 v2.19.12 資料夾或 zip，解壓 v2.19.13 取代 `tai_backtest.exe` 與 `_internal\`（保留 `settings.yaml`、`data\`、`strategies\`、`taifex_closures.yaml`），重新啟動並確認標題為 v2.19.13。 / **EXE swap**: stop the bot cleanly, keep the v2.19.12 folder or zip, unzip v2.19.13 and replace `tai_backtest.exe` and `_internal\` (keep `settings.yaml`, `data\`, `strategies\`, `taifex_closures.yaml`), relaunch and check the title shows v2.19.13.

## 回退 Rollback

- EXE：停止 bot，換回 v2.19.12 release zip（sha256 `3ddb0ecea19badda7d45e5b8174482d5f7e2cf7b1c5852aeb2ebfeb508970770`）後重新啟動。 / EXE: stop the bot, swap back to the v2.19.12 release zip (sha256 `3ddb0ecea19badda7d45e5b8174482d5f7e2cf7b1c5852aeb2ebfeb508970770`) and relaunch.

## 已知問題 Known issues

- **Windows pytest 已知失敗（同 v2.19.12）**：`tests/test_backfill_bars.py::test_refuses_target_outside_backfill`（WinError 1314）與 `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress`（WinError 5）。 / **Known Windows pytest failures (same as v2.19.12)**: `tests/test_backfill_bars.py::test_refuses_target_outside_backfill` (WinError 1314) and `tests/test_hung_heartbeat.py::test_windows_reader_writer_stress` (WinError 5).
- **儀表板停止僅限 headless**（同 v2.19.12）。 / **Dashboard stop is headless-only** (unchanged).
- 假日缺漏 [#209](https://github.com/ericwu13/tai-robot/issues/209) 同 v2.19.12。 / Holiday gap [#209](https://github.com/ericwu13/tai-robot/issues/209) unchanged.
