"""Clear a fill gate a refused resume left held, and re-check a late OI snapshot.

Follow-up to #171 / #168. A refusal (opposite book, missing snapshot, or
no COM) keeps ``fill_pending`` and does not arm the poller. Nothing in
the app could release that gate, and a save wrote it back. A snapshot
that arrives a few seconds later never re-ran the book check.

These tests assert the safe behaviour. A failure is the hazard.
"""

import queue
from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live.account_monitor import AccountMonitor
from src.live.live_runner import LiveRunner
from src.live.session_store import load_session
from src.live.trading_guard import TradingGuard
from tests.test_issue_168_restore_fidelity import (
    SilentStrategy,
    _enable_fill_com,
    _guard_session,
    _resume_guard_app,
)


class _Book:
    """Mutable real book. The resume check reads these attributes live."""

    def __init__(self, signed=0, snapshot=False):
        self.signed = signed
        self.oi_snapshot_received = snapshot
        self.positions = []

    def get_signed_position(self, prefix):
        return self.signed


class _Btn:
    def __init__(self):
        self.state = "disabled"

    def config(self, **kw):
        if "state" in kw:
            self.state = kw["state"]


class _Orders:
    def __init__(self):
        self.calls = []

    def send(self, *args, **kwargs):
        self.calls.append(("send", args, kwargs))

    def handle(self, *args, **kwargs):
        self.calls.append(("handle", args, kwargs))

    def confirmed(self, *args, **kwargs):
        self.calls.append(("confirmed", args, kwargs))


_CLOSE = {"action": "TRADE_CLOSE", "side": "LONG", "price": 22400}


def _spy(app):
    orders = _Orders()
    app._send_real_order = orders.send
    app._handle_semi_auto_order = orders.handle
    app._on_fill_confirmed = orders.confirmed
    return orders


def _bind_save(app, tmp_path, name):
    runner = LiveRunner(
        SilentStrategy(), "TX00", point_value=200,
        log_dir=str(tmp_path), bot_name=name,
    )
    runner.set_guard_state_provider(app._trading_guard.to_dict)
    app._live_runner._auto_save_session = runner._auto_save_session
    return runner


def _refuse_missing_snapshot(monkeypatch, *, signed=0, snapshot=False):
    app = _resume_guard_app(position_size=0)
    app._account_monitor = _Book(signed=signed, snapshot=snapshot)
    app.btn_clear_fill = _Btn()
    _enable_fill_com(monkeypatch)
    app._reconcile_resumed_guard(
        _guard_session("entry", dict(_CLOSE)), "auto", "TX00",
    )
    return app


def test_manual_clear_persists_through_save_and_sends_nothing(tmp_path, monkeypatch):
    """Clear drops the gate and the close, and the next save stays clear.

    The round-trip is the stuck-gate bug: a save after the refusal used
    to put fill_pending back on the next resume.
    """
    app = _refuse_missing_snapshot(monkeypatch)
    guard = app._trading_guard
    assert guard.fill_pending is True
    assert guard._deferred_close["side"] == "LONG"
    assert app.btn_clear_fill.state == "normal"
    orders = _spy(app)
    runner = _bind_save(app, tmp_path, "clear-roundtrip")

    assert app._clear_fill_pending() is True

    assert guard.fill_pending is False
    assert guard.fill_pending_type == ""
    assert guard._deferred_close is None
    assert guard.real_entry_confirmed is False
    assert app._fill_poller.active is False
    assert app._resume_snapshot_recheck is None
    assert orders.calls == []
    assert app.btn_clear_fill.state == "disabled"
    assert any("cleared fill pending" in line for line in app.live_log.lines)
    assert any(title == "已清除成交等待 Fill Pending Cleared"
               for title, _msg in app._alerts)
    verdict, _details = guard.decide("auto", "ENTRY_FILL", "LONG")
    assert verdict == guard.SEND_ENTRY

    saved = load_session(runner.session_path)["trading_guard"]
    assert saved["fill_pending"] is False
    assert saved["deferred_close"] is None
    restored = TradingGuard()
    restored.on_fill_pending("entry")
    restored.defer_close(dict(_CLOSE))
    restored.restore_inflight(saved)
    assert restored.fill_pending is False
    assert restored._deferred_close is None


def test_clear_after_refused_resume_does_not_send(monkeypatch):
    """The clear path itself never confirms, replays, or sends."""
    app = _refuse_missing_snapshot(monkeypatch)
    orders = _spy(app)
    assert app._clear_fill_pending() is True
    assert orders.calls == []
    assert app._trading_guard._deferred_close is None


def test_held_gate_saved_then_restored_is_still_clearable(tmp_path, monkeypatch):
    """A gate saved while held comes back, and the clear still removes it."""
    app = _refuse_missing_snapshot(monkeypatch)
    runner = _bind_save(app, tmp_path, "held-then-clear")
    runner._auto_save_session()
    held = load_session(runner.session_path)["trading_guard"]
    assert held["fill_pending"] is True
    assert held["deferred_close"]["side"] == "LONG"

    app2 = _resume_guard_app(position_size=0)
    app2._trading_guard.restore_inflight(held)
    app2.btn_clear_fill = _Btn()
    app2._sync_clear_fill_button()
    assert app2._trading_guard.fill_pending is True
    assert app2._trading_guard._deferred_close["side"] == "LONG"
    assert app2.btn_clear_fill.state == "normal"
    orders = _spy(app2)
    runner.set_guard_state_provider(app2._trading_guard.to_dict)
    app2._live_runner._auto_save_session = runner._auto_save_session

    assert app2._clear_fill_pending() is True
    assert orders.calls == []
    assert app2._trading_guard.fill_pending is False
    assert app2._trading_guard._deferred_close is None

    cleared = load_session(runner.session_path)["trading_guard"]
    assert cleared["fill_pending"] is False
    assert cleared["deferred_close"] is None
    again = TradingGuard()
    again.restore_inflight(cleared)
    assert again.fill_pending is False
    assert again._deferred_close is None


def test_saved_held_gate_rechecks_when_snapshot_is_present(tmp_path, monkeypatch):
    """The saved gate is re-checked on the next resume, with the side intact.

    A matching book arms with expected_sign and resumed. The opposite
    print does not confirm.
    """
    app = _refuse_missing_snapshot(monkeypatch)
    runner = _bind_save(app, tmp_path, "held-recheck")
    runner._auto_save_session()
    held = load_session(runner.session_path)["trading_guard"]

    app2 = _resume_guard_app(position_size=0)
    app2._account_monitor = _Book(signed=1, snapshot=True)
    _enable_fill_com(monkeypatch)
    orders = _spy(app2)
    app2._reconcile_resumed_guard({"trading_guard": held}, "auto", "TX00")

    poller = app2._fill_poller
    assert poller.active is True
    assert poller._expected_sign == 1
    assert poller._resumed is True
    assert poller.on_position_update(-1) is None
    assert app2._trading_guard.real_entry_confirmed is False
    assert orders.calls == []
    matched = poller.on_position_update(1)
    assert matched is not None and matched.type == "confirmed"


def test_late_matching_snapshot_rearms_with_sign(monkeypatch):
    """A snapshot that lands after the refusal arms the poller on a match."""
    app = _refuse_missing_snapshot(monkeypatch)
    assert app._fill_poller.active is False
    assert app._resume_snapshot_recheck is not None
    orders = _spy(app)
    app._account_monitor.signed = 1
    app._account_monitor.oi_snapshot_received = True

    app._recheck_resume_after_snapshot()

    poller = app._fill_poller
    assert poller.active is True
    assert poller._expected_sign == 1
    assert poller._resumed is True
    assert poller.on_position_update(-1) is None
    assert app._trading_guard.fill_pending is True
    assert app._trading_guard.real_entry_confirmed is False
    assert orders.calls == []
    assert any("Late position snapshot" in line for line in app.live_log.lines)
    matched = poller.on_position_update(1)
    assert matched is not None and matched.type == "confirmed"


def test_late_mismatched_snapshot_stays_held(monkeypatch):
    """A late opposite book keeps the gate, drops the close, and does not arm."""
    app = _refuse_missing_snapshot(monkeypatch)
    orders = _spy(app)
    app._account_monitor.signed = -1
    app._account_monitor.oi_snapshot_received = True

    app._recheck_resume_after_snapshot()

    guard = app._trading_guard
    assert app._fill_poller.active is False
    assert app._fill_poller.on_position_update(-1) is None
    assert guard.fill_pending is True
    assert guard._deferred_close is None
    assert guard.real_entry_confirmed is False
    assert orders.calls == []
    assert any("not started" in msg for _title, msg in app._alerts)
    verdict, _details = guard.decide("auto", "TRADE_CLOSE", "LONG")
    assert verdict == guard.BLOCK_FILL_PENDING


def test_clear_then_late_snapshot_does_not_rearm(monkeypatch):
    """Once the operator clears the gate, the late row must not put it back."""
    app = _refuse_missing_snapshot(monkeypatch)
    orders = _spy(app)
    assert app._clear_fill_pending() is True
    app._account_monitor.signed = 1
    app._account_monitor.oi_snapshot_received = True

    app._recheck_resume_after_snapshot()

    assert app._fill_poller.active is False
    assert app._trading_guard.fill_pending is False
    assert app._trading_guard._deferred_close is None
    assert orders.calls == []


def test_opposite_refusal_is_not_reopened_by_a_later_snapshot(monkeypatch):
    """A known opposite book stays refused. A later row must not arm it."""
    app = _resume_guard_app(position_size=0)
    book = _Book(signed=-1, snapshot=True)
    app._account_monitor = book
    _enable_fill_com(monkeypatch)
    orders = _spy(app)
    app._reconcile_resumed_guard(
        _guard_session("entry", dict(_CLOSE)), "auto", "TX00",
    )
    assert app._fill_poller.active is False
    assert app._trading_guard._deferred_close is None
    assert app._resume_snapshot_recheck is None

    book.signed = 1
    app._recheck_resume_after_snapshot()

    assert app._fill_poller.active is False
    assert app._trading_guard.fill_pending is True
    assert app._trading_guard.real_entry_confirmed is False
    assert app._fill_poller.on_position_update(1) is None
    assert orders.calls == []


def test_no_com_refusal_is_not_reopened_by_a_snapshot(monkeypatch):
    """COM-down refusal does not become an auto-confirm when a row arrives."""
    app = _resume_guard_app(position_size=0)
    app._account_monitor = _Book(signed=1, snapshot=False)
    monkeypatch.setattr(rb, "_com_available", False)
    orders = _spy(app)
    app._reconcile_resumed_guard(
        _guard_session("entry", dict(_CLOSE)), "auto", "TX00",
    )
    assert app._fill_poller.active is False
    assert app._trading_guard.fill_pending is True
    assert app._trading_guard.real_entry_confirmed is False
    assert app._resume_snapshot_recheck is None

    _enable_fill_com(monkeypatch)
    app._account_monitor.oi_snapshot_received = True
    app._recheck_resume_after_snapshot()

    assert app._fill_poller.active is False
    assert app._trading_guard.fill_pending is True
    assert app._trading_guard.real_entry_confirmed is False
    assert orders.calls == []


def test_active_poller_is_not_clearable(monkeypatch):
    """The button stays disabled while the poller is doing its job."""
    app = _resume_guard_app(position_size=0)
    app._account_monitor = _Book(signed=1, snapshot=True)
    app.btn_clear_fill = _Btn()
    _enable_fill_com(monkeypatch)
    app._reconcile_resumed_guard(
        _guard_session("entry", dict(_CLOSE)), "auto", "TX00",
    )
    assert app._fill_poller.active is True
    assert app.btn_clear_fill.state == "disabled"
    assert app._clear_fill_pending() is False
    assert app._trading_guard.fill_pending is True


def test_queue_late_snapshot_rearms(monkeypatch):
    """The OnOpenInterest queue path is what re-arms, not a side channel."""
    app = _resume_guard_app(position_size=0)
    app._account_monitor = AccountMonitor()
    app.btn_clear_fill = _Btn()
    _enable_fill_com(monkeypatch)
    app._reconcile_resumed_guard(
        _guard_session("entry", dict(_CLOSE)), "auto", "TX00",
    )
    assert app._fill_poller.active is False
    app._update_real_account_display = lambda: None
    orders = _spy(app)

    while True:
        try:
            rb._ui_queue.get_nowait()
        except queue.Empty:
            break
    try:
        rb._ui_queue.put_nowait((
            "open_interest",
            "TF,x,TXFD0,B,1,0,22500,0,0,0",
        ))
        app._process_ui_queue()
    finally:
        while True:
            try:
                rb._ui_queue.get_nowait()
            except queue.Empty:
                break

    poller = app._fill_poller
    assert poller.active is True
    assert poller._expected_sign == 1
    assert poller._resumed is True
    assert poller.on_position_update(-1) is None
    assert orders.calls == []


def test_guard_clear_fill_pending_does_not_confirm_or_clear_halt():
    guard = TradingGuard()
    guard.on_fill_pending("entry")
    guard.defer_close(dict(_CLOSE))
    guard.halted = True
    guard.halt_reason = "kept"
    guard.real_entry_confirmed = True
    guard.clear_fill_pending()
    assert guard.fill_pending is False
    assert guard.fill_pending_type == ""
    assert guard._deferred_close is None
    assert guard.halted is True
    assert guard.halt_reason == "kept"
    assert guard.real_entry_confirmed is True
    guard.clear_halt()
    assert guard.halted is False
