# v2.17.9

## 繁體中文

### 修正
- **發佈通知改從 GitHub Release 讀取**：Discord 發佈通知現在完全從 GitHub Release body 取得內容，不再查詢本機 `.md` 檔案，確保通知訊息與 Release 頁面一致
- **每日報告累計統計修正 (#95)**：累計統計改由 `broker.trades` 透過 `calculate_metrics()` 計算，取代先前不可靠的磁碟報表彙整方式；損益值現在顯示為整數（不再帶 `.0`），已刪除棄用的 `_cumulative_from_reports()`

---

## English

### Fixed
- **Release notification reads from GitHub Release**: Discord release notification now reads exclusively from the GitHub Release body instead of falling back to a local `.md` file, ensuring the notification always matches the published release page
- **Daily report cumulative stats (#95)**: cumulative stats now computed from `broker.trades` via `calculate_metrics()` instead of the unreliable on-disk report aggregation; P&L values display as integers (no `.0` suffix); removed deprecated `_cumulative_from_reports()`
