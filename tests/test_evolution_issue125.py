"""issue #125 — watermark advances only after a completed plan with
``action=change``, never at launch / worker start.

Pre-fix, ``_bot_evolution`` called ``save_watermark(bot_dir, cut_idx)``
immediately after dispatching the pipeline (or the plan-only fallback),
so a ``no_change`` / FAIL Saturday burned next week's design-window
delta. CEO package A: defer the save until the plan completes with
``action=change``. Gate thresholds stay unchanged.

These tests are designed to FAIL against the pre-fix glue:
``test_bot_evolution_does_not_save_watermark_at_launch`` and
``test_pipeline_saves_watermark_after_change_plan_only``.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import run_backtest as rb


# ── Policy helper (unit-level; fails pre-fix: helper did not exist) ──

class TestShouldAdvanceWatermark:
    def test_change_advances(self):
        from src.evolution.notify import should_advance_watermark
        assert should_advance_watermark("change") is True
        assert should_advance_watermark("CHANGE") is True
        assert should_advance_watermark(" change ") is True

    def test_no_change_does_not_advance(self):
        from src.evolution.notify import should_advance_watermark
        assert should_advance_watermark("no_change") is False

    def test_missing_or_unknown_does_not_advance(self):
        from src.evolution.notify import should_advance_watermark
        assert should_advance_watermark(None) is False
        assert should_advance_watermark("") is False
        assert should_advance_watermark("fail") is False


class TestMaybeAdvanceWatermark:
    def test_change_writes_file(self, tmp_path):
        from src.evolution.notify import (
            load_watermark, maybe_advance_watermark)
        assert maybe_advance_watermark(
            tmp_path, 42, "change", at="2026-09-19 05:05:00") is True
        assert load_watermark(tmp_path) == {
            "trade_count": 42, "at": "2026-09-19 05:05:00"}

    def test_no_change_does_not_write(self, tmp_path):
        from src.evolution.notify import (
            WATERMARK_FILENAME, load_watermark, maybe_advance_watermark)
        assert maybe_advance_watermark(tmp_path, 42, "no_change") is False
        assert load_watermark(tmp_path) is None
        assert not (tmp_path / WATERMARK_FILENAME).exists()

    def test_none_bot_dir_is_noop(self, tmp_path):
        from src.evolution.notify import maybe_advance_watermark
        assert maybe_advance_watermark(None, 42, "change") is False


# ── Glue control-flow (inspect; these FAIL on pre-fix run_backtest.py) ──

def _pipeline_src() -> str:
    return textwrap.dedent(
        inspect.getsource(rb.BacktestApp._start_evolution_pipeline))


def _evolution_src() -> str:
    return textwrap.dedent(
        inspect.getsource(rb.BacktestApp._bot_evolution))


def test_bot_evolution_does_not_save_watermark_at_launch():
    """FAILS pre-fix: ``save_watermark`` sat at the end of ``_bot_evolution``
    after dispatch, before the plan returned."""
    src = _evolution_src()
    assert "save_watermark" not in src, (
        "issue #125: launching the worker must not advance the watermark")
    assert "maybe_advance_watermark" not in src, (
        "launch path must not save; the pipeline worker owns the save")


def test_pipeline_saves_watermark_after_change_plan_only():
    """FAILS pre-fix: the pipeline never saved; launch did."""
    src = _pipeline_src()
    assert "maybe_advance_watermark" in src
    i_parse = src.index("parse_plan_directives")
    i_no_change = src.index('== "no_change"')
    i_save = src.index("maybe_advance_watermark")
    assert i_parse < i_no_change < i_save, (
        "save must sit AFTER the no_change early-return, so a no_change "
        "plan cannot consume next week's delta")


def test_pipeline_receives_design_cut_and_bot_dir():
    src = _pipeline_src()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "_start_evolution_pipeline")
    args = [a.arg for a in fn.args.args]
    assert "bot_dir" in args, "worker needs the live bot dir to save"
    assert "design_cut_idx" in args, "save to the DESIGN cut, not len(trades)"
    launch = _evolution_src()
    assert "design_cut_idx=cut_idx" in launch
    assert "bot_dir=bot_dir" in launch


def test_gate_thresholds_unchanged_by_this_pr():
    """Package A: defer watermark only — do not soften gates."""
    from src.evolution.fitness import MIN_TRADES
    from src.evolution.pipeline import (
        ABS_PF_FLOOR, HOLDOUT_DD_RATIO_MAX, MIN_EXPRESSED_TRADES)
    assert ABS_PF_FLOOR == 1.0
    assert MIN_EXPRESSED_TRADES == 3
    assert HOLDOUT_DD_RATIO_MAX == 1.2
    assert MIN_TRADES == 30
