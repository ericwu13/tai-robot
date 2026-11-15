"""#259 blocker probe / acceptance tests: EXIT seq attribution on a same-bar
re-entry. Proposed API: note_real_order_sent(..., open_position=False);
only _stop_live passes open_position=True."""
from __future__ import annotations

import inspect

import pytest

from scripts.dashboard import real_pnl
from src.backtest.broker import Order, OrderSide, SimulatedBroker

PV = 10
SEQ_E1 = "2315600000001"
SEQ_X1 = "2315600000002"
SEQ_X2 = "2315600000003"
SEQ_E2 = "2315600000004"


def _rows(b):
    return b.to_dict()["trades"]


def _open(b, bar, price=45000, side=OrderSide.LONG, tag="L"):
    b.queue_entry(Order(tag=tag, side=side, qty=1))
    b.on_bar_close(bar_index=bar, close=price)


def _caller_exit_bar(b):           # what run_backtest._send_real_order passes
    return b.trades[-1].exit_bar_index if b.trades else -1


def _stamp_stop_close(b, seq):
    kw = {}
    if "open_position" in inspect.signature(b.note_real_order_sent).parameters:
        kw["open_position"] = True
    else:
        pytest.fail("note_real_order_sent has no open_position flag")
    return b.note_real_order_sent("exit", seq, _caller_exit_bar(b), **kw)


def _same_bar_stop_then_reentry():
    b = SimulatedBroker(point_value=PV)              # LiveRunner default on_close
    _open(b, bar=5)
    assert b.note_real_order_sent("entry", SEQ_E1, 5)
    b.queue_exit(Order(tag="SL", side=OrderSide.SHORT, qty=1, stop=44900,
                       from_entry="L"))
    b.check_exits(20, 44950, 44960, 44850, 44880)     # stop fills on bar 20
    assert b.position_size == 0
    _open(b, bar=20, price=44880, side=OrderSide.SHORT, tag="S")  # same bar
    assert b.position_size > 0 and b.entry_bar_index == 20
    # root.after(0) -> TRADE_CLOSE real exit is stamped only now
    return b


def test_same_bar_stop_reentry_exit_seq_stays_on_closed_trade():
    b = _same_bar_stop_then_reentry()
    assert b.note_real_order_sent("exit", SEQ_X1, _caller_exit_bar(b))
    assert b.trades[0].exit_order_seq == SEQ_X1
    assert b.pending_exit_order_seq == ""
    # paper re-entry (entry blocked by BLOCK_FILL_PENDING), closes later
    b.force_close(25, 44800)
    t1, t2 = _rows(b)
    assert real_pnl.is_real_order(t1)
    assert t2["exit_order_seq"] == "" and t2["entry_order_seq"] == ""
    assert not real_pnl.is_real_order(t2)
    assert real_pnl.classify(t2, PV)["status"] != "missing"


def test_same_bar_market_close_plus_entry_in_one_on_bar_close():
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.note_real_order_sent("entry", SEQ_E1, 5)
    b.queue_market_close("close", "L")
    b.queue_entry(Order(tag="S", side=OrderSide.SHORT, qty=1))
    b.on_bar_close(bar_index=9, close=44900)          # reversal bar
    assert b.position_size > 0 and b.trades[-1].exit_bar_index == 9
    assert b.note_real_order_sent("exit", SEQ_X1, _caller_exit_bar(b))
    assert b.trades[0].exit_order_seq == SEQ_X1
    b.force_close(14, 44800)
    assert b.trades[1].exit_order_seq == ""
    assert not real_pnl.is_real_order(_rows(b)[1])


def test_reentry_trade_keeps_its_own_exit_seq():
    b = _same_bar_stop_then_reentry()
    b.note_real_order_sent("exit", SEQ_X1, _caller_exit_bar(b))
    assert b.note_real_order_sent("entry", SEQ_E2, 20)
    b.force_close(25, 44800)
    assert b.note_real_order_sent("exit", SEQ_X2, _caller_exit_bar(b))
    assert (b.trades[1].entry_order_seq, b.trades[1].exit_order_seq) == (SEQ_E2, SEQ_X2)
    assert b.trades[0].exit_order_seq == SEQ_X1


def test_late_exit_stamp_after_next_bar_entry_lands_on_closed_trade():
    """Semi-auto confirm dialog / deferred TRADE_CLOSE: the exit for trade 1
    goes out after a paper entry opened on a later bar."""
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    _open(b, bar=10)
    assert b.note_real_order_sent("exit", SEQ_X1, 9)
    assert b.trades[0].exit_order_seq == SEQ_X1 and b.pending_exit_order_seq == ""


def test_stop_close_flag_parks_seq_on_open_position():
    """The #258 P2-1 case must keep working, now via the explicit flag."""
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)                           # paper trade 1
    _open(b, bar=12)
    b.note_real_order_sent("entry", SEQ_E1, 12)
    assert _stamp_stop_close(b, SEQ_X1)
    assert b.trades[0].exit_order_seq == ""
    b.force_close(15, 44800)
    assert (b.trades[1].entry_order_seq, b.trades[1].exit_order_seq) == (SEQ_E1, SEQ_X1)


def test_stop_close_flag_when_flat_is_rejected():
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    assert not _stamp_stop_close(b, SEQ_X1)
    assert b.trades[0].exit_order_seq == ""


# Guards (pass before and after; behaviour must not change)
def test_guard_normal_exit_without_reentry_unchanged():
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.note_real_order_sent("entry", SEQ_E1, 5)
    b.force_close(9, 44900)
    assert b.note_real_order_sent("exit", SEQ_X1, 9)
    assert not b.note_real_order_sent("exit", SEQ_X2, 9)   # first wins
    assert not b.note_real_order_sent("exit", SEQ_X2, 8)   # wrong bar
    assert b.trades[0].exit_order_seq == SEQ_X1


def _send_real_order_calls(tree):
    """Every ``self._send_real_order(...)`` call: (function, action, flag).

    ``flag`` is True/False when the ``exit_open_position`` keyword is a
    constant, None when the keyword is omitted, or the string
    ``"non-constant"`` otherwise. Nested functions keep their own name.
    """
    import ast

    calls = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.fn = []

        def visit_FunctionDef(self, node):
            self.fn.append(node.name)
            self.generic_visit(node)
            self.fn.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "_send_real_order":
                flag = None
                for kw in node.keywords:
                    if kw.arg != "exit_open_position":
                        continue
                    if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, bool):
                        flag = kw.value.value
                    else:
                        flag = "non-constant"
                action = None
                if len(node.args) >= 3 and isinstance(node.args[2], ast.Constant):
                    action = node.args[2].value
                calls.append((self.fn[-1] if self.fn else "", action, flag))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return calls


def test_only_stop_live_passes_exit_open_position(monkeypatch):
    """Stop-close and the manual-flatten confirm are the True call sites.

    A strategy-exit confirm through the same dialog leaves the flag off.
    The source walk covers call sites this runtime mock does not reach.
    """
    import ast
    from pathlib import Path
    from types import SimpleNamespace

    import run_backtest as rb
    from src.live.trading_guard import TradingGuard

    tree = ast.parse(Path(rb.__file__).read_text(encoding="utf-8"))
    calls = _send_real_order_calls(tree)
    flagged = [c for c in calls if c[2] is True]
    assert ("_stop_live", "exit", True) in flagged
    assert ("on_confirm", None, True) in flagged
    assert len(flagged) == 2
    for fn, action, flag in calls:
        if flag is True:
            assert (fn, action) in {("_stop_live", "exit"), ("on_confirm", None)}
        elif action == "exit":
            assert flag in (None, False)

    recorded = []

    def _record(buy_sell, order_symbol, action_type, sim_price,
                new_close=2, exit_type="", exit_limit=None,
                exit_open_position=False):
        recorded.append({
            "action_type": action_type,
            "exit_open_position": exit_open_position,
        })
        return True

    def _order_app(guard):
        return SimpleNamespace(
            _live_runner=SimpleNamespace(
                symbol="TMF00",
                log_blocked_signal=lambda *a, **k: None,
            ),
            _trading_guard=guard,
            _trading_mode="auto",
            _live_log_msg=lambda *a, **k: None,
            _log_order_decision=lambda *a, **k: None,
            _is_settlement_no_entry_window=lambda *a, **k: False,
            _SETTLEMENT_NO_ENTRY_MINUTES=60,
            _send_real_order=_record,
            _retry_force_close=lambda *a, **k: None,
            _FORCE_CLOSE_RETRY_DELAYS=[5000, 10000, 15000],
        )

    guard = TradingGuard(daily_loss_limit=10000)
    guard.real_entry_confirmed = True
    app = _order_app(guard)
    rb.BacktestApp._handle_semi_auto_order(
        app, {"action": "TRADE_CLOSE", "side": "LONG", "price": 44900,
              "exit_type": "stop"})
    rb.BacktestApp._handle_semi_auto_order(
        app, {"action": "FORCE_CLOSE", "side": "LONG", "price": 44900})
    rb.BacktestApp._retry_force_close(
        app, 1, "TMFL5", 44900, attempt=1, max_attempts=3, last_error="x")

    class _Dlg:
        def title(self, *a, **k):
            return None

        def attributes(self, *a, **k):
            return None

        def resizable(self, *a, **k):
            return None

        def protocol(self, *a, **k):
            return None

        def after(self, *a, **k):
            return 1

        def after_cancel(self, *a, **k):
            return None

        def winfo_exists(self):
            return True

        def destroy(self):
            return None

    class _Node:
        def __init__(self, *a, **k):
            self.command = k.get("command")

        def pack(self, *a, **k):
            return self

        def grid(self, *a, **k):
            return self

        def config(self, *a, **k):
            return None

        def configure(self, *a, **k):
            return None

    confirmed = []

    def _button(*a, **k):
        node = _Node(*a, **k)
        if "Confirm" in str(k.get("text", "")):
            confirmed.append(k.get("command"))
        return node

    monkeypatch.setattr(rb.tk, "Toplevel", lambda *a, **k: _Dlg())
    monkeypatch.setattr(rb.ttk, "Frame", _Node)
    monkeypatch.setattr(rb.ttk, "Label", _Node)
    monkeypatch.setattr(rb.ttk, "Progressbar", _Node)
    monkeypatch.setattr(rb.ttk, "Button", _button)
    monkeypatch.setattr(rb, "apply_window_theme", lambda *a, **k: None)
    monkeypatch.setattr(rb, "place_dialog", lambda *a, **k: None)
    monkeypatch.setattr(rb, "_discord", None)

    dialog_app = _order_app(guard)
    dialog_app.root = SimpleNamespace()
    dialog_app._order_confirm_timer_id = None
    dialog_app._order_confirm_dlg = None
    dialog_app._dismiss_order_dialog = lambda reason: None
    rb.BacktestApp._show_order_confirm_dialog(
        dialog_app, 1, "TMFL5", "平倉賣 CLOSE SELL", 44800, "exit", new_close=1)
    assert confirmed and confirmed[0] is not None
    confirmed[0]()

    class _Widget:
        def config(self, **k):
            return None

        def set(self, value):
            self.value = value

    stop_app = rb.BacktestApp.__new__(rb.BacktestApp)
    stop_app.root = SimpleNamespace(
        after_cancel=lambda *a, **k: None, after=lambda *a, **k: None)
    stop_app._real_account_poll_id = None
    stop_app._live_poll_id = None
    stop_app._reconnect_timer_id = None
    stop_app._conn_monitor = SimpleNamespace(reset=lambda: None)
    stop_app._warmup_timeout_id = None
    stop_app._fill_poll_timer_id = None
    stop_app._trading_guard = SimpleNamespace(
        fill_pending=False, real_entry_confirmed=True, reset=lambda: None)
    stop_app._live_tick_count = 0
    stop_app._live_history_done = False
    stop_app._live_tick_active = False
    stop_app._tick_watchdog = SimpleNamespace(reset=lambda: None)
    stop_app._live_chart = None
    stop_app._trading_mode = "auto"
    stop_app._live_runner = SimpleNamespace(
        symbol="TMF00",
        broker=SimpleNamespace(
            position_size=1, position_side=SimpleNamespace(value="LONG")),
        csv_logger=None,
        suppress_strategy=False,
        stop=lambda: {"trades": 0, "pnl": 0, "bars_1m": 0, "bars_agg": 0},
    )
    stop_app._get_latest_price = lambda: (44800, "tick")
    stop_app._live_log_msg = lambda *a, **k: None
    stop_app._log_order_decision = lambda *a, **k: None
    stop_app._send_real_order = _record
    stop_app._run_evolution_check_after_report = lambda: None
    stop_app._update_live_results = lambda: None
    stop_app._update_live_status = lambda: None
    stop_app._position_ledger = None
    stop_app._regime_manager = None
    stop_app._quote_connected = False
    stop_app._update_manual_order_buttons = lambda: None
    for name in ("btn_deploy", "chart_tf_combo", "chart_tf_var", "btn_api",
                 "btn_tv", "btn_update", "symbol_combo", "strategy_combo",
                 "bot_name_var", "trading_mode_var", "mode_combo"):
        setattr(stop_app, name, _Widget())
    stop_app.set_status = lambda *a, **k: None
    rb.BacktestApp._stop_live(stop_app)

    exits = [c for c in recorded if c["action_type"] == "exit"]
    assert len(exits) == 5
    assert [c["exit_open_position"] for c in exits] == [False, False, False, False, True]


def _install_fake_confirm_dialog(monkeypatch):
    """Replace the Tk confirm dialog with a button that records on_confirm."""
    import run_backtest as rb

    confirmed = []

    class _Dlg:
        def title(self, *a, **k):
            return None

        def attributes(self, *a, **k):
            return None

        def resizable(self, *a, **k):
            return None

        def protocol(self, *a, **k):
            return None

        def after(self, *a, **k):
            return 1

        def after_cancel(self, *a, **k):
            return None

        def winfo_exists(self):
            return True

        def destroy(self):
            return None

    class _Node:
        def __init__(self, *a, **k):
            self.command = k.get("command")

        def pack(self, *a, **k):
            return self

        def grid(self, *a, **k):
            return self

        def config(self, *a, **k):
            return None

        def configure(self, *a, **k):
            return None

    def _button(*a, **k):
        node = _Node(*a, **k)
        if "Confirm" in str(k.get("text", "")):
            confirmed.append(k.get("command"))
        return node

    monkeypatch.setattr(rb.tk, "Toplevel", lambda *a, **k: _Dlg())
    monkeypatch.setattr(rb.ttk, "Frame", _Node)
    monkeypatch.setattr(rb.ttk, "Label", _Node)
    monkeypatch.setattr(rb.ttk, "Progressbar", _Node)
    monkeypatch.setattr(rb.ttk, "Button", _button)
    monkeypatch.setattr(rb, "apply_window_theme", lambda *a, **k: None)
    monkeypatch.setattr(rb, "place_dialog", lambda *a, **k: None)
    monkeypatch.setattr(rb, "_discord", None)
    return confirmed


def _com_order_app(broker, monkeypatch, *, seq):
    """App whose real ``_send_real_order`` accepts one mocked COM order."""
    import sys
    import types
    from types import SimpleNamespace

    import run_backtest as rb
    from src.live.account_monitor import AccountMonitor

    def send(*_a, **_k):
        return (seq, 0)

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
    monkeypatch.setattr(rb, "skO", SimpleNamespace(SendFutureOrderCLR=send))

    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = SimpleNamespace(after=lambda *a, **k: 1, after_cancel=lambda *a, **k: None)
    app._futures_account = "ACCT"
    app.login_user_var = SimpleNamespace(get=lambda: "user")
    app._trading_mode = "semi_auto"
    app._trading_guard = SimpleNamespace(
        on_entry_sent=lambda: None,
        on_exit_sent=lambda: None,
        on_fill_pending=lambda *_a: None,
    )
    app._fill_tracker = SimpleNamespace(register_order=lambda *_a, **_k: None)
    app._live_log_msg = lambda *a, **k: None
    app._order_confirm_timer_id = None
    app._order_confirm_dlg = None
    app._reply_poller = None
    app._position_ledger = None
    app._last_real_order_side = None
    app._get_latest_price = lambda: (44800, "tick")
    app._live_runner = SimpleNamespace(
        symbol="TMF00",
        broker=broker,
        csv_logger=None,
        strategy_display_name="Probe",
    )
    app._account_monitor = AccountMonitor()
    return app


def _confirm_manual_flatten(broker, monkeypatch, *, seq):
    import run_backtest as rb

    confirmed = _install_fake_confirm_dialog(monkeypatch)
    app = _com_order_app(broker, monkeypatch, seq=seq)
    rb.BacktestApp._manual_close(app)
    assert confirmed and confirmed[0] is not None
    confirmed[0]()
    return app


def test_manual_flatten_while_open_parks_seq_on_the_open_position(monkeypatch):
    """The CLOSE button sends while the sim position is still open.

    The seq parks on that position. The previous trade stays paper, and
    the fill price registered against its exit bar does not land on it.
    """
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    _open(b, bar=12)
    prev = b.trades[-1].exit_bar_index
    _confirm_manual_flatten(b, monkeypatch, seq=SEQ_X1)
    assert b.pending_exit_order_seq == SEQ_X1
    row = _rows(b)[0]
    assert row["exit_order_seq"] == ""
    assert not real_pnl.is_real_order(row)
    assert real_pnl.classify(row, PV)["status"] != "missing"
    assert b.try_set_real_exit_price(44700, exit_bar_index=prev) is False
    assert b.trades[-1].real_exit_price == 0
    assert b.trades[-1].exit_order_seq == ""


def test_manual_flatten_while_flat_stamps_nothing(monkeypatch):
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    app = _com_order_app(b, monkeypatch, seq=SEQ_X1)
    app._last_real_order_side = 0          # book unknown: still offers a close
    confirmed = _install_fake_confirm_dialog(monkeypatch)
    import run_backtest as rb
    rb.BacktestApp._manual_close(app)
    assert confirmed and confirmed[0] is not None
    confirmed[0]()
    assert b.pending_exit_order_seq == ""
    assert b.trades[-1].exit_order_seq == ""
    assert not real_pnl.is_real_order(_rows(b)[0])


def test_strategy_exit_confirm_after_same_bar_reentry_stamps_closed_trade(monkeypatch):
    """A strategy-exit confirm is not a manual flatten. The sim has already
    closed, even if a same-bar re-entry is open, so the seq stays on the
    closed trade."""
    import run_backtest as rb

    b = _same_bar_stop_then_reentry()
    confirmed = _install_fake_confirm_dialog(monkeypatch)
    app = _com_order_app(b, monkeypatch, seq=SEQ_X1)
    rb.BacktestApp._show_order_confirm_dialog(
        app, 1, "TMFL5", "平倉賣 CLOSE SELL", 44880, "exit", new_close=1)
    assert confirmed and confirmed[0] is not None
    confirmed[0]()
    assert b.trades[-1].exit_order_seq == SEQ_X1
    assert b.pending_exit_order_seq == ""


def test_only_stop_live_and_manual_flatten_pass_exit_open_position(monkeypatch):
    """The flag is where the order came from. Only stop-close and the
    manual-flatten confirm pass it."""
    import ast
    from pathlib import Path
    from types import SimpleNamespace

    import run_backtest as rb

    tree = ast.parse(Path(rb.__file__).read_text(encoding="utf-8"))
    calls = _send_real_order_calls(tree)
    flagged = [c for c in calls if c[2] is True]
    assert ("_stop_live", "exit", True) in flagged
    assert ("on_confirm", None, True) in flagged
    assert len(flagged) == 2
    for fn, action, flag in calls:
        if flag is True:
            assert (fn, action) in {("_stop_live", "exit"), ("on_confirm", None)}

    recorded = []

    def _record(buy_sell, order_symbol, action_type, sim_price,
                new_close=2, exit_type="", exit_limit=None,
                exit_open_position=False):
        recorded.append(exit_open_position)
        return True

    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    confirmed = _install_fake_confirm_dialog(monkeypatch)
    app = _com_order_app(b, monkeypatch, seq=SEQ_X1)
    app._send_real_order = _record
    rb.BacktestApp._manual_close(app)
    assert confirmed and confirmed[0] is not None
    confirmed[0]()
    # A strategy-exit confirm through the same dialog leaves the flag off.
    confirmed.clear()
    rb.BacktestApp._show_order_confirm_dialog(
        app, 1, "TMFL5", "平倉", 45000, "exit", new_close=1)
    confirmed[0]()
    assert recorded == [True, False]


def _parked_open(seq=SEQ_X2):
    """Paper trade closed on bar 9, a new position open, exit seq parked."""
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    _open(b, bar=12)
    assert b.note_real_order_sent("exit", seq, b.trades[-1].exit_bar_index,
                                  open_position=True)
    return b


def _set_exit_price(b, price, bar, seq):
    """Pass ``seq`` once try_set_real_exit_price accepts it.

    On the pre-fix broker the keyword is absent, so the call is the
    bar-only write those tests have to fail.
    """
    if "seq" in inspect.signature(b.try_set_real_exit_price).parameters:
        return b.try_set_real_exit_price(
            price, exit_bar_index=bar, seq=seq)
    return b.try_set_real_exit_price(price, exit_bar_index=bar)


def test_parked_seq_fill_then_close_is_a_real_price():
    """Reed: the stop/manual-flatten fill arrives before the sim close."""
    b = _parked_open(SEQ_X1)
    assert _set_exit_price(b, 44700, b.trades[-1].exit_bar_index, SEQ_X1)
    b.force_close(15, 44800)
    closed = b.trades[-1]
    assert closed.exit_order_seq == SEQ_X1
    assert closed.real_exit_price == 44700
    assert closed.real_exit_estimated is False


def test_parked_seq_close_then_fill_is_a_real_price():
    """The same fill, arriving after the sim force-close, is still real."""
    b = _parked_open(SEQ_X1)
    send_bar = b.trades[-1].exit_bar_index
    b.force_close(15, 44800)
    assert _set_exit_price(b, 44700, send_bar, SEQ_X1)
    closed = b.trades[-1]
    assert closed.exit_order_seq == SEQ_X1
    assert closed.real_exit_price == 44700
    assert closed.real_exit_estimated is False


def test_late_fill_for_the_closed_trade_during_a_parked_exit():
    """X's own exit seq still prices trades[-1] while a newer exit is parked."""
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    assert b.note_real_order_sent("exit", SEQ_X1, 9)
    _open(b, bar=12)
    assert b.note_real_order_sent("exit", SEQ_X2, 9, open_position=True)
    assert _set_exit_price(b, 44700, 9, SEQ_X1)
    assert b.trades[-1].real_exit_price == 44700
    assert b.trades[-1].exit_order_seq == SEQ_X1
    assert b.pending_exit_order_seq == SEQ_X2
    assert getattr(b, "pending_real_exit_price", None) is None


def test_mark_estimated_does_not_touch_the_previous_trade_while_parked():
    b = _parked_open()
    assert b.mark_real_exit_estimated() is False
    assert b.trades[-1].real_exit_estimated is False


def test_parked_exit_price_round_trips_and_old_files_default_none():
    b = _parked_open(SEQ_X1)
    assert _set_exit_price(b, 44700, 9, SEQ_X1)
    restored = SimulatedBroker.from_dict(b.to_dict())
    assert restored.pending_exit_order_seq == SEQ_X1
    assert restored.pending_real_exit_price == 44700
    restored.force_close(15, 44800)
    assert restored.trades[-1].real_exit_price == 44700
    assert restored.trades[-1].real_exit_estimated is False
    old = b.to_dict()
    old.pop("pending_real_exit_price", None)
    old.pop("pending_real_exit_dt", None)
    legacy = SimulatedBroker.from_dict(old)
    assert legacy.pending_exit_order_seq == SEQ_X1
    assert legacy.pending_real_exit_price is None


def _flatten_confirm_app(monkeypatch, broker, seq, raw):
    """Auto-mode app whose exit confirm can poll GetFulfillReport."""
    import run_backtest as rb
    from datetime import datetime
    from types import SimpleNamespace

    from src.live.fill_poller import FillPoller
    from src.live.fill_report import RealFillTracker
    from src.live.trading_guard import TradingGuard
    from tests.test_issue_168_restore_fidelity import _Root
    from tests.test_issue_218 import gfr

    send = datetime(2026, 10, 30, 4, 16, 6)
    guard = TradingGuard()
    guard.on_fill_pending("exit")
    poller = FillPoller(guard)
    poller.start("exit", 1, com_available=True, entry_bar_index=12)
    tracker = RealFillTracker()
    tracker.register_order(
        seq, "exit", bar_index=9, sim_price=44800,
        product="TM0000", side="S", new_close="O", send_time=send)
    tracker._orders[seq]["book_no"] = "a1006"

    class _Discord:
        enabled = True

        def __init__(self):
            self.prices = []

        def fill_confirmed(self, action_type, fill_price="", strategy="",
                           estimated=False):
            self.prices.append(str(fill_price))

        def exit_fill_unconfirmed(self, *args, **kwargs):
            self.prices.append("unconfirmed")

    discord = _Discord()
    calls = []

    def _report(*_a, **_k):
        calls.append(seq)
        return raw if raw is not None else gfr(price="44700.0000")

    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(rb, "_log", lambda msg: None)
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(GetFulfillReport=object()))
    monkeypatch.setattr(rb, "call_com", _report)

    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = _Root()
    app._fill_poller = poller
    app._trading_guard = guard
    app._fill_poll_timer_id = None
    app._fill_tracker = tracker
    app._futures_account = "0000"
    app.login_user_var = SimpleNamespace(get=lambda: "user")
    app._last_exit_order_seq = seq
    app._exit_fill_stopped = True
    app._exit_fill_retry_timer = None
    app._exit_fill_retry_ctx = None
    app._live_runner = SimpleNamespace(
        broker=broker, symbol="TMF00", _auto_save_session=lambda: None)
    app._live_log_msg = lambda *a, **k: None
    app._log_order_decision = lambda *a, **k: None
    app._active_strategy_name = lambda: "S"
    app._session_end_exit_pending = False
    return app, discord, calls


def test_auto_flatten_reads_the_parked_price_not_the_earlier_trade(monkeypatch):
    """An earlier trade is already priced. The open flatten's seq is parked
    and still has no price. Discord and the broker read must not use the
    earlier price, and the report retry must still run."""
    from tests.test_issue_218 import _fire_one

    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    b.trades[-1].real_exit_price = 44000
    b.trades[-1].exit_order_seq = SEQ_X1
    _open(b, bar=12)
    assert b.note_real_order_sent(
        "exit", SEQ_X2, 9, open_position=True)
    assert b.pending_real_exit_price is None
    app, discord, calls = _flatten_confirm_app(monkeypatch, b, SEQ_X2, None)
    assert app._real_exit_price_from_broker() == 0
    app._on_fill_confirmed()
    assert app.root.pending, "GetFulfillReport retry was skipped"
    assert discord.prices == []
    _fire_one(app.root)
    assert calls, "report poll did not run"
    assert b.pending_real_exit_price == 44700
    assert b.trades[-1].real_exit_price == 44000
    assert discord.prices == ["44700"]


def test_flat_flatten_mismatched_seq_does_not_reprice_the_closed_trade(caplog):
    import logging

    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    assert b.note_real_order_sent("exit", SEQ_X1, 9)
    assert b.position_size == 0
    assert b.pending_exit_order_seq == ""
    with caplog.at_level(logging.WARNING):
        assert b.try_set_real_exit_price(44700, exit_bar_index=9, seq=SEQ_X2) is False
        assert b.try_set_real_exit_price(44700, exit_bar_index=9, seq=SEQ_X2) is False
    assert b.trades[-1].real_exit_price == 0
    warnings = [r for r in caplog.records
                if r.levelno >= logging.WARNING and SEQ_X2 in r.getMessage()]
    assert len(warnings) == 1
    assert "44700" in warnings[0].getMessage()


def test_matching_seq_and_empty_exit_seq_still_bar_match():
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    assert b.note_real_order_sent("exit", SEQ_X1, 9)
    assert b.try_set_real_exit_price(44700, exit_bar_index=9, seq=SEQ_X1)
    assert b.trades[-1].real_exit_price == 44700

    bare = SimulatedBroker(point_value=PV)
    _open(bare, bar=5)
    bare.force_close(9, 44900)
    assert bare.trades[-1].exit_order_seq == ""
    assert bare.try_set_real_exit_price(44600, exit_bar_index=9, seq=SEQ_X2)
    assert bare.trades[-1].real_exit_price == 44600


def test_duplicate_poll_of_a_priced_parked_fill_is_resolved(monkeypatch):
    import time

    import run_backtest as rb

    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    _open(b, bar=12)
    assert b.note_real_order_sent("exit", SEQ_X2, 9, open_position=True)
    assert b.try_set_real_exit_price(44700, exit_bar_index=9, seq=SEQ_X2)
    app, discord, calls = _flatten_confirm_app(monkeypatch, b, SEQ_X2, None)
    app._exit_fill_stopped = False
    app._exit_fill_retry_ctx = {
        "action_type": "exit",
        "fallback_price": "44800",
        "strategy": "S",
        "attempt": 0,
        "started": time.monotonic(),
    }
    rb.BacktestApp._poll_exit_fill_step(app)
    assert calls == []
    assert discord.prices == ["44700"]
    assert b.pending_real_exit_price == 44700
    assert app._exit_fill_retry_ctx is None
