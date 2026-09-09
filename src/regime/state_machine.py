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


def _vote_quorum(cfg, direction: str) -> int:
    """Votes needed for *direction* ("trending-up" / "trending-down")."""
    if direction == "trending-down":
        return int(getattr(cfg, "vote_quorum_down", 1))
    return int(getattr(cfg, "vote_quorum_up", 2))


def vote_rule_tag(votes: list, raw: str, cfg) -> str:
    """Short got/needed audit string, e.g. ``"up:1/2"`` / ``"down:2/1"``.

    Empty when no votes were present at all — the history column then
    reads blank instead of claiming a rule was evaluated.  For a
    non-trending *raw* (transitional / range-bound, where the selector
    rather than the confirmation gate uses the votes) the majority side
    is reported.
    """
    if not votes:
        return ""
    ups = sum(1 for v in votes if v == "trending-up")
    downs = sum(1 for v in votes if v == "trending-down")
    if raw == "trending-up":
        side = "up"
    elif raw == "trending-down":
        side = "down"
    else:
        side = "down" if downs > ups else "up"
    got = ups if side == "up" else downs
    needed = _vote_quorum(cfg, f"trending-{side}")
    return f"{side}:{got}/{needed}"


@dataclass
class RegimeConfig:
    enabled: bool = False
    adx_enter: float = 25.0
    adx_exit: float = 20.0
    confirm_sessions: int = 2
    vol_spike_ratio: float = 1.5
    max_flips: int = 3
    flip_window: int = 10
    pause_sessions: int = 5
    # Fast-track threshold: a single session with ADX above this confirms
    # a regime change without waiting confirm_sessions. Only active while
    # adx_strong > adx_enter — otherwise every trending read would
    # fast-track and raising adx_enter would silently disable hysteresis.
    adx_strong: float = 30.0
    long_strategy: str = ""
    short_strategy: str = ""
    range_bias_action: str = "sit_out"   # "sit_out" | "short_half" | "long_half" | "both_half"
    # How many agreeing external votes it takes to accelerate a
    # confirmation (and, in the selector, to probe a range). Deliberately
    # ASYMMETRIC: entering long on external evidence is the expensive
    # mistake (W2, the loudest voter, is 3/10 right by the time the
    # nightly lane reads it), while confirming a DOWN read early is the
    # cheap, protective direction. 0 on either side disables votes for
    # that direction entirely.
    vote_quorum_up: int = 2
    vote_quorum_down: int = 1
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
    # session_count values at which flips were confirmed — the actual
    # sliding-window data for the flip-counter pause. flip_history keeps
    # the human-readable dates for display/state inspection.
    flip_sessions: list = field(default_factory=list)
    paused_until_session: int = 0       # session index counter
    session_count: int = 0
    manual_override: str = "auto"
    last_assessed: str = ""
    last_features: dict = field(default_factory=dict)


class RegimeStateMachine:
    def step(self, state: RegimeState, result: RegimeResult, cfg: RegimeConfig, session_date: str, vote_directions: list[str] | None = None, vote_sources: list[str] | None = None) -> RegimeState:
        """Advance state by one NIGHT session. Returns new state (immutable-ish).

        *vote_directions*: list of cross-market regime votes from
        independent sources (one per source — one file per source, so
        counting list entries counts sources).  Confirmation is
        immediate when the number agreeing with tonight's raw
        classification meets that direction's quorum
        (``cfg.vote_quorum_up`` / ``cfg.vote_quorum_down``).

        *vote_sources*: same votes as ``"SOURCE:direction"`` strings
        (e.g. ``"W3:trending-up"``) — audit only, parallel to
        ``vote_directions`` so the selector's membership tests on
        ``_votes`` keep their plain-direction elements.
        """
        import copy
        s = copy.deepcopy(state)
        s.session_count += 1
        s.last_assessed = session_date
        s.last_features = result.to_dict()
        # Persist tonight's external votes for the selector (information
        # fusion in range-bound regimes) and for post-hoc audit — the vote
        # files themselves are consumed right after classification.
        votes = [v for v in (vote_directions or []) if v]
        s.last_features["_votes"] = votes
        s.last_features["_vote_sources"] = [v for v in (vote_sources or []) if v]
        s.last_features["_vote_accelerated"] = False
        s.last_features["_vote_rule"] = ""

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
        # Stamped before the pause/transitional early-returns so the
        # audit column is populated on every assessed session that saw
        # votes, not only the ones that reached the confirmation gate.
        s.last_features["_vote_rule"] = vote_rule_tag(votes, raw, cfg)

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
            strong = cfg.adx_strong > cfg.adx_enter and adx > cfg.adx_strong
            if raw == s.pending_label:
                s.pending_count += 1
            else:
                s.pending_label = raw
                s.pending_count = 1

            needed = _vote_quorum(cfg, raw)
            agreeing = sum(1 for v in votes if v == raw)
            vote_agrees = needed > 0 and agreeing >= needed
            if raw != s.effective_regime and (s.pending_count >= cfg.confirm_sessions or strong or vote_agrees):
                # Audit: did the vote alone confirm this flip (hysteresis
                # not yet satisfied, no adx_strong fast-track)?
                if vote_agrees and s.pending_count < cfg.confirm_sessions and not strong:
                    s.last_features["_vote_accelerated"] = True
                # Regime change confirmed
                s.effective_regime = raw
                s.effective_since = session_date
                s.pending_count = 0
                s.flip_history.append(session_date)
                s.flip_history = s.flip_history[-cfg.flip_window:]
                # Flip-counter pause: max_flips within the last flip_window
                # SESSIONS (a true sliding window — lifetime flip count is
                # irrelevant, so an old bot doesn't pause on every flip).
                s.flip_sessions.append(s.session_count)
                s.flip_sessions = [
                    c for c in s.flip_sessions
                    if s.session_count - c < cfg.flip_window
                ]
                if len(s.flip_sessions) >= cfg.max_flips:
                    s.paused_until_session = s.session_count + cfg.pause_sessions

        s.last_features["_vol_spike"] = vol_spike
        return s
