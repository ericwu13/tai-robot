"""Unit tests for the regime state machine (Phase 1 shadow mode)."""

from src.daily_report.regime_classifier import RegimeResult
from src.regime.state_machine import RegimeStateMachine, RegimeState, RegimeConfig


def make_result(adx, plus_di=30.0, minus_di=10.0, atr_ratio=1.0, ema_slope=0.0,
                direction="bullish", last_close=18000.0):
    """Build a RegimeResult with just the fields the state machine reads."""
    return RegimeResult(
        label="test",
        trend_strength="trending",
        volatility="normal",
        direction=direction,
        adx_value=adx,
        plus_di_value=plus_di,
        minus_di_value=minus_di,
        atr_value=100.0,
        atr_ratio=atr_ratio,
        ema_50=17900.0,
        last_close=last_close,
        ema_slope=ema_slope,
    )


def _step(machine, state, cfg, result, date):
    return machine.step(state, result, cfg, date)


def test_transitional_does_not_change_effective():
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True)
    s = RegimeState()
    # ADX 22 sits in the 20–25 dead zone -> transitional.
    s = _step(m, s, cfg, make_result(adx=22.0), "2026-01-01")
    assert s.raw_regime == "transitional"
    assert s.effective_regime == "unknown"
    assert s.pending_count == 0


def test_two_session_confirmation():
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()
    # adx 27 (> enter, < 30 so no fast-track), +DI dominant -> trending-up.
    s = _step(m, s, cfg, make_result(adx=27.0), "2026-01-01")
    assert s.raw_regime == "trending-up"
    assert s.effective_regime == "unknown"   # not yet confirmed
    assert s.pending_count == 1
    s = _step(m, s, cfg, make_result(adx=27.0), "2026-01-02")
    assert s.effective_regime == "trending-up"
    assert s.effective_since == "2026-01-02"
    assert s.pending_count == 0


def test_strong_adx_fast_tracks_after_one_session():
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()
    # adx 35 (> 30) fast-tracks the confirmation in a single session.
    s = _step(m, s, cfg, make_result(adx=35.0), "2026-01-01")
    assert s.effective_regime == "trending-up"
    assert s.effective_since == "2026-01-01"


def test_trending_down_derived_from_di():
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True)
    s = RegimeState()
    s = _step(m, s, cfg, make_result(adx=35.0, plus_di=10.0, minus_di=30.0), "2026-01-01")
    assert s.raw_regime == "trending-down"
    assert s.effective_regime == "trending-down"


def test_vol_spike_does_not_change_effective():
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, vol_spike_ratio=1.5)
    s = RegimeState()
    # Transitional ADX + a large ATR ratio: the spike is flagged but does
    # not manufacture an effective regime.
    s = _step(m, s, cfg, make_result(adx=22.0, atr_ratio=2.0), "2026-01-01")
    assert s.effective_regime == "unknown"
    assert s.last_features["_vol_spike"] is True


def test_flip_counter_triggers_pause():
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, max_flips=3, pause_sessions=2, flip_window=10)
    s = RegimeState()
    # Three strong-ADX flips (up, down, up) each confirm in one session.
    s = _step(m, s, cfg, make_result(adx=35.0, plus_di=30, minus_di=10), "2026-01-01")
    s = _step(m, s, cfg, make_result(adx=35.0, plus_di=10, minus_di=30), "2026-01-02")
    s = _step(m, s, cfg, make_result(adx=35.0, plus_di=30, minus_di=10), "2026-01-03")
    assert len(s.flip_history) == 3
    assert s.paused_until_session == s.session_count + 2
    # Next session is frozen: effective regime does not move and _paused flags.
    frozen_before = s.effective_regime
    s = _step(m, s, cfg, make_result(adx=35.0, plus_di=10, minus_di=30), "2026-01-04")
    assert s.last_features.get("_paused") is True
    assert s.effective_regime == frozen_before


def test_pause_expires_after_pause_sessions():
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, max_flips=3, pause_sessions=1, flip_window=10)
    s = RegimeState()
    s = _step(m, s, cfg, make_result(adx=35.0, plus_di=30, minus_di=10), "2026-01-01")
    s = _step(m, s, cfg, make_result(adx=35.0, plus_di=10, minus_di=30), "2026-01-02")
    s = _step(m, s, cfg, make_result(adx=35.0, plus_di=30, minus_di=10), "2026-01-03")
    paused_until = s.paused_until_session      # session_count(3) + 1 = 4
    # Session 4 is frozen ...
    s = _step(m, s, cfg, make_result(adx=22.0), "2026-01-04")
    assert s.last_features.get("_paused") is True
    assert s.session_count == 4 == paused_until
    # ... session 5 processes normally again.
    s = _step(m, s, cfg, make_result(adx=22.0), "2026-01-05")
    assert not s.last_features.get("_paused")


def test_flips_outside_window_do_not_pause():
    """flip_window is a SLIDING window — 3 flips spread far apart must not
    pause. Old code counted lifetime flips (trimmed only to the last 10),
    so after the 3rd flip EVER, every flip paused the bot forever."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, max_flips=3, pause_sessions=2, flip_window=5)
    s = RegimeState()
    s = _step(m, s, cfg, make_result(adx=35.0, plus_di=30, minus_di=10), "2026-01-01")
    s = _step(m, s, cfg, make_result(adx=35.0, plus_di=10, minus_di=30), "2026-01-02")
    # 10 transitional sessions push the window past both flips
    for i in range(10):
        s = _step(m, s, cfg, make_result(adx=22.0), f"2026-01-{3 + i:02d}")
    # 3rd lifetime flip — but only 1 flip within the last 5 sessions
    s = _step(m, s, cfg, make_result(adx=35.0, plus_di=30, minus_di=10), "2026-01-13")
    assert s.effective_regime == "trending-up"
    assert s.paused_until_session == 0


def test_raised_adx_enter_does_not_disable_hysteresis():
    """With adx_enter raised above adx_strong, the fast-track is disabled
    instead of firing on every trending read (which would silently reduce
    confirm_sessions to 1)."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2, adx_enter=32.0)
    s = RegimeState()
    s = _step(m, s, cfg, make_result(adx=35.0), "2026-01-01")
    assert s.raw_regime == "trending-up"
    assert s.effective_regime == "unknown"   # one session is NOT enough
    s = _step(m, s, cfg, make_result(adx=35.0), "2026-01-02")
    assert s.effective_regime == "trending-up"  # confirmed on the 2nd


def test_manual_override_field_preserved_through_step():
    # The state machine does not consume manual_override (the manager does);
    # it must survive a step untouched.
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True)
    s = RegimeState(manual_override="long")
    s = _step(m, s, cfg, make_result(adx=35.0), "2026-01-01")
    assert s.manual_override == "long"
