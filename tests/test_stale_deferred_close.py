"""Stale deferred-close replay after an entry timeout (follow-up to #171).

``FillPoller.timeout()`` clears ``fill_pending`` and does not call
``TradingGuard.on_fill_timeout``. The deferred close used to stay set.
The next auto entry confirm pops it (``_on_fill_confirmed``). That leak
is pre-#171: the same session replays it after a switch back to auto.
#171 also wrote the close into session.json with ``fill_pending`` false,
and ``restore_inflight`` put it back — including on the open path, after
``restore_confirmed_position`` had just cleared it.

These tests assert the safe behaviour. Reed's ``test_E`` / ``test_E2``
asserted the hazard.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live.fill_poller import FillPoller
from src.live.trading_guard import TradingGuard
from tests.test_issue_168_restore_fidelity import (
    _enable_fill_com,
    _resume_guard_app,
)

_CLOSE_LONG = {"action": "TRADE_CLOSE", "side": "LONG", "price": 22400}
_CLOSE_SHORT = {"action": "TRADE_CLOSE", "side": "SHORT", "price": 22400}


def _book(app, signed: int) -> None:
    app._account_monitor = SimpleNamespace(
        get_signed_position=lambda prefix: signed,
        oi_snapshot_received=True,
        positions=[],
    )
    app._live_runner.broker.real_entry_price = 0
    app._live_runner.csv_logger = None
    app._sent = []
    app._handle_semi_auto_order = lambda decision: app._sent.append(dict(decision))
    app._get_signed_position = lambda: signed


def _guard_snapshot(pending: bool, kind: str, close: dict | None) -> dict:
    return {
        "trading_guard": {
            "fill_pending": pending,
            "fill_pending_type": kind,
            "deferred_close": close,
        },
    }


@pytest.mark.parametrize("pos", [0, 1])
def test_E_stale_deferred_not_restored(monkeypatch, pos):
    """A close saved with fill_pending false must not come back.

    Port of Reed's test_E_stale_deferred_not_restored[0] and [1].
    Sim flat (0) never calls restore_confirmed_position. Sim open (1)
    does, and that clear used to be undone by restore_inflight.
    """
    app = _resume_guard_app(position_size=pos)
    _book(app, 1)
    _enable_fill_com(monkeypatch)
    monkeypatch.setattr(rb, "_discord", None)
    app._reconcile_resumed_guard(
        _guard_snapshot(False, "entry", dict(_CLOSE_SHORT)), "auto", "TX00",
    )
    assert app._trading_guard._deferred_close is None, (
        "stale deferred close restored with fill_pending False"
    )
    assert app._trading_guard.fill_pending is False


def test_E2_stale_close_not_replayed_into_new_entry(monkeypatch):
    """Entry timeout, then resume, then a new short confirm.

    Port of Reed's test_E2. The timeout used to leave the close-LONG
    in the snapshot. Resuming that snapshot and confirming a new short
    replayed the SELL into the short.
    """
    guard = TradingGuard()
    guard.on_fill_pending("entry")
    guard.defer_close(dict(_CLOSE_LONG))
    poller = FillPoller(guard)
    poller.start("entry", 0, com_available=True)
    poller.timeout()
    saved = guard.to_dict()
    assert saved["fill_pending"] is False
    assert not saved["deferred_close"]
    assert guard._deferred_close is None

    app = _resume_guard_app(position_size=0)
    _book(app, 0)
    _enable_fill_com(monkeypatch)
    monkeypatch.setattr(rb, "_discord", None)
    app._reconcile_resumed_guard({"trading_guard": saved}, "auto", "TX00")
    assert app._trading_guard._deferred_close is None

    app._get_signed_position = lambda: 0
    app._trading_guard.on_fill_pending("entry")
    app._start_fill_polling("entry")
    result = app._fill_poller.on_position_update(-1)
    assert result is not None and result.type == "confirmed"
    app._on_fill_confirmed()
    assert not any(d.get("side") == "LONG" for d in app._sent), (
        f"stale close-LONG (SELL) replayed into new short: {app._sent}"
    )


def test_entry_timeout_same_session_does_not_replay_stale_close(monkeypatch):
    """Pre-#171, no restart: the next auto entry confirm pops the close.

    Timeout downgrades to semi-auto and does not save. Semi-auto does
    not run fill confirmation, so the close sits until a switch back
    to auto. This test is that next auto confirm, in the same process.
    """
    app = _resume_guard_app(position_size=0)
    _book(app, 0)
    _enable_fill_com(monkeypatch)
    monkeypatch.setattr(rb, "_discord", None)

    guard = app._trading_guard
    guard.on_fill_pending("entry")
    guard.defer_close(dict(_CLOSE_LONG))
    app._get_signed_position = lambda: 0
    app._start_fill_polling("entry")
    app._fill_poller.timeout()
    assert guard.fill_pending is False
    assert guard.real_entry_confirmed is True
    assert guard._deferred_close is None

    guard.on_fill_pending("entry")
    app._start_fill_polling("entry")
    result = app._fill_poller.on_position_update(-1)
    assert result is not None and result.type == "confirmed"
    app._on_fill_confirmed()
    assert app._sent == []


def test_pending_entry_deferred_close_restored_and_replayed(monkeypatch):
    """A close still waiting on an in-flight entry is restored and sent.

    The real book matches the close side, so resume re-arms the poller.
    Confirming that entry replays the close.
    """
    guard = TradingGuard()
    guard.on_fill_pending("entry")
    guard.defer_close(dict(_CLOSE_LONG))
    saved = guard.to_dict()
    assert saved["fill_pending"] is True
    assert saved["fill_pending_type"] == "entry"
    assert saved["deferred_close"]["side"] == "LONG"
    assert saved["deferred_close"] is not guard._deferred_close

    app = _resume_guard_app(position_size=0)
    _book(app, 1)
    _enable_fill_com(monkeypatch)
    monkeypatch.setattr(rb, "_discord", None)
    app._reconcile_resumed_guard({"trading_guard": saved}, "auto", "TX00")
    assert app._trading_guard.fill_pending is True
    assert app._trading_guard._deferred_close["side"] == "LONG"
    assert app._fill_poller.active is True
    assert app._fill_poller._expected_sign == 1

    result = app._fill_poller.on_position_update(1)
    assert result is not None and result.type == "confirmed"
    app._on_fill_confirmed()
    assert app._sent == [_CLOSE_LONG]
    assert app._trading_guard._deferred_close is None
    assert app._trading_guard.real_entry_confirmed is True


def test_to_dict_keeps_deferred_close_only_for_pending_entry():
    """The save gate, independent of timeout().

    fill_pending false, and an in-flight exit, must not write the close.
    """
    guard = TradingGuard()
    guard.defer_close(dict(_CLOSE_LONG))
    assert guard.to_dict()["deferred_close"] is None

    guard.on_fill_pending("exit")
    guard.defer_close(dict(_CLOSE_LONG))
    saved = guard.to_dict()
    assert saved["fill_pending"] is True
    assert saved["fill_pending_type"] == "exit"
    assert saved["deferred_close"] is None

    guard.on_fill_pending("entry")
    guard.defer_close(dict(_CLOSE_SHORT))
    saved = guard.to_dict()
    assert saved["deferred_close"]["side"] == "SHORT"
    assert saved["deferred_close"]["price"] == 22400
