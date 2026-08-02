"""Tests for the multi-fold paired validation layer (issue #99).

A single 14-day holdout on a ~30-trades/month strategy is statistically
meaningless, so the verdict is pooled across consecutive rolling folds
and paired fold-by-fold against the baseline.
"""

from datetime import datetime, timedelta

from src.backtest.broker import OrderSide, Trade
from src.backtest.strategy import BacktestStrategy
from src.market_data.models import Bar
from src.evolution.pipeline import (
    Fold,
    analyze_folds,
    decide_multifold_verdict,
    format_multifold_block,
    run_multifold_validation,
    split_trade_overlap,
    trade_key,
    window_stats,
)


# ── Helpers ──

def _t(exit_dt: str, pnl: int, idx: int = 0) -> Trade:
    """A dated trade. Two calls with the same (exit_dt, pnl, idx) produce
    IDENTICAL trade keys — that is how the fixtures make trades "shared"
    between the two sides."""
    return Trade(tag="L", side=OrderSide.LONG, qty=1, entry_price=20000,
                 exit_price=20000 + pnl, entry_bar_index=idx,
                 exit_bar_index=idx + 1, pnl=pnl, exit_dt=exit_dt)


def _fold(n: int, base_pnls: list[int], cand_pnls: list[int],
          shared: int = 0) -> Fold:
    b = window_stats([_t("", p) for p in base_pnls])
    c = window_stats([_t("", p) for p in cand_pnls])
    start = datetime(2026, 4, 1) + timedelta(days=14 * n)
    end = start + timedelta(days=14)
    return Fold(start=start.strftime("%Y-%m-%d %H:%M"),
                end=end.strftime("%Y-%m-%d %H:%M"),
                base=b, cand=c, shared=shared,
                base_only=len(base_pnls) - shared,
                cand_only=len(cand_pnls) - shared,
                delta=c.pnl - b.pnl)


def _winning_folds() -> list[Fold]:
    """3 folds, candidate ahead in every one, profitable holdout."""
    return [
        _fold(0, [100, -200], [200, -100]),   # Δ +200
        _fold(1, [100, -200], [-50, 100]),    # Δ +150
        _fold(2, [100, -200], [300, -100]),   # Δ +300, holdout PF 3.0
    ]


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


class _AlwaysLongWider(_AlwaysLong):
    """Same entries, different exit offsets — a mutation that expresses."""

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0:
            broker.entry("L", OrderSide.LONG)
        else:
            broker.exit("X", "L", limit=bar.close + 50, stop=bar.close - 40)


def _synthetic_bars(days: int) -> list[Bar]:
    bars = []
    for i in range(days * 20):   # 20 × 15-min bars per day
        day, slot = divmod(i, 20)
        c = 20000 + i % 37 * 5
        bars.append(Bar(symbol="T",
                        dt=datetime(2026, 1, 1, 9, 0)
                        + timedelta(days=day, minutes=15 * slot),
                        open=c - 3, high=c + 10, low=c - 10, close=c,
                        volume=100, interval=900))
    return bars


# ── trade_key / split_trade_overlap ──

class TestTradeOverlap:
    def test_identical_trades_are_shared(self):
        a = [_t("2026-06-01 10:00", 100), _t("2026-06-02 10:00", -50)]
        b = [_t("2026-06-01 10:00", 100), _t("2026-06-02 10:00", -50)]
        assert split_trade_overlap(a, b) == (2, 0, 0)

    def test_different_pnl_diverges(self):
        a = [_t("2026-06-01 10:00", 100)]
        b = [_t("2026-06-01 10:00", 120)]
        assert split_trade_overlap(a, b) == (0, 1, 1)

    def test_same_time_different_bar_index_diverges(self):
        a = [_t("2026-06-01 10:00", 100, idx=0)]
        b = [_t("2026-06-01 10:00", 100, idx=7)]
        assert split_trade_overlap(a, b) == (0, 1, 1)

    def test_multiset_not_set_semantics(self):
        # The same trade taken twice counts twice — set() would report
        # a repetitive baseline as artificially divergent.
        a = [_t("2026-06-01 10:00", 100), _t("2026-06-01 10:00", 100)]
        b = [_t("2026-06-01 10:00", 100)]
        assert split_trade_overlap(a, b) == (1, 1, 0)

    def test_empty_sides(self):
        assert split_trade_overlap([], []) == (0, 0, 0)
        assert split_trade_overlap(None, [_t("2026-06-01 10:00", 5)]) == (0, 0, 1)

    def test_key_tolerates_missing_fields(self):
        class _Bare:
            pnl = 5
        assert trade_key(_Bare()) == ("", "", 0, 0, -1, -1)


# ── window_stats ──

class TestWindowStats:
    def test_empty(self):
        w = window_stats([])
        assert (w.n, w.pnl, w.profit_factor, w.max_drawdown) == (0, 0, 0.0, 0)

    def test_mixed(self):
        w = window_stats([_t("", 200), _t("", -100), _t("", 100)])
        assert w.n == 3 and w.pnl == 200 and w.wins == 2
        assert w.gross_win == 300 and w.gross_loss == 100
        assert w.profit_factor == 3.0

    def test_no_losses_is_inf(self):
        assert window_stats([_t("", 100)]).profit_factor == float("inf")

    def test_max_drawdown_is_peak_to_trough_points(self):
        # cum: 500, 200, -100, 100 → peak 500, trough -100 → dd 600
        w = window_stats([_t("", 500), _t("", -300), _t("", -300), _t("", 200)])
        assert w.max_drawdown == 600

    def test_all_losses_pf_zero(self):
        assert window_stats([_t("", -100), _t("", -50)]).profit_factor == 0.0


# ── analyze_folds ──

class TestAnalyzeFolds:
    def _base(self) -> list[Trade]:
        return [
            _t("2026-05-25 10:00", 100),    # fold 1
            _t("2026-06-01 10:00", -50),    # fold 1
            _t("2026-06-10 10:00", 200),    # fold 2
            _t("2026-06-20 10:00", -100),   # fold 3
            _t("2026-06-30 10:00", -100),   # fold 3
        ]

    def _cand(self) -> list[Trade]:
        return [
            _t("2026-05-25 10:00", 100),    # fold 1, identical to base
            _t("2026-06-02 10:00", 300),    # fold 1
            _t("2026-06-10 10:00", 200),    # fold 2, identical to base
            _t("2026-06-21 10:00", 400),    # fold 3
            _t("2026-06-30 10:00", -100),   # fold 3, identical to base
        ]

    def test_three_folds_chronological(self):
        folds = analyze_folds(self._base(), self._cand(), fold_days=14)
        assert len(folds) == 3
        assert folds[0].start < folds[1].start < folds[2].start
        # anchor rounds up to the midnight after the last exit so that
        # trade lands inside its own half-open window
        assert folds[-1].end == "2026-07-01 00:00"
        assert folds[-1].start == "2026-06-17 00:00"

    def test_bucketing_counts(self):
        folds = analyze_folds(self._base(), self._cand(), fold_days=14)
        assert [f.base.n for f in folds] == [2, 1, 2]
        assert [f.cand.n for f in folds] == [2, 1, 2]

    def test_deltas_and_overlap(self):
        folds = analyze_folds(self._base(), self._cand(), fold_days=14)
        assert [f.delta for f in folds] == [350, 0, 500]
        assert [f.shared for f in folds] == [1, 1, 1]
        assert folds[-1].base_only == 1 and folds[-1].cand_only == 1

    def test_holdout_is_last(self):
        folds = analyze_folds(self._base(), self._cand(), fold_days=14)
        holdout = folds[-1]
        assert holdout.base.pnl == -200      # 06-20 and 06-30
        assert holdout.cand.pnl == 300       # 06-21 and 06-30

    def test_undated_trades_ignored(self):
        base = self._base() + [_t("", 9999)]
        folds = analyze_folds(base, self._cand(), fold_days=14)
        assert sum(f.base.n for f in folds) == 5

    def test_max_folds_caps_the_walk_back(self):
        folds = analyze_folds(self._base(), self._cand(), fold_days=14,
                              max_folds=2)
        assert len(folds) == 2
        assert folds[-1].end == "2026-07-01 00:00"

    def test_explicit_span_end_used_verbatim(self):
        folds = analyze_folds(self._base(), self._cand(), fold_days=14,
                              span_end="2026-07-01 00:00")
        assert folds[-1].end == "2026-07-01 00:00"
        assert len(folds) == 3

    def test_no_dated_trades_returns_empty(self):
        assert analyze_folds([], []) == []
        assert analyze_folds([_t("", 100)], [_t("", 50)]) == []

    def test_unparseable_span_end_returns_empty(self):
        assert analyze_folds(self._base(), self._cand(),
                             span_end="not-a-date") == []

    def test_leading_empty_folds_dropped(self):
        # A ~12-week gap before the recent cluster: max_folds stops the
        # walk-back inside the gap, stranding empty oldest windows that
        # carry no evidence at all.
        base = [_t("2026-04-01 10:00", 100), _t("2026-06-25 10:00", 100)]
        cand = [_t("2026-04-01 10:00", 100), _t("2026-06-26 10:00", 50)]
        folds = analyze_folds(base, cand, fold_days=14, max_folds=3)
        assert len(folds) == 1                     # 2 empty leaders dropped
        assert folds[0].end == "2026-06-27 00:00"
        assert folds[0].base.n == 1 and folds[0].cand.n == 1

    def test_interior_empty_fold_kept(self):
        base = [_t("2026-05-05 10:00", 100), _t("2026-06-25 10:00", 100)]
        cand = [_t("2026-05-05 10:00", 100), _t("2026-06-25 10:00", 100)]
        folds = analyze_folds(base, cand, fold_days=14)
        assert len(folds) >= 3
        assert any(f.base.n == 0 and f.cand.n == 0 for f in folds[1:])


# ── decide_multifold_verdict ──

class TestDecideMultifoldVerdict:
    def test_pass_case(self):
        v = decide_multifold_verdict(_winning_folds())
        assert v.passed
        assert not v.used_criteria
        assert any("fold majority" in r and "3/3" in r for r in v.reasons)
        assert any("paired total" in r for r in v.reasons)

    def test_losing_holdout_fold_fails(self):
        folds = _winning_folds()
        # candidate loses in the most recent window; it still wins the
        # majority and the paired total, so ONLY the holdout floor bites
        folds[-1] = _fold(2, [100, -200], [-100, -200])
        v = decide_multifold_verdict(folds)
        assert not v.passed
        assert any("holdout fold PF" in r and "✗" in r for r in v.reasons)

    def test_no_trades_in_holdout_fold_fails(self):
        folds = _winning_folds()
        folds[-1] = _fold(2, [100, -200], [])
        v = decide_multifold_verdict(folds)
        assert not v.passed
        assert any("no trades in the holdout fold" in r for r in v.reasons)

    def test_no_op_mutation_fails(self):
        folds = [_fold(i, [100, -50], [100, -50], shared=2) for i in range(3)]
        v = decide_multifold_verdict(folds)
        assert not v.passed
        assert any("no-op" in r and "did not express" in r for r in v.reasons)

    def test_small_sample_warning_on_pass(self):
        v = decide_multifold_verdict(_winning_folds())
        assert v.passed
        assert any("PROVISIONAL" in r and "⚠" in r for r in v.reasons)

    def test_minority_of_folds_fails(self):
        folds = [
            _fold(0, [100], [-400]),    # Δ -500
            _fold(1, [100], [-400]),    # Δ -500
            _fold(2, [-100], [900, 200]),  # Δ +1200, holdout profitable
        ]
        v = decide_multifold_verdict(folds)
        assert not v.passed
        assert any("fold majority" in r and "1/3" in r for r in v.reasons)

    def test_negative_paired_total_fails(self):
        folds = [
            _fold(0, [100], [110]),      # Δ +10
            _fold(1, [100], [110]),      # Δ +10
            _fold(2, [1000], [100, 20]),  # Δ -880, holdout still PF INF
        ]
        v = decide_multifold_verdict(folds)
        assert not v.passed
        assert any("paired total" in r and "✗" in r for r in v.reasons)

    def test_no_folds_fails(self):
        v = decide_multifold_verdict([])
        assert not v.passed
        assert "no folds" in v.reasons[0]

    def test_candidate_never_traded_fails(self):
        folds = [_fold(i, [100, -50], []) for i in range(3)]
        v = decide_multifold_verdict(folds)
        assert not v.passed
        assert any("0 trades" in r for r in v.reasons)

    def test_criteria_applied_to_pooled_aggregate(self):
        # pooled candidate P&L = 200 + 50 + 200 = 450 < 10_000
        v = decide_multifold_verdict(_winning_folds(),
                                     {"total_pnl_min": 10000})
        assert not v.passed
        assert v.used_criteria
        assert any("pooled P&L" in r for r in v.reasons)

    def test_criteria_that_hold_still_pass(self):
        v = decide_multifold_verdict(_winning_folds(),
                                     {"profit_factor_min": 1.2,
                                      "win_rate_min": 0.4})
        assert v.passed
        assert v.used_criteria
        assert any("pooled PF" in r for r in v.reasons)
        assert any("pooled WR" in r for r in v.reasons)

    def test_dd_criterion_skipped_with_explanation(self):
        v = decide_multifold_verdict(_winning_folds(),
                                     {"max_drawdown_pct_max": 1})
        assert v.passed  # the impossible dd threshold does NOT gate here
        assert any("window-length" in r for r in v.reasons)


# ── run_multifold_validation ──

class TestRunMultifoldValidation:
    def test_smoke_over_60_days(self):
        bars = _synthetic_bars(60)
        folds, base_ab, cand_ab = run_multifold_validation(
            _AlwaysLong, _AlwaysLongWider, bars, 200, fold_days=14)
        assert base_ab.error == "" and cand_ab.error == ""
        assert base_ab.result.metrics.total_trades > 0
        assert cand_ab.result.metrics.total_trades > 0
        assert len(folds) >= 3
        assert folds[0].start < folds[-1].start
        assert sum(f.base.n for f in folds) > 0
        assert sum(f.cand.n for f in folds) > 0

    def test_broken_side_returns_no_folds(self):
        class _Broken(BacktestStrategy):
            kline_type = 0
            kline_minute = 15

            def __init__(self):
                raise RuntimeError("boom")

            def on_bar(self, bar, data_store, broker):
                pass

        folds, base_ab, cand_ab = run_multifold_validation(
            _AlwaysLong, _Broken, _synthetic_bars(30), 200)
        assert folds == []
        assert cand_ab.error != ""

    def test_verdict_runs_on_real_folds(self):
        bars = _synthetic_bars(60)
        folds, _, _ = run_multifold_validation(
            _AlwaysLong, _AlwaysLongWider, bars, 200, fold_days=14)
        v = decide_multifold_verdict(folds)
        assert isinstance(v.passed, bool)
        assert v.reasons


# ── format_multifold_block ──

class TestFormatMultifoldBlock:
    def test_pass_block(self):
        folds = _winning_folds()
        v = decide_multifold_verdict(folds)
        block = format_multifold_block(folds, v, "Capital API 60d",
                                       saved_as="AI: FooEvo1")
        assert "multi-fold" in block
        assert "PASS" in block
        assert "AI: FooEvo1" in block
        assert "deploy manually" in block
        assert block.count("\n  #") == 3
        assert "Δ" in block and "shared" in block

    def test_fail_block(self):
        folds = [_fold(i, [100, -50], [100, -50], shared=2) for i in range(3)]
        v = decide_multifold_verdict(folds)
        block = format_multifold_block(folds, v, "Capital API 60d")
        assert "FAIL" in block
        assert "nothing was saved" in block
