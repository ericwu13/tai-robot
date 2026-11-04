"""Issue #230 — reply-disconnect arity, poller gates, and hang evidence.

Each test in this module fails on v2.19.11 (00e67b4) and passes once
the fix on this branch is in place. COM is not loaded; the handlers
are the Python sink methods.
"""

from __future__ import annotations

import queue

import run_backtest as rb


def _drain() -> list:
    found = []
    while True:
        try:
            found.append(rb._ui_queue.get_nowait())
        except queue.Empty:
            return found


# Vendor COM arguments (not counting self). Quote and order arities are
# the ones this process has been receiving; the reply disconnect arity
# is the vendor's (bstrUserID, nErrorCode). One extra positional argument
# stands in for a future IDL drift.
_REPLY_CALLS = (
    ("OnReplyMessage", ("U", "hello"), -1),
    ("OnConnect", ("U", 0), None),
    ("OnComplete", ("U",), None),
    ("OnSolaceReplyDisconnect", ("U", 3033), None),
    ("OnSolaceReplyConnection", ("U", 0), None),
    ("OnNewData", ("U", "row"), None),
)

_QUOTE_CALLS = (
    ("OnNotifyStockList", (2, "data")),
    ("OnConnection", (3003, 0)),
    ("OnNotifyKLineData", ("TX00", "bar")),
    ("OnKLineComplete", (0,)),
    ("OnNotifyQuoteLONG", (2, 1)),
    ("OnNotifyHistoryTicksLONG", (2, 1, 0, 20261103, 150000, 0, 1, 2, 3, 4, 0)),
    ("OnNotifyTicksLONG", (2, 1, 0, 20261103, 150000, 0, 1, 2, 3, 4, 0)),
    ("OnNotifyBest5LONG", (2, 1)),
    ("OnNotifyServerTime", (15, 0, 0, 1)),
)

_ORDER_CALLS = (
    ("OnAccount", ("U", "TF,0,,1")),
    ("OnAsyncOrder", (1, 0, "ok")),
    ("OnOpenInterest", ("row",)),
    ("OnFutureRights", ("rights",)),
)


def test_reply_handlers_accept_com_arity():
    """Vendor arity and vendor arity + 1 must not raise inside the sink."""
    rb._app = None
    _drain()
    reply = rb.SKReplyLibEvent()
    for name, args, expected in _REPLY_CALLS:
        method = getattr(reply, name)
        assert method(*args) == expected
        assert method(*args, "drift") == expected
    quote = rb.SKQuoteLibEvents()
    for name, args in _QUOTE_CALLS:
        method = getattr(quote, name)
        method(*args)
        method(*args, "drift")
    order = rb.SKOrderLibEvents()
    for name, args in _ORDER_CALLS:
        method = getattr(order, name)
        method(*args)
        method(*args, "drift")
    queued = _drain()
    assert ("link", ("reply_disconnect",)) in queued
    assert any(
        item[0] == "log" and "code=3033" in str(item[1])
        for item in queued
    )
    assert ("reply_seen", "OnNewData") in queued


def test_poll_reply_link_reschedules_when_poll_raises():
    """A raising poll still re-arms the entry gate and the backoff timer."""
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    gated = []
    scheduled = []

    class _Poller:
        stopped = False

        def poll(self, now):
            raise RuntimeError("poll blew")

        def pending_delay_s(self, now):
            return 5.0

    class _Root:
        def after(self, ms, fn, *args):
            scheduled.append(ms)
            return "tok"

        def after_cancel(self, token):
            return None

    app._reply_poller = _Poller()
    app._reply_backoff_token = None
    app.root = _Root()
    app._apply_link_gate = lambda: gated.append("gate")
    app._poll_reply_link("backoff")
    assert gated == ["gate"]
    assert scheduled == [5000]
