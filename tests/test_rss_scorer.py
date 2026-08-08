"""Tests for W3 RSS+Gemini news scorer."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "news_bridge"))
import rss_scorer


@pytest.fixture
def tmp_vote(tmp_path):
    return str(tmp_path / "regime_vote.json")


def _w3_path(base_path):
    """Derive the per-source vote file path W3 writes to."""
    stem, ext = os.path.splitext(base_path)
    return f"{stem}_w3{ext}"


@pytest.fixture
def tmp_state(tmp_path):
    return str(tmp_path / "state.json")


def _make_article(guid: str, title: str, hours_ago: float = 0.5) -> dict:
    return {
        "guid": guid,
        "title": title,
        "summary": f"Summary for {title}",
        "published_dt": datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    }


# ── Aggregation tests ───────────────────────────────────────────────────

class TestAggregation:
    def test_neutral_no_vote(self):
        scored = [
            {"article": {}, "score": {"direction": "neutral", "confidence": 0.5}},
            {"article": {}, "score": {"direction": "bullish", "confidence": 0.3}},
            {"article": {}, "score": {"direction": "bearish", "confidence": 0.2}},
        ]
        net = rss_scorer.aggregate_scores(scored)
        assert rss_scorer.net_score_to_vote(net) is None

    def test_bullish_above_threshold(self):
        scored = [
            {"article": {}, "score": {"direction": "bullish", "confidence": 0.5}},
            {"article": {}, "score": {"direction": "bullish", "confidence": 0.4}},
        ]
        net = rss_scorer.aggregate_scores(scored)
        assert net >= 0.8
        assert rss_scorer.net_score_to_vote(net) == "trending-up"

    def test_bearish_below_threshold(self):
        scored = [
            {"article": {}, "score": {"direction": "bearish", "confidence": 0.5}},
            {"article": {}, "score": {"direction": "bearish", "confidence": 0.4}},
        ]
        net = rss_scorer.aggregate_scores(scored)
        assert net <= -0.8
        assert rss_scorer.net_score_to_vote(net) == "trending-down"

    def test_mixed_cancels(self):
        scored = [
            {"article": {}, "score": {"direction": "bullish", "confidence": 0.9}},
            {"article": {}, "score": {"direction": "bearish", "confidence": 0.8}},
        ]
        net = rss_scorer.aggregate_scores(scored)
        assert -0.8 < net < 0.8
        assert rss_scorer.net_score_to_vote(net) is None

    def test_exact_threshold(self):
        scored = [
            {"article": {}, "score": {"direction": "bullish", "confidence": 0.8}},
        ]
        net = rss_scorer.aggregate_scores(scored)
        assert net == 0.8
        assert rss_scorer.net_score_to_vote(net) == "trending-up"


# ── Deduplication tests ──────────────────────────────────────────────────

class TestDedup:
    def test_new_articles_pass(self):
        state = {"seen_guids": []}
        articles = [_make_article("a1", "Title A"), _make_article("a2", "Title B")]
        new = rss_scorer.dedup_articles(articles, state)
        assert len(new) == 2

    def test_seen_guid_filtered(self):
        state = {"seen_guids": ["a1"]}
        articles = [_make_article("a1", "Title A"), _make_article("a2", "Title B")]
        new = rss_scorer.dedup_articles(articles, state)
        assert len(new) == 1
        assert new[0]["guid"] == "a2"

    def test_all_seen(self):
        state = {"seen_guids": ["a1", "a2"]}
        articles = [_make_article("a1", "Title A"), _make_article("a2", "Title B")]
        new = rss_scorer.dedup_articles(articles, state)
        assert len(new) == 0

    def test_mark_seen_caps_at_5000(self):
        state = {"seen_guids": [f"old-{i}" for i in range(4999)]}
        state = rss_scorer.mark_seen(state, ["new-1", "new-2"])
        assert len(state["seen_guids"]) == 5000
        assert "new-2" in state["seen_guids"]


# ── End-to-end check_once with mocked feeds + Gemini ─────────────────────

class TestCheckOnce:
    def _mock_cfg(self, webhook=""):
        return {
            "feeds": ["https://example.com/rss"],
            "interval_minutes": 30,
            "state_file": "data/state.json",
            "gemini_api_key": "fake-key",
            "discord_webhook_url": webhook,
        }

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_neutral_no_vote_written(self, mock_score, mock_feeds, tmp_vote):
        mock_feeds.return_value = [_make_article("n1", "Routine update")]
        mock_score.return_value = {"direction": "neutral", "confidence": 0.3, "reason": "routine"}

        state = {"seen_guids": []}
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote)

        assert not os.path.exists(_w3_path(tmp_vote))

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_bullish_vote_written(self, mock_score, mock_feeds, tmp_vote):
        mock_feeds.return_value = [
            _make_article("b1", "Markets surge"),
            _make_article("b2", "Tech rally extends"),
        ]
        mock_score.return_value = {"direction": "bullish", "confidence": 0.5, "reason": "surge"}

        state = {"seen_guids": []}
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote)

        w3_file = _w3_path(tmp_vote)
        assert os.path.exists(w3_file)
        with open(w3_file) as f:
            vote = json.load(f)
        assert vote["direction"] == "trending-up"
        assert vote["source"] == "W3"
        assert vote["version"] == 1

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_bearish_vote_written(self, mock_score, mock_feeds, tmp_vote):
        mock_feeds.return_value = [
            _make_article("c1", "Markets crash"),
            _make_article("c2", "Sell-off deepens"),
        ]
        mock_score.return_value = {"direction": "bearish", "confidence": 0.5, "reason": "crash"}

        state = {"seen_guids": []}
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote)

        w3_file = _w3_path(tmp_vote)
        assert os.path.exists(w3_file)
        with open(w3_file) as f:
            vote = json.load(f)
        assert vote["direction"] == "trending-down"
        assert vote["source"] == "W3"

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_dedup_scores_only_once(self, mock_score, mock_feeds, tmp_vote):
        articles = [_make_article("dup1", "Big news")]
        mock_feeds.return_value = articles
        mock_score.return_value = {"direction": "bullish", "confidence": 0.9, "reason": "big"}

        state = {"seen_guids": []}
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote)
        assert mock_score.call_count == 1

        mock_feeds.return_value = articles
        mock_score.reset_mock()
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote)
        assert mock_score.call_count == 0


# ── State persistence ────────────────────────────────────────────────────

class TestState:
    def test_load_missing(self, tmp_path):
        state = rss_scorer.load_state(str(tmp_path / "nope.json"))
        assert "seen_guids" in state

    def test_save_and_load(self, tmp_path):
        path = str(tmp_path / "state.json")
        state = {"seen_guids": ["g1", "g2"], "last_check": "2026-08-07T10:00:00"}
        rss_scorer.save_state(path, state)
        loaded = rss_scorer.load_state(path)
        assert loaded["seen_guids"] == ["g1", "g2"]


# ── Night session key ────────────────────────────────────────────────────

class TestNightSessionKey:
    def test_evening(self):
        dt = datetime(2026, 8, 7, 20, 0, tzinfo=timezone(timedelta(hours=8)))
        assert rss_scorer.night_session_key(dt) == "2026-08-07|NIGHT"

    def test_after_midnight(self):
        dt = datetime(2026, 8, 8, 2, 0, tzinfo=timezone(timedelta(hours=8)))
        assert rss_scorer.night_session_key(dt) == "2026-08-07|NIGHT"
