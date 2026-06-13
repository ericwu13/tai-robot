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
        # 4 great trades vs a 40-trade baseline = filtered (nearly)
        # everything away — can't pass no matter the metrics
        baseline = _ab("base", [100] * 40)
        candidate = _ab("cand", [500] * 4)
        criteria = {"profit_factor_min": 1.0}
        v = decide_verdict(baseline, candidate, criteria)
        assert not v.passed

    def test_floor_is_relative_to_baseline_window(self):
        # The 0422 case: 8 candidate trades vs 18 baseline in a 14-day
        # holdout is normal density for a trend filter — must NOT fail
        # the floor (old absolute floor of 10 wrongly rejected this).
        baseline = _ab("base", [100, -100] * 9)   # 18 trades
        candidate = _ab("cand", [200, -50] * 4)   # 8 trades, better PF/DD
        v = decide_verdict(baseline, candidate, None)
        assert v.passed

    def test_floor_absolute_minimum_still_applies(self):
        # Even vs a tiny baseline, fewer than 5 trades can't validate
        baseline = _ab("base", [100, -100] * 4)   # 8 trades → floor 5
        candidate = _ab("cand", [500] * 4)        # 4 trades
        v = decide_verdict(baseline, candidate, None)
        assert not v.passed

    def test_candidate_trade_floor_math(self):
        from src.evolution.pipeline import candidate_trade_floor
        assert candidate_trade_floor(18) == 5    # ceil(4.5) < abs-min 5
        assert candidate_trade_floor(40) == 10   # ceil(10)
        assert candidate_trade_floor(122) == 31  # 90d window scale
        assert candidate_trade_floor(None) == MIN_CANDIDATE_TRADES
        assert candidate_trade_floor(0) == MIN_CANDIDATE_TRADES

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


# ── walk-forward validation ──

def _trade_at(exit_dt: str, pnl: int = 100):
    return Trade(tag="L", side=OrderSide.LONG, qty=1, entry_price=20000,
                 exit_price=20000 + pnl, entry_bar_index=0, exit_bar_index=1,
                 pnl=pnl, exit_dt=exit_dt)


class TestDesignHoldoutSplit:
    def test_cut_anchored_at_last_exit(self):
        from src.evolution.pipeline import compute_design_cut
        trades = [_trade_at("2026-06-01 10:00"), _trade_at("2026-06-12 04:58")]
        cut = compute_design_cut(trades, 14)
        assert cut == "2026-05-29 04:58"

    def test_cut_empty_without_dated_trades(self):
        from src.evolution.pipeline import compute_design_cut
        assert compute_design_cut([], 14) == ""
        assert compute_design_cut([_trade_at("")], 14) == ""

    def test_holdout_is_a_suffix(self):
        from src.evolution.pipeline import design_cutoff_index
        trades = [_trade_at("2026-05-01 10:00"),
                  _trade_at("2026-05-20 10:00"),
                  _trade_at("2026-06-05 10:00"),
                  _trade_at("2026-06-10 10:00")]
        idx = design_cutoff_index(trades, 0, "2026-06-01 00:00")
        assert idx == 2  # first two are design, last two holdout

    def test_no_holdout_trades(self):
        from src.evolution.pipeline import design_cutoff_index
        trades = [_trade_at("2026-05-01 10:00")]
        assert design_cutoff_index(trades, 0, "2026-06-01 00:00") == 1

    def test_all_in_holdout(self):
        from src.evolution.pipeline import design_cutoff_index
        trades = [_trade_at("2026-06-05 10:00")]
        assert design_cutoff_index(trades, 0, "2026-06-01 00:00") == 0

    def test_undated_trade_stays_in_design(self):
        from src.evolution.pipeline import design_cutoff_index
        trades = [_trade_at(""), _trade_at("2026-06-05 10:00")]
        assert design_cutoff_index(trades, 0, "2026-06-01 00:00") == 1

    def test_start_offset_respected(self):
        from src.evolution.pipeline import design_cutoff_index
        trades = [_trade_at("2026-06-05 10:00"),
                  _trade_at("2026-06-06 10:00")]
        # scanning from index 1 cannot return less than 1
        assert design_cutoff_index(trades, 1, "2026-06-01 00:00") == 1


class TestTrainCollapseGate:
    def _deep2(self, name, train_pnls, test_pnls, mc=0.1):
        from src.evolution.pipeline import DeepResult
        return DeepResult(name=name, train=_ab(f"{name} train", train_pnls),
                          test=_ab(f"{name} test", test_pnls),
                          fragile=False, mc_variance=mc,
                          train_span="t", test_span="t")

    def test_train_dd_collapse_fails_despite_good_holdout(self):
        from src.evolution.pipeline import decide_deep_verdict
        # baseline train: mild DD; candidate train: deep DD (curve-fit
        # signature, like the observed atr-1.5 case: 71% vs 21%)
        base = self._deep2("base", [200, -100] * 20, [100, -200] * 10)
        cand = self._deep2("cand", [500, -450, -450, 500] * 10,
                           [200, -100] * 10)
        v = decide_deep_verdict(base, cand, None)
        assert not v.passed
        assert any("collapse" in r for r in v.reasons)

    def test_healthy_ratios_pass(self):
        from src.evolution.pipeline import decide_deep_verdict
        base = self._deep2("base", [100, -200] * 20, [100, -200] * 10)
        cand = self._deep2("cand", [120, -180] * 20, [200, -100] * 10)
        v = decide_deep_verdict(base, cand, None)
        assert v.passed

    def test_dd_criterion_checked_on_train_window(self):
        # Plan dd thresholds are anchored on the long-window basis —
        # a candidate whose TRAIN dd violates them fails, even when the
        # holdout dd looks fine.
        from src.evolution.pipeline import decide_deep_verdict
        base = self._deep2("base", [100, -200] * 20, [100, -200] * 10)
        cand = self._deep2("cand", [100, -200] * 20, [200, -100] * 10)
        # baseline-like train: deep dd; ask for dd ≤ 5 (impossible)
        v = decide_deep_verdict(base, cand, {"max_drawdown_pct_max": 5})
        assert not v.passed
        assert any("train-window MaxDD%" in r for r in v.reasons)
        assert v.used_criteria  # dd-only criteria still count as criteria

    def test_holdout_dd_relative_gate(self):
        # Holdout dd is gated vs baseline (≤ 1.2×), never vs the plan's
        # absolute threshold — short-window dd% explodes by construction.
        from src.evolution.pipeline import decide_deep_verdict
        base = self._deep2("base", [100, -200] * 20, [200, -100] * 10)
        # candidate holdout: much deeper dd than baseline's
        cand = self._deep2("cand", [120, -180] * 20,
                           [500, -450, -450, 500] * 5)
        v = decide_deep_verdict(base, cand, None)
        assert not v.passed
        assert any("holdout MaxDD ratio" in r for r in v.reasons)


class TestSplitTrainTest:
    def test_anchored_at_end(self):
        from src.evolution.pipeline import split_train_test
        bars = _bars(n=200)  # 15-min bars ≈ 2 days... use day-spaced
        from datetime import datetime, timedelta
        from src.market_data.models import Bar
        bars = [Bar(symbol="T", dt=datetime(2026, 1, 1) + timedelta(days=i),
                    open=1, high=2, low=1, close=1, volume=1, interval=86400)
                for i in range(100)]
        train, test = split_train_test(bars, 30, 30)
        assert len(test) > 0 and len(train) > 0
        assert train[-1].dt < test[0].dt
        # test window = most recent 30 days
        assert (bars[-1].dt - test[0].dt).days <= 30
        # older bars discarded
        assert train[0].dt >= bars[-1].dt - timedelta(days=60)

    def test_empty_input(self):
        from src.evolution.pipeline import split_train_test
        assert split_train_test([], 30, 30) == ([], [])


class TestRunDeepValidation:
    def test_runs_both_windows(self):
        from src.evolution.pipeline import run_deep_validation
        from datetime import datetime, timedelta
        from src.market_data.models import Bar
        bars = []
        for i in range(50 * 20):  # 50 days × 20 bars/day
            day, slot = divmod(i, 20)
            c = 20000 + i % 37 * 5
            bars.append(Bar(symbol="T",
                            dt=datetime(2026, 1, 1, 9, 0) + timedelta(days=day, minutes=15 * slot),
                            open=c - 3, high=c + 10, low=c - 10, close=c,
                            volume=100, interval=900))
        res = run_deep_validation(_AlwaysLong, bars, 200, "base", 25, 25,
                                  monte_carlo=False)
        assert res.train.error == "" and res.test.error == ""
        assert res.train.metrics.total_trades > 0
        assert res.test.metrics.total_trades > 0
        assert "→" in res.train_span and "→" in res.test_span


class TestDecideDeepVerdict:
    def _deep(self, name, train_pnls, test_pnls, fragile=False, mc=0.0):
        from src.evolution.pipeline import DeepResult
        return DeepResult(name=name, train=_ab(f"{name} train", train_pnls),
                          test=_ab(f"{name} test", test_pnls),
                          fragile=fragile, mc_variance=mc,
                          train_span="t", test_span="t")

    def test_oos_decides_pass(self):
        from src.evolution.pipeline import decide_deep_verdict
        base = self._deep("base", [100, -200] * 20, [100, -200] * 10)
        cand = self._deep("cand", [100, -200] * 20, [200, -100] * 10, mc=0.1)
        v = decide_deep_verdict(base, cand, None)
        assert v.passed

    def test_fragile_fails_even_with_better_oos(self):
        from src.evolution.pipeline import decide_deep_verdict
        base = self._deep("base", [100, -200] * 20, [100, -200] * 10)
        cand = self._deep("cand", [100, -200] * 20, [200, -100] * 10,
                          fragile=True, mc=0.55)
        v = decide_deep_verdict(base, cand, None)
        assert not v.passed
        assert any("FRAGILE" in r for r in v.reasons)

    def test_worse_oos_composite_fails(self):
        from src.evolution.pipeline import decide_deep_verdict
        # 40 trades each → composites not gated; candidate worse OOS
        base = self._deep("base", [200, -100] * 20, [200, -100] * 20)
        cand = self._deep("cand", [200, -100] * 20, [100, -200] * 20)
        v = decide_deep_verdict(base, cand, None)
        assert not v.passed

    def test_candidate_test_error_fails(self):
        from src.evolution.pipeline import decide_deep_verdict, DeepResult, ABResult
        base = self._deep("base", [100] * 20, [100] * 20)
        cand = DeepResult(name="cand", train=_ab("t", [100] * 20),
                          test=ABResult(name="te", error="boom"))
        v = decide_deep_verdict(base, cand, None)
        assert not v.passed

    def test_criteria_checked_on_test_window(self):
        from src.evolution.pipeline import decide_deep_verdict
        base = self._deep("base", [100, -200] * 20, [100, -200] * 10)
        # candidate OOS WR = 50%, criteria demands 60% → fail
        cand = self._deep("cand", [200, -100] * 20, [200, -100] * 10, mc=0.1)
        v = decide_deep_verdict(base, cand, {"win_rate_min": 0.6})
        assert not v.passed
        assert v.used_criteria


class TestFormatDeepVerdictBlock:
    def test_block_structure(self):
        from src.evolution.pipeline import (
            decide_deep_verdict, format_deep_verdict_block, DeepResult)
        base = DeepResult(name="基準 baseline",
                          train=_ab("基準 train", [100, -200] * 20),
                          test=_ab("基準 test", [100, -200] * 10),
                          train_span="2026-01-01 → 2026-03-31 (x)",
                          test_span="2026-04-01 → 2026-06-30 (y)")
        cand = DeepResult(name="候選 FooEvo1",
                          train=_ab("候選 train", [100, -200] * 20),
                          test=_ab("候選 test", [200, -100] * 10),
                          mc_variance=0.1,
                          train_span="2026-01-01 → 2026-03-31 (x)",
                          test_span="2026-04-01 → 2026-06-30 (y)")
        v = decide_deep_verdict(base, cand, None)
        block = format_deep_verdict_block(base, cand, v, "Capital API",
                                          saved_as="AI: FooEvo1")
        assert "walk-forward" in block
        assert "out-of-sample" in block
        assert "UNSEEN by design" in block
        assert "訓練期" in block and "測試期" in block
