# v2.19.7

## 修復 Fixes

### 更新包：交換前驗證 SHA-256 與 zip CRC（#148，#146）

**變更**：下載組裝後、寫入 swap／退出前，抓取發布的 `.sha256` 並用 `hashlib.sha256` 比對，再跑 `zipfile.testzip`。不符則刪除 zip 並拋 `ChecksumError`，GUI 可應用內重試。下單／交易路徑不變。

### 手動平倉：只對本商品期貨下單（#149，#145）

**變更**：`_manual_close` 改以 `close_order_side` 決定方向——僅本 bot 下單代號前綴 + 期貨市場 `TF` 的非零 OI 列。外國商品／選擇權（含同 TX 前綴的 TO）忽略；OI 快照顯示本商品空手則拒絕（不再回退到上一筆實單／sim）。確認對話明確傳 `new_close=1`。僅 GUI 手動平倉路徑；自動下單／無頭路徑不變。

### 狀態列命中區與聊天輸入排版（#150，#139 items 3–4）

**變更**：狀態列命中目標抬離縮放帶；聊天提示對齊 caret。修正 SM_93 內邊距膨脹。下單／交易路徑不變。

---

## Fixes (English)

### Updater: SHA-256 + zip CRC before swap (#148, #146)

**What changed**: After the zip is assembled and before swap / exit, fetch the published `.sha256`, compare with `hashlib.sha256`, then `zipfile.testzip`. On mismatch delete the zip and raise `ChecksumError` so the GUI can retry in-app. Order/trading path unchanged.

### Manual flatten: this product's futures only (#149, #145)

**What changed**: `_manual_close` uses `close_order_side` — first non-zero OI row for this bot's order-symbol prefix and futures market `TF`. Foreign products / options (including TO sharing a TX prefix) ignored; refuse when the OI snapshot says this product is flat (no last-order / sim fallback). Confirm dialog passes `new_close=1`. GUI manual-close path only; auto / headless unchanged.

### Status-strip hit targets + chat composer (#150, #139 items 3–4)

**What changed**: Lift status-strip hits off the resize band; seat the chat hint on the caret. Fixes SM_93 pad blow-up. Order/trading path unchanged.

---

## 升級注意 Upgrade notes

- GUI 需換包才能拿到 checksum 驗證、手動平倉修正與狀態列／聊天排版。Restart/resync GUI for the new EXE.
- 從 ≤v2.19.6 拉本版時，舊 updater 仍不驗證內容：落地 2.19.7 後後續更新才有 checksum。Chicken-egg: first hop to 2.19.7 still uses pre-verify updater.
- Paper／live 若走 GUI 手動平倉：需 pull／重啟以套用 #149。Paper/live using GUI manual close: pull/restart for #149.
- 無頭持續交易路徑不變（#148/#150 為 frozen GUI；#149 僅 GUI 手動平倉）。Headless continuous trading unchanged.
