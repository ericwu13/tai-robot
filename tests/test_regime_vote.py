"""Tests for news-as-votes: cross-market regime confirmation acceleration."""

import json
import os
import time
from datetime import datetime, timedelta, timezone

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
    _get_write_regime_vote,
)


def _make_result(adx, plus_di=30.0, minus_di=10.0, atr_ratio=1.0,
                 ema_slope=0.0):
    return RegimeResult(
        label="test", trend_strength="trending", volatility="normal",
        direction="bullish", adx_value=adx, plus_di_value=plus_di,
        minus_di_value=minus_di, atr_value=100.0, atr_ratio=atr_ratio,
        ema_50=17900.0, last_close=18000.0, ema_slope=ema_slope,
    )


# ── 1. Vote agrees with raw label → immediate flip ──

def test_vote_agrees_with_raw_label_immediate_flip():
    """Cross-market votes matching tonight's raw classification skip the
    normal 2-session hysteresis — the flip confirms in 1 night.

    Updated for the asymmetric quorum: an UP flip now needs
    vote_quorum_up (2) agreeing sources. One used to be enough — see
    test_single_up_vote_does_not_accelerate."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()

    # First session: ADX 27 (> enter but < strong), +DI dominant → trending-up.
    # Without votes, this would NOT confirm (pending_count=1 < confirm_sessions=2).
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-up", "trending-up"])

    assert s.raw_regime == "trending-up"
    assert s.effective_regime == "trending-up", "quorum should accelerate confirmation"
    assert s.effective_since == "2026-08-05"


# ── 1b. Asymmetric quorum: up needs 2, down needs 1 ──

def test_single_up_vote_does_not_accelerate():
    """One agreeing UP vote is below vote_quorum_up (2) — normal
    hysteresis applies.  Pre-change ANY single vote accelerated, which is
    how a 3/10 W2 read flipped the bot long on one night's evidence."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()

    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-up"])

    assert s.raw_regime == "trending-up"
    assert s.effective_regime == "unknown"
    assert s.pending_count == 1
    assert s.last_features["_vote_accelerated"] is False
    assert s.last_features["_vote_rule"] == "up:1/2"


def test_two_up_votes_accelerate():
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()

    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-up", "trending-up"],
               vote_sources=["W2:trending-up", "W3:trending-up"])

    assert s.effective_regime == "trending-up"
    assert s.last_features["_vote_accelerated"] is True
    assert s.last_features["_vote_rule"] == "up:2/2"


def test_single_down_vote_still_accelerates():
    """The DOWN quorum stays at 1 — confirming a down read early is the
    cheap, protective direction.  Unchanged behaviour."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()

    s = m.step(s, _make_result(adx=27.0, plus_di=10.0, minus_di=30.0), cfg,
               "2026-08-05", vote_directions=["trending-down"])

    assert s.raw_regime == "trending-down"
    assert s.effective_regime == "trending-down"
    assert s.last_features["_vote_accelerated"] is True
    assert s.last_features["_vote_rule"] == "down:1/1"


def test_vote_rule_blank_without_votes():
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True)
    s = RegimeState()
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05")
    assert s.last_features["_vote_rule"] == ""


def test_vote_rule_stamped_on_a_transitional_night():
    """The rule is stamped before the transitional early-exit, so a
    session that saw votes always records what the rule saw."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True)
    s = RegimeState()
    s = m.step(s, _make_result(adx=22.0), cfg, "2026-08-05",
               vote_directions=["trending-down", "trending-down"])
    assert s.raw_regime == "transitional"
    assert s.last_features["_vote_rule"] == "down:2/1"


def test_quorum_is_configurable():
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2, vote_quorum_up=1)
    s = RegimeState()
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-up"])
    assert s.effective_regime == "trending-up"


def test_zero_quorum_disables_that_direction():
    """vote_quorum_down=0 means "votes never accelerate a down flip"."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2, vote_quorum_down=0)
    s = RegimeState()
    s = m.step(s, _make_result(adx=27.0, plus_di=10.0, minus_di=30.0), cfg,
               "2026-08-05", vote_directions=["trending-down"])
    assert s.effective_regime == "unknown"


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


# ── 4b. Audit trail: _vote_sources + _vote_accelerated ──

def test_vote_sources_and_acceleration_stamped():
    """step() persists WHO voted and whether the vote alone confirmed
    the flip — the regime tab reads both from regime_state.json."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()

    # Two sources: the up quorum is 2 (see test_single_up_vote_...).
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-up", "trending-up"],
               vote_sources=["W3:trending-up", "W4:trending-up"])
    assert s.last_features["_vote_sources"] == ["W3:trending-up", "W4:trending-up"]
    assert s.last_features["_vote_accelerated"] is True


def test_hysteresis_flip_not_marked_accelerated():
    """A flip that would have confirmed anyway (pending_count reached
    confirm_sessions) must NOT claim vote acceleration, even when an
    agreeing vote is present."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05")
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-06",
               vote_directions=["trending-up"],
               vote_sources=["W3:trending-up"])
    assert s.effective_regime == "trending-up"
    assert s.last_features["_vote_accelerated"] is False
    assert s.last_features["_vote_sources"] == ["W3:trending-up"]


def test_disagreeing_vote_not_marked_accelerated():
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-down"],
               vote_sources=["W4:trending-down"])
    assert s.effective_regime == "unknown"
    assert s.last_features["_vote_accelerated"] is False
    assert s.last_features["_vote_sources"] == ["W4:trending-down"]


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
    # 22:00 TPE on Aug 5 → inside the night that opened Aug 5
    assert night_session_key(datetime(2026, 8, 5, 22, 0, tzinfo=tz)) == "2026-08-05|NIGHT"
    # 03:00 TPE on Aug 6 → still inside the night that opened Aug 5
    assert night_session_key(datetime(2026, 8, 6, 3, 0, tzinfo=tz)) == "2026-08-05|NIGHT"
    # 10:00 TPE on Aug 6 → day session; vote targets tonight (Aug 6)
    assert night_session_key(datetime(2026, 8, 6, 10, 0, tzinfo=tz)) == "2026-08-06|NIGHT"
    # 05:00 TPE on Aug 6 → gap after night close; vote targets tonight (Aug 6)
    assert night_session_key(datetime(2026, 8, 6, 5, 0, tzinfo=tz)) == "2026-08-06|NIGHT"
    # 14:00 TPE on Aug 6 → gap before night open; vote targets tonight (Aug 6)
    assert night_session_key(datetime(2026, 8, 6, 14, 0, tzinfo=tz)) == "2026-08-06|NIGHT"
    # 04:59 TPE on Aug 6 → still inside last night (opened Aug 5)
    assert night_session_key(datetime(2026, 8, 6, 4, 59, tzinfo=tz)) == "2026-08-05|NIGHT"


def test_monitor_write_vote(tmp_path):
    """Monitor's write_regime_vote produces a per-source vote file."""
    base_path = str(tmp_path / "regime_vote.json")
    monitor_write_vote = _get_write_regime_vote()
    monitor_write_vote(base_path, "trending-down", "2026-08-05|NIGHT", source="W2")

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


def test_multi_source_quorum_counts_only_agreeing_sources():
    """Was test_multi_source_any_agrees_accelerates: ANY single agreeing
    source used to accelerate.  Under the asymmetric quorum an UP flip
    needs two agreeing sources, so one agree + one disagree is not
    enough — and adding the second agreeing source tips it."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2)
    s = RegimeState()

    # W2 says trending-down (disagrees), W3 says trending-up (agrees)
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-down", "trending-up"])
    assert s.effective_regime == "unknown", "1 of the 2 needed up votes"
    assert s.last_features["_vote_rule"] == "up:1/2"

    s = RegimeState()
    s = m.step(s, _make_result(adx=27.0), cfg, "2026-08-05",
               vote_directions=["trending-up", "trending-up", "trending-down"])
    assert s.effective_regime == "trending-up"


def test_source_path_derivation():
    """_source_path strips the dash suffix from source names."""
    assert _source_path("/data/regime_vote.json", "W2").endswith("regime_vote_w2.json")
    assert _source_path("/data/regime_vote.json", "W3").endswith("regime_vote_w3.json")
    assert _source_path("/data/regime_vote.json", "W3-manual").endswith("regime_vote_w3.json")


# ── 10. Votes reach the selector (range-bound information fusion) ──

def test_votes_stamped_into_last_features():
    """step() persists tonight's votes in last_features for the selector
    and for post-hoc audit (the vote files are consumed right after)."""
    m = RegimeStateMachine()
    cfg = RegimeConfig(enabled=True)
    s = RegimeState()

    s = m.step(s, _make_result(adx=15.0), cfg, "2026-08-05",
               vote_directions=["trending-up"])
    assert s.last_features["_votes"] == ["trending-up"]

    s = m.step(s, _make_result(adx=15.0), cfg, "2026-08-06",
               vote_directions=[])
    assert s.last_features["_votes"] == []


def test_range_bound_with_agreeing_vote_deploys_half_size():
    """The 2026-08 grind-up case: ADX below adx_exit (range-bound) while
    price drifts up and an external vote says trending-up → the selector
    deploys the long leg half-size instead of sitting out."""
    from src.regime.selector import StrategySelector

    m = RegimeStateMachine()
    sel = StrategySelector()
    cfg = RegimeConfig(enabled=True, confirm_sessions=2,
                       long_strategy="LongBot", short_strategy="ShortBot")
    s = RegimeState(effective_regime="range-bound",
                    effective_since="2026-08-06")

    # Two up votes: the range-bound probe uses the same asymmetric quorum.
    s = m.step(s, _make_result(adx=18.7, plus_di=25.1, minus_di=16.6,
                               ema_slope=101.1),
               cfg, "2026-08-07",
               vote_directions=["trending-up", "trending-up"])
    assert s.effective_regime == "range-bound"

    rec = sel.select(s, cfg)
    assert rec.action == "deploy_long_half"
    assert rec.strategy_name == "LongBot"
    assert rec.qty_scale == 0.5


# ── 11. fired_at + the nightly lane's per-source age gate ──

_TPE = timezone(timedelta(hours=8))


def _write_raw(tmp_path, suffix, *, source="W2", direction="trending-up",
               session="2026-08-05|NIGHT", fired_at=""):
    """Write a vote file by hand so fired_at can be absent or ancient."""
    payload = {
        "version": 1, "direction": direction,
        "expires_after_session": session, "source": source,
    }
    if fired_at is not None:
        payload["fired_at"] = fired_at or datetime.now(_TPE).isoformat(timespec="seconds")
    path = tmp_path / f"regime_vote_{suffix}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class _VoteReader:
    """The runner's vote reader, without the COM/GUI runner around it."""

    from src.live.regime_switching_runner import RegimeSwitchingRunner as _R
    _read_regime_vote = _R._read_regime_vote
    del _R

    def __init__(self, base_path, max_age=None):
        from src.config.settings import NewsConfig
        cfg = NewsConfig(enabled=True, regime_vote_path=str(base_path))
        if max_age is not None:
            cfg.nightly_vote_max_age_h = max_age
        self._news_cfg = cfg


def test_write_stamps_fired_at(tmp_path):
    """Every writer goes through write_regime_vote, so every vote file
    carries a wall-clock fire time — the session key alone says only
    WHICH night the vote targets, not how stale the evidence is."""
    base = tmp_path / "regime_vote.json"
    write_regime_vote(base, "trending-up", "2026-08-05|NIGHT", source="W2")

    data = json.loads((tmp_path / "regime_vote_w2.json").read_text(encoding="utf-8"))
    assert data["version"] == 1, "fired_at is additive — schema stays v1"
    stamped = datetime.fromisoformat(data["fired_at"])
    assert stamped.utcoffset() == timedelta(hours=8)
    assert abs((datetime.now(_TPE) - stamped).total_seconds()) < 120


def test_read_populates_age_from_fired_at(tmp_path):
    path = _write_raw(tmp_path, "w2", direction="trending-down",
                      fired_at=(datetime.now(_TPE) - timedelta(hours=3)).isoformat())
    vote = read_regime_vote(path, "2026-08-05|NIGHT")
    assert vote.fired_at
    assert 3 * 3600 - 60 < vote.age_sec < 3 * 3600 + 60


def test_read_falls_back_to_mtime_without_fired_at(tmp_path):
    """Pre-fired_at vote files (and unparseable stamps) age off mtime."""
    path = _write_raw(tmp_path, "w2", fired_at=None)
    old = time.time() - 5 * 3600
    os.utime(path, (old, old))

    vote = read_regime_vote(path, "2026-08-05|NIGHT")
    assert vote.fired_at == ""
    assert 5 * 3600 - 60 < vote.age_sec < 5 * 3600 + 60


def test_unparseable_fired_at_falls_back_to_mtime(tmp_path):
    path = _write_raw(tmp_path, "w2", fired_at="not-a-timestamp")
    old = time.time() - 2 * 3600
    os.utime(path, (old, old))

    vote = read_regime_vote(path, "2026-08-05|NIGHT")
    assert 2 * 3600 - 60 < vote.age_sec < 2 * 3600 + 60


def test_future_fired_at_clamps_to_zero(tmp_path):
    """A bridge host whose clock runs fast must not read as negative age."""
    path = _write_raw(tmp_path, "w2",
                      fired_at=(datetime.now(_TPE) + timedelta(minutes=5)).isoformat())
    assert read_regime_vote(path, "2026-08-05|NIGHT").age_sec == 0.0


def test_runner_drops_w2_vote_older_than_its_limit(tmp_path):
    """The nightly lane fires at ~04:58, at the END of the session a W2
    vote names, and the leg goes live at the next 08:45 open — a 4h-edge
    signal is long dead. The vote is still CONSUMED, just not classified."""
    _write_raw(tmp_path, "w2",
               fired_at=(datetime.now(_TPE) - timedelta(hours=5)).isoformat())
    runner = _VoteReader(tmp_path / "regime_vote.json")

    votes = runner._read_regime_vote("2026-08-05|NIGHT")

    assert votes == []
    assert not (tmp_path / "regime_vote_w2.json").exists(), "expired votes are consumed"


def test_runner_keeps_w3_vote_of_the_same_age(tmp_path):
    """Only sources listed in nightly_vote_max_age_h are aged out. W3
    news and W4 chips are daily-cadence signals — ageing them out at 4h
    would silence them entirely."""
    _write_raw(tmp_path, "w3", source="W3",
               fired_at=(datetime.now(_TPE) - timedelta(hours=5)).isoformat())
    runner = _VoteReader(tmp_path / "regime_vote.json")

    votes = runner._read_regime_vote("2026-08-05|NIGHT")

    assert [v.source for v in votes] == ["W3"]


def test_runner_keeps_a_fresh_w2_vote(tmp_path):
    _write_raw(tmp_path, "w2",
               fired_at=(datetime.now(_TPE) - timedelta(hours=1)).isoformat())
    runner = _VoteReader(tmp_path / "regime_vote.json")

    assert [v.source for v in runner._read_regime_vote("2026-08-05|NIGHT")] == ["W2"]


def test_runner_ages_a_stampless_w2_vote_off_mtime(tmp_path):
    path = _write_raw(tmp_path, "w2", fired_at=None)
    old = time.time() - 6 * 3600
    os.utime(path, (old, old))
    runner = _VoteReader(tmp_path / "regime_vote.json")

    assert runner._read_regime_vote("2026-08-05|NIGHT") == []


def test_runner_empty_age_map_disables_the_gate(tmp_path):
    _write_raw(tmp_path, "w2",
               fired_at=(datetime.now(_TPE) - timedelta(hours=48)).isoformat())
    runner = _VoteReader(tmp_path / "regime_vote.json", max_age={})

    assert [v.source for v in runner._read_regime_vote("2026-08-05|NIGHT")] == ["W2"]


def test_source_suffix_matches_the_bridge_stem_limit():
    """A "W3-manual" vote is keyed by its bridge stem, like its filename."""
    from src.live.regime_switching_runner import _vote_age_limit_h
    assert _vote_age_limit_h({"W3": 4.0}, "W3-manual") == 4.0
    assert _vote_age_limit_h({"W3": 4.0}, "W2") is None
    assert _vote_age_limit_h({}, "W2") is None
    assert _vote_age_limit_h({"W2": "junk"}, "W2") is None
