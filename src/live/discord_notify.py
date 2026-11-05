"""Discord bot notifications for live trading events.

Sends non-blocking notifications to a Discord channel via bot token + REST API.
One daemon worker drains a bounded queue so a send never blocks the Tk thread
and a stalled POST cannot spawn a thread per message.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta

from version import APP_VERSION

logger = logging.getLogger(__name__)

_TPE = timezone(timedelta(hours=8))
_API_BASE = "https://discord.com/api/v10"

# Send budget. ``httpx``'s timeout bounds connect/read/write but NOT
# ``socket.getaddrinfo`` — name resolution is an uninterruptible C call.
# On 2026-11-04 every bot on the live host had _post threads parked in
# getaddrinfo for 90+ s while this client was already configured with a
# 10 s timeout, so the timeout alone is not a containment story.
#
# What is containable is the queue in front of that stall. One worker
# thread owns the shared client. Callers enqueue without blocking. When
# the queue is full, routine messages are counted and dropped; priority
# messages (HUNG, order, trade, exit) wait in a small backlog and are
# flushed in arrival order once a send succeeds. The 429 backoff stays
# inside the worker, so a retry already in flight is not a new enqueue
# and cannot be dropped for queue pressure.
HTTP_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 5.0
SEND_QUEUE_MAX = 8
PRIORITY_BACKLOG_MAX = 8

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
        self._http: "httpx.Client | None" = None
        self._http_lock = threading.Lock()
        # One worker, one bounded queue. ``put_nowait`` is the only enqueue
        # the Tk thread (and the hang watchdog) ever do.
        self._queue: queue.Queue = queue.Queue(maxsize=SEND_QUEUE_MAX)
        self._priority_backlog: deque = deque()
        self._state_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._dropped = 0
        self._late = 0
        self._priority_overflow = 0
        if self._token and self._channel_id:
            if not self._evolution_channel_id:
                logger.warning(
                    "discord_evolution_channel_id is empty — evolution "
                    "notifications will use the main channel")

    def _get_http(self):
        """Lazily create a shared httpx.Client for connection pooling."""
        if self._http is None:
            import httpx
            self._http = httpx.Client(
                timeout=httpx.Timeout(HTTP_TIMEOUT_S, connect=CONNECT_TIMEOUT_S))
        return self._http

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._channel_id)

    def _resolve_channel(self, channel_id: str = "") -> str:
        """Pick the target channel, falling back to the main channel."""
        return channel_id or self._channel_id

    def _send(self, content: str, channel_id: str = "", *,
              priority: bool = False) -> None:
        """Enqueue one message. Never blocks the caller.

        ``channel_id`` optionally overrides the default channel (used to
        route evolution logs / daily reports to their own channels). Empty
        → the main channel.

        ``priority`` defaults to False. Routine messages are dropped when
        the queue is full. Priority messages are parked in
        :data:`PRIORITY_BACKLOG_MAX` and flushed in order after a send
        succeeds. The default stays routine so a caller that does not opt
        in cannot fill that backlog.

        HTTP 429 is retried by the worker (1s, 2s, 4s), not by enqueueing
        again, so a retry cannot be dropped because the queue is full.
        """
        if not self.enabled:
            return
        target = self._resolve_channel(channel_id)
        if not target:
            return
        logger.debug(
            "Discord send attempt: channel=%s, msg_len=%d, bot=%s",
            target, len(content), self._bot_name,
        )
        self._ensure_worker()
        item = (content, target, priority)
        # One attempt. The ``return`` is the drop: it must not fall through
        # into a send, and it must not ``continue`` (that spins on a full
        # queue and blocks the caller, which is the Tk thread).
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                self._on_queue_full(item, priority)
                return

    def _ensure_worker(self) -> None:
        worker = self._worker
        if worker is not None and worker.is_alive():
            return
        with self._worker_lock:
            worker = self._worker
            if worker is not None and worker.is_alive():
                return
            worker = threading.Thread(
                target=self._worker_loop,
                name="discord-notify",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def _on_queue_full(self, item, priority: bool) -> None:
        """Queue is full. Park a priority message, or count a routine drop.

        Returns without sending. A stalled POST must not be done on the
        caller, and a full queue must not start another thread.
        """
        if priority:
            with self._state_lock:
                if len(self._priority_backlog) < PRIORITY_BACKLOG_MAX:
                    self._priority_backlog.append(item)
                    return
                self._priority_overflow += 1
            logger.warning(
                "Discord priority backlog overflow (bot=%s). "
                "A stalled send filled the priority backlog.",
                self._bot_name,
            )
            return
        with self._state_lock:
            self._dropped += 1
        logger.warning(
            "Discord send dropped: notifier queue full (bot=%s). "
            "A stalled send must not queue more work behind it.",
            self._bot_name,
        )
        return

    def _worker_loop(self) -> None:
        while True:
            item, from_backlog, queued = self._next_item()
            content, target, _priority = item
            try:
                ok = self._deliver(content, target)
                if from_backlog and ok:
                    with self._state_lock:
                        self._late += 1
                if ok:
                    # Catch-up runs before task_done so this item's success
                    # and the one summary stay in that order.
                    self._post_stall_summary()
            finally:
                if queued:
                    self._queue.task_done()

    def _next_item(self):
        """Priority backlog first, then the main queue. Blocks on the queue."""
        with self._state_lock:
            if self._priority_backlog:
                return self._priority_backlog.popleft(), True, False
        return self._queue.get(), False, True

    def _post_stall_summary(self) -> None:
        """One catch-up line after a successful send, once the backlog is empty."""
        with self._state_lock:
            if self._priority_backlog:
                return
            dropped = self._dropped
            late = self._late
            overflow = self._priority_overflow
            if dropped == 0 and late == 0 and overflow == 0:
                return
            # Clear first so this post cannot enqueue a second summary.
            self._dropped = 0
            self._late = 0
            self._priority_overflow = 0
        text = (
            f"Discord was stalled: {dropped} messages dropped, "
            f"{late} delivered late"
        )
        if overflow:
            text += f", {overflow} priority overflow"
        self._deliver(f"{self._header()}\n{text}", self._channel_id)

    def _deliver(self, content: str, target: str) -> bool:
        """POST one message. Retry HTTP 429 with backoff 1s, 2s, 4s.

        Returns True when Discord accepted the message. The lock is not
        held across the backoff sleep, and a 429 never re-enters the queue.
        """
        try:
            url = f"{_API_BASE}/channels/{target}/messages"
            headers = {
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
            }
            delay = 1.0
            for _attempt in range(3):
                with self._http_lock:
                    client = self._get_http()
                    resp = client.post(url, json={"content": content},
                                       headers=headers)
                if resp.status_code != 429:
                    if resp.status_code >= 400:
                        logger.error(
                            "Discord send failed: HTTP %d → %s "
                            "(channel=%s, bot=%s)",
                            resp.status_code, resp.text[:500], target,
                            self._bot_name,
                        )
                        return False
                    return True
                time.sleep(delay)
                delay *= 2
            logger.warning(
                "Discord send failed after 3 retries (429 rate-limited), "
                "channel=%s, bot=%s", target, self._bot_name,
            )
            return False
        except Exception:
            logger.exception(
                "Discord send error (channel=%s, bot=%s)",
                target, self._bot_name,
            )
            return False

    def notify(self, message: str, *, priority: bool = False) -> None:
        """Public method to send a free-form notification with the bot header.

        ``priority`` defaults to False (routine). The HUNG alert passes
        True; order, trade, and exit methods opt in themselves.
        """
        self._send(f"{self._header()}\n{message}", priority=priority)

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
            f"委託編號: `{order_id}`",
            priority=True,
        )

    def order_failed(self, side: str, symbol: str, code: int, error: str) -> None:
        self._send(
            f"{self._header()}\n"
            f"❌ **委託失敗 Order Failed**\n"
            f"方向: {side} | 商品: `{symbol}` | "
            f"錯誤碼: {code} | {error}",
            priority=True,
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
            f"{strat_str}",
            priority=True,
        )

    def exit_fill_unconfirmed(self, sim_price: str = "",
                              strategy: str = "") -> None:
        """The exit-fill retry ended without a broker price (issue #218).

        Distinct from :meth:`fill_confirmed` with ``estimated=True``: that
        one still reads as a fill confirmation. This one says the broker
        price was never captured and the simulated price is what stands.
        """
        price_str = ""
        if sim_price:
            try:
                price_str = f" @**{float(sim_price):,.1f}**"
            except (TypeError, ValueError):
                price_str = f" @{sim_price}"
        strat_str = f"\n策略 Strategy: `{strategy}`" if strategy else ""
        self._send(
            f"{self._header()}\n"
            f"⚠️ **出場成交價未確認 Exit Fill Unconfirmed**{price_str} (est.)\n"
            f"券商成交價未取得，沿用模擬價。Broker fill was not captured; "
            f"the simulated price is kept.{strat_str}",
            priority=True,
        )

    def fill_price_correction(self, action_type: str, real_price: int,
                              est_price: int, strategy: str = "") -> None:
        """Follow-up when the real fill price arrives after an (est.) notice."""
        strat_str = f"\n策略 Strategy: `{strategy}`" if strategy else ""
        self._send(
            f"{self._header()}\n"
            f"🔁 **成交價更正 Fill Price Correction** ({action_type}) "
            f"@**{real_price:,}** (原估 est. was {est_price:,})"
            f"{strat_str}",
            priority=True,
        )

    def fill_timeout_downgrade(self, action_type: str, timeout_s: float) -> None:
        self._send(
            f"{self._header()}\n"
            f"⚠️ **成交超時 Fill Timeout** ({action_type}, {timeout_s:.0f}s)\n"
            f"已降級為半自動 Downgraded to semi-auto",
            priority=True,
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
        restored_leg: str = "",
        restored_strategy: str = "",
        news_enabled: bool = False,
        news_tier2: bool = False,
        news_directional: bool = False,
    ) -> None:
        """Deploy notification for a regime-switching bot.

        Distinct from :meth:`bot_deployed` so the message announces that
        regime switching is enabled and names both legs, instead of a single
        strategy (which for a regime bot is just the timeframe donor).

        When *restored_leg* is provided (``"long"`` or ``"short"``), an extra
        line announces the restored regime so the user sees one combined
        message instead of two separate notifications.

        The news line always states whether the news circuit breaker is ON or
        OFF, so a deploy that silently dropped the flag is visible at deploy
        time instead of going unnoticed for days. Same rule for the
        directional (suppress_scope) sub-mode: whenever the breaker is ON,
        the line states ✅/❌ explicitly — a bot the user believes is
        directional but that deployed as gate-both would otherwise sit
        silent through the exact incident the knob exists to prevent.
        """
        ver = f" v{version}" if version else ""
        if news_enabled and news_tier2:
            news_line = ("📰 新聞斷路器 News breaker: ✅ ON "
                         "(+Tier2 強制進場 forced-entry)")
        elif news_enabled:
            news_line = "📰 新聞斷路器 News breaker: ✅ ON"
        else:
            news_line = "📰 新聞斷路器 News breaker: ❌ OFF"
        if news_enabled:
            news_line += (
                " | 方向性 directional: ✅ 只擋衝突方向 conflicting-leg only"
                if news_directional else
                " | 方向性 directional: ❌ 擋雙向 gates both legs")
        lines = [
            f"{self._header()}",
            f"🚀 **機器人啟動 Bot Deployed**{ver}",
            f"🔄 **多空切換 Regime Switching** 已啟用 Enabled",
            f"做多 Long: {long_strategy} | 做空 Short: {short_strategy}",
            f"模式: {mode}",
            news_line,
        ]
        if restored_leg in ("long", "short"):
            _LEG_LABELS = {"long": "做多 LONG", "short": "做空 SHORT"}
            lines.append(
                f"✅ 已恢復 Restored: "
                f"{_LEG_LABELS[restored_leg]} · {restored_strategy}")
        self._send("\n".join(lines))

    def regime_idle_warning(self) -> None:
        """Warn that signals are suppressed while awaiting regime classification."""
        self._send(
            f"{self._header()}\n"
            f"⚠️ **策略訊號暫停 Signals Suppressed**\n"
            f"Bot 處於 regime-idle 狀態，等待市場分類結果。"
            f"在套用 regime 前不會進行任何交易。\n"
            f"Awaiting regime classification — no trades until regime is applied."
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

    def mode_switch_refused(self, new_mode: str, reason: str,
                            requested_by: str = "") -> None:
        """A dashboard mode request was refused by the bot's own checks."""
        label = _MODE_LABELS.get(new_mode, new_mode)
        who = f"（請求者 {requested_by[:64]}）" if requested_by else ""
        self._send(
            f"{self._header()}\n"
            f"⛔ **模式切換被拒 Mode Switch Refused** → {label}{who}\n"
            f"{reason}"
        )

    def force_close_failed(self, symbol: str, attempts: int, last_error: str) -> None:
        self._send(
            f"{self._header()}\n"
            f"🚨 **強制平倉失敗 FORCE CLOSE FAILED** 🚨\n"
            f"商品: `{symbol}` | 重試: {attempts} 次\n"
            f"最後錯誤: {last_error}\n"
            f"**需要立即人工介入！Position may still be open!**",
            priority=True,
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
          📊 {bot_name} · {date} · v{version}
          AI: {strategy}
          市場 Regime: {bilingual_regime_label}   (omitted when unknown/None)
          今日 Today: +1,200 · 3 筆
          累計 Cumul: +5,600 · 12 筆 · 勝率 Win 67%
        """
        from src.regime.manager import _regime_label

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
            f"📊 **{bot}** · {date} · v{APP_VERSION}",
            f"AI: {strat_line}",
        ]
        if regime_sw:
            eff = regime_sw.get("effective_regime")
            if eff:
                lines.append(f"市場 Regime: {_regime_label(eff)}")
        elif (mr := report.get("market_regime")) and mr.get("label"):
            lines.append(f"市場 Regime: {_regime_label(mr['label'])}")
        lines.extend([
            f"今日 Today: {int(today_pnl):+,} · {today_trades} 筆",
            f"累計 Cumul: {int(total_pnl):+,} · {total_trades} 筆 · "
            f"勝率 Win {win_rate:.0f}%",
        ])
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
