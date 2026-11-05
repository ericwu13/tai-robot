"""A stalled Discord send must not multiply threads or lose later messages.

2026-11-04: every bot on one host had ``_post`` threads parked in
``socket.getaddrinfo`` for 90+ seconds while the shared ``httpx.Client``
was already configured with a 10 s timeout. httpx bounds connect / read /
write; it does not bound name resolution, which is an uninterruptible C
call. Every notification spawned its own thread, so each stalled send
queued more daemon threads behind the shared client.

The contract pinned here, all through the public ``_send`` path:

* one worker thread per notifier, however long a send stalls;
* messages behind a stall are delivered late and IN ORDER — never
  dropped (fill confirmations, loss-limit and force-close-failed alerts
  are among them);
* only a backlog of MAX_PENDING sheds anything, oldest first;
* a raising transport does not kill delivery of later messages.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from src.live import discord_notify
from src.live.discord_notify import DiscordNotifier


class _Resp:
    status_code = 200
    text = ""


class _GatedClient:
    """Fake httpx.Client whose POSTs block until ``gate`` is set."""

    def __init__(self, fail_first: bool = False):
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.attempted: list[str] = []
        self.posted: list[str] = []
        self._fail_first = fail_first
        self._lock = threading.Lock()

    def post(self, url, json=None, headers=None):
        content = json["content"]
        with self._lock:
            self.attempted.append(content)
            first = len(self.attempted) == 1
        self.entered.set()
        self.gate.wait(10)
        if first and self._fail_first:
            raise OSError("name resolution failed")
        with self._lock:
            self.posted.append(content)
        return _Resp()


def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _notifier(client):
    n = DiscordNotifier("token", "channel", bot_name="test")
    n._http = client
    return n


def test_a_stalled_send_does_not_multiply_threads():
    client = _GatedClient()
    n = _notifier(client)
    before = threading.active_count()

    for i in range(20):
        n._send(f"m{i}")
    assert client.entered.wait(5), "first send never reached the transport"
    time.sleep(0.2)                       # let any per-send threads spawn
    grown = threading.active_count() - before

    client.gate.set()
    assert _wait_for(lambda: len(client.posted) == 20)
    assert grown <= 1, f"{grown} threads for one stalled notifier"


def test_messages_behind_a_stall_arrive_in_order_not_dropped(monkeypatch):
    # The pre-fix design dropped anything that waited longer than its lock
    # wait; model a stall longer than that wait (no-op on the new design).
    monkeypatch.setattr(discord_notify, "LOCK_WAIT_S", 0.05, raising=False)
    client = _GatedClient()
    n = _notifier(client)

    for i in range(10):
        n._send(f"m{i}")
    assert client.entered.wait(5)
    time.sleep(0.3)                       # the stall
    client.gate.set()

    assert _wait_for(lambda: len(client.posted) == 10), client.posted
    assert client.posted == [f"m{i}" for i in range(10)]


def test_backlog_is_bounded_and_sheds_the_oldest(monkeypatch):
    monkeypatch.setattr(discord_notify, "MAX_PENDING", 5, raising=False)
    client = _GatedClient()
    n = _notifier(client)

    n._send("m0")
    assert client.entered.wait(5)         # m0 is in flight, stalled
    for i in range(1, 11):
        n._send(f"m{i}")
    client.gate.set()

    assert _wait_for(lambda: len(client.posted) == 6), client.posted
    time.sleep(0.1)
    assert client.posted == ["m0", "m6", "m7", "m8", "m9", "m10"]


def test_a_raising_post_does_not_stop_later_messages():
    client = _GatedClient(fail_first=True)
    client.gate.set()
    n = _notifier(client)

    n._send("a")
    n._send("b")

    assert _wait_for(lambda: client.posted == ["b"]), client.attempted
    assert client.attempted == ["a", "b"]


def test_client_timeout_is_explicit_per_phase():
    """A bare ``timeout=10`` hides the connect budget; pin both."""
    n = DiscordNotifier("token", "channel", bot_name="test")
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
