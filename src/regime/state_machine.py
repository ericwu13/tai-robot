"""Regime state machine — Phase 1 (shadow mode).

Advances a persistent regime assessment one NIGHT session at a time,
applying hysteresis (confirmation streaks), a strong-ADX fast-track, a
volatility-spike flag, and a flip-counter pause. The machine only reads
the classifier's raw features; it never places orders. See
``src.regime.selector`` for how the effective regime maps to a
recommendation.
"""

from dataclasses import dataclass, field

from src.daily_report.regime_classifier import RegimeResult


@dataclass
class RegimeConfig:
    enabled: bool = False
    dry_run: bool = True
    adx_enter: float = 25.0
    adx_exit: float = 20.0
    confirm_sessions: int = 2
    vol_spike_ratio: float = 1.5
    max_flips: int = 3
    flip_window: int = 10
    pause_sessions: int = 5
    long_strategy: str = ""
    short_strategy: str = ""
    range_bias_action: str = "sit_out"   # "sit_out" | "short_half"
    manual_override: str = "auto"        # "auto" | "long" | "short" | "sit_out"
    classify_interval: int = 3600


@dataclass
class RegimeState:
    effective_regime: str = "unknown"    # "trending-up" | "trending-down" | "range-bound" | "unknown"
    effective_since: str = ""
    raw_regime: str = "unknown"
    pending_label: str = ""
    pending_count: int = 0
    flip_history: list = field(default_factory=list)
    paused_until_session: int = 0       # session index counter
    session_count: int = 0
    manual_override: str = "auto"
    last_assessed: str = ""
    last_features: dict = field(default_factory=dict)


class RegimeStateMachine:
    def step(self, state: RegimeState, result: RegimeResult, cfg: RegimeConfig, session_date: str) -> RegimeState:
        """Advance state by one NIGHT session. Returns new state (immutable-ish)."""
        import copy
        s = copy.deepcopy(state)
        s.session_count += 1
        s.last_assessed = session_date
        s.last_features = result.to_dict()

        # --- 1. Derive raw regime from configurable thresholds ---
        # RegimeResult has no trend_direction field; derive it from the
        # directional indicators (+DI vs -DI), matching how the ADX system
        # itself defines up/down.
        adx = result.adx_value
        trend_direction = "up" if result.plus_di_value >= result.minus_di_value else "down"
        if adx > cfg.adx_enter:
            if trend_direction == "down":
                raw = "trending-down"
            elif trend_direction == "up":
                raw = "trending-up"
            else:
                raw = "transitional"
        elif adx < cfg.adx_exit:
            raw = "range-bound"
        else:
            raw = "transitional"   # dead zone 20-25
        s.raw_regime = raw

        # --- 2. Vol spike override (does not change effective regime) ---
        vol_spike = result.atr_ratio > cfg.vol_spike_ratio

        # --- 3. Flip-counter pause check ---
        if s.paused_until_session > 0 and s.session_count <= s.paused_until_session:
            s.last_features["_paused"] = True
            s.last_features["_vol_spike"] = vol_spike
            return s   # frozen

        # --- 4. Hysteresis confirmation ---
        if raw == "transitional":
            s.pending_count = 0   # reset streak but don't change effective
        else:
            strong = adx > 30
            if raw == s.pending_label:
                s.pending_count += 1
            else:
                s.pending_label = raw
                s.pending_count = 1

            if raw != s.effective_regime and (s.pending_count >= cfg.confirm_sessions or strong):
                # Regime change confirmed
                s.effective_regime = raw
                s.effective_since = session_date
                s.pending_count = 0
                s.flip_history.append(session_date)
                # Trim to flip_window
                s.flip_history = s.flip_history[-cfg.flip_window:]
                # Check if flip count triggers pause
                if len(s.flip_history) >= cfg.max_flips:
                    s.paused_until_session = s.session_count + cfg.pause_sessions

        s.last_features["_vol_spike"] = vol_spike
        return s
