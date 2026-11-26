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


def _note():
    return DiscordNotifier("token", "999", bot_name="b")


def test_false_deliver_is_retried_and_read_timeout_is_not():
    import httpx
    import threading
    import time

    note = _note()
    calls, retried, done = [], [], threading.Event()

    def _deliver(content, target):
        calls.append(content)
        if len(calls) == 1:
            return False
        done.set()
        return True

    orig = note._retry_failed_item
    note._deliver = _deliver
    note._retry_failed_item = lambda item, backlog: retried.append(item[0]) or orig(item, backlog)
    note._send("ping", priority=True)
    assert done.wait(3), calls
    assert calls == ["ping", "ping"] and retried == ["ping"]

    posts = []

    class _Client:
        def post(self, *args, **kwargs):
            posts.append(1)
            raise httpx.ReadTimeout("read")

    late = _note()
    late._http = _Client()
    late._send("once", priority=True)
    deadline = time.monotonic() + 1.0
    while not posts and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.15)
    assert posts == [1]


def test_retry_after_header_sets_the_429_sleep_and_caps_it(monkeypatch):
    import src.live.discord_notify as dn

    sleeps = []
    monkeypatch.setattr(dn.time, "sleep", lambda delay: sleeps.append(delay))

    class _Resp:
        def __init__(self, code, retry_after=None):
            self.status_code, self.text = code, ""
            self.headers = {} if retry_after is None else {"Retry-After": retry_after}

    class _Client:
        def __init__(self, responses):
            self._left = list(responses)

        def post(self, *args, **kwargs):
            return self._left.pop(0)

    def _run(responses):
        sleeps.clear()
        note = _note()
        note._http = _Client(responses)
        return note._deliver("hi", "999")

    assert _run([_Resp(429, "3"), _Resp(200)]) is True and sleeps == [3.0]
    assert _run([_Resp(429, "999")] * 3) is False and sleeps == [15.0, 15.0, 15.0]
    assert _run([_Resp(429, "soon")] * 3) is False and sleeps == [1.0, 2.0, 4.0]


def test_flush_exit_timeout_matches_the_429_cycle():
    from src.live.discord_notify import (
        CONNECT_TIMEOUT_S, FLUSH_EXIT_TIMEOUT_S, HTTP_TIMEOUT_S, WORKER_SEND_RETRIES,
    )
    assert FLUSH_EXIT_TIMEOUT_S == (
        (HTTP_TIMEOUT_S * 3 + 1 + 2 + 4) * (1 + WORKER_SEND_RETRIES) + CONNECT_TIMEOUT_S)
