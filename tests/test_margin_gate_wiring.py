"""The real-order entry gate reads one lot's margin from margin_table."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live import margin_table as mt
from tests.test_margin_table import _table


@pytest.fixture(autouse=True)
def _clear_memo():
    mt._MEMO.clear()
    yield
    mt._MEMO.clear()


def _app(monkeypatch, available: str):
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace())
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._futures_account = "ACCT"
    app._equity_refresh_owed = False
    app._account_monitor = SimpleNamespace(rights={"available": available})
    app.decisions, app.blocked = [], []
    app._live_log_msg = lambda *a, **k: None
    app._log_order_decision = lambda action, reason: app.decisions.append(
        (action, reason))
    app._live_runner = SimpleNamespace(
        symbol="TMF00",
        log_blocked_signal=lambda *a: app.blocked.append(a))
    return app


def _send_entry(app):
    return rb.BacktestApp._send_real_order(
        app, 0, "TM0000", "entry", 44800, new_close=0)


def test_entry_blocked_below_current_builtin_margin(monkeypatch):
    # 30,000 passed the stale 16,100 constant; TAIFEX wants 35,050.
    app = _app(monkeypatch, "30000")
    assert _send_entry(app) is False
    assert app.decisions == [(
        "REAL_ORDER_BLOCKED",
        "保證金不足 insufficient margin: available 30,000 < required 35,050 "
        "(內建 2026/08/12)")]
    assert app.blocked[0][3] == "MARGIN_BLOCK"


def test_entry_gate_uses_newer_synced_table(monkeypatch):
    assert mt.write_cache(mt.default_cache_path(), _table("2026/12/01"))
    app = _app(monkeypatch, "36000")
    assert _send_entry(app) is False
    assert "required 37,000 (期交所 2026/12/01)" in app.decisions[0][1]


def test_entry_with_enough_margin_is_not_margin_blocked(monkeypatch):
    app = _app(monkeypatch, "40000")
    _send_entry(app)          # goes on to build the COM order (absent here)
    assert not [d for d in app.decisions if "保證金不足" in d[1]]
    assert not app.blocked


def test_margin_sync_setting_false_skips_download(monkeypatch):
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._settings = {"taifex_margin_sync": False}
    rb.BacktestApp._check_margin_sync(app)
    assert getattr(app, "_margin_sync", None) is None


def test_margin_sync_poll_errors_are_contained(monkeypatch):
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._settings = {}

    class _Boom:
        def poll(self):
            raise RuntimeError("no network stack")
    app._margin_sync = _Boom()
    rb.BacktestApp._check_margin_sync(app)     # must not raise


def test_settings_default_turns_sync_on(monkeypatch, tmp_path):
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    (tmp_path / "settings.yaml").write_text(
        "trading:\n  mode: paper\n", encoding="utf-8")
    assert rb._load_settings()["taifex_margin_sync"] is True
    (tmp_path / "settings.yaml").write_text(
        "trading:\n  taifex_margin_sync: false\n", encoding="utf-8")
    assert rb._load_settings()["taifex_margin_sync"] is False
