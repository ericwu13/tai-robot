"""issue #114 — three independent defects that between them froze the
weekly evolution pipeline.

A. The drawdown gate's zero sentinel.
   ``calculate_metrics`` derives ``max_drawdown_pct`` from
   ``peak = initial_balance`` (default 0). The evolution backtests never
   pass a balance, so a window whose cumulative P&L never rises above 0
   reports MaxDD% = 0.00 — a "perfect" value — while a window with a
   tiny peak reports absurd percentages (107.9% / 174.5% observed on
   real data). Every baseline-RELATIVE drawdown gate then rejects any
   candidate with a drawdown whenever the baseline never went positive.
   The absolute ``max_drawdown`` (currency) carries no sentinel and is
   comparable on the same bars, so the relative gates use it.

B. The plan prompt's small-sample rule pointed at the wrong denominator.
   The trade list the model receives is a DELTA (only trades new since
   the previous watermark), but the rule said "fewer than 30 trades →
   no change". The model applied it to the short list and answered
   no_change nearly every week.

C. Nothing asserted the generated class is actually named
   ``candidate_name``. ``load_strategy_from_source`` returns the first
   ``BacktestStrategy`` subclass whatever it is called, and the PASS
   path saves by ``candidate_cls.__name__`` — a model that kept the base
   class name would overwrite the LIVE strategy's stored source and its
   registry entry.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

import run_backtest as rb
from src.backtest.strategy import BacktestStrategy
from src.evolution.pipeline import (
    DeepResult,
    decide_deep_verdict,
    decide_verdict,
)
from tests.test_evolution_pipeline import _ab


# Metrics for these two lists (verified against calculate_metrics):
#   baseline: PF 0.11 | max_drawdown 1,250 | max_drawdown_pct 0.00 ← sentinel
#   candidate: PF 5.60 | max_drawdown   150 | max_drawdown_pct 13.04
_SINKING_BASELINE = [-300, -200, 100, -400, -100, 50, -250, -150]
_WINNING_CANDIDATE = [300, 200, -150, 400, 100, -100, 250, 150]


def _deep(name: str, train_pnls: list[int], test_pnls: list[int],
          mc: float = 0.1) -> DeepResult:
    return DeepResult(name=name, train=_ab(f"{name} train", train_pnls),
                      test=_ab(f"{name} test", test_pnls),
                      fragile=False, mc_variance=mc,
                      train_span="t", test_span="t")


# ── A. drawdown gates compare absolute drawdown ──

class TestDrawdownGateIsAbsolute:
    """A baseline that never went positive reports MaxDD% 0.00; comparing
    against it made every drawdown-bearing candidate unpassable."""

    def test_default_guard_passes_clearly_better_candidate(self):
        baseline = _ab("base", _SINKING_BASELINE)
        candidate = _ab("cand", _WINNING_CANDIDATE)
        v = decide_verdict(baseline, candidate, None)
        assert v.passed, (
            "a PF 5.6 candidate with a 150-point drawdown must beat a PF 0.11 "
            "baseline that drew down 1,250 points — pre-#114 the dd% sentinel "
            f"(0.00) failed it. reasons={v.reasons}")

    def test_default_guard_reason_shows_absolute_drawdowns(self):
        baseline = _ab("base", _SINKING_BASELINE)
        candidate = _ab("cand", _WINNING_CANDIDATE)
        v = decide_verdict(baseline, candidate, None)
        dd_lines = [r for r in v.reasons if "MaxDD" in r]
        assert dd_lines, "the default guard must still explain its dd decision"
        assert any("150" in r and "1,250" in r for r in dd_lines), (
            f"the dd reason must show both absolute drawdowns: {dd_lines}")

    def test_default_guard_still_fails_a_deeper_drawdown(self):
        # The gate must stay a gate: same baseline, candidate with a
        # WORSE absolute drawdown but a fine profit factor.
        baseline = _ab("base", [100, -100] * 9)            # dd 100
        candidate = _ab("cand", [900, -800, -800, 900] * 3)  # dd 1,600
        v = decide_verdict(baseline, candidate, None)
        assert not v.passed
        assert any("MaxDD" in r and "✗" in r for r in v.reasons)

    def test_holdout_dd_ratio_passes_against_sentinel_baseline(self):
        base = _deep("base", [200, -100] * 20, _SINKING_BASELINE)
        cand = _deep("cand", [210, -100] * 20, _WINNING_CANDIDATE)
        v = decide_deep_verdict(base, cand, None)
        ratio_lines = [r for r in v.reasons if "holdout MaxDD ratio" in r]
        assert ratio_lines, "the holdout dd ratio line must still be emitted"
        assert all("✗" not in r for r in ratio_lines), (
            "pre-#114 the baseline's sentinel dd% 0.00 was floored to 1.0, "
            f"making the ratio 13.04 and failing the gate: {ratio_lines}")
        assert v.passed, f"reasons={v.reasons}"

    def test_holdout_dd_ratio_still_catches_a_worse_candidate(self):
        base = _deep("base", [200, -100] * 20, [200, -100] * 10)
        cand = _deep("cand", [210, -100] * 20, [500, -450, -450, 500] * 5)
        v = decide_deep_verdict(base, cand, None)
        assert not v.passed
        assert any("holdout MaxDD ratio" in r and "✗" in r for r in v.reasons)

    def test_design_collapse_gate_uses_absolute_drawdown(self):
        # Design window: baseline sinks (dd% sentinel 0.00), candidate
        # draws down far less in absolute terms. Pre-#114 the floored
        # baseline (1.0) made every candidate look like a collapse.
        base = _deep("base", _SINKING_BASELINE * 5, [200, -100] * 10)
        cand = _deep("cand", _WINNING_CANDIDATE * 5, [210, -100] * 10)
        v = decide_deep_verdict(base, cand, None)
        collapse = [r for r in v.reasons if "design-window MaxDD ratio" in r]
        assert collapse, "the design-window dd collapse line must be emitted"
        assert all("collapse" not in r for r in collapse), (
            f"a shallower candidate drawdown is not a collapse: {collapse}")

    def test_plan_dd_criterion_still_uses_percent(self):
        # max_drawdown_pct_max is the candidate's OWN number, specified
        # by the model in percent — it must not be switched to currency.
        base = _deep("base", [100, -200] * 20, [100, -200] * 10)
        cand = _deep("cand", [100, -200] * 20, [200, -100] * 10)
        v = decide_deep_verdict(base, cand, {"max_drawdown_pct_max": 5})
        assert not v.passed
        assert any("train-window MaxDD%" in r for r in v.reasons)


# ── B. the plan prompt's sample-size rule ──

class TestPlanSampleRule:
    """The rule must name the DESIGN-WINDOW total, and must say the trade
    list below is only the new-since-watermark delta."""

    def test_names_the_design_window_total_not_the_delta(self):
        from src.evolution.pipeline import plan_sample_rule
        # 09-12 shape: 3 new trades out of a 130-trade design window.
        text = plan_sample_rule(design_total=130, new_count=3)
        assert "130" in text, (
            f"the rule must be stated on the design-window total: {text!r}")

    def test_says_the_list_is_only_the_new_trades(self):
        from src.evolution.pipeline import plan_sample_rule
        text = plan_sample_rule(design_total=130, new_count=3)
        assert "3" in text
        low = text.lower()
        assert "delta" in low or "only the" in low, (
            f"the rule must say the list below is a delta: {text!r}")

    def test_says_a_short_list_is_not_a_reason_for_no_change(self):
        from src.evolution.pipeline import plan_sample_rule
        text = plan_sample_rule(design_total=130, new_count=3)
        low = text.lower()
        assert "not a reason" in low or "does not trigger" in low, (
            f"the rule must rule out no_change on list length: {text!r}")
        # ...and it must not be silently disarmed: the genuine
        # small-sample escape hatch has to survive.
        assert "no change" in low or "no_change" in low

    def test_small_design_window_still_allows_no_change(self):
        from src.evolution.pipeline import plan_sample_rule
        # issue #94 shape: 1 new trade of a 14-trade design window —
        # here the small-sample rule genuinely applies.
        text = plan_sample_rule(design_total=14, new_count=1)
        assert "14" in text
        assert "30" in text, "the <30 threshold must still be stated"

    def test_full_history_run_reads_naturally(self):
        from src.evolution.pipeline import plan_sample_rule
        # No watermark: the list IS the whole design window, so there is
        # no delta to warn about.
        text = plan_sample_rule(design_total=42, new_count=42)
        assert "42" in text
        assert "30" in text

    def test_composite_gated_signal_kept(self):
        from src.evolution.pipeline import plan_sample_rule
        assert "composite" in plan_sample_rule(130, 3).lower()

    def test_preamble_uses_the_helper(self):
        """The rule text must reach the prompt — an orphaned pure helper
        fixes nothing."""
        src = textwrap.dedent(
            inspect.getsource(rb.BacktestApp._bot_evolution))
        assert "plan_sample_rule" in src, (
            "the evolution preamble must build its sample-size rule with "
            "plan_sample_rule() so the design-window denominator is used")

    def test_old_ambiguous_wording_is_gone(self):
        src = inspect.getsource(rb.BacktestApp._bot_evolution)
        assert "fewer than 30 trades)" not in src, (
            "the ambiguous rule (no denominator) must be replaced, not "
            "duplicated alongside the new one")


# ── C. the generated class must carry the candidate name ──

class _FooStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        pass


class _FooStrategyEvo1(BacktestStrategy):
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        pass


class TestCheckCandidateName:

    def test_base_class_name_is_rejected(self):
        from src.evolution.pipeline import check_candidate_name
        err = check_candidate_name(_FooStrategy, "_FooStrategyEvo1")
        assert err, (
            "a candidate that kept the base class name must be rejected — "
            "saving it overwrites the LIVE strategy's stored source")
        assert "_FooStrategyEvo1" in err and "_FooStrategy" in err

    def test_correct_name_accepted(self):
        from src.evolution.pipeline import check_candidate_name
        assert check_candidate_name(_FooStrategyEvo1, "_FooStrategyEvo1") == ""

    def test_no_expectation_accepts_anything(self):
        from src.evolution.pipeline import check_candidate_name
        assert check_candidate_name(_FooStrategy, "") == ""

    def test_missing_class_is_rejected(self):
        from src.evolution.pipeline import check_candidate_name
        assert check_candidate_name(None, "_FooStrategyEvo1")


class TestCodegenLoopEnforcesCandidateName:
    """AST-level, like tests/test_evolution_codegen_retry.py: the loop
    lives in a Tk method with closures over local state, so its CONTROL
    FLOW is what is asserted."""

    @staticmethod
    def _codegen_loop() -> ast.For:
        src = textwrap.dedent(
            inspect.getsource(rb.BacktestApp._start_evolution_pipeline))
        tree = ast.parse(src)
        loops = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.For) and isinstance(n.target, ast.Name)
            and n.target.id == "attempt"
        ]
        assert len(loops) == 1
        return loops[0]

    def test_loop_checks_the_candidate_name(self):
        loop = self._codegen_loop()
        names = {n.id for n in ast.walk(loop) if isinstance(n, ast.Name)}
        assert "check_candidate_name" in names, (
            "the codegen loop must verify candidate_cls.__name__ against "
            "candidate_name before breaking out — otherwise a model that "
            "kept the base class name overwrites the live strategy")

    def test_name_mismatch_consumes_an_attempt(self):
        """The mismatch branch must feed last_err and `continue`, so the
        one advertised retry actually corrects the name and a double
        failure lands in the existing 'failed twice' report."""
        loop = self._codegen_loop()
        branches = [
            n for n in ast.walk(loop)
            if isinstance(n, ast.If)
            and "check_candidate_name" in {
                x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)}
        ]
        assert branches, "expected an `if` guarding on check_candidate_name"
        body = ast.Module(body=branches[0].body, type_ignores=[])
        assigned = {
            t.id for n in ast.walk(body) if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)
        }
        assert "last_err" in assigned, (
            "the mismatch must be recorded in last_err so the retry prompt "
            "tells the model what to fix")
        assert any(isinstance(n, ast.Continue) for n in ast.walk(body)), (
            "the mismatch must `continue` to the next attempt, not break")

    def test_mismatch_discards_the_class(self):
        """candidate_cls must be cleared, otherwise the post-loop
        `if candidate_cls is None` check passes and the misnamed class
        reaches the save path anyway."""
        loop = self._codegen_loop()
        branches = [
            n for n in ast.walk(loop)
            if isinstance(n, ast.If)
            and "check_candidate_name" in {
                x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)}
        ]
        body = ast.Module(body=branches[0].body, type_ignores=[])
        cleared = [
            n for n in ast.walk(body) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "candidate_cls"
                    for t in n.targets)
            and isinstance(n.value, ast.Constant) and n.value.value is None
        ]
        assert cleared, "the misnamed class must be discarded (set to None)"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
