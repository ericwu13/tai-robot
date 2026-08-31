# v2.17.18

## 修復 Bug Fixes

### 🔧 斷線重連永久放棄問題 (#102)
部署 Live Runner 期間，若斷線重連嘗試達 10 次上限，程式會永久放棄重連（因為盤中 `seconds_until_market_open()` 回傳 0，導致 `defer_to_market` 分支無法觸發）。此後 TickWatchdog 被關閉、重入鎖阻止新的重連循環，機器人在有持倉的情況下完全停止接收行情。

**修正**：新增 `rest_cycle` 動作 — 當重連次數用盡但仍有 Live Runner 部署時，重設計數器並排程 5 分鐘冷卻後重試，永不放棄。無 Live Runner 時（手動模式）仍維持原先的 `give_up` 行為。

### 🔧 Reconnection permanently gives up while live runner is deployed (#102)
When reconnect attempts hit the 10-attempt limit during open market hours, the bot permanently gave up because `seconds_until_market_open()` returns 0 while the market is open, so the `defer_to_market` branch never fired. After give-up, the TickWatchdog was disarmed and the re-entry guard blocked new cycles — the bot went dead with a possible open position.

**Fix**: Added a `rest_cycle` action — when max attempts are exhausted with a live runner deployed, reset the counter and schedule a 5-minute cool-down before retrying. The bot never gives up while a live runner is active. Manual mode (no live runner) retains the existing `give_up` behavior.
