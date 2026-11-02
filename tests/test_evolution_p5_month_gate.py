"""P5 month gates: fold a short newest month, scale the slack.

Defaults are the P5 rules. ``merge_stub`` folds the newest exit month
into the previous one when the price bars show fewer than 12 distinct
TAIFEX trading dates. A night session belongs to the next trading day,
so a night that crosses midnight is one date and Friday night maps to
Monday. ``slack.mode=scaled`` sets the allowance to
``clamp(k × σ × sqrt(n), 3000, 15000)``; σ is the sample (n−1)
standard deviation and gate 3 uses half of that allowance.
``max_exit_month`` and ``flat`` are opt-outs.

The #153 9/26 pin still FAILs. One baseline prior trade cannot
estimate σ, so the default allowance on that pin is the 3000 floor.
The flat 5k line is the explicit opt-out.
"""

from __future__ import annotations

import ast
import inspect
import math
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import run_backtest as rb
from src.evolution.paper_gate import (
    EvolutionGateConfig,
    PromotionGate,
    assemble_promotion_verdict,
)
from src.evolution.pipeline import (
    EvolutionVerdict,
    MonthGateConfig,
    MonthSlackConfig,
    combine_evolution_verdicts,
    count_month_sessions,
    decide_month_gates,
    month_gate_config_from_mapping,
    scaled_slack_twd,
    trading_date_of_bar,
)
from src.market_data.holidays import is_taifex_holiday


def _trade(exit_dt: str, pnl: int):
    return SimpleNamespace(exit_dt=exit_dt, pnl=pnl)


def _bars(days: list[str], *, hour: int = 9, minute: int = 0):
    """Day-session bars (09:00) unless the caller picks another clock time."""
    out = []
    for day in days:
        dt = datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, minute=minute)
        out.append(SimpleNamespace(dt=dt))
    return out


def _open_days(start: str, n: int) -> list[str]:
    """``n`` TAIFEX-open dates beginning at ``start`` (that date included)."""
    d = datetime.strptime(start, "%Y-%m-%d").date()
    out: list[str] = []
    while len(out) < n:
        if not is_taifex_holiday(d):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _flat(**overrides) -> MonthGateConfig:
    """Explicit opt-out: newest exit month, flat 5k, gate 3 Δ ≥ 0."""
    return MonthGateConfig(
        latest_def=overrides.get("latest_def", "max_exit_month"),
        min_sessions=overrides.get("min_sessions", 12),
        slack=MonthSlackConfig(mode="flat"),
    )


def _scaled(**overrides) -> MonthGateConfig:
    """Scaled slack with the fold pinned off, so these cases judge slack."""
    slack = dict(mode="scaled", k=1.0, gate3_k=0.5, floor=3000, cap=15000,
                 flat_twd=5000)
    slack.update(overrides.pop("slack", {}))
    cfg = dict(latest_def="max_exit_month", min_sessions=12)
    cfg.update(overrides)
    return MonthGateConfig(slack=MonthSlackConfig(**slack), **cfg)


# ── session fold ──────────────────────────────────────────────────────


class TestShortMonthFoldBoundary:
    """11 price-bar sessions fold. 12 do not. The count is not a calendar."""

    def _books(self):
        # Oct delta −100, Sep delta +1000, Aug flat. Folding Oct into Sep
        # makes the latest block 100% of a positive edge.
        base = [
            _trade("2026-08-15 13:45", 0),
            _trade("2026-09-15 13:45", 0),
            _trade("2026-10-15 13:45", 0),
        ]
        cand = [
            _trade("2026-08-15 13:45", 0),
            _trade("2026-09-15 13:45", 1000),
            _trade("2026-10-15 13:45", -100),
        ]
        return base, cand

    def test_eleven_sessions_fold_and_twelve_do_not(self):
        base, cand = self._books()
        # Open dates only, so a weekend or the 10/9 holiday cannot
        # collapse two stamps onto one trading date.
        eleven = _open_days("2026-10-01", 11)
        folded = decide_month_gates(
            base, cand, holdout_start="2026-09-20 00:00",
            bars=_bars(eleven))
        text = "\n".join(folded.reasons)
        assert not folded.passed, text
        assert "calendar-month dominated" in text
        assert folded.month_gates["folded_from"] == "2026-10"
        assert folded.month_gates["latest"] == "2026-09"
        assert folded.month_gates["latest_sessions"] == 11
        assert folded.month_gates["latest_def"] == "merge_stub"
        assert folded.month_gates["slack_mode"] == "scaled"
        assert "11 trading sessions" in text

        twelve = _open_days("2026-10-01", 12)
        kept = decide_month_gates(
            base, cand, holdout_start="2026-09-20 00:00",
            bars=_bars(twelve))
        kept_text = "\n".join(kept.reasons)
        assert kept.passed, kept_text
        assert "calendar-month dominated" not in kept_text
        assert kept.month_gates["folded_from"] == ""
        assert kept.month_gates["latest"] == "2026-10"
        assert kept.month_gates["latest_sessions"] == 12
        assert "kept as latest" in kept_text

    def test_night_across_midnight_is_one_trading_date(self):
        # Thursday 2026-10-01 night runs through Friday 02:00 and belongs
        # to Friday. Friday night runs through Saturday morning and
        # belongs to Monday 2026-10-05.
        thursday_night = [
            SimpleNamespace(dt=datetime(2026, 10, 1, 22, 0)),
            SimpleNamespace(dt=datetime(2026, 10, 2, 2, 0)),
        ]
        friday_day = SimpleNamespace(dt=datetime(2026, 10, 2, 9, 0))
        friday_night = [
            SimpleNamespace(dt=datetime(2026, 10, 2, 21, 0)),
            SimpleNamespace(dt=datetime(2026, 10, 3, 4, 0)),
        ]
        assert trading_date_of_bar(thursday_night[0]) == "2026-10-02"
        assert trading_date_of_bar(thursday_night[1]) == "2026-10-02"
        assert trading_date_of_bar(friday_day) == "2026-10-02"
        assert count_month_sessions(thursday_night + [friday_day], "2026-10") == 1
        assert trading_date_of_bar(friday_night[0]) == "2026-10-05"
        assert trading_date_of_bar(friday_night[1]) == "2026-10-05"
        assert count_month_sessions(friday_night, "2026-10") == 1
        assert count_month_sessions(
            thursday_night + friday_night, "2026-10") == 2

    def test_friday_nights_do_not_turn_ten_trading_days_into_twelve(self):
        # Ten day sessions, plus the Saturday-morning half of each of
        # two Friday nights. Calendar dates of those bars are 12.
        # Both Saturday mornings belong to the Monday already in the
        # ten, so the trading-date count stays 10.
        days = [
            "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07",
            "2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13",
            "2026-01-14", "2026-01-15",
        ]
        bars = _bars(days, hour=9) + [
            SimpleNamespace(dt=datetime(2026, 1, 2, 21, 0)),
            SimpleNamespace(dt=datetime(2026, 1, 3, 4, 0)),
            SimpleNamespace(dt=datetime(2026, 1, 9, 21, 0)),
            SimpleNamespace(dt=datetime(2026, 1, 10, 4, 0)),
        ]
        calendar_dates = {bar.dt.date().isoformat() for bar in bars}
        assert len(calendar_dates) == 12
        assert trading_date_of_bar(bars[-3]) == "2026-01-05"
        assert trading_date_of_bar(bars[-1]) == "2026-01-12"
        assert count_month_sessions(bars, "2026-01") == 10

    def test_repeated_bars_on_one_date_count_once(self):
        days = _open_days("2026-10-01", 11)
        bars = _bars(days, hour=9) + _bars(days, hour=10)
        assert count_month_sessions(bars, "2026-10") == 11
        base, cand = self._books()
        v = decide_month_gates(
            base, cand, holdout_start="2026-09-20 00:00", bars=bars)
        assert v.month_gates["latest_sessions"] == 11
        assert v.month_gates["folded_from"] == "2026-10"

    def test_trade_count_does_not_set_the_session_count(self):
        # Thirty October exits would look like a full month if trades
        # were the calendar. Eleven trading dates still fold.
        base = [_trade("2026-09-15 13:45", 0)]
        cand = [_trade("2026-09-15 13:45", 1000)]
        for i in range(1, 31):
            base.append(_trade(f"2026-10-{i:02d} 13:45", 0))
            cand.append(_trade(f"2026-10-{i:02d} 13:45", -10))
        folded = decide_month_gates(
            base, cand, holdout_start="2026-09-20 00:00",
            bars=_bars(_open_days("2026-10-01", 11)))
        assert folded.month_gates["latest_sessions"] == 11
        assert folded.month_gates["folded_from"] == "2026-10"

        kept = decide_month_gates(
            [_trade("2026-09-15 13:45", 0), _trade("2026-10-15 13:45", 0)],
            [_trade("2026-09-15 13:45", 1000), _trade("2026-10-15 13:45", -100)],
            holdout_start="2026-09-20 00:00",
            bars=_bars(_open_days("2026-10-01", 12)))
        assert kept.month_gates["latest_sessions"] == 12
        assert kept.month_gates["folded_from"] == ""
        assert kept.month_gates["latest"] == "2026-10"

    def test_max_exit_month_opt_out_does_not_fold(self):
        base, cand = self._books()
        v = decide_month_gates(
            base, cand, holdout_start="2026-09-20 00:00",
            bars=_bars(_open_days("2026-10-01", 11)),
            month_gate=_flat())
        assert v.passed, v.reasons
        assert v.month_gates["latest"] == "2026-10"
        assert v.month_gates["folded_from"] == ""
        assert v.month_gates["latest_def"] == "max_exit_month"
        assert v.month_gates["slack_mode"] == "flat"
        assert v.month_gates["slack_allowance"] == 5000
        assert v.month_gates["gate3_slack"] == 0

    def test_merge_stub_without_bars_does_not_invent_a_count(self):
        base, cand = self._books()
        v = decide_month_gates(
            base, cand, holdout_start="2026-09-20 00:00")
        assert v.passed, v.reasons
        assert v.month_gates["latest"] == "2026-10"
        assert v.month_gates["latest_sessions"] is None
        assert v.month_gates["slack_mode"] == "scaled"
        assert any("price bars not supplied" in r for r in v.reasons)

    def test_january_stub_folds_into_previous_december(self):
        base = [_trade("2026-01-15 13:45", 0)]
        cand = [_trade("2026-01-15 13:45", 100)]
        v = decide_month_gates(
            base, cand, holdout_start="2025-12-20 00:00",
            bars=_bars(_open_days("2026-01-01", 11)))
        assert v.month_gates["latest"] == "2025-12"
        assert v.month_gates["folded_from"] == "2026-01"
        assert v.month_gates["latest_sessions"] == 11

    def test_holdout_starting_in_the_stub_month_is_inside_the_block(self):
        # October folds into September. A holdout that starts in October
        # is inside the merged block, so gate 3 runs. The same books with
        # a holdout in August stay outside Sep+Oct.
        base = [
            _trade("2026-08-15 10:00", 10000),
            _trade("2026-09-15 10:00", 0),
            _trade("2026-10-15 10:00", 0),
        ]
        cand = [
            _trade("2026-08-15 10:00", 8499),
            _trade("2026-09-15 10:00", 0),
            _trade("2026-10-15 10:00", 0),
        ]
        bars = _bars(_open_days("2026-10-01", 11))
        inside = decide_month_gates(
            base, cand, holdout_start="2026-10-15 00:00", bars=bars)
        text = "\n".join(inside.reasons)
        assert inside.month_gates["folded_from"] == "2026-10"
        assert inside.month_gates["latest"] == "2026-09"
        assert inside.provisional, text
        assert "outside latest" not in text
        assert any("but Δ −1501 ≥ −1500" in r for r in inside.reasons)

        outside = decide_month_gates(
            base, cand, holdout_start="2026-08-20 00:00", bars=bars)
        assert any(
            "outside latest calendar month Sep+Oct" in r
            for r in outside.reasons)
        assert not any("prior-month OOS" in r for r in outside.reasons)

    def test_stub_trades_stay_in_the_latest_block(self):
        # After the fold, October's −100 is part of the latest delta
        # (Sep 1000 + Oct −100 − base 200 = 700), not the prior block.
        base = [
            _trade("2026-08-15 10:00", 500),
            _trade("2026-09-15 10:00", 0),
            _trade("2026-10-15 10:00", 200),
        ]
        cand = [
            _trade("2026-08-15 10:00", 500),
            _trade("2026-09-15 10:00", 1000),
            _trade("2026-10-15 10:00", -100),
        ]
        v = decide_month_gates(
            base, cand, holdout_start="2026-09-20 00:00",
            bars=_bars(_open_days("2026-10-01", 11)))
        assert v.month_gates["folded_from"] == "2026-10"
        assert v.month_gates["latest"] == "2026-09"
        assert v.month_gates["base_prior_net"] == 500
        assert v.month_gates["cand_prior_net"] == 500
        assert v.month_gates["latest_delta"] == 700
        rows = {row["month"]: row for row in v.month_gates["months"]}
        assert rows["2026-10"]["cand_net"] == -100
        assert rows["2026-10"]["delta"] == -300


# ── scaled slack ──────────────────────────────────────────────────────


class TestScaledSlackClamp:
    def test_formula_clamp_edges(self):
        # n = 4 → sqrt(n) = 2, so k × σ × 2 is exact.
        assert scaled_slack_twd(1499, 4) == 3000      # 2998 → floor
        assert scaled_slack_twd(1500, 4) == 3000      # exactly the floor
        assert scaled_slack_twd(1501, 4) == 3002      # just above, unclamped
        assert scaled_slack_twd(4000, 4) == 8000      # interior
        assert scaled_slack_twd(7499, 4) == 14998     # just under the cap
        assert scaled_slack_twd(7500, 4) == 15000     # exactly the cap
        assert scaled_slack_twd(15000, 1) == 15000    # cap itself is kept
        assert scaled_slack_twd(15001, 1) == 15000    # 15001 → cap
        assert scaled_slack_twd(7501, 4) == 15000     # 15002 → cap
        assert scaled_slack_twd(float("nan"), 4) == 3000
        assert scaled_slack_twd(1000, 0) == 3000

    def test_gate_uses_full_slack_at_the_floor_and_the_cap(self):
        # Two baseline trades, diff 2000 → sample σ = 1000√2.
        # n_cand = 4 → raw ≈ 2828.43 → floor 3000.
        low = self._judge(base_pnls=(4000, 6000), cand_net=7000, n_cand=4)
        assert low.month_gates["slack_allowance"] == 3000
        assert low.month_gates["gate3_slack"] == 1500
        assert low.month_gates["floor"] == 10000 - 3000
        assert any("baseline−3000" in r and "✓" in r for r in low.reasons)

        # One TWD under the floored allowance fails gate 1.
        missed = self._judge(base_pnls=(4000, 6000), cand_net=6999, n_cand=4)
        assert not missed.passed
        assert any("prior-months collapse" in r for r in missed.reasons)

        # Huge σ, same n, hits the 15k cap. Gate 1 still uses the full cap.
        high = self._judge(base_pnls=(-100000, 120000), cand_net=5000, n_cand=4)
        assert high.month_gates["slack_allowance"] == 15000
        assert high.month_gates["gate3_slack"] == 7500
        assert high.month_gates["floor"] == 20000 - 15000
        # 5000 >= 20000 − 15000, so the cap is enough and gate 1 passes.
        assert any("baseline−15000" in r and "✓" in r for r in high.reasons)
        over = self._judge(base_pnls=(-100000, 120000), cand_net=4999, n_cand=4)
        assert any("prior-months collapse" in r for r in over.reasons)

    def test_sigma_uses_the_sample_divisor(self):
        # Two baseline prior trades, 0 and 10000. Sample σ (n−1) is
        # 5000√2 ≈ 7071; the population divisor would be 5000. n_cand
        # is 1, so the allowance is that σ, unclamped.
        v = self._judge(base_pnls=(0, 10000), cand_net=10000, n_cand=1)
        sigma = 5000 * math.sqrt(2)
        assert v.month_gates["sigma_per_trade"] == pytest.approx(sigma)
        assert v.month_gates["slack_allowance"] == round(sigma)
        assert v.month_gates["slack_allowance"] == 7071
        assert v.month_gates["n_prior_cand"] == 1

    def test_undefined_sigma_uses_the_floor(self):
        # One baseline prior trade cannot estimate σ.
        v = self._judge(base_pnls=(10000,), cand_net=7000, n_cand=4)
        assert v.month_gates["sigma_per_trade"] is None
        assert v.month_gates["slack_allowance"] == 3000
        assert v.month_gates["n_prior_cand"] == 4

    def _judge(self, *, base_pnls, cand_net, n_cand):
        base = [_trade(f"2026-08-{i + 1:02d} 10:00", pnl)
                for i, pnl in enumerate(base_pnls)]
        base.append(_trade("2026-09-15 10:00", 0))
        # n_cand prior trades. The first carries the net; the rest are 0
        # so PF stays defined (inf when the net is positive).
        cand = []
        for i in range(n_cand):
            pnl = cand_net if i == 0 else 0
            cand.append(_trade(f"2026-08-{i + 1:02d} 10:00", pnl))
        cand.append(_trade("2026-09-15 10:00", 0))
        # Holdout is in August, outside the September latest month, so
        # gate 3 does not fire and the ablation line is what these judge.
        return decide_month_gates(
            base, cand, holdout_start="2026-08-20 00:00",
            month_gate=_scaled())


class TestStrictHalfSlack:
    def test_half_of_the_clamped_slack_is_the_gate3_bound(self):
        # Same σ as the floor case: allowance 3000, strict half 1500.
        # Latest-month pnl matches, so the edge is the prior delta and
        # the share gate stays off. PF is infinite (no losses).
        passed = self._gate3(cand_net=10000 - 1500)
        assert passed.passed, passed.reasons
        assert not passed.provisional
        assert passed.month_gates["slack_allowance"] == 3000
        assert passed.month_gates["gate3_slack"] == 1500
        assert any("Δ −1500 ≥ −1500 ✓" in r for r in passed.reasons)

        failed = self._gate3(cand_net=10000 - 1501)
        assert not failed.passed
        assert failed.provisional
        assert any("Δ −1501 ≥ −1500" in r and "✗" in r for r in failed.reasons)
        # Half, not the full allowance: −1501 is inside the 3000 gate-1 slack.
        assert not any("prior-months collapse" in r for r in failed.reasons)

    def test_flat_mode_keeps_a_zero_gate3_slack(self):
        # Half of the flat 5k is not applied. Δ = −1 still fails gate 3.
        v = self._gate3(cand_net=9999, month_gate=_flat())
        assert not v.passed and v.provisional
        assert v.month_gates["slack_allowance"] == 5000
        assert v.month_gates["gate3_slack"] == 0
        assert any("Δ −1 ≥ 0: ✗" in r for r in v.reasons)

    def _gate3(self, *, cand_net, month_gate=None):
        if month_gate is None:
            month_gate = _scaled()
        base = [
            _trade("2026-08-01 10:00", 4000),
            _trade("2026-08-02 10:00", 6000),
            _trade("2026-09-15 10:00", 0),
        ]
        cand = [
            _trade("2026-08-01 10:00", cand_net),
            _trade("2026-08-02 10:00", 0),
            _trade("2026-08-03 10:00", 0),
            _trade("2026-08-04 10:00", 0),
            _trade("2026-09-15 10:00", 0),
        ]
        return decide_month_gates(
            base, cand, holdout_start="2026-09-02 00:00",
            bars=_bars(_open_days("2026-09-01", 12)),
            month_gate=month_gate)


class TestProvisionalTrailer:
    """The combined trailer names the slack rule that is actually on."""

    def test_scaled_trailer_states_allowance_and_half(self):
        failed = TestStrictHalfSlack()._gate3(cand_net=10000 - 1501)
        combined = combine_evolution_verdicts(failed)
        rule = combined.provisional_rule
        assert rule == (
            "PROVISIONAL — never auto-save (scaled allowance "
            "3000, strict check uses half (1500))"
        )
        assert rule in combined.reasons
        assert "Δ≥0" not in rule
        assert "Δ≥0" not in "\n".join(combined.reasons)
        src = inspect.getsource(combine_evolution_verdicts)
        assert "Δ≥0" not in src

    def test_flat_trailer_states_the_flat_rule(self):
        failed = TestStrictHalfSlack()._gate3(cand_net=9999, month_gate=_flat())
        combined = combine_evolution_verdicts(failed)
        rule = combined.provisional_rule
        assert rule == (
            "PROVISIONAL — never auto-save (flat allowance "
            "5000, strict check is Δ ≥ 0)"
        )
        assert rule in combined.reasons
        assert "Δ≥0" not in rule


# ── #153 pin ──────────────────────────────────────────────────────────


class TestIssue153PinStillFails:
    def test_default_path_still_fails_the_gold_numbers(self):
        from tests.test_evolution_issue153 import _ema80_verdict

        v = _ema80_verdict()
        text = "\n".join(v.reasons)
        assert not v.passed
        assert "prior-months collapse" in text
        assert "calendar-month dominated" in text
        assert "161.5%" in text
        assert v.month_gates["latest"] == "2026-09"
        assert v.month_gates["slack_mode"] == "scaled"
        # One baseline prior trade: σ is undefined, so the floor applies.
        assert v.month_gates["slack_allowance"] == 3000
        assert v.month_gates["gate3_slack"] == 1500
        assert "baseline−3000" in text
        assert "Δ −13430 ≥ −1500" in text

    def test_full_september_and_capped_slack_still_fail(self):
        """September is not a stub. The 15k cap does not rescue ema80.

        Prior nets stay the gold 49100 / 35670. Baseline prior variance
        is large enough that the scaled allowance clamps at 15000, which
        is the most generous this policy can be. Share stays 161.5%.
        """
        from tests.test_evolution_issue153 import _load

        data = _load()
        assert data["base_prior_net"] == 49100
        assert data["cand_prior_net"] == 35670
        # Two baseline trades, net 49100, far apart → σ × √4 blows past 15k.
        base = [
            _trade("2026-08-10 13:45", -100000),
            _trade("2026-08-20 13:45", 149100),
            _trade("2026-09-15 13:45", data["base_latest_net"]),
        ]
        assert sum(t.pnl for t in base if t.exit_dt.startswith("2026-08")) == 49100
        cand = [
            _trade("2026-08-10 13:45", data["cand_prior_net"]),
            _trade("2026-08-11 13:45", 0),
            _trade("2026-08-12 13:45", 0),
            _trade("2026-08-13 13:45", 0),
            _trade("2026-09-15 13:45", data["cand_latest_net"]),
        ]
        raw = math.sqrt(((-100000 - 24550) ** 2 + (149100 - 24550) ** 2) / 1)
        # n_cand prior = 4, so the unclamped SE is well above the cap.
        assert raw * 2 > 15000
        september = _open_days("2026-09-01", 18)
        assert len(september) == 18
        v = decide_month_gates(
            base, cand, holdout_start=data["holdout_start"],
            bars=_bars(september),
            month_gate=_scaled(latest_def="merge_stub"))
        text = "\n".join(v.reasons)
        assert not v.passed, text
        assert "calendar-month dominated" in text
        assert "161.5%" in text
        assert v.month_gates["latest"] == "2026-09"
        assert v.month_gates["folded_from"] == ""
        assert v.month_gates["latest_sessions"] == 18
        assert v.month_gates["slack_allowance"] == 15000
        assert v.month_gates["share_pct"] == 161.5
        # The cap clears the ablation floor (35670 >= 49100 − 15000) and
        # the verdict still fails on the September share.
        assert v.month_gates["floor"] == 49100 - 15000
        assert not any("prior-months collapse" in r for r in v.reasons)


# ── config ────────────────────────────────────────────────────────────


class TestMonthGateConfig:
    def test_defaults_are_merge_stub_and_scaled(self):
        cfg = month_gate_config_from_mapping({})
        assert cfg.warnings == ()
        assert cfg.latest_def == "merge_stub"
        assert cfg.min_sessions == 12
        assert cfg.slack.mode == "scaled"
        assert cfg.slack.flat_twd == 5000
        assert cfg.slack.k == pytest.approx(1.0)
        assert cfg.slack.gate3_k == pytest.approx(0.5)
        assert cfg.slack.floor == 3000
        assert cfg.slack.cap == 15000

    def test_example_settings_carry_the_same_defaults(self):
        data = yaml.safe_load(
            Path("settings.example.yaml").read_text(encoding="utf-8"))
        cfg = month_gate_config_from_mapping(data["evolution"])
        blank = month_gate_config_from_mapping({})
        assert cfg.latest_def == blank.latest_def == "merge_stub"
        assert cfg.slack.mode == blank.slack.mode == "scaled"
        assert cfg.min_sessions == blank.min_sessions == 12
        assert cfg.slack == blank.slack
        assert cfg.warnings == ()

    def test_junk_falls_back_and_warns(self):
        cfg = month_gate_config_from_mapping({
            "month_gate": {
                "latest_def": "trailing_30d",
                "min_sessions": 0,
                "slack": {
                    "mode": "bonus",
                    "k": 0,
                    "gate3_k": 2,
                    "floor": 20000,
                    "cap": 1000,
                },
            },
        })
        assert cfg.latest_def == "merge_stub"
        assert cfg.min_sessions == 12
        assert cfg.slack.mode == "scaled"
        assert cfg.slack.k == pytest.approx(1.0)
        assert cfg.slack.gate3_k == pytest.approx(0.5)
        assert cfg.slack.floor == 3000
        assert cfg.slack.cap == 15000
        assert cfg.warnings

    def test_enabled_values_parse(self):
        cfg = month_gate_config_from_mapping({
            "month_gate": {
                "latest_def": "merge_stub",
                "min_sessions": 12,
                "slack": {"mode": "scaled"},
            },
        })
        assert cfg.warnings == ()
        assert cfg.latest_def == "merge_stub"
        assert cfg.slack.mode == "scaled"
        assert cfg.slack.flat_twd == 5000
        assert cfg.slack.floor == 3000
        assert cfg.slack.cap == 15000


class TestPromotionWiring:
    def test_assemble_forwards_bars_and_month_gate(self):
        base = [
            _trade("2026-08-15 13:45", 0),
            _trade("2026-09-15 13:45", 0),
            _trade("2026-10-15 13:45", 0),
        ]
        cand = [
            _trade("2026-08-15 13:45", 0),
            _trade("2026-09-15 13:45", 1000),
            _trade("2026-10-15 13:45", -100),
        ]
        bars = _bars(_open_days("2026-10-01", 11))
        cfg = MonthGateConfig(latest_def="merge_stub")
        assembled = assemble_promotion_verdict(
            gate=PromotionGate("absolute", ""),
            gate_cfg=EvolutionGateConfig(),
            folds=[],
            base_trades=base,
            cand_trades=cand,
            holdout_start="2026-09-20 00:00",
            criteria=None,
            window_verdict=EvolutionVerdict(True, ["ok"]),
            bars=bars,
            month_gate=cfg,
        )
        assert assembled.month_verdict.month_gates["folded_from"] == "2026-10"
        assert not assembled.month_verdict.passed

    def test_worker_passes_price_bars_and_parsed_config(self):
        src = textwrap.dedent(
            inspect.getsource(rb.BacktestApp._start_evolution_pipeline))
        assert "month_gate_config_from_mapping" in src
        tree = ast.parse(src)
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "assemble_promotion_verdict"
        ]
        # Saturday verdict and the structural-lane Q2 both go through
        # the same helper, and both forward the price bars and the
        # parsed month-gate config.
        assert len(calls) == 2
        for call in calls:
            names = {kw.arg for kw in call.keywords}
            assert "bars" in names
            assert "month_gate" in names
