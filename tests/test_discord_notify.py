"""Discord notice for a fill-timeout restore (issue #319).

The downgrade notice stays ``fill_timeout_downgrade``. Coming back to
auto is its own message: it names the order and does not reuse the
operator hot-swap text from ``mode_switched``.
"""

from __future__ import annotations

from src.live.discord_notify import DiscordNotifier


def test_fill_timeout_restored_names_the_order_and_is_priority():
    notifier = DiscordNotifier("token", "999", bot_name="ShortOnly", symbol="TMF00")
    sent = []

    def _send(content, channel_id="", **kwargs):
        sent.append((content, channel_id, kwargs))

    notifier._send = _send
    notifier.fill_timeout_restored("exit", "2315623892999")

    assert len(sent) == 1
    content, _channel, kwargs = sent[0]
    assert kwargs.get("priority") is True
    assert "exit" in content
    assert "2315623892999" in content
    assert "auto" in content.lower()
    assert "全自動" in content
    assert "交易模式切換" not in content
    assert "Downgraded to semi-auto" not in content
