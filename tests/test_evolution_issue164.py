"""Issue #164 — a custom DynamicExitPullback* without every grid knob.

Knob-mode eligibility used to be ``"DynamicExitPullback" in class_name``.
``read_strategy_knobs`` then raised ``KeyError: 'atr_buffer_mult'`` for a
V3-like class, and the Saturday run stopped before the watermark with a
cryptic refusal that repeated every week.

These tests fail against the pre-fix ``knobs.py``: ``knob_mode_ineligible``
did not exist, and ``saturday_knob_decision`` returned
``could not read baseline knobs (KeyError: 'atr_buffer_mult')``.

Option (iii): an ineligible class is recorded once as ``not applicable``,
keyed on class name plus source hash, and the watermark advances. The
same key is not retried or re-alerted. A new hash is re-evaluated.
No codegen fallback. Off-grid refusals on an eligible class still stop
before the watermark.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

import run_backtest as rb
from src.evolution.knobs import (
    DYNAMIC_EXIT_PULLBACK_GRID,
    KNOB_NOT_APPLICABLE,
    knob_applicability_key,
    knob_mode_ineligible,
    load_knob_not_applicable,
    saturday_knob_decision,
    saturday_knob_directives_if_eligible,
    settle_knob_ineligibility,
)
from src.evolution.notify import load_watermark

_V3_REASON = (
    "knob mode not applicable: DynamicExitPullbackV3 lacks knobs "
    "[atr_buffer_mult]; skipping knob mode"
)


class DynamicExitPullbackV3:
    """Custom pullback that never grew ``atr_buffer_mult`` (issue #164)."""

    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)
        self.atr_stop_mult = kwargs.get("atr_stop_mult", 2.5)

class DynamicExitPullbackMissingTwo:
    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)


_COMPUTED_KNOB = 50


class DynamicExitPullbackNonLiteral:
    """All four grid attrs; none is a literal ``kwargs.get`` default."""

    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", _COMPUTED_KNOB)
        self.atr_period = kwargs.get("atr_period", _COMPUTED_KNOB)
        self.atr_stop_mult = kwargs.get("atr_stop_mult", _COMPUTED_KNOB)
        self.atr_buffer_mult = kwargs.get("atr_buffer_mult", _COMPUTED_KNOB)


class DynamicExitPullbackBufferComputed:
    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)
        self.atr_stop_mult = kwargs.get("atr_stop_mult", 2.5)
        self.atr_buffer_mult = kwargs.get("atr_buffer_mult", _COMPUTED_KNOB)


class DynamicExitPullbackBoom:
    def __init__(self):
        raise RuntimeError("needs a session")


class DynamicExitPullbackRaisingKnob:
    """``atr_buffer_mult`` is present but its getter raises."""

    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)
        self.atr_stop_mult = kwargs.get("atr_stop_mult", 2.5)

    @property
    def atr_buffer_mult(self):
        raise RuntimeError("buffer unavailable")


# AI: strategies are exec'd from a string. inspect.getsource cannot see
# them, so eligibility has to judge the source the caller passes.
_IN_MEMORY_V2 = textwrap.dedent("""\
class DynamicExitPullbackInMemory:
    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)
        self.atr_stop_mult = kwargs.get("atr_stop_mult", 2.5)
        self.atr_buffer_mult = kwargs.get("atr_buffer_mult", 0.5)
""")

_IN_MEMORY_V3 = _IN_MEMORY_V2.replace(
    '    self.atr_buffer_mult = kwargs.get("atr_buffer_mult", 0.5)\n',
    "",
)


def _exec_in_memory(source: str):
    namespace: dict = {}
    exec(source, namespace)
    return namespace["DynamicExitPullbackInMemory"]


class TestKnobModeIneligible:
    def test_v3_missing_buffer_is_a_clear_refusal_not_keyerror(self):
        reason = knob_mode_ineligible(
            DynamicExitPullbackV3, DYNAMIC_EXIT_PULLBACK_GRID,
            inspect.getsource(DynamicExitPullbackV3))
        assert reason == _V3_REASON
        assert "KeyError" not in reason
        # A one-knob change that would be legal on V2 is not a partial grid.
        decision, applied = saturday_knob_decision(
            "DynamicExitPullbackV3", {"ema_period": 60},
            DynamicExitPullbackV3)
        assert applied is None
        assert decision == _V3_REASON
        assert decision

    def test_missing_knobs_are_listed_in_grid_order(self):
        reason, applied = saturday_knob_decision(
            "DynamicExitPullbackMissingTwo", {"ema_period": 60},
            DynamicExitPullbackMissingTwo)
        assert applied is None
        assert reason == (
            "knob mode not applicable: DynamicExitPullbackMissingTwo "
            "lacks knobs [atr_stop_mult, atr_buffer_mult]; skipping knob mode"
        )

    def test_nonliteral_defaults_are_ineligible(self):
        inst = DynamicExitPullbackNonLiteral()
        for key in DYNAMIC_EXIT_PULLBACK_GRID:
            assert hasattr(inst, key)
        source = inspect.getsource(DynamicExitPullbackNonLiteral)
        reason = knob_mode_ineligible(
            DynamicExitPullbackNonLiteral, DYNAMIC_EXIT_PULLBACK_GRID, source)
        assert reason == (
            "knob mode not applicable: DynamicExitPullbackNonLiteral lacks "
            "a literal kwargs.get default for knobs [ema_period, "
            "atr_period, atr_stop_mult, atr_buffer_mult]; skipping knob mode"
        )
        decision, applied = saturday_knob_decision(
            "DynamicExitPullbackNonLiteral", {"ema_period": 60},
            DynamicExitPullbackNonLiteral, source)
        assert applied is None
        assert decision == reason
        assert "KeyError" not in decision

    def test_only_nonliteral_knobs_are_named(self):
        source = inspect.getsource(DynamicExitPullbackBufferComputed)
        reason = knob_mode_ineligible(
            DynamicExitPullbackBufferComputed, DYNAMIC_EXIT_PULLBACK_GRID,
            source)
        assert reason == (
            "knob mode not applicable: DynamicExitPullbackBufferComputed "
            "lacks a literal kwargs.get default for knobs "
            "[atr_buffer_mult]; skipping knob mode"
        )

    def test_instantiation_failure_is_ineligible(self):
        reason = knob_mode_ineligible(
            DynamicExitPullbackBoom, DYNAMIC_EXIT_PULLBACK_GRID, "")
        assert reason == (
            "knob mode not applicable: DynamicExitPullbackBoom no-arg "
            "instantiation failed (RuntimeError: needs a session); "
            "skipping knob mode"
        )
        decision, applied = saturday_knob_decision(
            "DynamicExitPullbackBoom", {"ema_period": 60},
            DynamicExitPullbackBoom, "")
        assert applied is None
        assert decision == reason

    def test_stock_v2_stays_eligible(self):
        from dynamic_exit_pullback_strategy_v2 import (
            DynamicExitPullbackStrategyV2)
        source = inspect.getsource(DynamicExitPullbackStrategyV2)
        assert knob_mode_ineligible(
            DynamicExitPullbackStrategyV2, DYNAMIC_EXIT_PULLBACK_GRID,
            source) == ""
        ok, knobs = saturday_knob_decision(
            "DynamicExitPullbackStrategyV2", {"ema_period": 60},
            DynamicExitPullbackStrategyV2)
        assert ok == ""
        assert knobs == {"ema_period": 60}

    def test_non_grid_strategy_still_returns_empty(self):
        empty, applied = saturday_knob_decision(
            "SomeOtherStrategy", {"ema_period": 80}, object)
        assert empty == "" and applied is None
        assert saturday_knob_directives_if_eligible(object, "") == ""

    def test_in_memory_source_with_literal_knobs_is_eligible(self):
        """The passed source is the text eligibility rewrites.

        ``inspect.getsource`` raises on an exec'd class. Ignoring the
        ``source`` argument and always calling ``inspect.getsource``
        marks this class ineligible.
        """
        cls = _exec_in_memory(_IN_MEMORY_V2)
        # Empty-namespace exec (the AI loader) has no retrievable source.
        # _class_source swallows this TypeError and returns "".
        with pytest.raises(TypeError):
            inspect.getsource(cls)
        assert knob_mode_ineligible(
            cls, DYNAMIC_EXIT_PULLBACK_GRID, _IN_MEMORY_V2) == ""
        ok, knobs = saturday_knob_decision(
            cls.__name__, {"ema_period": 60}, cls, _IN_MEMORY_V2)
        assert ok == ""
        assert knobs == {"ema_period": 60}

    def test_in_memory_source_missing_buffer_is_the_clear_refusal(self):
        assert "atr_buffer_mult" not in _IN_MEMORY_V3
        cls = _exec_in_memory(_IN_MEMORY_V3)
        reason = (
            "knob mode not applicable: DynamicExitPullbackInMemory "
            "lacks knobs [atr_buffer_mult]; skipping knob mode"
        )
        assert knob_mode_ineligible(
            cls, DYNAMIC_EXIT_PULLBACK_GRID, _IN_MEMORY_V3) == reason
        decision, applied = saturday_knob_decision(
            cls.__name__, {"ema_period": 60}, cls, _IN_MEMORY_V3)
        assert applied is None
        assert decision == reason
        assert "KeyError" not in decision

    def test_raising_knob_property_is_a_refusal_not_an_error(self):
        reason = (
            "knob mode not applicable: DynamicExitPullbackRaisingKnob "
            "knob atr_buffer_mult raised RuntimeError"
        )
        assert knob_mode_ineligible(
            DynamicExitPullbackRaisingKnob, DYNAMIC_EXIT_PULLBACK_GRID,
            inspect.getsource(DynamicExitPullbackRaisingKnob)) == reason
        decision, applied = saturday_knob_decision(
            "DynamicExitPullbackRaisingKnob", {"ema_period": 60},
            DynamicExitPullbackRaisingKnob)
        assert applied is None
        assert decision == reason


class TestKnobPromptGate:
    def test_prompt_omits_knob_directives_for_ineligible_class(self):
        source = inspect.getsource(DynamicExitPullbackV3)
        note = saturday_knob_directives_if_eligible(
            DynamicExitPullbackV3, source)
        assert note == ""
        assert "MANDATORY" not in note
        from dynamic_exit_pullback_strategy_v2 import (
            DynamicExitPullbackStrategyV2)
        v2_note = saturday_knob_directives_if_eligible(
            DynamicExitPullbackStrategyV2,
            inspect.getsource(DynamicExitPullbackStrategyV2))
        assert "Saturday auto knob mode (MANDATORY on this weekly run)" in v2_note
        src = textwrap.dedent(inspect.getsource(rb.BacktestApp._bot_evolution))
        assert "saturday_knob_directives_if_eligible" in src
        assert "knob_note = saturday_knob_directives()" not in src
        assert "{knob_note}" in src

    def test_ineligible_settles_before_the_ai_and_codegen(self):
        """Same key skips before the pipeline. Off-grid refusal still
        sits before the change-plan watermark advance."""
        evo = textwrap.dedent(inspect.getsource(rb.BacktestApp._bot_evolution))
        assert evo.index("settle_knob_ineligibility") < evo.index(
            "No new design-window trades")
        assert evo.index('== "skip"') < evo.index('== "recorded"')
        assert evo.index("settle_knob_ineligibility") < evo.index(
            "_start_evolution_pipeline")
        src = textwrap.dedent(
            inspect.getsource(rb.BacktestApp._start_evolution_pipeline))
        assert src.index("saturday_knob_decision") < src.index(
            "maybe_advance_watermark")
        assert src.index("is_knob_not_applicable_reason") < src.index(
            "maybe_advance_watermark")
        assert src.index("is_knob_not_applicable_reason") < src.index(
            "evolution_codegen")
        tree = ast.parse(src)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "saturday_knob_decision"
        ]
        assert len(calls) == 1
        assert "strategy_source" in ast.dump(calls[0])


_AT = "2026-10-17 05:05:00"


class TestKnobNotApplicable:
    def test_ineligible_recorded_once_and_watermark_advances(self, tmp_path):
        source = inspect.getsource(DynamicExitPullbackV3)
        action, reason = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackV3, source, 12, at=_AT)
        assert action == "recorded"
        assert reason == _V3_REASON
        rec = load_knob_not_applicable(tmp_path)
        key = knob_applicability_key("DynamicExitPullbackV3", source)
        assert rec["outcome"] == KNOB_NOT_APPLICABLE == "not applicable"
        assert rec["class_name"] == key["class_name"]
        assert rec["source_hash"] == key["source_hash"]
        assert rec["reason"] == _V3_REASON
        assert rec["at"] == _AT
        assert load_watermark(tmp_path) == {"trade_count": 12, "at": _AT}

    def test_second_run_same_key_skips_silently(self, tmp_path):
        source = inspect.getsource(DynamicExitPullbackV3)
        settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackV3, source, 12, at=_AT)
        rec = load_knob_not_applicable(tmp_path)
        mark = load_watermark(tmp_path)
        action, reason = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackV3, source, 40,
            at="2026-10-24 05:05:00")
        assert action == "skip"
        assert reason == ""
        assert load_knob_not_applicable(tmp_path) == rec
        assert load_watermark(tmp_path) == mark

    def test_source_hash_change_reevaluates(self, tmp_path):
        source = inspect.getsource(DynamicExitPullbackV3)
        settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackV3, source, 12, at=_AT)
        edited = source + "\n# edited knob source\n"
        assert knob_applicability_key(
            "DynamicExitPullbackV3", edited)["source_hash"] != (
            knob_applicability_key(
                "DynamicExitPullbackV3", source)["source_hash"])
        action, reason = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackV3, edited, 18,
            at="2026-10-24 05:05:00")
        assert action == "recorded"
        assert reason == _V3_REASON
        rec = load_knob_not_applicable(tmp_path)
        assert rec["class_name"] == "DynamicExitPullbackV3"
        assert rec["source_hash"] == knob_applicability_key(
            "DynamicExitPullbackV3", edited)["source_hash"]
        assert rec["outcome"] == "not applicable"
        assert load_watermark(tmp_path)["trade_count"] == 18

    def test_class_name_is_part_of_the_key(self, tmp_path):
        source = inspect.getsource(DynamicExitPullbackV3)
        settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackV3, source, 12, at=_AT)
        other = type(
            "DynamicExitPullbackOther", (DynamicExitPullbackV3,), {})
        action, reason = settle_knob_ineligibility(
            tmp_path, other, source, 15, at="2026-10-24 05:05:00")
        assert action == "recorded"
        assert "lacks knobs [atr_buffer_mult]" in reason
        assert load_knob_not_applicable(tmp_path)["class_name"] == (
            "DynamicExitPullbackOther")

    def test_eligible_paths_do_not_record_or_advance(self, tmp_path):
        from dynamic_exit_pullback_strategy_v2 import (
            DynamicExitPullbackStrategyV2)
        settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackV3,
            inspect.getsource(DynamicExitPullbackV3), 12, at=_AT)
        source = inspect.getsource(DynamicExitPullbackStrategyV2)
        action, reason = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackStrategyV2, source, 99,
            at="2026-10-24 05:05:00")
        assert action == "eligible"
        assert reason == ""
        assert load_watermark(tmp_path)["trade_count"] == 12
        assert load_knob_not_applicable(tmp_path)["class_name"] == (
            "DynamicExitPullbackV3")
        ok, knobs = saturday_knob_decision(
            "DynamicExitPullbackStrategyV2", {"ema_period": 60},
            DynamicExitPullbackStrategyV2, source)
        assert ok == ""
        assert knobs == {"ema_period": 60}
        # The edited in-memory class is eligible, so the old record
        # does not keep it skipped and the watermark stays put.
        good = _exec_in_memory(_IN_MEMORY_V2)
        bad = _exec_in_memory(_IN_MEMORY_V3)
        again, _ = settle_knob_ineligibility(
            tmp_path, bad, _IN_MEMORY_V3, 12, at=_AT)
        assert again == "recorded"
        action, reason = settle_knob_ineligibility(
            tmp_path, good, _IN_MEMORY_V2, 40, at="2026-10-24 05:05:00")
        assert action == "eligible"
        assert reason == ""
        assert load_watermark(tmp_path)["trade_count"] == 12
        grid_skip, _ = settle_knob_ineligibility(
            tmp_path, object, "class X: pass", 12, at=_AT)
        assert grid_skip == "not_grid"
