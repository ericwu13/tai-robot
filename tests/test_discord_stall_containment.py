"""A stalled Discord send must not queue later sends behind it.

2026-11-04: every bot on one host had ``_post`` threads parked in
``socket.getaddrinfo`` for 90+ seconds while the shared ``httpx.Client``
was already configured with a 10 s timeout. httpx bounds connect / read /
write; it does not bound name resolution, which is an uninterruptible C
call. So the timeout is not the containment story.

What is containable is the queue: ``_http_lock`` guards the shared
client, so one wedged thread used to park every later notification
behind it for the whole stall. These tests pin the bounded lock wait and
the explicit per-phase timeout.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from src.live import discord_notify
from src.live.discord_notify import DiscordNotifier


def _extract_post_fn(notifier, content="hello"):
    """Grab the ``_post`` closure from ``_send`` without starting a thread."""
    captured = []
    original_init = threading.Thread.__init__

    def capture_target(self_t, *args, **kwargs):
        original_init(self_t, *args, **kwargs)
        if kwargs.get("target"):
            captured.append(kwargs["target"])

    with patch.object(threading.Thread, "__init__", capture_target), \
         patch.object(threading.Thread, "start", lambda self: None):
        notifier._send(content)
    assert captured, "_send did not create a thread"
    return captured[0]


def _notifier():
    return DiscordNotifier("token", "channel", bot_name="test")


def test_send_is_dropped_when_the_notifier_is_already_wedged(monkeypatch):
    """Lock held by a stalled send -> drop, do not block for the stall."""
    monkeypatch.setattr(discord_notify, "LOCK_WAIT_S", 0.05)
    n = _notifier()
    post = _extract_post_fn(n)

    client = MagicMock()
    client.post = MagicMock(side_effect=AssertionError("must not POST"))
    monkeypatch.setattr(n, "_get_http", lambda: client)

    n._http_lock.acquire()                      # simulate the wedged thread
    try:
        post()                                  # must return, not hang
    finally:
        n._http_lock.release()

    client.post.assert_not_called()


def test_a_normal_send_still_takes_the_lock_and_posts(monkeypatch):
    """The bound must not break the ordinary path."""
    monkeypatch.setattr(discord_notify, "LOCK_WAIT_S", 5.0)
    n = _notifier()
    post = _extract_post_fn(n)

    resp = MagicMock(); resp.status_code = 200; resp.text = ""
    client = MagicMock(); client.post = MagicMock(return_value=resp)
    monkeypatch.setattr(n, "_get_http", lambda: client)

    post()
    assert client.post.call_count == 1
    assert not n._http_lock.locked(), "lock must be released after the send"


def test_lock_is_released_when_the_post_raises(monkeypatch):
    """A raising transport must not leave the notifier permanently wedged."""
    monkeypatch.setattr(discord_notify, "LOCK_WAIT_S", 5.0)
    n = _notifier()
    post = _extract_post_fn(n)

    client = MagicMock()
    client.post = MagicMock(side_effect=OSError("name resolution failed"))
    monkeypatch.setattr(n, "_get_http", lambda: client)

    post()                                      # _post swallows and logs
    assert not n._http_lock.locked()


def test_client_timeout_is_explicit_per_phase():
    """A bare ``timeout=10`` hides the connect budget; pin both."""
    n = _notifier()
    created = {}

    class _FakeTimeout:
        def __init__(self, total, connect=None):
            created["total"], created["connect"] = total, connect

    class _FakeClient:
        def __init__(self, timeout=None):
            created["client_timeout"] = timeout

    fake_httpx = MagicMock()
    fake_httpx.Timeout = _FakeTimeout
    fake_httpx.Client = _FakeClient
    with patch.dict("sys.modules", {"httpx": fake_httpx}):
        n._get_http()

    assert created["total"] == discord_notify.HTTP_TIMEOUT_S
    assert created["connect"] == discord_notify.CONNECT_TIMEOUT_S
    assert isinstance(created["client_timeout"], _FakeTimeout)
