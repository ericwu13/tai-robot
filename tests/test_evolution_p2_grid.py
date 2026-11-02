"""P2 — pre-registered knob grid, DSR, CSCV PBO, plateau centre.

The model does not choose the setting. These tests lock the penalty
math to known values, the 48 / 2-knob / 4-value caps, the plateau
(including the ema60 spike), the divergent-fold floor of at least 2,
and the paper_gate default flip to relative.
"""

from __future__ import annotations

import inspect
import math
import textwrap

import pytest
import yaml

import run_backtest as rb
from src.backtest.strategy import BacktestStrategy
from src.evolution.grid_search import (
    HARD_MAX_SETTINGS,
    SEARCH_KNOB_CATALOG,
    GridPoint,
    build_search_settings,
    explanation_prompt,
    grid_search_applies,
    immediate_neighbours,
    iter_search_settings,
    on_plateau,
    plateau_centre,
    select_grid,
    within_plateau_band,
)
from src.evolution.knobs import knob_proposal_rejection
from src.evolution.paper_gate import (
    DEFAULT_MIN_DIVERGENT_FOLDS,
    TrialPenaltyConfig,
    bounded_min_divergent_folds,
    evolution_gate_config_from_mapping,
    resolve_promotion_gate,
)
from src.evolution.trial_penalty import (
    TrialEvidence,
    choose_partitions,
    cscv_pbo,
    deflated_sharpe_ratio,
    effective_trial_count,
    norm_cdf,
    norm_ppf,
    probability_of_backtest_overfitting,
    sample_moments,
    sharpe_ratio,
)

_DEP = {
    "ema_period": (40, 50, 60, 70),
    "atr_period": (10, 14, 21),
    "atr_stop_mult": (2.0, 2.5, 3.0),
    "atr_buffer_mult": (0.3, 0.5, 0.8),
}
_BASE = {
    "ema_period": 50,
    "atr_period": 14,
    "atr_stop_mult": 2.5,
    "atr_buffer_mult": 0.5,
}


class DynamicExitPullbackProbe:
    """Eligible knob class. The name is the strategy class, not a bot."""

    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)
        self.atr_stop_mult = kwargs.get("atr_stop_mult", 2.5)
        self.atr_buffer_mult = kwargs.get("atr_buffer_mult", 0.5)


def _independent_dsr_n1(returns: list[float]) -> float:
    """Bailey & López de Prado DSR with one trial, written out here.

    This does not call the implementation. N = 1 has no selection
    premium, so the result is the probabilistic Sharpe against 0.
    """
    n = len(returns)
    mu = sum(returns) / n
    sample_std = math.sqrt(sum((x - mu) ** 2 for x in returns) / (n - 1))
    sr = mu / sample_std
    devs = [x - mu for x in returns]
    m2 = sum(d * d for d in devs) / n
    m3 = sum(d ** 3 for d in devs) / n
    m4 = sum(d ** 4 for d in devs) / n
    skew = m3 / (m2 ** 1.5)
    kurt = m4 / (m2 * m2)
    var = (1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr) / (n - 1)
    z = sr / math.sqrt(var)
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


class TestKnownPenaltyValues:
    def test_normal_cdf_and_ppf_known_points(self):
        assert norm_cdf(0.0) == pytest.approx(0.5)
        assert norm_cdf(1.959963984540054) == pytest.approx(0.975)
        assert norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
        assert norm_ppf(0.975) == pytest.approx(1.959963984540054, abs=1e-9)

    def test_dsr_matches_the_hand_expansion_for_one_trial(self):
        returns = [1.0, 2.0, 3.0, 4.0]
        expected = _independent_dsr_n1(returns)
        # Frozen from that expansion: mean 2.5, sample std sqrt(5/3),
        # skew 0, non-excess kurtosis 1.64, z ≈ 2.65165.
        assert expected == pytest.approx(0.995995028835, abs=1e-12)
        sr = sharpe_ratio(returns)
        skew, kurt = sample_moments(returns)
        got = deflated_sharpe_ratio(sr, 0.0, 1, len(returns), skew, kurt)
        assert got == pytest.approx(expected, abs=1e-12)
        assert got == pytest.approx(0.995995028835, abs=1e-12)

    def test_effective_n_known_values(self):
        assert effective_trial_count(36, 0.0) == pytest.approx(36.0)
        assert effective_trial_count(36, 1.0) == pytest.approx(1.0)
        assert effective_trial_count(36, 0.5) == pytest.approx(18.5)
        # Identical trials do not inflate the expected maximum.
        assert effective_trial_count(1, 0.0) == pytest.approx(1.0)

    def test_pbo_zero_when_one_column_dominates(self):
        matrix = [[1.0, 0.0, -1.0] for _ in range(8)]
        pbo = cscv_pbo(matrix, 8)
        assert pbo == 0.0
        wrapped, partitions = probability_of_backtest_overfitting(matrix)
        assert partitions == 8
        assert wrapped == 0.0

    def test_pbo_known_fraction_on_the_symmetric_split(self):
        # First half rewards column 0, second half rewards column 1.
        # C(8, 4) = 70 splits. The 36 splits that take 2 rows from each
        # half tie, and the in-sample best lands on the OOS median.
        # A tie with the median counts as overfit (<=, not strict <),
        # and the other 34 land strictly below. 70/70.
        col_a = [1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0, -1.0]
        col_b = [-1.0, -1.0, -1.0, -1.0, 1.0, 1.0, 1.0, 1.0]
        matrix = [list(row) for row in zip(col_a, col_b)]
        assert cscv_pbo(matrix, 8) == pytest.approx(70 / 70)

    def test_partition_count_is_8_or_10(self):
        assert choose_partitions(7) is None
        assert choose_partitions(8) == 8
        assert choose_partitions(9) == 8
        assert choose_partitions(10) == 10
        assert choose_partitions(16) == 10
        short, parts = probability_of_backtest_overfitting([[1.0, 0.0]] * 7)
        assert short is None and parts is None
        nine = [[1.0, 0.0] for _ in range(9)]
        _pbo, parts = probability_of_backtest_overfitting(nine)
        assert parts == 8


class TestGridCaps:
    def test_full_dep_menu_is_capped_at_48_and_two_knobs(self):
        full = iter_search_settings(_BASE, _DEP)
        capped = build_search_settings(_BASE, _DEP)
        assert len(full) > HARD_MAX_SETTINGS
        assert len(capped) == HARD_MAX_SETTINGS
        assert all(1 <= len(item) <= 2 for item in full)
        atr_pairs = [
            item for item in full
            if len(item) == 2 and all(key.startswith("atr_") for key in item)
        ]
        assert atr_pairs
        assert all(item in capped for item in atr_pairs)
        singles = [item for item in capped if len(item) == 1]
        pairs = [item for item in capped if len(item) == 2]
        assert singles and pairs
        assert all(len(item) == 1 for item in capped[:len(singles)])
        ema_singles = {
            item["ema_period"] for item in singles if "ema_period" in item
        }
        # 80 and 90 are on the single-knob axis. The LLM allowlist is not.
        assert {30, 80, 90, 100} <= ema_singles
        assert 50 not in ema_singles
        pair_emas = {item["ema_period"] for item in pairs if "ema_period" in item}
        # Nearest 4 to baseline 50 are 30, 40, 50, 60. 50 is not a change.
        assert pair_emas <= {30, 40, 60}
        assert 80 not in pair_emas and 90 not in pair_emas

    def test_requesting_a_larger_grid_cannot_exceed_the_hard_caps(self):
        wide = {
            f"k{i}": (1, 2, 3, 4) for i in range(8)
        }
        baseline = {key: 1 for key in wide}
        full = iter_search_settings(
            baseline, wide, max_joint_knobs=5, max_values_per_knob=10)
        assert all(len(item) <= 2 for item in full)
        capped = build_search_settings(
            baseline, wide,
            TrialPenaltyConfig(max_settings=1000, max_joint_knobs=5,
                               max_values_per_knob=10))
        assert len(capped) == 48
        assert capped == full[:48]

    def test_llm_allowlist_still_rejects_ema_80_and_90(self):
        reason = knob_proposal_rejection(
            {"ema_period": 80}, _BASE, _DEP)
        assert "80/90" in reason
        assert 80 in SEARCH_KNOB_CATALOG["ema_period"]
        assert 90 in SEARCH_KNOB_CATALOG["ema_period"]


def _series(value: int, wobble: int = 0) -> list[float]:
    base = [float(value)] * 8
    if wobble:
        base[0] -= wobble
        base[1] += wobble
    return base


def _ema_point(value: int, index: int, pnls: list[float], *,
               baseline: bool = False) -> GridPoint:
    deltas = pnls  # baseline fold PnL is 0 in these fixtures
    knobs = () if baseline else (("ema_period", value),)
    return GridPoint(
        knobs=knobs,
        coords=(value,),
        index=(index,),
        fold_pnls=tuple(float(p) for p in pnls),
        mean_delta=sum(deltas) / len(deltas),
        is_baseline=baseline,
    )


def _evaluated(points: list[GridPoint]) -> dict:
    from src.evolution.grid_search import _coord_key
    return {_coord_key(point.coords): point for point in points}


class TestPlateau:
    def test_band_is_half_the_chosen_delta_and_must_be_positive(self):
        assert within_plateau_band(10.0, 20.0, 0.50)
        assert within_plateau_band(30.0, 20.0, 0.50)
        assert not within_plateau_band(9.0, 20.0, 0.50)
        assert not within_plateau_band(-5.0, 20.0, 0.50)
        assert not within_plateau_band(10.0, -20.0, 0.50)

    def test_ema60_spike_is_not_the_selection(self):
        # ema60's neighbour ema70 has the other sign of "almost nothing"
        # relative to the spike, and the baseline neighbour is Δ 0.
        # The old Saturday plan kept re-proposing ema60 alone.
        axis = ((40, 50, 60, 70, 80),)
        baseline = _ema_point(50, 1, [0.0] * 8, baseline=True)
        spike = _ema_point(60, 2, _series(100, 1))
        neighbour = _ema_point(70, 3, _series(-5, 1))
        far = _ema_point(80, 4, _series(4, 1))
        points = [spike, neighbour, far]
        evaluated = _evaluated([baseline, *points])
        assert not on_plateau(
            spike, immediate_neighbours(spike, evaluated, list(axis)), 0.50)
        outcome = select_grid(
            points, baseline, evaluated, list(axis), TrialPenaltyConfig(),
            preregistered=[{"ema_period": 60}, {"ema_period": 70},
                           {"ema_period": 80}])
        assert outcome.selected is None
        assert any("ema_period=60" in line for line in outcome.reasons)
        assert any("DECISION" in line and "nothing selected" in line
                   for line in outcome.reasons)
        assert len(outcome.settings) > 1

    def test_centre_is_chosen_not_the_maximum(self):
        # Plateau members are ema70 (mean 20) and ema80 (mean 24).
        # ema60 (mean 30) is higher but sits on the baseline, so it is
        # not on the plateau. The centre of {70, 80} is the one closer
        # to the baseline, ema70, even though ema80's Δ is larger.
        axis = ((40, 50, 60, 70, 80),)
        baseline = _ema_point(50, 1, [0.0] * 8, baseline=True)
        ema60 = _ema_point(60, 2, _series(30, 1))
        ema70 = _ema_point(70, 3, _series(20, 1))
        ema80 = _ema_point(80, 4, _series(24, 1))
        points = [ema60, ema70, ema80]
        evaluated = _evaluated([baseline, *points])
        members = [
            point for point in points
            if on_plateau(point, immediate_neighbours(
                point, evaluated, list(axis)), 0.50)
        ]
        assert {point.coords[0] for point in members} == {70, 80}
        centre = plateau_centre(members, baseline.index)
        assert centre is not None and centre.coords == (70,)
        assert centre.mean_delta < ema80.mean_delta
        outcome = select_grid(
            points, baseline, evaluated, list(axis), TrialPenaltyConfig(),
            preregistered=[
                {"ema_period": 60}, {"ema_period": 70}, {"ema_period": 80}],
        )
        assert outcome.selected == {"ema_period": 70}
        assert outcome.evidence is not None
        assert outcome.evidence.dsr > 0.90
        assert outcome.evidence.pbo < 0.50
        assert "model" not in inspect.signature(select_grid).parameters

    def test_a_point_with_no_neighbour_is_not_a_plateau(self):
        lonely = _ema_point(100, 0, _series(50, 1))
        assert not on_plateau(lonely, [], 0.50)

    def test_explanation_does_not_ask_the_model_to_choose(self):
        text = explanation_prompt({"ema_period": 70}, ["DSR=0.99"])
        assert "ema_period" in text and "70" in text
        assert "Do NOT propose a different setting" in text
        assert "grid_batch" not in inspect.signature(select_grid).parameters
        src = inspect.getsource(select_grid)
        assert "one_shot" not in src
        assert "send_message" not in src


class TestSearchSwitch:
    def test_default_search_applies_and_llm_one_does_not(self):
        source = inspect.getsource(DynamicExitPullbackProbe)
        assert grid_search_applies(DynamicExitPullbackProbe, source, {})
        assert not grid_search_applies(
            DynamicExitPullbackProbe, source, {"search": "llm_one"})
        assert not grid_search_applies(object, source, {})
        assert not grid_search_applies(DynamicExitPullbackProbe, "", {})

    def test_worker_scores_the_grid_before_the_watermark(self):
        src = textwrap.dedent(inspect.getsource(
            rb.BacktestApp._start_evolution_pipeline))
        assert src.index("run_grid_isolated") < src.index(
            "maybe_advance_watermark")
        assert 'getattr(self, "_grid_in_process", False)' in src
        assert "GridProcessFailure" in src
        assert src.index("grid_outcome.selected is None") < src.index(
            "maybe_advance_watermark")
        assert src.index("plan_unusable_reason(plan)") < src.index("one_shot(")
        assert "evolution_grid_explain" in src
        assert "call_site=\"bot_evolution\"" in src
        # The knob dict applied on the grid path is the grid's, and the
        # LLM plan parser is the other branch.
        assert src.index("dict(grid_outcome.selected)") < src.index(
            "parse_plan_directives(plan)")
        assert "fold_days=holdout_days" not in src
        assert "relative.fold_days" in src
        assert "gate_for_trial" in src

    def test_fold_days_narrower_than_the_holdout_falls_back(self):
        narrow = evolution_gate_config_from_mapping({
            "paper_gate": "relative",
            "holdout_days": 14,
            "relative": {"fold_days": 7},
        })
        assert narrow.paper_gate == "relative"
        assert narrow.real_gate == "absolute"
        assert narrow.relative.fold_days == 14
        assert any(
            "fold_days 7 < holdout_days 14" in line
            for line in narrow.warnings)
        longer_holdout = evolution_gate_config_from_mapping({
            "holdout_days": 21,
        })
        assert longer_holdout.relative.fold_days == 21
        assert longer_holdout.paper_gate == "relative"
        covered = evolution_gate_config_from_mapping({
            "holdout_days": 14,
            "relative": {"fold_days": 21},
        })
        assert covered.relative.fold_days == 21
        assert not any("OOS window" in line for line in covered.warnings)


class TestDivergentFloorAndDefault:
    def test_min_divergent_folds_out_of_range_uses_the_q2_default(self):
        # Q2 fold majority: "≥ 3 divergent folds". The fallback is that
        # named default, and Q2 is what sets the number to 3.
        q2_default = DEFAULT_MIN_DIVERGENT_FOLDS
        assert q2_default == 3
        assert bounded_min_divergent_folds(2) == 2
        assert bounded_min_divergent_folds(1) == q2_default
        assert bounded_min_divergent_folds(0) == q2_default
        assert bounded_min_divergent_folds(True) == q2_default
        assert bounded_min_divergent_folds(30) == 30
        assert bounded_min_divergent_folds(31) == q2_default
        assert bounded_min_divergent_folds("nope") == q2_default
        # 2.5 and 2.7 are not truncated to 2. The parser warns and
        # uses the same Q2 default the check uses.
        assert bounded_min_divergent_folds(2.5) == q2_default
        assert bounded_min_divergent_folds(2.7) == q2_default
        assert bounded_min_divergent_folds(2.0) == 2
        for raw in (2.5, 2.7):
            parsed = evolution_gate_config_from_mapping({
                "relative": {"min_divergent_folds": raw},
            })
            assert parsed.relative.min_divergent_folds == q2_default
            assert any("min_divergent_folds" in w and str(raw) in w
                       for w in parsed.warnings)
        kept = evolution_gate_config_from_mapping({
            "relative": {"min_divergent_folds": 2},
        })
        assert kept.warnings == ()
        assert kept.relative.min_divergent_folds == 2
        low = evolution_gate_config_from_mapping({
            "relative": {"min_divergent_folds": 1},
        })
        assert low.relative.min_divergent_folds == q2_default
        assert any("min_divergent_folds" in w for w in low.warnings)
        high = evolution_gate_config_from_mapping({
            "relative": {"min_divergent_folds": 31},
        })
        assert high.relative.min_divergent_folds == q2_default
        assert any("min_divergent_folds" in w for w in high.warnings)

    def test_relative_gate_uses_the_bounded_floor(self):
        from datetime import datetime, timedelta

        from src.evolution.paper_gate import (
            assemble_promotion_verdict,
            resolve_promotion_gate,
        )
        from src.evolution.pipeline import EvolutionVerdict, Fold, window_stats

        def folds(n_divergent: int):
            out = []
            for i in range(6):
                origin = datetime(2026, 5, 1) + timedelta(days=14 * i)
                end = origin + timedelta(days=14)
                delta = 100 if i < n_divergent else 0
                base = window_stats([])
                # Non-empty candidate so the nonempty floor is not the cause.
                cand_trades_n = 2
                cand = window_stats([
                    type("T", (), {"pnl": 50 + delta})(),
                    type("T", (), {"pnl": -40})(),
                ])
                out.append(Fold(
                    start=origin.strftime("%Y-%m-%d %H:%M"),
                    end=end.strftime("%Y-%m-%d %H:%M"),
                    base=base, cand=cand, shared=0, base_only=0,
                    cand_only=cand_trades_n, delta=delta,
                ))
            return out

        cfg = evolution_gate_config_from_mapping({
            "paper_gate": "relative",
            "relative": {"min_divergent_folds": 2},
        })
        gate = resolve_promotion_gate("paper", cfg.paper_gate, "absolute")
        kept_trial = TrialEvidence(
            dsr=0.97, pbo=0.10, n_trials=4, n_eff=3.0,
            rho_bar=0.0, partitions=8)
        two = assemble_promotion_verdict(
            gate=gate, gate_cfg=cfg, folds=folds(2),
            base_trades=[], cand_trades=[],
            holdout_start="2026-07-10 00:00", criteria=None,
            window_verdict=EvolutionVerdict(True, ["window held ✓"]),
            trial=kept_trial,
        )
        assert any("divergent≥2" in r for r in two.verdict.reasons)
        assert any("divergent folds:" in r and "≥ 2" in r and "✓" in r
                   for r in two.verdict.reasons)
        one = assemble_promotion_verdict(
            gate=gate, gate_cfg=cfg, folds=folds(1),
            base_trades=[], cand_trades=[],
            holdout_start="2026-07-10 00:00", criteria=None,
            window_verdict=EvolutionVerdict(True, ["window held ✓"]),
            trial=kept_trial,
        )
        assert any("divergent folds:" in r and "≥ 2" in r and "✗" in r
                   for r in one.verdict.reasons)

    def test_paper_gate_default_in_the_example_file_is_relative(self):
        data = yaml.safe_load(
            open("settings.example.yaml", encoding="utf-8"))
        evo = data["evolution"]
        assert evo["paper_gate"] == "relative"
        assert evo["search"] == "grid_batch"
        assert evo["relative"]["min_divergent_folds"] == DEFAULT_MIN_DIVERGENT_FOLDS
        assert evo["trial_penalty"]["max_settings"] == 48
        assert evo["trial_penalty"]["dsr_min"] == pytest.approx(0.90)
        assert evo["trial_penalty"]["pbo_max"] == pytest.approx(0.50)
        cfg = evolution_gate_config_from_mapping(evo)
        blank = evolution_gate_config_from_mapping({})
        assert cfg == blank
        assert cfg.paper_gate == "relative"

    def test_invalid_paper_gate_falls_back_to_absolute_with_a_warning(self):
        cfg = evolution_gate_config_from_mapping({"paper_gate": "nope"})
        assert cfg.paper_gate == "absolute"
        assert any("warning" in w and "paper_gate" in w for w in cfg.warnings)
        gate = resolve_promotion_gate("paper", "nope", "absolute")
        assert gate.mode == "absolute"
        assert "warning" in gate.note
        assert "absolute" in gate.note
        # A missing key is still relative, and only on a known paper mode.
        blank = evolution_gate_config_from_mapping({})
        assert blank.paper_gate == "relative"
        assert blank.warnings == ()
        known = resolve_promotion_gate("backtest", blank.paper_gate, "absolute")
        assert known.mode == "relative"
        assert known.note == ""

    def test_unknown_trading_mode_falls_back_to_absolute_with_a_warning(self):
        gate = resolve_promotion_gate("shadow", "relative", "absolute")
        assert gate.mode == "absolute"
        assert "warning" in gate.note
        assert "absolute" in gate.note
        assert "is real money" not in gate.note


def _logged_dsr(reasons) -> list[float]:
    found = []
    for line in reasons:
        if "DSR=" not in line or "DSR=n/a" in line:
            continue
        # "DSR=0.5000" or "DSR 0.5000" — the per-setting line uses DSR=.
        token = line.split("DSR=", 1)[1].split()[0]
        found.append(float(token))
    return found


class TestDeltaPenaltyAndGates:
    def test_dsr_known_value_includes_sr0(self):
        # Bailey & López de Prado (2014): SR0 from cross-trial dispersion
        # and N, then DSR = Φ((SR - SR0) / SE). Written out here so a
        # change to the formula cannot pass by calling itself.
        observed, sharpe_std, n_trials, n_obs = 0.5, 0.4, 20.0, 30
        skew, kurt = 0.0, 3.0
        gamma = 0.5772156649015328606
        sr0 = sharpe_std * (
            (1.0 - gamma) * norm_ppf(1.0 - 1.0 / n_trials)
            + gamma * norm_ppf(1.0 - 1.0 / (n_trials * math.e))
        )
        var = (
            1.0 - skew * observed
            + ((kurt - 1.0) / 4.0) * observed * observed
        ) / (n_obs - 1)
        expected = norm_cdf((observed - sr0) / math.sqrt(var))
        assert sr0 == pytest.approx(0.7602831804724796, abs=1e-12)
        assert expected == pytest.approx(0.09316650204349813, abs=1e-12)
        got = deflated_sharpe_ratio(
            observed, sharpe_std, n_trials, n_obs, skew, kurt)
        assert got == pytest.approx(expected, abs=1e-12)

    def test_sr0_zero_is_not_the_deflated_sharpe(self):
        # Same inputs as the pinned value. SR0 is about 0.76, so DSR is
        # about 0.09. A mutant that sets SR0 = 0 reports the
        # probabilistic Sharpe instead, which is above 0.99.
        got = deflated_sharpe_ratio(0.5, 0.4, 20, 30, 0.0, 3.0)
        no_penalty = deflated_sharpe_ratio(0.5, 0.0, 20, 30, 0.0, 3.0)
        assert got < 0.2
        assert no_penalty > 0.99
        assert got < no_penalty - 0.5

    def test_profitable_baseline_variants_are_not_dsr_one(self):
        # Absolute PnL of a winning baseline puts every near-copy at
        # DSR ≈ 1. The grid scores Δ, which here is noise around 0.
        base = [100.0, 102, 101, 103, 100, 104, 101, 102]
        noise = [0.5, -0.2, 0.4, -0.1, 0.3, -0.2, 0.2, 0.1]
        axis = ((50, 60, 70),)
        baseline = GridPoint(
            knobs=(), coords=(50,), index=(0,),
            fold_pnls=tuple(base), mean_delta=0.0, is_baseline=True)
        points = []
        for idx, scale in ((1, 1.0), (2, 0.8)):
            pnls = tuple(b + scale * n for b, n in zip(base, noise))
            delta = [c - b for c, b in zip(pnls, base)]
            points.append(GridPoint(
                knobs=(("ema_period", 50 + 10 * idx),),
                coords=(50 + 10 * idx,), index=(idx,),
                fold_pnls=pnls, mean_delta=sum(delta) / len(delta),
            ))
        evaluated = _evaluated([baseline, *points])
        outcome = select_grid(
            points, baseline, evaluated, list(axis), TrialPenaltyConfig(),
            preregistered=[{"ema_period": 60}, {"ema_period": 70}])
        logged = _logged_dsr(outcome.reasons)
        assert logged
        assert all(value < 0.9 for value in logged)

    def test_dsr_gate_rejects_a_plateau_centre(self):
        # Centre is the edge point (mean 1.25). Its DSR is ~0 because a
        # far point inflates SR0. PBO is 0 (that far point dominates).
        # Turning the DSR comparison off would select the centre.
        axis = ((40, 50, 60, 70),)
        baseline = _ema_point(50, 1, [0.0] * 8, baseline=True)

        def _pt(value, index, pnls):
            return _ema_point(value, index, pnls)

        centre = _pt(70, 3, [5, -4, 6, -3, 4, -2, 5, -1])
        neighbour = _pt(60, 2, [4, -3, 5, -2, 3, -1, 4, 0])
        far = _pt(40, 0, [20, 21, 19, 20, 22, 18, 20, 20])
        points = [far, neighbour, centre]
        evaluated = _evaluated([baseline, *points])
        assert on_plateau(
            centre, immediate_neighbours(centre, evaluated, list(axis)), 0.50)
        outcome = select_grid(
            points, baseline, evaluated, list(axis), TrialPenaltyConfig(),
            preregistered=[{"ema_period": v} for v in (40, 60, 70)])
        assert outcome.selected is None
        assert any("DSR" in line and "✗" in line for line in outcome.reasons)

    def test_pbo_gate_rejects_a_plateau_centre(self):
        # Two mirror-image columns. With "<=" the CSCV PBO is 1. DSR of
        # each column is ~1 (identical Sharpes, so SR0 is 0). The edge
        # point is on the plateau. Turning the PBO comparison off would
        # select it.
        axis = ((50, 60, 70),)
        baseline = _ema_point(50, 0, [0.0] * 8, baseline=True)
        inner = _ema_point(60, 1, [1, 1, 1, 1, 3, 3, 3, 3])
        edge = _ema_point(70, 2, [3, 3, 3, 3, 1, 1, 1, 1])
        points = [inner, edge]
        evaluated = _evaluated([baseline, *points])
        assert on_plateau(
            edge, immediate_neighbours(edge, evaluated, list(axis)), 0.50)
        outcome = select_grid(
            points, baseline, evaluated, list(axis), TrialPenaltyConfig(),
            preregistered=[{"ema_period": 60}, {"ema_period": 70}])
        assert outcome.selected is None
        assert any("PBO" in line and "✗" in line for line in outcome.reasons)

    def test_fold_days_sets_the_window_not_a_silent_default(self):
        from datetime import datetime, timedelta

        from src.backtest.broker import OrderSide, Trade
        from src.evolution.grid_search import _baseline_bounds

        def _trade(when):
            return Trade(
                tag="L", side=OrderSide.LONG, qty=1, entry_price=1,
                exit_price=1, entry_bar_index=0, exit_bar_index=1,
                pnl=0, exit_dt=when.strftime("%Y-%m-%d %H:%M"))

        last = datetime(2026, 6, 1, 12, 0)
        trades = [
            _trade(last - timedelta(days=20 * i)) for i in range(6)
        ]

        def _span(bounds):
            start, end = bounds[0]
            return (
                datetime.strptime(end, "%Y-%m-%d %H:%M")
                - datetime.strptime(start, "%Y-%m-%d %H:%M")
            )

        wide = _baseline_bounds(trades, 14, 10)
        narrow = _baseline_bounds(trades, 7, 10)
        assert _span(wide).days == 14
        assert _span(narrow).days == 7


class DynamicExitPullbackHoldoutProbe(BacktestStrategy):
    """Knob class whose backtest is replaced in the holdout test."""

    def __init__(self, **kwargs):
        self.ema_period = kwargs.get("ema_period", 50)
        self.atr_period = kwargs.get("atr_period", 14)

    def on_bar(self, bar, data_store, broker):
        return None

    def required_bars(self):
        return 1


class TestHoldoutIsNotUsedToPick:
    def test_perturbing_the_holdout_does_not_change_the_pick(self, monkeypatch):
        from datetime import datetime, timedelta
        from types import SimpleNamespace

        from src.backtest.broker import OrderSide, Trade
        from src.evolution.grid_search import run_grid_on_bars
        from src.evolution.pipeline import ABResult
        from src.market_data.models import Bar

        last = datetime(2026, 6, 1, 12, 0)
        dates = [
            (last - timedelta(days=14 * (8 - i))).strftime("%Y-%m-%d %H:%M")
            for i in range(9)
        ]
        late = "2026-06-03 12:00"

        def _trade(when, pnl):
            return Trade(
                tag="L", side=OrderSide.LONG, qty=1, entry_price=1,
                exit_price=1, entry_bar_index=0, exit_bar_index=1,
                pnl=int(pnl), exit_dt=when)

        def _selection(ema, i):
            value = {60: 30, 70: 20, 80: 24}.get(int(ema), 1)
            if i == 0:
                return value - 1
            if i == 1:
                return value + 1
            return value

        def _holdout(ema, perturbed):
            ema = int(ema)
            if perturbed and ema == 70:
                return -100000
            if perturbed and ema == 80:
                return 40000
            return {60: 30, 70: 20, 80: 24}.get(ema, 1)

        def fake_backtest(strategy_cls, bars, point_value, name,
                          capital_base=0):
            ema = int(getattr(strategy_cls(), "ema_period", 50))
            perturbed = bars[-1].close >= 90000
            if ema == 50:
                trades = [_trade(when, 0) for when in dates]
            else:
                trades = [
                    _trade(dates[i], _selection(ema, i)) for i in range(8)
                ]
                trades.append(_trade(dates[8], _holdout(ema, perturbed)))
                # After the baseline's last exit. Selection must ignore
                # it; the OOS window must keep it.
                trades.append(_trade(late, 777))
            return ABResult(
                name=name, result=SimpleNamespace(trades=trades))

        def three_settings(baseline, eligibility, trial=None):
            return [
                {"ema_period": 60},
                {"ema_period": 70},
                {"ema_period": 80},
            ]

        monkeypatch.setattr(
            "src.evolution.pipeline.run_ab_backtest", fake_backtest)
        monkeypatch.setattr(
            "src.evolution.grid_search.build_search_settings", three_settings)
        source = (
            "from src.backtest.strategy import BacktestStrategy\n\n"
            + inspect.getsource(DynamicExitPullbackHoldoutProbe)
        )

        def _bars(close):
            return [
                Bar(symbol="T", dt=datetime(2026, 1, 1, 9, 0),
                    open=100, high=110, low=90, close=100,
                    volume=1, interval=900),
                Bar(symbol="T", dt=datetime(2026, 6, 3, 9, 0),
                    open=100, high=110, low=90, close=close,
                    volume=1, interval=900),
            ]

        def _run(close):
            return run_grid_on_bars(
                strategy_cls=DynamicExitPullbackHoldoutProbe,
                source=source,
                bars=_bars(close),
                point_value=1,
                capital_base=100000,
                evo={},
                fold_days=14,
            )

        plain = _run(100)
        moved = _run(90000)
        assert plain.selected == {"ema_period": 70}
        assert moved.selected == {"ema_period": 70}
        assert plain.holdout is not None and moved.holdout is not None
        # The June 1 holdout trade (20, or -100000 once perturbed) plus
        # the trade after the baseline's last exit (777).
        assert plain.holdout.cand_pnl == pytest.approx(20 + 777)
        assert moved.holdout.cand_pnl == pytest.approx(-100000 + 777)
        assert plain.holdout.base_pnl == pytest.approx(0)
        assert plain.holdout.delta == pytest.approx(plain.holdout.cand_pnl)
        assert "excluded from selection" in " ".join(plain.reasons)
        start = datetime.strptime(
            plain.holdout.selection_start, "%Y-%m-%d %H:%M")
        end = datetime.strptime(
            plain.holdout.selection_end, "%Y-%m-%d %H:%M")
        assert (end - start).days == 14
        assert plain.holdout.fold_days == 14
        assert plain.holdout.end > late

    def test_planted_gap_before_the_midnight_fold_does_not_change_the_pick(
            self, monkeypatch):
        """Default 14/14 still leaks ~10h if selection stops at the fold.

        Folds round the last exit up to midnight. The walk-forward
        holdout starts at the last bar minus 14 days. A 13:45 close
        leaves 2026-05-18 13:45–2026-05-19 00:00 inside the previous
        fold. A huge PnL planted there must not move the pick, and the
        holdout window must start at the bar anchor.
        """
        from datetime import datetime, timedelta
        from types import SimpleNamespace

        from src.backtest.broker import OrderSide, Trade
        from src.evolution.grid_search import run_grid_on_bars
        from src.evolution.pipeline import ABResult
        from src.market_data.models import Bar

        last_exit = datetime(2026, 6, 1, 13, 45)
        fold_end = datetime(2026, 6, 2)
        # Eight selection folds, then the midnight holdout fold. The
        # baseline's last exit is last_exit, so the fold anchor is
        # fold_end and the bar holdout starts 10h15m earlier.
        fold_starts = [
            fold_end - timedelta(days=14 * (9 - i)) for i in range(9)]
        inside = [start + timedelta(days=3) for start in fold_starts[:8]]
        inside.append(last_exit)
        leak = datetime(2026, 5, 18, 18, 0)
        holdout_anchor = last_exit - timedelta(days=14)

        def _trade(when, pnl):
            return Trade(
                tag="L", side=OrderSide.LONG, qty=1, entry_price=1,
                exit_price=1, entry_bar_index=0, exit_bar_index=1,
                pnl=int(pnl), exit_dt=when.strftime("%Y-%m-%d %H:%M"))

        def _selection(ema, i):
            value = {60: 30, 70: 20, 80: 24}.get(int(ema), 1)
            if i == 0:
                return value - 1
            if i == 1:
                return value + 1
            return value

        def fake_backtest(strategy_cls, bars, point_value, name,
                          capital_base=0):
            ema = int(getattr(strategy_cls(), "ema_period", 50))
            planted = bars[-1].close >= 90000
            if ema == 50:
                trades = [_trade(when, 0) for when in inside]
            else:
                trades = [
                    _trade(inside[i], _selection(ema, i)) for i in range(8)
                ]
                trades.append(_trade(inside[8], 5))
                # Neighbours only. Counted under fold anchoring, this
                # knocks the centre off the plateau.
                if planted and ema != 70:
                    trades.append(_trade(leak, 100000))
            return ABResult(
                name=name, result=SimpleNamespace(trades=trades))

        def three_settings(baseline, eligibility, trial=None):
            return [
                {"ema_period": 60},
                {"ema_period": 70},
                {"ema_period": 80},
            ]

        monkeypatch.setattr(
            "src.evolution.pipeline.run_ab_backtest", fake_backtest)
        monkeypatch.setattr(
            "src.evolution.grid_search.build_search_settings", three_settings)
        source = (
            "from src.backtest.strategy import BacktestStrategy\n\n"
            + inspect.getsource(DynamicExitPullbackHoldoutProbe)
        )
        bars = [
            Bar(symbol="T", dt=datetime(2026, 1, 1, 9, 0),
                open=100, high=110, low=90, close=100,
                volume=1, interval=900),
            Bar(symbol="T", dt=last_exit,
                open=100, high=110, low=90, close=90000,
                volume=1, interval=900),
        ]
        outcome = run_grid_on_bars(
            strategy_cls=DynamicExitPullbackHoldoutProbe,
            source=source,
            bars=bars,
            point_value=1,
            capital_base=100000,
            evo={},
            fold_days=14,
        )
        assert outcome.selected == {"ema_period": 70}
        assert outcome.holdout is not None
        assert outcome.holdout.start == holdout_anchor.strftime(
            "%Y-%m-%d %H:%M")
        assert outcome.holdout.start <= leak.strftime("%Y-%m-%d %H:%M")
        assert leak.strftime("%Y-%m-%d %H:%M") < outcome.holdout.end


class TestNoGridEvidenceUsesAbsolute:
    def test_missing_trial_does_not_pass_and_falls_back(self):
        from datetime import datetime, timedelta

        from src.evolution.paper_gate import (
            NO_GRID_EVIDENCE_LOG,
            assemble_promotion_verdict,
            decide_relative_paper_checks,
            gate_for_trial,
        )
        from src.evolution.pipeline import (
            EvolutionVerdict,
            Fold,
            window_stats,
        )

        def _rows(origin, pnls):
            return [
                type("T", (), {
                    "pnl": pnl,
                    "exit_dt": (origin + timedelta(hours=1 + j)
                                ).strftime("%Y-%m-%d %H:%M"),
                })()
                for j, pnl in enumerate(pnls)
            ]

        folds, base, cand = [], [], []
        for i in range(6):
            origin = datetime(2026, 5, 1) + timedelta(days=14 * i)
            end = origin + timedelta(days=14)
            b = _rows(origin, [100, -400])
            c = _rows(origin, [100, -200])
            bs, cs = window_stats(b), window_stats(c)
            folds.append(Fold(
                start=origin.strftime("%Y-%m-%d %H:%M"),
                end=end.strftime("%Y-%m-%d %H:%M"),
                base=bs, cand=cs, shared=0, base_only=2, cand_only=2,
                delta=cs.pnl - bs.pnl,
            ))
            base.extend(b)
            cand.extend(c)

        # A mutant that treats "no grid result" as a relative pass makes
        # this True: the folds clear the other relative checks.
        from src.evolution.paper_gate import RelativeGateConfig
        direct = decide_relative_paper_checks(
            folds, [t.pnl for t in base],
            RelativeGateConfig(bootstrap_resamples=200, bootstrap_seed=1),
            None,
        )
        assert direct.passed is False
        assert any("Not a pass" in r for r in direct.reasons)

        cfg = evolution_gate_config_from_mapping({
            "paper_gate": "relative",
            "search": "llm_one",
            "relative": {"bootstrap_resamples": 200, "bootstrap_seed": 1},
        })
        gate = resolve_promotion_gate("paper", cfg.paper_gate, "absolute")
        fallen, line = gate_for_trial(gate, None)
        assert fallen.mode == "absolute"
        assert line == NO_GRID_EVIDENCE_LOG
        assembled = assemble_promotion_verdict(
            gate=gate, gate_cfg=cfg, folds=folds,
            base_trades=base, cand_trades=cand,
            holdout_start=folds[-1].start, criteria=None,
            window_verdict=EvolutionVerdict(True, ["window held ✓"]),
            trial=None,
        )
        text = "\n".join(assembled.verdict.reasons)
        assert NO_GRID_EVIDENCE_LOG in text
        # The month gate may say "price bars not supplied". That is not
        # the relative-path miss for a missing trial.
        assert "PBO and DSR: not supplied" not in text
        assert any("holdout fold PF" in r and "✗" in r
                   for r in assembled.verdict.reasons)
        assert assembled.verdict.passed is False
