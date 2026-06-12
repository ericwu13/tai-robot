"""Tests for the auto-evolution pipeline: directives parsing, candidate
naming, A/B backtest, and verdict logic."""

from datetime import datetime, timedelta
from itertools import accumulate

from src.backtest.broker import Trade, OrderSide
from src.backtest.metrics import calculate_metrics
from src.backtest.strategy import BacktestStrategy
from src.market_data.models import Bar
from src.evolution.pipeline import (
    ABResult,
    MIN_CANDIDATE_TRADES,
    decide_verdict,
    format_verdict_block,
    next_candidate_name,
    parse_plan_directives,
    run_ab_backtest,
)


# ── Helpers ──

def _ab(name: str, pnls: list[int]) -> ABResult:
    trades = [
        Trade(tag="L", side=OrderSide.LONG, qty=1, entry_price=20000,
              exit_price=20000 + p, entry_bar_index=i, exit_bar_index=i + 1,
              pnl=p)
        for i, p in enumerate(pnls)
    ]
    eq = list(accumulate(pnls))

    class _R:
        pass
    r = _R()
    r.metrics = calculate_metrics(trades, eq, initial_balance=0)
    r.trades = trades
    r.equity_curve = eq
    return ABResult(name=name, result=r)


def _bars(n: int = 200, rising: bool = True) -> list[Bar]:
    out = []
    base = 20000
    dt = datetime(2026, 6, 1, 9, 0)
    for i in range(n):
        drift = i * 5 if rising else -i * 5
        c = base + drift
        out.append(Bar(symbol="TEST", dt=dt + timedelta(minutes=15 * i),
                       open=c - 3, high=c + 10, low=c - 10, close=c,
                       volume=100, interval=900))
    return out


class _AlwaysLong(BacktestStrategy):
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0:
            broker.entry("L", OrderSide.LONG)
        else:
            broker.exit("X", "L", limit=bar.close + 30, stop=bar.close - 60)

    def required_bars(self):
        return 2


class _BrokenInit(BacktestStrategy):
    kline_type = 0
    kline_minute = 15

    def __init__(self):
        raise RuntimeError("boom")

    def on_bar(self, bar, data_store, broker):
        pass


# ── parse_plan_directives ──

class TestParsePlanDirectives:
    def test_full_block(self):
        plan = (
            "...analysis...\n```json\n"
            '{"action": "change", "max_drawdown_pct_max": 35, '
            '"profit_factor_min": 1.2, "win_rate_min": 0.45}\n```'
        )
        d = parse_plan_directives(plan)
        assert d["action"] == "change"
        assert d["criteria"] == {
            "max_drawdown_pct_max": 35.0,
            "profit_factor_min": 1.2,
            "win_rate_min": 0.45,
        }

    def test_no_change_action(self):
        plan = '```json\n{"action": "no_change"}\n```'
        d = parse_plan_directives(plan)
        assert d["action"] == "no_change"
        assert d["criteria"] is None

    def test_missing_fence_degrades_to_change(self):
        d = parse_plan_directives("plain text plan, no json")
        assert d["action"] == "change"
        assert d["criteria"] is None

    def test_corrupt_json_degrades(self):
        d = parse_plan_directives("```json\n{not valid json]\n```")
        assert d["action"] == "change"
        assert d["criteria"] is None

    def test_win_rate_percentage_normalized(self):
        plan = '```json\n{"action": "change", "win_rate_min": 45}\n```'
        d = parse_plan_directives(plan)
        assert d["criteria"]["win_rate_min"] == 0.45

    def test_last_fence_wins(self):
        plan = (
            '```json\n{"action": "no_change"}\n```\n...\n'
            '```json\n{"action": "change", "profit_factor_min": 1.5}\n```'
        )
        d = parse_plan_directives(plan)
        assert d["action"] == "change"
        assert d["criteria"]["profit_factor_min"] == 1.5

    def test_garbage_criteria_value_skipped(self):
        plan = ('```json\n{"action": "change", '
                '"profit_factor_min": "high", "win_rate_min": 0.4}\n```')
        d = parse_plan_directives(plan)
        assert d["criteria"] == {"win_rate_min": 0.4}

    def test_empty_input(self):
        assert parse_plan_directives("")["action"] == "change"
        assert parse_plan_directives(None)["action"] == "change"


# ── next_candidate_name ──

class TestNextCandidateName:
    def test_fresh_name(self):
        assert next_candidate_name("FooStrategy") == "FooStrategyEvo1"

    def test_skips_taken(self):
        taken = {"FooStrategyEvo1", "FooStrategyEvo2"}
        assert next_candidate_name("FooStrategy", taken) == "FooStrategyEvo3"

    def test_evolving_an_evolved_strategy(self):
        # FooEvo1 → FooEvo2, never FooEvo1Evo1
        assert next_candidate_name("FooStrategyEvo1") == "FooStrategyEvo2"

    def test_evolved_base_with_taken(self):
        taken = {"FooStrategyEvo2"}
        assert next_candidate_name("FooStrategyEvo1", taken) == "FooStrategyEvo3"


# ── decide_verdict ──

class TestDecideVerdict:
    def test_candidate_error_fails(self):
        baseline = _ab("base", [100] * 20)
        candidate = ABResult(name="cand", error="boom")
        v = decide_verdict(baseline, candidate, None)
        assert not v.passed

    def test_zero_trades_fails(self):
        baseline = _ab("base", [100] * 20)
        candidate = _ab("cand", [])
        v = decide_verdict(baseline, candidate, None)
        assert not v.passed

    def test_criteria_all_pass(self):
        baseline = _ab("base", [100] * 20)
        candidate = _ab("cand", [200] * 10 + [-50] * 2)
        criteria = {"profit_factor_min": 1.5, "win_rate_min": 0.5}
        v = decide_verdict(baseline, candidate, criteria)
        assert v.passed
        assert v.used_criteria

    def test_criteria_one_fails(self):
        baseline = _ab("base", [100] * 20)
        candidate = _ab("cand", [200] * 10 + [-50] * 10)  # WR 50%
        criteria = {"win_rate_min": 0.6}
        v = decide_verdict(baseline, candidate, criteria)
        assert not v.passed

    def test_hard_floor_overrides_perfect_criteria(self):
        # 5 great trades can't pass — "filter everything away" guard
        baseline = _ab("base", [100] * 20)
        candidate = _ab("cand", [500] * (MIN_CANDIDATE_TRADES - 5))
        criteria = {"profit_factor_min": 1.0}
        v = decide_verdict(baseline, candidate, criteria)
        assert not v.passed

    def test_default_guard_pass(self):
        baseline = _ab("base", [100, -200] * 10)
        candidate = _ab("cand", [150, -100] * 10)  # better PF, smaller DD
        v = decide_verdict(baseline, candidate, None)
        assert v.passed
        assert not v.used_criteria

    def test_default_guard_fails_on_worse_pf(self):
        baseline = _ab("base", [200, -100] * 10)
        candidate = _ab("cand", [100, -200] * 10)
        v = decide_verdict(baseline, candidate, None)
        assert not v.passed

    def test_default_guard_needs_baseline(self):
        baseline = ABResult(name="base", error="boom")
        candidate = _ab("cand", [100] * 20)
        v = decide_verdict(baseline, candidate, None)
        assert not v.passed


# ── run_ab_backtest ──

class TestRunABBacktest:
    def test_runs_and_scores(self):
        res = run_ab_backtest(_AlwaysLong, _bars(200), 200, "base")
        assert res.error == ""
        assert res.result is not None
        assert res.metrics.total_trades > 0
        assert res.fitness is not None

    def test_broken_strategy_reports_error(self):
        res = run_ab_backtest(_BrokenInit, _bars(200), 200, "cand")
        assert res.error != ""
        assert res.result is None


# ── format_verdict_block ──

class TestFormatVerdictBlock:
    def test_pass_block_mentions_saved(self):
        baseline = _ab("基準 baseline", [100, -200] * 10)
        candidate = _ab("候選 FooEvo1", [150, -100] * 10)
        v = decide_verdict(baseline, candidate, None)
        block = format_verdict_block(baseline, candidate, v,
                                     "2026-06-01 → 06-12 (200 bars)",
                                     saved_as="AI: FooEvo1")
        assert "PASS" in block
        assert "AI: FooEvo1" in block
        assert "deploy manually" in block

    def test_fail_block_mentions_rejection(self):
        baseline = _ab("基準 baseline", [200, -100] * 10)
        candidate = _ab("候選 FooEvo1", [100, -200] * 10)
        v = decide_verdict(baseline, candidate, None)
        block = format_verdict_block(baseline, candidate, v, "window")
        assert "FAIL" in block
        assert "nothing was saved" in block
