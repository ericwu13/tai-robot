"""Strategy selector — maps an effective regime to a recommendation.

The selector reads the state's ``last_features`` (the classifier
``to_dict()`` plus the ``_paused`` / ``_vol_spike`` flags stamped by the
state machine, and ``_event_risk`` stamped from the news calendar) and
returns a Recommendation that the switching runner applies at session
boundaries.
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
            return Recommendation(action, name, reason=f"手動覆寫 manual override: {state.manual_override}")

        # Paused
        if state.last_features.get("_paused"):
            return Recommendation("hold", "", reason="翻轉計數暫停中 flip-counter pause active")

        # Scheduled event risk — the flag value is the event NAME, stamped
        # from the news calendar. Deliberate gate: it outranks the vol-spike
        # heuristic below (both sit out, but this one is a known date).
        event = state.last_features.get("_event_risk")
        if event:
            return Recommendation(
                "sit_out", "",
                reason=f"重大事件迴避 scheduled event risk — sit out: {event}")

        # Vol spike
        if state.last_features.get("_vol_spike"):
            return Recommendation("sit_out", "", reason="波動突增 volatility spike — ATR ratio exceeded threshold")

        regime = state.effective_regime
        features = state.last_features

        if regime == "trending-up":
            return Recommendation("deploy_long", cfg.long_strategy, reason="上升趨勢確認 trending-up confirmed")
        elif regime == "trending-down":
            return Recommendation("deploy_short", cfg.short_strategy, reason="下降趨勢確認 trending-down confirmed")
        elif regime == "range-bound":
            bearish = features.get("ema_slope", 0) < 0 and features.get("direction") == "bearish"
            if bearish:
                if cfg.range_bias_action == "short_half":
                    return Recommendation("deploy_short_half", cfg.short_strategy, qty_scale=0.5,
                                         reason="盤整偏空 — 半倉 range-bound with bearish bias — half size")
                return Recommendation("sit_out", "", reason="盤整偏空 — 觀望 range-bound with bearish bias — sit out")
            return Recommendation("sit_out", "", reason="盤整中性/偏多 — 觀望 range-bound — neutral/bullish, sit out")
        else:
            return Recommendation("hold", "", reason=f"未知/過渡期 unknown/transitional regime: {regime}")
