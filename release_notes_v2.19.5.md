# v2.19.5

## 新功能 Features

### 工作台 UI：DPI 感知主題與版面重建（#138，含 #136）

**變更**：以執行期繪製的 `tairobot` ttk 主題取代 clam（圓角按鈕／欄位焦點環／底線分頁／膠囊捲軸；無 Pillow／點陣資產）。`enable_dpi_awareness()` 在 COM／`tk.Tk()` 前啟用；像素經 `S()` 縮放。頂欄／工具列／Live／Report／Regime 卡片化；部署對話框確認鈕改固定頁尾（邏輯與 10-tuple 回傳不變）。`TagTextLog` 修剪改 O(1)；except 區塊 lambda 閉包修正（py3.13）。無頭模式 `init_theme(minimal=True)` 不套主題、不閃窗。下單／COM／部署語意不變。

---

## Features (English)

### Workbench UI: DPI-aware theme + rebuilt layout (#138, includes #136)

**What changed**: Runtime-rasterized `tairobot` ttk theme replaces clam (rounded buttons / focus rings / underline tabs / pill scrollbars; no Pillow or bitmap assets). `enable_dpi_awareness()` before COM/`tk.Tk()`; metrics via `S()`. Top bar / toolbar / Live / Report / Regime card layout; deploy dialog confirms moved to a fixed footer (logic and 10-tuple return unchanged). `TagTextLog` trim is O(1); except-lambda closures fixed for py3.13. Headless stays unthemed and hidden (`init_theme(minimal=True)`). Order path / COM / deploy semantics unchanged.

---

## 亦含 Also in this tag

- #136 functional keep: un-blocked TV fetch, non-spinning deploy OI wait, log routing, status history, incremental trades, throttled report render (visual layer replaced by #138).

---

## 升級注意 Upgrade notes

- GUI 工作台需換包／重開才看到新主題。Restart/resync the GUI workbench to load the theme.
- 無頭／CLI 機器人不受主題影響；交易路徑不變。Headless/CLI bots unchanged; trading path unchanged.
