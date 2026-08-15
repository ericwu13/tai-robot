"""Tests for DiscordNotifier deploy notifications (plain vs regime switching).

Regression: the regime-bot deploy notification used to fall through to
``bot_deployed()`` with ``strategy_var.get()`` — the leftover main-dropdown
value — so Discord announced a single, often unrelated, strategy and never
revealed that regime switching was enabled or which legs were in play.
"""

from src.live.discord_notify import DiscordNotifier


def _capture(notifier):
    sent = []
    notifier._send = lambda content: sent.append(content)
    return sent


def test_plain_deploy_message_shape():
    n = DiscordNotifier("token", "channel", bot_name="0710", symbol="TMF00")
    sent = _capture(n)
    n.bot_deployed(strategy="1分K均線交叉 1m SMA Cross", mode="半自動",
                   version="2.16.3")
    assert len(sent) == 1
    msg = sent[0]
    assert "Bot Deployed" in msg
    assert "v2.16.3" in msg
    assert "1分K均線交叉 1m SMA Cross" in msg
    # a plain deploy must NOT masquerade as regime switching
    assert "Regime Switching" not in msg


def test_regime_deploy_names_both_legs():
    n = DiscordNotifier("token", "channel", bot_name="07-10-regime-mode",
                        symbol="TMF00")
    sent = _capture(n)
    n.bot_deployed_regime(
        long_strategy="AI: DynamicExitPullbackStrategyV2",
        short_strategy="AI: BbandSmaShortV3",
        mode="半自動",
        version="2.16.3",
    )
    assert len(sent) == 1
    msg = sent[0]
    assert "Bot Deployed" in msg
    assert "v2.16.3" in msg
    # announces regime switching is enabled
    assert "多空切換 Regime Switching" in msg
    assert "🔄" in msg
    # both legs named — the whole point of the fix
    assert "AI: DynamicExitPullbackStrategyV2" in msg
    assert "AI: BbandSmaShortV3" in msg
    assert "做多 Long" in msg and "做空 Short" in msg
    assert "半自動" in msg
    # header still carries bot + symbol
    assert "07-10-regime-mode" in msg and "TMF00" in msg


def test_regime_deploy_with_restored_leg():
    n = DiscordNotifier("token", "channel", bot_name="07-22-regime",
                        symbol="TMF00")
    sent = _capture(n)
    n.bot_deployed_regime(
        long_strategy="AI: DynamicExitPullbackStrategyV2",
        short_strategy="AI: BbandSmaShortV3",
        mode="模擬",
        version="2.17.10",
        restored_leg="short",
        restored_strategy="AI: BbandSmaShortV3",
    )
    assert len(sent) == 1
    msg = sent[0]
    assert "Bot Deployed" in msg
    assert "多空切換 Regime Switching" in msg
    assert "✅ 已恢復 Restored" in msg
    assert "做空 SHORT" in msg
    assert "BbandSmaShortV3" in msg


def test_regime_deploy_fresh_no_restored_line():
    """Fresh deploy (no restored leg) must NOT include a restored line."""
    n = DiscordNotifier("token", "channel", bot_name="07-22-regime",
                        symbol="TMF00")
    sent = _capture(n)
    n.bot_deployed_regime(
        long_strategy="LongStrat", short_strategy="ShortStrat", mode="模擬",
    )
    assert len(sent) == 1
    assert "已恢復 Restored" not in sent[0]


def test_regime_deploy_news_status_line():
    """News enablement must be stated at deploy time, ON or OFF.

    Regression: a deploy silently dropped the news flag and the breaker
    stayed off for a week because nothing in the deploy notification said
    so. The line is unconditional — absence of news is now visible.
    """
    n = DiscordNotifier("token", "channel", bot_name="08-14-regime",
                        symbol="TMF00")
    sent = _capture(n)

    n.bot_deployed_regime("Long", "Short", "模擬")
    n.bot_deployed_regime("Long", "Short", "模擬", news_enabled=True)
    n.bot_deployed_regime("Long", "Short", "模擬", news_enabled=True,
                          news_tier2=True)
    # tier2 without the breaker itself is still OFF
    n.bot_deployed_regime("Long", "Short", "模擬", news_tier2=True)

    off, on, on_tier2, tier2_only = sent
    assert "📰 新聞斷路器 News breaker: ❌ OFF" in off
    assert "📰 新聞斷路器 News breaker: ✅ ON" in on
    assert "Tier2" not in on
    assert ("📰 新聞斷路器 News breaker: ✅ ON "
            "(+Tier2 強制進場 forced-entry)") in on_tier2
    assert "📰 新聞斷路器 News breaker: ❌ OFF" in tier2_only


def test_regime_deploy_disabled_notifier_no_raise():
    n = DiscordNotifier("", "")
    assert n.enabled is False
    n.bot_deployed_regime("Long", "Short", "半自動")  # must not raise
