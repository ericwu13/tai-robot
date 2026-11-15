"""#285: a headless bot must not rebuild the never-visible Report tab on
the Tk thread that drains COM ticks."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from tests.test_ui_theme import _StubRunner, _TK_OK, _new_root, _trade

_tk_skip = pytest.mark.skipif(not _TK_OK, reason="Tk unavailable")


def _result(n):
    from src.backtest.metrics import calculate_metrics
    trades = [_trade(f"T{i}", (i % 5 - 2) * 1000) for i in range(n)]
    curve, total = [], 0
    for t in trades:
        total += t.pnl
        curve.append(total)
    return SimpleNamespace(trades=trades, equity_curve=curve, strategy_name="UiTest",
                           metrics=calculate_metrics(trades, curve, initial_balance=1_000_000))


@pytest.fixture
def headless():
    from src.live.headless_app import HeadlessBotApp
    root = _new_root()
    root.withdraw()
    try:
        yield HeadlessBotApp(root)
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def _subtree(w):
    return sum(1 + _subtree(c) for c in w.winfo_children())


@_tk_skip
def test_headless_live_results_build_no_report_widgets(headless, monkeypatch):
    import run_backtest as rb
    from src.live.live_runner import LiveState
    app = headless
    app._live_runner = _StubRunner(LiveState.RUNNING)
    calls = []
    monkeypatch.setattr(rb.BacktestApp, "_render_report_view_now",
                        lambda self, *a, **k: calls.append(1))
    before = _subtree(app.report_body)
    for _ in range(5):
        app._report_render_last = 0.0  # defeat the 1 s throttle
        app._display_results(_result(200), None)
    app.root.update()
    assert calls == []
    assert _subtree(app.report_body) == before


@_tk_skip
def test_headless_live_results_still_sync_the_trade_tree(headless):
    from src.live.live_runner import LiveState
    app = headless
    app._live_runner = _StubRunner(LiveState.RUNNING)
    app._display_results(_result(7), None)
    assert len(app.trade_tree.get_children()) == 7


@_tk_skip
def test_gui_render_path_is_unchanged(headless):
    """The base class still rebuilds the cards; only the headless seam skips."""
    import run_backtest as rb
    app = headless
    r = _result(20)
    rb.BacktestApp._render_report_view_now(app, "T", r.metrics, r.trades, None, [])
    assert _subtree(app.report_body) > 30


def test_headless_override_does_not_call_through():
    import run_backtest as rb
    from src.live.headless_app import HeadlessBotApp
    assert HeadlessBotApp._render_report_view is not rb.BacktestApp._render_report_view
    src = inspect.getsource(HeadlessBotApp._render_report_view)
    assert "super()" not in src and "_render_report_view_now" not in src
    assert "_flush_report_render" in inspect.getsource(rb.BacktestApp._render_report_view)


@_tk_skip
def test_headless_reconnect_history_transition_builds_no_report_widgets(headless, monkeypatch, tmp_path):
    """#285 follow-up: after 3003 Ready the resubscribe replays history ticks;
    the first fresh tick fires the history→live transition, which refreshed
    the Report tab. Headless must not rebuild it there either."""
    from datetime import datetime, timedelta
    import run_backtest as rb
    from src.backtest.strategy import BacktestStrategy
    from src.live.live_runner import LiveRunner, LiveState
    from src.market_data.bar_builder import BarBuilder

    class _Q(BacktestStrategy):
        kline_type = 0
        kline_minute = 5

        def on_bar(self, bar, ds, broker):
            return None

        def required_bars(self):
            return 1

    now = datetime(2026, 11, 13, 8, 48, 30)
    monkeypatch.setattr(rb, "_taipei_now", lambda: now.replace(tzinfo=rb._TZ_TAIPEI))

    class _B:
        def request_ticks(self, *_a):
            return 0

        def request_stock_list(self, *_a):
            return 0

    monkeypatch.setattr(rb, "_broker", _B())
    calls = []
    monkeypatch.setattr(rb.BacktestApp, "_render_report_view_now",
                        lambda self, *a, **k: calls.append(1))
    app = headless
    runner = LiveRunner(_Q(), "TMF00", log_dir=str(tmp_path / "logs"))
    runner.state = LiveState.RUNNING
    app._live_runner = runner
    app._live_bar_builder = BarBuilder("TMF00", interval=60)
    app._live_tick_symbol = "TMF00"
    app._live_tick_com_symbol = "TX00"
    app._live_tick_active = False  # keep _drain_tick_queue from rescheduling
    app._resubscribe_ticks()
    app._live_tick_active = False

    def sk(dt):
        return (dt.year * 10000 + dt.month * 100 + dt.day,
                dt.hour * 10000 + dt.minute * 100 + dt.second, 0)

    t = datetime(2026, 11, 13, 8, 45)
    while t < now - timedelta(seconds=150):  # stale history replay
        app._on_com_tick(*sk(t), 2300000, 2300100, 2300000, 1, 0, True)
        t += timedelta(seconds=5)
    assert app._live_history_done is False
    app._on_com_tick(*sk(now - timedelta(seconds=1)), 2300000, 2300100, 2300000, 1, 0, False)
    assert app._live_history_done is True  # transition fired
    assert calls == []
