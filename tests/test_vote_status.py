"""Tests for src/news/vote_status.py — per-source vote status assembly.

The display must answer two questions the raw files conflate:
1. "did this source vote for TONIGHT?" — only the vote file's own
   expires_after_session may answer (three bridges, three clocks)
2. "is the bridge alive?" — only the per-source liveness stamp may
   answer; a missing state file is UNKNOWN, never dead
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.news.vote_status import (
    SourceStatus,
    collect_vote_status,
    consumed_votes_line,
    read_pending_votes,
    vote_chip,
)

TPE = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 18, 10, 0, tzinfo=TPE)
TONIGHT = "2026-08-18|NIGHT"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _vote(direction="trending-up", expires=TONIGHT, source="W3"):
    return {"version": 1, "direction": direction,
            "expires_after_session": expires, "source": source}


def _paths(tmp_path):
    """(regime_vote_path, signal_path, rss_state_file) in one tmp dir."""
    return (str(tmp_path / "regime_vote.json"),
            str(tmp_path / "breaker_signal.json"),
            str(tmp_path / "rss_state.json"))


# ── read_pending_votes ───────────────────────────────────────────────────

def test_read_pending_votes_by_source(tmp_path):
    base = tmp_path / "regime_vote.json"
    _write(tmp_path / "regime_vote_w2.json", _vote(source="W2"))
    _write(tmp_path / "regime_vote_w3.json", _vote(source="W3-manual"))
    votes = read_pending_votes(str(base))
    assert set(votes) == {"W2", "W3"}   # "-manual" normalizes to W3


def test_read_pending_votes_skips_malformed(tmp_path):
    (tmp_path / "regime_vote_w2.json").write_text("{not json", encoding="utf-8")
    _write(tmp_path / "regime_vote_w4.json", _vote(direction="sideways"))
    votes = read_pending_votes(str(tmp_path / "regime_vote.json"))
    assert votes == {}


def test_read_pending_votes_never_consumes(tmp_path):
    f = tmp_path / "regime_vote_w4.json"
    _write(f, _vote(source="W4"))
    read_pending_votes(str(tmp_path / "regime_vote.json"))
    assert f.exists()


# ── collect_vote_status: vote bucketing by session key ───────────────────

def test_valid_pending_vote_for_tonight(tmp_path):
    vote_p, sig_p, rss_p = _paths(tmp_path)
    _write(tmp_path / "regime_vote_w3.json", _vote())
    report = collect_vote_status(vote_p, sig_p, rss_p, TONIGHT, NOW)
    w3 = next(s for s in report.sources if s.source == "W3")
    assert w3.vote == "trending-up"
    assert not w3.vote_expired


def test_expired_vote_flagged_not_shown_as_pending(tmp_path):
    vote_p, sig_p, rss_p = _paths(tmp_path)
    _write(tmp_path / "regime_vote_w3.json", _vote(expires="2026-08-15|NIGHT"))
    report = collect_vote_status(vote_p, sig_p, rss_p, TONIGHT, NOW)
    w3 = next(s for s in report.sources if s.source == "W3")
    assert w3.vote == ""
    assert w3.vote_expired
    assert w3.vote_target == "2026-08-15|NIGHT"


def test_all_three_sources_always_reported(tmp_path):
    vote_p, sig_p, rss_p = _paths(tmp_path)
    report = collect_vote_status(vote_p, sig_p, rss_p, TONIGHT, NOW)
    assert [s.source for s in report.sources] == ["W2", "W3", "W4"]


# ── liveness: alive / stale / unknown per source ─────────────────────────

def test_w2_liveness_from_monitor_state(tmp_path):
    vote_p, sig_p, rss_p = _paths(tmp_path)
    _write(tmp_path / "monitor_state.json", {
        "last_check": (NOW - timedelta(minutes=30)).isoformat(timespec="seconds"),
        "last_result": "fresh 3/4 | fired: none | vote: -",
    })
    report = collect_vote_status(vote_p, sig_p, rss_p, TONIGHT, NOW)
    w2 = next(s for s in report.sources if s.source == "W2")
    assert w2.known and not w2.stale
    assert "fresh 3/4" in w2.context


def test_w2_stale_after_a_day(tmp_path):
    vote_p, sig_p, rss_p = _paths(tmp_path)
    _write(tmp_path / "monitor_state.json", {
        "last_check": (NOW - timedelta(hours=30)).isoformat(timespec="seconds")})
    report = collect_vote_status(vote_p, sig_p, rss_p, TONIGHT, NOW)
    assert next(s for s in report.sources if s.source == "W2").stale


def test_w3_session_net_context_and_tight_staleness(tmp_path):
    vote_p, sig_p, rss_p = _paths(tmp_path)
    _write(Path(rss_p), {
        "last_check": (NOW - timedelta(hours=3)).isoformat(timespec="seconds"),
        "session_net": 1.8123, "session_peak": 2.1})
    report = collect_vote_status(vote_p, sig_p, rss_p, TONIGHT, NOW)
    w3 = next(s for s in report.sources if s.source == "W3")
    assert w3.stale                       # 3h > 2h threshold for a 30-min bridge
    assert "session net +1.81" in w3.context
    assert "peak +2.10" in w3.context


def test_w4_liveness_and_context(tmp_path):
    vote_p, sig_p, rss_p = _paths(tmp_path)
    _write(tmp_path / ".chips_state.json", {
        "last_check": (NOW - timedelta(hours=13)).isoformat(timespec="seconds"),
        "last_result": "oi +8,000 | equity n/a | vote: - | weak signal",
        "history": {"20260815": 8000.0}})
    report = collect_vote_status(vote_p, sig_p, rss_p, TONIGHT, NOW)
    w4 = next(s for s in report.sources if s.source == "W4")
    assert w4.known and not w4.stale      # 13h < 72h weekend-tolerant threshold
    assert "weak signal" in w4.context


def test_w4_pre_upgrade_state_falls_back_to_history_date(tmp_path):
    """Old .chips_state.json has only OI history — report last data day
    as UNKNOWN liveness, not dead."""
    vote_p, sig_p, rss_p = _paths(tmp_path)
    _write(tmp_path / ".chips_state.json",
           {"history": {"20260814": 1.0, "20260815": 2.0}})
    report = collect_vote_status(vote_p, sig_p, rss_p, TONIGHT, NOW)
    w4 = next(s for s in report.sources if s.source == "W4")
    assert not w4.known
    assert "20260815" in w4.context


def test_missing_state_files_report_unknown_not_dead(tmp_path):
    vote_p, sig_p, rss_p = _paths(tmp_path)
    report = collect_vote_status(vote_p, sig_p, rss_p, TONIGHT, NOW)
    for s in report.sources:
        assert not s.known
        assert not s.stale


# ── display helpers ──────────────────────────────────────────────────────

def test_vote_chip_states():
    assert "↑" in vote_chip(SourceStatus("W3", vote="trending-up"))
    assert "no vote" in vote_chip(SourceStatus("W2"))
    chip = vote_chip(SourceStatus("W4", vote_expired=True,
                                  vote_target="2026-08-15|NIGHT"))
    assert "expired" in chip and "2026-08-15" in chip


def test_consumed_votes_line():
    assert consumed_votes_line(None) == ""
    assert consumed_votes_line({}) == ""          # pre-upgrade state
    assert consumed_votes_line({"_vote_sources": []}) == "無投票 none"
    line = consumed_votes_line({
        "_vote_sources": ["W3:trending-up", "W2:trending-down"],
        "_vote_accelerated": True})
    assert "W3↑" in line and "W2↓" in line and "accelerated" in line
    line = consumed_votes_line({"_vote_sources": ["W3:trending-up"],
                                "_vote_accelerated": False})
    assert "accelerated" not in line
