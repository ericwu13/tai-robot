"""Evo redesign phase 1c: a bot restart does not re-run a FINISHED Saturday slot.

The weekly latch (``_weekly_evo_done``) lived only in memory, so a
restart later on Saturday ran the whole pipeline again. Observed on
Steven: 2026-10-31 08:08-08:25 re-ran every bot (one bot made a second
AI-backed attempt), and 2026-11-07 06:14 re-ran all of them as skips.

The latch is a file in the bot directory with two states:

* ``started`` is written when the pipeline begins. A run that dies before
  writing its history line (crash, hang-kill, failed API fetch) leaves
  it there, and a restart retries once ``SLOT_STARTED_GUARD_S`` has
  passed.
* ``done`` is written with the attempt's history line. A restart then
  skips — unless the attempt ended on a retryable infrastructure error
  (``pipeline_error`` such as an exhausted Gemini 503, ``grid_failed``,
  ``no_chat_client``), which leaves ``started`` in place.

A "restart" here is a brand-new app object pointed at the same bot
directory.
"""

from __future__ import annotations

import json
import os
import types
from datetime import datetime, timedelta

import pytest

import run_backtest as rb
from src.evolution.notify import (
    RETRYABLE_REASON_KINDS,
    SLOT_STARTED_GUARD_S,
    WEEKLY_SLOT_FILENAME,
    complete_weekly_slot,
    mark_weekly_slot,
    weekly_slot_key,
    weekly_slot_taken,
)
from src.live.live_runner import _TZ_TAIPEI, LiveState

_SAT = datetime(2026, 11, 7, 5, 5, tzinfo=_TZ_TAIPEI)
_LATER = _SAT + timedelta(seconds=SLOT_STARTED_GUARD_S + 60)


def _state(bot_dir):
    return json.loads((bot_dir / WEEKLY_SLOT_FILENAME).read_text(encoding="utf-8"))


def test_slot_key_is_the_tpe_iso_week():
    assert weekly_slot_key(_SAT) == "2026-W45"
    # Naive datetimes are TPE wall-clock.
    assert weekly_slot_key(datetime(2026, 11, 7, 6, 14)) == "2026-W45"


def test_guard_window_covers_the_grid_timeout():
    from src.evolution.grid_process import DEFAULT_GRID_TIMEOUT_S
    assert SLOT_STARTED_GUARD_S > DEFAULT_GRID_TIMEOUT_S


def test_started_blocks_only_inside_the_guard_window(tmp_path):
    assert mark_weekly_slot(tmp_path, "2026-W45", now=_SAT) is True
    assert _state(tmp_path)["state"] == "started"
    assert weekly_slot_taken(tmp_path, "2026-W45", now=_SAT + timedelta(minutes=5))
    assert not weekly_slot_taken(tmp_path, "2026-W45", now=_LATER)


def test_done_blocks_for_the_rest_of_the_week(tmp_path):
    mark_weekly_slot(tmp_path, "2026-W45", now=_SAT)
    assert complete_weekly_slot(tmp_path) is True
    assert _state(tmp_path)["state"] == "done"
    assert weekly_slot_taken(tmp_path, "2026-W45", now=_LATER + timedelta(hours=10))
    assert not weekly_slot_taken(tmp_path, "2026-W46", now=_LATER)


def test_complete_without_a_started_slot_is_a_no_op(tmp_path):
    assert complete_weekly_slot(tmp_path) is False
    assert not (tmp_path / WEEKLY_SLOT_FILENAME).exists()


def test_corrupt_or_missing_slot_file_is_not_taken(tmp_path):
    (tmp_path / WEEKLY_SLOT_FILENAME).write_text("{not json", encoding="utf-8")
    assert weekly_slot_taken(tmp_path, "2026-W45", now=_SAT) is False
    assert weekly_slot_taken(tmp_path / "missing", "2026-W45", now=_SAT) is False
    assert weekly_slot_taken(None, "2026-W45", now=_SAT) is False


def test_mark_never_raises_and_does_not_create_the_bot_dir(tmp_path):
    missing = tmp_path / "gone"
    assert mark_weekly_slot(missing, "2026-W45", now=_SAT) is False
    assert not missing.exists()
    assert mark_weekly_slot(None, "2026-W45", now=_SAT) is False


# ── the trigger, driven like the 30 s poll ───────────────────────────


def _app(bot_dir, calls, *, finish_kind=None):
    """``finish_kind`` = reason_kind of the history line the run writes.

    None = the run dies before writing one (crash / hang-kill).
    """
    runner = types.SimpleNamespace(state=LiveState.RUNNING, bot_dir=str(bot_dir))
    app = types.SimpleNamespace(
        _settings={"evolution_auto_pipeline": True},
        _live_runner=runner,
        _weekly_evo_done=None,
        logs=[],
    )
    app._live_log_msg = lambda msg, kind=None: app.logs.append(msg)

    def _evolve(auto_run=False):
        calls.append(auto_run)
        if finish_kind is not None:
            rb._persist_evolution_history(
                str(bot_dir), bot="B", final_status="fail",
                reason_kind=finish_kind, reason="x")
    app._bot_evolution = _evolve
    return app


@pytest.fixture
def clock(monkeypatch):
    now = {"t": _SAT}
    monkeypatch.setattr(rb, "_taipei_now", lambda: now["t"])
    monkeypatch.setattr(rb, "is_market_open", lambda: False)
    monkeypatch.setattr(
        "src.evolution.notify.is_post_close_evolution_window",
        lambda now=None: True)
    return now


def test_finished_run_is_not_rerun_after_restart(tmp_path, clock):
    calls = []
    first = _app(tmp_path, calls, finish_kind="validation")
    rb.BacktestApp._check_weekly_evolution(first)
    rb.BacktestApp._check_weekly_evolution(first)  # 30 s poll
    assert calls == [True]
    assert _state(tmp_path)["state"] == "done"
    clock["t"] = _LATER + timedelta(hours=1)
    rb.BacktestApp._check_weekly_evolution(_app(tmp_path, calls))
    assert calls == [True], "restart re-ran a finished Saturday slot"


def test_skip_lines_also_finish_the_slot(tmp_path, clock):
    calls = []
    rb.BacktestApp._check_weekly_evolution(
        _app(tmp_path, calls, finish_kind="min_design_trades"))
    clock["t"] = _LATER
    rb.BacktestApp._check_weekly_evolution(_app(tmp_path, calls))
    assert calls == [True]


def test_crash_after_start_is_retried_after_restart(tmp_path, clock):
    calls = []
    rb.BacktestApp._check_weekly_evolution(_app(tmp_path, calls, finish_kind=None))
    assert _state(tmp_path)["state"] == "started"
    # Restart inside the guard window: no overlap with a run that may be live.
    clock["t"] = _SAT + timedelta(minutes=10)
    restarted = _app(tmp_path, calls, finish_kind="validation")
    rb.BacktestApp._check_weekly_evolution(restarted)
    assert calls == [True]
    # Same process, once the window has passed: the 30 s poll retries.
    clock["t"] = _LATER
    rb.BacktestApp._check_weekly_evolution(restarted)
    assert calls == [True, True]
    assert _state(tmp_path)["state"] == "done"


@pytest.mark.parametrize("kind", sorted(RETRYABLE_REASON_KINDS))
def test_retryable_failures_leave_the_slot_open(tmp_path, clock, kind):
    calls = []
    rb.BacktestApp._check_weekly_evolution(_app(tmp_path, calls, finish_kind=kind))
    assert _state(tmp_path)["state"] == "started"
    clock["t"] = _LATER
    rb.BacktestApp._check_weekly_evolution(_app(tmp_path, calls))
    assert calls == [True, True]


def test_next_week_runs_again(tmp_path, clock):
    calls = []
    mark_weekly_slot(tmp_path, "2026-W44", now=_SAT - timedelta(days=7))
    complete_weekly_slot(tmp_path)
    rb.BacktestApp._check_weekly_evolution(_app(tmp_path, calls))
    assert calls == [True]


def test_unwritable_slot_still_runs_once_in_process(tmp_path, clock, monkeypatch):
    """A disk error must not silence evolution; the in-memory latch holds."""
    monkeypatch.setattr(
        "src.evolution.notify.mark_weekly_slot", lambda *a, **k: False)
    calls = []
    app = _app(tmp_path, calls)
    rb.BacktestApp._check_weekly_evolution(app)
    rb.BacktestApp._check_weekly_evolution(app)
    assert calls == [True]


def test_two_bots_have_separate_slots(tmp_path, clock):
    calls = []
    a, b = tmp_path / "a", tmp_path / "b"
    os.makedirs(a)
    os.makedirs(b)
    rb.BacktestApp._check_weekly_evolution(_app(a, calls, finish_kind="validation"))
    rb.BacktestApp._check_weekly_evolution(_app(b, calls, finish_kind="validation"))
    assert calls == [True, True]
