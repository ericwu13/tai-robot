# v2.17.6

## 繁體中文

### 修正
- **Discord 發布通知不再重複發送**：先前版本在建置時自動發送通知，若因標籤指向錯誤 commit 而需重建 GitHub Release，會導致通知重複（v2.17.5 發了兩次）
  - 通知從建置流程中移除，改為在 GitHub Release 確認建立成功後才發送
  - 新增版本標記檔（`dist/.notified_v{VERSION}`），防止同版本重複通知
  - `--notify-only` 會先驗證 GitHub Release 已發布才允許發送
- **Release 標籤現在固定指向正確的 commit**：`gh release create` 加入 `--target` 參數，明確指定 commit SHA，不再依賴 remote HEAD
  - 新增推送檢查：建立 Release 前驗證本機 HEAD 已推送至 remote，避免標籤指向舊 commit

---

## English

### Fixed
- **Discord release notification no longer fires multiple times**: previous versions sent the notification automatically during the build — if the GitHub Release needed to be recreated at the correct commit, the notification fired twice (v2.17.5 was announced twice)
  - Notification removed from the build pipeline; now fires only after GitHub Release creation is confirmed successful
  - Added a per-version marker file (`dist/.notified_v{VERSION}`) to prevent duplicate notifications
  - `--notify-only` now verifies the GitHub Release is published before sending
- **Release tag now pinned to the correct commit**: `gh release create` passes `--target` with the exact commit SHA instead of relying on remote HEAD
  - Added a pre-flight push check: verifies local HEAD is pushed to the remote before creating the release, preventing tags on stale commits
