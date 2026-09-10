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

        # Paused. The pause freezes trend ENTRIES, so a paused engine
        # that is still holding a trend holds its deployment. But the
        # machine can now EXIT to range-bound while paused, and a
        # range-bound read must reach its own branch (sit_out / probe) —
        # holding there would keep the dead leg deployed for the whole
        # pause window, which is exactly what this change removes.
        if state.last_features.get("_paused") and state.effective_regime != "range-bound":
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
            # Information fusion (news framework): unambiguous external
            # votes (W2 cross-market / W3 RSS / W4 chips) that agree with
            # the local price drift upgrade a range-bound sit-out to a
            # half-size probe. ±DI direction is deliberately NOT
            # required — with ADX below adx_exit the DI readings are
            # low-signal, and the votes themselves are the directional
            # evidence.
            #
            # The supporting side must meet its QUORUM (asymmetric: two
            # sources to probe long, one to probe short — see
            # RegimeConfig), while conflict-cancel stays absolute: ANY
            # opposing vote, quorum or not, kills the probe.
            votes = features.get("_votes") or []
            ups = sum(1 for v in votes if v == "trending-up")
            downs = sum(1 for v in votes if v == "trending-down")
            vote_up = cfg.vote_quorum_up > 0 and ups >= cfg.vote_quorum_up
            vote_down = cfg.vote_quorum_down > 0 and downs >= cfg.vote_quorum_down
            if vote_up and not downs and slope > 0:
                return Recommendation("deploy_long_half", cfg.long_strategy, qty_scale=0.5,
                                     reason="盤整+外部看多票 — 半倉多單 range-bound + external bullish vote — half-size long")
            if vote_down and not ups and slope < 0:
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
