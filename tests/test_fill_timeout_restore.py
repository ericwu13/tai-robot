"""Issue #319: lift FILL_TIMEOUT_DOWNGRADE when that order's own deal arrives.

One rule. A downgrade from auto back to auto happens only when the same
timed-out order's seq-matched OnNewData D row arrives while the bot is
still in that downgrade. A resumed timeout, a blank seq, a second
timeout, or any later mode change does not restore. The lift writes
auto directly. It does not go through ``_apply_mode_switch``, and the
marker is not written to session.json.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live.deploy_request import DeployRequest
from src.live.fill_poller import FillPoller
from src.live.fill_report import RealFillTracker
from src.live.trading_guard import TradingGuard
from tests.test_fill_report import make_deal_row

SEQ = "2315623892999"
OTHER = "2315623893000"


def _reason(action: str, seq: str) -> str:
    return f"{action} seq={seq} deal row → auto"


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Root:
    def __init__(self):
        self.pending = {}
        self._next = 1

    def after(self, ms, fn, *args):
        token = self._next
        self._next += 1
        self.pending[token] = (ms, fn, args)
        return token

    def after_cancel(self, token):
        self.pending.pop(token, None)


class _Discord:
    def __init__(self):
        self.enabled = True
        self.restored = []
        self.switched = []
        self.downgrades = []

    def fill_timeout_restored(self, action_type, order_seq):
        self.restored.append((action_type, order_seq))

    def mode_switched(self, old_mode, new_mode):
        self.switched.append((old_mode, new_mode))

    def fill_timeout_downgrade(self, action_type, timeout_s):
        self.downgrades.append((action_type, timeout_s))

    def order_sent(self, *_args, **_kwargs):
        return None

    def order_failed(self, *_args, **_kwargs):
        return None


def _make_app(monkeypatch, *, mode="auto", accept_exit=True, accept_entry=True):
    guard = TradingGuard()
    poller = FillPoller(guard)
    decisions = []
    logs = []
    saves = []
    switch_calls = []
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(rb, "_log", lambda msg: logs.append(msg))

    def _apply_mode_switch(new_mode, user_confirmed=False):
        switch_calls.append((new_mode, user_confirmed))

    broker = SimpleNamespace(
        trade_source="real",
        entry_bar_index=4,
        trades=[SimpleNamespace(exit_bar_index=9)],
        try_set_real_exit_price=lambda *_a, **_k: accept_exit,
        try_set_real_entry_price=lambda *_a, **_k: accept_entry,
        position_size=1,
    )
    runner = SimpleNamespace(
        trading_mode=mode,
        broker=broker,
        symbol="TMF00",
        _allow_live_override=False,
        _apply_mode_switch=_apply_mode_switch,
    )
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._trading_guard = guard
    app._fill_poller = poller
    app._fill_tracker = RealFillTracker()
    app._fill_timeout_restore_marker = None
    app._trading_mode = mode
    app._live_runner = runner
    app.trading_mode_var = _Var("全自動 Auto")
    app._mode_key_to_combo = {
        "paper": "模擬 Paper",
        "semi_auto": "輔助 Semi-Auto",
        "auto": "全自動 Auto",
    }
    app._fill_poll_timer_id = None
    app._futures_account = ""
    app._real_fill_count = 0
    app.real_fills_var = _Var("0")
    app._est_notified_exit_seq = ""
    app._session_end_exit_pending = False
    app._exit_fill_unresolved = lambda: False
    app._live_log_msg = lambda *args, **_kwargs: logs.append(args[0] if args else "")
    app._log_order_decision = lambda action, reason: decisions.append((action, reason))
    app._update_live_results = lambda: None
    app.root = _Root()
    app.login_user_var = _Var("user")
    app._account_monitor = SimpleNamespace(rights=None, oi_query_generation=1, positions=[])
    app._equity_refresh_owed = False
    app._reply_poller = None
    app._decisions = decisions
    app._logs = logs
    app._saves = saves
    app._switch_calls = switch_calls
    app._discord_spy = discord

    def _save():
        # The marker is cleared before the save, and the save itself is
        # the existing session snapshot. Nothing new is handed to it.
        saves.append(app._fill_timeout_restore_marker)

    runner._auto_save_session = _save
    return app


def _timeout(app, action, seq, *, position=None, resumed=False, expected_sign=None):
    if position is None:
        position = 1 if action == "exit" else 0
    app._trading_guard.on_fill_pending(action)
    app._fill_poller.start(
        action, position, com_available=True, order_seq=seq,
        resumed=resumed, expected_sign=expected_sign)
    app._on_fill_timeout()


def _deal(app, seq, action, price=39626):
    app._fill_tracker.register_order(seq, action, bar_index=1, sim_price=price)
    app._on_new_data(make_deal_row(seq=seq, price=f"{price}.000000"))


def _install_order_com(monkeypatch):
    sk_mod = types.ModuleType("comtypes.gen.SKCOMLib")

    class _Order:
        pass

    sk_mod.FUTUREORDER = _Order
    gen = types.ModuleType("comtypes.gen")
    comtypes_mod = types.ModuleType("comtypes")
    comtypes_mod.gen = gen
    monkeypatch.setitem(sys.modules, "comtypes", comtypes_mod)
    monkeypatch.setitem(sys.modules, "comtypes.gen", gen)
    monkeypatch.setitem(sys.modules, "comtypes.gen.SKCOMLib", sk_mod)


def _assert_restored(app, action, seq):
    assert app._trading_mode == "auto"
    assert app._live_runner.trading_mode == "auto"
    assert app._live_runner.broker.trade_source == "real"
    assert app.trading_mode_var.value == "全自動 Auto"
    assert app._fill_timeout_restore_marker is None
    assert ("FILL_TIMEOUT_RESTORED", _reason(action, seq)) in app._decisions
    assert app._discord_spy.restored == [(action, seq)]
    assert app._discord_spy.switched == []
    assert None in app._saves
    assert app._switch_calls == []


# ── (a) FillPoller contract, also covered in test_fill_poller.py ──

class TestPollerContract:
    def test_seq_round_trip_and_ten_second_timeout(self):
        guard = TradingGuard()
        poller = FillPoller(guard)
        assert poller.FILL_POLL_TIMEOUT == 10.0
        poller.start("exit", 1, order_seq="  " + SEQ + "  ")
        assert poller._order_seq == SEQ
        result = poller.timeout()
        assert result.order_seq == SEQ
        assert result.restorable is True
        assert result.timeout_seconds == 10.0
        poller.reset()
        assert poller._order_seq == ""

    @pytest.mark.parametrize("seq", ["", "   "])
    def test_blank_seq_is_not_restorable(self, seq):
        guard = TradingGuard()
        poller = FillPoller(guard)
        poller.start("entry", 0, order_seq=seq)
        result = poller.timeout()
        assert result.order_seq == ""
        assert result.restorable is False

    def test_resumed_mismatch_and_match_are_not_restorable(self):
        guard = TradingGuard()
        mismatch = FillPoller(guard)
        mismatch.start("entry", -1, expected_sign=1, resumed=True, order_seq=SEQ)
        early = mismatch.timeout()
        assert early.restorable is False
        assert early.order_seq == SEQ
        assert "NOT confirmed" in early.message

        guard2 = TradingGuard()
        guard2.on_fill_pending("entry")
        matched = FillPoller(guard2)
        matched.start("entry", 1, expected_sign=1, resumed=True, order_seq=SEQ)
        fallen = matched.timeout()
        assert fallen.restorable is False
        assert fallen.order_seq == SEQ
        assert guard2.real_entry_confirmed is True


# ── (b) (c) (d) (h) (i) the deal row lifts that one downgrade ──

def test_exit_timeout_then_same_seq_deal_restores_auto(monkeypatch):
    app = _make_app(monkeypatch, accept_exit=True)
    _timeout(app, "exit", SEQ)
    assert app._trading_mode == "semi_auto"
    assert app._live_runner.trading_mode == "semi_auto"
    assert app._fill_timeout_restore_marker == (SEQ, "exit")
    assert any(action == "FILL_TIMEOUT_DOWNGRADE" for action, _reason in app._decisions)
    app._live_runner.broker.trade_source = "paper"
    _deal(app, SEQ, "exit")
    _assert_restored(app, "exit", SEQ)


def test_entry_timeout_then_same_seq_deal_restores_auto(monkeypatch):
    app = _make_app(monkeypatch, accept_entry=True)
    _timeout(app, "entry", SEQ)
    assert app._fill_timeout_restore_marker == (SEQ, "entry")
    app._live_runner.broker.trade_source = "paper"
    _deal(app, SEQ, "entry", price=40175)
    _assert_restored(app, "entry", SEQ)


def test_different_seq_keeps_semi_auto_until_the_matching_deal(monkeypatch):
    app = _make_app(monkeypatch)
    _timeout(app, "exit", SEQ)
    _deal(app, OTHER, "exit", price=39600)
    assert app._trading_mode == "semi_auto"
    assert app._live_runner.trading_mode == "semi_auto"
    assert app._fill_timeout_restore_marker == (SEQ, "exit")
    assert app._discord_spy.restored == []
    assert app._discord_spy.switched == []
    app._live_runner.broker.trade_source = "paper"
    _deal(app, SEQ, "exit")
    _assert_restored(app, "exit", SEQ)


def test_rejected_exit_price_still_restores_before_the_early_return(monkeypatch):
    """try_set_real_exit_price False used to return before any later work.

    The restore sits after record_fill and before that return, so a
    price the broker refuses still lifts auto.
    """
    app = _make_app(monkeypatch, accept_exit=False)
    _timeout(app, "exit", SEQ)
    app._live_runner.broker.trade_source = "paper"
    _deal(app, SEQ, "exit")
    _assert_restored(app, "exit", SEQ)
    assert not any(action == "REAL_EXIT_CONFIRMED" for action, _reason in app._decisions)
    assert app._saves == [None]


def test_restore_does_not_use_apply_mode_switch_when_override_is_off(monkeypatch):
    """Headless allow_live_override=false refuses a dashboard climb to auto.

    This path writes the mode itself, so that gate does not apply.
    """
    app = _make_app(monkeypatch, accept_exit=False)
    assert app._live_runner._allow_live_override is False
    _timeout(app, "exit", SEQ)
    app._live_runner.broker.trade_source = "paper"
    _deal(app, SEQ, "exit")
    _assert_restored(app, "exit", SEQ)


def test_restore_without_a_mode_combo_still_writes_auto(monkeypatch):
    app = _make_app(monkeypatch, accept_entry=False)
    _timeout(app, "entry", SEQ)
    app.trading_mode_var = None
    app._live_runner.broker.trade_source = "paper"
    _deal(app, SEQ, "entry", price=40175)
    assert app._trading_mode == "auto"
    assert app._live_runner.trading_mode == "auto"
    assert app._live_runner.broker.trade_source == "real"
    assert app._fill_timeout_restore_marker is None
    assert app._switch_calls == []


def test_save_and_discord_failures_still_leave_auto(monkeypatch):
    app = _make_app(monkeypatch, accept_exit=False)

    def _boom_save():
        raise RuntimeError("disk")

    def _boom_discord(action_type, order_seq):
        raise RuntimeError("net")

    app._live_runner._auto_save_session = _boom_save
    app._discord_spy.fill_timeout_restored = _boom_discord
    _timeout(app, "exit", SEQ)
    _deal(app, SEQ, "exit")
    assert app._trading_mode == "auto"
    assert app._live_runner.trading_mode == "auto"
    assert app._fill_timeout_restore_marker is None
    assert any("session save failed" in line for line in app._logs)
    assert any("Discord" in line for line in app._logs)


# ── (e) (f) (g) the marker is one downgrade, then it is gone ──

def test_manual_mode_change_clears_the_marker_and_blocks_restore(monkeypatch):
    app = _make_app(monkeypatch)
    _timeout(app, "exit", SEQ)
    assert app._fill_timeout_restore_marker == (SEQ, "exit")
    app._on_mode_changed("semi_auto", "paper")
    assert app._fill_timeout_restore_marker is None
    assert app._trading_mode == "paper"
    assert app._discord_spy.switched == [("semi_auto", "paper")]
    _deal(app, SEQ, "exit")
    assert app._trading_mode == "paper"
    assert app._live_runner.trading_mode == "semi_auto"
    assert app._discord_spy.restored == []


def test_deploy_and_stop_clear_the_marker():
    app = SimpleNamespace(
        symbol_var=_Var("TMF00"),
        strategy_var=_Var("not-a-real-strategy"),
        bot_name_var=_Var(),
        trading_mode_var=_Var(),
        pv_var=_Var("10"),
        mode_combo=SimpleNamespace(config=lambda **_kwargs: None),
        _mode_key_to_combo={
            "paper": "Paper", "semi_auto": "Semi", "auto": "Auto",
        },
        _settings={},
        _trading_guard=TradingGuard(),
        _fill_poller=SimpleNamespace(reset=lambda: None),
        _session_end_exit_pending=True,
        _fill_timeout_restore_marker=(SEQ, "exit"),
        set_status=lambda *_args, **_kwargs: None,
    )
    req = DeployRequest(
        bot_name="restore-clear", trading_mode="paper", loss_limit="10000")
    assert rb.BacktestApp._deploy_live_from(app, req) is False
    assert app._fill_timeout_restore_marker is None

    stopped = rb.BacktestApp.__new__(rb.BacktestApp)
    stopped._fill_timeout_restore_marker = (SEQ, "entry")

    def _after_clear():
        raise RuntimeError("teardown continues")

    stopped._cancel_exit_fill_retry = _after_clear
    with pytest.raises(RuntimeError, match="teardown continues"):
        stopped._stop_live()
    assert stopped._fill_timeout_restore_marker is None
    assert stopped._evolution_cancel is True


def test_mode_left_semi_auto_by_hand_clears_marker_without_restore(monkeypatch):
    """A direct write off semi_auto (not _on_mode_changed) still disarms."""
    app = _make_app(monkeypatch)
    _timeout(app, "exit", SEQ)
    app._trading_mode = "paper"
    _deal(app, SEQ, "exit")
    assert app._trading_mode == "paper"
    assert app._fill_timeout_restore_marker is None
    assert app._discord_spy.restored == []
    assert app._switch_calls == []


def test_second_timeout_clears_the_marker_instead_of_replacing_it(monkeypatch):
    app = _make_app(monkeypatch, accept_exit=False)
    _timeout(app, "exit", SEQ)
    assert app._fill_timeout_restore_marker == (SEQ, "exit")
    # Still auto, with a marker already armed. A second restorable
    # timeout must drop the marker, not point it at the new seq.
    app._trading_mode = "auto"
    _timeout(app, "exit", OTHER)
    assert app._fill_timeout_restore_marker is None
    assert app._trading_mode == "semi_auto"
    _deal(app, SEQ, "exit")
    _deal(app, OTHER, "exit", price=39600)
    assert app._trading_mode == "semi_auto"
    assert app._live_runner.trading_mode == "semi_auto"
    assert app._discord_spy.restored == []
    assert app._switch_calls == []


def test_timeout_while_already_semi_auto_never_arms(monkeypatch):
    app = _make_app(monkeypatch, mode="semi_auto", accept_exit=False)
    app._live_runner.trading_mode = "semi_auto"
    assert app._fill_timeout_restore_marker is None
    _timeout(app, "exit", SEQ)
    assert app._fill_timeout_restore_marker is None
    assert app._trading_mode == "semi_auto"
    _deal(app, SEQ, "exit")
    assert app._trading_mode == "semi_auto"
    assert app._discord_spy.restored == []


@pytest.mark.parametrize(
    "position, expected_sign",
    [(-1, 1), (1, 1)],
    ids=["mismatch", "matching-sign"],
)
def test_resumed_timeout_never_arms_or_restores(monkeypatch, position, expected_sign):
    app = _make_app(monkeypatch, accept_entry=False)
    _timeout(
        app, "entry", SEQ, position=position, resumed=True,
        expected_sign=expected_sign)
    assert app._fill_poller._resumed is True
    assert app._fill_timeout_restore_marker is None
    assert app._trading_mode == "semi_auto"
    _deal(app, SEQ, "entry", price=40175)
    assert app._trading_mode == "semi_auto"
    assert app._discord_spy.restored == []
    assert app._saves == []


def test_blank_seq_timeout_from_auto_does_not_arm(monkeypatch):
    app = _make_app(monkeypatch, accept_exit=False)
    _timeout(app, "exit", "   ")
    assert app._trading_mode == "semi_auto"
    assert app._fill_timeout_restore_marker is None
    _deal(app, SEQ, "exit")
    assert app._trading_mode == "semi_auto"
    assert app._discord_spy.restored == []


# ── wiring: the seq from SendFutureOrderCLR reaches the poller ──

def test_start_fill_polling_forwards_order_seq(monkeypatch):
    app = _make_app(monkeypatch)
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", object())
    app._futures_account = "ACCT"
    app._get_signed_position = lambda: 1
    app._start_fill_polling("exit", order_seq="  " + SEQ + " \n")
    assert app._fill_poller._order_seq == SEQ
    assert app._fill_poller.active is True
    assert app._fill_poller.action_type == "exit"
    assert app._fill_poller._resumed is False


@pytest.mark.parametrize("action", ["entry", "exit"])
def test_send_real_order_passes_the_stripped_seq(monkeypatch, action):
    app = _make_app(monkeypatch)
    _install_order_com(monkeypatch)
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", object())
    monkeypatch.setattr(
        rb._broker, "place_order", lambda *_a, **_k: ("  " + SEQ + "  ", 0))
    app._futures_account = "ACCT"
    app._trading_mode = "auto"
    signed = 0 if action == "entry" else 1
    app._get_signed_position = lambda: signed
    buy_sell = 0 if action == "entry" else 1
    assert app._send_real_order(buy_sell, "TM0000", action, 39619) is True
    assert app._fill_poller._order_seq == SEQ
    assert app._fill_poller.action_type == action
    assert app._fill_poller.active is True
