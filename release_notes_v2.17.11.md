# v2.17.11

## 繁體中文

### 修正
- **K線圖在打包版無法顯示K棒 (#97)**：WebView2 執行環境更新（約 150.0.4078.83，2026-07-24）破壞了 `file://` 來源的 GPU 合成，導致打包 EXE 的K線圖只剩空白、畫不出K棒（開發模式因走 HTTP 一切正常）。本次移除凍結 EXE 專用的 `file://` INDEX 修補，讓所有版本一律透過 pywebview 內建 HTTP 伺服器載入圖表。已於實際打包 EXE 驗證K棒、布林通道與成交量正常繪製。

---

## English

### Fixed
- **K-chart candles not rendering in the packaged EXE (#97)**: A WebView2 runtime update (~150.0.4078.83, 2026-07-24) broke GPU compositing on `file://` origins, so the packaged EXE's k-chart showed a blank pane with no candles (dev mode, which serves over HTTP, was unaffected). This release removes the frozen-EXE `file://` INDEX patch so every build loads the chart through pywebview's built-in HTTP server instead. Verified in an actual packaged EXE that candles, Bollinger Bands, and volume all paint correctly.
