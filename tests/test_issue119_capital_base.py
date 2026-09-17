"""Issue #119 — the evolution drawdown percentage had no capital base.

`compute_fitness_from_trades` used to call ``calculate_metrics(...,
initial_balance=0)``, and `BacktestEngine` never passed one at all. With a
zero base, ``max_drawdown_pct = max_dd / peak * 100`` is measured against
the *peak of cumulative P&L*, not against capital:

* a window whose cumulative P&L never goes positive has ``peak == 0``, so
  ``max_drawdown_pct`` is reported as 0.00% — the BEST possible value, and
  the fitness drawdown sub-score becomes a perfect 1.0 for a strategy that
  did nothing but sink;
* a window with a tiny peak reports a dd% in the hundreds, saturating the
  sub-score at 0.0 purely from the size of the peak.

The fix seeds the metric with ``evolution.capital_base_twd`` (default
100 000 TWD) so the percentage is a real percentage of capital.

UNITS: ``Trade.pnl`` is ``points * qty * point_value`` and the evolution
paths run with the TXF point value (200), so both the trade P&L and the
equity curve are already TWD. The capital base is TWD too — no conversion.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime, timedelta

import pytest

from src.backtest.broker import Trade, OrderSide
from src.backtest.engine import BacktestEngine
from src.backtest.metrics import calculate_metrics
from src.backtest.report import format_report
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext
from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.evolution.fitness import (
    DEFAULT_CAPITAL_BASE_TWD,
    DEFAULT_WEIGHTS,
    MIN_TRADES,
    compute_fitness_from_trades,
)


# Weights that make the composite EQUAL the drawdown sub-score, so the
# assertions below read the sub-score directly instead of guessing at it.
DRAWDOWN_ONLY = {k: (1.0 if k == "drawdown" else 0.0) for k in DEFAULT_WEIGHTS}


def _trade(pnl: int, i: int) -> Trade:
    """A Trade carrying the given TWD P&L, dated so the monthly buckets
    the consistency term uses are populated (irrelevant under
    DRAWDOWN_ONLY weights, but keeps the object realistic)."""
    dt = datetime(2025, 1, 1, 9, 0) + timedelta(days=i)
    return Trade(
        tag="L", side=OrderSide.LONG, qty=1,
        entry_price=20000, exit_price=20000 + pnl // 200,
        entry_bar_index=i, exit_bar_index=i + 1,
        pnl=pnl,
        entry_dt=dt.strftime("%Y-%m-%d %H:%M:%S"),
        exit_dt=(dt + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
    )


class TestFitnessDrawdownHasACapitalBase:
    def test_sinking_equity_does_not_score_a_perfect_drawdown(self):
        """30 straight losers, cumulative P&L never positive.

        Pre-fix: peak == 0 → max_drawdown_pct == 0.00 → drawdown
        sub-score 1.0, i.e. the worst possible window scored perfectly.
        Post-fix: dd is 30 000 of a 100 000 base → 30.0%, which is the
        normalizer's cap, so the sub-score is 0.0.
        """
        trades = [_trade(-1000, i) for i in range(MIN_TRADES)]
        fit = compute_fitness_from_trades(trades, weights=DRAWDOWN_ONLY)

        assert fit.gated is False
        assert fit.max_drawdown_pct == pytest.approx(30.0)
        assert fit.composite < 1.0
        assert fit.composite == pytest.approx(0.0)

    def test_small_peak_does_not_zero_the_drawdown_subscore(self):
        """One +2 000 trade then 30 × -100: a barely-profitable window.

        Pre-fix: peak == 2 000 and dd == 3 000 → 150% → sub-score 0.0,
        an artefact of the tiny peak rather than of real risk.
        Post-fix: 3 000 against a 102 000 peak → 2.94%.
        """
        trades = [_trade(2000, 0)]
        trades += [_trade(-100, i) for i in range(1, 31)]
        fit = compute_fitness_from_trades(trades, weights=DRAWDOWN_ONLY)

        assert fit.gated is False
        assert fit.max_drawdown_pct == pytest.approx(3000 / 102000 * 100,
                                                    rel=1e-6)
        assert fit.composite > 0.8

    def test_sinking_scores_worse_than_barely_profitable(self):
        """The ordering the composite is supposed to express — inverted
        pre-fix (1.0 for the sinker, 0.0 for the survivor)."""
        sinking = [_trade(-1000, i) for i in range(MIN_TRADES)]
        surviving = [_trade(2000, 0)] + [_trade(-100, i) for i in range(1, 31)]
        fit_sink = compute_fitness_from_trades(sinking, weights=DRAWDOWN_ONLY)
        fit_ok = compute_fitness_from_trades(surviving, weights=DRAWDOWN_ONLY)
        assert fit_ok.composite > fit_sink.composite

    def test_explicit_capital_base_overrides_the_default(self):
        trades = [_trade(-1000, i) for i in range(MIN_TRADES)]
        fit = compute_fitness_from_trades(trades, capital_base=1_000_000)
        # 30 000 of 1 000 000 = 3%.
        assert fit.max_drawdown_pct == pytest.approx(3.0)

    def test_default_constant_is_100k(self):
        assert DEFAULT_CAPITAL_BASE_TWD == 100_000


# ── BacktestEngine ────────────────────────────────────────────────────


class _OneLoser(BacktestStrategy):
    """Enters long on the first bar only and rides it to the force close."""

    def __init__(self):
        self._done = False

    def required_bars(self) -> int:
        return 1

    def on_bar(self, bar: Bar, data_store: DataStore,
               broker: BrokerContext) -> None:
        if not self._done and broker.position_size == 0:
            broker.entry("Long", OrderSide.LONG)
            self._done = True


def _bars() -> list[Bar]:
    base = datetime(2025, 1, 1, 8, 45)
    spec = [(20000, 20010, 19990, 20000), (19960, 19970, 19940, 19950)]
    return [
        Bar(symbol="TX00", dt=base + timedelta(hours=4 * i),
            open=o, high=h, low=lo, close=c, volume=100, interval=14400)
        for i, (o, h, lo, c) in enumerate(spec)
    ]


class TestBacktestEngineInitialBalance:
    def test_default_keeps_the_zero_base_behaviour(self):
        """Every non-evolution backtest must be byte-identical to before:
        a losing-only run still reports 0.00% (peak == 0)."""
        result = BacktestEngine(_OneLoser(), point_value=1).run(_bars())
        assert result.metrics.total_pnl == -50
        assert result.metrics.max_drawdown == 50
        assert result.metrics.max_drawdown_pct == 0.0
        assert result.metrics.initial_balance == 0

    def test_initial_balance_makes_dd_pct_a_real_percentage(self):
        """Same run, 100 000 base: 50 TWD of drawdown against a 100 000
        peak is 0.05%."""
        result = BacktestEngine(_OneLoser(), point_value=1,
                                initial_balance=100_000).run(_bars())
        assert result.metrics.max_drawdown == 50
        assert result.metrics.max_drawdown_pct == pytest.approx(0.05)
        assert result.metrics.initial_balance == 100_000
        assert result.metrics.final_balance == 99_950


# ── Settings loader ───────────────────────────────────────────────────


def _cfg(tmp_path, monkeypatch, yaml_text: str) -> dict:
    import run_backtest as rb
    (tmp_path / "settings.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    return rb._load_settings()


class TestCapitalBaseSetting:
    def test_missing_key_defaults_to_100k(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path, monkeypatch, "evolution:\n  cadence: weekly\n")
        assert cfg["evolution_capital_base_twd"] == 100_000

    def test_missing_evolution_section_defaults_to_100k(self, tmp_path,
                                                        monkeypatch):
        cfg = _cfg(tmp_path, monkeypatch, "trading:\n  allow_live_override: false\n")
        assert cfg["evolution_capital_base_twd"] == 100_000

    def test_explicit_value_is_read(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path, monkeypatch,
                   "evolution:\n  capital_base_twd: 500000\n")
        assert cfg["evolution_capital_base_twd"] == 500_000

    @pytest.mark.parametrize("bad", ["0", "-1", '""', "null", "abc"])
    def test_non_positive_or_junk_falls_back_to_100k(self, tmp_path,
                                                     monkeypatch, bad):
        """A zero base would reinstate exactly the bug this fixes."""
        cfg = _cfg(tmp_path, monkeypatch,
                   f"evolution:\n  capital_base_twd: {bad}\n")
        assert cfg["evolution_capital_base_twd"] == 100_000


# ── Design-window AI report leftover (#119/#120) ──────────────────────
#
# Fitness already seeds capital_base via ``_evolution_capital_base()``.
# The design-window ``format_report`` in ``_bot_evolution`` still called
# ``calculate_metrics(..., initial_balance=0)``, so the AI still saw the
# zero-peak MaxDD% sentinel that #119/#120 removed from the composite.


def _design_equity(trades):
    d_eq, d_cum = [], 0
    for t in trades:
        d_cum += t.pnl
        d_eq.append(d_cum)
    return d_eq


class TestDesignReportUsesCapitalBase:
    """These two glue tests FAIL against the pre-fix ``_bot_evolution``
    (``initial_balance=0``). The report-number test documents what the
    AI must see once the wire-in lands.
    """

    def test_bot_evolution_design_report_does_not_pass_zero_base(self):
        """FAILS pre-fix: the leftover is literally ``initial_balance=0``."""
        import run_backtest as rb
        src = textwrap.dedent(
            inspect.getsource(rb.BacktestApp._bot_evolution))
        tree = ast.parse(src)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "calculate_metrics"
        ]
        assert calls, (
            "_bot_evolution must still score the design-window report")
        for call in calls:
            kw = {k.arg: k.value for k in call.keywords}
            assert "initial_balance" in kw
            val = kw["initial_balance"]
            assert not (isinstance(val, ast.Constant) and val.value == 0), (
                "issue #119 leftover: design-window format_report must not "
                "seed calculate_metrics with initial_balance=0")

    def test_bot_evolution_design_report_uses_evolution_capital_base(self):
        """FAILS pre-fix: ``_evolution_capital_base()`` is not the
        ``initial_balance`` of the design-window ``calculate_metrics``."""
        import run_backtest as rb
        src = inspect.getsource(rb.BacktestApp._bot_evolution)
        assert "format_report" in src
        assert "initial_balance=self._evolution_capital_base()" in src, (
            "design-window calculate_metrics / format_report must seed "
            "initial_balance from _evolution_capital_base() so MaxDD% "
            "matches the fitness composite")
        assert (
            "calculate_metrics(design_known, d_eq, initial_balance=0)"
            not in src)

    def test_sinking_design_window_report_shows_capital_based_dd(self):
        """30 × -1k TWD against a 100k base is 30.00%, not the 0.00%
        sentinel ``initial_balance=0`` prints — the number the AI reads
        in the design-window report body."""
        trades = [_trade(-1000, i) for i in range(MIN_TRADES)]
        d_eq = _design_equity(trades)
        m0 = calculate_metrics(trades, d_eq, initial_balance=0)
        m = calculate_metrics(trades, d_eq, initial_balance=100_000)
        report = format_report(
            "X — 設計窗 design window (holdout excluded)", m)
        assert m0.max_drawdown_pct == 0.0
        assert m.max_drawdown_pct == pytest.approx(30.0)
        assert m.initial_balance == 100_000
        assert m.final_balance == 70_000
        assert "100,000" in report
        assert "30.00%" in report
