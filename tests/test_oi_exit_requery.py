"""A confirmed exit leaves the OpenInterest book flat, not a phantom row (#204).

Exchange sequence for a normal exit, as the bot sees it:

1. the 30 s account poll opens a query generation; OnOpenInterest
   credits the LONG row, then ``##``;
2. the exit order goes out and the fill poll queries every 3 s. It does
   NOT open a generation (``_poll_fill_confirmation``), so its replies are
   tagged with the account poll's generation;
3. the fill poll's reply is ``001`` (no data) then ``##``.

#204 reads a ``001`` after a credited row in the same generation as a
conflict (a late reply that disagrees): the row stays and the book is
UNKNOWN. That rule protects a deploy, but here the same ``001`` also
confirms the exit, so the book kept a LONG the account no longer holds
until the next account poll (stale side for ``_manual_close``, stale
``avg_cost`` for an entry in that window). A confirmed exit now starts a
fresh query, so the flat answer is credited to a generation of its own.
"""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live.account_monitor import AccountMonitor
from tests.test_issue_168_restore_fidelity import _resume_guard_app

ROW_LONG = "TF,ACCT,TXFD0,B,1,0,22500.0,15,2,ID"     # TX00 order symbol TXFD0
PREFIX = rb.SYMBOL_CONFIG["TX00"]["order_symbol"][:2]


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _drain() -> None:
    while True:
        try:
            rb._ui_queue.get_nowait()
        except queue.Empty:
            return


@pytest.fixture
def app(monkeypatch):
    app = _resume_guard_app(position_size=1)
    app._account_monitor = AccountMonitor()
    app.login_user_var = _Var("user")
    app.real_pos_var = _Var("")
    app._update_real_account_display = lambda: None
    app._log_order_decision = lambda action, reason: None
    app._active_strategy_name = lambda: "S"
    app._real_exit_price_from_broker = lambda: 22480
    app._live_runner.broker.trades = []
    app._live_runner.broker.real_entry_price = 0
    app._live_runner.broker.try_set_real_entry_price = lambda price, **kw: True
    app.queries = []

    def _call_com(name, fn, *args, **kwargs):
        app.queries.append((name, app._account_monitor.oi_query_generation))
        return 0

    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(GetOpenInterestGW=lambda *a: 0))
    monkeypatch.setattr(rb, "call_com", _call_com)
    monkeypatch.setattr(rb, "_discord", None)
    previous = rb._app
    rb._app = app
    _drain()
    yield app
    rb._app = previous
    _drain()


def _oi(app, *rows) -> None:
    """Deliver rows through the real COM callback and UI-queue dispatch."""
    for row in rows:
        rb.SKOrderLibEvents().OnOpenInterest(row)
    rb.BacktestApp._process_ui_queue(app)


def _hold_long(app) -> None:
    app._account_monitor.begin_oi_query()             # 30 s account poll
    _oi(app, ROW_LONG, "##")
    assert app._account_monitor.get_signed_position(PREFIX) == 1


def test_exit_confirmed_by_fill_poll_leaves_a_flat_book(app):
    _hold_long(app)
    app._trading_guard.on_fill_pending("exit")
    app._start_fill_polling("exit")
    assert app._fill_poller.active

    _oi(app, "001,查無資料", "##")                    # fill poll: no position
    assert app._trading_guard.fill_pending is False   # exit confirmed

    monitor = app._account_monitor
    # The stale row is gone the moment the exit is confirmed ...
    assert monitor.get_signed_position(PREFIX) == 0
    assert monitor.positions == []
    # ... and a fresh query of its own went out for the flat answer.
    assert [q[0] for q in app.queries] == ["GetOpenInterestGW"]
    assert app.queries[0][1] == monitor.oi_query_generation

    _oi(app, "001,查無資料", "##")                    # that query's reply
    assert monitor.oi_snapshot_received is True
    assert monitor.positions == []
    assert app.real_pos_var.get() == "Flat (無持倉)"


def test_exit_confirmed_by_deal_row_also_requeries(app):
    """#103: the OnNewData deal row usually beats OpenInterest. The
    account poll's row is just as stale on that path."""
    _hold_long(app)
    app._trading_guard.on_fill_pending("exit")
    app._start_fill_polling("exit")
    assert app._fill_poller.on_deal_confirmed("exit") is not None
    app._on_fill_confirmed()

    monitor = app._account_monitor
    assert monitor.positions == []
    assert [q[0] for q in app.queries] == ["GetOpenInterestGW"]
    _oi(app, "001,查無資料", "##")
    assert monitor.oi_snapshot_received is True
    assert monitor.get_signed_position(PREFIX) == 0


def test_entry_confirm_keeps_the_book_and_does_not_requery(app):
    """An entry's fill price falls back to the OI row's avg_cost, so the
    row must stay; nothing about an entry makes the snapshot stale."""
    app._live_runner.broker.position_size = 0
    app._account_monitor.begin_oi_query()
    _oi(app, "001,查無資料", "##")                    # flat before the entry
    app._trading_guard.on_fill_pending("entry")
    app._get_signed_position = lambda: app._account_monitor.get_signed_position(PREFIX)
    app._start_fill_polling("entry")
    _oi(app, ROW_LONG, "##")                          # fill poll sees the LONG
    assert app._trading_guard.fill_pending is False
    assert app._account_monitor.get_signed_position(PREFIX) == 1
    assert app.queries == []


def test_late_001_after_a_credited_row_still_keeps_a_deploy_unknown():
    """#204's deploy protection is unchanged: within one query, a 001 that
    disagrees with a credited row is a conflict, not a flat book."""
    monitor = AccountMonitor()
    monitor.begin_oi_query()
    assert monitor.apply_open_interest(ROW_LONG) == "position"
    assert monitor.apply_open_interest("001,查無資料") == "ignored"
    assert monitor.apply_open_interest("##") == "ignored"
    assert monitor.oi_snapshot_received is False
    assert monitor.get_signed_position(PREFIX) == 1
