# v2.17.2

## 繁體中文

### 新功能
- **Discord 發佈公告（poster channel）**：`build_release.py` 打包完成後自動發送版本公告至指定的 Discord 公告頻道，包含截斷的版本說明與 GitHub Release 連結，並 tag 營運長身份組。頻道 ID 與身份組 ID 均從 `settings.yaml` 讀取（不寫死於程式碼中）。新增 `--notify-only` 旗標可單獨重發通知而不重新建置。

---

## English

### New
- **Discord release announcement (poster channel)**: after packaging, `build_release.py` automatically posts a release announcement to a dedicated Discord poster channel — includes truncated release notes, a GitHub Release link, and a role mention. Channel ID and role ID are read from `settings.yaml` (never hardcoded). A `--notify-only` flag allows re-sending the notification without rebuilding.
