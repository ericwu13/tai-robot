"""Tests for DiscordNotifier.evolution_verdict (message shape + truncation)."""

from src.live.discord_notify import DiscordNotifier


def _capture(notifier):
    sent = []
    notifier._send = lambda content: sent.append(content)
    return sent


def test_pass_verdict_message():
    n = DiscordNotifier("token", "channel", bot_name="0422", symbol="TMF00")
    sent = _capture(n)
    n.evolution_verdict(True, "🧬 EVO VERDICT (walk-forward) — PASS ✅\nline2")
    assert len(sent) == 1
    assert "Evolution PASS" in sent[0]
    assert "✅" in sent[0]
    assert "```" in sent[0]
    assert "line2" in sent[0]


def test_fail_verdict_message():
    n = DiscordNotifier("token", "channel")
    sent = _capture(n)
    n.evolution_verdict(False, "verdict body")
    assert "Evolution FAIL" in sent[0]
    assert "❌" in sent[0]


def test_long_block_truncated_under_discord_limit():
    n = DiscordNotifier("token", "channel")
    sent = _capture(n)
    n.evolution_verdict(False, "x" * 5000)
    assert len(sent[0]) < 2000
    assert "truncated" in sent[0]
