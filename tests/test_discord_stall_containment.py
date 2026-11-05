"""A stalled Discord send must not grow a thread per message.

On 2026-11-04 every bot on the live host had ``_post`` threads parked in
``socket.getaddrinfo`` for 90+ seconds. httpx bounds connect / read /
write; it does not bound name resolution. The containment is one worker,
a bounded queue, and a small priority backlog.

Tests 2 and 3 (``test_a_normal_send_still_takes_the_lock_and_posts`` and
``test_lock_is_released_when_the_post_raises``) used to fail on master
only because ``LOCK_WAIT_S`` was missing. The lock was already released
there. They now fail on that code because each send starts its own
thread.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from src.live import discord_notify
from src.live.discord_notify import DiscordNotifier


def _notifier():
    return DiscordNotifier("token", "channel", bot_name="test")


def _response(status, text=""):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    return resp


def _client(post):
    client = MagicMock()
    client.post = post
    return client


def _wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def _run_bounded(fn, timeout=1.0):
    """Run ``fn`` off to the side. Fail if it is still going after ``timeout``.

    A drop that falls through into the wedged POST blocks the caller on
    the HTTP lock. That must fail the test instead of hanging it.
    """
    errors = []

    def wrap():
        try:
            fn()
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=wrap, name="burst-helper")
    worker.start()
    worker.join(timeout)
    assert not worker.is_alive(), "enqueue blocked the caller"
    if errors:
        raise errors[0]


@contextmanager
def _spy_threads():
    born = []
    orig = threading.Thread.__init__

    def spy(self, *args, **kwargs):
        orig(self, *args, **kwargs)
        born.append(self)

    with patch.object(threading.Thread, "__init__", spy):
        yield born


class _Stall:
    """First POST blocks until :meth:`free`. Later POSTs return 200."""

    def __init__(self, monkeypatch):
        self.n = _notifier()
        self.posted = []
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = 0

        def post(url, json=None, headers=None):
            self.posted.append(json["content"])
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                self.release.wait()
            return _response(200)

        monkeypatch.setattr(self.n, "_get_http", lambda: _client(post))

    def arm(self):
        self.n._send("WEDGE")
        assert self.started.wait(2), "worker did not enter the wedged send"
        return self

    def free(self):
        self.release.set()


def _summaries(posted):
    return [body for body in posted if "Discord was stalled:" in body]


def _drained(n, posted):
    """Queue, backlog, and the one summary have all finished."""
    return (
        n._queue.unfinished_tasks == 0
        and n._queue.qsize() == 0
        and len(n._priority_backlog) == 0
        and bool(_summaries(posted))
    )


def test_send_is_dropped_when_the_notifier_is_already_wedged(caplog, monkeypatch):
    """A full queue drops routine sends, logs that at warning, and posts
    one catch-up line after the stall clears.

    The dropped body is never POSTed, and the drop does not fall through
    into the 429 retry (that ``return`` -> ``continue`` mutant logged
    "429 rate-limited" instead of leaving the send alone).
    """
    stall = _Stall(monkeypatch)
    extra = 30
    try:
        stall.arm()
        with _spy_threads() as born:
            with caplog.at_level(logging.WARNING, logger="src.live.discord_notify"):
                def burst():
                    for i in range(extra):
                        stall.n._send(f"msg-{i}")
                _run_bounded(burst)
        # Master starts one thread per send. This branch starts none.
        assert len(born) <= 1
        drop_logs = [
            rec for rec in caplog.records
            if "Discord send dropped" in rec.getMessage()
        ]
        assert drop_logs, "a dropped send must be logged"
        assert {rec.levelno for rec in drop_logs} == {logging.WARNING}
        assert not any("429" in rec.getMessage() for rec in caplog.records)
        stall.free()
        _wait_until(lambda: _drained(stall.n, stall.posted))
        summaries = _summaries(stall.posted)
        assert len(summaries) == 1
        dropped = extra - discord_notify.SEND_QUEUE_MAX
        assert (
            f"{dropped} messages dropped, 0 delivered late" in summaries[0]
        )
        bodies = [body for body in stall.posted if body not in summaries]
        assert "msg-0" in bodies
        assert f"msg-{discord_notify.SEND_QUEUE_MAX - 1}" in bodies
        assert f"msg-{discord_notify.SEND_QUEUE_MAX}" not in bodies
        assert "msg-29" not in bodies
    finally:
        stall.free()


def test_a_normal_send_still_takes_the_lock_and_posts(monkeypatch):
    """Both sends post, the lock is free afterwards, and one worker did both.

    On master the lock release was already true and each ``_send`` started
    its own thread. The thread count is what fails there.
    """
    n = _notifier()
    posted = []

    def post(url, json=None, headers=None):
        posted.append(json["content"])
        return _response(200)

    monkeypatch.setattr(n, "_get_http", lambda: _client(post))
    with _spy_threads() as born:
        n._send("one")
        n._send("two")
        _wait_until(
            lambda: len(posted) >= 2 and not n._http_lock.locked())
    assert len(born) == 1
    assert posted == ["one", "two"]
    assert not n._http_lock.locked()


def test_lock_is_released_when_the_post_raises(monkeypatch):
    """A raising transport releases the lock and the next send still posts.

    Both happen on the same worker. Master already released the lock, and
    the follow-up send started a second thread.
    """
    n = _notifier()
    calls = []

    def post(url, json=None, headers=None):
        calls.append(json["content"])
        if len(calls) == 1:
            raise OSError("name resolution failed")
        return _response(200)

    monkeypatch.setattr(n, "_get_http", lambda: _client(post))
    with _spy_threads() as born:
        n._send("boom")
        n._send("next")
        _wait_until(lambda: len(calls) >= 2 and not n._http_lock.locked())
    assert len(born) == 1
    assert calls == ["boom", "next"]
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


def test_wedged_send_bounds_threads_and_queue(monkeypatch):
    """Many sends behind one wedged POST stay on one worker and one queue."""
    stall = _Stall(monkeypatch)
    burst_n = 40
    try:
        with _spy_threads() as born:
            stall.arm()
            def burst():
                for i in range(burst_n):
                    stall.n._send(f"bulk-{i}")
            _run_bounded(burst)
        workers = [t for t in born if t.name == "discord-notify"]
        # The burst helper is the other thread. Send work did not add more.
        assert len(workers) == 1
        assert len(born) == 2
        assert stall.n._queue.maxsize == discord_notify.SEND_QUEUE_MAX
        assert stall.n._queue.qsize() == discord_notify.SEND_QUEUE_MAX
        assert stall.n._queue.maxsize < burst_n
    finally:
        stall.free()


def test_priority_messages_survive_a_full_queue_and_flush_in_order(monkeypatch):
    """HUNG, order, trade, and exit stay queued when routine sends are dropped.

    They flush in the order they were parked, then one catch-up summary.
    """
    stall = _Stall(monkeypatch)
    needles = [
        "委託送出 Order Sent",
        "委託失敗 Order Failed",
        "成交確認 Fill Confirmed",
        "出場成交價未確認",
        "FORCE CLOSE FAILED",
        "成交超時 Fill Timeout",
        "成交價更正 Fill Price Correction",
        "P1 HUNG probe",
    ]
    try:
        stall.arm()
        for i in range(discord_notify.SEND_QUEUE_MAX):
            stall.n._send(f"queued-{i}")
        stall.n._send("routine-drop")
        stall.n.order_sent("Buy", "TXF", "市價", 100, "ord-1")
        stall.n.order_failed("Buy", "TXF", 1, "rejected")
        stall.n.fill_confirmed("exit", "100", strategy="S")
        stall.n.exit_fill_unconfirmed("100", strategy="S")
        stall.n.force_close_failed("TXF", 3, "still open")
        stall.n.fill_timeout_downgrade("exit", 10)
        stall.n.fill_price_correction("exit", 101, 100, strategy="S")
        stall.n.notify("P1 HUNG probe", priority=True)
        assert stall.n._dropped == 1
        assert len(stall.n._priority_backlog) == len(needles)
        assert stall.n._priority_overflow == 0
        stall.free()
        _wait_until(lambda: _drained(stall.n, stall.posted))
        summaries = _summaries(stall.posted)
        assert len(summaries) == 1
        assert (
            f"1 messages dropped, {len(needles)} delivered late"
            in summaries[0]
        )
        assert "routine-drop" not in stall.posted
        assert "queued-0" in stall.posted

        def at(needle):
            for i, body in enumerate(stall.posted):
                if needle in body:
                    return i
            raise AssertionError(f"{needle} was not delivered")

        order = [at(needle) for needle in needles]
        assert len(set(order)) == len(order)
        assert order == sorted(order)
        assert order[-1] < at("Discord was stalled:")
    finally:
        stall.free()


def test_priority_backlog_overflow_is_counted(monkeypatch):
    """Past the backlog cap, priority sends are counted and not delivered."""
    stall = _Stall(monkeypatch)
    queued = discord_notify.SEND_QUEUE_MAX
    parked = discord_notify.PRIORITY_BACKLOG_MAX
    overflow = 2
    total = queued + parked + overflow
    try:
        stall.arm()
        for i in range(total):
            stall.n._send(f"prio-{i}", priority=True)
        assert len(stall.n._priority_backlog) == parked
        assert stall.n._priority_overflow == overflow
        assert stall.n._queue.qsize() == queued
        stall.free()
        _wait_until(lambda: _drained(stall.n, stall.posted))
        summaries = _summaries(stall.posted)
        assert len(summaries) == 1
        assert (
            f"0 messages dropped, {parked} delivered late, "
            f"{overflow} priority overflow"
        ) in summaries[0]
        assert "prio-0" in stall.posted
        # Backlog keeps arrival order and jumps ahead of the queued tail.
        backlog_at = [
            next(i for i, body in enumerate(stall.posted) if body == f"prio-{n}")
            for n in range(queued, queued + parked)
        ]
        assert backlog_at == sorted(backlog_at)
        summary_at = next(
            i for i, body in enumerate(stall.posted)
            if "Discord was stalled:" in body)
        assert backlog_at[-1] < summary_at
        assert f"prio-{total - 1}" not in stall.posted
    finally:
        stall.free()


def test_429_retry_is_not_dropped_when_the_queue_fills(monkeypatch):
    """A 429 backoff stays inside the worker. Filling the queue during the
    sleep must not eat the retry or skip the 1s delay.
    """
    n = _notifier()
    posted = []

    def post(url, json=None, headers=None):
        body = json["content"]
        seen = posted.count("NEED-RETRY")
        posted.append(body)
        if body == "NEED-RETRY" and seen == 0:
            return _response(429, "slow down")
        return _response(200)

    monkeypatch.setattr(n, "_get_http", lambda: _client(post))
    sleeps = []
    real_sleep = time.sleep

    def sleeper(delay):
        # Only the worker's backoff (1s, 2s, 4s) is the event under test.
        # The wait loop's short sleeps must still pause.
        if delay >= 1 and not sleeps:
            sleeps.append(delay)
            for i in range(discord_notify.SEND_QUEUE_MAX + 4):
                n._send(f"flood-{i}")
            return
        real_sleep(delay)

    monkeypatch.setattr(discord_notify.time, "sleep", sleeper)
    n._send("NEED-RETRY")
    _wait_until(lambda: _drained(n, posted) and posted.count("NEED-RETRY") >= 2)
    assert sleeps == [1.0]
    assert posted[0] == "NEED-RETRY"
    assert posted[1] == "NEED-RETRY"
    assert posted.count("NEED-RETRY") == 2
    summaries = _summaries(posted)
    assert len(summaries) == 1
    assert "4 messages dropped, 0 delivered late" in summaries[0]
    assert "429 rate-limited" not in summaries[0]
