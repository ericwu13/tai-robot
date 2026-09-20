"""Bilingual UI string constants (Phase 1 skeleton).

Seam titles are imported from ``src.live.headless`` — they are a
byte-exact contract with ``HeadlessPolicy.answer``. Do not redefine them.

Migration rule: any Phase 1/2 diff that touches a line containing a
bilingual literal moves it here. No dedicated migration PR.
"""

from __future__ import annotations

from src.live.headless import (  # noqa: F401 — re-export the seam contract
    TITLE_DATA_RANGE,
    TITLE_EXISTING_POSITION,
    TITLE_STRATEGY_CHANGE,
)

# Window / chrome
APP_TITLE_PREFIX = "tai-robot AI 策略工作台 AI Strategy Workbench"
READY = "就緒 Ready"
INITIALIZING = "初始化中 Initializing..."

# Toolbar
DEPLOY_BOT = "部署機器人 Deploy Bot"
STOP_BOT = "停止機器人 Stop Bot"
TV_BACKTEST = "TV 回測 Backtest"
API_BACKTEST = "API 回測 Backtest"
TAIFEX_BACKTEST = "TAIFEX 回測 Backtest"
K_CHART = "K線圖 K Chart"
EXPORT_TRADES = "匯出交易 Export Trades"
SETTINGS_TOGGLE = "▶ 設定 Settings"
AI_REVIEW = "AI檢視 AI Review"
EVOLUTION = "🧬 進化 Evolution"
REPORT_ISSUE = "回報問題 Report Issue"
CHECK_UPDATES = "檢查更新 Check for Updates"

# Tabs
TAB_REPORT = "績效報告 Report"
TAB_TRADES = "交易明細 Trades"
TAB_LIVE = "即時 Live"
TAB_REGIME = "多空 Regime"
TAB_LOG = "紀錄 Log"

# Empty states
CHAT_PLACEHOLDER = "開始對話… / Start chatting…"
REPORT_EMPTY = "(尚無結果 no results yet — 執行回測或部署 run a backtest or deploy)"

# Status strip
STATUS_HISTORY = "紀錄 History"
CONNECTED_READY = "已連線 Connected - Ready"
DISCONNECTED = "斷線 Disconnected"

# Log tab filter
LOG_FILTER_ALL = "All"
LOG_FILTER_INFO = "Info"
LOG_FILTER_DEBUG = "Debug"
LOG_PAUSE = "暫停 Pause"
LOG_FOLLOW = "跟隨 Follow"


def as_dict() -> dict[str, str]:
    """All public string constants (for a future labels.json dump)."""
    skip = {"as_dict", "main"}
    return {
        name: value
        for name, value in globals().items()
        if name.isupper() and name not in skip and isinstance(value, str)
    }


def main() -> None:
    """Emit labels.json on stdout (Phase 3 web UI)."""
    import json
    print(json.dumps(as_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
