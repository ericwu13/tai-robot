"""Issue #164 — a custom DynamicExitPullback* without every grid knob.

Knob-mode eligibility used to be ``"DynamicExitPullback" in class_name``.
``read_strategy_knobs`` then raised ``KeyError: 'atr_buffer_mult'`` for a
V3-like class, and the Saturday run stopped before the watermark with a
cryptic refusal that repeated every week.

These tests fail against the pre-fix ``knobs.py``: ``knob_mode_ineligible``
did not exist, and ``saturday_knob_decision`` returned
``could not read baseline knobs (KeyError: 'atr_buffer_mult')``.

Option (ii) stays: the reason is a refusal, so the caller returns before
``maybe_advance_watermark``. No codegen fallback and no partial grid.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

import run_backtest as rb
from src.evolution.knobs import (
    DYNAMIC_EXIT_PULLBACK_GRID,
    knob_mode_ineligible,
    saturday_knob_decision,
    saturday_knob_directives_if_eligible,
)

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
        # Non-empty refusal: the pipeline returns before the watermark.
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

    def test_ineligible_refusal_stays_before_the_watermark(self):
        src = textwrap.dedent(
            inspect.getsource(rb.BacktestApp._start_evolution_pipeline))
        assert src.index("saturday_knob_decision") < src.index(
            "maybe_advance_watermark")
        tree = ast.parse(src)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "saturday_knob_decision"
        ]
        assert len(calls) == 1
        assert "strategy_source" in ast.dump(calls[0])
        # The non-empty reason is what `if rejection: return` keys off.
        # Codegen stays behind that return (option ii).
        assert src.index("if rejection:") < src.index("maybe_advance_watermark")
        assert src.index("if rejection:") < src.index("evolution_codegen")
