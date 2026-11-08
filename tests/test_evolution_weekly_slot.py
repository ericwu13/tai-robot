"""Evo redesign phase 1c: a bot restart does not re-run the Saturday slot.

The weekly latch (``_weekly_evo_done``) lived only in memory, so a
restart later on Saturday ran the whole pipeline again. Observed on
Steven: 2026-10-31 08:08-08:25 re-ran every bot (one bot made a second
AI-backed attempt), and 2026-11-07 06:14 re-ran all of them as skips.
The latch is now also a file in the bot directory. A "restart" here is
a brand-new app object pointed at the same bot directory.
"""

from __future__ import annotations

import json
import os
import types
from datetime import datetime

import pytest

import run_backtest as rb
from src.evolution.notify import (
    WEEKLY_SLOT_FILENAME,
    mark_weekly_slot,
    weekly_slot_key,
    weekly_slot_taken,
)
from src.live.live_runner import _TZ_TAIPEI, LiveState

_SAT = datetime(2026, 11, 7, 6, 14, tzinfo=_TZ_TAIPEI)


def test_slot_key_is_the_tpe_iso_week():
    assert weekly_slot_key(_SAT) == "2026-W45"
    # Naive datetimes are TPE wall-clock.
    assert weekly_slot_key(datetime(2026, 11, 7, 6, 14)) == "2026-W45"


def test_mark_then_taken_round_trip(tmp_path):
    assert weekly_slot_taken(tmp_path, "2026-W45") is False
    assert mark_weekly_slot(tmp_path, "2026-W45") is True
    assert weekly_slot_taken(tmp_path, "2026-W45") is True
    assert weekly_slot_taken(tmp_path, "2026-W46") is False
    data = json.loads((tmp_path / WEEKLY_SLOT_FILENAME).read_text(encoding="utf-8"))
    assert data["week"] == "2026-W45"


def test_corrupt_or_missing_slot_file_is_not_taken(tmp_path):
    (tmp_path / WEEKLY_SLOT_FILENAME).write_text("{not json", encoding="utf-8")
    assert weekly_slot_taken(tmp_path, "2026-W45") is False
    assert weekly_slot_taken(tmp_path / "missing", "2026-W45") is False
    assert weekly_slot_taken(None, "2026-W45") is False


def test_mark_never_raises_and_does_not_create_the_bot_dir(tmp_path):
    missing = tmp_path / "gone"
    assert mark_weekly_slot(missing, "2026-W45") is False
    assert not missing.exists()
    assert mark_weekly_slot(None, "2026-W45") is False


def _app(bot_dir, calls):
    runner = types.SimpleNamespace(state=LiveState.RUNNING, bot_dir=str(bot_dir))
    app = types.SimpleNamespace(
        _settings={"evolution_auto_pipeline": True},
        _live_runner=runner,
        _weekly_evo_done=None,
        logs=[],
    )
    app._live_log_msg = lambda msg, kind=None: app.logs.append(msg)
    app._bot_evolution = lambda auto_run=False: calls.append(auto_run)
    return app


@pytest.fixture
def saturday(monkeypatch):
    monkeypatch.setattr(rb, "_taipei_now", lambda: _SAT)
    monkeypatch.setattr(rb, "is_market_open", lambda: False)
    monkeypatch.setattr(
        "src.evolution.notify.is_post_close_evolution_window",
        lambda now=None: True)


def test_restart_in_the_same_week_does_not_rerun(tmp_path, saturday):
    calls = []
    first = _app(tmp_path, calls)
    rb.BacktestApp._check_weekly_evolution(first)
    rb.BacktestApp._check_weekly_evolution(first)  # 30 s poll
    assert calls == [True]
    restarted = _app(tmp_path, calls)  # new process, same bot dir
    rb.BacktestApp._check_weekly_evolution(restarted)
    assert calls == [True], "restart re-ran the Saturday slot"


def test_next_week_runs_again(tmp_path, saturday, monkeypatch):
    calls = []
    mark_weekly_slot(tmp_path, "2026-W44")
    rb.BacktestApp._check_weekly_evolution(_app(tmp_path, calls))
    assert calls == [True]


def test_unwritable_slot_still_runs_once_in_process(tmp_path, saturday, monkeypatch):
    """A disk error must not silence evolution; the in-memory latch holds."""
    monkeypatch.setattr(
        "src.evolution.notify.mark_weekly_slot", lambda *a, **k: False)
    calls = []
    app = _app(tmp_path, calls)
    rb.BacktestApp._check_weekly_evolution(app)
    rb.BacktestApp._check_weekly_evolution(app)
    assert calls == [True]


def test_two_bots_have_separate_slots(tmp_path, saturday):
    calls = []
    a, b = tmp_path / "a", tmp_path / "b"
    os.makedirs(a)
    os.makedirs(b)
    rb.BacktestApp._check_weekly_evolution(_app(a, calls))
    rb.BacktestApp._check_weekly_evolution(_app(b, calls))
    assert calls == [True, True]
