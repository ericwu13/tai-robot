"""Tests for news-as-votes: cross-market regime confirmation acceleration."""

import json
import os

from src.daily_report.regime_classifier import RegimeResult
from src.news.regime_vote import (
    RegimeVote,
    consume_all_regime_votes,
    consume_regime_vote,
    read_all_regime_votes,
    read_regime_vote,
    write_regime_vote,
    _source_path,
)
from src.regime.state_machine import RegimeConfig, RegimeState, RegimeStateMachine
from scripts.news_bridge.crossmarket_monitor import (
    VOTE_THRESHOLDS,
    night_session_key,
    write_regime_vote as monitor_write_vote,
)


def _make_result(adx, plus_di=30.0, minus_di=10.0, atr_ratio=1.0):
    return RegimeResult(
        label="test", trend_strength="trending", volatility="normal",
        direction="bullish", adx_value=adx, plus_di_value=plus_di,
        minus_di_value=minus_di, atr_value=100.0, atr_ratio=atr_ratio,
        ema_50=17900.0, last_close=18000.0, ema_slope=0.0,
    )


# ── 1. Vote agrees with raw label → immediate flip ──

def test_vote_agrees_with_raw_label_immediate_flip():
    """A cross-market vote matching tonight's raw classification skips
    the normal 2-session hysteresis — the flip confirms in 1 night."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()

    # First session: ADX 27 (> enter but < strong), +DI dominant → trending-up.
    # Without a vote, this would NOT confirm (pending_count=1 < confirm_sessions=2).
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-up"])

    assert s.raw_regime == "trending-up"
    assert s.effective_regime == "trending-up", "vote should accelerate confirmation"
    assert s.effective_since == "2026-08-05"


# ── 2. Vote disagrees with raw label → discarded, normal hysteresis ──

def test_vote_disagrees_with_raw_label_normal_hysteresis():
    """A vote in the wrong direction is ignored — normal confirmation
    rules apply (2 sessions needed)."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()

    # Raw = trending-up, vote = trending-down → disagreement
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-down"])

    assert s.raw_regime == "trending-up"
    assert s.effective_regime == "unknown", "disagreeing vote must not accelerate"
    assert s.pending_count == 1


# ── 3. Expired vote → ignored, normal hysteresis ──

def test_expired_vote_ignored(tmp_path):
    """A vote whose expires_after_session doesn't match the current
    session is silently ignored."""
    base_path = tmp_path / "regime_vote.json"
    write_regime_vote(base_path, "trending-up", "2026-08-04|NIGHT", source="W2")

    w2_path = tmp_path / "regime_vote_w2.json"
    # Current session is 2026-08-05|NIGHT — the vote is from yesterday.
    vote = read_regime_vote(w2_path, "2026-08-05|NIGHT")
    assert vote is None, "expired vote should return None"


# ── 4. No vote file → normal hysteresis (regression guard) ──

def test_no_vote_file_normal_hysteresis():
    """Without any vote file, the state machine behaves identically to
    the pre-vote baseline: 2 sessions to confirm."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()

    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=[])
    assert s.effective_regime == "unknown"
    assert s.pending_count == 1

    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-06",
               vote_directions=[])
    assert s.effective_regime == "trending-up"


# ── 5. Vote file round-trip (write → read → consume) ──

def test_vote_file_round_trip(tmp_path):
    """write → read → consume lifecycle with per-source paths."""
    base_path = tmp_path / "regime_vote.json"
    session_key = "2026-08-05|NIGHT"

    write_regime_vote(base_path, "trending-up", session_key, source="W2")
    w2_path = tmp_path / "regime_vote_w2.json"
    assert w2_path.exists()

    vote = read_regime_vote(w2_path, session_key)
    assert vote is not None
    assert vote.direction == "trending-up"
    assert vote.expires_after_session == session_key
    assert vote.source == "W2"

    consume_regime_vote(w2_path)
    assert not w2_path.exists()

    # Consuming a missing file is a no-op.
    consume_regime_vote(w2_path)


# ── 6. Monitor vote threshold detection ──

def test_monitor_upside_vote_thresholds():
    """VOTE_THRESHOLDS differ from signal THRESHOLDS (TSM 2.0 vs 3.5)."""
    assert VOTE_THRESHOLDS["TSM"] == (-2.0, 2.0)
    assert VOTE_THRESHOLDS["SOXX"] == (-2.5, 2.5)
    assert VOTE_THRESHOLDS["QQQ"] == (-2.0, 2.0)


def test_monitor_night_session_key():
    """night_session_key returns the correct open-date key."""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    # 22:00 TPE on Aug 5 → session opened Aug 5
    assert night_session_key(datetime(2026, 8, 5, 22, 0, tzinfo=tz)) == "2026-08-05|NIGHT"
    # 03:00 TPE on Aug 6 → session opened Aug 5
    assert night_session_key(datetime(2026, 8, 6, 3, 0, tzinfo=tz)) == "2026-08-05|NIGHT"


def test_monitor_write_vote(tmp_path):
    """Monitor's write_regime_vote produces a per-source vote file."""
    base_path = str(tmp_path / "regime_vote.json")
    monitor_write_vote(base_path, "trending-down", "2026-08-05|NIGHT")

    w2_path = str(tmp_path / "regime_vote_w2.json")
    with open(w2_path, encoding="utf-8") as f:
        data = json.load(f)

    assert data["version"] == 1
    assert data["direction"] == "trending-down"
    assert data["expires_after_session"] == "2026-08-05|NIGHT"
    assert data["source"] == "W2"


# ── 7. Malformed / invalid vote files ──

def test_malformed_vote_file_returns_none(tmp_path):
    """A broken file is silently ignored (fail-open)."""
    vote_path = tmp_path / "regime_vote.json"
    vote_path.write_text("not json", encoding="utf-8")
    assert read_regime_vote(vote_path, "2026-08-05|NIGHT") is None


def test_invalid_direction_returns_none(tmp_path):
    vote_path = tmp_path / "regime_vote.json"
    vote_path.write_text(json.dumps({
        "version": 1, "direction": "sideways",
        "expires_after_session": "2026-08-05|NIGHT",
    }), encoding="utf-8")
    assert read_regime_vote(vote_path, "2026-08-05|NIGHT") is None


# ── 8. Vote does NOT accelerate when raw == effective (no flip needed) ──

def test_vote_no_effect_when_already_in_regime():
    """A vote matching effective_regime doesn't trigger a spurious
    re-confirmation (raw == effective → the confirmation gate's
    `raw != effective` check prevents it)."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()

    # Force into trending-up via strong ADX
    s = m.step(s, _make_result(adx=35.0), cfg, "2026-08-04")
    assert s.effective_regime == "trending-up"
    flip_count_before = len(s.flip_history)

    # Another trending-up night with a vote — should NOT add a flip
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-up"])
    assert s.effective_regime == "trending-up"
    assert len(s.flip_history) == flip_count_before


# ── 9. Multi-source votes: W2 + W3 independent, no overwrite ──

def test_multi_source_votes_both_read(tmp_path):
    """W2 and W3 write to separate files; read_all returns both."""
    base_path = tmp_path / "regime_vote.json"
    session_key = "2026-08-05|NIGHT"

    write_regime_vote(base_path, "trending-up", session_key, source="W2")
    write_regime_vote(base_path, "trending-up", session_key, source="W3")

    assert (tmp_path / "regime_vote_w2.json").exists()
    assert (tmp_path / "regime_vote_w3.json").exists()

    votes = read_all_regime_votes(base_path, session_key)
    assert len(votes) == 2
    sources = {v.source for v in votes}
    assert sources == {"W2", "W3"}


def test_multi_source_votes_consumed_together(tmp_path):
    """consume_all deletes all per-source vote files."""
    base_path = tmp_path / "regime_vote.json"
    session_key = "2026-08-05|NIGHT"

    write_regime_vote(base_path, "trending-up", session_key, source="W2")
    write_regime_vote(base_path, "trending-down", session_key, source="W3")

    consume_all_regime_votes(base_path)
    assert not (tmp_path / "regime_vote_w2.json").exists()
    assert not (tmp_path / "regime_vote_w3.json").exists()


def test_multi_source_any_agrees_accelerates():
    """If either W2 or W3 agrees with raw, confirmation accelerates."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()

    # W2 says trending-down (disagrees), W3 says trending-up (agrees)
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-down", "trending-up"])

    assert s.effective_regime == "trending-up", "W3 agrees → should accelerate"


def test_source_path_derivation():
    """_source_path strips the dash suffix from source names."""
    assert _source_path("/data/regime_vote.json", "W2").endswith("regime_vote_w2.json")
    assert _source_path("/data/regime_vote.json", "W3").endswith("regime_vote_w3.json")
    assert _source_path("/data/regime_vote.json", "W3-manual").endswith("regime_vote_w3.json")
