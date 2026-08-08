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
    action: str           # "deploy_long" | "deploy_short" | "sit_out" | "hold" | "deploy_short_half" | "deploy_long_half"
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
            slope = features.get("ema_slope", 0)
            # Information fusion (news framework): an unambiguous external
            # vote (W2 cross-market / W3 RSS) that agrees with the local
            # price drift upgrades a range-bound sit-out to a half-size
            # probe. Conflicting votes cancel each other; a vote against
            # the drift is ignored. ±DI direction is deliberately NOT
            # required — with ADX below adx_exit the DI readings are
            # low-signal, and the vote itself is the directional evidence.
            votes = features.get("_votes") or []
            vote_up = "trending-up" in votes
            vote_down = "trending-down" in votes
            if vote_up and not vote_down and slope > 0:
                return Recommendation("deploy_long_half", cfg.long_strategy, qty_scale=0.5,
                                     reason="盤整+外部看多票 — 半倉多單 range-bound + external bullish vote — half-size long")
            if vote_down and not vote_up and slope < 0:
                return Recommendation("deploy_short_half", cfg.short_strategy, qty_scale=0.5,
                                     reason="盤整+外部看空票 — 半倉空單 range-bound + external bearish vote — half-size short")

            bearish = slope < 0 and features.get("direction") == "bearish"
            bullish = slope > 0 and features.get("direction") == "bullish"
            if bearish and cfg.range_bias_action in ("short_half", "both_half"):
                return Recommendation("deploy_short_half", cfg.short_strategy, qty_scale=0.5,
                                     reason="盤整偏空 — 半倉 range-bound with bearish bias — half size")
            if bullish and cfg.range_bias_action in ("long_half", "both_half"):
                return Recommendation("deploy_long_half", cfg.long_strategy, qty_scale=0.5,
                                     reason="盤整偏多 — 半倉 range-bound with bullish bias — half size")
            if bearish:
                return Recommendation("sit_out", "", reason="盤整偏空 — 觀望 range-bound with bearish bias — sit out")
            return Recommendation("sit_out", "", reason="盤整中性/偏多 — 觀望 range-bound — neutral/bullish, sit out")
        else:
            return Recommendation("hold", "", reason=f"未知/過渡期 unknown/transitional regime: {regime}")
