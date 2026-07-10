"""Unit tests for the regime strategy selector (Phase 1 shadow mode)."""

from src.regime.state_machine import RegimeState, RegimeConfig
from src.regime.selector import StrategySelector, Recommendation


def cfg(**kw):
    base = dict(enabled=True, long_strategy="LongBot",
                short_strategy="ShortBot")
    base.update(kw)
    return RegimeConfig(**base)


def test_trending_up_deploys_long():
    sel = StrategySelector()
    s = RegimeState(effective_regime="trending-up")
    rec = sel.select(s, cfg())
    assert rec.action == "deploy_long"
    assert rec.strategy_name == "LongBot"


def test_trending_down_deploys_short():
    sel = StrategySelector()
    s = RegimeState(effective_regime="trending-down")
    rec = sel.select(s, cfg())
    assert rec.action == "deploy_short"
    assert rec.strategy_name == "ShortBot"


def test_range_bound_neutral_sits_out():
    sel = StrategySelector()
    s = RegimeState(effective_regime="range-bound",
                    last_features={"ema_slope": 5.0, "direction": "bullish"})
    rec = sel.select(s, cfg())
    assert rec.action == "sit_out"
    assert rec.strategy_name == ""


def test_range_bound_bearish_sits_out_by_default():
    sel = StrategySelector()
    s = RegimeState(effective_regime="range-bound",
                    last_features={"ema_slope": -5.0, "direction": "bearish"})
    rec = sel.select(s, cfg(range_bias_action="sit_out"))
    assert rec.action == "sit_out"


def test_range_bound_bearish_short_half():
    sel = StrategySelector()
    s = RegimeState(effective_regime="range-bound",
                    last_features={"ema_slope": -5.0, "direction": "bearish"})
    rec = sel.select(s, cfg(range_bias_action="short_half"))
    assert rec.action == "deploy_short_half"
    assert rec.strategy_name == "ShortBot"
    assert rec.qty_scale == 0.5


def test_paused_holds():
    sel = StrategySelector()
    s = RegimeState(effective_regime="trending-up",
                    last_features={"_paused": True})
    rec = sel.select(s, cfg())
    assert rec.action == "hold"


def test_vol_spike_sits_out():
    sel = StrategySelector()
    s = RegimeState(effective_regime="trending-up",
                    last_features={"_vol_spike": True})
    rec = sel.select(s, cfg())
    assert rec.action == "sit_out"


def test_unknown_regime_holds():
    sel = StrategySelector()
    s = RegimeState(effective_regime="unknown")
    rec = sel.select(s, cfg())
    assert rec.action == "hold"


def test_manual_override_long():
    sel = StrategySelector()
    s = RegimeState(effective_regime="range-bound", manual_override="long")
    rec = sel.select(s, cfg())
    assert rec.action == "deploy_long"
    assert rec.strategy_name == "LongBot"
    assert "manual override" in rec.reason


def test_manual_override_sit_out():
    sel = StrategySelector()
    s = RegimeState(effective_regime="trending-up", manual_override="sit_out")
    rec = sel.select(s, cfg())
    assert rec.action == "sit_out"
    assert rec.strategy_name == ""


def test_manual_override_consumed_after_one_step():
    """Override drives the first decision, then the manager resets it to
    'auto'; the next selection falls through to the regime logic."""
    sel = StrategySelector()
    s = RegimeState(effective_regime="trending-up", manual_override="sit_out")
    rec1 = sel.select(s, cfg())
    assert rec1.action == "sit_out"          # override wins
    # Manager consumes the one-shot override.
    s.manual_override = "auto"
    rec2 = sel.select(s, cfg())
    assert rec2.action == "deploy_long"      # back to regime-driven


def test_override_precedes_pause_and_vol_spike():
    sel = StrategySelector()
    s = RegimeState(manual_override="long",
                    last_features={"_paused": True, "_vol_spike": True})
    rec = sel.select(s, cfg())
    assert rec.action == "deploy_long"


def test_recommendation_has_no_dry_run():
    sel = StrategySelector()
    s = RegimeState(effective_regime="trending-up")
    rec = sel.select(s, cfg())
    assert not hasattr(rec, "dry_run")
