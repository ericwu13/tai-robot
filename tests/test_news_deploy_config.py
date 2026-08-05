"""Per-bot news enablement: the deploy-time NewsConfig resolver.

News enablement must be PER-BOT — the rollout runs a news-enabled paper
bot beside an untouched baseline regime bot, and a global switch would
let one ``risk_off`` flatten both. ``_resolve_news_config`` is the single
decision point: settings.yaml supplies paths and remembered defaults, the
deploy dialog's checkboxes decide.

Follows the test_regime_ui_settings.py / test_deploy_regime_integration.py
pattern — no Tk root needed.
"""

import os

import pytest

import run_backtest as rb


BASE = os.path.join("C:", os.sep, "bots")


def _settings(**overrides):
    s = {
        "news_enabled": True,          # remembered dialog default only
        "news_signal_path": "sig.json",
        "news_events_path": "ev.json",
        "news_ledger_path": "",
        "news_max_signal_age_sec": 900,
        "news_tier2_enabled": True,    # remembered dialog default only
        "news_calendar_min_severity": "high",
    }
    s.update(overrides)
    return s


def _resolve(settings=None, *, regime=True, news=True, tier2=False):
    return rb._resolve_news_config(
        settings if settings is not None else _settings(),
        regime_enabled=regime, news_enabled=news, tier2_enabled=tier2,
        base_dir=BASE,
    )


# ── Gating ──

class TestGating:
    def test_regime_off_is_none_even_with_news_ticked(self):
        """The breaker rides RegimeSwitchingRunner's poll — no regime, no news."""
        assert _resolve(regime=False, news=True) is None

    def test_regime_off_and_news_off_is_none(self):
        assert _resolve(regime=False, news=False) is None

    def test_news_checkbox_off_is_none(self):
        assert _resolve(regime=True, news=False) is None

    def test_both_on_returns_config(self):
        cfg = _resolve(regime=True, news=True)
        assert cfg is not None
        assert cfg.enabled is True

    def test_settings_enabled_alone_does_not_enable(self):
        """The global-flag regression: settings said yes, this bot did not."""
        cfg = _resolve(_settings(news_enabled=True), regime=True, news=False)
        assert cfg is None

    def test_settings_disabled_does_not_veto_the_checkbox(self):
        """settings.news.enabled is a remembered default, not a gate."""
        cfg = _resolve(_settings(news_enabled=False), regime=True, news=True)
        assert cfg is not None and cfg.enabled is True

    def test_two_bots_resolve_independently(self):
        """One settings dict, two deploys — only the ticked one is armed."""
        settings = _settings()
        baseline = _resolve(settings, news=False)
        news_bot = _resolve(settings, news=True, tier2=True)
        assert baseline is None
        assert news_bot is not None and news_bot.tier2_enabled is True


# ── Tier 2 comes from THIS deploy ──

class TestTier2PerDeploy:
    def test_tier2_from_deploy_not_settings_when_off(self):
        cfg = _resolve(_settings(news_tier2_enabled=True), tier2=False)
        assert cfg.tier2_enabled is False

    def test_tier2_from_deploy_not_settings_when_on(self):
        cfg = _resolve(_settings(news_tier2_enabled=False), tier2=True)
        assert cfg.tier2_enabled is True

    @pytest.mark.parametrize("raw,expected", [(1, True), (0, False),
                                              ("", False), (None, False)])
    def test_tier2_is_coerced_to_bool(self, raw, expected):
        assert _resolve(tier2=raw).tier2_enabled is expected


# ── Shared fields still come from settings ──

class TestSharedFields:
    def test_paths_and_limits_come_from_settings(self):
        cfg = _resolve(_settings(news_max_signal_age_sec=120,
                                 news_calendar_min_severity="medium"))
        assert cfg.max_signal_age_sec == 120
        assert cfg.calendar_min_severity == "medium"

    def test_relative_paths_resolve_against_base_dir(self):
        cfg = _resolve()
        assert cfg.signal_path == os.path.join(BASE, "sig.json")
        assert cfg.events_path == os.path.join(BASE, "ev.json")

    def test_absolute_paths_pass_through(self):
        abs_path = os.path.join("D:", os.sep, "n8n", "signal.json")
        cfg = _resolve(_settings(news_signal_path=abs_path))
        assert cfg.signal_path == abs_path

    def test_empty_ledger_path_stays_empty(self):
        """The runner turns "" into a bot-dir default; don't pre-empt it."""
        assert _resolve().ledger_path == ""

    def test_missing_settings_keys_fall_back_to_newsconfig_defaults(self):
        from src.config.settings import NewsConfig
        cfg = rb._resolve_news_config(
            {}, regime_enabled=True, news_enabled=True, tier2_enabled=False,
            base_dir=BASE)
        default = NewsConfig()
        assert cfg.signal_path == ""
        assert cfg.events_path == ""
        assert cfg.max_signal_age_sec == default.max_signal_age_sec
        assert cfg.calendar_min_severity == default.calendar_min_severity


# ── settings.yaml round-trip (defaults only) ──

class TestSettingsDefaults:
    def test_load_settings_reads_the_news_section(self, tmp_path, monkeypatch):
        (tmp_path / "settings.yaml").write_text(
            "news:\n"
            "  enabled: true\n"
            "  signal_path: sig.json\n"
            "  events_path: ev.json\n"
            "  ledger_path: led.json\n"
            "  max_signal_age_sec: 300\n"
            "  tier2_enabled: true\n"
            '  calendar_min_severity: "medium"\n',
            encoding="utf-8")
        monkeypatch.setattr(rb, "project_root", str(tmp_path))
        cfg = rb._load_settings()
        assert cfg["news_enabled"] is True
        assert cfg["news_signal_path"] == "sig.json"
        assert cfg["news_events_path"] == "ev.json"
        assert cfg["news_ledger_path"] == "led.json"
        assert cfg["news_max_signal_age_sec"] == 300
        assert cfg["news_tier2_enabled"] is True
        assert cfg["news_calendar_min_severity"] == "medium"

    def test_load_settings_news_defaults(self, tmp_path, monkeypatch):
        (tmp_path / "settings.yaml").write_text(
            "credentials:\n  user_id: test\n", encoding="utf-8")
        monkeypatch.setattr(rb, "project_root", str(tmp_path))
        cfg = rb._load_settings()
        assert cfg["news_enabled"] is False
        assert cfg["news_tier2_enabled"] is False
        assert cfg["news_signal_path"] == ""
        assert cfg["news_max_signal_age_sec"] == 900
        assert cfg["news_calendar_min_severity"] == "high"

    def test_remembered_defaults_feed_the_dialog_not_the_gate(
            self, tmp_path, monkeypatch):
        """Settings say enabled+tier2; a deploy that unticks both gets None."""
        (tmp_path / "settings.yaml").write_text(
            "news:\n  enabled: true\n  tier2_enabled: true\n", encoding="utf-8")
        monkeypatch.setattr(rb, "project_root", str(tmp_path))
        settings = rb._load_settings()
        # The dialog would pre-tick from these...
        assert settings["news_enabled"] is True
        assert settings["news_tier2_enabled"] is True
        # ...but the deploy's own answer is what resolves.
        assert rb._resolve_news_config(
            settings, regime_enabled=True, news_enabled=False,
            tier2_enabled=False, base_dir=BASE) is None


class TestResolveNewsPath:
    def test_empty_stays_empty(self):
        assert rb._resolve_news_path("", BASE) == ""

    def test_defaults_to_project_root(self, monkeypatch):
        monkeypatch.setattr(rb, "project_root", BASE)
        assert rb._resolve_news_path("x.json") == os.path.join(BASE, "x.json")
