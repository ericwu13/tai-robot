"""Tests for the Ultra Mode toggle: confirmation guard + settings persistence.

run_backtest.py is safe to import in tests — all COM/Tk work happens in
main() / _init_com(), module level is imports and definitions only.
"""

import yaml

import run_backtest as rb


class TestUltraToggleDecision:
    def test_off_to_on_requires_confirmation(self):
        assert rb._ultra_toggle_decision(False, lambda: True) is True

    def test_off_to_on_cancelled_returns_none(self):
        assert rb._ultra_toggle_decision(False, lambda: False) is None

    def test_on_to_off_immediate_never_asks(self):
        calls = []

        def confirm():
            calls.append(1)
            return True

        assert rb._ultra_toggle_decision(True, confirm) is False
        assert calls == []  # disable must NOT show a dialog


class TestUltraModePersistence:
    def test_roundtrip_true_then_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rb, "project_root", str(tmp_path))
        rb._save_ai_settings(ultra_mode=True)
        data = yaml.safe_load((tmp_path / "settings.yaml").read_text(encoding="utf-8"))
        assert data["ai"]["ultra_mode"] is True
        # False must persist too — a truthiness guard would silently skip it
        rb._save_ai_settings(ultra_mode=False)
        data = yaml.safe_load((tmp_path / "settings.yaml").read_text(encoding="utf-8"))
        assert data["ai"]["ultra_mode"] is False

    def test_unrelated_save_preserves_ultra_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rb, "project_root", str(tmp_path))
        rb._save_ai_settings(ultra_mode=True)
        rb._save_ai_settings(model="gemini-2.5-pro")  # ultra_mode=None default
        data = yaml.safe_load((tmp_path / "settings.yaml").read_text(encoding="utf-8"))
        assert data["ai"]["ultra_mode"] is True
        assert data["ai"]["model"] == "gemini-2.5-pro"

    def test_load_settings_reads_persisted_value(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rb, "project_root", str(tmp_path))
        monkeypatch.delenv("ULTRA_MODE", raising=False)  # env wins — keep it out
        rb._save_ai_settings(ultra_mode=True)
        cfg = rb._load_settings()
        assert cfg["ultra_mode"] is True

    def test_env_var_overrides_persisted_value(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rb, "project_root", str(tmp_path))
        rb._save_ai_settings(ultra_mode=True)
        monkeypatch.setenv("ULTRA_MODE", "0")
        cfg = rb._load_settings()
        assert cfg["ultra_mode"] is False
