"""Strategy selector — maps an effective regime to a recommendation.

Phase 1 is advisory only: every Recommendation carries ``dry_run`` and is
logged / announced but never auto-deployed. The selector reads the state's
``last_features`` (the classifier ``to_dict()`` plus the ``_paused`` /
``_vol_spike`` flags stamped by the state machine).
"""

from dataclasses import dataclass

from .state_machine import RegimeState, RegimeConfig


@dataclass
class Recommendation:
    action: str           # "deploy_long" | "deploy_short" | "sit_out" | "hold" | "deploy_short_half"
    strategy_name: str    # "" for sit_out/hold
    qty_scale: float = 1.0
    reason: str = ""
    dry_run: bool = True


class StrategySelector:
    def select(self, state: RegimeState, cfg: RegimeConfig) -> Recommendation:
        dry = cfg.dry_run

        # Manual override (one-shot)
        if state.manual_override != "auto":
            action_map = {"long": "deploy_long", "short": "deploy_short", "sit_out": "sit_out"}
            action = action_map.get(state.manual_override, "hold")
            name = cfg.long_strategy if action == "deploy_long" else (cfg.short_strategy if action == "deploy_short" else "")
            return Recommendation(action, name, reason=f"manual override: {state.manual_override}", dry_run=dry)

        # Paused
        if state.last_features.get("_paused"):
            return Recommendation("hold", "", reason="flip-counter pause active", dry_run=dry)

        # Vol spike
        if state.last_features.get("_vol_spike"):
            return Recommendation("sit_out", "", reason="volatility spike — ATR ratio exceeded threshold", dry_run=dry)

        regime = state.effective_regime
        features = state.last_features

        if regime == "trending-up":
            return Recommendation("deploy_long", cfg.long_strategy, reason="trending-up confirmed", dry_run=dry)
        elif regime == "trending-down":
            return Recommendation("deploy_short", cfg.short_strategy, reason="trending-down confirmed", dry_run=dry)
        elif regime == "range-bound":
            bearish = features.get("ema_slope", 0) < 0 and features.get("direction") == "bearish"
            if bearish:
                if cfg.range_bias_action == "short_half":
                    return Recommendation("deploy_short_half", cfg.short_strategy, qty_scale=0.5,
                                         reason="range-bound with bearish bias — half size", dry_run=dry)
                return Recommendation("sit_out", "", reason="range-bound with bearish bias — sit out", dry_run=dry)
            return Recommendation("sit_out", "", reason="range-bound — neutral/bullish, sit out", dry_run=dry)
        else:
            return Recommendation("hold", "", reason=f"unknown/transitional regime: {regime}", dry_run=dry)
