"""D1 — per-class knob sets, weekly exclusion line, grid-change recheck.

Nova Q8: eligibility is the intersection of the master grid with the
knobs a class exposes, and a class needs at least two. The live 2.10
shape (``DynamicExitPullbackV3``: ema_period, atr_period, atr_stop_mult)
gets that 3-knob grid. Fewer than two rewritable knobs means no weekly
codegen. ``evolution.knob_ineligible_fallback`` stays ``none`` unless
set; ``structural_lane`` does not turn weekly codegen back on.

No bot name is special-cased. The grid follows the class.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path

import pytest

import run_backtest as rb
from src.evolution.knobs import (
    DYNAMIC_EXIT_PULLBACK_GRID,
    class_knob_eligibility,
    format_knob_exclusion_line,
    knob_applicability_key,
    knob_grid_hash,
    load_knob_not_applicable,
    normalize_knob_ineligible_fallback,
    saturday_knob_decision,
    saturday_knob_directives,
    saturday_knob_directives_if_eligible,
    save_knob_not_applicable,
    settle_knob_ineligibility,
)
from src.evolution.notify import load_watermark

_AT = "2026-10-17 05:05:00"
_LATER = "2026-10-24 05:05:00"
_V3_KEYS = ("ema_period", "atr_period", "atr_stop_mult")


class DynamicExitPullbackV3:
    """Class shape Nova names for 2.10: ema, atr period, atr stop.

    No ``atr_buffer_mult``. The class name is the strategy class, not a
    bot id.
    """

    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)
        self.atr_stop_mult = kwargs.get("atr_stop_mult", 2.5)


class DynamicExitPullbackOneKnob:
    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)


class DynamicExitPullbackTwoKnob:
    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)


def _cfg(tmp_path, monkeypatch, yaml_text: str) -> dict:
    (tmp_path / "settings.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    return rb._load_settings()


class TestClass210ThreeKnobGrid:
    def test_v3_grid_is_ema_atr_period_and_atr_stop(self, tmp_path, caplog):
        caplog.set_level(logging.INFO, logger="src.evolution.knobs")
        source = inspect.getsource(DynamicExitPullbackV3)
        effective, reason = class_knob_eligibility(
            DynamicExitPullbackV3, DYNAMIC_EXIT_PULLBACK_GRID, source)
        assert reason == ""
        assert tuple(effective) == _V3_KEYS
        for key in _V3_KEYS:
            assert effective[key] == DYNAMIC_EXIT_PULLBACK_GRID[key]
        assert "atr_buffer_mult" not in effective

        note = saturday_knob_directives_if_eligible(
            DynamicExitPullbackV3, source)
        assert "MANDATORY" in note
        assert "ema_period" in note
        assert "atr_period" in note
        assert "atr_stop_mult" in note
        assert "atr_buffer_mult" not in note
        assert "80 and 90 are out of auto" in note

        ok, knobs = saturday_knob_decision(
            "DynamicExitPullbackV3", {"ema_period": 60},
            DynamicExitPullbackV3, source)
        assert ok == ""
        assert knobs == {"ema_period": 60}
        banned, _ = saturday_knob_decision(
            "DynamicExitPullbackV3", {"ema_period": 80},
            DynamicExitPullbackV3, source)
        assert "80/90" in banned
        too_many, _ = saturday_knob_decision(
            "DynamicExitPullbackV3",
            {"ema_period": 60, "atr_period": 21, "atr_stop_mult": 2.0},
            DynamicExitPullbackV3, source)
        assert "max 2" in too_many

        # A not-applicable file from the old all-or-nothing rule must
        # not keep this class excluded.
        save_knob_not_applicable(
            tmp_path, "DynamicExitPullbackV3", source,
            "knob mode not applicable: DynamicExitPullbackV3 lacks knobs "
            "[atr_buffer_mult]; skipping knob mode",
            _AT)
        action, line = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackV3, source, 12, at=_AT)
        assert action == "eligible"
        assert line == ""
        assert load_watermark(tmp_path) is None
        assert caplog.records == []

    def test_full_grid_class_and_prompt_stay_on_todays_grid(self):
        from dynamic_exit_pullback_strategy_v2 import (
            DynamicExitPullbackStrategyV2)
        source = inspect.getsource(DynamicExitPullbackStrategyV2)
        effective, reason = class_knob_eligibility(
            DynamicExitPullbackStrategyV2, DYNAMIC_EXIT_PULLBACK_GRID, source)
        assert reason == ""
        assert list(effective) == list(DYNAMIC_EXIT_PULLBACK_GRID)
        text = saturday_knob_directives()
        assert text == (
            "## Saturday auto knob mode (MANDATORY on this weekly run)\n"
            "Apply an allowlisted discrete grid only. Change at most "
            "2 knobs (prefer exactly one). "
            "Do not invent values between the grid points.\n"
            "- ema_period: 40, 50, 60, 70 (80 and 90 are out of auto)\n"
            "- atr_period: 10, 14, 21\n"
            "- atr_stop_mult: 2.0, 2.5, 3.0\n"
            "- atr_buffer_mult: 0.3, 0.5, 0.8\n"
            "Put the change in the directives JSON, for example:\n"
            "```json\n"
            '{"action": "change", "knobs": {"ema_period": 60}}\n'
            "```\n"
            "Structural changes are not applied on Saturday: "
            "structural/codegen escalate, not auto.\n"
            "Do NOT propose ema_period 80 or 90.\n\n"
        )


class TestUnderTwoKnobsExcluded:
    def test_each_run_logs_the_exclusion_and_does_not_codegen(
            self, tmp_path, monkeypatch, caplog):
        from tests.test_evolution_issue164 import (
            _CHANGE_PLAN, _DiscordLog, _InlineThread, _STUB_LINE,
            _wiring_app)

        caplog.set_level(logging.INFO, logger="src.evolution.knobs")
        source = inspect.getsource(DynamicExitPullbackOneKnob)
        action, line = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 4, at=_AT)
        assert action == "recorded"
        assert line == (
            "excluded: DynamicExitPullbackOneKnob — "
            "knob mode not applicable: DynamicExitPullbackOneKnob has 1 "
            "rewritable grid knob [ema_period] (need >= 2); "
            "skipping knob mode; fallback=none"
        )
        assert line == format_knob_exclusion_line(
            "DynamicExitPullbackOneKnob",
            load_knob_not_applicable(tmp_path)["reason"], "none")
        again, again_line = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 9, at=_LATER)
        assert again == "skip"
        assert again_line == line
        assert load_watermark(tmp_path)["trade_count"] == 4
        logged = [r.getMessage() for r in caplog.records
                  if r.name == "src.evolution.knobs"]
        assert logged == [line, line]

        chat = _chat()
        chat.send_message.return_value = _CHANGE_PLAN
        discord = _DiscordLog()
        debug = []
        monkeypatch.setattr(rb, "_discord", discord)
        monkeypatch.setattr(rb, "_log", debug.append)
        monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
        app = _wiring_app(tmp_path / "weekly", chat)

        rb.BacktestApp._bot_evolution(app, auto_run=True)
        rb.BacktestApp._bot_evolution(app, auto_run=True)

        assert discord.msgs == [_STUB_LINE, _STUB_LINE]
        assert app.chat == [("system", _STUB_LINE), ("system", _STUB_LINE)]
        assert debug == [_STUB_LINE, _STUB_LINE]
        assert chat.send_message.call_count == 0
        assert chat.one_shot.call_count == 0
        assert "codegen" not in _STUB_LINE


def _chat():
    from unittest.mock import MagicMock
    chat = MagicMock()
    chat.max_tokens = 1024
    chat.conversation = []
    return chat


class TestGridChangeRecheck:
    def test_widening_the_grid_rechecks_eligibility(self, tmp_path, monkeypatch):
        import src.evolution.knobs as knobs

        narrow = {"ema_period": (40, 50, 60, 70)}
        monkeypatch.setattr(knobs, "DYNAMIC_EXIT_PULLBACK_GRID", narrow)
        two = inspect.getsource(DynamicExitPullbackTwoKnob)
        action, line = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackTwoKnob, two, 3, at=_AT)
        assert action == "recorded"
        assert "has 1 rewritable grid knob [ema_period]" in line
        assert "fallback=none" in line
        narrow_hash = load_knob_not_applicable(tmp_path)["grid_hash"]
        assert narrow_hash == knob_grid_hash(narrow)

        action, line = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackTwoKnob, two, 8, at=_LATER)
        assert action == "skip"
        assert line.startswith("excluded:")
        assert load_watermark(tmp_path)["trade_count"] == 3

        wide = {
            "ema_period": (40, 50, 60, 70),
            "atr_period": (10, 14, 21),
        }
        monkeypatch.setattr(knobs, "DYNAMIC_EXIT_PULLBACK_GRID", wide)
        action, reason = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackTwoKnob, two, 8, at=_LATER)
        assert action == "eligible"
        assert reason == ""
        assert load_watermark(tmp_path)["trade_count"] == 3
        effective, why = class_knob_eligibility(
            DynamicExitPullbackTwoKnob, wide, two)
        assert why == ""
        assert list(effective) == ["ema_period", "atr_period"]

    def test_value_change_rechecks_a_still_excluded_class(
            self, tmp_path, monkeypatch):
        import src.evolution.knobs as knobs

        narrow = {"ema_period": (40, 50, 60, 70)}
        monkeypatch.setattr(knobs, "DYNAMIC_EXIT_PULLBACK_GRID", narrow)
        source = inspect.getsource(DynamicExitPullbackOneKnob)
        first, _ = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 2, at=_AT)
        assert first == "recorded"
        second, _ = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 2, at=_LATER)
        assert second == "skip"

        widened = {"ema_period": (30, 40, 50, 60, 70, 80, 100)}
        monkeypatch.setattr(knobs, "DYNAMIC_EXIT_PULLBACK_GRID", widened)
        action, line = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 6, at=_LATER)
        assert action == "recorded"
        assert "excluded:" in line
        assert "fallback=none" in line
        rec = load_knob_not_applicable(tmp_path)
        assert rec["grid_hash"] == knob_grid_hash(widened)
        assert rec["grid_hash"] != knob_grid_hash(narrow)
        assert load_watermark(tmp_path)["trade_count"] == 6

    def test_record_without_grid_hash_is_not_a_cache_hit(self, tmp_path):
        """A pre-D1 file (no grid hash) must not skip the next Saturday."""
        source = inspect.getsource(DynamicExitPullbackOneKnob)
        key = knob_applicability_key("DynamicExitPullbackOneKnob", source)
        path = tmp_path / "knob_not_applicable.json"
        path.write_text(
            '{"outcome": "not applicable", "class_name": '
            '"DynamicExitPullbackOneKnob", "source_hash": "%s", '
            '"reason": "old", "at": "%s"}' % (key["source_hash"], _AT),
            encoding="utf-8")
        action, line = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 5, at=_LATER)
        assert action == "recorded"
        assert line.startswith("excluded:")
        assert load_knob_not_applicable(tmp_path)["grid_hash"] == key["grid_hash"]
        assert "old" not in load_knob_not_applicable(tmp_path)["reason"]


class TestFallbackOffByDefault:
    @pytest.mark.parametrize("raw,expected", [
        (None, "none"),
        ("", "none"),
        ("none", "none"),
        ("NONE", "none"),
        ("codegen", "none"),
        ("CODEGEN", "none"),
        ("structural_lane", "structural_lane"),
        ("STRUCTURAL_LANE", "structural_lane"),
        ("structural-lane", "none"),
    ])
    def test_only_structural_lane_is_distinct_from_none(self, raw, expected):
        assert normalize_knob_ineligible_fallback(raw) == expected

    def test_settings_default_is_none_and_codegen_does_not_stick(
            self, tmp_path, monkeypatch):
        missing = _cfg(tmp_path, monkeypatch, "evolution:\n  cadence: weekly\n")
        assert missing["evolution_knob_ineligible_fallback"] == "none"
        absent = _cfg(
            tmp_path, monkeypatch, "trading:\n  allow_live_override: false\n")
        assert absent["evolution_knob_ineligible_fallback"] == "none"
        chosen = _cfg(
            tmp_path, monkeypatch,
            'evolution:\n  knob_ineligible_fallback: "structural_lane"\n')
        assert chosen["evolution_knob_ineligible_fallback"] == "structural_lane"
        retired = _cfg(
            tmp_path, monkeypatch,
            'evolution:\n  knob_ineligible_fallback: "codegen"\n')
        assert retired["evolution_knob_ineligible_fallback"] == "none"
        example = Path("settings.example.yaml").read_text(encoding="utf-8")
        assert 'knob_ineligible_fallback: "none"' in example
        src = Path("src/evolution/knobs.py").read_text(encoding="utf-8")
        assert "2.10" not in src

    def test_settle_without_fallback_arg_logs_none(self, tmp_path):
        source = inspect.getsource(DynamicExitPullbackOneKnob)
        _action, line = settle_knob_ineligibility(
            tmp_path, DynamicExitPullbackOneKnob, source, 1, at=_AT)
        assert line.endswith("; fallback=none")
        assert load_knob_not_applicable(tmp_path)["fallback"] == "none"

    def test_structural_lane_still_excludes_without_weekly_codegen(
            self, tmp_path, monkeypatch):
        from tests.test_evolution_issue164 import (
            _CHANGE_PLAN, _DiscordLog, _InlineThread, _wiring_app)

        chat = _chat()
        chat.send_message.return_value = _CHANGE_PLAN
        discord = _DiscordLog()
        monkeypatch.setattr(rb, "_discord", discord)
        monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
        app = _wiring_app(tmp_path, chat)
        app._settings["evolution_knob_ineligible_fallback"] = "structural_lane"
        # The lane defaults on. This case is the off switch: the fallback
        # string alone must not start weekly codegen.
        app._settings["evolution_structural_lane"] = {"enabled": False}

        rb.BacktestApp._bot_evolution(app, auto_run=True)

        assert len(discord.msgs) == 1
        assert discord.msgs[0].startswith("excluded: DynamicExitPullbackStub — ")
        assert discord.msgs[0].endswith("; fallback=structural_lane")
        assert chat.send_message.call_count == 0
        assert chat.one_shot.call_count == 0
        assert load_knob_not_applicable(tmp_path)["fallback"] == "structural_lane"
