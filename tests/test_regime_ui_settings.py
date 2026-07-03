"""Tests for the Regime Mode config UI save/validate logic (Phase 2).

No GUI is exercised here — only the pure helpers and the settings
round-trip in ``BacktestApp._save_regime_settings`` (called as an unbound
method against a lightweight stand-in so no Tk root is needed).
"""

import os

import pytest

import run_backtest as rb


# ── Validation ───────────────────────────────────────────────────────────

def test_validate_accepts_sane_values():
    ok, title, msg = rb._validate_regime_values(
        enabled=True, adx_enter=25, adx_exit=20, confirm_sessions=2,
        vol_spike_ratio=1.5, long_strategy="LongA", short_strategy="ShortB")
    assert ok is True
    assert title == "" and msg == ""


def test_validate_rejects_adx_enter_equal_exit():
    ok, title, msg = rb._validate_regime_values(
        enabled=True, adx_enter=20, adx_exit=20, confirm_sessions=2,
        vol_spike_ratio=1.5, long_strategy="LongA", short_strategy="ShortB")
    assert ok is False
    assert "enter must be greater" in msg.lower()


def test_validate_rejects_adx_enter_below_exit():
    ok, _, msg = rb._validate_regime_values(
        enabled=True, adx_enter=15, adx_exit=20, confirm_sessions=2,
        vol_spike_ratio=1.5, long_strategy="LongA", short_strategy="ShortB")
    assert ok is False
    assert "enter must be greater" in msg.lower()


def test_validate_rejects_confirm_below_one():
    ok, _, msg = rb._validate_regime_values(
        enabled=True, adx_enter=25, adx_exit=20, confirm_sessions=0,
        vol_spike_ratio=1.5, long_strategy="LongA", short_strategy="ShortB")
    assert ok is False
    assert "confirm" in msg.lower()


def test_validate_rejects_missing_strategy_when_enabled():
    ok, _, msg = rb._validate_regime_values(
        enabled=True, adx_enter=25, adx_exit=20, confirm_sessions=2,
        vol_spike_ratio=1.5, long_strategy="", short_strategy="ShortB")
    assert ok is False
    assert "strateg" in msg.lower()


def test_validate_allows_missing_strategy_when_disabled():
    ok, title, msg = rb._validate_regime_values(
        enabled=False, adx_enter=25, adx_exit=20, confirm_sessions=2,
        vol_spike_ratio=1.5, long_strategy="", short_strategy="")
    assert ok is True


def test_validate_rejects_non_numeric_threshold():
    ok, _, msg = rb._validate_regime_values(
        enabled=True, adx_enter="abc", adx_exit=20, confirm_sessions=2,
        vol_spike_ratio=1.5, long_strategy="A", short_strategy="B")
    assert ok is False


def test_validate_rejects_non_positive_vol_spike():
    ok, _, msg = rb._validate_regime_values(
        enabled=True, adx_enter=25, adx_exit=20, confirm_sessions=2,
        vol_spike_ratio=0, long_strategy="A", short_strategy="B")
    assert ok is False


# ── Pure block helpers ───────────────────────────────────────────────────

def test_yaml_block_casts_types():
    block = rb._regime_yaml_block({
        "enabled": 1, "dry_run": 0,
        "long_strategy": "LongA", "short_strategy": "ShortB",
        "range_bias_action": "short_half",
        "adx_enter": "27.5", "adx_exit": "18",
        "confirm_sessions": "3", "vol_spike_ratio": "2.0",
    })
    assert block["enabled"] is True
    assert block["dry_run"] is False
    assert block["adx_enter"] == 27.5 and isinstance(block["adx_enter"], float)
    assert block["adx_exit"] == 18.0
    assert block["confirm_sessions"] == 3 and isinstance(block["confirm_sessions"], int)
    assert block["vol_spike_ratio"] == 2.0
    assert block["range_bias_action"] == "short_half"


def test_block_to_flat_round_trips():
    block = rb._regime_yaml_block({
        "enabled": True, "dry_run": True,
        "long_strategy": "LongA", "short_strategy": "ShortB",
        "range_bias_action": "sit_out",
        "adx_enter": 25.0, "adx_exit": 20.0,
        "confirm_sessions": 2, "vol_spike_ratio": 1.5,
    })
    flat = rb._regime_block_to_flat_settings(block)
    assert flat["regime_enabled"] is True
    assert flat["regime_long_strategy"] == "LongA"
    assert flat["regime_short_strategy"] == "ShortB"
    assert flat["regime_adx_enter"] == 25.0
    assert flat["regime_adx_exit"] == 20.0
    assert flat["regime_confirm_sessions"] == 2
    assert flat["regime_vol_spike_ratio"] == 1.5
    assert flat["regime_range_bias_action"] == "sit_out"


# ── _save_regime_settings round-trip (no Tk) ─────────────────────────────

class _FakeVar:
    """Stand-in for a Tk variable — just holds a value behind .get()."""
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class _FakeStatusVar:
    def __init__(self):
        self.value = None

    def set(self, v):
        self.value = v


class _FakeApp:
    """Minimal stand-in exposing the attributes _save_regime_settings touches."""
    def __init__(self):
        self._settings = {}
        self._regime_manager = None
        self.status_var = _FakeStatusVar()

    def _update_regime_status(self):
        pass


def _vars(**overrides):
    base = {
        "enabled": _FakeVar(True), "dry_run": _FakeVar(True),
        "long_strategy": _FakeVar("LongA"), "short_strategy": _FakeVar("ShortB"),
        "range_bias_action": _FakeVar("sit_out"), "adx_enter": _FakeVar("26"),
        "adx_exit": _FakeVar("19"), "confirm_sessions": _FakeVar("3"),
        "vol_spike_ratio": _FakeVar("1.8"), "override": _FakeVar("auto"),
    }
    base.update(overrides)
    return base


def test_save_writes_yaml_and_updates_settings(tmp_path, monkeypatch):
    # Redirect the settings.yaml target to a temp dir.
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    app = _FakeApp()

    ok = rb.BacktestApp._save_regime_settings(app, _vars())
    assert ok is True

    # In-memory settings reflect the saved values (flat keys).
    assert app._settings["regime_enabled"] is True
    assert app._settings["regime_long_strategy"] == "LongA"
    assert app._settings["regime_adx_enter"] == 26.0
    assert app._settings["regime_adx_exit"] == 19.0
    assert app._settings["regime_confirm_sessions"] == 3
    assert app._settings["regime_vol_spike_ratio"] == 1.8

    # settings.yaml on disk round-trips back to the same block.
    path = os.path.join(str(tmp_path), "settings.yaml")
    assert os.path.exists(path)
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    reg = data["regime"]
    assert reg["enabled"] is True
    assert reg["adx_enter"] == 26.0
    assert reg["short_strategy"] == "ShortB"
    assert reg["confirm_sessions"] == 3


def test_save_merges_into_existing_yaml(tmp_path, monkeypatch):
    # Pre-existing settings.yaml with an unrelated section must survive.
    import yaml
    path = os.path.join(str(tmp_path), "settings.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump({"credentials": {"user_id": "F1111111"}}, f)

    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    app = _FakeApp()
    assert rb.BacktestApp._save_regime_settings(app, _vars()) is True

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["credentials"]["user_id"] == "F1111111"  # untouched
    assert data["regime"]["enabled"] is True


def test_save_rejected_on_bad_thresholds(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    # Capture the showerror instead of opening a Tk dialog.
    calls = []
    monkeypatch.setattr(rb.messagebox, "showerror",
                        lambda *a, **k: calls.append((a, k)))
    app = _FakeApp()

    # adx_enter <= adx_exit → rejected, nothing written.
    ok = rb.BacktestApp._save_regime_settings(
        app, _vars(adx_enter=_FakeVar("18"), adx_exit=_FakeVar("22")))
    assert ok is False
    assert calls, "expected showerror to be called on validation failure"
    assert not os.path.exists(os.path.join(str(tmp_path), "settings.yaml"))
    assert "regime_enabled" not in app._settings
