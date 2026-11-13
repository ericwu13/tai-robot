"""A deploy/resume abort must not flatten (owner decision 2026-11-13).

Eric: "On a deploy abort, stop the bot, leave the real position alone
and send an alert." Before this change every warmup / tick-subscribe
abort called the plain ``_stop_live()``. On a resumed semi_auto/auto bot
whose real position was reconciled (``real_entry_confirmed``) that sent
an IOC market stop-close, and ``LiveRunner.stop()`` force-closed the sim
position and saved the session flat, so a later ``--resume`` no longer
knew about the position.

The abort sites now call ``_abort_deploy(reason)``. The user's own stop
(the Stop Bot button, the headless STOP file / dashboard stop) still
calls ``_stop_live()`` and still flattens.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import run_backtest as rb
from src.backtest.broker import OrderSide, Trade
from src.live.fill_poller import FillPoller
from src.live.live_runner import LiveRunner
from src.live.session_store import load_session
from src.live.trading_guard import TradingGuard
from tests.test_issue_168_restore_fidelity import SilentStrategy, _open_broker
from tests.test_live_runner import _kline


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

    def update_idletasks(self):
        pass

    def drain(self):
        while self.pending:
            token = next(iter(self.pending))
            _ms, fn, args = self.pending.pop(token)
            fn(*args)


class _Discord:
    enabled = True

    def __init__(self):
        self.notes = []
        self.stopped = []

    def notify(self, message, *, priority=False):
        self.notes.append((message, priority))

    def bot_stopped(self, trades, pnl):
        self.stopped.append((trades, pnl))


def _runner(tmp_path, mode, side="LONG", *, warm=True):
    runner = LiveRunner(
        SilentStrategy(), "TX00", point_value=200,
        log_dir=str(tmp_path), bot_name="abortbot",
    )
    runner.trading_mode = mode
    runner.restore_session({
        "bar_index": 10,
        "broker": _open_broker(side=side, stop=22400, tag="SL"),
        "exit_levels": {"stop": 22400, "from_entry": "Long" if side == "LONG" else "Short",
                        "tag": "SL"},
    })
    if warm:
        runner.feed_warmup_bars([
            _kline("2026-02-20 09:00", 22500, 22510, 22490, 22500),
            _kline("2026-02-20 09:01", 22500, 22510, 22490, 22500),
        ])
    return runner


def _app(monkeypatch, runner, mode, *, confirmed=True):
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = _Root()
    app.logs = []
    app._live_log_msg = lambda msg, tag="status": app.logs.append((tag, msg))
    app.alerts = []
    app._alert = lambda title, msg: app.alerts.append((title, msg))
    app.orders = []
    app._send_real_order = lambda *a, **k: app.orders.append((a, k)) or True
    app._log_order_decision = lambda *a, **k: None
    app._get_latest_price = lambda: (22500, "tick")
    guard = TradingGuard()
    guard.reset()
    if confirmed:
        guard.on_entry_sent()
    app._trading_guard = guard
    app._fill_poller = FillPoller(guard, timeout=10.0)
    app._trading_mode = mode
    app._live_runner = runner
    app._live_warmup_mode = False
    app._live_warmup_data = []
    # teardown attributes touched by _stop_live
    app._evolution_cancel = False
    app._cancel_exit_fill_retry = lambda: None
    app._stop_account_polling = lambda: None
    app._live_poll_id = None
    app._reconnect_timer_id = None
    app._conn_monitor = SimpleNamespace(reset=lambda: None)
    app._warmup_timeout_id = None
    app._fill_poll_timer_id = None
    app._live_tick_count = 0
    app._live_history_done = False
    app._live_tick_active = False
    app._tick_watchdog = MagicMock()
    app._live_chart = None
    app._run_evolution_check_after_report = lambda: None
    app._update_live_results = lambda: None
    app._update_live_status = lambda: None
    app._stop_main_heartbeat = lambda: None
    app._position_ledger = None
    app._regime_manager = None
    app._update_manual_order_buttons = lambda: None
    app.set_status = lambda *a, **k: None
    app._quote_connected = True
    app._deploy_pending = False
    for name in ("btn_deploy", "btn_api", "btn_tv", "btn_update",
                 "symbol_combo", "strategy_combo", "bot_name_var",
                 "trading_mode_var", "mode_combo", "chart_tf_combo",
                 "chart_tf_var"):
        setattr(app, name, MagicMock())
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(rb, "_close_debug_log", lambda: None)
    return app, discord


# ── Abort sites ──────────────────────────────────────────────────────

def _abort_quote_down_at_subscribe(app, monkeypatch):
    # run_backtest.py _start_live_tick_subscription: "未連線 Not connected"
    app._quote_connected = False


def _abort_subscribe_rejected(app, monkeypatch):
    # _start_live_tick_subscription: RequestTicks returns >= 3000
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "_broker", SimpleNamespace(
        request_ticks=lambda *a: 3021))


def _abort_subscribe_raises(app, monkeypatch):
    monkeypatch.setattr(rb, "_com_available", True)

    def _boom(*a):
        raise RuntimeError("COM gone")
    monkeypatch.setattr(rb, "_broker", SimpleNamespace(request_ticks=_boom))


SUBSCRIBE_ABORTS = [
    pytest.param(_abort_quote_down_at_subscribe, id="quote-down"),
    pytest.param(_abort_subscribe_rejected, id="subscribe-3021"),
    pytest.param(_abort_subscribe_raises, id="subscribe-raises"),
]


@pytest.mark.parametrize("arrange", SUBSCRIBE_ABORTS)
@pytest.mark.parametrize("mode", ["auto", "semi_auto"])
def test_subscribe_abort_sends_no_order_and_keeps_position(
        tmp_path, monkeypatch, arrange, mode):
    runner = _runner(tmp_path, mode)
    app, discord = _app(monkeypatch, runner, mode)
    arrange(app, monkeypatch)

    app._start_live_tick_subscription()
    app.root.drain()

    assert app.orders == [], "a deploy abort must not send a real order"
    assert app._live_runner is None  # the bot is stopped
    # Sim position untouched: no FORCE_CLOSE trade was booked.
    assert runner.broker.position_size == 1
    assert runner.broker.position_side == OrderSide.LONG
    assert runner.broker.trades == []
    # Saved open, so --resume picks the position up again.
    saved = load_session(runner.session_path)
    assert saved["broker"]["position_size"] == 1
    assert saved["broker"]["position_side"] == "LONG"
    assert saved["exit_levels"]["stop"] == 22400


def test_abort_alert_names_symbol_side_qty_and_reason(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "auto", side="SHORT")
    app, discord = _app(monkeypatch, runner, "auto")
    app._quote_connected = False

    app._start_live_tick_subscription()

    prio = [m for m, p in discord.notes if p]
    assert len(prio) == 1, discord.notes
    msg = prio[0]
    assert "TX00" in msg
    assert "SHORT" in msg
    assert "x1" in msg
    assert "Not connected" in msg
    assert "NOT closed" in msg
    assert app.alerts and "SHORT" in app.alerts[-1][1]
    assert any("NOT closed" in m for _t, m in app.logs)


def test_abort_while_flat_still_alerts(tmp_path, monkeypatch):
    runner = LiveRunner(SilentStrategy(), "TX00", point_value=200,
                        log_dir=str(tmp_path), bot_name="flatbot")
    runner.trading_mode = "auto"
    app, discord = _app(monkeypatch, runner, "auto", confirmed=False)
    app._live_warmup_mode = True
    app._warmup_timeout()
    assert app.orders == []
    prio = [m for m, p in discord.notes if p]
    assert len(prio) == 1 and "FLAT" in prio[0] and "timeout" in prio[0]


def test_warmup_timeout_abort_sends_no_order(tmp_path, monkeypatch):
    # Before bars exist: master already left the sim alone here, but the
    # real stop-close still went out.
    runner = _runner(tmp_path, "auto", warm=False)
    app, discord = _app(monkeypatch, runner, "auto")
    app._live_warmup_mode = True

    app._warmup_timeout()

    assert app.orders == []
    assert runner.broker.position_size == 1
    assert load_session(runner.session_path)["broker"]["position_size"] == 1
    assert any(p for _m, p in discord.notes)


def test_warmup_fetch_rejected_abort_sends_no_order(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "auto", warm=False)
    app, discord = _app(monkeypatch, runner, "auto")
    monkeypatch.setattr(rb, "_broker", SimpleNamespace(
        request_kline=lambda *a: 3021))

    app._live_warmup_via_com(0, 1, 5)

    assert app.orders == []
    assert runner.broker.position_size == 1


def test_no_data_source_abort_sends_no_order(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "auto", warm=False)
    app, discord = _app(monkeypatch, runner, "auto")
    monkeypatch.setattr(rb, "_com_available", False)
    monkeypatch.setattr(rb, "_tv_available", False)

    app._start_live_warmup()

    assert app.orders == []
    assert runner.broker.position_size == 1


def test_tv_warmup_error_abort_sends_no_order(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "auto", warm=False)
    app, discord = _app(monkeypatch, runner, "auto")
    err = SimpleNamespace(message="tv down")
    monkeypatch.setattr(rb, "fetch_tv_dataframe", lambda *a: (None, err))

    class _SyncThread:
        def __init__(self, target, daemon=None):
            self._t = target

        def start(self):
            self._t()
    monkeypatch.setattr(rb.threading, "Thread", _SyncThread)

    app._live_warmup_via_tv(0, 1)
    app.root.drain()

    assert app.orders == []
    assert runner.broker.position_size == 1
    assert app._live_runner is None


def test_paper_abort_keeps_the_paper_position(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "paper")
    app, discord = _app(monkeypatch, runner, "paper", confirmed=False)
    app._quote_connected = False

    app._start_live_tick_subscription()

    assert app.orders == []
    assert runner.broker.position_size == 1
    assert runner.broker.trades == []
    assert load_session(runner.session_path)["broker"]["position_size"] == 1


def test_resume_after_abort_manages_the_position(tmp_path, monkeypatch):
    runner = _runner(tmp_path, "auto")
    app, _discord = _app(monkeypatch, runner, "auto")
    app._quote_connected = False
    app._start_live_tick_subscription()

    saved = load_session(runner.session_path)
    resumed = LiveRunner(SilentStrategy(), "TX00", point_value=200,
                         log_dir=str(tmp_path), bot_name="abortbot")
    resumed.trading_mode = "auto"
    resumed.restore_session(saved)
    assert resumed.broker.position_size == 1
    assert resumed.broker.position_side == OrderSide.LONG
    # The restored stop is armed again (issue #168 path).
    assert resumed._saved_exit_levels is not None
    assert resumed._saved_exit_levels["stop"] == 22400


# ── The user's own stop is unchanged ─────────────────────────────────

@pytest.mark.parametrize("mode", ["auto", "semi_auto"])
def test_user_stop_button_still_flattens(tmp_path, monkeypatch, mode):
    runner = _runner(tmp_path, mode)
    app, discord = _app(monkeypatch, runner, mode)
    runner.state = rb.LiveState.RUNNING

    app._toggle_live()  # the Stop Bot button

    assert len(app.orders) == 1
    args, kwargs = app.orders[0]
    assert args[0] == 1  # SELL closes the LONG
    assert args[2] == "exit"
    assert kwargs.get("new_close") == 1
    # The sim position is force-closed and saved flat, as before.
    assert runner.broker.position_size == 0
    assert len(runner.broker.trades) == 1
    assert load_session(runner.session_path)["broker"]["position_size"] == 0
    assert not [m for m, p in discord.notes if p and "NOT closed" in m]


def test_plain_stop_live_still_flattens(tmp_path, monkeypatch):
    # The headless STOP file and the dashboard "stop" call _stop_live().
    runner = _runner(tmp_path, "auto")
    app, _discord = _app(monkeypatch, runner, "auto")
    app._stop_live()
    assert len(app.orders) == 1
    assert runner.broker.position_size == 0


# ── Runners ──────────────────────────────────────────────────────────

def test_live_runner_stop_keep_position(tmp_path):
    runner = _runner(tmp_path, "auto")
    summary = runner.stop(keep_position=True)
    assert summary["trades"] == 0
    assert runner.broker.position_size == 1
    assert load_session(runner.session_path)["broker"]["position_size"] == 1
    assert runner.state == rb.LiveState.STOPPED


def test_regime_runner_stop_keep_position(tmp_path):
    from tests.test_regime_switching_runner import _make_runner
    runner = _make_runner(tmp_path)
    runner.broker.position_size = 1
    runner.broker.position_side = OrderSide.LONG
    runner.broker.entry_price = 22500
    runner.broker.entry_tag = "Long"
    runner.broker.entry_bar_index = 3
    n_before = len(runner.broker.trades)
    runner.stop(keep_position=True)
    assert runner.broker.position_size == 1
    assert len(runner.broker.trades) == n_before
    assert load_session(runner.session_path)["broker"]["position_size"] == 1


def test_abort_popup_comes_after_teardown(tmp_path, monkeypatch):
    # A modal popup runs a nested Tk loop. Show it only once the bot is
    # fully stopped; the priority Discord alert goes out before that.
    runner = _runner(tmp_path, "auto")
    app, discord = _app(monkeypatch, runner, "auto")
    seen = []
    app._alert = lambda title, msg: seen.append(
        (app._live_runner, runner.state, len(discord.notes)))
    app._quote_connected = False
    app._start_live_tick_subscription()
    assert seen == [(None, rb.LiveState.STOPPED, 1)]


def test_abort_does_not_latch_the_daily_report(tmp_path, monkeypatch):
    """A keep-position abort must not burn this session's report.

    The debounce key is persisted. Emitting while the position stays
    open makes the later exit (session end, or a real stop after
    ``--resume``) a no-op.
    """
    import src.daily_report.report_generator as rg
    monkeypatch.setattr(rg, "_REPORTS_DIR", tmp_path / "reports")

    runner = _runner(tmp_path, "auto")
    runner.broker.trades.append(Trade(
        tag="Long", side=OrderSide.LONG, qty=1,
        entry_price=22000, exit_price=22100,
        entry_bar_index=1, exit_bar_index=2,
        pnl=20000, exit_tag="TP",
        entry_dt="2026-11-13 15:10", exit_dt="2026-11-13 16:00",
    ))
    emitted = []
    runner.on("on_daily_report", emitted.append)
    app, _discord = _app(monkeypatch, runner, "auto")
    app._quote_connected = False

    app._start_live_tick_subscription()

    assert runner.broker.position_size == 1
    assert emitted == []
    assert not runner._last_report_key
    saved = load_session(runner.session_path)
    assert not saved.get("last_report_key")
    assert saved["broker"]["position_size"] == 1

    resumed = LiveRunner(
        SilentStrategy(), "TX00", point_value=200,
        log_dir=str(tmp_path), bot_name="abortbot",
    )
    later = []
    resumed.on("on_daily_report", later.append)
    resumed.restore_session(saved)
    resumed._generate_daily_report()
    assert len(later) == 1
    assert resumed._last_report_key
