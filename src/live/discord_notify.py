"""Discord bot notifications for live trading events.

Sends non-blocking notifications to a Discord channel via bot token + REST API.
All sends run in a background thread to avoid blocking the main/Tkinter thread.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone, timedelta

_TPE = timezone(timedelta(hours=8))
_API_BASE = "https://discord.com/api/v10"


def _taipei_now() -> datetime:
    return datetime.now(_TPE)


class DiscordNotifier:
    """Fire-and-forget Discord bot message sender."""

    def __init__(self, bot_token: str, channel_id: str,
                 bot_name: str = "", symbol: str = ""):
        self._token = bot_token.strip() if bot_token else ""
        self._channel_id = channel_id.strip() if channel_id else ""
        self._bot_name = bot_name
        self._symbol = symbol

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._channel_id)

    def _send(self, content: str) -> None:
        """Send a message via Discord bot REST API in a background thread."""
        if not self.enabled:
            return

        def _post():
            try:
                import httpx
                url = f"{_API_BASE}/channels/{self._channel_id}/messages"
                headers = {
                    "Authorization": f"Bot {self._token}",
                    "Content-Type": "application/json",
                }
                httpx.post(url, json={"content": content},
                           headers=headers, timeout=10)
            except Exception:
                pass  # best effort — don't crash the bot for notifications

        threading.Thread(target=_post, daemon=True).start()

    def notify(self, message: str) -> None:
        """Public method to send a free-form notification with the bot header."""
        self._send(f"{self._header()}\n{message}")

    def _header(self) -> str:
        ts = _taipei_now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [f"**[{ts}]**"]
        if self._bot_name:
            parts.append(f"`{self._bot_name}`")
        if self._symbol:
            parts.append(f"`{self._symbol}`")
        return " ".join(parts)

    def order_sent(self, side: str, symbol: str, price_label: str,
                   sim_price: int, order_id: str) -> None:
        self._send(
            f"{self._header()}\n"
            f"📤 **委託送出 Order Sent**\n"
            f"方向: **{side}** | 商品: `{symbol}` | "
            f"價格: {price_label} | 模擬價: {sim_price:,}\n"
            f"委託編號: `{order_id}`"
        )

    def order_failed(self, side: str, symbol: str, code: int, error: str) -> None:
        self._send(
            f"{self._header()}\n"
            f"❌ **委託失敗 Order Failed**\n"
            f"方向: {side} | 商品: `{symbol}` | "
            f"錯誤碼: {code} | {error}"
        )

    def fill_confirmed(self, action_type: str, fill_price: str = "") -> None:
        price_str = f" @**{float(fill_price):,.1f}**" if fill_price else ""
        self._send(
            f"{self._header()}\n"
            f"✅ **成交確認 Fill Confirmed** ({action_type}){price_str}"
        )

    def fill_timeout_downgrade(self, action_type: str, timeout_s: float) -> None:
        self._send(
            f"{self._header()}\n"
            f"⚠️ **成交超時 Fill Timeout** ({action_type}, {timeout_s:.0f}s)\n"
            f"已降級為半自動 Downgraded to semi-auto"
        )

    def bot_deployed(self, strategy: str, mode: str) -> None:
        self._send(
            f"{self._header()}\n"
            f"🚀 **機器人啟動 Bot Deployed**\n"
            f"策略: {strategy} | 模式: {mode}"
        )

    def bot_stopped(self, trades: int, pnl: int) -> None:
        self._send(
            f"{self._header()}\n"
            f"🛑 **機器人停止 Bot Stopped**\n"
            f"交易: {trades} 筆 | P&L: {pnl:+,}"
        )

    def force_close_failed(self, symbol: str, attempts: int, last_error: str) -> None:
        self._send(
            f"{self._header()}\n"
            f"🚨 **強制平倉失敗 FORCE CLOSE FAILED** 🚨\n"
            f"商品: `{symbol}` | 重試: {attempts} 次\n"
            f"最後錯誤: {last_error}\n"
            f"**需要立即人工介入！Position may still be open!**"
        )

    def daily_loss_limit(self, net_pnl: int, limit: int) -> None:
        self._send(
            f"{self._header()}\n"
            f"🚫 **每日虧損上限 Daily Loss Limit**\n"
            f"淨損益: {net_pnl:+,} | 上限: -{limit:,}"
        )
