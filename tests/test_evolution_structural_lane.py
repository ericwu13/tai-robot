"""PR-I — structural lane and Saturday propose mode.

Proposal mode records evidence and applies nothing. Graduation and
retirement recommendations are text: they do not write settings, an
allowlist, or bot state. The structural lane is a separate gate from
the knob grid and stays off unless configured.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime
from pathlib import Path

import pytest

import run_backtest as rb
from src.evolution.paper_gate import (
    RetireRecommendation,
    emit_retire_recommendation,
)
from src.evolution.structural_lane import (
    FORM_IDS,
    STRUCTURAL_CODEGEN_ATTEMPTS,
    Admission,
    admission_decision,
    applied_graduation_stamps,
    assess_graduation,
    default_lane_config,
    emit_graduation_recommendation,
    empty_ledger,
    ineligible_structural_divert,
    load_ledger,
    make_incubation,
    normalize_saturday_mode,
    parse_lane_config,
    parse_structural_form,
    passes_native_bar_guard,
    pool_promotion_allowed,
    record_proposal,
    reserve_structural_slot,
    save_ledger,
    settings_from_evolution,
    structural_codegen_prompt,
    structural_paper_decision,
    structural_plan_block,
    structural_saturday_followup,
)

_NOW = datetime(2026, 10, 24, 6, 0)
_PULLBACK = "PullbackFamily"
_BREAKOUT = "BreakoutFamily"


def _cfg(tmp_path, monkeypatch, text: str) -> dict:
    (tmp_path / "settings.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    return rb._load_settings()


def _bytes(path: Path):
    return path.read_bytes() if path.exists() else None


def _frozen_tree(root: Path) -> dict:
    names = (
        "settings.yaml", "allowlist.json", "session.json",
        "state.json", "evolution_watermark.json",
    )
    return {name: _bytes(root / name) for name in names}


def _plant(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.yaml").write_text("evolution:\n  saturday_mode: promote\n")
    (root / "allowlist.json").write_text("[]")
    (root / "session.json").write_text("{}")
    (root / "state.json").write_text('{"running": true}')
    (root / "evolution_watermark.json").write_text('{"trade_count": 3}')


def _spies():
    calls = []
    return calls, {
        "promote": lambda: calls.append("pool"),
        "save_store": lambda: calls.append("store"),
        "write_settings": lambda: calls.append("settings"),
        "write_allowlist": lambda: calls.append("allowlist"),
        "mutate_strategies": lambda: calls.append("strategies"),
    }


def _week_trades(pnl: int, weeks: tuple[str, ...], per_week: int = 4):
    rows = []
    for stamp in weeks:
        for hour in range(per_week):
            rows.append({
                "pnl": pnl,
                "exit_dt": f"{stamp} {10 + hour:02d}:00",
            })
    return rows


_WEEKS = ("2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26", "2026-11-02")


def _passing_observation(bot_id: str, month: str = "2026-10", sessions: int = 21):
    return {
        "bot_id": bot_id,
        "month": month,
        "sessions": sessions,
        "paper_trades": _week_trades(100, _WEEKS),
        "baseline_trades": _week_trades(10, _WEEKS),
        "backtest_mean_pnl": 100,
        "slippage": {
            "candidate": {"slippage": 1.0, "timeouts": 0},
            "baseline": {"slippage": 2.0, "timeouts": 1},
        },
    }


class TestSaturdayModeAppliesNothing:
    def test_default_is_propose_and_promote_is_opt_in(self):
        stored = settings_from_evolution({})
        assert stored["evolution_saturday_mode"] == "propose"
        assert stored["evolution_structural_lane"]["enabled"] is False
        assert stored["evolution_structural_lane"]["warnings"] == []
        assert normalize_saturday_mode(None) == "propose"
        assert normalize_saturday_mode("nope") == "propose"
        assert normalize_saturday_mode("propose") == "propose"
        assert normalize_saturday_mode("promote") == "promote"
        junk = settings_from_evolution({"saturday_mode": "nope"})
        assert junk["evolution_saturday_mode"] == "propose"
        assert any("using propose" in w
                   for w in junk["evolution_structural_lane"]["warnings"])
        # Missing mode is the Saturday default: record evidence, no pool.
        # An explicit promote still promotes. A manual run is neither.
        assert pool_promotion_allowed(
            saturday_mode=None, structural_lane=False,
            eligible=True, auto_run=True) is False
        assert pool_promotion_allowed(
            saturday_mode="promote", structural_lane=False,
            eligible=True, auto_run=True) is True
        assert pool_promotion_allowed(
            saturday_mode="propose", structural_lane=False,
            eligible=True, auto_run=False) is True

    @pytest.mark.parametrize("mode,structural,eligible,auto,allowed", [
        ("promote", False, True, True, True),
        ("propose", False, True, True, False),
        ("propose", False, True, False, True),
        ("promote", True, True, True, False),
        ("propose", True, True, False, False),
        ("promote", False, False, True, False),
    ])
    def test_pool_promotion_gate(self, mode, structural, eligible, auto, allowed):
        assert pool_promotion_allowed(
            saturday_mode=mode, structural_lane=structural,
            eligible=eligible, auto_run=auto) is allowed

    def test_propose_record_does_not_apply_or_write_settings(self, tmp_path):
        home = tmp_path / "home"
        _plant(home)
        before = _frozen_tree(home)
        calls, spies = _spies()
        body = record_proposal(
            tmp_path / "lane",
            bot_id=_PULLBACK,
            month="2026-10",
            lane="saturday",
            status="proposed",
            evidence={
                "applied": True,
                "verdict_passed": True,
                "reasons": ["fold Δ positive"],
                "dsr_trials": 0,
            },
            now=_NOW,
            **spies,
        )
        assert calls == []
        assert body["applied"] is False
        assert body["settings_written"] is False
        assert body["allowlist_written"] is False
        assert body["strategy_store_saved"] is False
        assert body["pool_promoted"] is False
        assert "applied" not in body["evidence"]
        assert body["evidence"]["reasons"] == ["fold Δ positive"]
        saved = json.loads(Path(body["path"]).read_text(encoding="utf-8"))
        assert saved["applied"] is False
        assert saved["lane"] == "saturday"
        assert _frozen_tree(home) == before
        from src.evolution.structural_lane import format_proposal_notice
        notice = format_proposal_notice(body)
        assert "nothing applied" in notice
        assert "StrategyStore" in notice

    def test_structural_proposal_is_not_a_knob_promotion(self, tmp_path):
        calls, spies = _spies()
        body = record_proposal(
            tmp_path / "lane",
            bot_id=_BREAKOUT,
            month="2026-10",
            lane="structural",
            status="proposed",
            form="change_exit_type",
            evidence={"dsr_trials": 1, "reasons": ["Q2"]},
            now=_NOW,
            **spies,
        )
        assert calls == []
        assert body["applied"] is False
        assert body["dsr_trials"] == 1
        assert body["form"] == "change_exit_type"
        assert "Pullback" not in Path(body["path"]).name
        assert "BreakoutFamily" in Path(body["path"]).name


class TestStructuralLaneGate:
    def test_disabled_and_wrong_fallback_do_not_admit(self):
        ledger = empty_ledger()
        cfg = default_lane_config()
        assert admission_decision(
            enabled=False, ineligible=True, fallback="structural_lane",
            bot_id=_PULLBACK, month="2026-10", ledger=ledger, config=cfg,
        ).admit is False
        cfg = default_lane_config()
        cfg["enabled"] = True
        refused = admission_decision(
            enabled=True, ineligible=True, fallback="none",
            bot_id=_PULLBACK, month="2026-10", ledger=ledger, config=cfg,
        )
        assert refused.admit is False
        assert "fallback" in refused.reason
        # Eligible class is not blocked by the knob fallback.
        assert admission_decision(
            enabled=True, ineligible=False, fallback="none",
            bot_id=_BREAKOUT, month="2026-10", ledger=ledger, config=cfg,
        ).admit is True

    def test_one_per_bot_per_month_and_fleet_cap(self, tmp_path):
        ledger_path = tmp_path / "structural_lane.json"
        cfg = default_lane_config()
        cfg["enabled"] = True
        first = reserve_structural_slot(
            ledger_path, bot_id=_PULLBACK, month="2026-10", config=cfg,
            now=_NOW, ineligible=False, fallback="none")
        assert first.admit is True
        second = reserve_structural_slot(
            ledger_path, bot_id=_PULLBACK, month="2026-10", config=cfg,
            now=_NOW, ineligible=False, fallback="none")
        assert second.admit is False
        assert "per_bot_per_month" in second.reason
        # A different family the same month is still a separate slot.
        other = reserve_structural_slot(
            ledger_path, bot_id=_BREAKOUT, month="2026-10", config=cfg,
            now=_NOW)
        assert other.admit is True
        # Next month is a new slot for the first family.
        nxt = reserve_structural_slot(
            ledger_path, bot_id=_PULLBACK, month="2026-11", config=cfg,
            now=_NOW)
        assert nxt.admit is True

        ledger = load_ledger(ledger_path)
        ledger["incubations"] = [
            make_incubation(bot_id=bot, month="2026-09", form="side_split",
                            status="paper_recommended", now=_NOW)
            for bot in (_PULLBACK, _BREAKOUT, "ThirdFamily")
        ]
        save_ledger(ledger_path, ledger)
        blocked = admission_decision(
            enabled=True, ineligible=False, fallback="none",
            bot_id="FourthFamily", month="2026-12",
            ledger=load_ledger(ledger_path), config=cfg)
        assert blocked.admit is False
        assert "max in paper" in blocked.reason

    def test_reserve_writes_the_ledger_only(self, tmp_path):
        home = tmp_path / "home"
        _plant(home)
        before = _frozen_tree(home)
        cfg = default_lane_config()
        cfg["enabled"] = True
        reserve_structural_slot(
            tmp_path / "lane" / "structural_lane.json",
            bot_id=_PULLBACK, month="2026-10", config=cfg, now=_NOW)
        assert _frozen_tree(home) == before
        ledger = load_ledger(tmp_path / "lane" / "structural_lane.json")
        assert ledger["slots"][0]["applied"] is False
        assert "applied_at" not in ledger["slots"][0]

    def test_corrupt_ledger_fails_closed(self, tmp_path):
        path = tmp_path / "structural_lane.json"
        path.write_text("{not json", encoding="utf-8")
        cfg = default_lane_config()
        cfg["enabled"] = True
        decision = admission_decision(
            enabled=True, ineligible=False, fallback="none",
            bot_id=_PULLBACK, month="2026-10",
            ledger=load_ledger(path), config=cfg)
        assert decision.admit is False
        assert "unreadable" in decision.reason

    def test_string_enabled_does_not_turn_the_lane_on(self):
        cfg = parse_lane_config({"enabled": "true", "paper_days": 14})
        assert cfg["enabled"] is False
        assert any("paper_days" in w for w in cfg["warnings"])
        assert cfg["min_paper_sessions"] == 21
        assert cfg["min_paper_trades"] == 20
        assert cfg["timeout_sessions"] == 45

    def test_paper_days_warning_is_only_inside_structural_lane(self):
        from src.evolution.pipeline import month_gate_config_from_mapping

        elsewhere = settings_from_evolution({
            "paper_days": 14,
            "month_gate": {"paper_days": 14, "min_sessions": 12},
            "relative": {"paper_days": 14},
            "retire": {"paper_days": 14},
        })
        assert elsewhere["evolution_structural_lane"]["warnings"] == []
        assert elsewhere["evolution_structural_lane"]["min_paper_sessions"] == 21
        assert elsewhere["evolution_structural_lane"]["min_paper_trades"] == 20
        month = month_gate_config_from_mapping({
            "month_gate": {"paper_days": 14, "latest_def": "merge_stub"},
        })
        assert not any("paper_days" in w for w in month.warnings)

        lane = settings_from_evolution({
            "structural_lane": {"enabled": True, "paper_days": 14},
        })
        warnings = lane["evolution_structural_lane"]["warnings"]
        assert warnings == [
            "structural_lane.paper_days is not the incubation gate; "
            "Nova Q6 uses min_paper_sessions and min_paper_trades"]
        assert lane["evolution_structural_lane"]["enabled"] is True
        assert lane["evolution_structural_lane"]["min_paper_sessions"] == 21
        assert lane["evolution_structural_lane"]["min_paper_trades"] == 20
        assert lane["evolution_structural_lane"]["timeout_sessions"] == 45

    def test_knob_plan_is_not_a_structural_form(self):
        knob = '{"action": "change", "knobs": {"ema_period": 60}}'
        assert parse_structural_form(knob) is None
        assert parse_structural_form(
            "structural_form: change_exit_type\n"
            "structural_form: side_split") is None
        assert parse_structural_form(
            "structural_form: change_exit_type") == "change_exit_type"
        assert parse_structural_form(structural_plan_block()) is None
        block = structural_plan_block()
        for form in FORM_IDS:
            assert form in block
        assert "knob grid" in block
        prompt = structural_codegen_prompt(
            source="class S: pass", plan="structural_form: side_split",
            candidate_name="SEvo1", form="side_split")
        assert "side_split" in prompt
        assert "only attempt" in prompt
        assert STRUCTURAL_CODEGEN_ATTEMPTS == 1

    def test_native_bars_and_trial_penalty_fail_closed(self):
        assert passes_native_bar_guard(149) is False
        assert passes_native_bar_guard(150) is True
        ok, reasons = structural_paper_decision(
            sandbox_ok=True, native_bars=200, q2_passed=True,
            pbo=None, dsr=None)
        assert ok is False
        assert any("not supplied" in line for line in reasons)
        ok, reasons = structural_paper_decision(
            sandbox_ok=True, native_bars=200, q2_passed=True,
            pbo=0.2, dsr=0.95)
        assert ok is True
        assert any("recommendation only" in line for line in reasons)
        bad, _ = structural_paper_decision(
            sandbox_ok=True, native_bars=200, q2_passed=True,
            pbo=0.8, dsr=0.95)
        assert bad is False

    def test_module_does_not_import_the_knob_grid_or_open_drafts(self):
        tree = ast.parse(Path("src/evolution/structural_lane.py").read_text(
            encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
        joined = " ".join(modules)
        for banned in ("knobs", "paper_gate", "grid_search",
                       "trial_penalty", "history"):
            assert banned not in joined


class TestRecommendOnly:
    def test_graduation_recommends_without_writing_state(self, tmp_path):
        home = tmp_path / "home"
        _plant(home)
        before = _frozen_tree(home)
        obs = _passing_observation(_PULLBACK)
        rec = assess_graduation(
            paper_trades=obs["paper_trades"],
            baseline_trades=obs["baseline_trades"],
            sessions=21,
            backtest_mean_pnl=100,
            slippage=obs["slippage"],
        )
        assert rec.status == "graduation_recommended"
        assert rec.recommend is True
        assert rec.evidence["applied"] is False
        logs, changes, report = [], [], []

        def _changelog(**kwargs):
            changes.append(kwargs)

        emit_graduation_recommendation(
            rec, log=logs.append, changelog=_changelog, report=report,
            strategy_name=_PULLBACK, version="unchanged")
        retire = RetireRecommendation(
            recommend=True, reasons=["performance"],
            evidence={"condition_a": False, "condition_b": False,
                      "thresholds": {}})
        emit_retire_recommendation(
            retire, log=logs.append, changelog=_changelog, report=report,
            strategy_name=_BREAKOUT, version="unchanged")
        assert _frozen_tree(home) == before
        assert logs and report
        assert all(c["version_before"] == c["version_after"] for c in changes)
        text = "\n".join(logs)
        assert "CEO call" in text
        assert "action: none" in text
        assert "RETIRE RECOMMENDATION only" in text
        assert "no strategy" in text.lower() or "No strategy" in text

    def test_missing_slippage_and_timeout_do_not_graduate(self):
        obs = _passing_observation(_BREAKOUT)
        waiting = assess_graduation(
            paper_trades=obs["paper_trades"],
            baseline_trades=obs["baseline_trades"],
            sessions=21,
            backtest_mean_pnl=100,
            slippage=None,
        )
        assert waiting.status == "open"
        assert waiting.recommend is False
        closed = assess_graduation(
            paper_trades=obs["paper_trades"],
            baseline_trades=obs["baseline_trades"],
            sessions=46,
            backtest_mean_pnl=100,
            slippage=obs["slippage"],
        )
        assert closed.status == "inconclusive"
        assert closed.recommend is True
        assert "inconclusive" in "\n".join(closed.reasons)

    def test_followup_updates_only_the_ledger(self, tmp_path):
        home = tmp_path / "home"
        _plant(home)
        before = _frozen_tree(home)
        ledger_path = tmp_path / "lane" / "structural_lane.json"
        ledger = empty_ledger()
        ledger["incubations"] = [make_incubation(
            bot_id=_PULLBACK, month="2026-10", form="add_session_filter",
            status="paper_recommended", now=_NOW)]
        save_ledger(ledger_path, ledger)
        changes = []
        structural_saturday_followup(
            ledger_path,
            observations=[_passing_observation(_PULLBACK)],
            log=lambda _text: None,
            changelog=lambda **kw: changes.append(kw),
            report=[],
            strategy_name=_PULLBACK,
            version="v-same",
        )
        assert _frozen_tree(home) == before
        updated = load_ledger(ledger_path)
        row = updated["incubations"][0]
        assert row["status"] == "graduation_recommended"
        assert row["applied"] is False
        assert "applied_at" not in row
        assert changes
        assert changes[0]["version_before"] == changes[0]["version_after"] == "v-same"
        # A recommendation is not an applied graduation.
        assert applied_graduation_stamps(updated) == []

    def test_empty_observation_does_not_rewrite_or_touch_settings(self, tmp_path):
        home = tmp_path / "home"
        _plant(home)
        before = _frozen_tree(home)
        ledger_path = tmp_path / "structural_lane.json"
        ledger = empty_ledger()
        ledger["incubations"] = [make_incubation(
            bot_id=_BREAKOUT, month="2026-10", form="side_split",
            status="not_admitted", now=_NOW)]
        save_ledger(ledger_path, ledger)
        raw = ledger_path.read_bytes()
        assert structural_saturday_followup(ledger_path, observations=()) == []
        assert ledger_path.read_bytes() == raw
        assert _frozen_tree(home) == before


class TestSettingsAndWiring:
    def test_loader_defaults_and_example_yaml(self, tmp_path, monkeypatch):
        loaded = _cfg(tmp_path, monkeypatch, "evolution:\n  cadence: weekly\n")
        assert loaded["evolution_saturday_mode"] == "propose"
        assert loaded["evolution_structural_lane"]["enabled"] is False
        opt_in = _cfg(
            tmp_path, monkeypatch, "evolution:\n  saturday_mode: promote\n")
        assert opt_in["evolution_saturday_mode"] == "promote"
        chosen = _cfg(
            tmp_path, monkeypatch,
            "evolution:\n  saturday_mode: propose\n"
            "  structural_lane:\n    enabled: true\n"
            "    ledger_path: /tmp/evo-lane.json\n")
        assert chosen["evolution_saturday_mode"] == "propose"
        assert chosen["evolution_structural_lane"]["enabled"] is True
        example = Path("settings.example.yaml").read_text(encoding="utf-8")
        assert 'saturday_mode: "propose"' in example
        assert "latest_def: merge_stub" in example
        assert "enabled: false" in example
        assert "per_bot_per_month: 1" in example

    def test_pipeline_promotes_only_through_the_gate(self):
        src = inspect.getsource(rb.BacktestApp._start_evolution_pipeline)
        assert src.index("pool_promotion_allowed") < src.index(
            "record_validated_candidate")
        assert "evolution_codegen_structural" in src
        assert src.count("for attempt in (1, 2)") == 1
        evo = inspect.getsource(rb.BacktestApp._bot_evolution)
        assert evo.index("ineligible_structural_divert") < evo.index(
            "if not structural_only")
        assert evo.index("waiting for sample") < evo.index(
            "_maybe_launch_structural")
        launch = inspect.getsource(rb.BacktestApp._maybe_launch_structural)
        assert "guard_held=True" in launch
        assert "_persist_evolution_history" in launch
        assert "_evolution_try_begin" not in launch
        assert "evolution_history.jsonl" not in launch
        assert ".lock" not in launch

    def test_disabled_lane_still_excludes_without_codegen(
            self, tmp_path, monkeypatch):
        from tests.test_evolution_d1_knobs import _chat
        from tests.test_evolution_issue164 import _DiscordLog, _InlineThread, _wiring_app

        chat = _chat()
        discord = _DiscordLog()
        monkeypatch.setattr(rb, "_discord", discord)
        monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
        app = _wiring_app(tmp_path, chat)
        app._settings["evolution_knob_ineligible_fallback"] = "structural_lane"
        app._settings["evolution_structural_lane"] = {
            "enabled": False,
            "ledger_path": str(tmp_path / "lane.json"),
        }

        def _boom(*_a, **_k):
            raise AssertionError("structural lane launched while disabled")

        monkeypatch.setattr(rb.BacktestApp, "_maybe_launch_structural", _boom)
        rb.BacktestApp._bot_evolution(app, auto_run=True)
        assert chat.send_message.call_count == 0
        assert chat.one_shot.call_count == 0
        assert discord.msgs[0].endswith("; fallback=structural_lane")
        assert not (tmp_path / "lane.json").exists()

    def test_enabled_lane_is_gated_by_sample_and_fallback(
            self, tmp_path, monkeypatch):
        from tests.test_evolution_d1_knobs import _chat
        from tests.test_evolution_issue164 import _DiscordLog, _InlineThread, _wiring_app

        chat = _chat()
        discord = _DiscordLog()
        monkeypatch.setattr(rb, "_discord", discord)
        monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
        app = _wiring_app(tmp_path / "none", chat)
        app._settings["evolution_structural_lane"] = {
            "enabled": True,
            "ledger_path": str(tmp_path / "none-lane.json"),
        }
        app._settings["evolution_knob_ineligible_fallback"] = "none"

        def _boom(*_a, **_k):
            raise AssertionError("lane ran for a none fallback")

        monkeypatch.setattr(rb.BacktestApp, "_maybe_launch_structural", _boom)
        rb.BacktestApp._bot_evolution(app, auto_run=True)
        assert chat.send_message.call_count == 0
        assert any(m.startswith("excluded:") for m in discord.msgs)

        # Fresh dir so the first D1 record has not already watermarked it.
        # One design trade is below the default sample floor. The lane
        # must not spend the month or call the model.
        chat2 = _chat()
        app2 = _wiring_app(tmp_path / "sample", chat2)
        app2._settings["evolution_structural_lane"] = {
            "enabled": True,
            "ledger_path": str(tmp_path / "sample-lane.json"),
        }
        app2._settings["evolution_knob_ineligible_fallback"] = "structural_lane"
        rb.BacktestApp._bot_evolution(app2, auto_run=True)
        assert chat2.send_message.call_count == 0
        assert chat2.one_shot.call_count == 0
        assert not (tmp_path / "sample-lane.json").exists()
        assert any("waiting for sample" in text for _role, text in app2.chat)

    def test_launch_reserves_once_and_marks_the_pipeline(self, tmp_path):
        from tests.test_evolution_issue164 import _wiring_app
        from unittest.mock import MagicMock

        chat = MagicMock()
        app = _wiring_app(tmp_path, chat)
        home = tmp_path / "home"
        _plant(home)
        before = _frozen_tree(home)
        ledger = tmp_path / "lane" / "structural_lane.json"
        app._settings["evolution_structural_lane"] = {
            "enabled": True,
            "ledger_path": str(ledger),
        }
        app._settings["evolution_knob_ineligible_fallback"] = "structural_lane"
        captured = []

        def _pipeline(*args, **kwargs):
            captured.append((args, kwargs))

        app._start_evolution_pipeline = _pipeline
        assert rb.BacktestApp._maybe_launch_structural(
            app, "preamble", ["trade"], "", app._result,
            True, 14, tmp_path, 1, True) is True
        assert rb.BacktestApp._maybe_launch_structural(
            app, "preamble", ["trade"], "", app._result,
            True, 14, tmp_path, 1, True) is False
        assert len(captured) == 1
        args, kwargs = captured[0]
        assert kwargs["structural_lane"] is True
        assert kwargs["guard_held"] is True
        assert kwargs["structural_month"]
        assert "Structural lane" in args[0]
        assert "knob grid" in args[0]
        assert load_ledger(ledger)["slots"][0]["applied"] is False
        assert _frozen_tree(home) == before
        from src.evolution.history import (
            evolution_history_path, load_evolution_history)
        rows = load_evolution_history(evolution_history_path(tmp_path))
        assert len(rows) == 1
        assert rows[0]["final_status"] == "refused"
        assert rows[0]["reason_kind"] == "refused"
        assert rows[0]["ai_calls"] == 0
        assert "not run" in rows[0]["reason"]

    def test_ineligible_divert_matches_the_gate(self, tmp_path):
        cfg_settings = {
            "evolution_structural_lane": {
                "enabled": True,
                "ledger_path": str(tmp_path / "lane.json"),
            }
        }
        assert ineligible_structural_divert(
            cfg_settings, fallback="none", bot_id=_PULLBACK,
            month="2026-10", ledger_path=tmp_path / "lane.json") is False
        assert ineligible_structural_divert(
            cfg_settings, fallback="structural_lane", bot_id=_BREAKOUT,
            month="2026-10", ledger_path=tmp_path / "missing.json") is True
        assert ineligible_structural_divert(
            {"evolution_structural_lane": {"enabled": False}},
            fallback="structural_lane", bot_id=_PULLBACK,
            month="2026-10", ledger_path=tmp_path / "missing.json") is False


def test_propose_saturday_appends_one_history_line_and_skips_the_pool(
        tmp_path, monkeypatch):
    """Propose mode uses the D2 append and does not promote the pool."""
    from tests.test_evolution_history import (
        _validation_app, _patch_validation, load_evolution_history,
        evolution_history_path)
    from tests.test_evolution_min_design_trades import _InlineThread

    rb._evolution_guard_clear()
    ledger = tmp_path / "lane" / "structural_lane.json"
    calls = []

    def _pool(*_args, **kwargs):
        calls.append(kwargs)
        return "should-not-promote"

    monkeypatch.setattr(
        "src.evolution.pool.record_validated_candidate", _pool)
    _patch_validation(monkeypatch, passed=True)
    monkeypatch.setattr(
        "src.evolution.pipeline.eligible_for_validated_pool", lambda verdict: True)
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    from unittest.mock import MagicMock
    from tests.test_evolution_history import _HIST_CODE, _HIST_PLAN
    chat = MagicMock()
    chat.send_message.return_value = _HIST_PLAN
    chat.one_shot.return_value = _HIST_CODE
    chat.max_tokens = 1024
    chat.conversation = []
    app = _validation_app(tmp_path, chat, paper_gate="relative")
    app._settings["evolution_structural_lane"] = {
        "enabled": False,
        "ledger_path": str(ledger),
    }
    rb.BacktestApp._start_evolution_pipeline(
        app, "preamble", ["design trade"], "", app._result,
        auto_run=True, holdout_days=14, bot_dir=str(tmp_path),
        design_cut_idx=1)
    rows = load_evolution_history(evolution_history_path(tmp_path))
    assert app.errors == [], app.chat
    assert calls == []
    assert len(rows) == 1, rows
    assert rows[0]["final_status"] == "pass"
    assert rows[0]["reason_kind"] == "validation"
    assert rows[0]["reason"] == "proposal recorded; nothing applied"
    assert rows[0]["gates"]["month"] is not None
    proposals = list((tmp_path / "lane" / "proposals").glob("*.json"))
    assert len(proposals) == 1
    body = json.loads(proposals[0].read_text(encoding="utf-8"))
    assert body["lane"] == "saturday"
    assert body["applied"] is False
    assert body["pool_promoted"] is False
    assert not list(tmp_path.glob("*.lock"))
    assert rb._evolution_in_progress == set()


def test_structural_saturday_uses_the_same_run_guard(tmp_path, monkeypatch):
    """A structural Saturday holds the in-process guard and appends one line."""
    import threading
    import time
    from unittest.mock import MagicMock
    from tests.test_evolution_min_design_trades import (
        _DiscordLog, _NO_CHANGE, _app, _design_book)
    from src.evolution.history import (
        evolution_history_path, load_evolution_history)

    rb._evolution_guard_clear()
    ledger = tmp_path / "lane" / "structural_lane.json"
    entered = threading.Event()
    release = threading.Event()
    chat = MagicMock()
    chat.max_tokens = 1024
    chat.conversation = []

    def _block(*_args, **_kwargs):
        entered.set()
        release.wait(5)
        return _NO_CHANGE

    chat.send_message.side_effect = _block
    app = _app(tmp_path, _design_book(30), chat)
    app._settings["evolution_structural_lane"] = {
        "enabled": True,
        "ledger_path": str(ledger),
    }
    app._settings["evolution_min_design_trades"] = 0
    discord = _DiscordLog()
    logs = []
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(rb, "_log", logs.append)
    try:
        rb.BacktestApp._bot_evolution(app, auto_run=True)
        assert entered.wait(5), app.chat
        rb.BacktestApp._bot_evolution(app, auto_run=False)
        assert any("already running" in line for line in logs)
        assert any("manual Evolve" in text for _role, text in app.chat)
        assert load_evolution_history(evolution_history_path(tmp_path)) == []
        assert not list(Path(tmp_path).glob("*.lock"))
    finally:
        release.set()
        rb._evolution_guard_clear()

    deadline = time.time() + 5
    while time.time() < deadline:
        rows = load_evolution_history(evolution_history_path(tmp_path))
        if rows and rows[0]["reason_kind"] == "no_change":
            break
        time.sleep(0.02)
    else:
        raise AssertionError(load_evolution_history(evolution_history_path(tmp_path)))
    assert len(rows) == 1
    assert rows[0]["final_status"] == "no_change"
    assert rows[0]["ai_calls"] == 1
    proposals = list((tmp_path / "lane" / "proposals").glob("*.json"))
    assert len(proposals) == 1
    body = json.loads(proposals[0].read_text(encoding="utf-8"))
    assert body["lane"] == "structural"
    assert body["applied"] is False
    assert body["pool_promoted"] is False


def test_admission_is_a_decision_not_a_write(tmp_path):
    """The pure gate does not create a ledger."""
    decision = admission_decision(
        enabled=True, ineligible=False, fallback="none",
        bot_id=_PULLBACK, month="2026-10", ledger=empty_ledger(),
        config={**default_lane_config(), "enabled": True})
    assert isinstance(decision, Admission)
    assert decision.admit is True
    assert list(tmp_path.iterdir()) == []
