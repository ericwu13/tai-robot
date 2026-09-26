"""Issue #153 — weekend evo must reject a September-dominated EMA80 stretch.

FAILS against the pre-fix pipeline: ``decide_month_gates`` did not exist,
Saturday auto judged only ``decide_deep_verdict``, and a PASS wrote
StrategyStore + the STRATEGIES dropdown (the evo1 auto-save).

Calibration (Kai 2026-09-26, read-only): May–Aug cand net 35670 < floor
44100, and September's share of the full-window edge is 161.5%.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from pathlib import Path

import pytest

import run_backtest as rb
from src.backtest.broker import OrderSide, Trade
from src.evolution.pipeline import (
    EvolutionVerdict,
    combine_evolution_verdicts,
    decide_month_gates,
    decide_multifold_verdict,
    eligible_for_validated_pool,
    format_weekend_verdict_block,
    parse_plan_directives,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue153_ema80_kai_nets.json"


def _load() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _trade(exit_dt: str, pnl: int, idx: int) -> Trade:
    return Trade(
        tag="L", side=OrderSide.LONG, qty=1, entry_price=20000,
        exit_price=20000, entry_bar_index=idx, exit_bar_index=idx + 1,
        pnl=pnl, exit_dt=exit_dt,
    )


def _ema80_books(data: dict) -> tuple[list[Trade], list[Trade]]:
    """Prior block is May–Aug lumped into August exits; September is latest.

    Candidate prior is split into a win and a loss so PF matches the
    calibration's 1.238 while the net stays 35670.
    """
    assert (data["cand_prior_gross_win"] - data["cand_prior_gross_loss"]
            == data["cand_prior_net"])
    assert data["cand_prior_net"] < data["floor"]
    assert data["floor"] == data["base_prior_net"] - 5000
    base = [
        _trade("2026-08-20 13:45", data["base_prior_net"], 1),
        _trade("2026-09-15 13:45", data["base_latest_net"], 2),
    ]
    cand = [
        _trade("2026-08-10 13:45", data["cand_prior_gross_win"], 1),
        _trade("2026-08-20 13:45", -data["cand_prior_gross_loss"], 2),
        _trade("2026-09-15 13:45", data["cand_latest_net"], 3),
    ]
    return base, cand


def _ema80_verdict(data: dict | None = None):
    data = data or _load()
    base, cand = _ema80_books(data)
    return decide_month_gates(base, cand, holdout_start=data["holdout_start"])


class TestEma80MustFail:
    """Fail-first oracle. Pre-fix this import / these strings did not exist,
    and the deep-only path would have PASSed the September holdout."""

    def test_prior_months_and_september_share_fail(self):
        data = _load()
        # May–Aug cand 35670 < floor 44100; Sep share 161.5%.
        assert data["cand_prior_net"] == 35670
        assert data["floor"] == 44100
        assert data["share"] == "161.5%"
        v = _ema80_verdict(data)
        text = "\n".join(v.reasons)
        assert not v.passed
        assert "prior-months collapse" in text
        assert "calendar-month dominated" in text
        assert (
            "month ablation: prior-months net +35670 ≥ baseline−5k "
            "(+44100): ✗ (slack −8430) — prior-months collapse"
        ) in text
        assert (
            "latest-month edge share: edge +21830 > 0; "
            "Sep share 35260/21830 = 161.5% ≤ 50%: ✗ — "
            "calendar-month dominated"
        ) in text
        assert f"PF {data['prior_pf']}" in text
        assert "Δ −13430 ≥ 0: ✗" in text
        assert "provisional, never auto-save" in text
        assert v.provisional
        assert not eligible_for_validated_pool(v)

    def test_discord_prefix_shows_multifold_and_month(self):
        """The first 1700 chars (Discord's cap) keep both fail lines and
        a multi-fold reason, even when the walk-forward section is long."""
        from tests.test_evolution_multifold import _winning_folds

        month = _ema80_verdict()
        folds = _winning_folds()
        mf = decide_multifold_verdict(folds)
        assert mf.passed
        deep = EvolutionVerdict(True, ["OOS composite filler " + ("x" * 80)] * 40)
        combined = combine_evolution_verdicts(month, mf, deep)
        block = format_weekend_verdict_block(combined, folds, "TMF00 15m")
        visible = block[:1700]
        assert "multi-fold" in visible
        assert "prior-months collapse" in visible
        assert "calendar-month dominated" in visible
        assert "161.5%" in visible
        assert "35670" in visible and "44100" in visible
        assert any(token in visible for token in (
            "fold majority", "non-empty folds", "divergent folds"))
        assert not combined.passed
        assert not eligible_for_validated_pool(combined)
        assert "nothing was saved" in block


class TestMonthGatesDiscriminate:
    def test_stable_prior_block_passes(self):
        """ema40/atr21/stop2.5/buf0.8 shape from the same calibration:
        May–Aug net above the floor and September share 29.3%."""
        base = [
            _trade("2026-08-20 13:45", 49100, 1),
            _trade("2026-09-15 13:45", -12880, 2),
        ]
        cand = [
            _trade("2026-08-20 13:45", 81530, 1),
            _trade("2026-09-15 13:45", 530, 2),
        ]
        v = decide_month_gates(base, cand, holdout_start="2026-09-11 00:00")
        text = "\n".join(v.reasons)
        assert v.passed
        assert not v.provisional
        assert eligible_for_validated_pool(v)
        assert "prior-months collapse" not in text
        assert "calendar-month dominated" not in text
        assert "29.3%" in text
        assert "13410/45840" in text

    def test_exact_floor_and_half_share_pass(self):
        # floor = 0 − 5000 = −5000; cand prior 1000 clears it.
        # edge = 2000, latest-month Δ = 1000 → share 50.0% (the ceiling).
        base = [
            _trade("2026-08-01 10:00", 0, 1),
            _trade("2026-09-01 10:00", 0, 2),
        ]
        cand = [
            _trade("2026-08-01 10:00", 1000, 1),
            _trade("2026-09-01 10:00", 1000, 2),
        ]
        v = decide_month_gates(base, cand, holdout_start="2026-09-02 00:00")
        text = "\n".join(v.reasons)
        assert v.passed, text
        assert "50.0%" in text
        assert "calendar-month dominated" not in text
        assert "prior-months collapse" not in text

    def test_unknown_holdout_is_provisional(self):
        base = [_trade("2026-08-01 10:00", 1000, 1)]
        cand = [_trade("2026-08-01 10:00", 2000, 1)]
        v = decide_month_gates(base, cand, holdout_start="")
        assert not v.passed
        assert v.provisional
        assert not eligible_for_validated_pool(v)
        assert any("never auto-save" in r for r in v.reasons)


class TestPoolPromotionBlocksStore:
    def test_record_validated_only(self, tmp_path):
        from src.evolution.pool import StrategyPool, record_validated_candidate
        pool = StrategyPool(tmp_path / "pool.db")
        sid = record_validated_candidate(
            pool, name="DynamicExitPullbackStrategyV2Evo1",
            source_code="class X: pass", walkforward_fitness=0.42)
        entry = pool.get(sid)
        assert entry is not None
        assert entry.status == "validated"
        assert entry.source_code == "class X: pass"
        assert entry.walkforward_fitness == pytest.approx(0.42)
        assert pool.count("candidate") == 0
        assert pool.count("validated") == 1


class TestSaturdayKnobMode:
    def test_grid_excludes_ema_80_and_90(self):
        from dynamic_exit_pullback_strategy_v2 import (
            DynamicExitPullbackStrategyV2)
        from src.evolution.knobs import (
            DYNAMIC_EXIT_PULLBACK_GRID, iter_knob_candidates,
            knob_proposal_rejection, read_strategy_knobs, saturday_knob_decision,
        )
        grid = DYNAMIC_EXIT_PULLBACK_GRID
        assert 80 not in grid["ema_period"]
        assert 90 not in grid["ema_period"]
        assert set(grid["ema_period"]) == {40, 50, 60, 70}
        baseline = read_strategy_knobs(DynamicExitPullbackStrategyV2, grid)
        points = iter_knob_candidates(baseline, grid, max_knobs=2)
        assert points
        for point in points:
            assert len(point) <= 2
            assert 80 not in point.values()
            assert 90 not in point.values()
            assert knob_proposal_rejection(point, baseline, grid) == ""
        refused, applied = saturday_knob_decision(
            "DynamicExitPullbackStrategyV2",
            {"ema_period": 80},
            DynamicExitPullbackStrategyV2)
        assert applied is None
        assert "80/90" in refused
        assert "structural/codegen escalate, not auto" in refused
        ok, knobs = saturday_knob_decision(
            "DynamicExitPullbackStrategyV2",
            {"ema_period": 60, "atr_period": 21},
            DynamicExitPullbackStrategyV2)
        assert ok == ""
        assert knobs == {"ema_period": 60, "atr_period": 21}
        too_many, _ = saturday_knob_decision(
            "DynamicExitPullbackStrategyV2",
            {"ema_period": 60, "atr_period": 21, "atr_stop_mult": 2.0},
            DynamicExitPullbackStrategyV2)
        assert "max 2" in too_many
        empty, codegen = saturday_knob_decision(
            "SomeOtherStrategy", {"ema_period": 80}, object)
        assert empty == "" and codegen is None

    def test_rewrite_applies_grid_default_not_codegen(self):
        from src.ai.code_sandbox import load_strategy_from_source
        from src.evolution.knobs import rewrite_knob_source
        source = Path("dynamic_exit_pullback_strategy_v2.py").read_text(
            encoding="utf-8")
        code = rewrite_knob_source(
            source, "DynamicExitPullbackStrategyV2",
            "DynamicExitPullbackStrategyV2Evo1",
            {"ema_period": 60})
        assert "class DynamicExitPullbackStrategyV2Evo1" in code
        assert 'kwargs.get("ema_period", 60)' in code
        assert 'kwargs.get("atr_period", 14)' in code
        cls = load_strategy_from_source(code)
        assert cls.__name__ == "DynamicExitPullbackStrategyV2Evo1"
        assert cls().ema_period == 60
        assert cls().atr_stop_mult == 2.5

    def test_prompt_is_discrete_and_omits_holdout_bars(self):
        from src.evolution.knobs import saturday_knob_directives
        text = saturday_knob_directives()
        assert "80 and 90 are out of auto" in text
        assert "at most 2" in text
        assert "structural/codegen escalate, not auto" in text
        assert "profit_factor_min" not in text
        assert '"ema_period": 60' in text

    def test_directives_keep_knobs(self):
        d = parse_plan_directives(
            '```json\n{"action": "change", "knobs": {"ema_period": 60}}\n```')
        assert d["action"] == "change"
        assert d["criteria"] is None
        assert d["knobs"] == {"ema_period": 60}


def _pipeline_src() -> str:
    return textwrap.dedent(
        inspect.getsource(rb.BacktestApp._start_evolution_pipeline))


def _evolution_src() -> str:
    return textwrap.dedent(inspect.getsource(rb.BacktestApp._bot_evolution))


class TestPipelineWiring:
    """Glue. These fail on the pre-fix worker, which called
    ``decide_deep_verdict`` and then ``_strategy_store.save``."""

    def test_saturday_and_deep_path_ands_multifold_and_month(self):
        src = _pipeline_src()
        assert "run_multifold_validation" in src
        assert "decide_multifold_verdict" in src
        assert "decide_month_gates" in src
        assert "combine_evolution_verdicts" in src
        assert "format_weekend_verdict_block" in src
        assert "eligible_for_validated_pool" in src
        assert src.index("decide_month_gates") < src.index(
            "combine_evolution_verdicts")
        assert src.index("block = format_weekend_verdict_block") < src.index(
            "evolution_verdict(verdict.passed, block)")
        assert "evolution_verdict(verdict.passed, block)" in src

    def test_pass_does_not_autosave_strategy_store(self):
        src = _pipeline_src()
        assert "_strategy_store.save" not in src
        assert "STRATEGIES[saved_as]" not in src
        assert "record_validated_candidate" in src

    def test_knob_refusal_is_before_watermark(self):
        src = _pipeline_src()
        assert src.index("saturday_knob_decision") < src.index(
            "maybe_advance_watermark")
        assert "structural/codegen escalate, not auto" in src
        # The codegen loop is still there for non-grid strategies, and
        # the name check stays inside it (issue #114).
        tree = ast.parse(src)
        loops = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.For) and isinstance(n.target, ast.Name)
            and n.target.id == "attempt"
        ]
        assert len(loops) == 1

    def test_saturday_preamble_uses_knob_directives(self):
        src = _evolution_src()
        assert "saturday_knob_directives" in src
        assert "{knob_note}" in src
        assert "auto_run" in src
