"""Evo redesign phase 1a: a grid that selects nothing still runs the retire check.

Before this change the grid no-pick exit returned before
``assess_retirement``. That week was never recorded as an evaluated
relative-gate failure, so an incumbent whose whole knob neighbourhood
had stopped beating it could never reach the #188 "not improvable"
streak. These tests drive the real worker (``_start_evolution_pipeline``)
through the no-pick exit and fail on the pre-fix code.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import run_backtest as rb
from src.evolution.history import evolution_history_path, load_evolution_history
from src.evolution.paper_gate import RELATIVE_EVAL_MARKER, relative_eval_summary
from tests.test_evolution_history import (
    _HIST_CODE,
    _HIST_PLAN,
    _validation_app,
)
from tests.test_evolution_min_design_trades import _InlineThread


class _T:
    def __init__(self, exit_dt: str, pnl: int):
        self.exit_dt = exit_dt
        self.entry_dt = exit_dt
        self.pnl = pnl


def _recent_trades(n: int = 40, pnl: int = -10) -> list[_T]:
    """Dated closes ending yesterday, so the bot is not idle."""
    end = datetime.now() - timedelta(days=1)
    return [_T((end - timedelta(hours=i)).strftime("%Y-%m-%d %H:%M"), pnl)
            for i in range(n)]


def _run_no_pick(monkeypatch, bot_dir, *, paper_gate="relative",
                 changelog_rows=None, base_trades=None):
    from src.evolution.grid_search import GridOutcome
    import src.daily_report.changelog as changelog
    import src.evolution.pipeline as pipeline

    def _no_pick(**kwargs):
        return GridOutcome(
            None, ("plateau / DSR / PBO not met",), (), None,
            "Grid selected nothing.")

    monkeypatch.setattr(
        "src.evolution.grid_search.grid_search_applies",
        lambda *args, **kwargs: True)
    monkeypatch.setattr("src.evolution.grid_search.run_grid_on_bars", _no_pick)
    advanced = []
    monkeypatch.setattr(
        "src.evolution.notify.maybe_advance_watermark",
        lambda *args, **kwargs: advanced.append(args))
    side = types.SimpleNamespace(
        metrics=None, fitness=None, error="",
        result=types.SimpleNamespace(trades=list(base_trades or [])))
    backtests = []

    def _ab(*args, **kwargs):
        backtests.append(args)
        return side

    monkeypatch.setattr(pipeline, "run_ab_backtest", _ab)
    written = []
    monkeypatch.setattr(
        changelog, "append_changelog", lambda **kwargs: written.append(kwargs))
    monkeypatch.setattr(
        changelog, "load_changelog", lambda: list(changelog_rows or []))
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    chat = MagicMock()
    chat.send_message.return_value = _HIST_PLAN
    chat.one_shot.return_value = _HIST_CODE
    chat.max_tokens = 1024
    chat.conversation = []
    app = _validation_app(bot_dir, chat, paper_gate=paper_gate)
    app._grid_in_process = True
    rb.BacktestApp._start_evolution_pipeline(
        app, "preamble", ["design trade"], "", app._result,
        auto_run=False, holdout_days=14, bot_dir=str(bot_dir),
        design_cut_idx=1)
    rows = load_evolution_history(evolution_history_path(bot_dir))
    return app, rows, written, advanced, chat, backtests


def test_relative_no_pick_counts_as_an_evaluated_failed_week(tmp_path, monkeypatch):
    app, rows, written, advanced, chat, backtests = _run_no_pick(
        monkeypatch, tmp_path)
    assert app.errors == [], app.chat
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["reason_kind"] == "grid_no_pick"
    assert row["final_status"] == "fail"
    # The week now counts toward the #188 streak (and never as a pass).
    assert row["retire_week"]["evaluated"] is True
    assert row["retire_week"]["passed"] is False
    # Changelog gets the same relative-gate marker the validation exit writes.
    markers = [w for w in written
               if str(w.get("change_summary", "")).startswith(RELATIVE_EVAL_MARKER)]
    assert len(markers) == 1, written
    assert "passed=false" in markers[0]["change_summary"]
    # Unchanged contract: no model pick, watermark does not move.
    assert chat.send_message.call_count == 0
    assert advanced == []
    # The incumbent is backtested once for the retire check's trades.
    assert len(backtests) == 1


def test_absolute_no_pick_records_no_week(tmp_path, monkeypatch):
    app, rows, written, *_ = _run_no_pick(
        monkeypatch, tmp_path, paper_gate="absolute")
    assert app.errors == [], app.chat
    row = rows[0]
    assert row["reason_kind"] == "grid_no_pick"
    assert row["retire_week"]["evaluated"] is False
    assert not any(
        str(w.get("change_summary", "")).startswith(RELATIVE_EVAL_MARKER)
        for w in written)


def test_fourth_no_pick_week_emits_the_retire_recommendation(tmp_path, monkeypatch):
    """Three prior failed weeks + this no-pick = the 4-week streak."""
    now = datetime.now()
    prior = [
        {"strategy": "HistBase",
         "change_summary": relative_eval_summary(
             False, (now - timedelta(days=7 * k)).strftime("%Y-%m-%dT%H:%M:%S"))}
        for k in (1, 2, 3)
    ]
    app, rows, written, *_ = _run_no_pick(
        monkeypatch, tmp_path, changelog_rows=prior,
        base_trades=_recent_trades(40))
    assert app.errors == [], app.chat
    text = "\n".join(t for _role, t in app.chat)
    assert "RETIRE RECOMMENDATION only" in text, text
    assert "not improvable" in text
    assert any("RETIRE RECOMMENDATION" in str(w.get("change_summary", ""))
               for w in written), written
    assert rows[0]["retire_week"]["evaluated"] is True


def test_grid_failure_still_records_no_week(tmp_path, monkeypatch):
    """A crashed/timed-out sweep has no evidence: not a failed week."""
    from src.evolution.grid_process import GridProcessFailure
    import src.daily_report.changelog as changelog

    monkeypatch.setattr(
        "src.evolution.grid_search.grid_search_applies",
        lambda *args, **kwargs: True)
    import src.evolution.grid_process as gp
    monkeypatch.setattr(
        gp, "run_grid_isolated",
        lambda *a, **k: GridProcessFailure(kind="timeout", pid=1, detail="t"))
    written = []
    monkeypatch.setattr(
        changelog, "append_changelog", lambda **kwargs: written.append(kwargs))
    monkeypatch.setattr(changelog, "load_changelog", lambda: [])
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    chat = MagicMock()
    chat.max_tokens = 1024
    chat.conversation = []
    app = _validation_app(tmp_path, chat, paper_gate="relative")
    rb.BacktestApp._start_evolution_pipeline(
        app, "preamble", ["design trade"], "", app._result,
        auto_run=False, holdout_days=14, bot_dir=str(tmp_path),
        design_cut_idx=1)
    rows = load_evolution_history(evolution_history_path(tmp_path))
    assert rows[0]["reason_kind"] == "grid_failed"
    assert rows[0]["retire_week"]["evaluated"] is False
    assert written == []


# ── the shared helper on its own ─────────────────────────────────────


def test_run_retire_review_never_raises(monkeypatch):
    from src.evolution import retire_review

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(retire_review, "assess_retirement", _boom)
    logs = []
    out = retire_review.run_retire_review(
        base_trades=[], strategy_name="S", version="V", gate_mode="relative",
        verdict_passed=False, retire_config=None, log=logs.append)
    assert out.report_lines == []
    assert any("retire recommendation failed" in line for line in logs)


@pytest.mark.parametrize("mode,expect_week", [("relative", True), ("absolute", False)])
def test_run_retire_review_records_week_only_on_relative(monkeypatch, mode, expect_week):
    import src.daily_report.changelog as changelog
    from src.evolution import retire_review

    written = []
    monkeypatch.setattr(changelog, "append_changelog", lambda **k: written.append(k))
    monkeypatch.setattr(changelog, "load_changelog", lambda: [])
    seen = {}

    real = retire_review.assess_retirement

    def _spy(trades, **kwargs):
        seen["weeks"] = kwargs["weeks"]
        return real(trades, **kwargs)

    monkeypatch.setattr(retire_review, "assess_retirement", _spy)
    retire_review.run_retire_review(
        base_trades=[], strategy_name="S", version="V", gate_mode=mode,
        verdict_passed=False, retire_config=None)
    assert (len(seen["weeks"]) == 1) is expect_week
    assert bool(written) is expect_week
