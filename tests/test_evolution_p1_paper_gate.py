"""P1 — relative paper gate, absolute real-money gate, recommend-only retire.

A candidate that is still losing, but ahead of its baseline, passes the
paper path when ``paper_gate`` is relative and fails the real-money path.
Retirement returns a recommendation and does not change bot state.
"""

from __future__ import annotations

import ast
import inspect
import math
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

import run_backtest as rb
import src.evolution.paper_gate as paper_gate
from src.backtest.metrics import PerformanceMetrics
from src.evolution.paper_gate import (
    RelativeGateConfig,
    RetireConfig,
    WeekRecord,
    abs_pf_required,
    assemble_promotion_verdict,
    assess_retirement,
    decide_relative_paper_checks,
    emit_retire_recommendation,
    meets_min_folds,
    nonempty_fold_count,
    evolution_gate_config_from_mapping,
    evolution_promotion_mode,
    gate_provenance,
    mean_pnl_upper_bound,
    paired_fold_bootstrap_p,
    relative_eval_summary,
    resolve_promotion_gate,
    weeks_from_changelog,
)
from src.evolution.trial_penalty import TrialEvidence
from src.evolution.pipeline import (
    ABResult,
    ABS_PF_FLOOR,
    DeepResult,
    EvolutionVerdict,
    Fold,
    decide_deep_verdict,
    decide_month_gates,
    decide_multifold_verdict,
    decide_verdict,
    window_stats,
)


_NOW = datetime(2026, 10, 24, 6, 0)

# A grid trial that clears DSR > 0.90 and PBO < 0.50. Tests of the
# other relative checks pass this in so a missing penalty is not what
# fails them. The penalty's own pass/fail is covered separately.
_PASSING_TRIAL = TrialEvidence(
    dsr=0.97, pbo=0.10, n_trials=12, n_eff=8.0, rho_bar=0.2, partitions=8)


@dataclass
class _Trade:
    exit_dt: str
    pnl: int


@dataclass
class _Result:
    metrics: PerformanceMetrics
    trades: list | None = None


def _fold(i: int, base_pnls: list[int], cand_pnls: list[int],
          start: datetime | None = None) -> tuple[Fold, list[_Trade], list[_Trade]]:
    origin = start or (datetime(2026, 5, 1) + timedelta(days=14 * i))
    end = origin + timedelta(days=14)
    base_trades = [
        _Trade((origin + timedelta(hours=1 + j)).strftime("%Y-%m-%d %H:%M"), p)
        for j, p in enumerate(base_pnls)
    ]
    cand_trades = [
        _Trade((origin + timedelta(hours=1 + j)).strftime("%Y-%m-%d %H:%M"), p)
        for j, p in enumerate(cand_pnls)
    ]
    b = window_stats(base_trades)
    c = window_stats(cand_trades)
    fold = Fold(
        start=origin.strftime("%Y-%m-%d %H:%M"),
        end=end.strftime("%Y-%m-%d %H:%M"),
        base=b, cand=c, shared=0,
        base_only=len(base_pnls), cand_only=len(cand_pnls),
        delta=c.pnl - b.pnl,
    )
    return fold, base_trades, cand_trades


def _improved_losing(n_folds: int = 6):
    """Candidate PF < 1 on every fold, but each fold Δ is +200.

    Baseline per fold [100, -400] (sum -300). Candidate [100, -200]
    (sum -100, PF 0.5). Six folds clear the relative floors; the last
    fold's PF fails the absolute floor.
    """
    folds, base, cand = [], [], []
    for i in range(n_folds):
        fold, b, c = _fold(i, [100, -400], [100, -200])
        folds.append(fold)
        base.extend(b)
        cand.extend(c)
    return folds, base, cand


def _gate(trading_mode: str, paper_gate: str, real_gate: str = "absolute"):
    cfg = evolution_gate_config_from_mapping({
        "paper_gate": paper_gate,
        "real_gate": real_gate,
        "relative": {},
        "retire": {},
    })
    gate = resolve_promotion_gate(trading_mode, cfg.paper_gate, real_gate)
    return cfg, gate


def _judge(trading_mode, paper_gate, folds, base, cand, *, real_gate="absolute",
           window=None, holdout_start=None, trial=_PASSING_TRIAL):
    cfg, gate = _gate(trading_mode, paper_gate, real_gate)
    if window is None:
        window = EvolutionVerdict(True, ["walk-forward collapse guards held ✓"])
    if holdout_start is None and folds:
        holdout_start = folds[-1].start
    assembled = assemble_promotion_verdict(
        gate=gate, gate_cfg=cfg, folds=folds,
        base_trades=base, cand_trades=cand,
        holdout_start=holdout_start, criteria=None,
        window_verdict=window,
        trial=trial,
    )
    return assembled.verdict, gate


def _ab(n: int, pf: float, dd: int) -> ABResult:
    return ABResult(
        "side",
        result=_Result(PerformanceMetrics(
            total_trades=n, profit_factor=pf, max_drawdown=dd,
            max_drawdown_pct=5.0, total_pnl=0, win_rate=0.4,
        )),
    )


def _deep(train_pf: float, test_pf: float, *, train_dd: int = 100,
          test_dd: int = 100) -> DeepResult:
    return DeepResult(
        name="side",
        train=_ab(40, train_pf, train_dd),
        test=_ab(12, test_pf, test_dd),
        fragile=False, mc_variance=0.0,
    )


def _series(n: int, pnl: int, start: datetime | None = None) -> list[_Trade]:
    origin = start or datetime(2026, 1, 1, 9, 0)
    return [
        _Trade((origin + timedelta(days=i)).strftime("%Y-%m-%d %H:%M"), pnl)
        for i in range(n)
    ]


def _weeks(dates: list[str], passed: bool = False,
           evaluated: bool = True) -> list[WeekRecord]:
    return [
        WeekRecord(at=f"{day}T05:00:00", evaluated=evaluated, passed=passed)
        for day in dates
    ]


# ── config ────────────────────────────────────────────────────────────


class TestGateConfig:
    def test_defaults_match_the_approved_spec(self):
        cfg = evolution_gate_config_from_mapping({})
        assert cfg.paper_gate == "relative"
        assert cfg.real_gate == "absolute"
        assert cfg.search == "grid_batch"
        assert cfg.relative.dsr_min == pytest.approx(0.90)
        assert cfg.relative.pbo_max == pytest.approx(0.50)
        assert cfg.trial.max_settings == 48
        assert cfg.trial.max_joint_knobs == 2
        assert cfg.trial.max_values_per_knob == 4
        assert cfg.trial.plateau_band == pytest.approx(0.50)
        rel, ret = cfg.relative, cfg.retire
        assert rel.min_nonempty_folds == 6
        assert rel.min_divergent_folds == 3
        assert rel.fold_majority == pytest.approx(0.60)
        assert rel.bootstrap_alpha == pytest.approx(0.10)
        assert rel.bootstrap_resamples == 10_000
        assert rel.bootstrap_seed == 20261024
        assert rel.effect_size_sigma == pytest.approx(0.25)
        assert rel.min_post_backfill_folds == 2
        assert rel.post_backfill_start == ""
        assert rel.fold_days == 14
        assert ret.min_trades == 150
        assert ret.pf_below == pytest.approx(0.95)
        assert ret.upper_q == pytest.approx(0.90)
        assert ret.consecutive_weeks == 4
        assert ret.min_eligible_trades == 30
        assert ret.idle_days == 7
        assert ret.structural_quiet_days == 60
        assert ret.bootstrap_resamples == 10_000
        assert ret.bootstrap_seed == 20261024

    def test_example_settings_carry_the_same_defaults(self):
        data = yaml.safe_load(
            Path("settings.example.yaml").read_text(encoding="utf-8"))
        cfg = evolution_gate_config_from_mapping(data["evolution"])
        blank = evolution_gate_config_from_mapping({})
        assert cfg == blank

    def test_junk_paper_gate_uses_the_relative_default_and_real_gate_cannot_go_relative(self):
        cfg = evolution_gate_config_from_mapping({
            "paper_gate": "nope",
            "real_gate": "relative",
        })
        assert cfg.paper_gate == "relative"
        assert cfg.real_gate == "absolute"
        assert any("paper_gate" in w for w in cfg.warnings)
        forced = resolve_promotion_gate("paper", "relative", "relative")
        assert forced.mode == "relative"
        assert "ignored" in forced.note
        assert ABS_PF_FLOOR == 1.0

    def test_real_money_modes_ignore_a_relative_paper_gate(self):
        for mode in ("semi_auto", "auto", "live", "full_auto", "shadow"):
            gate = resolve_promotion_gate(mode, "relative", "absolute")
            assert gate.mode == "absolute", mode
            assert abs_pf_required(gate)
        paper = resolve_promotion_gate("paper", "relative", "absolute")
        backtest = resolve_promotion_gate("backtest", "relative", "absolute")
        assert paper.mode == "relative"
        assert backtest.mode == "relative"
        assert not abs_pf_required(paper)
        assert resolve_promotion_gate("paper", "absolute", "absolute").mode == "absolute"

    @pytest.mark.parametrize("mode", ["semi_auto", "auto", "live", "full_auto"])
    def test_each_real_money_mode_is_absolute_and_named_as_such(self, mode):
        # Membership kills a mutant that drops "auto" from the tuple:
        # an unknown mode is also absolute, so the mode alone is not enough.
        assert mode in paper_gate._REAL_MONEY_MODES
        gate = resolve_promotion_gate(mode, "relative", "absolute")
        assert gate.mode == "absolute"
        assert "is real money" in gate.note
        assert abs_pf_required(gate)

    @pytest.mark.parametrize(
        "mode", ["", None, "shadow", "Paper", " backtest", "paper ", "AUTO"])
    def test_empty_none_and_nonexact_modes_stay_absolute(self, mode):
        gate = resolve_promotion_gate(mode, "relative", "absolute")
        assert gate.mode == "absolute"
        assert "is real money" not in gate.note
        assert abs_pf_required(gate)

    def test_out_of_range_thresholds_fall_back_with_a_warning(self):
        cfg = evolution_gate_config_from_mapping({
            "relative": {
                "fold_majority": 0,
                "effect_size_sigma": 1,
                "bootstrap_alpha": 1.0,
                "min_nonempty_folds": 1,
            },
        })
        assert cfg.relative.fold_majority == pytest.approx(0.60)
        assert cfg.relative.effect_size_sigma == pytest.approx(0.25)
        assert cfg.relative.bootstrap_alpha == pytest.approx(0.10)
        assert cfg.relative.min_nonempty_folds == 6
        text = " ".join(cfg.warnings)
        for key in ("fold_majority", "effect_size_sigma",
                    "bootstrap_alpha", "min_nonempty_folds"):
            assert key in text

    def test_absent_threshold_keys_do_not_warn(self):
        assert evolution_gate_config_from_mapping({}).warnings == ()
        assert evolution_gate_config_from_mapping(
            {"relative": {}}).warnings == ()

    def test_in_range_thresholds_are_kept(self):
        cfg = evolution_gate_config_from_mapping({
            "relative": {
                "fold_majority": 0.999,
                "effect_size_sigma": 0.01,
                "bootstrap_alpha": 0.5,
                "min_nonempty_folds": 6,
            },
        })
        assert cfg.warnings == ()
        assert cfg.relative.fold_majority == pytest.approx(0.999)
        assert cfg.relative.min_nonempty_folds == 6
        top = evolution_gate_config_from_mapping({
            "relative": {"min_nonempty_folds": 30},
        })
        assert top.warnings == ()
        assert top.relative.min_nonempty_folds == 30
        over = evolution_gate_config_from_mapping({
            "relative": {"min_nonempty_folds": 31, "fold_majority": 1},
        })
        assert over.relative.min_nonempty_folds == 6
        assert over.relative.fold_majority == pytest.approx(0.60)
        assert len(over.warnings) == 2


# ── pass / fail edges ─────────────────────────────────────────────────


class TestRelativePaperEdges:
    def test_losing_but_improved_passes_paper_and_fails_real(self):
        folds, base, cand = _improved_losing()
        paper, paper_gate = _judge("paper", "relative", folds, base, cand)
        real, real_gate = _judge("auto", "relative", folds, base, cand)
        still_absolute, _ = _judge("paper", "absolute", folds, base, cand)
        assert paper_gate.mode == "relative"
        assert real_gate.mode == "absolute"
        assert paper.passed, paper.reasons
        assert not paper.provisional
        assert not real.passed
        assert not still_absolute.passed
        assert any("dropped on the paper relative path" in r for r in paper.reasons)
        assert any("holdout fold PF" in r and "✗" in r for r in real.reasons)
        assert any("drops prior-month" in r and "✓" in r for r in paper.reasons)
        assert any("prior-month OOS PF" in r and "✗" in r for r in real.reasons)
        assert any("seed=20261024" in r for r in paper.reasons)
        assert any("DSR 0.9700 > 0.9" in r and "✓" in r for r in paper.reasons)
        assert any("PBO 0.1000 < 0.5" in r and "✓" in r for r in paper.reasons)
        # Same numbers, two families: the gate has no strategy-name argument.
        for _family in ("DynamicExitPullback", "BollingerBreakout"):
            again, _ = _judge("paper", "relative", folds, base, cand)
            assert again.passed is paper.passed
            assert again.reasons == paper.reasons

    def test_five_folds_fail_meets_min_folds_six_are_eligible(self):
        assert meets_min_folds(5) is False
        assert meets_min_folds(6) is True
        assert meets_min_folds(7) is True
        fast = RelativeGateConfig(bootstrap_resamples=50, bootstrap_seed=1)

        five, base5, cand5 = _improved_losing(5)
        assert nonempty_fold_count(five) == 5
        five_rel = decide_relative_paper_checks(five, [t.pnl for t in base5], fast)
        assert not five_rel.passed
        assert any(
            "meets_min_folds: 5 ≥ 6" in r and "FAIL" in r
            for r in five_rel.reasons)
        five_promo, _gate = _judge("paper", "relative", five, base5, cand5)
        assert not five_promo.passed
        assert any("meets_min_folds: 5 ≥ 6" in r and "FAIL" in r
                   for r in five_promo.reasons)

        six, base6, _cand6 = _improved_losing(6)
        assert nonempty_fold_count(six) == 6
        assert meets_min_folds(nonempty_fold_count(six))
        six_rel = decide_relative_paper_checks(
            six, [t.pnl for t in base6], fast, _PASSING_TRIAL)
        assert any(
            "meets_min_folds: 6 ≥ 6" in r and "eligible" in r
            for r in six_rel.reasons)
        assert six_rel.passed, six_rel.reasons

    def test_majority_threshold_is_sixty_percent(self):
        # 3/5 = 60% passes the relative majority; 2/5 does not.
        # Holdout PF stays above 1 so the absolute floor is not the cause.
        def folds_with(wins: int, n: int = 5):
            out = []
            for i in range(n):
                # Single winning trade on each side so PF stays above 1.
                # A win fold is Δ +100; a loss fold is Δ −10, so 2/5 is
                # still a positive paired total and only the 60% rule bites.
                if i < wins:
                    fold, _, _ = _fold(i, [100], [200])
                else:
                    fold, _, _ = _fold(i, [100], [90])
                out.append(fold)
            return out

        passed = decide_multifold_verdict(
            folds_with(3), require_abs_pf=False, min_nonempty=5, majority=0.60)
        failed = decide_multifold_verdict(
            folds_with(2), require_abs_pf=False, min_nonempty=5, majority=0.60)
        assert passed.passed, passed.reasons
        assert any("3/5" in r and "60%" in r and "✓" in r for r in passed.reasons)
        assert not failed.passed
        assert any("2/5" in r and "60%" in r and "✗" in r for r in failed.reasons)
        # Default absolute path is still the half rule, not 60%.
        legacy = decide_multifold_verdict(folds_with(3))
        assert any("需 ≥ half" in r for r in legacy.reasons)

    def test_bootstrap_pass_fail_and_seed(self):
        pos = [100.0] * 6
        neg = [-100.0] * 6
        p_pos, mean_pos = paired_fold_bootstrap_p(pos, resamples=200, seed=7)
        p_neg, _ = paired_fold_bootstrap_p(neg, resamples=200, seed=7)
        again, _ = paired_fold_bootstrap_p(pos, resamples=200, seed=7)
        assert p_pos == 0.0 and mean_pos == 100.0
        assert p_neg == 1.0
        assert again == p_pos
        built, _, _ = _improved_losing()
        verdict = decide_relative_paper_checks(
            built, [100, -400] * 6,
            RelativeGateConfig(bootstrap_resamples=200, bootstrap_seed=7),
        )
        assert any("seed=7" in r and "p 0.0000" in r and "✓" in r
                   for r in verdict.reasons)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nonfinite_fold_deltas_fail_the_bootstrap_closed(self, bad):
        p_value, observed = paired_fold_bootstrap_p(
            [10.0, bad], resamples=50, seed=1)
        assert p_value == 1.0
        assert math.isnan(observed)
        folds, _, _ = _improved_losing()
        folds[0].delta = bad
        verdict = decide_relative_paper_checks(
            folds, [100, -400] * 6,
            RelativeGateConfig(bootstrap_resamples=20, bootstrap_seed=1),
        )
        assert not verdict.passed
        assert any("paired fold bootstrap" in r and "✗" in r
                   for r in verdict.reasons)

    def test_relative_path_logs_the_configured_divergent_floor(self):
        folds, base, cand = _improved_losing()
        for fold in folds[3:]:
            fold.delta = 0
        cfg = evolution_gate_config_from_mapping({
            "paper_gate": "relative",
            "relative": {"min_divergent_folds": 5, "bootstrap_resamples": 20},
        })
        gate = resolve_promotion_gate("paper", cfg.paper_gate, "absolute")
        assembled = assemble_promotion_verdict(
            gate=gate, gate_cfg=cfg, folds=folds,
            base_trades=base, cand_trades=cand,
            holdout_start=folds[-1].start, criteria=None,
            window_verdict=EvolutionVerdict(True, ["window held ✓"]),
        )
        reasons = assembled.verdict.reasons
        assert any("divergent≥5" in r for r in reasons)
        assert any("divergent folds:" in r and "≥ 5" in r and "✗" in r
                   for r in reasons)
        # Absolute path keeps the constant floor, so the logged number
        # still matches the check that ran.
        abs_cfg = evolution_gate_config_from_mapping({
            "paper_gate": "absolute",
            "relative": {"min_divergent_folds": 5},
        })
        abs_gate = resolve_promotion_gate("auto", "absolute", "absolute")
        absolute = assemble_promotion_verdict(
            gate=abs_gate, gate_cfg=abs_cfg, folds=folds,
            base_trades=base, cand_trades=cand,
            holdout_start=folds[-1].start, criteria=None,
            window_verdict=EvolutionVerdict(True, ["window held ✓"]),
        )
        assert any("divergent folds:" in r and "≥ 3" in r
                   for r in absolute.verdict.reasons)
        assert not any("≥ 5" in r for r in absolute.verdict.reasons)

    def test_effect_size_rejects_a_tiny_improvement(self):
        folds = []
        for i in range(6):
            fold, _, _ = _fold(i, [5000, -5000], [5001, -5000])  # Δ +1
            folds.append(fold)
        verdict = decide_relative_paper_checks(
            folds, [5000, -5000] * 6,
            RelativeGateConfig(bootstrap_resamples=50, bootstrap_seed=1),
        )
        assert not verdict.passed
        assert any("effect size:" in r and "✗" in r for r in verdict.reasons)
        assert any("paired fold bootstrap" in r and "✓" in r for r in verdict.reasons)

    def test_post_backfill_span_edges(self):
        folds, base, _ = _improved_losing()
        pnls = [t.pnl for t in base]
        fast = dict(bootstrap_resamples=50, bootstrap_seed=1)
        deferred = decide_relative_paper_checks(
            folds, pnls, RelativeGateConfig(**fast), _PASSING_TRIAL)
        assert deferred.passed, deferred.reasons
        assert any("span not configured" in r for r in deferred.reasons)
        late = decide_relative_paper_checks(
            folds, pnls, RelativeGateConfig(
                post_backfill_start="2026-07-01", **fast))
        assert not late.passed
        assert any("post-backfill folds: 1 ≥ 2" in r and "✗" in r
                   for r in late.reasons)
        early = decide_relative_paper_checks(
            folds, pnls, RelativeGateConfig(
                post_backfill_start="2026-05-01", **fast), _PASSING_TRIAL)
        assert early.passed, early.reasons
        assert any("post-backfill folds: 6 ≥ 2" in r and "✓" in r
                   for r in early.reasons)

    def test_gate3_prior_pf_drops_only_on_the_relative_path(self):
        # Prior PF 0.5, Δ > 0, holdout inside the latest month.
        base = [_Trade("2026-08-01 10:00", -200), _Trade("2026-09-15 10:00", 0)]
        cand = [
            _Trade("2026-08-01 10:00", 100),
            _Trade("2026-08-02 10:00", -200),
            _Trade("2026-09-15 10:00", 0),
        ]
        absolute = decide_month_gates(
            base, cand, holdout_start="2026-09-02 00:00")
        relative = decide_month_gates(
            base, cand, holdout_start="2026-09-02 00:00",
            drop_prior_pf_floor=True)
        assert not absolute.passed and absolute.provisional
        assert any("prior-month OOS PF" in r and "✗" in r for r in absolute.reasons)
        assert relative.passed, relative.reasons
        assert not relative.provisional
        # Δ below the strict half-slack (1500, half the 3000 floor when
        # σ is undefined) is still provisional when the PF floor is dropped.
        worse = decide_month_gates(
            [_Trade("2026-08-01 10:00", 500), _Trade("2026-09-15 10:00", 0)],
            [_Trade("2026-08-01 10:00", -1001), _Trade("2026-09-15 10:00", 0)],
            holdout_start="2026-09-02 00:00",
            drop_prior_pf_floor=True,
        )
        assert not worse.passed and worse.provisional

    def test_window_pf_floor_drops_on_paper_and_collapse_guards_stay(self):
        baseline = _deep(train_pf=1.0, test_pf=0.40, test_dd=200)
        candidate = _deep(train_pf=1.0, test_pf=0.50, test_dd=100)
        absolute = decide_deep_verdict(baseline, candidate, None)
        relative = decide_deep_verdict(
            baseline, candidate, None, require_abs_pf=False)
        assert not absolute.passed
        assert any("hard floor: PF" in r and "✗" in r for r in absolute.reasons)
        assert relative.passed, relative.reasons
        assert any("dropped on the paper relative path" in r
                   for r in relative.reasons)
        # Train PF collapse still fails the relative path.
        collapsed = decide_deep_verdict(
            _deep(train_pf=1.0, test_pf=0.40, test_dd=200),
            _deep(train_pf=0.10, test_pf=0.50, test_dd=100),
            None, require_abs_pf=False,
        )
        assert not collapsed.passed
        assert any("collapse" in r for r in collapsed.reasons)
        # Simple A/B: trade floor still applies when the PF floor is off.
        thin = decide_verdict(
            _ab(20, 0.4, 200), _ab(1, 0.5, 100), None, require_abs_pf=False)
        assert not thin.passed
        assert any("hard floor: candidate has 1 trades" in r for r in thin.reasons)


# ── retirement is a recommendation ────────────────────────────────────


class TestRetireRecommendation:
    def _eligible_trades(self, n: int = 40) -> list[_Trade]:
        # Last exit the day before _NOW, so the bot is not idle.
        start = _NOW - timedelta(days=n)
        return _series(n, -20, start)

    def _four_fails(self) -> list[WeekRecord]:
        return _weeks(["2026-09-05", "2026-09-12", "2026-09-19", "2026-09-26"])

    def test_performance_rule_edges(self):
        fast = RetireConfig(bootstrap_resamples=30)
        losing = assess_retirement(
            _series(150, -10), now=_NOW, config=fast)
        assert losing.recommend and losing.evidence["condition_a"]
        assert losing.evidence["last_pf"] < 0.95
        assert losing.evidence["mean_pnl_upper_bound"] < 0
        short = assess_retirement(_series(149, -10), now=_NOW, config=fast)
        assert not short.recommend and not short.evidence["condition_a"]
        flat = assess_retirement(_series(150, 0), now=_NOW, config=fast)
        assert flat.evidence["mean_pnl_upper_bound"] == 0
        assert not flat.recommend
        winners = assess_retirement(_series(150, 10), now=_NOW, config=fast)
        assert winners.evidence["last_pf"] == float("inf")
        assert not winners.recommend
        # Older wins must not hide a losing recent window.
        mixed = _series(50, 100_000) + _series(
            150, -10, datetime(2026, 3, 1, 9, 0))
        recent = assess_retirement(mixed, now=_NOW, config=fast)
        assert recent.evidence["condition_a"]
        assert recent.recommend
        # One calendar day cannot form two folds, so the bound is missing
        # and (a) does not fire.
        same_day = [
            _Trade("2026-10-01 09:00", -10) for _ in range(150)
        ]
        clump = assess_retirement(same_day, now=_NOW, config=fast)
        assert clump.evidence["mean_pnl_upper_bound"] is None
        assert not clump.evidence["condition_a"]
        # Default resample count is the approved 10_000. Constant folds
        # make the bound exact, so this also locks the seed path.
        approved = assess_retirement(_series(150, -10), now=_NOW)
        assert approved.evidence["bootstrap_resamples"] == 10_000
        assert approved.evidence["bootstrap_seed"] == 20261024
        assert approved.recommend

    def test_upper_bound_matches_the_helper(self):
        trades = _series(150, -10)
        bound = mean_pnl_upper_bound(
            trades, q=0.90, resamples=20, seed=3, fold_days=14)
        assert bound == -10

    def test_four_evaluated_failures_recommend_and_edges_do_not(self):
        trades = self._eligible_trades()
        hit = assess_retirement(
            trades, weeks=self._four_fails(), now=_NOW)
        assert hit.recommend and hit.evidence["condition_b"]
        assert not hit.evidence["condition_a"]
        only_three = assess_retirement(
            trades, weeks=_weeks(["2026-09-05", "2026-09-12", "2026-09-19"]),
            now=_NOW)
        assert not only_three.recommend
        # A skipped week is absent from the count: four fails plus a skip
        # still count as four, and a pass inside the tail blocks it.
        with_skip = self._four_fails() + [
            WeekRecord("2026-09-15T05:00:00", evaluated=False, passed=False)
        ]
        assert assess_retirement(trades, weeks=with_skip, now=_NOW).recommend
        broken = _weeks(["2026-09-05", "2026-09-12", "2026-09-19"]) + [
            WeekRecord("2026-09-26T05:00:00", evaluated=True, passed=True)
        ]
        assert not assess_retirement(trades, weeks=broken, now=_NOW).recommend
        # Same calendar day does not count twice.
        duped = [
            WeekRecord("2026-09-05T01:00:00", True, False),
            WeekRecord("2026-09-05T02:00:00", True, False),
            WeekRecord("2026-09-12T05:00:00", True, False),
            WeekRecord("2026-09-19T05:00:00", True, False),
        ]
        assert not assess_retirement(trades, weeks=duped, now=_NOW).recommend

    def test_idle_small_sample_and_structural_graduation_block_rule_b(self):
        fails = self._four_fails()
        idle_trades = _series(40, -20, datetime(2026, 8, 1, 9, 0))
        idle = assess_retirement(idle_trades, weeks=fails, now=_NOW)
        assert idle.evidence["idle"]
        assert not idle.recommend
        small = assess_retirement(self._eligible_trades(20), weeks=fails, now=_NOW)
        assert not small.evidence["eligible"]
        assert not small.recommend
        recent_grad = assess_retirement(
            self._eligible_trades(), weeks=fails, now=_NOW,
            structural_graduations=["2026-10-01"],
        )
        assert recent_grad.evidence["structural_graduation_recent"]
        assert not recent_grad.recommend
        old_grad = assess_retirement(
            self._eligible_trades(), weeks=fails, now=_NOW,
            structural_graduations=["2026-08-01"],
        )
        assert old_grad.recommend and old_grad.evidence["condition_b"]

    def test_week_history_is_per_strategy(self):
        fail = relative_eval_summary(False, "2026-10-03T05:00:00")
        ok = relative_eval_summary(True, "2026-10-03T05:00:00")
        entries = [
            {"strategy": "DynamicExitPullback", "change_summary": fail},
            {"strategy": "BollingerBreakout", "change_summary": ok},
            {"strategy": "DynamicExitPullback", "change_summary": "unrelated"},
        ]
        dep = weeks_from_changelog(entries, "DynamicExitPullback")
        bb = weeks_from_changelog(entries, "BollingerBreakout")
        assert len(dep) == 1 and dep[0].passed is False
        assert len(bb) == 1 and bb[0].passed is True
        assert weeks_from_changelog(entries, "SomeOtherFamily") == []
        # The marker inside an AI plan excerpt is not a Saturday record.
        leaked = (
            "promotion_gate=relative; auto-evolution pool-validated; "
            "plan excerpt: EVO_RELATIVE_GATE record only; no strategy "
            "change; passed=false at=2026-10-03T05:00:00"
        )
        assert weeks_from_changelog(
            [{"strategy": "DynamicExitPullback", "change_summary": leaked}],
            "DynamicExitPullback",
        ) == []
        padded = "  " + fail
        assert weeks_from_changelog(
            [{"strategy": "DynamicExitPullback", "change_summary": padded}],
            "DynamicExitPullback",
        )[0].passed is False
        # The marker regex is case-sensitive.
        folded = fail.replace("EVO_RELATIVE_GATE", "evo_relative_gate")
        assert weeks_from_changelog(
            [{"strategy": "DynamicExitPullback", "change_summary": folded}],
            "DynamicExitPullback",
        ) == []

    def test_emit_recommends_without_touching_bot_state(self):
        bot = {"trading_mode": "auto", "enabled": True, "pool_status": "live"}
        before = dict(bot)
        trades = list(self._eligible_trades())
        rec = assess_retirement(
            trades, weeks=self._four_fails(), now=_NOW)
        assert rec.recommend
        logs, changelogs, report = [], [], []

        def _changelog(**kwargs):
            changelogs.append(kwargs)

        emit_retire_recommendation(
            rec, log=logs.append, changelog=_changelog, report=report,
            strategy_name="DynamicExitPullback", version="DynamicExitPullbackV2",
        )
        assert bot == before
        assert trades == self._eligible_trades()
        assert logs and "RETIRE RECOMMENDATION only" in logs[0]
        assert "action: none" in logs[0]
        assert "no bot was disabled" in logs[0]
        assert report == logs
        assert changelogs[0]["version_before"] == changelogs[0]["version_after"]
        assert changelogs[0]["version_before"] == "DynamicExitPullbackV2"
        assert changelogs[0]["initiated_by"] == "ai"
        assert "action: none" in changelogs[0]["change_summary"]
        # A non-recommendation writes nothing.
        quiet_logs, quiet_changes = [], []
        emit_retire_recommendation(
            assess_retirement([], now=_NOW),
            log=quiet_logs.append,
            changelog=lambda **kwargs: quiet_changes.append(kwargs),
            report=report,
        )
        assert quiet_logs == [] and quiet_changes == []
        # The passing paper verdict is independent of the recommendation.
        folds, base, cand = _improved_losing()
        paper, _ = _judge("paper", "relative", folds, base, cand)
        assert paper.passed
        assert rec.recommend
        assert paper.passed

    def test_worker_emits_retirement_after_pool_promotion(self):
        src = inspect.getsource(rb.BacktestApp._start_evolution_pipeline)
        assert src.index("eligible_for_validated_pool(verdict)") < src.index(
            "rec = assess_retirement")
        assert src.index("rec = assess_retirement") < src.index(
            "evolution_verdict(verdict.passed, block)")
        assert "verdict.passed =" not in src
        # Retire still runs on an absolute gate: the call sits after the
        # relative-only record block, not inside it.
        assert src.index('if evo_gate.mode == "relative"') < src.index(
            "rec = assess_retirement")
        gate_src = Path("src/evolution/paper_gate.py").read_text(encoding="utf-8")
        assert "StrategyPool" not in gate_src
        assert "settings.yaml" not in gate_src
        assert "session.json" not in gate_src
        assert "trading_mode =" not in gate_src


def _pipeline_tree():
    src = textwrap.dedent(
        inspect.getsource(rb.BacktestApp._start_evolution_pipeline))
    return src, ast.parse(src)


class TestPromotionProvenance:
    def test_assemble_exposes_month_verdict(self):
        folds, base, cand = _improved_losing()
        cfg, gate = _gate("paper", "relative")
        assembled = assemble_promotion_verdict(
            gate=gate, gate_cfg=cfg, folds=folds,
            base_trades=base, cand_trades=cand,
            holdout_start=folds[-1].start, criteria=None,
            window_verdict=EvolutionVerdict(True, ["window held ✓"]),
        )
        month_verdict = assembled.month_verdict
        assert month_verdict is not None
        assert isinstance(month_verdict.month_gates, dict)
        # assemble_promotion_verdict threads the observation out. The
        # PASS changelog reads this same object.
        assert month_verdict.month_gates is assembled.month_gates
        assert "latest" in month_verdict.month_gates
        assert "share_applied" in month_verdict.month_gates
        assert month_verdict.month_gates["passed"] is month_verdict.passed

    def test_pass_records_name_the_gate_that_passed(self):
        assert gate_provenance("relative") == "promotion_gate=relative"
        assert gate_provenance("absolute") == "promotion_gate=absolute"
        assert gate_provenance("nope") == "promotion_gate=absolute"
        src = inspect.getsource(rb.BacktestApp._start_evolution_pipeline)
        notes_at = src.index("record_validated_candidate(")
        summary_at = src.index("change_summary=")
        fail_at = src.index("EVO changelog append failed")
        assert "gate_provenance(evo_gate.mode)" in src[notes_at:summary_at]
        assert "gate_provenance(evo_gate.mode)" in src[summary_at:fail_at]
        # Provenance leads the summary, ahead of the plan excerpt.
        assert src.index("gate_provenance(evo_gate.mode)") < src.index(
            "plan excerpt:")
        bind = src.index("month_verdict = assembled.month_verdict")
        forwarded = src.index("month_gates=month_verdict.month_gates")
        assert bind < summary_at < forwarded < fail_at
        assert "params_before=params_before" in src[summary_at:fail_at]
        assert "params_reason=params_reason" in src[summary_at:fail_at]

    def test_idle_evolve_uses_configured_mode_and_never_invents_one(self):
        class _Runner:
            def __init__(self, mode):
                self.trading_mode = mode

        running = evolution_promotion_mode(
            runner=_Runner("semi_auto"), runner_idle=False,
            configured_mode="paper")
        assert running == "semi_auto"
        switched = evolution_promotion_mode(
            runner=_Runner("auto"), runner_idle=False,
            configured_mode="paper")
        assert switched == "auto"
        idle = evolution_promotion_mode(
            runner=_Runner("paper"), runner_idle=True,
            configured_mode="semi_auto")
        assert idle == "semi_auto"
        missing = evolution_promotion_mode(
            runner=None, runner_idle=True, configured_mode=None)
        assert missing is None
        assert missing != "backtest"
        assert missing != "paper"
        unknown = evolution_promotion_mode(
            runner=None, runner_idle=True, configured_mode="weird")
        gate = resolve_promotion_gate(unknown, "relative", "absolute")
        assert gate.mode == "absolute"
        assert "is real money" not in gate.note
        # Configured paper, runner idle: relative is allowed because
        # that is the bot's mode, not a substitute.
        paper = resolve_promotion_gate(
            evolution_promotion_mode(
                runner=None, runner_idle=True, configured_mode="paper"),
            "relative", "absolute")
        assert paper.mode == "relative"

    def test_worker_resolves_mode_at_decision_time(self):
        src, tree = _pipeline_tree()
        assert "promotion_trading_mode" not in src
        assert 'else "backtest"' not in src
        assert '"paper") if live' not in src
        worker = src.split("def _worker", 1)[1]
        assert worker.index("evolution_promotion_mode(") < worker.index(
            "resolve_promotion_gate(")
        assert "LiveState.IDLE" in worker
        assert "EVO gate config:" in worker
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "resolve_promotion_gate"
        ]
        assert len(calls) == 1
        arg0 = calls[0].args[0]
        # A string constant ("paper") would let a live semi_auto bot
        # take the relative gate. The argument has to be the name the
        # helper returned.
        assert isinstance(arg0, ast.Name)
        assert not isinstance(arg0, ast.Constant)
        assert arg0.id == "promotion_mode"
        assigns = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "promotion_mode"
                    for t in n.targets)
        ]
        assert len(assigns) == 1
        call = assigns[0].value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Name)
        assert call.func.id == "evolution_promotion_mode"
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        assert not isinstance(kwargs["configured_mode"], ast.Constant)
        assert not isinstance(kwargs["runner"], ast.Constant)
