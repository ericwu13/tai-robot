"""Discord bot notifications for live trading events.

Sends non-blocking notifications to a Discord channel via bot token + REST API.
All sends run in a background thread to avoid blocking the main/Tkinter thread.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone, timedelta

_TPE = timezone(timedelta(hours=8))
_API_BASE = "https://discord.com/api/v10"

# Trading mode → readable bilingual label for the mode-switch notification.
_MODE_LABELS = {
    "paper": "模擬 Paper",
    "semi_auto": "輔助 Semi-Auto",
    "auto": "全自動 Auto",
}


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

    def bot_deployed_regime(
        self, long_strategy: str, short_strategy: str, mode: str
    ) -> None:
        """Deploy notification for a regime-switching bot.

        Distinct from :meth:`bot_deployed` so the message announces that
        regime switching is enabled and names both legs, instead of a single
        strategy (which for a regime bot is just the timeframe donor).
        """
        self._send(
            f"{self._header()}\n"
            f"🚀 **機器人啟動 Bot Deployed**\n"
            f"🔄 **多空切換 Regime Switching** 已啟用 Enabled\n"
            f"做多 Long: {long_strategy} | 做空 Short: {short_strategy}\n"
            f"模式: {mode}"
        )

    def bot_stopped(self, trades: int, pnl: int) -> None:
        self._send(
            f"{self._header()}\n"
            f"🛑 **機器人停止 Bot Stopped**\n"
            f"交易: {trades} 筆 | P&L: {pnl:+,}"
        )

    def mode_switched(self, old_mode: str, new_mode: str) -> None:
        """Notify a live hot-swap of the trading mode.

        Takes the raw mode keys and maps them to readable labels here so
        the message format stays self-contained. bot_name / symbol /
        timestamp come from _header() (same as every other notification),
        so they aren't duplicated in the body.
        """
        old_label = _MODE_LABELS.get(old_mode, old_mode)
        new_label = _MODE_LABELS.get(new_mode, new_mode)
        self._send(
            f"{self._header()}\n"
            f"🔄 **交易模式切換 Trading Mode Switched**\n"
            f"{old_label} → {new_label}"
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

    def strategy_improved(
        self,
        composite: float,
        previous_best: float,
        delta: float,
        n_trades: int,
        sortino: float,
        win_rate: float,
        profit_factor: float,
        max_drawdown_pct: float,
        source: str,
        first_run: bool = False,
    ) -> None:
        """Announce that the strategy hit a new best fitness composite.

        Wired by :func:`src.evolution.notify.check_and_notify_after_report`
        which runs right after the daily report is saved.  ``first_run``
        gets a slightly different header because there's no prior best
        to compare against — composite IS the delta.
        """
        if first_run:
            headline = "🌱 **首次評分 First Fitness Score**"
            delta_str = f"composite **{composite:.3f}**"
        else:
            headline = "📈 **策略進步 Strategy Improvement**"
            delta_str = (
                f"composite **{composite:.3f}** "
                f"(前最佳 prev best {previous_best:.3f}, "
                f"Δ {delta:+.3f})"
            )
        self._send(
            f"{self._header()}\n"
            f"{headline}\n"
            f"{delta_str}\n"
            f"交易數 trades: {n_trades} | "
            f"勝率 win-rate: {win_rate:.1%} | "
            f"PF: {profit_factor:.2f}\n"
            f"Sortino: {sortino:.2f} | "
            f"最大回撤 max DD: {max_drawdown_pct:.1f}% | "
            f"來源 source: `{source}`"
        )

    def evolution_verdict(self, passed: bool, verdict_block: str) -> None:
        """Send the 🧬 evolution pipeline verdict (manual or weekly auto).

        The block is the same text shown in the chat log, wrapped in a
        code fence for monospace alignment. Discord caps messages at
        2000 chars — truncate the body defensively (header + fences eat
        some budget).
        """
        body = verdict_block
        if len(body) > 1700:
            body = body[:1700] + "\n…(truncated)"
        icon = "✅" if passed else "❌"
        self._send(
            f"{self._header()}\n"
            f"🧬 **演化結果 Evolution {'PASS' if passed else 'FAIL'}** {icon}\n"
            f"```\n{body}\n```"
        )

    def daily_report(self, report: dict) -> None:
        """Send a daily report summary to Discord."""
        date = report.get("date", "?")
        summary = report.get("summary", {})
        regime = report.get("market_regime")
        strategy = report.get("strategy", {})
        session = report.get("session") or {}

        total = summary.get("total_trades", 0)
        pnl = summary.get("total_pnl", 0)
        win_rate = summary.get("win_rate", 0)
        pf = summary.get("profit_factor", 0)
        dd = summary.get("max_drawdown", 0)

        lines = [
            f"{self._header()}",
            f"📊 **每日報告 Daily Report** — {date}",
        ]

        # Session identifier line: bot_name + version + start time so the
        # report stands on its own when read out of context (e.g. saved
        # JSON, archived Discord logs). bot_name is also in _header() but
        # belongs in the body for readers who land on the message alone.
        session_parts: list[str] = []
        bot = session.get("bot_name") or ""
        version = session.get("version") or ""
        started = session.get("started_at") or ""
        if bot:
            session_parts.append(f"機器人 Bot: `{bot}`")
        if version:
            session_parts.append(f"v{version}")
        if started:
            # ISO "2026-04-27T15:02:30" → "2026-04-27 15:02"
            started_short = started.replace("T", " ")[:16]
            session_parts.append(f"啟動 Started: {started_short}")
        if session_parts:
            lines.append(" · ".join(session_parts))

        lines.extend([
            f"策略: {strategy.get('name', '?')}",
            f"交易: {total} 筆 | 勝率: {win_rate:.1%} | PF: {pf:.2f}",
            f"損益: {pnl:+,} | 最大回撤: {dd:,}",
        ])
        # Real-order subset (semi_auto/auto fills). The headline numbers
        # above are the full simulated view; this line shows what actually
        # hit the real account. Absent for pure paper sessions.
        real_summary = report.get("real_summary")
        if real_summary:
            lines.append(
                f"實單 Real: {real_summary.get('total_trades', 0)} 筆 | "
                f"勝率: {real_summary.get('win_rate', 0):.1%} | "
                f"損益: {real_summary.get('total_pnl', 0):+,}"
            )
        if regime:
            lines.append(
                f"市場狀態: {regime.get('label', '?')} "
                f"(ADX {regime.get('adx', 0):.1f})"
            )
        # Per-strategy P&L breakdown (regime switching bots)
        per_strategy = report.get("per_strategy") or {}
        if per_strategy:
            parts = []
            for sname, smetrics in per_strategy.items():
                parts.append(f"  {sname}: {smetrics.get('total_trades', 0)} 筆, "
                             f"P&L {smetrics.get('total_pnl', 0):+,}")
            lines.append("策略別 Per-strategy:\n" + "\n".join(parts))
        # Regime switching status
        regime_sw = report.get("regime_switching")
        if regime_sw:
            leg = regime_sw.get("active_leg", "?")
            lines.append(f"🔄 多空切換 Regime: {leg}")
        self._send("\n".join(lines))
