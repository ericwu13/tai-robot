"""Tests for regime settings wiring, evolved-params overlay, outcome
fitness, gene bounds, and fitness-gated write."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from src.regime.state_machine import RegimeConfig
from src.regime.config_loader import build_regime_config, _load_evolved_params
from src.evolution.regime_fitness import (
    compute_label_accuracy,
    compute_weighted_pnl,
    compute_regime_fitness,
)
from src.evolution.regime_pipeline import (
    REGIME_GENE_BOUNDS,
    RegimeCandidate,
    clamp_genes,
    mutate,
    MIN_FITNESS_THRESHOLD,
    _write_evolved_params,
    run_regime_evolution,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_settings(**overrides) -> dict:
    """Flat settings dict mimicking what ``_load_settings()`` produces."""
    base = {
        "regime_adx_enter": 22.0,
        "regime_adx_exit": 18.0,
        "regime_adx_strong": 28.0,
        "regime_confirm_sessions": 3,
        "regime_flip_pause_sessions": 4,
        "regime_max_flips_in_window": 2,
        "regime_flip_window_sessions": 8,
        "regime_classify_interval": 1800,
    }
    base.update(overrides)
    return base


def _make_report(effective_regime: str, pnl: float) -> dict:
    """Minimal daily report dict with regime switching data."""
    return {
        "regime_switching": {"effective_regime": effective_regime},
        "trades": [{"pnl": pnl}],
    }


# ── 1. Settings wiring ──────────────────────────────────────────────


class TestSettingsWiring:
    def test_config_picks_up_settings_values(self, tmp_path):
        settings = _make_settings()
        cfg = build_regime_config(settings, enabled=True,
                                  long_strategy="A", short_strategy="B",
                                  base_dir=str(tmp_path))
        assert cfg.adx_enter == 22.0
        assert cfg.adx_exit == 18.0
        assert cfg.adx_strong == 28.0
        assert cfg.confirm_sessions == 3
        assert cfg.pause_sessions == 4
        assert cfg.max_flips == 2
        assert cfg.flip_window == 8
        assert cfg.classify_interval == 1800
        assert cfg.enabled is True
        assert cfg.long_strategy == "A"
        assert cfg.short_strategy == "B"

    def test_config_uses_defaults_when_settings_empty(self, tmp_path):
        cfg = build_regime_config({}, enabled=False, base_dir=str(tmp_path))
        default = RegimeConfig()
        assert cfg.adx_enter == default.adx_enter
        assert cfg.adx_exit == default.adx_exit
        assert cfg.confirm_sessions == default.confirm_sessions

    def test_settings_values_differ_from_defaults(self, tmp_path):
        """Proves the test is meaningful — settings != dataclass defaults."""
        settings = _make_settings()
        cfg = build_regime_config(settings, base_dir=str(tmp_path))
        default = RegimeConfig()
        assert cfg.adx_enter != default.adx_enter
        assert cfg.confirm_sessions != default.confirm_sessions


# ── 2. Evolved params overlay ────────────────────────────────────────


class TestEvolvedParamsOverlay:
    def test_evolved_params_override_settings(self, tmp_path):
        evolved_dir = tmp_path / "data" / "regime"
        evolved_dir.mkdir(parents=True)
        evolved_file = evolved_dir / "regime_evolved_params.json"
        evolved_file.write_text(json.dumps({
            "adx_enter": 20.0,
            "adx_strong": 35.0,
            "confirm_sessions": 1,
            "evolved_at": "2026-08-01",
            "fitness_score": 0.65,
        }))
        settings = _make_settings()
        cfg = build_regime_config(settings, base_dir=str(tmp_path))
        assert cfg.adx_enter == 20.0
        assert cfg.adx_strong == 35.0
        assert cfg.confirm_sessions == 1

    def test_stale_evolved_params_ignored(self, tmp_path):
        evolved_dir = tmp_path / "data" / "regime"
        evolved_dir.mkdir(parents=True)
        evolved_file = evolved_dir / "regime_evolved_params.json"
        evolved_file.write_text(json.dumps({
            "adx_enter": 20.0,
            "adx_strong": 35.0,
            "confirm_sessions": 1,
            "evolved_at": "2020-01-01",
            "fitness_score": 0.65,
        }))
        settings = _make_settings()
        cfg = build_regime_config(settings, base_dir=str(tmp_path))
        assert cfg.adx_enter == 22.0  # from settings, not evolved

    def test_missing_evolved_file_uses_settings(self, tmp_path):
        settings = _make_settings()
        cfg = build_regime_config(settings, base_dir=str(tmp_path))
        assert cfg.adx_enter == 22.0

    def test_corrupt_evolved_file_uses_settings(self, tmp_path):
        evolved_dir = tmp_path / "data" / "regime"
        evolved_dir.mkdir(parents=True)
        (evolved_dir / "regime_evolved_params.json").write_text("not json")
        settings = _make_settings()
        cfg = build_regime_config(settings, base_dir=str(tmp_path))
        assert cfg.adx_enter == 22.0


# ── 3. Outcome fitness ──────────────────────────────────────────────


class TestOutcomeFitness:
    def test_label_accuracy_all_correct(self):
        reports = [
            _make_report("trending-up", 100),
            _make_report("trending-down", 50),
            _make_report("trending-up", 200),
        ]
        assert compute_label_accuracy(reports) == 1.0

    def test_label_accuracy_mixed(self):
        reports = [
            _make_report("trending-up", 100),
            _make_report("trending-up", -50),
        ]
        assert compute_label_accuracy(reports) == 0.5

    def test_label_accuracy_ignores_range_bound(self):
        reports = [
            _make_report("range-bound", 100),
            _make_report("unknown", -50),
        ]
        assert compute_label_accuracy(reports) == 0.0

    def test_label_accuracy_no_trades(self):
        reports = [{"regime_switching": {"effective_regime": "trending-up"},
                     "trades": []}]
        assert compute_label_accuracy(reports) == 0.0

    def test_weighted_pnl(self):
        reports = [
            _make_report("trending-up", 100),
            _make_report("trending-down", -30),
            _make_report("range-bound", 200),  # skipped
        ]
        assert compute_weighted_pnl(reports) == 70.0

    def test_combined_fitness(self):
        reports = [
            _make_report("trending-up", 100),
            _make_report("trending-up", 100),
        ]
        fitness = compute_regime_fitness(reports, pnl_normalization_cap=200)
        # accuracy = 1.0, pnl = 200 → normalized = 1.0
        # combined = 1.0*0.6 + 1.0*0.4 = 1.0
        assert fitness == pytest.approx(1.0)

    def test_fitness_zero_when_no_directional_reports(self):
        reports = [_make_report("range-bound", 500)]
        assert compute_regime_fitness(reports) == 0.0


# ── 4. Gene bounds respected after mutation ──────────────────────────


class TestGeneBounds:
    def test_clamp_within_bounds(self):
        candidate = RegimeCandidate(adx_enter=15.0, adx_strong=50.0,
                                    confirm_sessions=0)
        clamped = clamp_genes(candidate)
        lo_e, hi_e = REGIME_GENE_BOUNDS["adx_enter"]
        lo_s, hi_s = REGIME_GENE_BOUNDS["adx_strong"]
        lo_c, hi_c = REGIME_GENE_BOUNDS["confirm_sessions"]
        assert lo_e <= clamped.adx_enter <= hi_e
        assert lo_s <= clamped.adx_strong <= hi_s
        assert lo_c <= clamped.confirm_sessions <= hi_c

    def test_adx_strong_always_above_adx_enter(self):
        candidate = RegimeCandidate(adx_enter=30.0, adx_strong=29.0,
                                    confirm_sessions=2)
        clamped = clamp_genes(candidate)
        assert clamped.adx_strong > clamped.adx_enter

    def test_mutation_stays_in_bounds(self):
        parent = RegimeCandidate(adx_enter=25.0, adx_strong=30.0,
                                 confirm_sessions=2)
        for _ in range(100):
            child = mutate(parent)
            lo_e, hi_e = REGIME_GENE_BOUNDS["adx_enter"]
            lo_s, hi_s = REGIME_GENE_BOUNDS["adx_strong"]
            lo_c, hi_c = REGIME_GENE_BOUNDS["confirm_sessions"]
            assert lo_e <= child.adx_enter <= hi_e
            assert lo_s <= child.adx_strong <= hi_s
            assert lo_c <= child.confirm_sessions <= hi_c
            assert child.adx_strong > child.adx_enter


# ── 5. Evolved params not written if fitness below threshold ─────────


class TestFitnessGatedWrite:
    def test_no_write_below_threshold(self, tmp_path):
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        # Write reports with range-bound only → fitness = 0
        for i in range(15):
            report = _make_report("range-bound", 100)
            report["date"] = f"2026-07-{i+1:02d}"
            (reports_dir / f"2026-07-{i+1:02d}.json").write_text(
                json.dumps(report))
        evolved_path = tmp_path / "data" / "regime" / "regime_evolved_params.json"
        result = run_regime_evolution(
            reports_dir=str(reports_dir),
            base_dir=str(tmp_path),
        )
        assert result is None
        assert not evolved_path.exists()

    def test_write_when_fitness_sufficient(self, tmp_path):
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        for i in range(15):
            report = _make_report("trending-up", 5000)
            report["date"] = f"2026-07-{i+1:02d}"
            (reports_dir / f"2026-07-{i+1:02d}.json").write_text(
                json.dumps(report))
        result = run_regime_evolution(
            reports_dir=str(reports_dir),
            base_dir=str(tmp_path),
        )
        evolved_path = tmp_path / "data" / "regime" / "regime_evolved_params.json"
        assert result is not None
        assert result.fitness >= MIN_FITNESS_THRESHOLD
        assert evolved_path.exists()
        data = json.loads(evolved_path.read_text())
        assert data["adx_enter"] > 0
        assert data["fitness_score"] > 0
