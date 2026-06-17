"""Tests for DiscordNotifier.mode_switched (message shape + label mapping)."""

from src.live.discord_notify import DiscordNotifier


def _capture(notifier):
    sent = []
    notifier._send = lambda content: sent.append(content)
    return sent


def test_mode_switch_message_shape():
    n = DiscordNotifier("token", "channel", bot_name="0422", symbol="TMF00")
    sent = _capture(n)
    n.mode_switched("paper", "semi_auto")
    assert len(sent) == 1
    msg = sent[0]
    assert "Trading Mode Switched" in msg
    assert "🔄" in msg
    # header carries bot + symbol (not duplicated in the body)
    assert "0422" in msg and "TMF00" in msg
    # readable labels, arrow between them
    assert "模擬 Paper → 輔助 Semi-Auto" in msg


def test_all_mode_labels():
    n = DiscordNotifier("token", "channel")
    sent = _capture(n)
    n.mode_switched("semi_auto", "auto")
    assert "輔助 Semi-Auto → 全自動 Auto" in sent[0]
    n.mode_switched("auto", "paper")
    assert "全自動 Auto → 模擬 Paper" in sent[1]


def test_unknown_mode_falls_back_to_raw_key():
    n = DiscordNotifier("token", "channel")
    sent = _capture(n)
    n.mode_switched("paper", "weird")
    assert "模擬 Paper → weird" in sent[0]


def test_disabled_notifier_sends_nothing():
    # No token/channel → not enabled → real _send is a no-op (we don't
    # monkeypatch here, so a send attempt would try httpx). enabled is
    # False, so _send returns early.
    n = DiscordNotifier("", "")
    assert n.enabled is False
    n.mode_switched("paper", "semi_auto")  # must not raise
