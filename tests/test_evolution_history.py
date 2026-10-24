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
    PromotionAssembly,
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
    bot_dir.mkdir()
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


def test_bad_bytes_are_skipped_and_a_later_line_is_appended(tmp_path):
    path = tmp_path / HISTORY_FILENAME
    path.write_bytes(b"\xff\xfe not utf8\n")
    assert load_evolution_history(path) == []
    assert append_evolution_history(
        path, {"bot": "x", "final_status": "skip"}) is True
    raw = path.read_bytes()
    assert raw.startswith(b"\xff\xfe not utf8\n")
    assert [row["bot"] for row in load_evolution_history(path)] == ["x"]
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob("*.lock")) == []


def test_write_failure_keeps_previous_bytes_and_does_not_raise(tmp_path, monkeypatch):
    import src.evolution.history as history_mod

    path = tmp_path / HISTORY_FILENAME
    assert append_evolution_history(path, {"bot": "first", "final_status": "skip"})
    before = path.read_bytes()
    logs = []
    real_open = history_mod._open_binary_append

    def wrapped(target):
        handle = real_open(target)

        def _boom(_data):
            raise OSError("disk full")

        handle.write = _boom
        return handle

    monkeypatch.setattr(history_mod, "_open_binary_append", wrapped)
    assert append_evolution_history(
        path, {"bot": "second", "final_status": "fail"}, log=logs.append) is False
    assert path.read_bytes() == before
    assert load_evolution_history(path) == [{"bot": "first", "final_status": "skip"}]
    assert logs and "OSError" in logs[0]
    assert list(tmp_path.glob("*.tmp")) == []


def test_open_permission_error_retries_then_succeeds(tmp_path, monkeypatch):
    """First N PermissionErrors on open, then success: line kept, no warning."""
    import src.evolution.history as history_mod
    from src.live import main_heartbeat as hb

    path = tmp_path / HISTORY_FILENAME
    assert append_evolution_history(path, {"bot": "first", "final_status": "skip"})
    before = path.read_bytes()
    sleeps = []
    calls = {"n": 0}
    real_open = history_mod._open_binary_append

    def wrapped(target):
        calls["n"] += 1
        if calls["n"] < hb._REPLACE_ATTEMPTS:
            raise PermissionError("busy")
        return real_open(target)

    monkeypatch.setattr(history_mod, "_open_binary_append", wrapped)
    logs = []
    assert append_evolution_history(
        path, {"bot": "second", "final_status": "pass"},
        log=logs.append, sleep=lambda delay: sleeps.append(delay),
    ) is True
    assert calls["n"] == hb._REPLACE_ATTEMPTS
    assert sleeps == list(hb._REPLACE_BACKOFF_S)
    assert logs == []
    assert path.read_bytes().startswith(before)
    assert [row["bot"] for row in load_evolution_history(path)] == [
        "first", "second"]
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob("*.lock")) == []


def test_open_permission_error_always_fails_warns_without_raising(
        tmp_path, monkeypatch):
    """Every open attempt fails: warning, previous bytes kept, no raise."""
    import src.evolution.history as history_mod
    from src.live import main_heartbeat as hb

    path = tmp_path / HISTORY_FILENAME
    assert append_evolution_history(path, {"bot": "first", "final_status": "skip"})
    before = path.read_bytes()
    calls = {"n": 0}
    sleeps = []

    def wrapped(_target):
        calls["n"] += 1
        raise PermissionError("denied")

    monkeypatch.setattr(history_mod, "_open_binary_append", wrapped)
    logs = []
    assert append_evolution_history(
        path, {"bot": "second", "final_status": "fail"},
        log=logs.append, sleep=lambda delay: sleeps.append(delay),
    ) is False
    assert calls["n"] == hb._REPLACE_ATTEMPTS
    assert sleeps == list(hb._REPLACE_BACKOFF_S)
    assert path.read_bytes() == before
    assert load_evolution_history(path) == [
        {"bot": "first", "final_status": "skip"}]
    assert logs and "warning" in logs[0].lower()
    assert "PermissionError" in logs[0]
    assert list(tmp_path.glob("*.tmp")) == []


def test_sequential_appends_keep_every_line_in_order(tmp_path):
    path = tmp_path / HISTORY_FILENAME
    for i in range(20):
        assert append_evolution_history(
            path, {"bot": "Probe", "n": i, "final_status": "skip"}) is True
    raw = path.read_bytes()
    lines = [line for line in raw.splitlines() if line]
    assert len(lines) == 20
    assert [json.loads(line)["n"] for line in lines] == list(range(20))
    assert [row["n"] for row in load_evolution_history(path)] == list(range(20))
    assert list(tmp_path.glob("*.lock")) == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_missing_bot_directory_is_not_created(tmp_path):
    missing = tmp_path / "no-such-bot"
    logs = []
    assert record_evolution_attempt(
        missing, bot="Probe", final_status="skip", reason_kind="watermark",
        log=logs.append) is False
    assert not missing.exists()
    assert logs == []
    not_dir = tmp_path / "not-a-dir"
    not_dir.write_text("x", encoding="utf-8")
    assert record_evolution_attempt(not_dir, bot="Probe", log=logs.append) is False
    assert not_dir.read_text(encoding="utf-8") == "x"
    assert logs == []
    assert list(tmp_path.glob(HISTORY_FILENAME)) == []


def test_numpy_scalars_and_datetimes_are_serialized(tmp_path):
    import numpy as np

    when = datetime(2026, 10, 24, 5, 5, 1)
    record = build_evolution_record(
        bot="Probe",
        final_status="fail",
        reason_kind="validation",
        params_before={
            "ema": np.int64(80),
            "scale": np.float64(1.25),
            "at": when,
            "day": when.date(),
            "bad": np.float64("nan"),
        },
        thresholds={"n": np.int32(4)},
    )
    before = record["params_before"]
    assert before["ema"] == 80 and type(before["ema"]) is int
    assert before["scale"] == 1.25 and type(before["scale"]) is float
    assert before["at"] == "2026-10-24T05:05:01"
    assert before["day"] == "2026-10-24"
    assert before["bad"] is None
    assert record["thresholds"]["n"] == 4 and type(record["thresholds"]["n"]) is int
    path = tmp_path / HISTORY_FILENAME
    assert append_evolution_history(path, record) is True
    loaded = load_evolution_history(path)[0]
    assert loaded["params_before"]["ema"] == 80
    assert loaded["params_before"]["scale"] == 1.25
    assert loaded["params_before"]["at"] == "2026-10-24T05:05:01"
    assert loaded["params_before"]["day"] == "2026-10-24"
    assert loaded["thresholds"]["n"] == 4
    text = path.read_text(encoding="utf-8")
    assert '"ema":80' in text
    assert '"bad":null' in text


def test_successful_append_keeps_the_previous_line_as_a_prefix(tmp_path):
    directory = tmp_path / "nested"
    directory.mkdir()
    path = directory / HISTORY_FILENAME
    assert append_evolution_history(path, {"bot": "first", "final_status": "skip"})
    first = path.read_bytes()
    assert first.endswith(b"\n")
    assert append_evolution_history(path, {"bot": "second", "final_status": "pass"})
    body = path.read_bytes()
    assert body.startswith(first)
    assert [row["bot"] for row in load_evolution_history(path)] == ["first", "second"]


def test_log_callback_failure_does_not_raise(tmp_path, monkeypatch):
    import src.evolution.history as history_mod

    path = tmp_path / HISTORY_FILENAME
    real_open = history_mod._open_binary_append

    def wrapped(target):
        handle = real_open(target)

        def _boom(_data):
            raise OSError("nope")

        handle.write = _boom
        return handle

    def _log(_msg):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(history_mod, "_open_binary_append", wrapped)
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


# ── worker behavior (mutants) and the in-process overlap guard ────────


_HIST_BASE = """
from src.backtest.strategy import BacktestStrategy


class HistBase(BacktestStrategy):
    kline_type = 0
    kline_minute = 1

    def required_bars(self):
        return 1

    def on_bar(self, bar, data_store, broker):
        return None
"""

_HIST_CAND = """
from src.backtest.strategy import BacktestStrategy


class HistBaseEvo1(BacktestStrategy):
    kline_type = 0
    kline_minute = 1

    def required_bars(self):
        return 1

    def on_bar(self, bar, data_store, broker):
        return None
"""

_HIST_PLAN = '```json\n{"action": "change"}\n```\n'
_HIST_CODE = "```python\n" + _HIST_CAND + "\n```\n"


def _hist_bars(n=200):
    from datetime import timedelta
    from src.market_data.models import Bar

    start = datetime(2026, 1, 5, 9, 0)
    return [
        Bar(
            symbol="TXF",
            dt=start + timedelta(minutes=i),
            open=20000, high=20010, low=19990, close=20000,
            volume=1, interval=60,
        )
        for i in range(n)
    ]


def _assembly(passed: bool):
    month = EvolutionVerdict(
        True, ["month held"], month_gates={"passed": True, "reasons": ["month held"]})
    verdict = EvolutionVerdict(
        bool(passed), ["combined"], provisional=False,
        month_gates=month.month_gates)
    return PromotionAssembly(
        verdict=verdict,
        month_verdict=month,
        window_verdict=EvolutionVerdict(bool(passed), ["window held"]),
        multifold_verdict=EvolutionVerdict(bool(passed), ["folds held"]),
        relative_verdict=None,
    )


def _validation_app(bot_dir, chat, *, paper_gate: str):
    import types
    from src.ai.code_sandbox import load_strategy_from_source
    from src.live.live_runner import LiveState

    cls = load_strategy_from_source(_HIST_BASE)
    bars = _hist_bars()
    runner = types.SimpleNamespace(
        state=LiveState.RUNNING,
        bot_dir=str(bot_dir),
        strategy=cls(),
        trading_mode="paper",
        point_value=200,
        target_interval=60,
        symbol="TXF",
        get_bars=lambda: list(bars),
    )
    result = types.SimpleNamespace(trades=[], strategy_name="HistBase", metrics=None)
    app = types.SimpleNamespace(
        _live_runner=runner,
        _settings={
            "evolution_holdout_days": 14,
            "evolution_auto_pipeline": True,
            "evolution_capital_base_twd": 100000,
            "evolution_validation": "simple",
            "evolution_gate_raw": {
                "paper_gate": paper_gate,
                "real_gate": "absolute",
            },
            "ai_provider": "anthropic",
            "ultra_mode": False,
        },
        _chat_client=chat,
        _REVIEW_SINGLE_SHOT_CHAR_LIMIT=600_000,
        _quote_connected=False,
        _live_warmup_mode=False,
        _evo_fetch_cb=None,
        _trading_mode="paper",
        _last_bars=[],
        _strategy_store=types.SimpleNamespace(list_strategies=lambda: []),
        strategy_var=types.SimpleNamespace(get=lambda: "HistBase"),
        chat=[],
        errors=[],
        root=types.SimpleNamespace(after=lambda delay, fn, *args: fn(*args)),
        btn_send=types.SimpleNamespace(config=lambda **kwargs: None),
        _result=result,
    )
    app._resolve_strategy_source = lambda name, cls: _HIST_BASE
    app._append_chat = lambda role, text: app.chat.append((role, text))
    app.set_status = lambda msg: None
    app._on_chat_response = lambda plan: None
    app._on_chat_error = lambda msg: app.errors.append(msg)
    app._remove_last_system_line = lambda: None
    app._evolution_capital_base = types.MethodType(
        rb.BacktestApp._evolution_capital_base, app)
    return app


def _patch_validation(monkeypatch, *, passed: bool):
    import types
    import src.daily_report.changelog as changelog
    import src.evolution.paper_gate as paper_gate
    import src.evolution.pipeline as pipeline

    side = types.SimpleNamespace(
        metrics=None, fitness=None, error="", result=types.SimpleNamespace(trades=[]))
    monkeypatch.setattr(pipeline, "run_ab_backtest", lambda *args, **kwargs: side)
    monkeypatch.setattr(
        pipeline, "run_multifold_validation",
        lambda *args, **kwargs: ([], side, side))
    monkeypatch.setattr(pipeline, "eligible_for_validated_pool", lambda verdict: False)
    monkeypatch.setattr(
        paper_gate, "assemble_promotion_verdict",
        lambda **kwargs: _assembly(passed))
    monkeypatch.setattr(changelog, "append_changelog", lambda **kwargs: None)
    monkeypatch.setattr(changelog, "load_changelog", lambda: [])


def _run_validation(monkeypatch, bot_dir, *, paper_gate: str, passed: bool):
    from tests.test_evolution_min_design_trades import _InlineThread

    chat = MagicMock()
    chat.send_message.return_value = _HIST_PLAN
    chat.one_shot.return_value = _HIST_CODE
    chat.max_tokens = 1024
    chat.conversation = []
    _patch_validation(monkeypatch, passed=passed)
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    app = _validation_app(bot_dir, chat, paper_gate=paper_gate)
    rb.BacktestApp._start_evolution_pipeline(
        app, "preamble", ["design trade"], "", app._result,
        auto_run=False, holdout_days=14, bot_dir=str(bot_dir),
        design_cut_idx=1)
    rows = load_evolution_history(evolution_history_path(bot_dir))
    return app, rows


def test_relative_pass_records_an_evaluated_retire_week(tmp_path, monkeypatch):
    app, rows = _run_validation(
        monkeypatch, tmp_path, paper_gate="relative", passed=True)
    assert app.errors == [], app.chat
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["final_status"] == "pass"
    assert row["reason_kind"] == "validation"
    assert row["retire_week"]["evaluated"] is True
    assert row["retire_week"]["passed"] is True
    assert row["gates"]["promotion_gate"]["mode"] == "relative"
    assert row["gates"]["promotion_gate"]["passed"] is True
    assert row["gates"]["deep"]["reasons"]
    assert row["gates"]["month"]["passed"] is True


def test_relative_fail_keeps_retire_passed_false(tmp_path, monkeypatch):
    app, rows = _run_validation(
        monkeypatch, tmp_path, paper_gate="relative", passed=False)
    assert app.errors == [], app.chat
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["final_status"] == "fail"
    assert row["reason_kind"] == "validation"
    assert row["retire_week"]["evaluated"] is True
    assert row["retire_week"]["passed"] is False
    assert row["gates"]["promotion_gate"]["passed"] is False


def test_absolute_mode_does_not_count_a_retire_week(tmp_path, monkeypatch):
    app, rows = _run_validation(
        monkeypatch, tmp_path, paper_gate="absolute", passed=True)
    assert app.errors == [], app.chat
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["final_status"] == "pass"
    assert row["reason_kind"] == "validation"
    assert row["gates"]["promotion_gate"]["mode"] == "absolute"
    assert row["retire_week"]["evaluated"] is False
    assert row["retire_week"]["passed"] is False


def test_failure_after_the_validation_row_does_not_append_another(
        tmp_path, monkeypatch):
    def _sealed():
        raise RuntimeError("sealed")

    monkeypatch.setattr(rb, "_evolution_history_sealed", _sealed)
    app, rows = _run_validation(
        monkeypatch, tmp_path, paper_gate="relative", passed=True)
    assert app.errors and "sealed" in app.errors[0]
    assert len(rows) == 1, rows
    assert rows[0]["reason_kind"] == "validation"
    assert rows[0]["final_status"] == "pass"
    assert rows[0]["retire_week"]["evaluated"] is True
    assert rows[0]["retire_week"]["passed"] is True


def test_pipeline_error_is_recorded_and_the_guard_is_released(
        tmp_path, monkeypatch):
    from tests.test_evolution_min_design_trades import (
        _NO_CHANGE, _InlineThread, _app, _design_book)

    rb._evolution_guard_clear()
    chat = MagicMock()
    chat.max_tokens = 1024
    chat.conversation = []

    def _explode(*args, **kwargs):
        raise RuntimeError("plan exploded")

    chat.send_message.side_effect = _explode
    monkeypatch.setattr(rb.threading, "Thread", _InlineThread)
    app = _app(tmp_path, _design_book(30), chat)
    rb.BacktestApp._bot_evolution(app, auto_run=True)
    rows = load_evolution_history(evolution_history_path(tmp_path))
    assert len(rows) == 1, (rows, app.chat, app.errors)
    assert rows[0]["reason_kind"] == "pipeline_error"
    assert rows[0]["final_status"] == "fail"
    assert "plan exploded" in rows[0]["reason"]

    chat.send_message.side_effect = None
    chat.send_message.return_value = _NO_CHANGE
    rb.BacktestApp._bot_evolution(app, auto_run=True)
    rows = load_evolution_history(evolution_history_path(tmp_path))
    assert len(rows) == 2, rows
    assert rows[1]["reason_kind"] == "no_change"
    assert not any("already running" in text for _role, text in app.chat)
    rb._evolution_guard_clear()


def test_second_run_is_refused_while_the_first_is_active(tmp_path, monkeypatch):
    import threading
    import time
    from tests.test_evolution_min_design_trades import (
        _DiscordLog, _NO_CHANGE, _app, _design_book)

    rb._evolution_guard_clear()
    entered = threading.Event()
    release = threading.Event()
    chat = MagicMock()
    chat.max_tokens = 1024
    chat.conversation = []

    def _block(*args, **kwargs):
        entered.set()
        release.wait(5)
        return _NO_CHANGE

    chat.send_message.side_effect = _block
    app = _app(tmp_path, _design_book(30), chat)
    discord = _DiscordLog()
    logs = []
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(rb, "_log", logs.append)
    try:
        rb.BacktestApp._bot_evolution(app, auto_run=True)
        assert entered.wait(5), app.chat
        rb.BacktestApp._bot_evolution(app, auto_run=False)
        assert any("already running" in line for line in logs)
        assert any("already running" in text for _role, text in app.chat)
        assert any("already running" in msg for msg in discord.msgs)
        assert any("manual Evolve" in text for _role, text in app.chat)
        assert load_evolution_history(evolution_history_path(tmp_path)) == []
    finally:
        release.set()
        rb._evolution_guard_clear()

    deadline = time.time() + 5
    while time.time() < deadline:
        rows = load_evolution_history(evolution_history_path(tmp_path))
        if any(row["reason_kind"] == "no_change" for row in rows):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("first run did not finish after the guard released")

    chat.send_message.side_effect = None
    chat.send_message.return_value = _NO_CHANGE
    before = len(discord.msgs)
    rb.BacktestApp._bot_evolution(app, auto_run=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        rows = load_evolution_history(evolution_history_path(tmp_path))
        if len(rows) >= 2:
            break
        time.sleep(0.02)
    else:
        raise AssertionError(load_evolution_history(evolution_history_path(tmp_path)))
    assert rows[-1]["reason_kind"] == "no_change"
    assert discord.msgs[before:] == [] or not any(
        "already running" in msg for msg in discord.msgs[before:])
    rb._evolution_guard_clear()


def test_different_bots_do_not_block_each_other(tmp_path, monkeypatch):
    import threading
    import time
    from tests.test_evolution_min_design_trades import (
        _NO_CHANGE, _app, _design_book)

    rb._evolution_guard_clear()
    entered = threading.Event()
    release = threading.Event()
    busy = MagicMock()
    busy.max_tokens = 1024
    busy.conversation = []

    def _block(*args, **kwargs):
        entered.set()
        release.wait(5)
        return _NO_CHANGE

    busy.send_message.side_effect = _block
    bot_a = tmp_path / "bot-a"
    bot_b = tmp_path / "bot-b"
    bot_a.mkdir()
    bot_b.mkdir()
    app_a = _app(bot_a, _design_book(30), busy)
    other = MagicMock()
    other.max_tokens = 1024
    other.conversation = []
    other.send_message.return_value = _NO_CHANGE
    app_b = _app(bot_b, _design_book(30), other)
    try:
        rb.BacktestApp._bot_evolution(app_a, auto_run=True)
        assert entered.wait(5), app_a.chat
        rb.BacktestApp._bot_evolution(app_b, auto_run=True)
        deadline = time.time() + 5
        while time.time() < deadline:
            rows_b = load_evolution_history(evolution_history_path(bot_b))
            if rows_b:
                break
            time.sleep(0.02)
        else:
            raise AssertionError(app_b.chat)
        assert rows_b[0]["reason_kind"] == "no_change"
        assert not any("already running" in text for _role, text in app_b.chat)
        assert load_evolution_history(evolution_history_path(bot_a)) == []
    finally:
        release.set()
        rb._evolution_guard_clear()
