"""Tests for the regime deploy dialog integration.

Verifies that the deploy dialog returns regime info and that RegimeConfig
is correctly constructed from dialog values (no Tk root needed).
"""

import os

import pytest

from src.regime.state_machine import RegimeConfig


# ── RegimeConfig defaults ───────────────────────────────────────────────

def test_regime_config_defaults():
    cfg = RegimeConfig()
    assert cfg.enabled is False
    assert cfg.long_strategy == ""
    assert cfg.short_strategy == ""
    assert cfg.adx_enter == 25.0
    assert cfg.adx_exit == 20.0
    assert cfg.confirm_sessions == 2
    assert cfg.vol_spike_ratio == 1.5


def test_regime_config_from_dialog_values():
    cfg = RegimeConfig(
        enabled=True,
        long_strategy="LongA",
        short_strategy="ShortB",
    )
    assert cfg.enabled is True
    assert cfg.long_strategy == "LongA"
    assert cfg.short_strategy == "ShortB"
    assert cfg.adx_enter == 25.0
    assert cfg.confirm_sessions == 2


def test_regime_config_no_dry_run():
    """dry_run was removed in the Phase 3 UI simplification."""
    cfg = RegimeConfig()
    assert not hasattr(cfg, "dry_run")


# ── Settings loading (simplified) ─────────────────────────────────────

def test_load_settings_reads_regime_strategies(tmp_path, monkeypatch):
    import run_backtest as rb
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "regime:\n  long_strategy: LongBot\n  short_strategy: ShortBot\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    cfg = rb._load_settings()
    assert cfg["regime_long_strategy"] == "LongBot"
    assert cfg["regime_short_strategy"] == "ShortBot"


def test_load_settings_regime_defaults(tmp_path, monkeypatch):
    import run_backtest as rb
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("credentials:\n  user_id: test\n", encoding="utf-8")
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    cfg = rb._load_settings()
    assert cfg.get("regime_long_strategy", "") == ""
    assert cfg.get("regime_short_strategy", "") == ""


def test_load_settings_threshold_keys_loaded(tmp_path, monkeypatch):
    """Regime threshold keys are loaded from settings.yaml."""
    import run_backtest as rb
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "regime:\n  adx_enter: 30\n  long_strategy: X\n  short_strategy: Y\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    cfg = rb._load_settings()
    assert cfg["regime_adx_enter"] == 30.0
    assert cfg["regime_long_strategy"] == "X"
    assert "regime_enabled" not in cfg
    assert "regime_dry_run" not in cfg
