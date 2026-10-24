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
from datetime import datetime, timedelta
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
        assert stored["evolution_structural_lane"]["enabled"] is True
        assert stored["evolution_structural_lane"]["warnings"] == []
        assert default_lane_config()["enabled"] is True
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
            pbo=None, dsr=None, selection_winner=True)
        assert ok is False
        assert any("not supplied" in line for line in reasons)
        # PBO is logged. It does not decide. DSR must clear 0.95.
        ok, reasons = structural_paper_decision(
            sandbox_ok=True, native_bars=200, q2_passed=True,
            pbo=0.99, dsr=0.99, selection_winner=True)
        assert ok is True, reasons
        assert any("informational" in line for line in reasons)
        assert any("recommendation only" in line for line in reasons)
        below, below_reasons = structural_paper_decision(
            sandbox_ok=True, native_bars=200, q2_passed=True,
            pbo=0.1, dsr=0.93, selection_winner=True)
        assert below is False
        assert any(line.startswith("DSR ") and "FAIL" in line
                   for line in below_reasons)
        not_winner, winner_reasons = structural_paper_decision(
            sandbox_ok=True, native_bars=200, q2_passed=True,
            pbo=0.1, dsr=0.99, selection_winner=False)
        assert not_winner is False
        assert any("selection-fold winner" in line and "FAIL" in line
                   for line in winner_reasons)

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
        assert loaded["evolution_structural_lane"]["enabled"] is True
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
        assert "structural_lane:\n    enabled: true" in example
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


def _validated_count(path: Path) -> int:
    if not path.exists():
        return 0
    from src.evolution.pool import StrategyPool
    return StrategyPool(path).count("validated")


def _repo_pool_bytes() -> bytes | None:
    from src.evolution.pool import default_pool_path
    path = default_pool_path()
    return path.read_bytes() if path.exists() else None


def test_propose_saturday_appends_one_history_line_and_skips_the_pool(
        tmp_path, monkeypatch):
    """Propose mode uses the D2 append and does not promote the pool.

    The pool is real. Replacing ``if _promote_pool`` with
    ``if eligible_for_validated_pool(verdict)`` inserts a validated row.
    """
    from tests.test_evolution_history import (
        _validation_app, _patch_validation, load_evolution_history,
        evolution_history_path)
    from tests.test_evolution_min_design_trades import _InlineThread

    rb._evolution_guard_clear()
    ledger = tmp_path / "lane" / "structural_lane.json"
    pool_path = tmp_path / "pool.db"
    repo_before = _repo_pool_bytes()
    monkeypatch.setattr(
        "src.evolution.pool.default_pool_path", lambda: pool_path)
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
    app._settings["evolution_saturday_mode"] = "propose"
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
    assert _validated_count(pool_path) == 0
    assert _repo_pool_bytes() == repo_before
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


def test_docstrings_do_not_say_the_default_is_promote():
    norm = inspect.getsource(normalize_saturday_mode)
    defaults = inspect.getsource(default_lane_config)
    assert "Anything else is promote" not in norm
    assert "Saturday path on promote" not in defaults
    assert "Lane on" in defaults
    assert "does not promote" in defaults


@pytest.mark.parametrize("flag", ["true", "false", "True", "yes", 1, 0, None])
def test_non_boolean_enabled_stays_off(flag):
    from src.evolution.structural_lane import lane_config_from_settings

    parsed = parse_lane_config({"enabled": flag})
    assert parsed["enabled"] is False
    assert any("not a boolean" in line for line in parsed["warnings"])
    stored = settings_from_evolution({"structural_lane": {"enabled": flag}})
    assert stored["evolution_structural_lane"]["enabled"] is False
    loaded = lane_config_from_settings({
        "evolution_structural_lane": {"enabled": flag},
    })
    assert loaded["enabled"] is False
    assert isinstance(loaded["enabled"], bool)


def test_loader_non_boolean_enabled_stays_off(tmp_path, monkeypatch):
    quoted = _cfg(
        tmp_path, monkeypatch,
        'evolution:\n  structural_lane:\n    enabled: "true"\n')
    assert quoted["evolution_structural_lane"]["enabled"] is False
    assert any(
        "not a boolean" in line
        for line in quoted["evolution_structural_lane"]["warnings"])
    off = _cfg(
        tmp_path, monkeypatch,
        "evolution:\n  structural_lane:\n    enabled: false\n")
    assert off["evolution_structural_lane"]["enabled"] is False


def test_proposal_files_are_timestamped_and_never_overwrite(tmp_path):
    moment = datetime(2026, 10, 24, 8, 0, 0)
    first = record_proposal(
        tmp_path / "lane", bot_id=_BREAKOUT, month="2026-10",
        lane="structural", status="proposed",
        evidence={"n": 1}, now=moment)
    raw = Path(first["path"]).read_bytes()
    second = record_proposal(
        tmp_path / "lane", bot_id=_BREAKOUT, month="2026-10",
        lane="structural", status="proposed",
        evidence={"n": 2}, now=moment)
    assert first["path"] != second["path"]
    assert Path(first["path"]).read_bytes() == raw
    assert "20261024T080000" in Path(first["path"]).name
    assert "2026-W43" in Path(first["path"]).name
    assert Path(second["path"]).name.startswith(Path(first["path"]).stem)
    later = record_proposal(
        tmp_path / "lane", bot_id=_BREAKOUT, month="2026-10",
        lane="structural", status="proposed",
        evidence={"n": 3}, now=datetime(2026, 10, 31, 8, 0, 0))
    assert later["path"] != first["path"]
    assert "2026-W44" in Path(later["path"]).name
    assert Path(first["path"]).read_bytes() == raw
    assert Path(second["path"]).name.endswith("-2.json")
    assert json.loads(raw.decode("utf-8"))["evidence"]["n"] == 1
    assert len(list((tmp_path / "lane" / "proposals").glob("*.json"))) == 3


def test_applied_at_never_survives_a_lane_write(tmp_path):
    ledger = tmp_path / "lane" / "structural_lane.json"
    cfg = default_lane_config()
    assert cfg["enabled"] is True
    reserve_structural_slot(
        ledger, bot_id=_BREAKOUT, month="2026-10", config=cfg, now=_NOW)
    stored = load_ledger(ledger)
    stored["slots"][0]["applied_at"] = "2026-01-01T00:00:00"
    stored["slots"][0]["applied"] = True
    save_ledger(ledger, stored)
    planted = make_incubation(
        bot_id=_BREAKOUT, month="2026-10", form="side_split",
        status="not_admitted", now=_NOW)
    assert "applied_at" not in planted
    planted["applied_at"] = "2026-02-02T00:00:00"
    planted["applied"] = True
    body = record_proposal(
        tmp_path / "lane",
        bot_id=_BREAKOUT,
        month="2026-10",
        lane="structural",
        status="proposed",
        evidence={
            "applied": True,
            "applied_at": "2026-03-03T00:00:00",
            "reasons": ["q"],
        },
        form="side_split",
        ledger_path=ledger,
        touch_slot=True,
        incubation=planted,
        now=_NOW,
    )
    assert body["applied"] is False
    assert "applied_at" not in body
    assert "applied_at" not in body["evidence"]
    updated = load_ledger(ledger)
    assert updated["slots"][0]["applied"] is False
    assert "applied_at" not in updated["slots"][0]
    row = updated["incubations"][0]
    assert row["applied"] is False
    assert "applied_at" not in row
    again = load_ledger(ledger)
    again["incubations"][0]["applied_at"] = "2026-04-04T00:00:00"
    again["incubations"][0]["applied"] = True
    save_ledger(ledger, again)
    structural_saturday_followup(
        ledger,
        observations=[{
            "bot_id": _BREAKOUT,
            "month": "2026-10",
            "sessions": 3,
            "paper_trades": [],
        }],
        config=cfg,
    )
    final = load_ledger(ledger)
    assert final["incubations"][0]["applied"] is False
    assert "applied_at" not in final["incubations"][0]
    assert applied_graduation_stamps(final) == []


def test_sanitize_bot_id_blocks_path_traversal(tmp_path):
    from src.evolution.structural_lane import sanitize_bot_id

    assert sanitize_bot_id("") == "bot"
    assert sanitize_bot_id("   ") == "bot"
    assert sanitize_bot_id("***") == "bot"
    assert sanitize_bot_id("TXF/Plain bot") == "TXF_Plain_bot"
    assert sanitize_bot_id("ok.name-1") == "ok.name-1"
    assert len(sanitize_bot_id("b" * 90)) == 80
    root = (tmp_path / "lane" / "proposals").resolve()
    for raw in (
            "../../etc/passwd",
            r"..\..\windows\system32",
            "TXF/Plain bot",
            "a/b\\c",
    ):
        cleaned = sanitize_bot_id(raw)
        assert "/" not in cleaned and "\\" not in cleaned
        body = record_proposal(
            tmp_path / "lane",
            bot_id=raw,
            month="2026-10",
            lane="structural",
            status="refused",
            now=_NOW,
        )
        path = Path(body["path"]).resolve()
        assert path.parent == root
        assert ".." not in path.parts
        assert cleaned in path.name


def _no_proposals(directory: Path) -> None:
    proposals = directory / "proposals"
    if not proposals.exists():
        return
    assert list(proposals.glob("*.json")) == []


@pytest.mark.parametrize("flag", [False, "true"])
def test_saturday_off_switch_blocks_gate_and_launch(
        tmp_path, monkeypatch, flag):
    """Auto-Saturday with the lane off does not launch and writes no proposal.

    ``flag`` is boolean false or the non-boolean string ``true``.
    M17: the class-exclusion wrapper ignores ``enabled`` and calls divert.
    M18b: ``_lane_on = True`` calls the launch function.
    M18: the launch function's enabled return is removed, and an
    admission stub then starts the pipeline.
    A deleted ``if not structural_only`` return also reaches weekly codegen.
    """
    import src.evolution.structural_lane as lane_mod
    from tests.test_evolution_d1_knobs import _chat
    from tests.test_evolution_issue164 import (
        _CHANGE_PLAN, _DiscordLog, _InlineThread, _wiring_app)
    from tests.test_evolution_min_design_trades import (
        _app, _design_book, _run)
    from src.evolution.history import (
        evolution_history_path, load_evolution_history)

    rb._evolution_guard_clear()
    monkeypatch.setattr(rb, "_discord", _DiscordLog())
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    seen = []
    real_divert = lane_mod.ineligible_structural_divert

    def _divert(*args, **kwargs):
        seen.append(1)
        return real_divert(*args, **kwargs)

    monkeypatch.setattr(lane_mod, "ineligible_structural_divert", _divert)
    home = tmp_path / f"off-{flag}"
    chat = _chat()
    chat.send_message.return_value = _CHANGE_PLAN
    app = _wiring_app(home, chat)
    app._settings["evolution_knob_ineligible_fallback"] = "structural_lane"
    app._settings["evolution_min_design_trades"] = 0
    app._settings["evolution_structural_lane"] = {
        "enabled": flag,
        "ledger_path": str(home / "lane.json"),
    }
    rb.BacktestApp._bot_evolution(app, auto_run=True)
    assert seen == []
    assert chat.send_message.call_count == 0
    assert chat.one_shot.call_count == 0
    _no_proposals(home)
    rows = load_evolution_history(evolution_history_path(home))
    assert len(rows) == 1
    assert rows[0]["reason_kind"] == "knob_ineligible"
    rb._evolution_guard_clear()

    launches = []
    real_launch = rb.BacktestApp._maybe_launch_structural

    def _wrap(self, *args, **kwargs):
        launches.append(1)
        return real_launch(self, *args, **kwargs)

    monkeypatch.setattr(rb.BacktestApp, "_maybe_launch_structural", _wrap)
    plain_home = tmp_path / f"plain-{flag}"
    plain_chat = _chat()
    plain = _app(
        plain_home, _design_book(1), plain_chat,
        {
            "evolution_min_design_trades": 0,
            "evolution_structural_lane": {
                "enabled": flag,
                "ledger_path": str(plain_home / "lane.json"),
            },
        })
    _run(monkeypatch, plain)
    assert launches == []
    assert plain_chat.send_message.call_count == 1
    _no_proposals(plain_home)
    monkeypatch.setattr(rb.BacktestApp, "_maybe_launch_structural", real_launch)
    rb._evolution_guard_clear()

    admitted = []

    def _admit(*_args, **_kwargs):
        admitted.append(1)
        return Admission(True, "admitted")

    monkeypatch.setattr(lane_mod, "admission_decision", _admit)
    launch_home = tmp_path / f"launch-{flag}"
    launch_app = _wiring_app(launch_home, _chat())
    launch_app._settings["evolution_structural_lane"] = {
        "enabled": flag,
        "ledger_path": str(launch_home / "lane.json"),
    }
    started = []
    launch_app._start_evolution_pipeline = (
        lambda *args, **kwargs: started.append(1))
    assert rb.BacktestApp._maybe_launch_structural(
        launch_app, "preamble", ["trade"], "", launch_app._result,
        True, 14, launch_home, 1, False) is False
    assert admitted == []
    assert started == []
    _no_proposals(launch_home)
    rb._evolution_guard_clear()


def test_refused_structural_only_does_not_fall_into_weekly(
        tmp_path, monkeypatch):
    """A refused ineligible class must not start the weekly plan call.

    Deleting ``elif structural_only: pass`` falls into weekly codegen.
    """
    import src.evolution.structural_lane as lane_mod
    from tests.test_evolution_d1_knobs import _chat
    from tests.test_evolution_issue164 import (
        _CHANGE_PLAN, _DiscordLog, _InlineThread, _wiring_app)

    rb._evolution_guard_clear()
    monkeypatch.setattr(rb, "_discord", _DiscordLog())
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    calls = {"n": 0}

    def _gate(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return Admission(True, "admitted")
        return Admission(False, "refused for the test")

    monkeypatch.setattr(lane_mod, "admission_decision", _gate)
    chat = _chat()
    chat.send_message.return_value = _CHANGE_PLAN
    app = _wiring_app(tmp_path, chat)
    app._settings["evolution_knob_ineligible_fallback"] = "structural_lane"
    app._settings["evolution_min_design_trades"] = 0
    app._settings["evolution_structural_lane"] = {
        "enabled": True,
        "ledger_path": str(tmp_path / "lane.json"),
    }
    rb.BacktestApp._bot_evolution(app, auto_run=True)
    assert chat.send_message.call_count == 0
    assert chat.one_shot.call_count == 0
    _no_proposals(tmp_path)
    from src.evolution.history import (
        evolution_history_path, load_evolution_history)
    rows = load_evolution_history(evolution_history_path(tmp_path))
    assert len(rows) == 1
    assert rows[0]["final_status"] == "refused"
    assert rows[0]["reason_kind"] == "refused"
    rb._evolution_guard_clear()


def test_default_lane_runs_propose_and_adds_no_pool_row(
        tmp_path, monkeypatch):
    """A missing structural_lane key runs the lane and writes no pool row.

    Replacing ``if _promote_pool`` with ``if eligible_for_validated_pool``
    inserts a validated row. The knob grid must not run inside the lane.
    """
    import types
    from unittest.mock import MagicMock
    import src.evolution.knobs as knobs
    from tests.test_evolution_history import (
        _HIST_CODE, _patch_validation, _validation_app,
        evolution_history_path, load_evolution_history)
    from tests.test_evolution_issue164 import _DiscordLog, _InlineThread
    from tests.test_evolution_min_design_trades import _design_book

    rb._evolution_guard_clear()
    pool_path = tmp_path / "pool.db"
    repo_before = _repo_pool_bytes()
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    monkeypatch.setattr(rb, "_discord", _DiscordLog())
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    monkeypatch.setattr(
        "src.evolution.pool.default_pool_path", lambda: pool_path)
    _patch_validation(monkeypatch, passed=True)
    monkeypatch.setattr(
        "src.evolution.pipeline.eligible_for_validated_pool",
        lambda verdict: True)
    knob_calls = []

    def _knob(*args, **kwargs):
        knob_calls.append(args)
        raise AssertionError("knob grid ran inside the structural lane")

    def _rewrite(*_args, **_kwargs):
        raise AssertionError("knob rewrite ran inside the structural lane")

    monkeypatch.setattr(knobs, "saturday_knob_decision", _knob)
    monkeypatch.setattr(knobs, "rewrite_knob_source", _rewrite)
    plan = (
        "Performance is flat.\n"
        "```json\n"
        '{"action": "change", "knobs": {"ema_period": 60}}\n'
        "```\n"
        "structural_form: side_split\n"
    )
    chat = MagicMock()
    chat.send_message.return_value = plan
    chat.one_shot.return_value = _HIST_CODE
    chat.max_tokens = 1024
    chat.conversation = []
    app = _validation_app(tmp_path / "bot", chat, paper_gate="relative")
    trades = _design_book(1)
    app._result.trades = trades
    app._settings["evolution_min_design_trades"] = 0
    assert "evolution_structural_lane" not in app._settings
    assert "evolution_saturday_mode" not in app._settings
    app._gather_analysis_payload = lambda: (
        app._result, "report", "timeframe", "",
        [f"trade {i}" for i in range(len(trades))])
    app._ensure_chat_client = lambda: True
    app._build_evolution_context = types.MethodType(
        rb.BacktestApp._build_evolution_context, app)
    app._start_evolution_pipeline = types.MethodType(
        rb.BacktestApp._start_evolution_pipeline, app)
    rb.BacktestApp._bot_evolution(app, auto_run=True)
    assert app.errors == [], app.chat
    assert knob_calls == []
    sites = [
        call.kwargs.get("call_site")
        for call in chat.one_shot.call_args_list
    ]
    assert sites == ["evolution_codegen_structural"]
    assert _validated_count(pool_path) == 0
    assert _repo_pool_bytes() == repo_before
    proposals = list((tmp_path / "data" / "evolution" / "proposals").glob("*.json"))
    assert len(proposals) == 1
    body = json.loads(proposals[0].read_text(encoding="utf-8"))
    assert body["lane"] == "structural"
    assert body["applied"] is False
    assert body["pool_promoted"] is False
    assert "applied_at" not in body
    assert "class HistBaseEvo1" in body["source_code"]
    assert "def on_bar" in body["source_code"]
    assert body["source_code"] != body["evidence"].get("candidate")
    rows = load_evolution_history(evolution_history_path(tmp_path / "bot"))
    assert len(rows) == 1, rows
    assert rows[0]["final_status"] == "pass"
    assert rows[0]["reason"] == "proposal recorded; nothing applied"
    assert rb._evolution_in_progress == set()


def test_proposal_stores_candidate_source_not_just_the_class_name(tmp_path):
    source = "class SideSplit:\n    def on_bar(self):\n        return None\n"
    body = record_proposal(
        tmp_path / "lane",
        bot_id=_BREAKOUT,
        month="2026-10",
        lane="structural",
        status="proposed",
        form="side_split",
        evidence={"candidate": "SideSplit", "dsr_trials": 1},
        source_code=source,
        now=_NOW,
    )
    saved = json.loads(Path(body["path"]).read_text(encoding="utf-8"))
    assert saved["source_code"] == source
    assert saved["evidence"]["candidate"] == "SideSplit"
    assert saved["source_code"] != saved["evidence"]["candidate"]
    assert "def on_bar" in saved["source_code"]
    assert "source_code" not in saved["evidence"]


def test_concurrent_reserve_from_two_bots_loses_no_entry(tmp_path, monkeypatch):
    """Two bots reserving together both keep a slot. There is no file lock."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    import src.evolution.structural_lane as lane_mod

    monkeypatch.setattr(
        lane_mod, "_ledger_write_pause", lambda: time.sleep(0.05))
    ledger = tmp_path / "structural_lane.json"
    cfg = default_lane_config()
    src = Path("src/evolution/structural_lane.py").read_text(encoding="utf-8")
    assert "fcntl" not in src
    assert "portalocker" not in src
    assert "FileLock" not in src
    assert "_LEDGER_LOCK" in src

    def _reserve(bot_id):
        return reserve_structural_slot(
            ledger, bot_id=bot_id, month="2026-10", config=cfg, now=_NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(_reserve, ("bot-a", "bot-b"))
    assert first.admit and second.admit
    slots = load_ledger(ledger)["slots"]
    assert {row["bot_id"] for row in slots} == {"bot-a", "bot-b"}
    assert len(slots) == 2
    assert all(row["applied"] is False for row in slots)


def test_reserve_exception_refuses_without_the_grid_and_releases_the_guard(
        tmp_path, monkeypatch):
    """A reserve failure logs, writes one history line, and does not sweep the grid.

    Returning False from the except would start the knob grid for an
    eligible class. The run guard must be clear afterwards.
    """
    import inspect
    import types
    from tests.test_evolution_d1_knobs import _chat
    from tests.test_evolution_issue164 import (
        DynamicExitPullbackV3, _DiscordLog, _InlineThread, _wiring_app)
    import src.evolution.structural_lane as lane_mod

    rb._evolution_guard_clear()
    launch = inspect.getsource(rb.BacktestApp._maybe_launch_structural)
    assert "reserve failed" in launch
    assert 'return "refused"' in launch
    assert ".lock" not in launch
    monkeypatch.setattr(rb, "_discord", _DiscordLog())
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    grid_calls = []

    def _grid(*_args, **_kwargs):
        grid_calls.append(1)
        raise AssertionError("knob grid ran after a reserve failure")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("ledger blew up")

    # The worker calls run_grid_isolated. Patching run_grid_on_bars
    # misses that call: the child rebuilds the class before the grid.
    monkeypatch.setattr(
        "src.evolution.grid_process.run_grid_isolated", _grid)
    monkeypatch.setattr(lane_mod, "reserve_structural_slot", _boom)
    chat = _chat()
    app = _wiring_app(tmp_path, chat)
    app._live_runner.strategy = DynamicExitPullbackV3()
    app.strategy_var = types.SimpleNamespace(get=lambda: "DynamicExitPullbackV3")
    app._resolve_strategy_source = (
        lambda name, cls: inspect.getsource(DynamicExitPullbackV3))
    app._settings["evolution_min_design_trades"] = 0
    app._settings["evolution_structural_lane"] = {
        "enabled": True,
        "ledger_path": str(tmp_path / "lane.json"),
    }
    rb.BacktestApp._bot_evolution(app, auto_run=True)
    assert grid_calls == []
    assert chat.send_message.call_count == 0
    assert chat.one_shot.call_count == 0
    assert rb._evolution_in_progress == set()
    from src.evolution.history import (
        evolution_history_path, load_evolution_history)
    rows = load_evolution_history(evolution_history_path(tmp_path))
    assert len(rows) == 1, rows
    assert rows[0]["final_status"] == "refused"
    assert rows[0]["reason_kind"] == "refused"
    assert "reserve failed" in rows[0]["reason"]
    assert "RuntimeError" in rows[0]["reason"]
    assert any("reserve failed" in text for _role, text in app.chat)
    rb._evolution_guard_clear()


def test_structural_saturday_replaces_the_grid_other_saturdays_run_it(
        tmp_path, monkeypatch):
    """The free monthly slot replaces that Saturday's grid. The next one runs it."""
    import inspect
    import types
    from tests.test_evolution_d1_knobs import _chat
    from tests.test_evolution_issue164 import (
        DynamicExitPullbackV3, _DiscordLog, _InlineThread, _wiring_app)

    rb._evolution_guard_clear()
    monkeypatch.setattr(rb, "_discord", _DiscordLog())
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    grid_calls = []

    def _grid(*_args, **_kwargs):
        grid_calls.append(1)
        return types.SimpleNamespace(
            selected=None, reasons=["no plateau"], rationale="", evidence=None)

    # Patch the isolation entry the worker actually calls. A parent
    # patch of run_grid_on_bars does not apply in the spawn child, and
    # `_grid_in_process` still fails here: the child rebuilds
    # DynamicExitPullbackV3 from inspect.getsource and reports
    # "class not found" before the patched grid function runs.
    monkeypatch.setattr(
        "src.evolution.grid_process.run_grid_isolated", _grid)
    chat = _chat()
    chat.send_message.return_value = (
        "```json\n{\"action\": \"no_change\"}\n```\n")
    app = _wiring_app(tmp_path, chat)
    app._live_runner.strategy = DynamicExitPullbackV3()
    app.strategy_var = types.SimpleNamespace(get=lambda: "DynamicExitPullbackV3")
    app._resolve_strategy_source = (
        lambda name, cls: inspect.getsource(DynamicExitPullbackV3))
    app._settings["evolution_min_design_trades"] = 0
    app._settings["evolution_saturday_mode"] = "propose"
    app._settings["evolution_structural_lane"] = {
        "enabled": True,
        "ledger_path": str(tmp_path / "lane.json"),
    }
    rb.BacktestApp._bot_evolution(app, auto_run=True)
    assert grid_calls == []
    assert chat.send_message.call_count == 1
    from src.evolution.structural_lane import load_ledger
    assert len(load_ledger(tmp_path / "lane.json")["slots"]) == 1
    rb.BacktestApp._bot_evolution(app, auto_run=True)
    assert grid_calls == [1]
    assert chat.send_message.call_count == 1
    assert rb._evolution_in_progress == set()
    rb._evolution_guard_clear()


# Selection-fold deltas. The tenth value is the holdout and is excluded
# from DSR and PBO. Nine selection folds support CSCV S = 8.
_STRONG_DELTAS = [180, 220, 160, 240, 190, 210, 170, 230, 200, 200]
# Early folds look strong and later folds give it back. CSCV PBO is 1
# and DSR stays under 0.95, so the paper recommendation fails closed.
# PBO on that book is high and is not what decides it.
_OVERFIT_DELTAS = [1200, 1100, 1300, 1000, -900, -800, -1000, -700, -900, -800]


def _fold_trades(deltas):
    """Four trades per fold. Baseline sums to -300; the last trade carries Δ."""
    from dataclasses import dataclass

    @dataclass
    class _Trade:
        exit_dt: str
        pnl: int
        entry_dt: str = ""
        entry_price: int = 1
        exit_price: int = 1
        entry_bar_index: int = 0

    base, cand = [], []
    origin = datetime(2026, 1, 5, 9, 0)
    for i, delta in enumerate(deltas):
        start = origin + timedelta(days=14 * i)
        for j, (b, c) in enumerate(
                zip([100, 50, -200, -250], [100, 50, -200, -250 + delta])):
            stamp = (start + timedelta(hours=j)).strftime("%Y-%m-%d %H:%M")
            base.append(_Trade(
                stamp, b, entry_dt=stamp, entry_bar_index=i * 10 + j))
            cand.append(_Trade(
                stamp, c, entry_dt=stamp, exit_price=2,
                entry_bar_index=i * 10 + j))
    return base, cand


def _q2_bars():
    """200 native bars ending when the holdout fold starts, so selection is untrimmed."""
    from datetime import timedelta
    from src.market_data.models import Bar

    end = datetime(2026, 5, 12, 0, 0)
    return [
        Bar(
            symbol="TXF",
            dt=end - timedelta(minutes=199 - i),
            open=20000, high=20010, low=19990, close=20000,
            volume=1, interval=60,
        )
        for i in range(200)
    ]


def _structural_q2_calls():
    """Keyword ``trial`` values on every assemble_promotion_verdict call."""
    tree = ast.parse(Path("run_backtest.py").read_text(encoding="utf-8"))
    trials = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id != "assemble_promotion_verdict":
            continue
        for kw in node.keywords:
            if kw.arg == "trial":
                trials.append(kw.value)
    return trials


class TestStructuralQ2Evidence:
    def test_one_candidate_pbo_is_two_columns_and_fails_closed(self):
        """One scored candidate is baseline plus that candidate.

        Fewer than 8 selection folds, or a constant Δ (no Sharpe), leaves
        PBO or DSR missing. Missing is not a pass.
        """
        from src.evolution.grid_search import structural_selection_pnls
        from src.evolution.trial_penalty import structural_trial_evidence

        doc = structural_trial_evidence.__doc__ or ""
        assert "two columns" in doc
        assert "None is not a pass" in doc
        base, cand = _fold_trades(_STRONG_DELTAS)
        from src.evolution.pipeline import analyze_folds
        folds = analyze_folds(base, cand, fold_days=14, max_folds=10)
        b_pnls, c_pnls = structural_selection_pnls(
            base, cand, fold_days=14, holdout_start=folds[-1].start)
        assert len(b_pnls) == 9
        trial = structural_trial_evidence(
            baseline_folds=b_pnls, attempts=[c_pnls])
        assert trial.n_trials == 1
        assert trial.partitions == 8
        assert trial.pbo == 0.0
        assert trial.dsr is not None and trial.dsr > 0.90
        # Ten trade-folds in, nine selection folds out: the holdout is gone.
        assert [c - b for b, c in zip(b_pnls, c_pnls)] == [
            float(d) for d in _STRONG_DELTAS[:-1]]

        short_b, short_c = _fold_trades(_STRONG_DELTAS[:7])
        short_folds = analyze_folds(short_b, short_c, fold_days=14, max_folds=10)
        sb, sc = structural_selection_pnls(
            short_b, short_c, fold_days=14,
            holdout_start=short_folds[-1].start)
        assert len(sb) < 8
        short = structural_trial_evidence(baseline_folds=sb, attempts=[sc])
        assert short.pbo is None and short.partitions is None
        flat = structural_trial_evidence(
            baseline_folds=b_pnls, attempts=[[x + 200 for x in b_pnls]])
        assert flat.dsr is None
        ok, reasons = structural_paper_decision(
            sandbox_ok=True, native_bars=200, q2_passed=True,
            pbo=flat.pbo, dsr=flat.dsr)
        assert ok is False
        assert any("not supplied" in line for line in reasons)

    def test_failed_codegen_attempts_count_in_the_trial(self):
        """None columns have no series and still raise N and N_eff."""
        from src.evolution.grid_search import structural_selection_pnls
        from src.evolution.pipeline import analyze_folds
        from src.evolution.trial_penalty import structural_trial_evidence

        base, cand = _fold_trades(_STRONG_DELTAS)
        folds = analyze_folds(base, cand, fold_days=14, max_folds=10)
        b_pnls, c_pnls = structural_selection_pnls(
            base, cand, fold_days=14, holdout_start=folds[-1].start)
        counted = structural_trial_evidence(
            baseline_folds=b_pnls, attempts=[None, None, c_pnls])
        assert counted.n_trials == 3
        assert counted.n_eff == 3
        assert counted.rho_bar == 0.0
        assert counted.pbo == 0.0
        assert counted.partitions == 8
        src = Path("run_backtest.py").read_text(encoding="utf-8")
        assert "[None] * max(0, _structural_codegen_attempts - 1)" in src
        inc = src.index("_structural_codegen_attempts += 1")
        site = src.index('call_site="evolution_codegen_structural"')
        recorded = src.index('"dsr_trials": _structural_codegen_attempts')
        assert inc < site < recorded

    def test_reverting_q2_to_trial_none_fails(self):
        """The structural assembly passes the computed trial, not None.

        ``trial=None`` on a relative gate falls back to the absolute PF
        floor. This losing-but-improved book then fails. Replacing
        ``trial=_structural_trial`` with ``trial=None`` fails this test.
        """
        from src.evolution.grid_search import structural_selection_pnls
        from src.evolution.paper_gate import (
            NO_GRID_EVIDENCE_LOG,
            assemble_promotion_verdict,
            evolution_gate_config_from_mapping,
            resolve_promotion_gate,
        )
        from src.evolution.pipeline import EvolutionVerdict, analyze_folds
        from src.evolution.trial_penalty import structural_trial_evidence

        trials = _structural_q2_calls()
        assert len(trials) == 2
        assert any(
            isinstance(node, ast.Name) and node.id == "_structural_trial"
            for node in trials)
        assert any(
            isinstance(node, ast.Name) and node.id == "grid_trial"
            for node in trials)
        assert not any(isinstance(node, ast.Constant) and node.value is None
                       for node in trials)
        src = inspect.getsource(rb.BacktestApp._start_evolution_pipeline)
        assert "abs_pf_required(evo_gate)" in src
        assert "self._structural_trial_penalty = _structural_trial" in src
        assert 'PromotionGate("relative"' not in src

        base, cand = _fold_trades(_STRONG_DELTAS)
        folds = analyze_folds(base, cand, fold_days=14, max_folds=10)
        b_pnls, c_pnls = structural_selection_pnls(
            base, cand, fold_days=14, holdout_start=folds[-1].start)
        trial = structural_trial_evidence(
            baseline_folds=b_pnls, attempts=[None, c_pnls])
        assert trial.n_trials == 2
        cfg = evolution_gate_config_from_mapping({
            "paper_gate": "relative", "real_gate": "absolute"})
        gate = resolve_promotion_gate("paper", cfg.paper_gate, "absolute")
        window = EvolutionVerdict(True, ["window held"])
        common = dict(
            gate=gate, gate_cfg=cfg, folds=folds, base_trades=base,
            cand_trades=cand, holdout_start=folds[-1].start, criteria=None,
            window_verdict=window)
        passed = assemble_promotion_verdict(**common, trial=trial)
        assert passed.verdict.passed and not passed.verdict.provisional
        assert any("dropped on the paper relative path" in r
                   for r in passed.verdict.reasons)
        fallen = assemble_promotion_verdict(**common, trial=None)
        assert not fallen.verdict.passed
        assert any(NO_GRID_EVIDENCE_LOG in r for r in fallen.verdict.reasons)
        real = resolve_promotion_gate("auto", cfg.paper_gate, "absolute")
        assert real.mode == "absolute"
        absolute = assemble_promotion_verdict(
            **{**common, "gate": real}, trial=trial)
        assert not absolute.verdict.passed
        assert any("holdout fold PF" in r and "✗" in r
                   for r in absolute.verdict.reasons)
        assert not any("dropped on the paper relative path" in r
                       for r in absolute.verdict.reasons)

    def test_framework_doc_names_the_lock_and_the_relative_q2(self):
        text = Path("docs/evolution_framework.md").read_text(encoding="utf-8")
        assert "do not add a lock" not in text
        assert "threading.RLock" in text
        assert "One GUI per tree is the supported model." in text
        assert "no cross-process lock" in text
        assert "One scored candidate is two columns" in text
        assert "fails closed" in text
        example = Path("settings.example.yaml").read_text(encoding="utf-8")
        assert "One GUI per tree is the supported" in example
        assert "no cross-process lock" in example
        assert "two-column CSCV" in text
        assert "0.95" in text
        assert "about half the time" in text

    def test_structural_dsr_min_is_stricter_than_the_grid(self):
        from src.evolution.paper_gate import DEFAULT_DSR_MIN as grid_dsr_min
        assert default_lane_config()["dsr_min"] == pytest.approx(0.95)
        assert grid_dsr_min == pytest.approx(0.90)
        src = inspect.getsource(rb.BacktestApp._start_evolution_pipeline)
        assert 'dsr_min=float(_lane_cfg["dsr_min"])' in src
        assert "pbo_required=False" in src

    def test_assumed_sharpe_dispersion_makes_n_trials_bite(self):
        """The same Δ passes at one attempt and fails when N is large.

        One scored column has sample Sharpe dispersion 0. Without the
        assumed-σ floor, SR0 stays 0 and a large attempt count does not
        move DSR.
        """
        from src.evolution.trial_penalty import (
            STRUCTURAL_ASSUMED_SHARPE_STD, structural_trial_evidence)

        assert STRUCTURAL_ASSUMED_SHARPE_STD == 1.0
        mild = [20.0, 60.0, 10.0, 80.0, 30.0, 70.0, 15.0, 55.0, 40.0]
        base = [0.0] * len(mild)
        cand = [b + d for b, d in zip(base, mild)]
        one = structural_trial_evidence(
            baseline_folds=base, attempts=[cand])
        many = structural_trial_evidence(
            baseline_folds=base, attempts=[None] * 9 + [cand])
        assert one.n_trials == 1
        assert many.n_trials == 10
        assert one.dsr is not None and one.dsr > 0.95
        assert many.dsr is not None and many.dsr <= 0.95
        passed, _ = structural_paper_decision(
            sandbox_ok=True, native_bars=200, q2_passed=True,
            pbo=one.pbo, dsr=one.dsr, selection_winner=True)
        failed, reasons = structural_paper_decision(
            sandbox_ok=True, native_bars=200, q2_passed=True,
            pbo=many.pbo, dsr=many.dsr, selection_winner=True)
        assert passed is True
        assert failed is False
        assert any(line.startswith("DSR ") and "FAIL" in line
                   for line in reasons)

    def test_holdout_cutoff_before_the_last_fold_drops_the_tail(self):
        """Walk-forward cutoff earlier than the last fold start is excluded.

        Ignoring ``holdout_start`` (HOLDOUT_START_IGNORED) leaves the
        tail inside the selection fold and this comparison fails.
        """
        from src.evolution.grid_search import structural_selection_pnls
        from src.evolution.pipeline import analyze_folds

        base, cand = _fold_trades(_STRONG_DELTAS)
        folds = analyze_folds(base, cand, fold_days=14, max_folds=10)
        last_start = folds[-1].start
        early = "2026-04-27 11:00"
        assert early < last_start
        full_b, full_c = structural_selection_pnls(
            base, cand, fold_days=14, holdout_start=last_start)
        cut_b, cut_c = structural_selection_pnls(
            base, cand, fold_days=14, holdout_start=early)
        assert full_c[-1] == pytest.approx(-100.0)
        assert cut_c[-1] == pytest.approx(150.0)
        assert cut_b[-1] == pytest.approx(150.0)
        assert (full_b, full_c) != (cut_b, cut_c)

    def test_grid_still_requires_pbo_and_structural_does_not(self):
        from src.evolution.paper_gate import (
            RelativeGateConfig,
            assemble_promotion_verdict,
            decide_relative_paper_checks,
            evolution_gate_config_from_mapping,
            resolve_promotion_gate,
        )
        from src.evolution.pipeline import EvolutionVerdict, analyze_folds
        from src.evolution.trial_penalty import TrialEvidence

        base, cand = _fold_trades(_STRONG_DELTAS)
        folds = analyze_folds(base, cand, fold_days=14, max_folds=10)
        cfg = evolution_gate_config_from_mapping({
            "paper_gate": "relative", "real_gate": "absolute"})
        gate = resolve_promotion_gate("paper", cfg.paper_gate, "absolute")
        high_pbo = TrialEvidence(
            dsr=0.99, pbo=0.99, n_trials=1, n_eff=1.0,
            rho_bar=0.0, partitions=8)
        window = EvolutionVerdict(True, ["window held"])
        common = dict(
            gate=gate, gate_cfg=cfg, folds=folds, base_trades=base,
            cand_trades=cand, holdout_start=folds[-1].start, criteria=None,
            window_verdict=window, trial=high_pbo)
        gated = assemble_promotion_verdict(**common)
        assert not gated.verdict.passed
        assert any("PBO 0.9900" in r and "✗" in r for r in gated.verdict.reasons)
        open_pbo = assemble_promotion_verdict(**common, pbo_required=False)
        assert open_pbo.verdict.passed and not open_pbo.verdict.provisional
        assert any("informational only" in r for r in open_pbo.verdict.reasons)
        # Default argument stays the grid's gate. Flipping it to false
        # lets this high PBO through.
        direct = decide_relative_paper_checks(
            folds, [t.pnl for t in base], RelativeGateConfig(
                bootstrap_resamples=50), high_pbo)
        assert not direct.passed


def _run_structural_q2(tmp_path, monkeypatch, *, deltas, trading_mode,
                       codegen_ok=True):
    """Drive the structural worker with fixture trades. No live pool write."""
    import types
    from unittest.mock import MagicMock
    from src.backtest.metrics import PerformanceMetrics
    from src.evolution.pipeline import ABResult, analyze_folds
    import src.evolution.pipeline as pipeline
    import src.daily_report.changelog as changelog
    from tests.test_evolution_history import _HIST_CODE, _validation_app
    from tests.test_evolution_issue164 import _InlineThread

    rb._evolution_guard_clear()
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    monkeypatch.setattr(rb, "_discord", None)
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    monkeypatch.setattr(changelog, "append_changelog", lambda **kwargs: None)
    monkeypatch.setattr(changelog, "load_changelog", lambda: [])
    pool_path = tmp_path / "pool.db"
    monkeypatch.setattr(
        "src.evolution.pool.default_pool_path", lambda: pool_path)

    base, cand = _fold_trades(deltas)

    def _metrics(pf, dd, pnl):
        return PerformanceMetrics(
            total_trades=40, profit_factor=pf, max_drawdown=dd,
            max_drawdown_pct=5.0, total_pnl=pnl, win_rate=0.4)

    def _side(name, trades, pf, dd, pnl):
        return ABResult(
            name=name,
            result=types.SimpleNamespace(
                trades=list(trades), metrics=_metrics(pf, dd, pnl)))

    def _ab(*_args, **kwargs):
        name = kwargs.get("name") or (_args[3] if len(_args) > 3 else "")
        if "候選" in str(name):
            return _side(name, cand, 0.6, 4000, -1000)
        return _side(name, base, 0.4, 5000, -3000)

    def _mf(*_args, **_kwargs):
        folds = analyze_folds(base, cand, fold_days=14, max_folds=10)
        return (
            folds,
            _side("base", base, 0.4, 5000, -3000),
            _side("cand", cand, 0.6, 4000, -1000),
        )

    monkeypatch.setattr(pipeline, "run_ab_backtest", _ab)
    monkeypatch.setattr(pipeline, "run_multifold_validation", _mf)
    chat = MagicMock()
    chat.send_message.return_value = "structural_form: side_split\n"
    chat.one_shot.return_value = _HIST_CODE if codegen_ok else "no code block"
    chat.max_tokens = 1024
    chat.conversation = []
    bot = tmp_path / "bot"
    app = _validation_app(bot, chat, paper_gate="relative")
    app._live_runner.trading_mode = trading_mode
    app._trading_mode = trading_mode
    app._live_runner.get_bars = lambda: _q2_bars()
    ledger = tmp_path / "lane" / "structural_lane.json"
    rb.BacktestApp._start_evolution_pipeline(
        app, "preamble", ["design trade"], "", app._result,
        auto_run=True, holdout_days=14, bot_dir=str(bot),
        design_cut_idx=1, structural_lane=True,
        structural_ledger=str(ledger), structural_bot_id="HistBase",
        structural_month="2026-10")
    proposals = list((tmp_path / "lane" / "proposals").glob("*.json"))
    return app, proposals, pool_path, ledger


def test_lane_recommends_paper_for_a_strong_candidate(tmp_path, monkeypatch):
    """A strong losing-but-improved candidate can be recommended for paper.

    The recommendation is text. The pool stays empty. Replacing the Q2
    trial with None drops this book onto the absolute PF floor, so
    paper_recommended becomes false and this test fails.
    """
    from src.evolution.structural_lane import load_ledger

    app, proposals, pool_path, ledger = _run_structural_q2(
        tmp_path, monkeypatch, deltas=_STRONG_DELTAS, trading_mode="paper")
    assert app.errors == [], app.errors
    assert len(proposals) == 1
    body = json.loads(proposals[0].read_text(encoding="utf-8"))
    evidence = body["evidence"]
    assert evidence["paper_recommended"] is True
    assert evidence["q2_passed"] is True
    assert any("dropped on the paper relative path" in line
               for line in evidence["q2_reasons"])
    assert not any("no grid trial evidence" in line
                   for line in evidence["q2_reasons"])
    assert any("DSR" in line and "pass" in line
               for line in evidence["paper_reasons"])
    assert any("PBO" in line and "informational" in line
               for line in evidence["paper_reasons"])
    assert evidence["selection_winner"] is True
    assert evidence["dsr"] > 0.95
    assert evidence["pbo"] == 0.0
    assert any("recommendation only" in line
               for line in evidence["paper_reasons"])
    assert body["applied"] is False
    assert body["pool_promoted"] is False
    assert body["dsr_trials"] == 1
    assert "applied_at" not in body
    row = load_ledger(ledger)["incubations"][-1]
    assert row["status"] == "paper_recommended"
    assert row["applied"] is False
    assert "applied_at" not in row
    assert _validated_count(pool_path) == 0


def test_lane_fails_closed_on_an_overfit_candidate(tmp_path, monkeypatch):
    app, proposals, pool_path, ledger = _run_structural_q2(
        tmp_path, monkeypatch, deltas=_OVERFIT_DELTAS, trading_mode="paper")
    assert app.errors == [], app.errors
    body = json.loads(proposals[0].read_text(encoding="utf-8"))
    evidence = body["evidence"]
    assert evidence["paper_recommended"] is False
    assert any("informational" in line for line in evidence["paper_reasons"])
    assert not any(line.startswith("PBO ") and "FAIL" in line
                   for line in evidence["paper_reasons"])
    assert any(line.startswith("DSR ") and "FAIL" in line
               for line in evidence["paper_reasons"])
    from src.evolution.structural_lane import load_ledger
    assert load_ledger(ledger)["incubations"][-1]["status"] == "not_admitted"
    assert _validated_count(pool_path) == 0


def test_real_money_structural_q2_stays_absolute(tmp_path, monkeypatch):
    app, proposals, _pool, _ledger = _run_structural_q2(
        tmp_path, monkeypatch, deltas=_STRONG_DELTAS, trading_mode="auto")
    assert app.errors == [], app.errors
    evidence = json.loads(proposals[0].read_text(encoding="utf-8"))["evidence"]
    assert evidence["paper_recommended"] is False
    assert evidence["q2_passed"] is False
    assert any("holdout fold PF" in line and "✗" in line
               for line in evidence["q2_reasons"])
    assert not any("dropped on the paper relative path" in line
                   for line in evidence["q2_reasons"])


def test_no_edge_candidate_is_not_recommended(tmp_path, monkeypatch):
    """A selection-fold tie with the baseline is not a paper recommendation.

    Two-column PBO can clear a no-edge book. The lane does not.
    """
    from src.evolution.structural_lane import load_ledger

    # Nine selection folds sum to 0. The tenth value is the holdout.
    deltas = [80, -80, 60, -60, 40, -40, 20, -20, 0, 0]
    app, proposals, pool_path, ledger = _run_structural_q2(
        tmp_path, monkeypatch, deltas=deltas, trading_mode="paper")
    assert app.errors == [], app.errors
    body = json.loads(proposals[0].read_text(encoding="utf-8"))
    evidence = body["evidence"]
    assert evidence["paper_recommended"] is False
    assert evidence["selection_winner"] is False
    assert any("selection-fold winner" in line and "FAIL" in line
               for line in evidence["paper_reasons"])
    assert any("informational" in line for line in evidence["paper_reasons"])
    assert body["pool_promoted"] is False
    assert body["applied"] is False
    assert load_ledger(ledger)["incubations"][-1]["status"] == "not_admitted"
    assert _validated_count(pool_path) == 0


def test_failed_codegen_attempt_is_one_trial(tmp_path, monkeypatch):
    app, proposals, pool_path, _ledger = _run_structural_q2(
        tmp_path, monkeypatch, deltas=_STRONG_DELTAS, trading_mode="paper",
        codegen_ok=False)
    assert app.errors == [], app.errors
    assert len(proposals) == 1
    body = json.loads(proposals[0].read_text(encoding="utf-8"))
    assert body["status"] == "refused"
    assert body["dsr_trials"] == 1
    assert body["evidence"]["dsr_trials"] == 1
    assert body["evidence"]["sandbox_ok"] is False
    assert body["pool_promoted"] is False
    assert body["applied"] is False
    assert _validated_count(pool_path) == 0
