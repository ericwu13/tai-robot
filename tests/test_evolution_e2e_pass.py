"""Realistic evolution e2e PASS: plan → codegen stub → bars → deep AND-chain.

The weekly-review / evo-debug skills keep asking whether any test pins a
realistic PASS through ``run_deep_validation`` + ``decide_deep_verdict``.
Existing tests either hand-build ``DeepResult`` from synthetic PnL lists
(so they never run the engine, fitness, or Monte Carlo) or smoke
``run_deep_validation`` without asserting the verdict.

This fixture stubs the AI (plan text + a fenced candidate source) and
then runs the same validation path the live pipeline uses after codegen:

    parse_plan_directives → extract_python_code → load_strategy_from_source
    → check_candidate_name → run_deep_validation (train/test + MC + fitness)
    → decide_deep_verdict → format_deep_verdict_block

A PASS here means the AND-chain is reachable. It does not relax any
gate, touch watermark / kwargs-MC / design-report balance, or send
orders. Fail-path coverage stays in ``test_evolution_pipeline.py`` and
``test_evolution_issue114.py``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

pytest.importorskip("httpx")  # MC lives in evaluator.py which imports httpx

from src.ai.code_sandbox import extract_python_code, load_strategy_from_source
from src.evolution.fitness import DEFAULT_CAPITAL_BASE_TWD
from src.evolution.pipeline import (
    ABS_PF_FLOOR,
    MIN_EXPRESSED_TRADES,
    check_candidate_name,
    decide_deep_verdict,
    format_deep_verdict_block,
    next_candidate_name,
    parse_plan_directives,
    run_deep_validation,
)
from src.market_data.models import Bar

# Match the live glue in run_backtest.py (DEEP_TRAIN_DAYS / holdout_days).
TRAIN_DAYS = 90
TEST_DAYS = 14
POINT_VALUE = 200
BASE_CLASS = "TrendCapture"
CANDIDATE_CLASS = "TrendCaptureEvo1"


# ── Fixture: plan + codegen stub + bars ─────────────────────────────────

_BASE_SOURCE = f'''
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import OrderSide


class {BASE_CLASS}(BacktestStrategy):
    kline_type = 0
    kline_minute = 15

    def __init__(self, take_profit=40, stop_loss=80):
        self.take_profit = int(take_profit)
        self.stop_loss = int(stop_loss)
        self._entry = 0

    def required_bars(self):
        return 1

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0:
            broker.entry("L", OrderSide.LONG)
            self._entry = bar.close
        broker.exit(
            "X", "L",
            limit=self._entry + self.take_profit,
            stop=self._entry - self.stop_loss,
        )
'''

_CANDIDATE_SOURCE = f'''
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import OrderSide


class {CANDIDATE_CLASS}(BacktestStrategy):
    kline_type = 0
    kline_minute = 15

    def __init__(self, take_profit=80, stop_loss=50):
        self.take_profit = int(take_profit)
        self.stop_loss = int(stop_loss)
        self._entry = 0

    def required_bars(self):
        return 1

    def on_bar(self, bar, data_store, broker):
        if broker.position_size == 0:
            broker.entry("L", OrderSide.LONG)
            self._entry = bar.close
        broker.exit(
            "X", "L",
            limit=self._entry + self.take_profit,
            stop=self._entry - self.stop_loss,
        )
'''

# Realistic plan: one concrete change + the machine-readable directives
# block the live prompt requires. Criteria are the production-shaped set
# (dd% / PF / WR / P&L) — generous enough to be reachable, tight enough
# that a broken candidate would still fail them.
_PLAN_TEXT = f"""
Review of the last design window: {BASE_CLASS} leaves money on the table
on trending days (take-profit 40) and gives back too much on reversals
(stop 80). Propose ONE change: raise take-profit to 80 and tighten the
stop to 50. Keep entries identical.

```json
{{"action": "change", "max_drawdown_pct_max": 35, "profit_factor_min": 1.2, "win_rate_min": 0.4, "total_pnl_min": 0}}
```
"""

# Codegen is stubbed: a fenced response with trailing markdown notes of
# the kind extract_python_code is supposed to strip (issue #114 family).
_CODEGEN_RESPONSE = f"""Here is the evolved strategy.

```python
{_CANDIDATE_SOURCE}
```

### **Notes**
- take_profit 40 → 80, stop_loss 80 → 50; everything else unchanged.
- Class renamed to `{CANDIDATE_CLASS}` as required.
"""


def _trending_bars(n_days: int = TRAIN_DAYS + TEST_DAYS, bars_per_day: int = 20) -> list[Bar]:
    """Native 15-min bars with a rising drift plus periodic crash bars.

    Rising bars have enough high-range for both take-profits to fill;
    crash bars gap through both stops so profit-factor is finite (the
    design-window PF-ratio gate is skipped when PF is inf). Deterministic.
    """
    bars: list[Bar] = []
    price = 20000
    start = datetime(2026, 1, 5, 9, 0)
    for i in range(n_days * bars_per_day):
        day, slot = divmod(i, bars_per_day)
        if i > 0 and i % 25 == 0:
            o = price - 90
            c = o - 8
            high, low = o + 8, c - 15
            price = c
        else:
            o = price
            c = price + 10
            high, low = o + 160, o - 8
            price = c
        bars.append(Bar(
            symbol="TXF",
            dt=start + timedelta(days=day, minutes=15 * slot),
            open=int(o), high=int(high), low=int(low), close=int(c),
            volume=100, interval=900,
        ))
    return bars


def _has(reasons: list[str], needle: str) -> bool:
    return any(needle in r and "✓" in r for r in reasons)


# ── The e2e ─────────────────────────────────────────────────────────────

class TestEvolutionE2EPass:
    def test_plan_codegen_bars_deep_and_chain_pass(self):
        # Phase 1 — plan
        directives = parse_plan_directives(_PLAN_TEXT)
        assert directives["action"] == "change"
        criteria = directives["criteria"]
        assert criteria is not None
        assert criteria["profit_factor_min"] == 1.2
        assert criteria["win_rate_min"] == 0.4

        # Phase 2 — codegen stub (extract + sandbox load + name gate)
        expected_name = next_candidate_name(BASE_CLASS)
        assert expected_name == CANDIDATE_CLASS
        code = extract_python_code(_CODEGEN_RESPONSE)
        assert code and "class TrendCaptureEvo1" in code
        assert "### **Notes**" not in code  # trailing markdown stripped
        candidate_cls = load_strategy_from_source(code)
        assert check_candidate_name(candidate_cls, expected_name) == ""
        baseline_cls = load_strategy_from_source(_BASE_SOURCE)

        # Phase 3 — walk-forward + Monte Carlo + fitness, production windows
        bars = _trending_bars()
        assert (bars[-1].dt - bars[0].dt).days >= TRAIN_DAYS + TEST_DAYS - 1
        assert len(bars) >= 150  # live glue refuses to validate below this

        baseline = run_deep_validation(
            baseline_cls, bars, POINT_VALUE, "基準 baseline",
            TRAIN_DAYS, TEST_DAYS, monte_carlo=False,
            capital_base=DEFAULT_CAPITAL_BASE_TWD)
        candidate = run_deep_validation(
            candidate_cls, bars, POINT_VALUE, f"候選 {CANDIDATE_CLASS}",
            TRAIN_DAYS, TEST_DAYS, monte_carlo=True,
            capital_base=DEFAULT_CAPITAL_BASE_TWD)

        assert baseline.test.error == "" and candidate.test.error == ""
        assert baseline.train.metrics.total_trades >= 30
        assert candidate.test.metrics.total_trades >= 30

        # Fitness path: run_ab_backtest scores both windows.
        bf, cf = baseline.test.fitness, candidate.test.fitness
        assert bf is not None and cf is not None
        assert not bf.gated and not cf.gated
        assert cf.composite >= bf.composite
        assert candidate.train.fitness is not None
        assert candidate.train.fitness.composite > 0

        # MC actually ran (numeric __init__ defaults, not the kwargs no-op)
        # and did not flag the candidate as curve-fit.
        assert candidate.mc_variance > 0
        assert candidate.fragile is False

        verdict = decide_deep_verdict(baseline, candidate, criteria)
        assert verdict.passed, f"expected PASS, reasons={verdict.reasons}"
        assert verdict.used_criteria

        # Full AND-chain, in the order decide_deep_verdict emits it.
        reasons = verdict.reasons
        expressed = next(r for r in reasons if "mutation expressed" in r)
        n_divergent = int(re.search(r"(\d+) divergent", expressed).group(1))
        assert n_divergent >= MIN_EXPRESSED_TRADES
        assert _has(reasons, "Monte Carlo robustness")
        assert _has(reasons, "OOS composite")
        assert _has(reasons, "design-window PF ratio")
        assert _has(reasons, "design-window MaxDD ratio")
        assert _has(reasons, "train-window MaxDD%")
        assert _has(reasons, "holdout MaxDD ratio")
        assert _has(reasons, f"PF {candidate.test.metrics.profit_factor:.2f} ≥ {ABS_PF_FLOOR:g}")
        assert _has(reasons, "WR ")
        assert _has(reasons, "P&L ")
        assert all("✗" not in r for r in reasons)

        block = format_deep_verdict_block(
            baseline, candidate, verdict, "fixture 15-min bars",
            saved_as=f"AI: {CANDIDATE_CLASS}")
        assert "PASS ✅" in block
        assert "walk-forward" in block
        assert "UNSEEN by design" in block
        assert f"已存檔 Saved as「AI: {CANDIDATE_CLASS}」" in block
