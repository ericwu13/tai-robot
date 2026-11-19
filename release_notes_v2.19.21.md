# v2.19.21

## EXE 變更 EXE changes

- **#267**（squash `17536af2`）當 Capital 報價回呼 3002（Quote，中間狀態）出現後 15 秒內沒有 3003（Ready）時，改走既有 #178 報價未就緒路徑，不再讓 bot 默默維持 READY（修 2026-11-12 約 07:30 TPE 事故；Eric 2026-11-13 決定）：
  - 3002 表示報價通道起來，3003 才是報價就緒、可訂閱 Tick。正常連線是 3002 接著 3003。v2.19.20 及更早版本：若先前已 Ready，逾時可能不記 log、heartbeat 仍顯示 ready，也不會啟動修復。
  - **報價未就緒（quote-down）不平倉、不停止 bot、不重新登入、不呼叫 LeaveMonitor、也不暫停出場單**；只阻擋新進場，並在 5 秒後僅當 `IsConnected()==0` 時呼叫 `EnterMonitorLONG`。狀態列為「報價未就緒 Quote not ready — waiting for ticks」。之後若有新 Tick，可在約 300 秒卡住上限前恢復「已連線 Connected」。
  - 開盤中、已訂閱且最近 30 秒內有新 Tick：維持 READY，並記錄「報價仍跳動 … ticks are flowing」。開盤存活訊號只看 Tick，不看 `IsConnected()`。
  - 開盤中尚未訂閱、無法產生 Tick：不標記未就緒，狀態為「Quote unverified — waiting for ticks」，等到 Ready、斷線、手動重連、新 Tick，或下次訂閱的 15 秒視窗再判斷。
  - 部署／暖機進行中：最多從該次 3002 起延後 60 秒再判斷，避免清除連線旗標而中止部署。
  - 休市時段且 `IsConnected()==0`：在時段內走 #178（約 5 秒後 EnterMonitorLONG），但暫停卡住計時，不會在隔夜變成 DEGRADED。`IsConnected()` 不是 0 且未訂閱時先掛起、不標記未就緒。開盤前一分鐘仍不呼叫 EnterMonitor（#157）。
  - 若 `IsConnected()` 一直為 1 且沒有新 Tick：不會呼叫 EnterMonitorLONG，約 5 分鐘後進入 **DEGRADED**（進場持續封鎖、出場仍允許）。這是本版接受的終態；headless 自動重啟是後續 [#279](https://github.com/ericwu13/tai-robot/issues/279)，不在本版。

  **#267** (squash `17536af2`) when a Capital quote callback 3002 (Quote, intermediate) is not followed by 3003 (Ready) within 15 s, take the existing #178 quote-not-ready path instead of leaving the bot silently READY (fixes the 2026-11-12 ~07:30 TPE incident; Eric's 2026-11-13 decisions):
  - 3002 means the quote channel came up; 3003 means the quote is ready and ticks can be subscribed. A normal connect is 3002 then 3003. On v2.19.20 and earlier, after a previous Ready the timeout could return with no log, the heartbeat stayed ready, and recovery never started.
  - **Quote-down does not flatten, stop the bot, re-login, call LeaveMonitor, or pause exit orders.** It only blocks new entries, then after 5 s calls `EnterMonitorLONG` only if `IsConnected()==0`. Status becomes "報價未就緒 Quote not ready — waiting for ticks". A later fresh tick can restore "已連線 Connected" before the ~300 s stuck limit.
  - In an open session with a subscription and a fresh tick in the last 30 s, the quote stays READY and the log says ticks are flowing. Open-session liveness is tick-only, not `IsConnected()`.
  - In an open session with nothing subscribed (no ticks possible), the quote is not marked down; status is "Quote unverified — waiting for ticks" until Ready, a disconnect, manual Reconnect, a fresh tick, or a 15 s window at the next subscription.
  - During deploy / warmup the timeout is deferred for up to 60 s from the 3002 so a stray event does not clear the connected flag and abort the deploy.
  - In a closed gap with `IsConnected()==0`, #178 runs during the gap (EnterMonitorLONG after about 5 s) but the stuck clock is paused so the bot cannot go DEGRADED overnight. If `IsConnected()` is not 0 and nothing is subscribed, the 3002 stays pending with no mark-down. EnterMonitor is still not called in the minute before the open (#157).
  - If `IsConnected()` stays 1 and no fresh tick arrives, `EnterMonitorLONG` never runs and the link reaches **DEGRADED** after about 5 min (entries stay blocked, exits stay allowed). That is the accepted end state for this release; automatic headless restart is follow-up [#279](https://github.com/ericwu13/tai-robot/issues/279) and is not included.

## 操作注意 Operator caveats

- **報價未就緒時持倉仍在、出場仍允許，但若 Tick 真的停了，策略可能看不到價格、出場單可能不會觸發**，直到 Tick 恢復或操作員處理。bot 不會被停止，也不會自動平倉。 / **During quote-down the position stays open and exits stay allowed, but if ticks have actually stopped the strategy may not see price and managed exits may not fire** until ticks resume or an operator acts. The bot is not stopped and the position is not flattened.
- **DEGRADED 不會自動恢復（尤其 headless）**：約 5 分鐘後若仍無 Tick，會發優先 Discord「⚠️ DEGRADED…」，GUI 狀態為「降級 DEGRADED — 手動重連」並啟用重連按鈕。headless 沒有該按鈕，須操作員介入或之後的 #279。同一交易時段第二次類似事件，若一次性重訂閱已用完，可能維持 DEGRADED 直到手動重連。 / **DEGRADED does not recover by itself (especially headless):** after about 5 min with no ticks, a priority Discord "⚠️ DEGRADED…" is sent (when Discord is enabled); the GUI shows "降級 DEGRADED — 手動重連" and enables Reconnect. A headless bot has no such button and needs an operator, or later #279. A second episode in the same session can stay DEGRADED until manual Reconnect if the one-shot resubscribe is already spent.
- **操作員可看**：log「報價未就緒 Quote (3002) seen but Ready (3003) did not arrive…」、或「報價仍跳動 … ticks are flowing」、或「報價待確認 … leaving pending」；狀態列與連線圓點；heartbeat 不再在報價已死時維持 ready。 / **What to look for:** the log lines above, the status bar and connection dot, and a heartbeat that no longer stays ready when the quote is dead.

## 升級注意 Upgrade / redeploy notes

- **換 EXE**：正常停止 bot，保留 v2.19.20 資料夾或 zip，解壓 v2.19.21 取代 `tai_backtest.exe` 與 `_internal\`（保留 `settings.yaml`、`data\`、`strategies\`、`taifex_closures.yaml`；不要用 zip 內的範本 `settings.yaml` 覆蓋），重新啟動並確認標題為 v2.19.21。持倉中的 bot 請待平倉後再換版。若尚未換上 v2.19.20，可一次換到 v2.19.21（含 v2.19.20 的 #282／#292）。換版時請逐一確認重啟的 bot 確實在執行（無 exit 4／5、無 `[DEPLOY ABORT]`、無 🚨）。 / **EXE swap**: stop cleanly, keep the v2.19.20 folder or zip, unzip v2.19.21 and replace `tai_backtest.exe` and `_internal\` (keep `settings.yaml`, `data\`, `strategies\`, `taifex_closures.yaml`; do not copy the zip's template `settings.yaml` over yours), relaunch and confirm the title shows v2.19.21. Swap a bot holding a position only after it is flat. If v2.19.20 has not been swapped on yet, a single swap to v2.19.21 also includes #282 / #292 from v2.19.20. During a swap, confirm each relaunched bot is actually running (no exit 4 or 5, no `[DEPLOY ABORT]`, no 🚨).
- **#267 需換 EXE** 才生效。 / **#267 needs an EXE swap** to take effect.

## 回退 Rollback

- EXE：停止 bot，換回 v2.19.20 release zip（sha256 `b88c2f9ebabaa3df224781f64e5973e852ea12e9ebad66b482a4ac3b2e28fd4a`）後重新啟動。無新設定鍵、無磁碟格式變更。v2.19.20 不會在 3002 之後沒有 3003 時標記報價未就緒，原本「默默維持 READY」的缺口會回來。 / EXE: stop the bot, swap back to the v2.19.20 release zip (sha256 `b88c2f9ebabaa3df224781f64e5973e852ea12e9ebad66b482a4ac3b2e28fd4a`), relaunch. No new settings key and no on-disk format change. v2.19.20 will not mark the quote down when 3002 is not followed by 3003, so the original silent-ready gap returns.

## 已知問題 Known issues

- [#279](https://github.com/ericwu13/tai-robot/issues/279) headless bot 在開盤中持續 DEGRADED 時的自動重啟不在本版（#267 已知限制）。 / [#279](https://github.com/ericwu13/tai-robot/issues/279) auto-restart of a headless bot that stays DEGRADED during an open session is not in this release (known limitation of #267).
- [#306](https://github.com/ericwu13/tai-robot/issues/306) 05:00 暫停延續（同 v2.19.20）。 / [#306](https://github.com/ericwu13/tai-robot/issues/306) 05:00 pause carry (same as v2.19.20).
- [#285](https://github.com/ericwu13/tai-robot/issues/285) headless 卡住尚未宣稱解決。 / [#285](https://github.com/ericwu13/tai-robot/issues/285) headless hang not claimed solved.
- 假日缺漏 [#209](https://github.com/ericwu13/tai-robot/issues/209) 不變。 / Holiday gap [#209](https://github.com/ericwu13/tai-robot/issues/209) unchanged.
