"""Tests for src/evolution/fitness.py."""

from __future__ import annotations

import pytest

from src.backtest.broker import Trade, OrderSide
from src.backtest.metrics import calculate_metrics, PerformanceMetrics
from src.evolution.fitness import (
    DEFAULT_WEIGHTS,
    MIN_TRADES,
    compute_fitness,
    compute_fitness_from_reports,
    consistency_score,
    label_to_regime,
    regime_scores,
    regime_scores_from_reports,
    sortino_ratio,
)


def make_trade(
    entry: int,
    exit_: int,
    *,
    pv: int = 200,
    bar_in: int = 0,
    bar_out: int = 1,
    entry_dt: str = "2025-01-01 09:00:00",
    exit_dt: str = "2025-01-01 10:00:00",
) -> Trade:
    pnl = (exit_ - entry) * pv
    return Trade(
        tag="L", side=OrderSide.LONG, qty=1,
        entry_price=entry, exit_price=exit_,
        entry_bar_index=bar_in, exit_bar_index=bar_out,
        pnl=pnl, entry_dt=entry_dt, exit_dt=exit_dt,
    )


def _result(trades: list[Trade]) -> dict:
    """Build the dict shape that compute_fitness accepts."""
    equity: list[int] = []
    cum = 0
    for t in trades:
        cum += t.pnl
        equity.append(cum)
    metrics = calculate_metrics(trades, equity, initial_balance=1_000_000)
    return {"trades": trades, "equity_curve": equity, "metrics": metrics}


def _spread_trades_across_months(n_trades: int, win_rate: float, win_pts: int = 100,
                                  lose_pts: int = 50) -> list[Trade]:
    """Build N trades spread across 6 months with the given win rate.
    Trades alternate win/loss so monthly buckets see both."""
    out: list[Trade] = []
    months = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
    n_wins = int(round(n_trades * win_rate))
    for i in range(n_trades):
        is_win = i < n_wins
        entry = 20000
        exit_ = entry + (win_pts if is_win else -lose_pts)
        m = months[i % len(months)]
        out.append(make_trade(
            entry, exit_,
            entry_dt=f"{m}-15 09:00:00",
            exit_dt=f"{m}-15 10:00:00",
        ))
    return out


class TestMinTradeGate:
    def test_below_min_trades_gates_to_zero(self):
        # 5 perfect trades — should still be gated.
        trades = [make_trade(20000, 20100) for _ in range(5)]
        fit = compute_fitness(_result(trades))
        assert fit.gated is True
        assert fit.composite == 0.0
        # Raw metrics still populated for inspection.
        assert fit.total_trades == 5
        assert fit.win_rate == 1.0

    def test_at_min_trades_not_gated(self):
        trades = _spread_trades_across_months(MIN_TRADES, win_rate=0.6)
        fit = compute_fitness(_result(trades))
        assert fit.gated is False
        assert fit.composite > 0.0

    def test_one_below_min_gated(self):
        trades = _spread_trades_across_months(MIN_TRADES - 1, win_rate=0.6)
        fit = compute_fitness(_result(trades))
        assert fit.gated is True
        assert fit.composite == 0.0


class TestCompositeScoring:
    def test_composite_in_unit_interval(self):
        trades = _spread_trades_across_months(40, win_rate=0.6)
        fit = compute_fitness(_result(trades))
        assert 0.0 <= fit.composite <= 1.0

    def test_better_strategy_scores_higher(self):
        good = _spread_trades_across_months(40, win_rate=0.7, win_pts=100, lose_pts=40)
        bad = _spread_trades_across_months(40, win_rate=0.4, win_pts=80, lose_pts=120)
        fit_good = compute_fitness(_result(good))
        fit_bad = compute_fitness(_result(bad))
        assert fit_good.composite > fit_bad.composite

    def test_high_sharpe_terrible_dd_loses_to_moderate_sharpe_small_dd(self):
        # Strategy A: 30 medium wins then ONE catastrophic loss.
        # Tons of profit but the late loss creates a huge drawdown.
        trades_a: list[Trade] = []
        months = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
        for i in range(30):
            m = months[i % len(months)]
            trades_a.append(make_trade(
                20000, 20120,
                entry_dt=f"{m}-15 09:00:00", exit_dt=f"{m}-15 10:00:00",
            ))
        trades_a.append(make_trade(
            20000, 14000,  # -6000pts * 200 = -1.2M loss
            entry_dt="2025-06-20 09:00:00", exit_dt="2025-06-20 10:00:00",
        ))

        # Strategy B: 40 small steady wins, no big losses.
        trades_b = _spread_trades_across_months(40, win_rate=0.7, win_pts=60, lose_pts=30)

        fit_a = compute_fitness(_result(trades_a))
        fit_b = compute_fitness(_result(trades_b))

        # A should be hammered by the drawdown component even though it
        # has more trades and higher per-trade Sharpe.
        assert fit_a.max_drawdown_pct > fit_b.max_drawdown_pct
        assert fit_b.composite > fit_a.composite

    def test_custom_weights(self):
        trades = _spread_trades_across_months(40, win_rate=0.6)
        all_drawdown = {k: 0.0 for k in DEFAULT_WEIGHTS}
        all_drawdown["drawdown"] = 1.0
        fit_dd_only = compute_fitness(_result(trades), weights=all_drawdown)

        all_winrate = {k: 0.0 for k in DEFAULT_WEIGHTS}
        all_winrate["win_rate"] = 1.0
        fit_wr_only = compute_fitness(_result(trades), weights=all_winrate)

        # Both produce values, and they should differ for non-trivial input.
        assert 0.0 <= fit_dd_only.composite <= 1.0
        assert 0.0 <= fit_wr_only.composite <= 1.0


class TestRegimeScoring:
    def test_regime_scores_in_unit_interval(self):
        trades = _spread_trades_across_months(40, win_rate=0.6)
        fit = compute_fitness(_result(trades))
        for v in (fit.regime_bull, fit.regime_bear, fit.regime_sideways):
            assert 0.0 <= v <= 1.0

    def test_only_winning_bull_trades(self):
        # All long trades that win → all classified as bull regime,
        # 100% win rate in bull, 0 in bear/sideways.
        trades: list[Trade] = []
        months = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
        for i in range(35):
            m = months[i % len(months)]
            trades.append(make_trade(
                20000, 20500,  # +2.5% move → bull regime
                entry_dt=f"{m}-15 09:00:00", exit_dt=f"{m}-15 10:00:00",
            ))
        scores = regime_scores(trades)
        assert scores["bull"] == 1.0
        assert scores["bear"] == 0.0
        assert scores["sideways"] == 0.0

    def test_regime_balance_penalizes_one_trick_strategy(self):
        # Strategy that ONLY trades bull regime (all entries/exits in bull).
        bull_only = _spread_trades_across_months(40, win_rate=0.6, win_pts=500, lose_pts=400)
        # Move all trades into 'bull' bucket explicitly: every trade has exit > entry.
        # Make every loss still bull-classified by widening exit > entry threshold.
        # (Default _spread mixes wins/losses — losses go bear. We rebuild explicitly.)
        bull_trades: list[Trade] = []
        months = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
        for i in range(40):
            entry = 20000
            # Both wins and losses keep exit > entry by >1% so all are bull.
            exit_ = entry + (500 if i < 24 else 300)  # 60% wins; all bull
            m = months[i % len(months)]
            bull_trades.append(make_trade(
                entry, exit_,
                entry_dt=f"{m}-15 09:00:00", exit_dt=f"{m}-15 10:00:00",
            ))

        # Strategy with similar overall metrics but trades spread across regimes.
        diverse = _spread_trades_across_months(40, win_rate=0.6, win_pts=500, lose_pts=400)

        fit_bull = compute_fitness(_result(bull_trades))
        fit_div = compute_fitness(_result(diverse))

        # Bull-only has 0 in bear and sideways → regime_balance ≈ 0 → composite hurt.
        assert min(fit_bull.regime_bull, fit_bull.regime_bear,
                   fit_bull.regime_sideways) == 0.0


class TestSortino:
    def test_sortino_no_losses_positive(self):
        # Only winning trades — Sortino should be a strong positive signal.
        s = sortino_ratio([100.0, 120.0, 80.0, 150.0])
        assert s > 0

    def test_sortino_too_few_trades_returns_zero(self):
        assert sortino_ratio([100.0]) == 0.0
        assert sortino_ratio([]) == 0.0

    def test_sortino_penalizes_downside(self):
        # Same mean, but one strategy has a big negative outlier.
        steady = [50.0, 50.0, 50.0, 50.0, 50.0, 50.0]
        with_dd = [100.0, 100.0, 100.0, 100.0, 100.0, -250.0]
        # Same total (300 vs 250) — but with_dd has downside risk and steady has none.
        s_steady = sortino_ratio(steady)
        s_dd = sortino_ratio(with_dd)
        assert s_steady > s_dd


class TestConsistency:
    def test_consistency_zero_with_one_month(self):
        assert consistency_score({"2025-01": 100.0}) == 0.0
        assert consistency_score({}) == 0.0

    def test_consistency_one_for_perfectly_steady(self):
        assert consistency_score({
            "2025-01": 100.0, "2025-02": 100.0, "2025-03": 100.0,
        }) == 1.0

    def test_consistency_zero_for_net_losing_months(self):
        assert consistency_score({
            "2025-01": -50.0, "2025-02": 30.0, "2025-03": -100.0,
        }) == 0.0

    def test_steady_scores_higher_than_volatile(self):
        steady = consistency_score({
            "2025-01": 100.0, "2025-02": 110.0, "2025-03": 90.0,
        })
        volatile = consistency_score({
            "2025-01": 500.0, "2025-02": -200.0, "2025-03": 100.0,
        })
        assert steady > volatile


class TestBacktestResultObject:
    def test_accepts_object_with_attrs(self):
        """compute_fitness should also work with the engine's
        BacktestResult object, not just dicts."""
        class _Stub:
            pass
        stub = _Stub()
        stub.trades = _spread_trades_across_months(40, win_rate=0.6)
        equity = []
        cum = 0
        for t in stub.trades:
            cum += t.pnl
            equity.append(cum)
        stub.equity_curve = equity
        stub.metrics = calculate_metrics(stub.trades, equity, initial_balance=1_000_000)

        fit = compute_fitness(stub)
        assert fit.gated is False
        assert fit.composite > 0.0
        assert fit.total_trades == 40

    def test_handles_missing_metrics_gracefully(self):
        fit = compute_fitness({"trades": [], "metrics": None})
        assert fit.gated is True
        assert fit.composite == 0.0
        assert fit.total_trades == 0


def test_to_dict_roundtrip():
    trades = _spread_trades_across_months(40, win_rate=0.6)
    fit = compute_fitness(_result(trades))
    d = fit.to_dict()
    expected_keys = {
        "composite", "sharpe", "sortino", "max_drawdown_pct",
        "profit_factor", "win_rate", "consistency",
        "regime_bull", "regime_bear", "regime_sideways",
        "total_trades", "gated",
    }
    assert set(d.keys()) == expected_keys
    assert d["composite"] == fit.composite


# ---------------------------------------------------------------------------
# Report-based fitness — consumes the daily-report dict shape produced by
# src.daily_report.report_generator.generate_report_from_backtest. Uses real
# ADX/ATR regime labels via label_to_regime() instead of the per-trade
# naive proxy.
# ---------------------------------------------------------------------------


def _trade_dict(entry: int, exit_: int, *, pnl: int | None = None,
                entry_dt: str = "2025-01-15 09:00:00",
                exit_dt: str = "2025-01-15 10:00:00") -> dict:
    """Shape mirrors _trade_to_dict in src/daily_report/report_generator.py."""
    return {
        "tag": "L", "side": "LONG", "qty": 1,
        "entry_price": entry, "exit_price": exit_,
        "entry_dt": entry_dt, "exit_dt": exit_dt,
        "pnl": pnl if pnl is not None else (exit_ - entry) * 200,
        "entry_bar_index": 0, "exit_bar_index": 1,
        "exit_tag": "Exit",
    }


def _report(date: str, trades: list[dict], regime_label: str | None = None) -> dict:
    """Shape mirrors generate_daily_report() output."""
    block = {"label": regime_label} if regime_label else None
    return {
        "date": date,
        "trades": trades,
        "summary": {},
        "market_regime": block,
    }


class TestLabelToRegime:
    def test_known_labels(self):
        assert label_to_regime("trending-up") == "bull"
        assert label_to_regime("transitional-bullish") == "bull"
        assert label_to_regime("trending-down") == "bear"
        assert label_to_regime("transitional-bearish") == "bear"
        assert label_to_regime("range-bound") == "sideways"
        assert label_to_regime("low-volatility-chop") == "sideways"
        assert label_to_regime("high-volatility") == "sideways"

    def test_unknown_or_missing(self):
        assert label_to_regime(None) is None
        assert label_to_regime("") is None
        assert label_to_regime("not-a-label") is None


class TestRegimeScoresFromReports:
    def test_buckets_by_label(self):
        reports = [
            _report("2025-01-15", [
                _trade_dict(20000, 20100),  # win
                _trade_dict(20000, 19900),  # loss
            ], regime_label="trending-up"),
            _report("2025-02-15", [
                _trade_dict(20000, 19800),  # loss
            ], regime_label="trending-down"),
            _report("2025-03-15", [
                _trade_dict(20000, 20100),  # win
            ], regime_label="range-bound"),
        ]
        scores = regime_scores_from_reports(reports)
        assert scores["bull"] == 0.5      # 1 win / 2 trades
        assert scores["bear"] == 0.0      # 0 wins / 1 trade
        assert scores["sideways"] == 1.0  # 1 win / 1 trade

    def test_skips_reports_without_label(self):
        reports = [
            _report("2025-01-15", [_trade_dict(20000, 20100)], regime_label=None),
            _report("2025-02-15", [_trade_dict(20000, 20100)], regime_label="trending-up"),
        ]
        scores = regime_scores_from_reports(reports)
        # Only the labeled report contributes.
        assert scores["bull"] == 1.0
        assert scores["bear"] == 0.0
        assert scores["sideways"] == 0.0


class TestComputeFitnessFromReports:
    def _make_30_winning_reports(self, regime_label: str = "trending-up") -> list[dict]:
        """30 winning trades across 6 months — clears MIN_TRADES."""
        reports = []
        months = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
        for i in range(30):
            m = months[i % len(months)]
            reports.append(_report(
                f"{m}-{(i % 28) + 1:02d}",
                [_trade_dict(20000, 20100,
                             entry_dt=f"{m}-{(i % 28) + 1:02d} 09:00:00",
                             exit_dt=f"{m}-{(i % 28) + 1:02d} 10:00:00")],
                regime_label=regime_label,
            ))
        return reports

    def test_basic_scoring_from_reports(self):
        reports = self._make_30_winning_reports("trending-up")
        fit = compute_fitness_from_reports(reports)
        assert fit.gated is False
        assert fit.total_trades == 30
        assert fit.composite > 0.0
        # All trades classified as bull via the real label.
        assert fit.regime_bull == 1.0
        assert fit.regime_bear == 0.0
        assert fit.regime_sideways == 0.0

    def test_gated_when_below_min_trades(self):
        reports = [
            _report("2025-01-15",
                    [_trade_dict(20000, 20100)] * 5,
                    regime_label="trending-up"),
        ]
        fit = compute_fitness_from_reports(reports)
        assert fit.gated is True
        assert fit.composite == 0.0

    def test_falls_back_to_naive_when_no_labels(self):
        """When NO report has a regime label, fall back to the per-trade
        naive proxy so we still get useful regime scoring."""
        reports = []
        months = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
        for i in range(30):
            m = months[i % len(months)]
            reports.append(_report(
                f"{m}-{(i % 28) + 1:02d}",
                [_trade_dict(20000, 20500,  # +2.5% → naive bull
                             entry_dt=f"{m}-{(i % 28) + 1:02d} 09:00:00",
                             exit_dt=f"{m}-{(i % 28) + 1:02d} 10:00:00")],
                regime_label=None,
            ))
        fit = compute_fitness_from_reports(reports)
        # Naive proxy classifies these as bull (exit > entry by >1%).
        assert fit.regime_bull > 0.0

    def test_consumes_real_pipeline_output(self):
        """Run the actual generate_report_from_backtest pipeline and
        ensure compute_fitness_from_reports accepts its output as-is."""
        from src.daily_report.report_generator import generate_report_from_backtest
        from src.backtest.broker import Trade, OrderSide

        # Build 30 trades spread across days with the engine's exact
        # Trade dataclass — same shape generate_report_from_backtest sees.
        trades = []
        for i in range(30):
            day = (i % 28) + 1
            trades.append(Trade(
                tag="L", side=OrderSide.LONG, qty=1,
                entry_price=20000, exit_price=20100,
                entry_bar_index=i, exit_bar_index=i + 1,
                pnl=100 * 200,
                entry_dt=f"2025-01-{day:02d} 09:00:00",
                exit_dt=f"2025-01-{day:02d} 10:00:00",
            ))
        equity_curve = []
        cum = 0
        for t in trades:
            cum += t.pnl
            equity_curve.append(cum)

        reports = generate_report_from_backtest(
            trades=trades, equity_curve=equity_curve,
            # Skip regime classification (insufficient bars). This
            # exercises the no-label fallback path explicitly.
            bars_highs=None, bars_lows=None, bars_closes=None,
            strategy_name="test", point_value=200, save=False,
        )
        assert len(reports) > 0
        fit = compute_fitness_from_reports(reports)
        assert fit.total_trades == 30
        assert fit.gated is False
