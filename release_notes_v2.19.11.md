# v2.19.11

> 草稿 / DRAFT — 尚未打 tag 或發布 / not tagged or published. Base: master `6f69ff15` (v2.19.10 `115a0447` + #201 #216 #224 #162 #221 #222 #207). #208 尚未合併，不包含在內 / #208 is still open and not included.

## EXE 變更 EXE changes

- **#207**（squash `6f69ff15`，實單安全防護 real-money safety guard）：headless bot 的 `mode_override.json` 只接受 `"paper"`。要求 `auto`、`real`、`semi_auto`、其他字串或非字串值（以及無效 JSON）一律拒絕，即使 `allow_live_override` 為 true；目前模式不變，檔案改名為 `mode_override.json.refused`（舊的 refused 移為 `.refused.N`；改名失敗則刪除，再失敗則以指紋忽略該檔直到其變更）。操作者看到的是 bot 日誌與狀態列的警告 `[MODE] headless refused mode_override.json trading_mode=... staying on '<mode>' (file consumed)`；不發 Discord／警報、bot 不退出、無退出碼。`deploy --mode auto` 不受影響。GUI 部署（origin 為 gui 且非 headless）的熱切換不變；缺少 origin 時預設啟用防護（fail closed）。headless 驅動在 `_deploy` 的 try 與 finally 都會啟用防護。
  **#207** (squash `6f69ff15`, real-money safety guard): a headless bot's `mode_override.json` accepts only `"paper"`. `auto`, `real`, `semi_auto`, any other string, any non-string value, and invalid JSON are refused, even with `allow_live_override: true`. The mode stays unchanged and the file is renamed to `mode_override.json.refused` (an older one moves to `.refused.N`; if the rename fails it is deleted; if that fails the exact file is fingerprinted and ignored until it changes). The operator sees a warning in the bot log and status line: `[MODE] headless refused mode_override.json trading_mode=... staying on '<mode>' (file consumed)`. There is no Discord alert, no exit, and no exit code. `deploy --mode auto` is unchanged. GUI deploys (origin gui and not headless) keep file hot-swap as before; a missing origin arms the guard (fail closed). The headless driver arms it in both the try and the finally of `_deploy`.
- **#222**（squash `d77d8aef`，緩解 #219）：回報（reply）連線靜默時的輪詢與新進場暫停。v2.19.9 起回報事件可能未送達 Python，連線狀態機看不到回報斷線、也不會送 `ConnectByID`。現每 60 秒及每次 3003 輪詢 `IsConnectedByID`：回傳 `0` 時暫停新的實單進場（沿用 `orders_paused`／`link_entries_blocked`），並依 5、10、30、60 秒退避送 `ConnectByID`，10 次未恢復即再發警告並停止重試；回傳 `1` 時標記回報 READY 並解除該暫停。登入、偵測到斷線或送出實單後 60 秒內沒有任何 OnConnect／OnComplete／OnNewData，會發 `REPLY CHANNEL SILENT` 警告（不暫停）。出場與平倉一律允許。不呼叫登出。GUI 與 headless 共用。另增加回報事件診斷日誌（不含帳號資料）。根因（事件分派）仍待查。
  **#222** (squash `d77d8aef`, mitigates #219): poll a silent reply link and pause new entries. Since v2.19.9 reply events may not reach Python, so the link machine never saw a reply drop or sent `ConnectByID`. `IsConnectedByID` is now polled every 60 s and on each 3003. `0` pauses new real entries (the existing `orders_paused` / `link_entries_blocked` path) and sends `ConnectByID` with 5/10/30/60 s backoff; after 10 attempts without recovery it alerts again and stops retrying. `1` marks the reply READY and lifts that pause. A `REPLY CHANNEL SILENT` alert fires if no OnConnect/OnComplete/OnNewData arrives within 60 s of login, a detected drop, or a real order (alert only, no pause). Exits and closes are always allowed. Logout is never called. GUI and headless share it. Adds reply-sink diagnostics to the bot log (no account data). The event-dispatch root cause is still open.
- **#221**（squash `24e562fa`）：headless `deploy` 在取得鎖後寫入 `headless.json`（pid 與程序建立時間），結束時移除；GUI 部署會刪除殘留的 `headless.json`。儀表板只有在此標記有效（pid 與鎖相符、程序確實存活、建立時間相符）時才啟用「停止」與「改為模擬」，否則按鈕停用並顯示「GUI 機器人，請在程式中操作」，且不寫入 `STOP`／`CONTROL`／`mode_override.json`。控制日誌預設改為安裝目錄下的 `dashboard_control_log.csv`。GUI 即時迴圈仍不讀 `STOP`。
  **#221** (squash `24e562fa`): a headless `deploy` writes `headless.json` (pid and process start time) after acquiring the lock and removes it on exit; a GUI deploy deletes a stale `headless.json`. The dashboard enables 停止 (stop) and 改為模擬 (switch to paper) only when that marker is valid (pid matches the lock, the process is strictly alive, start time matches); otherwise the buttons are disabled with "GUI 機器人，請在程式中操作" and nothing is written (`STOP`, `CONTROL`, `mode_override.json`). The control log now defaults to `dashboard_control_log.csv` in the install directory. The GUI live loop still does not poll `STOP`.


## 開發／維運 Developer / ops

- **監控 Monitor — #201 `0bf9d180`、#162 `19cfeaba`**：#201 監控腳本改用 FILE_SHARE_DELETE 讀取 bot 檔案（新增 `src/utils/fileio.py`，僅供監控使用），不會再阻擋 bot 存檔或刪除；#162 `check_evolution` 能辨識週六 knob-mode 候選與 pool PASS（修正 #161）。 / #201: monitor scripts read live bot files with FILE_SHARE_DELETE (new `src/utils/fileio.py`, used only by the monitor), so they cannot block a bot's save or delete. #162: `check_evolution` recognises Saturday knob-mode candidates and pool PASSes (fixes #161).
- **儀表板 Dashboard — #216 `a9f71c36`、#221**：#216 持有部位的 bot 在其健康分級中置頂，並顯示持倉標籤；#221 的儀表板端見上。 / #216: bots holding a position float to the top of their health tier with a holding pill; #221's dashboard side is described above.
- **測試／CI Tests — #224 `d58e4e63`**（僅測試 test-only）：regime 演化測試不再固定日期。 / regime evolution fixture no longer pinned to a fixed date.

## 操作影響 Operator impact

- headless bot 不再接受 `mode_override.json` 切換為 auto／real，只接受 `paper`；被拒的檔案會留下 `.refused` 副本。 / Headless bots no longer accept `mode_override.json` switches to auto/real, only `paper`; refused files leave a `.refused` copy.
- 回報連線斷線時，新實單進場會暫停，直到 `IsConnectedByID` 回到 1；可能看到 `REPLY CHANNEL SILENT` 或「10 次 ConnectByID 仍未恢復」警告。 / When the reply link is down, new real entries pause until `IsConnectedByID` returns 1; expect `REPLY CHANNEL SILENT` or "still down after 10 ConnectByID attempts" alerts.
- 儀表板「停止／改為模擬」只對以 v2.19.11 啟動的 headless bot 有效；GUI bot 顯示停用。 / Dashboard stop/paper work only for headless bots started on v2.19.11; GUI bots show them disabled.

## 風險 Risks

- **#207**：之前以 `mode_override.json` 將 headless bot 升為 auto 的流程不再有效（只能以 `deploy --mode auto` 重新部署）；拒絕只寫日誌、不發警報，需看日誌或 `.refused` 檔確認。 / **#207**: any workflow that promoted a headless bot to auto by dropping `mode_override.json` stops working (redeploy with `deploy --mode auto` instead). A refusal only logs, with no alert, so check the bot log or the `.refused` file.
- **#222 暫停進場**：若 `IsConnectedByID` 誤報 0，或 10 次重試後仍未恢復，新進場會持續暫停（出場仍允許），直到回報恢復或手動處理；此為緩解，根因未解。 / **#222 entry pause**: a false `0`, or a link that stays down after 10 attempts, keeps new entries paused (exits still allowed) until the reply recovers or someone intervenes. It is a mitigation; the root cause is open.
- **#222 警告量**：靜默監測可能在回報正常但事件未送達時發出警告。 / **#222 alert noise**: the silent-sink watchdog can alert when the link is up but events are not delivered.
- **#221 控制停用**：仍在舊版、或升級後尚未重啟的 headless bot 沒有 `headless.json`，儀表板停止／改為模擬會維持停用，直到以新 EXE 重啟。Windows 無法讀取建立時間時略過時間比對。 / **#221 controls disabled**: headless bots on older builds, or not restarted after the swap, have no `headless.json`, so dashboard stop/paper stay disabled until they restart on the new EXE. If the process start time cannot be read, the time check is skipped.

## 升級注意 Upgrade / redeploy notes

- 將監控與儀表板的程式碼更新到 master 並重新啟動。 / Update the monitor and dashboard checkouts to master and restart them.
- **換 EXE**（#207、#221、#222 屬 EXE 變更）：正常停止 bot，保留 v2.19.10 資料夾或 zip，解壓 v2.19.11 取代 `tai_backtest.exe` 與 `_internal\`（保留 `settings.yaml`、`data\`、`strategies\`、`taifex_closures.yaml`），重新啟動並確認標題為 v2.19.11。設定檔格式不變。 / **EXE swap** (#207, #221 and #222 change the EXE): stop the bot cleanly, keep the v2.19.10 folder or zip, unzip v2.19.11 and replace `tai_backtest.exe` and `_internal\` (keep `settings.yaml`, `data\`, `strategies\`, `taifex_closures.yaml`), relaunch and check the title shows v2.19.11. No settings change.

## 回退 Rollback

- EXE：停止 bot，換回 v2.19.10 release zip（sha256 `8364b5a683c5fc5973d647f869187dc07c98aa217660122ccdaf851722d28d56`）後重新啟動。 / EXE: stop the bot, swap back to the v2.19.10 release zip (sha256 `8364b5a683c5fc5973d647f869187dc07c98aa217660122ccdaf851722d28d56`) and relaunch.
- 監控與儀表板：將程式碼重設到 tag `v2.19.10` 並重新啟動。 / Monitor and dashboard: reset the checkouts to tag `v2.19.10` and restart them.

## 已知問題 Known issues

- **儀表板停止僅限 headless（已由 #221 收緊）**：GUI bot 仍不讀 `STOP`；#221 現會在 GUI bot 停用按鈕且不寫入檔案，取代 v2.19.10 的「僅 headless 有效」注意事項。 / **Dashboard stop is headless-only (now enforced by #221)**: GUI bots still do not poll `STOP`; #221 now disables the buttons for GUI bots and writes nothing, replacing v2.19.10's "headless-only until #221" caveat.
- **#219 根因**：回報事件分派根因仍待查。 / **#219 root cause**: reply event dispatch is still under investigation.
- **#208 未包含**：尚未合併。 / **#208 not included**: not merged yet.
- 假日缺漏 [#209](https://github.com/ericwu13/tai-robot/issues/209) 同 v2.19.10。 / Holiday gap [#209](https://github.com/ericwu13/tai-robot/issues/209) unchanged from v2.19.10.
