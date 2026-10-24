"""Issue #164 / D1 — per-class knob grids, not all-or-nothing.

Knob-mode eligibility used to be ``"DynamicExitPullback" in class_name``.
``read_strategy_knobs`` then raised ``KeyError: 'atr_buffer_mult'`` for a
V3-like class, and the Saturday run stopped before the watermark with a
cryptic refusal that repeated every week.

D1: the grid is the knobs the class actually exposes. V3 (ema_period,
atr_period, atr_stop_mult) is eligible on that 3-knob grid. A class with
fewer than 2 rewritable grid knobs is still recorded as ``not applicable``
(class + source hash + grid hash) and the watermark advances once. Every
later Saturday repeats ``excluded: <class> — <reason>; fallback=<mode>``
and does not codegen. A new source hash or grid hash is re-evaluated.
Off-grid refusals on an eligible class still stop before the watermark.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import types
from unittest.mock import MagicMock

import pytest

import run_backtest as rb
from src.evolution.knobs import (
    DYNAMIC_EXIT_PULLBACK_GRID,
    KNOB_NOT_APPLICABLE,
    class_knob_eligibility,
    format_knob_exclusion_line,
    knob_applicability_key,
    knob_mode_ineligible,
    load_knob_not_applicable,
    saturday_knob_decision,
    saturday_knob_directives_if_eligible,
    settle_knob_ineligibility,
)
from src.evolution.notify import load_watermark

_V3_GRID_KEYS = ("ema_period", "atr_period", "atr_stop_mult")


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


class DynamicExitPullbackOneKnob:
    """One rewritable grid knob — below the D1 minimum of 2."""

    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)


_ONE_KNOB_REASON = (
    "knob mode not applicable: DynamicExitPullbackOneKnob has 1 "
    "rewritable grid knob [ema_period] (need >= 2); skipping knob mode"
)


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

_IN_MEMORY_ONE = textwrap.dedent("""\
class DynamicExitPullbackInMemory:
    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
""")


def _exec_in_memory(source: str):
    namespace: dict = {}
    exec(source, namespace)
    return namespace["DynamicExitPullbackInMemory"]


class TestKnobModeIneligible:
    def test_v3_missing_buffer_is_a_three_knob_grid_not_keyerror(self):
        source = inspect.getsource(DynamicExitPullbackV3)
        effective, reason = class_knob_eligibility(
            DynamicExitPullbackV3, DYNAMIC_EXIT_PULLBACK_GRID, source)
        assert reason == ""
        assert tuple(effective) == _V3_GRID_KEYS
        assert "atr_buffer_mult" not in effective
        assert knob_mode_ineligible(
            DynamicExitPullbackV3, DYNAMIC_EXIT_PULLBACK_GRID, source) == ""
        decision, applied = saturday_knob_decision(
            "DynamicExitPullbackV3", {"ema_period": 60},
            DynamicExitPullbackV3)
        assert decision == ""
        assert applied == {"ema_period": 60}
        assert "KeyError" not in decision
        refused, not_applied = saturday_knob_decision(
            "DynamicExitPullbackV3", {"atr_buffer_mult": 0.5},
            DynamicExitPullbackV3)
        assert not_applied is None
        assert "not allowlisted" in refused

    def test_two_exposed_knobs_are_eligible_in_grid_order(self):
        source = inspect.getsource(DynamicExitPullbackMissingTwo)
        effective, reason = class_knob_eligibility(
            DynamicExitPullbackMissingTwo, DYNAMIC_EXIT_PULLBACK_GRID, source)
        assert reason == ""
        assert list(effective) == ["ema_period", "atr_period"]
        decision, applied = saturday_knob_decision(
            "DynamicExitPullbackMissingTwo", {"ema_period": 60},
            DynamicExitPullbackMissingTwo)
        assert decision == ""
        assert applied == {"ema_period": 60}

    def test_nonliteral_defaults_are_ineligible(self):
        inst = DynamicExitPullbackNonLiteral()
        for key in DYNAMIC_EXIT_PULLBACK_GRID:
            assert hasattr(inst, key)
        source = inspect.getsource(DynamicExitPullbackNonLiteral)
        reason = knob_mode_ineligible(
            DynamicExitPullbackNonLiteral, DYNAMIC_EXIT_PULLBACK_GRID, source)
        assert reason == (
            "knob mode not applicable: DynamicExitPullbackNonLiteral has 0 "
            "rewritable grid knobs [] (need >= 2); not rewritable "
            "[ema_period, atr_period, atr_stop_mult, atr_buffer_mult]; "
            "skipping knob mode"
        )
        decision, applied = saturday_knob_decision(
            "DynamicExitPullbackNonLiteral", {"ema_period": 60},
            DynamicExitPullbackNonLiteral, source)
        assert applied is None
        assert decision == reason
        assert "KeyError" not in decision

    def test_nonliteral_knob_drops_out_of_an_otherwise_eligible_grid(self):
        source = inspect.getsource(DynamicExitPullbackBufferComputed)
        effective, reason = class_knob_eligibility(
            DynamicExitPullbackBufferComputed, DYNAMIC_EXIT_PULLBACK_GRID,
            source)
        assert reason == ""
        assert list(effective) == ["ema_period", "atr_period", "atr_stop_mult"]
        assert "atr_buffer_mult" not in effective
        decision, applied = saturday_knob_decision(
            "DynamicExitPullbackBufferComputed", {"ema_period": 60},
            DynamicExitPullbackBufferComputed, source)
        assert decision == ""
        assert applied == {"ema_period": 60}

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

    def test_in_memory_source_missing_buffer_keeps_the_three_knob_grid(self):
        assert "atr_buffer_mult" not in _IN_MEMORY_V3
        cls = _exec_in_memory(_IN_MEMORY_V3)
        effective, reason = class_knob_eligibility(
            cls, DYNAMIC_EXIT_PULLBACK_GRID, _IN_MEMORY_V3)
        assert reason == ""
        assert tuple(effective) == _V3_GRID_KEYS
        decision, applied = saturday_knob_decision(
            cls.__name__, {"ema_period": 60}, cls, _IN_MEMORY_V3)
        assert decision == ""
        assert applied == {"ema_period": 60}
        assert "KeyError" not in decision

    def test_raising_knob_property_is_a_refusal_not_an_error(self):
        reason = (
            "knob mode not applicable: DynamicExitPullbackRaisingKnob "
            "knob atr_buffer_mult raised RuntimeError; skipping knob mode"
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
    def test_prompt_uses_the_class_grid_and_omits_ineligible_classes(self):
        one = saturday_knob_directives_if_eligible(
            DynamicExitPullbackOneKnob,
            inspect.getsource(DynamicExitPullbackOneKnob))
        assert one == ""
        v3_note = saturday_knob_directives_if_eligible(
            DynamicExitPullbackV3, inspect.getsource(DynamicExitPullbackV3))
        assert "Saturday auto knob mode (MANDATORY on this weekly run)" in v3_note
        assert "atr_stop_mult" in v3_note
        assert "atr_buffer_mult" not in v3_note
        from dynamic_exit_pullback_strategy_v2 import (
            DynamicExitPullbackStrategyV2)
        v2_note = saturday_knob_directives_if_eligible(
            DynamicExitPullbackStrategyV2,
            inspect.getsource(DynamicExitPullbackStrategyV2))
        assert "Saturday auto knob mode (MANDATORY on this weekly run)" in v2_note
        assert "atr_buffer_mult" in v2_note
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
        # skip and recorded share one branch, so a repeat week still logs.
        assert 'in ("skip", "recorded")' in evo
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


def _one_knob_line(reason: str = _ONE_KNOB_REASON) -> str:
    return format_knob_exclusion_line(
        "DynamicExitPullbackOneKnob", reason, "none")


class TestKnobNotApplicable:
    def test_ineligible_recorded_once_and_watermark_advances(self, tmp_path):
        source = inspect.getsource(DynamicExitPullbackOneKnob)
        action, line = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 12, at=_AT)
        assert action == "recorded"
        assert line == _one_knob_line()
        rec = load_knob_not_applicable(tmp_path)
        key = knob_applicability_key("DynamicExitPullbackOneKnob", source)
        assert rec["outcome"] == KNOB_NOT_APPLICABLE == "not applicable"
        assert rec["class_name"] == key["class_name"]
        assert rec["source_hash"] == key["source_hash"]
        assert rec["grid_hash"] == key["grid_hash"]
        assert rec["reason"] == _ONE_KNOB_REASON
        assert rec["fallback"] == "none"
        assert rec["at"] == _AT
        assert load_watermark(tmp_path) == {"trade_count": 12, "at": _AT}

    def test_second_run_same_key_logs_again_without_rerecording(self, tmp_path):
        source = inspect.getsource(DynamicExitPullbackOneKnob)
        settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 12, at=_AT)
        rec = load_knob_not_applicable(tmp_path)
        mark = load_watermark(tmp_path)
        action, line = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 40,
            at="2026-10-24 05:05:00")
        assert action == "skip"
        assert line == _one_knob_line()
        assert load_knob_not_applicable(tmp_path) == rec
        assert load_watermark(tmp_path) == mark

    def test_source_hash_change_reevaluates(self, tmp_path):
        source = inspect.getsource(DynamicExitPullbackOneKnob)
        settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 12, at=_AT)
        edited = source + "\n# edited knob source\n"
        assert knob_applicability_key(
            "DynamicExitPullbackOneKnob", edited)["source_hash"] != (
            knob_applicability_key(
                "DynamicExitPullbackOneKnob", source)["source_hash"])
        action, line = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, edited, 18,
            at="2026-10-24 05:05:00")
        assert action == "recorded"
        assert line == _one_knob_line()
        rec = load_knob_not_applicable(tmp_path)
        assert rec["class_name"] == "DynamicExitPullbackOneKnob"
        assert rec["source_hash"] == knob_applicability_key(
            "DynamicExitPullbackOneKnob", edited)["source_hash"]
        assert rec["outcome"] == "not applicable"
        assert load_watermark(tmp_path)["trade_count"] == 18

    def test_class_name_is_part_of_the_key(self, tmp_path):
        source = inspect.getsource(DynamicExitPullbackOneKnob)
        settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 12, at=_AT)
        other = type(
            "DynamicExitPullbackOther", (DynamicExitPullbackOneKnob,), {})
        action, line = settle_knob_ineligibility(
            tmp_path, other, source, 15, at="2026-10-24 05:05:00")
        assert action == "recorded"
        assert "DynamicExitPullbackOther" in line
        assert "has 1 rewritable grid knob [ema_period]" in line
        assert load_knob_not_applicable(tmp_path)["class_name"] == (
            "DynamicExitPullbackOther")

    def test_eligible_paths_do_not_record_or_advance(self, tmp_path):
        from dynamic_exit_pullback_strategy_v2 import (
            DynamicExitPullbackStrategyV2)
        settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob,
            inspect.getsource(DynamicExitPullbackOneKnob), 12, at=_AT)
        source = inspect.getsource(DynamicExitPullbackStrategyV2)
        action, reason = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackStrategyV2, source, 99,
            at="2026-10-24 05:05:00")
        assert action == "eligible"
        assert reason == ""
        assert load_watermark(tmp_path)["trade_count"] == 12
        assert load_knob_not_applicable(tmp_path)["class_name"] == (
            "DynamicExitPullbackOneKnob")
        ok, knobs = saturday_knob_decision(
            "DynamicExitPullbackStrategyV2", {"ema_period": 60},
            DynamicExitPullbackStrategyV2, source)
        assert ok == ""
        assert knobs == {"ema_period": 60}
        # An eligible class is not kept skipped by an older record, and
        # the watermark stays put. A still-ineligible in-memory class
        # re-records.
        good = _exec_in_memory(_IN_MEMORY_V2)
        bad = _exec_in_memory(_IN_MEMORY_ONE)
        again, _ = settle_knob_ineligibility(
            tmp_path, bad, _IN_MEMORY_ONE, 12, at=_AT)
        assert again == "recorded"
        action, reason = settle_knob_ineligibility(
            tmp_path, good, _IN_MEMORY_V2, 40, at="2026-10-24 05:05:00")
        assert action == "eligible"
        assert reason == ""
        assert load_watermark(tmp_path)["trade_count"] == 12
        grid_skip, _ = settle_knob_ineligibility(
            tmp_path, object, "class X: pass", 12, at=_AT)
        assert grid_skip == "not_grid"


class DynamicExitPullbackStub:
    """Ineligible live class for the ``_bot_evolution`` / backstop calls.

    One rewritable grid knob, so weekly codegen must not run.
    """

    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)


_STUB_SOURCE = inspect.getsource(DynamicExitPullbackStub)
_STUB_REASON = (
    "knob mode not applicable: DynamicExitPullbackStub has 1 "
    "rewritable grid knob [ema_period] (need >= 2); skipping knob mode"
)
_STUB_LINE = format_knob_exclusion_line(
    "DynamicExitPullbackStub", _STUB_REASON, "none")
# Usable action=change plan. The backstop sits after this plan call;
# codegen (``one_shot``) must not run for an ineligible class.
_CHANGE_PLAN = (
    "Performance summary: the pullback is unchanged.\n"
    "```json\n"
    '{"action": "change", "knobs": {"ema_period": 60}}\n'
    "```\n"
)


class _InlineThread:
    """Run the evolution worker before ``_bot_evolution`` returns.

    A real daemon thread would let a removed ``return`` reach
    ``send_message`` after the assertion had already passed.
    """

    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


class _DiscordLog:
    def __init__(self):
        self.enabled = True
        self.msgs: list[str] = []

    def notify_evolution(self, msg):
        self.msgs.append(msg)


def _wiring_trades():
    """One design-window trade and one holdout trade (14d holdout)."""
    from src.backtest.broker import OrderSide, Trade

    def trade(exit_dt):
        return Trade(
            tag="t", side=OrderSide.LONG, qty=1,
            entry_price=20000, exit_price=20010,
            entry_bar_index=0, exit_bar_index=1, pnl=200,
            entry_dt=exit_dt, exit_dt=exit_dt, source="paper",
        )

    return [
        trade("2026-01-01 10:00"),
        trade("2026-06-01 10:00"),
    ]


def _wiring_app(bot_dir, chat):
    """Stub ``BacktestApp`` enough for ``_bot_evolution`` to settle.

    Methods the early return never calls are still bound, so a mutant
    that falls into the AI pipeline hits the mocked client instead of
    an ``AttributeError`` on the stub.
    """
    from src.live.live_runner import LiveState

    runner = types.SimpleNamespace(
        state=LiveState.RUNNING,
        bot_dir=bot_dir,
        strategy=DynamicExitPullbackStub(),
        point_value=200,
        target_interval=60,
        symbol="TXF",
        get_bars=lambda: [],
    )
    trades = _wiring_trades()
    result = types.SimpleNamespace(
        trades=trades, strategy_name="DynamicExitPullbackStub", metrics=None)
    app = types.SimpleNamespace(
        _live_runner=runner,
        _settings={
            "evolution_holdout_days": 14,
            "evolution_auto_pipeline": True,
            "evolution_capital_base_twd": 100000,
            "ai_provider": "anthropic",
            "ultra_mode": False,
            "evolution_validation": "off",
        },
        _chat_client=chat,
        _REVIEW_SINGLE_SHOT_CHAR_LIMIT=600_000,
        _quote_connected=False,
        _live_warmup_mode=False,
        _evo_fetch_cb=None,
        _trading_mode="paper",
        _last_bars=[],
        _strategy_store=types.SimpleNamespace(list_strategies=lambda: []),
        strategy_var=types.SimpleNamespace(get=lambda: "DynamicExitPullbackStub"),
        chat=[],
        root=types.SimpleNamespace(
            after=lambda delay, fn, *args: fn(*args)),
        btn_send=types.SimpleNamespace(config=lambda **kwargs: None),
        _result=result,
    )
    app._gather_analysis_payload = lambda: (
        result, "report", "timeframe", "", ["design trade", "holdout trade"])
    app._ensure_chat_client = lambda: True
    app._resolve_strategy_source = lambda name, cls: _STUB_SOURCE
    app._append_chat = lambda role, text: app.chat.append((role, text))
    app.set_status = lambda msg: None
    app._on_chat_response = lambda plan: None
    app._on_chat_error = lambda msg: None
    app._remove_last_system_line = lambda: None
    app._evolution_capital_base = types.MethodType(
        rb.BacktestApp._evolution_capital_base, app)
    app._build_evolution_context = types.MethodType(
        rb.BacktestApp._build_evolution_context, app)
    app._start_evolution_pipeline = types.MethodType(
        rb.BacktestApp._start_evolution_pipeline, app)
    return app


class TestKnobSettleWiring:
    """Call the Saturday entry points. AST order does not catch a
    broken prefix check, a missing ``return``, or a backstop that
    never records (M5–M8).
    """

    def test_bot_evolution_records_advances_and_second_run_skips(
            self, tmp_path, monkeypatch):
        chat = MagicMock()
        chat.send_message.return_value = _CHANGE_PLAN
        chat.max_tokens = 1024
        chat.conversation = []
        discord = _DiscordLog()
        monkeypatch.setattr(rb, "_discord", discord)
        monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
        app = _wiring_app(tmp_path, chat)

        rb.BacktestApp._bot_evolution(app, auto_run=True)

        rec = load_knob_not_applicable(tmp_path)
        key = knob_applicability_key("DynamicExitPullbackStub", _STUB_SOURCE)
        assert rec is not None
        assert rec["outcome"] == KNOB_NOT_APPLICABLE
        assert rec["class_name"] == key["class_name"]
        assert rec["source_hash"] == key["source_hash"]
        assert rec["reason"] == _STUB_REASON
        mark = load_watermark(tmp_path)
        assert mark is not None and mark["trade_count"] == 1
        assert chat.send_message.call_count == 0
        assert chat.one_shot.call_count == 0
        assert app.chat == [("system", _STUB_LINE)]
        assert discord.msgs == [_STUB_LINE]

        rb.BacktestApp._bot_evolution(app, auto_run=True)

        assert load_knob_not_applicable(tmp_path) == rec
        assert load_watermark(tmp_path) == mark
        assert app.chat == [("system", _STUB_LINE), ("system", _STUB_LINE)]
        assert discord.msgs == [_STUB_LINE, _STUB_LINE]
        assert chat.send_message.call_count == 0
        assert chat.one_shot.call_count == 0

    def test_pipeline_backstop_records_and_second_run_skips(
            self, tmp_path, monkeypatch):
        chat = MagicMock()
        chat.send_message.return_value = _CHANGE_PLAN
        chat.max_tokens = 1024
        chat.conversation = []
        discord = _DiscordLog()
        monkeypatch.setattr(rb, "_discord", discord)
        monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
        bot_dir = tmp_path / "backstop"
        bot_dir.mkdir()
        app = _wiring_app(bot_dir, chat)

        rb.BacktestApp._start_evolution_pipeline(
            app, "preamble", ["design trade"], "", app._result,
            auto_run=True, holdout_days=14, bot_dir=bot_dir,
            design_cut_idx=4)

        rec = load_knob_not_applicable(bot_dir)
        key = knob_applicability_key("DynamicExitPullbackStub", _STUB_SOURCE)
        assert rec is not None
        assert rec["outcome"] == KNOB_NOT_APPLICABLE
        assert rec["class_name"] == key["class_name"]
        assert rec["source_hash"] == key["source_hash"]
        assert rec["reason"] == _STUB_REASON
        mark = load_watermark(bot_dir)
        assert mark is not None and mark["trade_count"] == 4
        assert chat.one_shot.call_count == 0
        assert discord.msgs == [_STUB_LINE]
        assert not any("Saturday knob mode refused" in m for m in discord.msgs)
        assert sum(1 for _role, text in app.chat if _STUB_REASON in text) == 1

        rb.BacktestApp._start_evolution_pipeline(
            app, "preamble", ["design trade"], "", app._result,
            auto_run=True, holdout_days=14, bot_dir=bot_dir,
            design_cut_idx=9)

        assert load_knob_not_applicable(bot_dir) == rec
        assert load_watermark(bot_dir) == mark
        assert chat.one_shot.call_count == 0
        assert discord.msgs == [_STUB_LINE, _STUB_LINE]
        assert sum(1 for _role, text in app.chat if _STUB_REASON in text) == 2
        assert not any(
            "Saturday knob mode refused" in text or "No new design-window" in text
            for _role, text in app.chat)
