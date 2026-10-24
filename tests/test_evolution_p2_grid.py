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
        # half tie, and the in-sample best (lowest index) lands on the
        # OOS median, so they are not counted. The other 34 land strictly
        # below. 34/70.
        col_a = [1.0, 1.0, 1.0, 1.0, -1.0, -1.0, -1.0, -1.0]
        col_b = [-1.0, -1.0, -1.0, -1.0, 1.0, 1.0, 1.0, 1.0]
        matrix = [list(row) for row in zip(col_a, col_b)]
        assert cscv_pbo(matrix, 8) == pytest.approx(34 / 70)

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
        assert capped == full[:HARD_MAX_SETTINGS]
        assert all(1 <= len(item) <= 2 for item in full)
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
        assert src.index("run_grid_on_bars") < src.index(
            "maybe_advance_watermark")
        assert src.index("grid_outcome.selected is None") < src.index(
            "maybe_advance_watermark")
        assert src.index("plan_unusable_reason(plan)") < src.index("one_shot(")
        assert "evolution_grid_explain" in src
        assert "call_site=\"bot_evolution\"" in src
        # The knob dict applied on the grid path is the grid's, and the
        # LLM plan parser is the other branch.
        assert src.index("dict(grid_outcome.selected)") < src.index(
            "parse_plan_directives(plan)")


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
        two = assemble_promotion_verdict(
            gate=gate, gate_cfg=cfg, folds=folds(2),
            base_trades=[], cand_trades=[],
            holdout_start="2026-07-10 00:00", criteria=None,
            window_verdict=EvolutionVerdict(True, ["window held ✓"]),
        )
        assert any("divergent≥2" in r for r in two.verdict.reasons)
        assert any("divergent folds:" in r and "≥ 2" in r and "✓" in r
                   for r in two.verdict.reasons)
        one = assemble_promotion_verdict(
            gate=gate, gate_cfg=cfg, folds=folds(1),
            base_trades=[], cand_trades=[],
            holdout_start="2026-07-10 00:00", criteria=None,
            window_verdict=EvolutionVerdict(True, ["window held ✓"]),
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
