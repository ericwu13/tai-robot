# v2.17.3

## 修正 Fixes

### Discord 每日報告累計統計錯誤
- **累計數據來源錯誤**：累計統計（交易次數、損益、勝率）原本從 broker 記憶體中的交易清單計算，該清單在重新部署時會重置，且可能包含繼承自舊 session 的錯誤數據。現改為從該 bot 自己的 `data/daily-reports/*.json` 檔案計算，按 `session.bot_name` 過濾，跨部署皆可靠。
- **損益金額 10 倍誤差**：broker 已將 `point_value` 乘入 `Trade.pnl`（即 pnl 已是台幣），但報告產生器又乘了一次。修正 `_trade_to_dict`、`generate_session_report` 中的重複乘算。

### Discord daily report cumulative stats incorrect
- **Wrong data source for cumulative**: cumulative stats (trade count, P&L, win rate) were computed from the broker's in-memory trade list, which resets on fresh deploys and can carry stale session data. Now computed from the bot's own `data/daily-reports/*.json` files, filtered by `session.bot_name` — reliable across deploys.
- **P&L 10x inflated**: the broker already multiplies `point_value` into `Trade.pnl` (i.e., pnl is already in TWD), but the report generator multiplied again. Fixed double-multiplication in `_trade_to_dict` and `generate_session_report`.
