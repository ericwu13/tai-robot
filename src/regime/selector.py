"""Strategy selector — maps an effective regime to a recommendation.

The selector reads the state's ``last_features`` (the classifier
``to_dict()`` plus the ``_paused`` / ``_vol_spike`` flags stamped by the
state machine) and returns a Recommendation that the switching runner
applies at session boundaries.
"""

from dataclasses import dataclass

from .state_machine import RegimeState, RegimeConfig


@dataclass
class Recommendation:
    action: str           # "deploy_long" | "deploy_short" | "sit_out" | "hold" | "deploy_short_half"
    strategy_name: str    # "" for sit_out/hold
    qty_scale: float = 1.0
    reason: str = ""


class StrategySelector:
    def select(self, state: RegimeState, cfg: RegimeConfig) -> Recommendation:
        # Manual override (one-shot)
        if state.manual_override != "auto":
            action_map = {"long": "deploy_long", "short": "deploy_short", "sit_out": "sit_out"}
            action = action_map.get(state.manual_override, "hold")
            name = cfg.long_strategy if action == "deploy_long" else (cfg.short_strategy if action == "deploy_short" else "")
            return Recommendation(action, name, reason=f"manual override: {state.manual_override}")

        # Paused
        if state.last_features.get("_paused"):
            return Recommendation("hold", "", reason="flip-counter pause active")

        # Vol spike
        if state.last_features.get("_vol_spike"):
            return Recommendation("sit_out", "", reason="volatility spike — ATR ratio exceeded threshold")

        regime = state.effective_regime
        features = state.last_features

        if regime == "trending-up":
            return Recommendation("deploy_long", cfg.long_strategy, reason="trending-up confirmed")
        elif regime == "trending-down":
            return Recommendation("deploy_short", cfg.short_strategy, reason="trending-down confirmed")
        elif regime == "range-bound":
            bearish = features.get("ema_slope", 0) < 0 and features.get("direction") == "bearish"
            if bearish:
                if cfg.range_bias_action == "short_half":
                    return Recommendation("deploy_short_half", cfg.short_strategy, qty_scale=0.5,
                                         reason="range-bound with bearish bias — half size")
                return Recommendation("sit_out", "", reason="range-bound with bearish bias — sit out")
            return Recommendation("sit_out", "", reason="range-bound — neutral/bullish, sit out")
        else:
            return Recommendation("hold", "", reason=f"unknown/transitional regime: {regime}")
