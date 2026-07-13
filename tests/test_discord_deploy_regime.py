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
    n.bot_deployed(strategy="1分K均線交叉 1m SMA Cross", mode="半自動")
    assert len(sent) == 1
    msg = sent[0]
    assert "Bot Deployed" in msg
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
    )
    assert len(sent) == 1
    msg = sent[0]
    assert "Bot Deployed" in msg
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


def test_regime_deploy_disabled_notifier_no_raise():
    n = DiscordNotifier("", "")
    assert n.enabled is False
    n.bot_deployed_regime("Long", "Short", "半自動")  # must not raise
