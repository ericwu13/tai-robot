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


def test_event_risk_sits_out_with_event_name():
    sel = StrategySelector()
    s = RegimeState(effective_regime="trending-up",
                    last_features={"_event_risk": "US CPI"})
    rec = sel.select(s, cfg())
    assert rec.action == "sit_out"
    assert rec.strategy_name == ""
    assert "US CPI" in rec.reason


def test_event_risk_outranks_vol_spike():
    """Both sit out, but the reason must name the scheduled event."""
    sel = StrategySelector()
    s = RegimeState(effective_regime="trending-down",
                    last_features={"_event_risk": "FOMC", "_vol_spike": True})
    rec = sel.select(s, cfg())
    assert rec.action == "sit_out"
    assert "FOMC" in rec.reason


def test_pause_precedes_event_risk():
    sel = StrategySelector()
    s = RegimeState(effective_regime="trending-up",
                    last_features={"_paused": True, "_event_risk": "US CPI"})
    rec = sel.select(s, cfg())
    assert rec.action == "hold"


def test_override_precedes_event_risk():
    sel = StrategySelector()
    s = RegimeState(manual_override="long",
                    last_features={"_event_risk": "US CPI"})
    rec = sel.select(s, cfg())
    assert rec.action == "deploy_long"


def test_falsy_event_risk_does_not_gate():
    sel = StrategySelector()
    for value in ("", None, False):
        s = RegimeState(effective_regime="trending-up",
                        last_features={"_event_risk": value})
        assert sel.select(s, cfg()).action == "deploy_long"


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


# ── Range-bound information fusion: external W2/W3 votes ──

def _range_state(slope, direction="bullish", votes=None, extra=None):
    feats = {"ema_slope": slope, "direction": direction}
    if votes is not None:
        feats["_votes"] = votes
    if extra:
        feats.update(extra)
    return RegimeState(effective_regime="range-bound", last_features=feats)


def test_range_bound_vote_up_with_updrift_deploys_long_half():
    """A bullish external vote + positive drift beats sit_out — even with
    range_bias_action=sit_out (the fusion path bypasses that knob)."""
    sel = StrategySelector()
    s = _range_state(slope=101.0, votes=["trending-up"])
    rec = sel.select(s, cfg(range_bias_action="sit_out"))
    assert rec.action == "deploy_long_half"
    assert rec.strategy_name == "LongBot"
    assert rec.qty_scale == 0.5


def test_range_bound_vote_down_with_downdrift_deploys_short_half():
    sel = StrategySelector()
    s = _range_state(slope=-50.0, direction="bearish", votes=["trending-down"])
    rec = sel.select(s, cfg(range_bias_action="sit_out"))
    assert rec.action == "deploy_short_half"
    assert rec.strategy_name == "ShortBot"
    assert rec.qty_scale == 0.5


def test_range_bound_vote_against_drift_ignored():
    """Vote up but price drifting down → external and local evidence
    disagree → sit out."""
    sel = StrategySelector()
    s = _range_state(slope=-50.0, direction="bearish", votes=["trending-up"])
    rec = sel.select(s, cfg())
    assert rec.action == "sit_out"


def test_range_bound_conflicting_votes_cancel():
    """W2 says up, W3 says down → ambiguous external evidence → falls
    back to the technical-bias-only path (sit_out by default)."""
    sel = StrategySelector()
    s = _range_state(slope=101.0, votes=["trending-up", "trending-down"])
    rec = sel.select(s, cfg(range_bias_action="sit_out"))
    assert rec.action == "sit_out"


def test_range_bound_vote_needs_only_slope_agreement():
    """The DI-derived direction field may disagree (low-signal when ADX
    is low) — slope agreement alone qualifies the vote."""
    sel = StrategySelector()
    s = _range_state(slope=98.8, direction="bearish", votes=["trending-up"])
    rec = sel.select(s, cfg())
    assert rec.action == "deploy_long_half"


def test_vote_does_not_override_vol_spike():
    sel = StrategySelector()
    s = _range_state(slope=101.0, votes=["trending-up"],
                     extra={"_vol_spike": True})
    rec = sel.select(s, cfg())
    assert rec.action == "sit_out"
    assert "volatility" in rec.reason


def test_vote_does_not_override_event_risk():
    sel = StrategySelector()
    s = _range_state(slope=101.0, votes=["trending-up"],
                     extra={"_event_risk": "FOMC"})
    rec = sel.select(s, cfg())
    assert rec.action == "sit_out"
    assert "FOMC" in rec.reason


def test_trending_up_ignores_votes_stays_full_size():
    """Votes only matter in range-bound; a confirmed trend deploys full."""
    sel = StrategySelector()
    s = RegimeState(effective_regime="trending-up",
                    last_features={"_votes": ["trending-down"]})
    rec = sel.select(s, cfg())
    assert rec.action == "deploy_long"
    assert rec.qty_scale == 1.0


# ── range_bias_action: technical bias alone (no votes) ──

def test_range_bound_bullish_long_half():
    sel = StrategySelector()
    s = _range_state(slope=101.0, direction="bullish")
    rec = sel.select(s, cfg(range_bias_action="long_half"))
    assert rec.action == "deploy_long_half"
    assert rec.strategy_name == "LongBot"
    assert rec.qty_scale == 0.5


def test_range_bound_both_half_bullish():
    sel = StrategySelector()
    s = _range_state(slope=101.0, direction="bullish")
    rec = sel.select(s, cfg(range_bias_action="both_half"))
    assert rec.action == "deploy_long_half"


def test_range_bound_both_half_bearish():
    sel = StrategySelector()
    s = _range_state(slope=-50.0, direction="bearish")
    rec = sel.select(s, cfg(range_bias_action="both_half"))
    assert rec.action == "deploy_short_half"


def test_range_bound_long_half_needs_full_bias():
    """Technical-bias-only path (unlike the vote path) requires slope AND
    direction to agree — no external evidence to lean on."""
    sel = StrategySelector()
    s = _range_state(slope=101.0, direction="bearish")
    rec = sel.select(s, cfg(range_bias_action="long_half"))
    assert rec.action == "sit_out"
