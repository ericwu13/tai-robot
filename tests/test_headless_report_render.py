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
