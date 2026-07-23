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
                 bot_name: str = "", symbol: str = "",
                 evolution_channel_id: str = "",
                 daily_report_channel_id: str = "",
                 regime_channel_id: str = ""):
        self._token = bot_token.strip() if bot_token else ""
        self._channel_id = channel_id.strip() if channel_id else ""
        # Optional dedicated channels. Empty → fall back to the main channel
        # (via _resolve_channel), so a single-channel setup keeps working.
        self._evolution_channel_id = (
            evolution_channel_id.strip() if evolution_channel_id else "")
        self._daily_report_channel_id = (
            daily_report_channel_id.strip() if daily_report_channel_id else "")
        self._regime_channel_id = (
            regime_channel_id.strip() if regime_channel_id else "")
        self._bot_name = bot_name
        self._symbol = symbol

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._channel_id)

    def _resolve_channel(self, channel_id: str = "") -> str:
        """Pick the target channel, falling back to the main channel."""
        return channel_id or self._channel_id

    def _send(self, content: str, channel_id: str = "") -> None:
        """Send a message via Discord bot REST API in a background thread.

        ``channel_id`` optionally overrides the default channel (used to
        route evolution logs / daily reports to their own channels). Empty
        → the main channel.
        """
        if not self.enabled:
            return
        target = self._resolve_channel(channel_id)
        if not target:
            return

        def _post():
            try:
                import httpx
                url = f"{_API_BASE}/channels/{target}/messages"
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

    def notify_evolution(self, message: str) -> None:
        """Free-form notification routed to the evolution channel.

        Same shape as :meth:`notify` but targets the dedicated evolution
        channel (falls back to the main channel when unconfigured). Use for
        evolution status/skip/error messages so they don't clutter the
        trading channel.
        """
        self._send(f"{self._header()}\n{message}", self._evolution_channel_id)

    def _header(self) -> str:
        ts = _taipei_now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [f"**[{ts}]**"]
        if self._bot_name:
            parts.append(f"`{self._bot_name}`")
        if self._symbol:
            parts.append(f"`{self._symbol}`")
        return " ".join(parts)

    def order_sent(self, side: str, symbol: str, price_label: str,
                   sim_price: int, order_id: str, strategy: str = "") -> None:
        strat_line = f"策略 Strategy: `{strategy}`\n" if strategy else ""
        self._send(
            f"{self._header()}\n"
            f"📤 **委託送出 Order Sent**\n"
            f"{strat_line}"
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

    def fill_confirmed(self, action_type: str, fill_price: str = "",
                       strategy: str = "",
                       estimated: bool = False) -> None:
        est = " (est.)" if estimated else ""
        price_str = f" @**{float(fill_price):,.1f}**{est}" if fill_price else ""
        strat_str = f"\n策略 Strategy: `{strategy}`" if strategy else ""
        self._send(
            f"{self._header()}\n"
            f"✅ **成交確認 Fill Confirmed** ({action_type}){price_str}"
            f"{strat_str}"
        )

    def fill_timeout_downgrade(self, action_type: str, timeout_s: float) -> None:
        self._send(
            f"{self._header()}\n"
            f"⚠️ **成交超時 Fill Timeout** ({action_type}, {timeout_s:.0f}s)\n"
            f"已降級為半自動 Downgraded to semi-auto"
        )

    def bot_deployed(self, strategy: str, mode: str,
                     version: str = "") -> None:
        ver = f" v{version}" if version else ""
        self._send(
            f"{self._header()}\n"
            f"🚀 **機器人啟動 Bot Deployed**{ver}\n"
            f"策略: {strategy} | 模式: {mode}"
        )

    def bot_deployed_regime(
        self, long_strategy: str, short_strategy: str, mode: str,
        version: str = "",
    ) -> None:
        """Deploy notification for a regime-switching bot.

        Distinct from :meth:`bot_deployed` so the message announces that
        regime switching is enabled and names both legs, instead of a single
        strategy (which for a regime bot is just the timeframe donor).
        """
        ver = f" v{version}" if version else ""
        self._send(
            f"{self._header()}\n"
            f"🚀 **機器人啟動 Bot Deployed**{ver}\n"
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
            f"來源 source: `{source}`",
            self._evolution_channel_id,
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
            f"```\n{body}\n```",
            self._evolution_channel_id,
        )

    def daily_report(self, report: dict) -> None:
        """Send a compact daily summary to Discord.

        Format:
          📊 {bot_name} · {date}
          {active_strategy}
          今日 Today: +1,200 · 3 筆
          累計 Cumul: +5,600 · 12 筆 · 勝率 Win 67%
        """
        date = report.get("date", "?")
        strategy = report.get("strategy", {})
        regime_sw = report.get("regime_switching")
        summary = report.get("summary", {}) or {}

        today_pnl = summary.get("today_pnl", 0) or 0
        today_trades = summary.get("today_trades", 0) or 0
        total_pnl = summary.get("total_pnl", 0) or 0
        total_trades = summary.get("total_trades", 0) or 0
        win_rate = summary.get("win_rate", 0) or 0

        bot = self._bot_name or "?"
        strat_name = strategy.get("name", "?")
        if regime_sw:
            leg = regime_sw.get("active_leg", "?")
            _LEG_LABELS = {"long": "做多 Long", "short": "做空 Short",
                           "idle": "閒置 Idle"}
            strat_line = f"🔄 {_LEG_LABELS.get(leg, leg)} | {strat_name}"
        else:
            strat_line = strat_name

        lines = [
            f"📊 **{bot}** · {date}",
            strat_line,
            f"今日 Today: {today_pnl:+,} · {today_trades} 筆",
            f"累計 Cumul: {total_pnl:+,} · {total_trades} 筆 · "
            f"勝率 Win {win_rate:.0f}%",
        ]
        self._send("\n".join(lines), self._daily_report_channel_id)

    def regime_swap(self, from_leg: str, to_leg: str,
                    strategy_name: str) -> None:
        """Notify a regime strategy swap."""
        _LEG_LABELS = {"long": "做多 Long", "short": "做空 Short",
                       "idle": "閒置 Idle"}
        ts = _taipei_now().strftime("%Y-%m-%d %H:%M:%S")
        self._send(
            f"🔄 **{self._bot_name}** · 策略切換 Strategy Swap\n"
            f"{_LEG_LABELS.get(from_leg, from_leg)} → "
            f"{_LEG_LABELS.get(to_leg, to_leg)}\n"
            f"{strategy_name}\n"
            f"{ts}",
            self._regime_channel_id,
        )

    def notify_regime(self, message: str) -> None:
        """Free-form notification routed to the regime channel."""
        self._send(f"{self._header()}\n{message}", self._regime_channel_id)
