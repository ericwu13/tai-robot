# v2.19.6

## 修復 Fixes

### 更新下載：慢 CDN 邊緣中止並重試（#140，#139 item 2）

**變更**：`download_release` 對持續低於 128 KB/s（8 秒滑動窗）的連線中止並換新 TCP／邊緣重試（最多 4 次）；最後一次不監控速率（均勻慢線仍可完成）。支援 HTTP Range 續傳；伺服器宣告 Accept-Ranges 且檔案 ≥1 MiB 時可 4 路並行。超過宣告 Content-Length 拒絕並刪除。下單／交易路徑不變。

### 持倉確認：忽略 qty=0／## 結束列（#142，#139）

**變更**：Capital 在「查無資料」後可能送 `##` 結束列；先前誤解析為「空 SHORT x0」並觸發 Existing Position。改以 `has_open_positions()`／`first_open_position()` 閘控部署確認與手動平倉。標題字串不變。下單語意不變（僅避免假持倉閘／錯誤平倉方向）。

### W2 新鮮度：拒絕交易時段外／tape 規則外的 Yahoo 戳記（#141）

**變更**：W2 寫入／分類拒絕落在交易時段外或不符合 tape 規則的 Yahoo 時間戳；`^KS11` 仍留在 quorum（tape 過濾過期收盤）。下單路徑不變。n8n 路徑：main tree `git pull` 即可，無需重啟 bot。

---

## Fixes (English)

### Updater: abort slow CDN edges and retry (#140, #139 item 2)

**What changed**: `download_release` aborts a connection that stays under 128 KB/s for 8s and retries on a new TCP/edge (up to 4 attempts); last attempt is unwatched so a uniformly slow link still completes. HTTP Range resume; optional 4-way parallel when Accept-Ranges and size ≥1 MiB. Oversized downloads rejected/deleted. Order/trading path unchanged.

### Existing Position: ignore qty=0 / ## terminator (#142, #139)

**What changed**: Capital can follow empty-OI with a `##` end-of-data row; pre-fix parsed as 「空 SHORT x0」 and tripped Existing Position. Gate deploy confirm and manual close on non-zero qty (`has_open_positions` / `first_open_position`). Title seam unchanged. Order semantics unchanged (avoids false gate / wrong close side only).

### W2 freshness: reject Yahoo stamps outside trading period / tape rule (#141)

**What changed**: W2 write/classify rejects Yahoo timestamps outside the trading period or tape rule; `^KS11` stays in quorum (tape filters stale closes). Order path unchanged. n8n path: `git pull` on the main tree is enough — no bot restart required for that path.

---

## 亦含 Also in this tag

- #143 `settings.example.yaml` `ai.max_tokens` 4096→16384 (template only; live `settings.yaml` untouched).

---

## 升級注意 Upgrade notes

- GUI 需換包才能拿到新 updater、持倉閘與 W2 新鮮度。Restart/resync GUI for the new EXE.
- 從 ≤v2.19.5 拉本版時，舊 updater 仍可能撞到慢邊緣：再按一次「檢查更新」，或從 GitHub Releases 手動下載 zip。Chicken-egg: first hop may still need retry or manual zip.
- n8n／W2 寫入：main tree `git pull` 即可套用 #141；無頭持續交易路徑不變。n8n/W2: pull main tree for #141; headless continuous trading unchanged.
- 部署確認／手動平倉／檢查更新才受 #140/#142 影響。Deploy-confirm / manual-close / Check-for-Updates affected by #140/#142.
