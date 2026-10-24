"""Issue #154 — partial current month is logged.

The 10-10 Evo1 ema60 shape still PASSes when the default call supplies
no price bars: ``merge_stub`` cannot count sessions and keeps the
October stub. That stub's delta is −260 against a +3240 edge
(−8.0% ≤ 50%). September's +5890 sits in the prior block. Gate 1
uses the default scaled allowance (13190 TWD for this two-trade prior
block). Passing 11 October trading dates folds the stub and that
shape FAILs.

The #153 9/26 gold pin stays in ``test_evolution_issue153`` and must
still FAIL. Existing PASS fixtures there are not modified.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from pathlib import Path

import run_backtest as rb
from src.backtest.broker import OrderSide, Trade
from src.evolution.pipeline import (
    decide_month_gates,
    eligible_for_validated_pool,
    format_month_gates_debug,
)

_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures" / "issue154_oct_stub_month_gates.json"
)


def _load() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _trade(exit_dt: str, pnl: int, idx: int) -> Trade:
    return Trade(
        tag="L", side=OrderSide.LONG, qty=1, entry_price=20000,
        exit_price=20000, entry_bar_index=idx, exit_bar_index=idx + 1,
        pnl=pnl, exit_dt=exit_dt,
    )


def _books(data: dict) -> tuple[list[Trade], list[Trade]]:
    base: list[Trade] = []
    cand: list[Trade] = []
    for i, row in enumerate(data["months"], start=1):
        exit_dt = f"{row['month']}-15 13:45"
        base.append(_trade(exit_dt, row["base_net"], i))
        cand.append(_trade(exit_dt, row["cand_net"], i + 100))
        assert row["cand_net"] - row["base_net"] == row["delta"]
    return base, cand


def _verdict(data: dict | None = None):
    data = data or _load()
    base, cand = _books(data)
    return decide_month_gates(base, cand, holdout_start=data["holdout_start"])


def _pipeline_src() -> str:
    return textwrap.dedent(
        inspect.getsource(rb.BacktestApp._start_evolution_pipeline))


def _month_gate_log_try(src: str) -> str:
    """Source of the try that directly guards the month-gate debug line.

    The evolution worker is itself a try. Pick the smallest try whose
    text contains the debug call so the worker's outer handler is not
    the one under test.
    """
    tree = ast.parse(src)
    matches: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        segment = ast.get_source_segment(src, node)
        if segment and "format_month_gates_debug" in segment:
            matches.append(segment)
    if not matches:
        raise AssertionError("month-gate debug log is not inside try/except")
    return min(matches, key=len)


def _normalize_source_segment(segment: str) -> str:
    """``ast.get_source_segment`` drops the indent on the first line only.

    Shift the remaining lines back by their common indent so the snippet
    parses and executes on its own.
    """
    lines = segment.splitlines()
    indents = [
        len(line) - len(line.lstrip(" "))
        for line in lines[1:]
        if line.strip()
    ]
    if not indents:
        return segment
    cut = min(indents)
    normalized = [lines[0]]
    for line in lines[1:]:
        if line.strip():
            normalized.append(line[cut:])
        else:
            normalized.append("")
    return "\n".join(normalized)


class TestOctStubCurrentPass:
    """10-10 shape under the rule that is shipped today."""

    def test_shape_sep_oct_and_full_edge(self):
        data = _load()
        by_month = {row["month"]: row for row in data["months"]}
        assert by_month["2026-09"]["delta"] == data["sep_delta"] == 5890
        assert by_month["2026-10"]["delta"] == data["oct_stub_delta"] == -260
        assert sum(row["delta"] for row in data["months"]) == data["full_edge"] == 3240
        # September alone is 181.8% of the edge; the stub is −8.0%.
        assert float(f"{(5890 / 3240) * 100:.1f}") == 181.8
        assert data["desired_sep_share"] == "181.8%"
        assert data["current_share"] == "-8.0%"
        assert data["ablation_slack"] == 8500
        assert data["cand_prior_net"] - data["floor"] == 8500
        assert data["floor"] == data["base_prior_net"] - 5000

    def test_current_verdict_passes(self):
        data = _load()
        v = _verdict(data)
        text = "\n".join(v.reasons)
        assert v.passed, text
        assert not v.provisional
        assert eligible_for_validated_pool(v)
        assert "prior-months collapse" not in text
        assert "calendar-month dominated" not in text
        assert (
            "month ablation: prior-months net +36690 ≥ baseline−13190 "
            "(+20000): ✓ (slack +16690)"
        ) in text
        assert (
            "latest-month edge share: edge +3240 > 0; "
            "Oct share -260/3240 = -8.0% ≤ 50%: ✓"
        ) in text
        assert "outside latest calendar month Oct" in text
        block = v.month_gates
        assert block is not None
        assert block["passed"] is True
        assert block["latest"] == "2026-10"
        assert block["latest_label"] == "Oct"
        assert block["edge"] == 3240
        assert block["latest_delta"] == -260
        assert block["share_pct"] == -8.0
        assert block["share_applied"] is True
        assert block["edge"] > 0
        assert block["ablation_slack"] == 16690
        assert block["base_prior_net"] == 33190
        assert block["cand_prior_net"] == 36690
        rows = {row["month"]: row for row in block["months"]}
        assert rows["2026-09"]["delta"] == 5890
        assert rows["2026-09"]["base_net"] == 10000
        assert rows["2026-09"]["cand_net"] == 15890
        assert rows["2026-10"]["delta"] == -260
        assert rows["2026-10"]["base_net"] == -9740
        assert rows["2026-10"]["cand_net"] == -10000
        assert block["reasons"] == v.reasons

    def test_debug_line_carries_inputs_and_reasons(self):
        block = _verdict().month_gates
        log = format_month_gates_debug(block)
        assert "verdict=PASS" in log
        assert "latest=Oct (2026-10)" in log
        assert "edge=+3240" in log
        assert "share=-8.0%" in log
        assert "ablation_slack=+16690" in log
        assert "holdout=2026-09-25 00:00" in log
        assert "2026-09 base=+10000 cand=+15890 Δ=+5890" in log
        assert "2026-10 base=−9740 cand=−10000 Δ=−260" in log
        assert "Oct share -260/3240 = -8.0%" in log
        assert "reasons:" in log

    def test_debug_log_file_receives_the_line(self, tmp_path, monkeypatch):
        path = tmp_path / "debug.log"
        handle = path.open("a", encoding="utf-8")
        monkeypatch.setattr(rb, "_debug_log_file", handle)
        monkeypatch.setattr(rb, "_app", None)
        try:
            rb._log(format_month_gates_debug(_verdict().month_gates))
            handle.flush()
            text = path.read_text(encoding="utf-8")
        finally:
            handle.close()
        assert "EVO month gates: verdict=PASS" in text
        assert "latest=Oct" in text
        assert "share=-8.0%" in text
        assert "ablation_slack=+16690" in text
        assert "2026-09" in text and "2026-10" in text


class TestShareAppliedTracksPositiveEdge:
    """``share_applied=edge > 0`` forced to True still passes the 10-10
    case. A non-positive edge is what kills that mutant."""

    def test_share_applied_is_false_when_edge_is_not_positive(self):
        zero_base = [_trade("2026-10-01 10:00", 100, 1)]
        zero_cand = [_trade("2026-10-01 10:00", 100, 2)]
        zero = decide_month_gates(
            zero_base, zero_cand, holdout_start="2026-09-01 00:00")
        assert zero.month_gates["edge"] == 0
        assert zero.month_gates["share_applied"] is False
        assert any("share gate not applied" in r for r in zero.reasons)

        neg_base = [_trade("2026-10-01 10:00", 100, 1)]
        neg_cand = [_trade("2026-10-01 10:00", 0, 2)]
        neg = decide_month_gates(
            neg_base, neg_cand, holdout_start="2026-09-01 00:00")
        assert neg.month_gates["edge"] == -100
        assert neg.month_gates["share_applied"] is False
        assert any("share gate not applied" in r for r in neg.reasons)


class TestOctStubDesiredFail:
    def test_partial_october_should_fail_share_cap(self):
        """A short October stub must not be the only month the share cap sees.

        September's +5890 is 181.8% of the +3240 edge. With
        ``latest_def=merge_stub`` and 11 October trading dates in the
        price bars, October folds into September and this shape FAILs
        (calendar-month dominated). The no-bars default call still
        PASSes; that pin is ``TestOctStubCurrentPass``.
        """
        from datetime import datetime, timedelta

        from src.evolution.pipeline import MonthGateConfig
        from src.market_data.holidays import is_taifex_holiday

        # Eleven day-session trading dates. Midnight stamps would map
        # through the night session and collapse weekends.
        cursor = datetime(2026, 10, 1)
        days = []
        while len(days) < 11:
            if not is_taifex_holiday(cursor.date()):
                days.append(cursor.replace(hour=9))
            cursor += timedelta(days=1)
        bars = [type("B", (), {"dt": day})() for day in days]
        data = _load()
        base, cand = _books(data)
        v = decide_month_gates(
            base, cand,
            holdout_start=data["holdout_start"],
            bars=bars,
            month_gate=MonthGateConfig(latest_def="merge_stub"),
        )
        text = "\n".join(v.reasons)
        assert not v.passed, text
        assert "calendar-month dominated" in text
        assert v.month_gates["folded_from"] == "2026-10"
        assert v.month_gates["latest"] == "2026-09"
        assert v.month_gates["latest_sessions"] == 11


class TestIssue153GoldPinStillFails:
    def test_september_dominated_ema80_still_fails(self):
        from tests.test_evolution_issue153 import _ema80_verdict

        v = _ema80_verdict()
        text = "\n".join(v.reasons)
        assert not v.passed
        assert "prior-months collapse" in text
        assert "calendar-month dominated" in text
        assert "161.5%" in text
        assert v.month_gates is not None
        assert v.month_gates["latest"] == "2026-09"
        assert v.month_gates["latest_label"] == "Sep"
        assert v.month_gates["share_pct"] == 161.5
        assert v.month_gates["passed"] is False


class TestMonthGateLoggingWired:
    def test_worker_logs_before_combine_and_persists_block(self):
        """The worker logs the observation assemble_promotion_verdict
        threads out, then the PASS changelog forwards the same value.

        ``combine_evolution_verdicts`` stays inside the helper. The
        worker's month verdict is ``assembled.month_verdict``.
        """
        src = _pipeline_src()
        assembled = src.index("assembled = assemble_promotion_verdict(")
        bind = src.index("month_verdict = assembled.month_verdict")
        logged = src.index("_log(format_month_gates_debug(month_verdict.month_gates))")
        persisted = src.index("month_gates=month_verdict.month_gates")
        assert assembled < bind < logged < persisted
        assert "month_verdict = decide_month_gates(" not in src
        assert "verdict = combine_evolution_verdicts(" not in src
        # The call is inside the worker, not only the import.
        tree = ast.parse(src)
        logs = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_log"
            and node.args
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Name)
            and node.args[0].func.id == "format_month_gates_debug"
        ]
        assert len(logs) == 1
        guarded = _month_gate_log_try(src)
        assert guarded.strip().startswith("try:")
        assert "except Exception" in guarded
        assert bind < src.index(guarded) < persisted
        # The handler records the failure and does not re-raise.
        guard_tree = ast.parse(_normalize_source_segment(guarded))
        try_node = guard_tree.body[0]
        assert isinstance(try_node, ast.Try)
        assert try_node.handlers
        assert all(
            isinstance(h.type, ast.Name) and h.type.id == "Exception"
            for h in try_node.handlers)
        assert not any(isinstance(n, ast.Raise) for n in ast.walk(try_node))

    def test_log_failure_does_not_escape(self):
        src = _pipeline_src()
        segment = _normalize_source_segment(_month_gate_log_try(src))
        month_verdict = type("V", (), {"month_gates": {"passed": True}})()

        logged: list[str] = []

        def _ok(msg):
            logged.append(msg)

        exec(segment, {
            "_log": _ok,
            "format_month_gates_debug": lambda block: "EVO month gates: verdict=PASS",
            "month_verdict": month_verdict,
        })
        assert logged == ["EVO month gates: verdict=PASS"]

        def _format_boom(block):
            raise ValueError("bad observation")

        logged.clear()
        exec(segment, {
            "_log": _ok,
            "format_month_gates_debug": _format_boom,
            "month_verdict": month_verdict,
        })
        assert len(logged) == 1
        assert "EVO month gates log failed:" in logged[0]
        assert "ValueError" in logged[0]

        def _log_boom(msg):
            raise OSError("debug.log unavailable")

        exec(segment, {
            "_log": _log_boom,
            "format_month_gates_debug": lambda block: "EVO month gates: verdict=PASS",
            "month_verdict": month_verdict,
        })
