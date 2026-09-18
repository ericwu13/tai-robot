"""CEO option-1 — evolution plan prompt must not ask for numeric holdout
criteria.

TMF00_0422 Path A Discord FAILs 2026-09-19: the model copied the
example JSON bars (``profit_factor_min`` 1.2 / ``win_rate_min`` 0.45,
and later PF≥1.5) into ``directives["criteria"]``. Afternoon candidates
that cleared #125 floors still FAILed the invented plan bars. Default
verdict path is not-worse-than-baseline when ``criteria=None``.

These tests are designed to FAIL against the pre-fix prompt: the
``_bot_evolution`` preamble still embeds
``"profit_factor_min": 1.2, "win_rate_min": 0.45`` and instructs
``Include ONLY the criteria keys``.
"""

from __future__ import annotations

import inspect
import textwrap

import run_backtest as rb


def _evolution_src() -> str:
    return textwrap.dedent(
        inspect.getsource(rb.BacktestApp._bot_evolution))


_OLD_PF_BAR = '"profit_factor_min": 1.2'
_OLD_WR_BAR = '"win_rate_min": 0.45'
_OLD_CRITERIA_ASK = "Include ONLY the criteria keys"


class TestPlanPromptOmitsNumericHoldoutCriteria:
    def test_helper_omits_old_example_bars(self):
        """FAILS pre-fix: ``plan_directives_block`` did not exist / the
        example JSON still carried PF 1.2 and WR 0.45."""
        from src.evolution.pipeline import plan_directives_block
        text = plan_directives_block()
        assert _OLD_PF_BAR not in text, (
            "plan prompt must not show example PF≥1.2 — the model copies "
            f"it into directives['criteria']: {text!r}")
        assert _OLD_WR_BAR not in text, (
            "plan prompt must not show example WR≥0.45 — the model copies "
            f"it into directives['criteria']: {text!r}")

    def test_helper_does_not_instruct_emitting_criteria_keys(self):
        """FAILS pre-fix: the prompt told the model to include criteria
        keys its validation section specified."""
        from src.evolution.pipeline import plan_directives_block
        text = plan_directives_block()
        assert _OLD_CRITERIA_ASK not in text, (
            "must not instruct emitting profit_factor_min / win_rate_min: "
            f"{text!r}")
        low = text.lower()
        assert "omit" in low or "leave" in low or "do not" in low, (
            "must tell the model to omit criteria / leave the JSON as "
            f"action only: {text!r}")
        assert "not-worse-than-baseline" in low, (
            "must name the default verdict path so the model does not "
            f"invent holdout bars: {text!r}")

    def test_helper_example_is_action_only(self):
        from src.evolution.pipeline import plan_directives_block
        text = plan_directives_block()
        assert '{"action": "change"}' in text
        assert "max_drawdown_pct_max" not in text
        assert "total_pnl_min" not in text
        # Key names may appear in a do-not-emit list, but never as
        # numeric example assignments the model can copy.
        assert "profit_factor_min" not in text or "do not" in text.lower()
        assert ": 1.2" not in text
        assert ": 0.45" not in text

    def test_preamble_uses_the_helper(self):
        """The fragment must reach the prompt — an orphaned helper
        fixes nothing."""
        src = _evolution_src()
        assert "plan_directives_block" in src, (
            "the evolution preamble must build its JSON-directives "
            "section with plan_directives_block()")

    def test_old_example_bars_gone_from_bot_evolution(self):
        """FAILS pre-fix: the bars sat inline in ``_bot_evolution``."""
        src = _evolution_src()
        assert _OLD_PF_BAR not in src
        assert _OLD_WR_BAR not in src
        assert _OLD_CRITERIA_ASK not in src


def test_gate_floors_unchanged_by_this_pr():
    """CEO option-1: prompt only — #125 floors stay locked."""
    from src.evolution.evaluator import MC_FRAGILE_VARIANCE
    from src.evolution.fitness import MIN_TRADES
    from src.evolution.pipeline import (
        ABS_PF_FLOOR, HOLDOUT_DD_RATIO_MAX, MIN_EXPRESSED_TRADES)
    assert ABS_PF_FLOOR == 1.0
    assert MIN_EXPRESSED_TRADES == 3
    assert HOLDOUT_DD_RATIO_MAX == 1.2
    assert MC_FRAGILE_VARIANCE == 0.30
    assert MIN_TRADES == 30
