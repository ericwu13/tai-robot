"""A stalled Discord send must not grow a thread per message.

On 2026-11-04 every bot on the live host had ``_post`` threads parked in
``socket.getaddrinfo`` for 90+ seconds. httpx bounds connect / read /
write; it does not bound name resolution. The containment is one worker,
a bounded queue of 200, and a priority backlog of the same size.
A stall that queues fewer than that loses nothing.

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

        client = _client(post)
        # Keep the mock on the instance. A worker still running after
        # the monkeypatch is undone must not open a real connection.
        self.n._http = client
        monkeypatch.setattr(self.n, "_get_http", lambda: client)

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
    extra = discord_notify.SEND_QUEUE_MAX + 5
    try:
        stall.arm()
        with _spy_threads() as born:
            with caplog.at_level(logging.WARNING, logger="src.live.discord_notify"):
                def burst():
                    for i in range(extra):
                        stall.n._send(f"msg-{i}")
                _run_bounded(burst, timeout=3.0)
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
        assert f"msg-{extra - 1}" not in bodies
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

    client = _client(post)
    n._http = client
    monkeypatch.setattr(n, "_get_http", lambda: client)
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

    client = _client(post)
    n._http = client
    monkeypatch.setattr(n, "_get_http", lambda: client)
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
    burst_n = discord_notify.SEND_QUEUE_MAX + 15
    try:
        with _spy_threads() as born:
            stall.arm()
            def burst():
                for i in range(burst_n):
                    stall.n._send(f"bulk-{i}")
            _run_bounded(burst, timeout=3.0)
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
    """Past the priority cap, further priority sends are counted and not delivered.

    Priority never occupies the routine queue, so the cap is the deque
    alone.
    """
    stall = _Stall(monkeypatch)
    parked = discord_notify.PRIORITY_BACKLOG_MAX
    overflow = 2
    total = parked + overflow
    try:
        stall.arm()
        for i in range(total):
            stall.n._send(f"prio-{i}", priority=True)
        assert len(stall.n._priority_backlog) == parked
        assert stall.n._priority_overflow == overflow
        assert stall.n._queue.qsize() == 0
        stall.free()
        _wait_until(lambda: _drained(stall.n, stall.posted))
        summaries = _summaries(stall.posted)
        assert len(summaries) == 1
        assert (
            f"0 messages dropped, {parked} delivered late, "
            f"{overflow} priority overflow"
        ) in summaries[0]
        backlog_at = [
            next(i for i, body in enumerate(stall.posted) if body == f"prio-{n}")
            for n in range(parked)
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

    client = _client(post)
    n._http = client
    monkeypatch.setattr(n, "_get_http", lambda: client)
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


def test_risk_notices_survive_a_full_queue(monkeypatch):
    """Loss limit, refused mode, mode change, idle entries, news flatten,
    and a connection alert stay in the backlog. A regime recommendation
    does not.
    """
    stall = _Stall(monkeypatch)
    needles = [
        "Daily Loss Limit",
        "Mode Switch Refused",
        "Trading Mode Switched",
        "Signals Suppressed",
        "flattened + entries suppressed",
        "REPLY CHANNEL SILENT",
    ]
    try:
        stall.arm()
        for i in range(discord_notify.SEND_QUEUE_MAX):
            stall.n._send(f"queued-{i}")
        stall.n.notify_regime("regime advice only")
        stall.n.daily_loss_limit(-5000, 10000)
        stall.n.mode_switch_refused("auto", "open position")
        stall.n.mode_switched("paper", "auto")
        stall.n.regime_idle_warning()
        stall.n.notify_regime("flattened + entries suppressed", priority=True)
        stall.n.notify("REPLY CHANNEL SILENT", priority=True)
        assert stall.n._dropped == 1
        assert len(stall.n._priority_backlog) == len(needles)
        stall.free()
        _wait_until(lambda: _drained(stall.n, stall.posted))
        assert "regime advice only" not in stall.posted
        summaries = _summaries(stall.posted)
        assert len(summaries) == 1
        assert (
            f"1 messages dropped, {len(needles)} delivered late"
            in summaries[0]
        )

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


def test_failed_stall_summary_keeps_the_counts(monkeypatch):
    """A summary that Discord rejects must be retried with the same counts."""
    stall = _Stall(monkeypatch)
    summary_codes = []

    def post(url, json=None, headers=None):
        body = json["content"]
        stall.posted.append(body)
        stall.calls += 1
        if stall.calls == 1:
            stall.started.set()
            stall.release.wait()
            return _response(200)
        if "Discord was stalled:" in body:
            code = 500 if not summary_codes else 200
            summary_codes.append(code)
            return _response(code, "no" if code == 500 else "")
        return _response(200)

    # Replace the wedge client's post. _Stall already installed one.
    client = _client(post)
    stall.n._http = client
    monkeypatch.setattr(stall.n, "_get_http", lambda: client)
    extra_dropped = 3
    try:
        stall.arm()
        for i in range(discord_notify.SEND_QUEUE_MAX):
            stall.n._send(f"queued-{i}")
        for i in range(extra_dropped):
            stall.n._send(f"drop-{i}")
        assert stall.n._dropped == extra_dropped
        stall.free()
        _wait_until(lambda: summary_codes == [500, 200])
        _wait_until(lambda: _drained(stall.n, stall.posted))
        assert summary_codes == [500, 200]
        summaries = _summaries(stall.posted)
        assert len(summaries) == 2
        phrase = f"{extra_dropped} messages dropped, 0 delivered late"
        assert all(phrase in body for body in summaries)
        assert stall.n._dropped == 0
    finally:
        stall.free()


def test_raising_send_does_not_kill_the_worker(monkeypatch):
    """One raised delivery is retried once, then the next message still goes.

    The worker that took the first item is the one that delivers the next.
    A dead thread would leave the already-queued message sitting there.
    """
    n = _notifier()
    calls = []
    posted = []
    started = threading.Event()
    release = threading.Event()

    def fake_deliver(content, target):
        calls.append(content)
        if content == "boom":
            if not started.is_set():
                started.set()
                release.wait()
            raise RuntimeError("boom")
        posted.append(content)
        return True

    n._http = _client(lambda *a, **k: _response(200))
    monkeypatch.setattr(n, "_deliver", fake_deliver)
    try:
        n._send("boom")
        assert started.wait(2), "worker did not take the raising send"
        worker = n._worker
        n._send("ok-msg")
        release.set()
        _wait_until(
            lambda: "ok-msg" in posted
            and calls.count("boom") >= 1 + discord_notify.WORKER_SEND_RETRIES)
        assert calls.count("boom") == 1 + discord_notify.WORKER_SEND_RETRIES
        assert "ok-msg" in posted
        assert n._worker is worker
        assert worker.is_alive()
    finally:
        release.set()


def test_link_settlement_and_news_call_sites_are_priority(monkeypatch):
    """The connection, settlement, and news-breaker call sites opt in."""
    import run_backtest as rb

    notes = []

    class _Discord:
        enabled = True

        def notify(self, message, *, priority=False):
            notes.append(("notify", message, priority))

        def notify_regime(self, message, *, priority=False):
            notes.append(("regime", message, priority))

    monkeypatch.setattr(rb, "_discord", _Discord())
    monkeypatch.setattr(rb, "_log", lambda *a, **k: None)
    monkeypatch.setattr(rb, "_log_reply_error", lambda *a, **k: None)

    rb._notify_reply_silent("REPLY CHANNEL SILENT")
    rb._notify_news("flattened + entries suppressed")

    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.set_status = lambda *a, **k: None
    app._set_conn_dot = lambda *a, **k: None
    app._live_log_msg = lambda *a, **k: None
    app._on_link_alert("DEGRADED reply down")

    import src.market_data.holidays as holidays
    app._live_runner = type("R", (), {"symbol": "TXF00"})()
    monkeypatch.setattr(holidays, "is_settlement_day", lambda now: True)
    monkeypatch.setattr(holidays, "is_front_month_contract", lambda sym, now: True)
    monkeypatch.setattr(rb, "resolve_order_symbol", lambda symbol: symbol)
    app._notify_if_settlement_day()

    assert ("notify", "REPLY CHANNEL SILENT", True) in [
        (kind, msg, flag) for kind, msg, flag in notes
    ]
    assert notes[0] == ("notify", "REPLY CHANNEL SILENT", True)
    assert notes[1] == ("regime", "flattened + entries suppressed", True)
    assert notes[2][0] == "notify" and notes[2][2] is True
    assert "DEGRADED reply down" in notes[2][1]
    assert notes[3][2] is True
    assert "SETTLEMENT DAY" in notes[3][1]
    assert "entries blocked" in notes[3][1]


def test_equity_hold_alerts_survive_a_full_queue(monkeypatch):
    """The immediate entries-held alert and the 90s still-held note are
    priority. Removing either flag drops that body while the queue is full.
    """
    import run_backtest as rb

    stall = _Stall(monkeypatch)
    previous = rb._discord
    needles = (
        "[CONN] entries held: equity never refreshed",
        "Equity reply not received",
    )
    try:
        stall.arm()
        for i in range(discord_notify.SEND_QUEUE_MAX):
            stall.n._send(f"queued-{i}")
        monkeypatch.setattr(rb, "_discord", stall.n)
        monkeypatch.setattr(rb, "_log", lambda *a, **k: None)
        monkeypatch.setattr(rb, "_log_debug", lambda *a, **k: None)
        app = rb.BacktestApp.__new__(rb.BacktestApp)
        app._equity_refresh_owed = True
        app._trading_mode = "auto"
        app._equity_refresh_held_alerted = False
        app._live_log_msg = lambda *a, **k: None
        app._alert_equity_refresh_held("no rights")
        app._equity_refresh_held_alerted = False
        app._equity_hold_since = time.monotonic() - (
            rb.EQUITY_ENTRY_HOLD_TIMEOUT_S + 5)
        app._note_equity_hold_timeout()
        assert stall.n._dropped == 0
        assert len(stall.n._priority_backlog) == len(needles)
        stall.free()
        _wait_until(lambda: _drained(stall.n, stall.posted))
        for needle in needles:
            assert any(needle in body for body in stall.posted), needle
        summaries = _summaries(stall.posted)
        assert len(summaries) == 1
        assert (
            f"0 messages dropped, {len(needles)} delivered late"
            in summaries[0]
        )
    finally:
        stall.free()
        rb._discord = previous


def test_long_stall_keeps_fifty_mixed_messages_in_order(monkeypatch):
    """About 50 mixed messages during a long stall are all delivered.

    Priority stays ahead of routine, and each class stays in its own
    FIFO order. Fifty is more than the old cap of 8 and less than 200.
    A cap of 8 drops the routine tail, so this test fails.
    """
    stall = _Stall(monkeypatch)
    labels = [f"m-{i}" for i in range(50)]
    priority = [label for i, label in enumerate(labels) if i % 5 == 0]
    routine = [label for i, label in enumerate(labels) if i % 5 != 0]
    try:
        stall.arm()
        for i, label in enumerate(labels):
            stall.n._send(label, priority=(i % 5 == 0))
        assert stall.n._dropped == 0
        assert stall.n._priority_overflow == 0
        assert [item[0] for item in stall.n._priority_backlog] == priority
        assert stall.n._queue.qsize() == len(routine)
        stall.free()
        _wait_until(lambda: _drained(stall.n, stall.posted))
        delivered = [
            body for body in stall.posted if "Discord was stalled:" not in body
        ]
        assert delivered == ["WEDGE", *priority, *routine]
        assert stall.n._dropped == 0
        summaries = _summaries(stall.posted)
        assert len(summaries) == 1
        assert (
            f"0 messages dropped, {len(priority)} delivered late"
            in summaries[0]
        )
    finally:
        stall.free()


def test_overflowing_200_routine_messages_sheds_only_those(caplog, monkeypatch):
    """Past the routine cap, only the extra routine messages are dropped."""
    stall = _Stall(monkeypatch)
    extra = 7
    cap = discord_notify.SEND_QUEUE_MAX
    try:
        stall.arm()
        with caplog.at_level(logging.WARNING, logger="src.live.discord_notify"):
            for i in range(cap + extra):
                stall.n._send(f"routine-{i}")
        assert stall.n._dropped == extra
        assert stall.n._priority_overflow == 0
        assert len(stall.n._priority_backlog) == 0
        assert stall.n._queue.qsize() == cap
        drop_logs = [
            rec for rec in caplog.records
            if "Discord send dropped" in rec.getMessage()
        ]
        assert len(drop_logs) == extra
        assert {rec.levelno for rec in drop_logs} == {logging.WARNING}
        stall.free()
        _wait_until(lambda: _drained(stall.n, stall.posted))
        assert f"routine-{cap - 1}" in stall.posted
        assert f"routine-{cap}" not in stall.posted
        assert f"routine-{cap + extra - 1}" not in stall.posted
        summaries = _summaries(stall.posted)
        assert len(summaries) == 1
        assert f"{extra} messages dropped, 0 delivered late" in summaries[0]
    finally:
        stall.free()


def test_priority_survives_a_routine_overflow(monkeypatch):
    """A full routine queue sheds the next routine send and keeps priority.

    Swapping the two branches would drop the priority line and deliver
    the extra routine one instead.
    """
    stall = _Stall(monkeypatch)
    extra = 4
    cap = discord_notify.SEND_QUEUE_MAX
    try:
        stall.arm()
        for i in range(cap + extra):
            stall.n._send(f"routine-{i}")
        stall.n._send("kept-priority", priority=True)
        stall.n._send("kept-hung", priority=True)
        assert stall.n._dropped == extra
        assert stall.n._priority_overflow == 0
        assert [item[0] for item in stall.n._priority_backlog] == [
            "kept-priority", "kept-hung",
        ]
        stall.free()
        _wait_until(lambda: _drained(stall.n, stall.posted))
        assert "kept-priority" in stall.posted
        assert "kept-hung" in stall.posted
        assert f"routine-{cap}" not in stall.posted
        assert f"routine-0" in stall.posted
        summaries = _summaries(stall.posted)
        assert len(summaries) == 1
        assert f"{extra} messages dropped, 2 delivered late" in summaries[0]
        priority_at = stall.posted.index("kept-priority")
        hung_at = stall.posted.index("kept-hung")
        summary_at = next(
            i for i, body in enumerate(stall.posted)
            if "Discord was stalled:" in body)
        assert priority_at < hung_at < summary_at
    finally:
        stall.free()


def test_daily_loss_limit_jumps_ahead_of_queued_routine(monkeypatch):
    """A loss-limit alert queued behind routine traffic is the next send.

    The in-flight POST finishes first. The loss-limit message is the
    one after it, not after the 50 routine messages. Putting that alert
    on the routine queue fails this.
    """
    stall = _Stall(monkeypatch)
    try:
        stall.arm()
        for i in range(50):
            stall.n._send(f"routine-{i}")
        stall.n.daily_loss_limit(-12000, 10000)
        assert stall.n._queue.qsize() == 50
        assert len(stall.n._priority_backlog) == 1
        assert "Daily Loss Limit" in stall.n._priority_backlog[0][0]
        stall.free()
        _wait_until(lambda: (
            stall.n._queue.unfinished_tasks == 0
            and stall.n._queue.qsize() == 0
            and len(stall.n._priority_backlog) == 0
            and any(body == "routine-49" for body in stall.posted)
        ))
        loss = [
            i for i, body in enumerate(stall.posted)
            if "Daily Loss Limit" in body
        ]
        assert loss == [1]
        assert stall.posted[0] == "WEDGE"
        routines = [body for body in stall.posted if body.startswith("routine-")]
        assert routines == [f"routine-{i}" for i in range(50)]
        assert stall.posted.index("routine-0") > loss[0]
    finally:
        stall.free()
