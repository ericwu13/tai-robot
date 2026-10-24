"""P1 — relative paper gate, absolute real-money gate, recommend-only retire.

A candidate that is still losing, but ahead of its baseline, passes the
paper path when ``paper_gate`` is relative and fails the real-money path.
Retirement returns a recommendation and does not change bot state.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

import run_backtest as rb
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
    evolution_gate_config_from_mapping,
    mean_pnl_upper_bound,
    paired_fold_bootstrap_p,
    relative_eval_summary,
    resolve_promotion_gate,
    weeks_from_changelog,
)
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
           window=None, holdout_start=None):
    cfg, gate = _gate(trading_mode, paper_gate, real_gate)
    if window is None:
        window = EvolutionVerdict(True, ["walk-forward collapse guards held ✓"])
    if holdout_start is None and folds:
        holdout_start = folds[-1].start
    verdict = assemble_promotion_verdict(
        gate=gate, gate_cfg=cfg, folds=folds,
        base_trades=base, cand_trades=cand,
        holdout_start=holdout_start, criteria=None,
        window_verdict=window,
    )
    return verdict, gate


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
        assert cfg.paper_gate == "absolute"
        assert cfg.real_gate == "absolute"
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

    def test_junk_paper_gate_stays_absolute_and_real_gate_cannot_go_relative(self):
        cfg = evolution_gate_config_from_mapping({
            "paper_gate": "nope",
            "real_gate": "relative",
        })
        assert cfg.paper_gate == "absolute"
        assert cfg.real_gate == "absolute"
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
        assert any("PBO and DSR not applied" in r for r in paper.reasons)
        # Same numbers, two families: the gate has no strategy-name argument.
        for _family in ("DynamicExitPullback", "BollingerBreakout"):
            again, _ = _judge("paper", "relative", folds, base, cand)
            assert again.passed is paper.passed
            assert again.reasons == paper.reasons

    def test_five_nonempty_folds_fail_the_relative_floor(self):
        folds, base, cand = _improved_losing(5)
        verdict, _ = _judge("paper", "relative", folds, base, cand)
        assert not verdict.passed
        assert any("non-empty folds: 5 ≥ 6" in r and "✗" in r
                   for r in verdict.reasons)

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
            folds, pnls, RelativeGateConfig(**fast))
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
                post_backfill_start="2026-05-01", **fast))
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
        # Δ < 0 still provisional when the PF floor is dropped.
        worse = decide_month_gates(
            [_Trade("2026-08-01 10:00", 500), _Trade("2026-09-15 10:00", 0)],
            [_Trade("2026-08-01 10:00", 100), _Trade("2026-09-15 10:00", 0)],
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
        gate_src = Path("src/evolution/paper_gate.py").read_text(encoding="utf-8")
        assert "StrategyPool" not in gate_src
        assert "settings.yaml" not in gate_src
        assert "session.json" not in gate_src
        assert "trading_mode =" not in gate_src
