"""D2: append-only evolution_history.jsonl for every gate decision.

A missing or corrupt file must not raise. A failed replace must leave
the previous bytes in place. The #188 retire streak is derived from
the history rows, not from the changelog.
"""

from __future__ import annotations

import inspect
import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import run_backtest as rb
from src.evolution.history import (
    HISTORY_FILENAME,
    append_evolution_history,
    build_evolution_record,
    evolution_history_path,
    load_evolution_history,
    record_evolution_attempt,
    retire_streak_from_history,
    validation_gates,
    weeks_from_history,
)
from src.evolution.paper_gate import (
    RetireConfig,
    assess_retirement,
    assemble_promotion_verdict,
    evolution_gate_config_from_mapping,
    resolve_promotion_gate,
)
from src.evolution.pipeline import EvolutionVerdict


class _Trade:
    def __init__(self, exit_dt: str, pnl: int = -10):
        self.exit_dt = exit_dt
        self.entry_dt = exit_dt
        self.pnl = pnl


def _eligible_trades(n: int = 40) -> list[_Trade]:
    return [_Trade(f"2026-10-20 10:{i:02d}") for i in range(n)]


def _now() -> datetime:
    return datetime(2026, 10, 24, 8, 0)


def _row(bot, ts, *, evaluated, passed, kind="validation"):
    return build_evolution_record(
        ts=ts,
        bot=bot,
        class_name="Probe",
        final_status="pass" if passed and evaluated else (
            "skip" if not evaluated else "fail"),
        reason_kind=kind,
        reason="skip week" if not evaluated else "",
        retire_evaluated=evaluated,
        retire_passed=passed,
        ai_calls=0 if not evaluated else 1,
    )


def _append_rows(path, rows):
    for row in rows:
        assert append_evolution_history(path, row) is True


# ── schema: FAIL and PASS keep every gate's reasons ──────────────────


def test_fail_run_records_deep_multifold_month_and_promotion(tmp_path):
    """A validation FAIL keeps each block's reasons and the #181 dict."""
    from tests.test_evolution_issue153 import _ema80_books, _load

    data = _load()
    base, cand = _ema80_books(data)
    window = EvolutionVerdict(False, ["deep gate: PF 0.40 ≥ 1: ✗"])
    cfg = evolution_gate_config_from_mapping({"paper_gate": "absolute"})
    gate = resolve_promotion_gate("paper", "absolute", "absolute")
    assembled = assemble_promotion_verdict(
        gate=gate, gate_cfg=cfg, folds=[],
        base_trades=base, cand_trades=cand,
        holdout_start=data["holdout_start"],
        criteria=None, window_verdict=window,
    )
    gates = validation_gates(
        assembled=assembled, folds=[], baseline_res=None, candidate_res=None,
        gate=gate, gate_cfg=cfg,
    )
    bot_dir = tmp_path / "bot"
    assert record_evolution_attempt(
        bot_dir,
        bot="Probe",
        class_name="DynamicExitPullback",
        source="class DynamicExitPullback: pass\n",
        cut_idx=12,
        watermark={"trade_count": 4, "at": "2026-10-01 05:00:00", "secret": "nope"},
        plan_excerpt="x" * 500,
        directives={"action": "change", "criteria": {"profit_factor_min": 1.2},
                    "knobs": {"ema_period": 60}, "api_key": "sk-secret"},
        final_status="fail",
        provisional=bool(assembled.verdict.provisional),
        ai_calls=2,
        reason="validation failed",
        reason_kind="validation",
        gates=gates,
        retire_evaluated=False,
        retire_passed=False,
        thresholds_raw={
            "paper_gate": "absolute",
            "discord_bot_token": "secret-token",
        },
    )

    path = Path(evolution_history_path(bot_dir))
    assert path.name == HISTORY_FILENAME
    assert path.parent.name == "bot"
    text = path.read_text(encoding="utf-8")
    assert "secret-token" not in text
    assert "sk-secret" not in text
    assert "api_key" not in text
    assert text.count("\n") == 1
    loaded = load_evolution_history(path)
    assert len(loaded) == 1
    row = loaded[0]
    assert row["final_status"] == "fail"
    assert row["bot"] == "Probe"
    assert row["class"] == "DynamicExitPullback"
    assert len(row["source_hash"]) == 64
    assert row["cut_idx"] == 12
    assert row["watermark"] == {"trade_count": 4, "at": "2026-10-01 05:00:00"}
    assert len(row["plan_excerpt"]) == 300
    assert row["directives"]["action"] == "change"
    assert row["directives"]["knobs"] == {"ema_period": 60}
    assert "api_key" not in row["directives"]
    assert row["ai_calls"] == 2
    assert row["params_before"] is None
    assert row["params_after"] is None
    deep_text = "\n".join(row["gates"]["deep"]["reasons"])
    multi_text = "\n".join(row["gates"]["multifold"]["reasons"])
    month = row["gates"]["month"]
    month_text = "\n".join(month["reasons"])
    promo = row["gates"]["promotion_gate"]
    assert "deep gate: PF 0.40 ≥ 1: ✗" in deep_text
    assert row["gates"]["deep"]["passed"] is False
    assert row["gates"]["deep"]["kind"] == "ab"
    assert "no folds" in multi_text
    assert row["gates"]["multifold"]["passed"] is False
    assert "prior-months collapse" in month_text
    assert "calendar-month dominated" in month_text
    assert month == json.loads(json.dumps(assembled.month_gates))
    assert month["latest"] == "2026-09"
    assert promo["mode"] == "absolute"
    assert promo["require_abs_pf"] is True
    assert promo["passed"] is False
    assert promo["relative_reasons"] is None
    assert "prior-months collapse" in "\n".join(promo["reasons"])
    assert "no folds" in "\n".join(promo["reasons"])
    assert "deep gate: PF 0.40 ≥ 1: ✗" in "\n".join(promo["reasons"])
    assert row["retire_week"]["evaluated"] is False
    assert row["retire_week"]["passed"] is False
    assert row["thresholds"]["paper_gate"] == "absolute"
    assert "discord_bot_token" not in json.dumps(row["thresholds"])


def test_pass_run_records_each_gate_reason(tmp_path):
    from src.evolution.paper_gate import PromotionAssembly

    month_obs = {
        "passed": True,
        "provisional": False,
        "reasons": ["month pass ✓"],
        "latest": "2026-10",
        "edge": 10,
    }
    assembled = PromotionAssembly(
        verdict=EvolutionVerdict(True, ["combined pass ✓"]),
        month_verdict=EvolutionVerdict(
            True, ["month pass ✓"], month_gates=month_obs),
        window_verdict=EvolutionVerdict(True, ["deep pass ✓"]),
        multifold_verdict=EvolutionVerdict(True, ["multifold pass ✓"]),
    )
    gate = resolve_promotion_gate("paper", "relative", "absolute")
    cfg = evolution_gate_config_from_mapping({"paper_gate": "relative"})
    gates = validation_gates(
        assembled=assembled, folds=[], baseline_res=None, candidate_res=None,
        gate=gate, gate_cfg=cfg,
    )
    assert record_evolution_attempt(
        tmp_path, bot="Probe", class_name="Probe",
        final_status="pass", reason_kind="validation",
        gates=gates, ai_calls=1,
        retire_evaluated=True, retire_passed=True,
        ts="2026-10-24T05:05:00",
        thresholds_raw={"paper_gate": "relative"},
    )
    row = load_evolution_history(evolution_history_path(tmp_path))[0]
    assert row["final_status"] == "pass"
    assert row["gates"]["deep"]["reasons"] == ["deep pass ✓"]
    assert row["gates"]["deep"]["passed"] is True
    assert row["gates"]["multifold"]["reasons"] == ["multifold pass ✓"]
    assert row["gates"]["month"]["reasons"] == ["month pass ✓"]
    assert row["gates"]["month"]["edge"] == 10
    promo = row["gates"]["promotion_gate"]
    assert promo["passed"] is True
    assert promo["mode"] == "relative"
    assert promo["require_abs_pf"] is False
    assert promo["reasons"] == ["combined pass ✓"]
    assert row["retire_week"] == {
        "at": "2026-10-24T05:05:00",
        "evaluated": True,
        "passed": True,
    }
    assert row["thresholds"]["paper_gate"] == "relative"
    assert row["thresholds"]["retire"]["consecutive_weeks"] == 4


def test_relative_fail_stores_relative_reasons(tmp_path):
    cfg = evolution_gate_config_from_mapping({"paper_gate": "relative"})
    gate = resolve_promotion_gate("paper", "relative", "absolute")
    window = EvolutionVerdict(False, ["window held ✗"])
    assembled = assemble_promotion_verdict(
        gate=gate, gate_cfg=cfg, folds=[],
        base_trades=[], cand_trades=[],
        holdout_start="", criteria=None, window_verdict=window,
    )
    assert assembled.relative_verdict is not None
    assert assembled.window_verdict is window
    assert assembled.multifold_verdict is not None
    gates = validation_gates(
        assembled=assembled, folds=[], baseline_res=None, candidate_res=None,
        gate=gate, gate_cfg=cfg,
    )
    record_evolution_attempt(
        tmp_path, bot="Probe", final_status="fail", reason_kind="validation",
        gates=gates, retire_evaluated=True, retire_passed=False,
        provisional=True, ts="2026-10-17T05:05:00",
    )
    row = load_evolution_history(evolution_history_path(tmp_path))[0]
    assert row["gates"]["promotion_gate"]["relative_reasons"]
    assert row["provisional"] is True
    assert row["retire_week"]["evaluated"] is True
    assert row["retire_week"]["passed"] is False


# ── file tolerance and atomic append ─────────────────────────────────


def test_missing_file_reads_empty_and_empty_bot_dir_does_not_write(tmp_path):
    assert load_evolution_history(tmp_path / "missing.jsonl") == []
    assert record_evolution_attempt(None, bot="Probe") is False
    assert record_evolution_attempt("", bot="Probe") is False
    assert not (tmp_path / HISTORY_FILENAME).exists()


def test_corrupt_lines_are_skipped_and_preserved(tmp_path):
    path = tmp_path / HISTORY_FILENAME
    path.write_text(
        '{"bot":"ok","final_status":"skip"}\n'
        "NOT JSON\n"
        '{"bot":"also"}\n'
        '{"bot":',
        encoding="utf-8",
    )
    assert [row["bot"] for row in load_evolution_history(path)] == ["ok", "also"]
    assert append_evolution_history(path, {"bot": "new", "final_status": "fail"})
    text = path.read_text(encoding="utf-8")
    assert "NOT JSON" in text
    assert '{"bot":' in text
    assert [row["bot"] for row in load_evolution_history(path)] == [
        "ok", "also", "new"]


def test_undecodable_file_is_left_untouched(tmp_path):
    path = tmp_path / HISTORY_FILENAME
    raw = b"\xff\xfe not utf8"
    path.write_bytes(raw)
    assert load_evolution_history(path) == []
    logs = []
    assert append_evolution_history(
        path, {"bot": "x", "final_status": "skip"}, log=logs.append) is False
    assert path.read_bytes() == raw
    assert logs and "evolution history append failed" in logs[0]
    assert list(tmp_path.glob("*.tmp")) == []


def test_replace_failure_keeps_previous_bytes_and_does_not_raise(tmp_path, monkeypatch):
    path = tmp_path / HISTORY_FILENAME
    assert append_evolution_history(path, {"bot": "first", "final_status": "skip"})
    before = path.read_bytes()
    logs = []

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    assert append_evolution_history(
        path, {"bot": "second", "final_status": "fail"}, log=logs.append) is False
    assert path.read_bytes() == before
    assert load_evolution_history(path) == [{"bot": "first", "final_status": "skip"}]
    assert logs and "OSError" in logs[0]
    assert list(tmp_path.glob("*.tmp")) == []


def test_successful_append_keeps_the_previous_line_as_a_prefix(tmp_path):
    path = tmp_path / "nested" / HISTORY_FILENAME
    assert append_evolution_history(path, {"bot": "first", "final_status": "skip"})
    first = path.read_bytes()
    assert first.endswith(b"\n")
    assert append_evolution_history(path, {"bot": "second", "final_status": "pass"})
    body = path.read_bytes()
    assert body.startswith(first)
    assert [row["bot"] for row in load_evolution_history(path)] == ["first", "second"]


def test_log_callback_failure_does_not_raise(tmp_path, monkeypatch):
    path = tmp_path / HISTORY_FILENAME

    def _boom(src, dst):
        raise OSError("nope")

    def _log(_msg):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(os, "replace", _boom)
    assert append_evolution_history(path, {"bot": "x"}, log=_log) is False


# ── retire streak from history ────────────────────────────────────────


def test_retire_streak_ignores_skips_and_matches_assess_retirement(tmp_path):
    path = Path(evolution_history_path(tmp_path))
    rows = [
        _row("Probe", "2026-09-05T05:05:00", evaluated=True, passed=False),
        _row("Probe", "2026-09-08T05:05:00", evaluated=False, passed=False,
             kind="watermark"),
        _row("Other", "2026-09-12T05:05:00", evaluated=True, passed=False),
        _row("Other", "2026-09-19T05:05:00", evaluated=True, passed=False),
        _row("Other", "2026-09-26T05:05:00", evaluated=True, passed=False),
        _row("Other", "2026-10-03T05:05:00", evaluated=True, passed=False),
        _row("Probe", "2026-09-12T05:05:00", evaluated=True, passed=False),
        _row("Probe", "2026-09-15T05:05:00", evaluated=False, passed=False,
             kind="min_design_trades"),
        _row("Probe", "2026-09-19T05:05:00", evaluated=True, passed=False),
        _row("Probe", "2026-09-26T05:05:00", evaluated=True, passed=False),
    ]
    # A corrupt line between real rows must not drop the streak.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this line is corrupt\n", encoding="utf-8")
    _append_rows(path, rows)
    loaded = load_evolution_history(path)
    streak = retire_streak_from_history(loaded, consecutive_weeks=4, bot="Probe")
    assert streak["streak"] is True
    assert streak["evaluated_count"] == 4
    assert streak["evaluated_failures_tail"] == [
        "2026-09-05T05:05:00",
        "2026-09-12T05:05:00",
        "2026-09-19T05:05:00",
        "2026-09-26T05:05:00",
    ]
    weeks = weeks_from_history(loaded, bot="Probe")
    assert all(week.evaluated and not week.passed for week in weeks)
    rec = assess_retirement(
        _eligible_trades(), weeks=weeks, now=_now(),
        config=RetireConfig())
    assert rec.evidence["condition_b"] is True
    assert rec.evidence["condition_a"] is False
    assert rec.recommend is True
    # Another bot's four failures are not this bot's streak.
    assert retire_streak_from_history(
        loaded, consecutive_weeks=4, bot="Nobody")["streak"] is False

    # A later PASS on the same calendar day replaces that day's FAIL.
    _append_rows(path, [
        _row("Probe", "2026-09-26T18:00:00", evaluated=True, passed=True),
    ])
    loaded = load_evolution_history(path)
    broken = retire_streak_from_history(loaded, consecutive_weeks=4, bot="Probe")
    assert broken["streak"] is False
    assert broken["evaluated_count"] == 4
    again = assess_retirement(
        _eligible_trades(),
        weeks=weeks_from_history(loaded, bot="Probe"),
        now=_now(),
    )
    assert again.evidence["condition_b"] is False

    # Unevaluated rows cannot force passed=True into the streak.
    forced = build_evolution_record(
        ts="2026-10-03T05:05:00", bot="Probe",
        retire_evaluated=False, retire_passed=True,
        final_status="skip", reason_kind="watermark")
    assert forced["retire_week"]["passed"] is False


def test_three_failures_do_not_streak_and_a_pass_breaks_it(tmp_path):
    path = evolution_history_path(tmp_path)
    _append_rows(path, [
        _row("Probe", f"2026-09-{day:02d}T05:05:00", evaluated=True, passed=False)
        for day in (5, 12, 19)
    ])
    loaded = load_evolution_history(path)
    assert retire_streak_from_history(
        loaded, consecutive_weeks=4, bot="Probe")["streak"] is False
    _append_rows(path, [
        _row("Probe", "2026-09-26T05:05:00", evaluated=True, passed=True),
    ])
    loaded = load_evolution_history(path)
    assert retire_streak_from_history(
        loaded, consecutive_weeks=4, bot="Probe")["streak"] is False
    assert assess_retirement(
        _eligible_trades(),
        weeks=weeks_from_history(loaded, bot="Probe"),
        now=_now(),
    ).evidence["condition_b"] is False


# ── pipeline wiring (tmp bot dir, no real data/) ──────────────────────


def test_min_design_skip_is_appended_without_changing_the_notice(
        tmp_path, monkeypatch):
    from tests.test_evolution_min_design_trades import (
        _app, _chat, _chat_text, _design_book, _run)

    chat = _chat()
    app = _app(tmp_path, _design_book(18), chat)
    discord = _run(monkeypatch, app)

    rows = load_evolution_history(evolution_history_path(tmp_path))
    assert len(rows) == 1
    row = rows[0]
    assert row["final_status"] == "skip"
    assert row["reason_kind"] == "min_design_trades"
    assert "waiting for sample: 18/30" in row["reason"]
    assert row["ai_calls"] == 0
    assert row["gates"]["deep"] is None
    assert row["gates"]["month"] is None
    assert row["gates"]["promotion_gate"] is None
    assert row["retire_week"]["evaluated"] is False
    assert row["plan_excerpt"] == ""
    assert any("waiting for sample: 18/30" in msg for msg in discord.msgs)
    assert _chat_text(app).count("waiting for sample: 18/30") == 1
    assert app.errors == []


def test_no_change_records_the_plan_and_one_ai_call(tmp_path, monkeypatch):
    from tests.test_evolution_min_design_trades import (
        _NO_CHANGE, _app, _chat, _design_book, _run)

    chat = _chat()
    chat.send_message.return_value = _NO_CHANGE
    app = _app(tmp_path, _design_book(30), chat)
    discord = _run(monkeypatch, app)

    assert chat.send_message.call_count == 1
    assert chat.one_shot.call_count == 0
    rows = load_evolution_history(evolution_history_path(tmp_path))
    assert len(rows) == 1
    row = rows[0]
    assert row["final_status"] == "no_change"
    assert row["reason_kind"] == "no_change"
    assert row["ai_calls"] == 1
    assert row["directives"]["action"] == "no_change"
    assert "no_change" in row["plan_excerpt"]
    assert row["retire_week"]["evaluated"] is False
    assert row["gates"]["promotion_gate"] is None
    assert any("no change" in msg.lower() or "不修改" in msg for msg in discord.msgs)
    assert app.errors == []


def test_watermark_skip_records_the_reason_and_keeps_the_discord_line(
        tmp_path, monkeypatch):
    from tests.test_evolution_min_design_trades import (
        _TPE, _app, _chat, _chat_text, _freeze_now, _run, _trade)
    from src.evolution.notify import load_watermark, save_watermark

    _freeze_now(monkeypatch, datetime(2026, 10, 24, 6, 0, tzinfo=_TPE))
    trades = [_trade("2026-10-01 10:00") for _ in range(4)]
    save_watermark(tmp_path, len(trades), at="2026-10-02 05:00:00")
    before = load_watermark(tmp_path)
    chat = _chat()
    app = _app(tmp_path, trades, chat)
    discord = _run(monkeypatch, app)

    line = "idle 23 days since last exit 2026-10-01"
    assert line in _chat_text(app)
    assert any(line in msg for msg in discord.msgs)
    assert load_watermark(tmp_path) == before
    rows = load_evolution_history(evolution_history_path(tmp_path))
    assert len(rows) == 1
    assert rows[0]["final_status"] == "skip"
    assert rows[0]["reason_kind"] == "watermark"
    assert line in rows[0]["reason"]
    assert "No new design-window trades" in rows[0]["reason"]
    assert rows[0]["ai_calls"] == 0
    assert rows[0]["watermark"]["trade_count"] == len(trades)
    assert rows[0]["retire_week"]["evaluated"] is False


def test_knob_exclusion_skip_records_the_exclusion_line(tmp_path, monkeypatch):
    from tests.test_evolution_issue164 import (
        _CHANGE_PLAN, _DiscordLog, _InlineThread, _STUB_LINE, _wiring_app)

    chat = MagicMock()
    chat.send_message.return_value = _CHANGE_PLAN
    chat.max_tokens = 1024
    chat.conversation = []
    discord = _DiscordLog()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    app = _wiring_app(tmp_path, chat)

    rb.BacktestApp._bot_evolution(app, auto_run=True)

    assert chat.send_message.call_count == 0
    assert discord.msgs == [_STUB_LINE]
    assert app.chat == [("system", _STUB_LINE)]
    rows = load_evolution_history(evolution_history_path(tmp_path))
    assert len(rows) == 1
    assert rows[0]["final_status"] == "skip"
    assert rows[0]["reason_kind"] == "knob_ineligible"
    assert rows[0]["reason"] == _STUB_LINE
    assert rows[0]["ai_calls"] == 0
    assert rows[0]["retire_week"]["evaluated"] is False


def test_worker_writes_history_after_the_discord_verdict():
    src = inspect.getsource(rb.BacktestApp._start_evolution_pipeline)
    assert 'assembled=assembled' in src
    assert src.index("evolution_verdict(verdict.passed, block)") < src.index(
        "assembled=assembled")
    assert src.index("rec = assess_retirement") < src.index(
        "evolution_verdict(verdict.passed, block)")
    assert src.index("block = format_weekend_verdict_block") < src.index(
        "evolution_verdict(verdict.passed, block)")
    evo = inspect.getsource(rb.BacktestApp._bot_evolution)
    for kind in ("knob_ineligible", "watermark", "min_design_trades",
                 "no_design_window", "plan_only"):
        assert f'reason_kind="{kind}"' in evo or f"reason_kind=\"{kind}\"" in evo
        assert kind in evo
