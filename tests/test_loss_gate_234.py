"""Issue #234: loss pause on the heartbeat, and a fail-closed startup hold.

Real/auto entries wait until the first valid GetFutureRights reply has
gone through TradingGuard.update_pnl. Exits, flattens and fill polls do
not. The debug log is per deploy: a later Taipei date does not rotate it.
"""

from __future__ import annotations

import json
import queue
import sys
import time
import types
from types import SimpleNamespace

import pytest

import run_backtest as rb
import src.live.main_heartbeat as hb
from src.live.account_monitor import AccountMonitor
from src.live.deploy_request import DeployRequest
from src.live.fill_poller import FillPoller
from src.live.trading_guard import TradingGuard


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Log:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def append(self, line, tag="status"):
        self.lines.append((tag, line))

    def text(self) -> str:
        return "\n".join(line for _tag, line in self.lines)


class _Root:
    def __init__(self):
        self.pending = []

    def after(self, _ms, fn, *args):
        self.pending.append((fn, args))
        return len(self.pending)

    def after_cancel(self, _token):
        return None


class _Host:
    def __init__(self):
        self.machine = SimpleNamespace(degraded=False)

    def entries_allowed(self) -> bool:
        return True

    def snapshot(self) -> dict:
        return {
            "quote": "ready",
            "reply": "ready",
            "orders_paused": False,
            "entries_allowed": True,
            "entries_blocked": False,
        }


def _drain_ui() -> None:
    while True:
        try:
            rb._ui_queue.get_nowait()
        except queue.Empty:
            return


def _rights(**overrides) -> str:
    fields = ["0"] * 35
    mapping = {
        "float_pnl": 1,
        "realized_cost": 2,
        "tax": 3,
        "equity": 6,
        "realized_pnl": 11,
        "available": 31,
    }
    fields[mapping["equity"]] = "500000"
    fields[mapping["available"]] = "5000000"
    for key, value in overrides.items():
        fields[mapping[key]] = str(value)
    return ",".join(fields)


def _entry(price: int = 20000) -> dict:
    return {"action": "ENTRY_FILL", "side": "LONG", "price": price}


def _app(mode: str = "auto") -> rb.BacktestApp:
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = _Root()
    app.live_log = _Log()
    app.login_user_var = _Var("U")
    app._trading_mode = mode
    app._trading_guard = TradingGuard(daily_loss_limit=10000)
    app._logged_in = True
    app._futures_account = "ACCT"
    app._account_monitor = AccountMonitor()
    app._link_host = _Host()
    app._reply_poller = None
    app._equity_refresh_owed = False
    app._equity_refresh_sent = False
    app._link_gate_latched = False
    app.set_status = lambda *_a, **_k: None
    app._set_conn_dot = lambda *_a, **_k: None
    app._is_settlement_no_entry_window = lambda *_a, **_k: False
    app._get_signed_position = lambda: 1
    for name in (
        "real_pos_var", "real_equity_var", "real_available_var",
        "real_pnl_var", "real_realized_var", "real_fees_var",
        "real_net_var", "real_loss_limit_var", "real_maint_var",
    ):
        setattr(app, name, _Var("--"))
    blocked: list = []
    app._live_runner = SimpleNamespace(
        symbol="TX00",
        csv_logger=None,
        strategy_display_name="Probe",
        broker=SimpleNamespace(
            position_size=0,
            entry_bar_index=1,
            trades=[SimpleNamespace(exit_bar_index=2)],
        ),
        log_blocked_signal=lambda *args, **_kwargs: blocked.append(args),
    )
    app._blocked = blocked
    app._fill_poller = FillPoller(app._trading_guard)
    app._fill_tracker = SimpleNamespace(register_order=lambda *_a, **_k: None)
    return app


def _install_com(monkeypatch):
    calls: list[str] = []
    orders: list = []

    def send(_user, _async, order):
        orders.append(order)
        return ("SEQ1", 0)

    def call_com(name, fn, *args, **kwargs):
        calls.append(name)
        return fn(*args, **kwargs)

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
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(
        GetFutureRights=lambda *_a: 0,
        GetOpenInterestGW=lambda *_a: 0,
        SendFutureOrderCLR=send,
    ))
    monkeypatch.setattr(rb, "call_com", call_com)
    monkeypatch.setattr(rb, "_discord", None)
    monkeypatch.setattr(rb, "_app", None)
    return calls, orders


def _push_rights(app, raw: str) -> None:
    _drain_ui()
    rb._ui_queue.put(("future_rights", raw))
    app._process_ui_queue()


def test_heartbeat_shows_loss_pause_and_clears_it(tmp_path):
    """Paused guard: loss_paused true with net and limit. Otherwise false."""
    bot_dir = str(tmp_path / "bot")
    writer = hb.HeartbeatWriter(bot_dir)
    hb._writer = writer
    app = _app("auto")
    guard = app._trading_guard
    guard.paused = True
    guard._last_net_pnl = -10042
    try:
        app._publish_link_heartbeat()
        hb.tick()
        data = json.loads((tmp_path / "bot" / "heartbeat.json").read_text(
            encoding="utf-8"))
        assert data["loss_paused"] is True
        assert data["loss_net"] == -10042
        assert data["loss_limit"] == 10000
        assert data["link"]["orders_paused"] is False
        assert data["link"]["entries_allowed"] is True
        snap = hb._load_heartbeat(bot_dir)
        assert snap["loss_paused"] is True
        assert snap["loss_net"] == -10042
        assert snap["loss_limit"] == 10000

        guard.paused = False
        guard._last_net_pnl = -10
        app._publish_link_heartbeat()
        hb.tick()
        cleared = json.loads((tmp_path / "bot" / "heartbeat.json").read_text(
            encoding="utf-8"))
        assert cleared["loss_paused"] is False
        assert cleared["loss_net"] == -10
        assert cleared["loss_limit"] == 10000
        assert cleared["link"]["orders_paused"] is False
        assert cleared["link"]["entries_allowed"] is True
        snap = hb._load_heartbeat(bot_dir)
        assert snap["loss_paused"] is False
        assert snap["loss_net"] == -10
        assert snap["loss_limit"] == 10000
    finally:
        hb._writer = None


@pytest.mark.parametrize("mode", ["auto", "semi_auto"])
def test_real_deploy_arms_startup_hold_paper_does_not(mode):
    app = _app(mode)
    app._reset_trading_guard_for_deploy()
    assert app._equity_refresh_owed is True
    assert app._trading_guard.paused is False

    paper = _app("paper")
    paper._equity_refresh_owed = True
    paper._reset_trading_guard_for_deploy()
    assert paper._equity_refresh_owed is False


def test_entry_before_rights_is_held_then_pause_sticks(monkeypatch):
    """Restart under the limit: no order before the reply, then paused."""
    calls, _orders = _install_com(monkeypatch)
    app = _app("auto")
    app._reset_trading_guard_for_deploy()

    app._handle_semi_auto_order(_entry())
    assert "SendFutureOrderCLR" not in calls
    assert "Entry held until equity refresh" in app.live_log.text()

    app._apply_link_gate()
    assert app._trading_guard.link_entries_blocked is True
    assert calls.count("GetFutureRights") == 1
    app._handle_semi_auto_order(_entry())
    assert "SendFutureOrderCLR" not in calls
    assert app.live_log.text().count("Entry held until equity refresh") >= 2

    _push_rights(app, "##,end")
    assert app._equity_refresh_owed is True
    assert app._trading_guard.paused is False

    bad = _rights(realized_pnl="not-a-number")
    _push_rights(app, bad)
    assert app._equity_refresh_owed is True
    assert app._trading_guard.paused is False

    _push_rights(app, _rights(realized_pnl="-10042"))
    assert app._trading_guard.paused is True
    assert app._trading_guard._last_net_pnl == -10042
    assert app._equity_refresh_owed is False
    app._handle_semi_auto_order(_entry())
    assert "SendFutureOrderCLR" not in calls


def test_entries_open_after_rights_when_net_is_above_the_limit(monkeypatch):
    calls, orders = _install_com(monkeypatch)
    app = _app("auto")
    app._reset_trading_guard_for_deploy()
    app._apply_link_gate()
    app._handle_semi_auto_order(_entry())
    assert "SendFutureOrderCLR" not in calls
    assert "held" in app.live_log.text()

    _push_rights(app, _rights(realized_pnl="-100"))
    assert app._trading_guard.paused is False
    assert app._trading_guard._last_net_pnl == -100
    assert app._equity_refresh_owed is False
    assert app._trading_guard.link_entries_blocked is False

    app._handle_semi_auto_order(_entry())
    assert calls.count("SendFutureOrderCLR") == 1
    assert len(orders) == 1
    assert orders[0].sNewClose == 0


def test_exit_flatten_and_fill_pass_during_startup_hold(monkeypatch):
    """An exit, a flatten, and a fill poll are not held."""
    calls, orders = _install_com(monkeypatch)
    app = _app("auto")
    app._reset_trading_guard_for_deploy()
    app._apply_link_gate()
    assert app._equity_refresh_owed is True
    app._trading_guard.on_entry_sent()

    app._handle_semi_auto_order(
        {"action": "TRADE_CLOSE", "side": "LONG", "price": 20000})
    app._handle_semi_auto_order(
        {"action": "FORCE_CLOSE", "side": "LONG", "price": 20000})
    assert app._equity_refresh_owed is True
    assert [order.sNewClose for order in orders] == [2, 1]
    assert calls.count("SendFutureOrderCLR") == 2

    before = calls.count("GetOpenInterestGW")
    app._poll_fill_confirmation()
    assert calls.count("GetOpenInterestGW") == before + 1
    assert app._equity_refresh_owed is True
    assert "SendFutureOrderCLR" not in calls[calls.index("GetOpenInterestGW"):]


def test_equity_hold_alerts_once_and_keeps_holding(monkeypatch):
    calls, _orders = _install_com(monkeypatch)
    notes: list[str] = []
    monkeypatch.setattr(
        rb, "_discord",
        SimpleNamespace(enabled=True, notify=notes.append))
    app = _app("auto")
    app._reset_trading_guard_for_deploy()
    app._apply_link_gate()
    app._note_equity_hold_timeout()
    assert notes == []

    app._equity_hold_since = time.monotonic() - (rb.EQUITY_ENTRY_HOLD_TIMEOUT_S + 1)
    app._note_equity_hold_timeout()
    app._note_equity_hold_timeout()
    assert len(notes) == 1
    assert "held" in notes[0]
    assert app.live_log.text().count("stay held") == 1
    assert app._equity_refresh_owed is True

    app._handle_semi_auto_order(_entry())
    assert "SendFutureOrderCLR" not in calls


@pytest.mark.parametrize("mode, armed", [
    ("auto", True),
    ("semi_auto", True),
    ("paper", False),
])
def test_deploy_live_from_arms_the_hold_for_real_modes_only(mode, armed):
    """The production arm inside ``_deploy_live_from``, not the test helper.

    An unknown strategy returns after that arm and before any bot dir
    is created. Deleting the arm leaves ``_equity_refresh_owed`` unset.
    """
    app = SimpleNamespace(
        symbol_var=_Var("TMF00"),
        strategy_var=_Var("not-a-real-strategy"),
        bot_name_var=_Var(),
        trading_mode_var=_Var(),
        pv_var=_Var("10"),
        mode_combo=SimpleNamespace(config=lambda **_kwargs: None),
        _mode_key_to_combo={
            "paper": "Paper",
            "semi_auto": "Semi",
            "auto": "Auto",
        },
        _settings={},
        _trading_guard=TradingGuard(daily_loss_limit=1),
        _fill_poller=SimpleNamespace(reset=lambda: None),
        _session_end_exit_pending=True,
        set_status=lambda *_args, **_kwargs: None,
    )
    req = DeployRequest(
        bot_name="loss-gate", trading_mode=mode, loss_limit="10000")
    assert rb.BacktestApp._deploy_live_from(app, req) is False
    assert app._trading_mode == mode
    assert app._equity_refresh_owed is armed
    if armed:
        assert app._equity_hold_since is not None
        assert app._equity_refresh_sent is False
    else:
        assert app._equity_hold_since is None


def test_paper_to_real_switch_holds_entries_at_net_zero(monkeypatch):
    """A hot-swap onto real orders must not enter while net is still 0."""
    calls, _orders = _install_com(monkeypatch)
    for new_mode in ("auto", "semi_auto"):
        app = _app("paper")
        app.trading_mode_var = _Var()
        app._mode_key_to_combo = {
            "paper": "Paper", "semi_auto": "Semi", "auto": "Auto",
        }
        app._trading_guard._last_net_pnl = 0
        assert app._trading_guard.paused is False
        app._on_mode_changed("paper", new_mode)
        assert app._trading_mode == new_mode
        assert app._equity_refresh_owed is True
        assert app._trading_guard.link_entries_blocked is True
        app._handle_semi_auto_order(_entry())
        assert "SendFutureOrderCLR" not in calls
        assert "Entry held until equity refresh" in app.live_log.text()
        calls.clear()


def test_switch_within_real_modes_does_not_rearm_the_hold():
    """auto↔semi_auto keeps the hold it already has. Leaving real does not arm."""
    app = _app("auto")
    app.trading_mode_var = _Var()
    app._mode_key_to_combo = {
        "paper": "Paper", "semi_auto": "Semi", "auto": "Auto",
    }
    app._equity_refresh_owed = False
    app._equity_hold_since = None
    app._on_mode_changed("auto", "semi_auto")
    assert app._trading_mode == "semi_auto"
    assert app._equity_refresh_owed is False
    assert app._equity_hold_since is None

    app._trading_mode = "auto"
    app._equity_refresh_owed = True
    app._equity_hold_since = 123.0
    app._equity_refresh_sent = True
    app._on_mode_changed("semi_auto", "auto")
    assert app._equity_refresh_owed is True
    assert app._equity_hold_since == 123.0
    assert app._equity_refresh_sent is True

    app._equity_refresh_owed = False
    app._equity_hold_since = None
    app._on_mode_changed("auto", "paper")
    assert app._trading_mode == "paper"
    assert app._equity_refresh_owed is False


def test_non_real_mode_other_than_paper_also_arms_on_the_way_to_auto():
    app = _app("paper")
    app.trading_mode_var = _Var()
    app._mode_key_to_combo = {"paper": "Paper", "auto": "Auto", "semi_auto": "Semi"}
    app._equity_refresh_owed = False
    app._on_mode_changed("", "auto")
    assert app._equity_refresh_owed is True
    assert app._equity_hold_since is not None


def test_paper_link_gate_releases_the_equity_hold():
    """M9: the paper branch of ``_apply_link_gate`` must clear the hold."""
    app = _app("paper")
    app._equity_refresh_owed = True
    app._equity_hold_since = 1.0
    app._equity_refresh_sent = True
    app._apply_link_gate()
    assert app._equity_refresh_owed is False
    assert app._equity_hold_since is None
    assert app._trading_guard.link_entries_blocked is False

    real = _app("auto")
    real._equity_refresh_owed = True
    real._equity_hold_since = 1.0
    real._apply_link_gate()
    assert real._equity_refresh_owed is True
    assert real._trading_guard.link_entries_blocked is True


def test_equity_alert_is_suppressed_while_the_reply_link_is_down(monkeypatch):
    """#231 already alerts while queries are blocked. This note waits."""
    notes: list[str] = []
    monkeypatch.setattr(
        rb, "_discord",
        SimpleNamespace(enabled=True, notify=notes.append))
    app = _app("auto")
    app._equity_refresh_owed = True
    app._equity_hold_alerted = False
    app._equity_hold_since = time.monotonic() - (rb.EQUITY_ENTRY_HOLD_TIMEOUT_S + 1)
    app._reply_poller = SimpleNamespace(down=True)
    app._note_equity_hold_timeout()
    assert notes == []
    assert app._equity_hold_alerted is False
    assert app._equity_refresh_owed is True

    app._reply_poller = SimpleNamespace(down=False)
    app._note_equity_hold_timeout()
    app._note_equity_hold_timeout()
    assert len(notes) == 1
    assert "held" in notes[0]


def test_heartbeat_tick_sends_the_equity_hold_alert(monkeypatch):
    """M10: the alert is fired from ``_on_main_heartbeat``, not only the helper."""
    notes: list[str] = []
    monkeypatch.setattr(
        rb, "_discord",
        SimpleNamespace(enabled=True, notify=notes.append))
    app = _app("auto")
    app._heartbeat_active = True
    app._equity_refresh_owed = True
    app._equity_hold_alerted = False
    app._equity_hold_since = time.monotonic() - (rb.EQUITY_ENTRY_HOLD_TIMEOUT_S + 1)
    app._on_main_heartbeat()
    assert len(notes) == 1
    assert "held" in notes[0]
    assert app._equity_refresh_owed is True
    assert app._heartbeat_after_id is not None


def test_debug_log_is_per_deploy_not_rotated(tmp_path, monkeypatch):
    """The docstring matches the file: one deploy, one file, no dropped lines."""
    from datetime import datetime

    doc = " ".join((rb._open_debug_log.__doc__ or "").lower().split())
    assert "per-deploy" in doc or "per deploy" in doc
    assert "daily-rotated" not in doc

    day = {"when": datetime(2026, 11, 3, 22, 0)}
    monkeypatch.setattr(rb, "_taipei_now", lambda: day["when"])
    monkeypatch.setattr(rb, "_app", None)
    rb._close_debug_log()
    try:
        rb._open_debug_log(str(tmp_path))
        rb._log("line-on-open-day")
        day["when"] = datetime(2026, 11, 4, 0, 5)
        rb._log("line-after-midnight")
        files = sorted(tmp_path.glob("debug_*.log"))
        assert [path.name for path in files] == ["debug_20261103.log"]
        text = files[0].read_text(encoding="utf-8")
        assert "line-on-open-day" in text
        assert "line-after-midnight" in text
    finally:
        rb._close_debug_log()
