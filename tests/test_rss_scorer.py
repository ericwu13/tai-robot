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

    def test_bearish_dominant_produces_negative_net(self):
        """Regression: bearish(0.9)+bearish(0.8)+3×neutral(0.1) must be -1.7."""
        scored = [
            {"article": {}, "score": {"direction": "bearish", "confidence": 0.9}},
            {"article": {}, "score": {"direction": "bearish", "confidence": 0.8}},
            {"article": {}, "score": {"direction": "neutral", "confidence": 0.1}},
            {"article": {}, "score": {"direction": "neutral", "confidence": 0.1}},
            {"article": {}, "score": {"direction": "neutral", "confidence": 0.1}},
        ]
        net = rss_scorer.aggregate_scores(scored)
        assert net == pytest.approx(-1.7)
        assert rss_scorer.net_score_to_vote(net) == "trending-down"

    def test_case_insensitive_direction(self):
        """Gemini may return 'Bearish' — must still subtract."""
        scored = [
            {"article": {}, "score": {"direction": "Bearish", "confidence": 0.9}},
            {"article": {}, "score": {"direction": "BULLISH", "confidence": 0.3}},
        ]
        net = rss_scorer.aggregate_scores(scored)
        assert net == pytest.approx(-0.6)


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

_TPE = timezone(timedelta(hours=8))
# Fixed weekday evening (Tue 2026-08-04 20:00 TPE) — inside the night
# session, outside the weekend pause, so tests never depend on wall time.
_WEEKDAY_EVE = datetime(2026, 8, 4, 20, 0, tzinfo=_TPE)


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
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote, now=_WEEKDAY_EVE)

        assert not os.path.exists(_w3_path(tmp_vote))

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_strong_single_run_votes_immediately(self, mock_score, mock_feeds, tmp_vote):
        """Requirement 1: a strong enough signal fires within ONE run."""
        mock_feeds.return_value = [
            _make_article("b1", "Markets surge"),
            _make_article("b2", "Tech rally extends"),
        ]
        mock_score.return_value = {"direction": "bullish", "confidence": 1.0, "reason": "surge"}

        state = {"seen_guids": []}
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote, now=_WEEKDAY_EVE)

        w3_file = _w3_path(tmp_vote)
        assert os.path.exists(w3_file)
        with open(w3_file) as f:
            vote = json.load(f)
        assert vote["direction"] == "trending-up"
        assert vote["source"] == "W3"
        assert vote["version"] == 1

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_single_below_threshold_run_does_not_vote(self, mock_score, mock_feeds, tmp_vote):
        """Requirement 2: one ±0.8-style run (old per-run threshold) must
        NOT set the standing vote anymore — that was the weekend
        flip-flop bug."""
        mock_feeds.return_value = [
            _make_article("c1", "Markets dip"),
            _make_article("c2", "Mild sell-off"),
        ]
        mock_score.return_value = {"direction": "bearish", "confidence": 0.5, "reason": "dip"}

        state = {"seen_guids": []}
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote, now=_WEEKDAY_EVE)

        assert not os.path.exists(_w3_path(tmp_vote))
        assert state["session_net"] == pytest.approx(-1.0)

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_session_accumulates_to_vote(self, mock_score, mock_feeds, tmp_vote):
        """Two below-threshold runs in the same session accumulate and
        fire the vote on the second run."""
        cfg = self._mock_cfg()
        state = {"seen_guids": []}
        mock_score.return_value = {"direction": "bearish", "confidence": 0.5, "reason": "down"}

        mock_feeds.return_value = [_make_article("s1", "A"), _make_article("s2", "B")]
        state = rss_scorer.check_once(cfg, state, tmp_vote, now=_WEEKDAY_EVE)
        assert not os.path.exists(_w3_path(tmp_vote))

        mock_feeds.return_value = [_make_article("s3", "C"), _make_article("s4", "D")]
        state = rss_scorer.check_once(cfg, state, tmp_vote,
                                      now=_WEEKDAY_EVE + timedelta(minutes=30))

        w3_file = _w3_path(tmp_vote)
        assert os.path.exists(w3_file)
        with open(w3_file) as f:
            vote = json.load(f)
        assert vote["direction"] == "trending-down"
        assert state["session_net"] == pytest.approx(-2.0)

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_flip_flop_runs_cancel(self, mock_score, mock_feeds, tmp_vote):
        """The weekend scenario: an up run followed by an equal down run
        nets to zero — no standing vote."""
        cfg = self._mock_cfg()
        state = {"seen_guids": []}

        mock_feeds.return_value = [_make_article("f1", "A"), _make_article("f2", "B")]
        mock_score.return_value = {"direction": "bullish", "confidence": 0.6, "reason": "up"}
        state = rss_scorer.check_once(cfg, state, tmp_vote, now=_WEEKDAY_EVE)

        mock_feeds.return_value = [_make_article("f3", "C"), _make_article("f4", "D")]
        mock_score.return_value = {"direction": "bearish", "confidence": 0.6, "reason": "down"}
        state = rss_scorer.check_once(cfg, state, tmp_vote,
                                      now=_WEEKDAY_EVE + timedelta(minutes=30))

        assert not os.path.exists(_w3_path(tmp_vote))
        assert state["session_net"] == pytest.approx(0.0)

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_session_rollover_resets_accumulator(self, mock_score, mock_feeds, tmp_vote):
        """A new night session starts from zero — yesterday's near-miss
        must not leak into tonight's vote."""
        cfg = self._mock_cfg()
        state = {"seen_guids": []}
        mock_score.return_value = {"direction": "bearish", "confidence": 0.5, "reason": "down"}

        mock_feeds.return_value = [_make_article("r1", "A"), _make_article("r2", "B")]
        state = rss_scorer.check_once(cfg, state, tmp_vote, now=_WEEKDAY_EVE)
        assert state["session_net"] == pytest.approx(-1.0)

        mock_feeds.return_value = [_make_article("r3", "C"), _make_article("r4", "D")]
        state = rss_scorer.check_once(cfg, state, tmp_vote,
                                      now=_WEEKDAY_EVE + timedelta(days=1))
        assert state["session_net"] == pytest.approx(-1.0)
        assert not os.path.exists(_w3_path(tmp_vote))

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_weekend_pause_skips_scoring(self, mock_score, mock_feeds, tmp_vote):
        """Sat 05:00 - Mon 15:00 TPE: no fetch, no Gemini, no vote."""
        saturday = datetime(2026, 8, 8, 10, 0, tzinfo=_TPE)
        state = {"seen_guids": []}
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote, now=saturday)

        assert mock_feeds.call_count == 0
        assert mock_score.call_count == 0
        assert not os.path.exists(_w3_path(tmp_vote))
        assert "last_check" in state

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_dedup_scores_only_once(self, mock_score, mock_feeds, tmp_vote):
        articles = [_make_article("dup1", "Big news")]
        mock_feeds.return_value = articles
        mock_score.return_value = {"direction": "bullish", "confidence": 0.9, "reason": "big"}

        state = {"seen_guids": []}
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote, now=_WEEKDAY_EVE)
        assert mock_score.call_count == 1

        mock_feeds.return_value = articles
        mock_score.reset_mock()
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote,
                                      now=_WEEKDAY_EVE + timedelta(minutes=30))
        assert mock_score.call_count == 0

    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    @patch("rss_scorer._get_write_regime_vote")
    def test_state_preserved_when_vote_write_fails(
        self, mock_get_vote, mock_score, mock_feeds, tmp_vote
    ):
        """Regression: if write_regime_vote raises, GUIDs must still be in
        the returned state so save_state() in main() persists them."""
        mock_feeds.return_value = [
            _make_article("fail1", "Big crash"),
            _make_article("fail2", "Markets tank"),
        ]
        mock_score.return_value = {"direction": "bearish", "confidence": 1.0, "reason": "crash"}
        mock_get_vote.side_effect = ImportError("regime_vote not found")

        state = {"seen_guids": []}
        state = rss_scorer.check_once(self._mock_cfg(), state, tmp_vote, now=_WEEKDAY_EVE)

        assert mock_get_vote.call_count == 1
        assert "fail1" in state["seen_guids"]
        assert "fail2" in state["seen_guids"]
        assert "last_check" in state


# ── Embed gating: post only when something changed ───────────────────────

class TestEmbedGating:
    def _cfg(self):
        return {
            "feeds": ["https://example.com/rss"],
            "interval_minutes": 30,
            "state_file": "data/state.json",
            "gemini_api_key": "fake-key",
            "discord_webhook_url": "https://discord.example/webhook",
        }

    @patch("rss_scorer.post_discord_embed")
    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_scored_run_posts(self, mock_score, mock_feeds, mock_embed, tmp_vote):
        mock_feeds.return_value = [_make_article("e1", "News")]
        mock_score.return_value = {"direction": "neutral", "confidence": 0.2, "reason": "x"}
        state = rss_scorer.check_once(self._cfg(), {"seen_guids": []}, tmp_vote, now=_WEEKDAY_EVE)
        assert mock_embed.call_count == 1

    @patch("rss_scorer.post_discord_embed")
    @patch("rss_scorer.fetch_all_feeds")
    def test_no_new_articles_stays_silent(self, mock_feeds, mock_embed, tmp_vote):
        mock_feeds.return_value = []
        rss_scorer.check_once(self._cfg(), {"seen_guids": []}, tmp_vote, now=_WEEKDAY_EVE)
        assert mock_embed.call_count == 0

    @patch("rss_scorer.post_discord_embed")
    @patch("rss_scorer.fetch_all_feeds")
    def test_weekend_pause_posts_once(self, mock_feeds, mock_embed, tmp_vote):
        saturday = datetime(2026, 8, 8, 10, 0, tzinfo=_TPE)
        state = {"seen_guids": []}
        state = rss_scorer.check_once(self._cfg(), state, tmp_vote, now=saturday)
        assert mock_embed.call_count == 1
        state = rss_scorer.check_once(self._cfg(), state, tmp_vote,
                                      now=saturday + timedelta(minutes=30))
        assert mock_embed.call_count == 1  # no second post
        assert mock_feeds.call_count == 0

    @patch("rss_scorer.post_discord_embed")
    @patch("rss_scorer.fetch_all_feeds")
    @patch("rss_scorer.score_article")
    def test_pause_notify_rearms_after_weekday_run(self, mock_score, mock_feeds,
                                                   mock_embed, tmp_vote):
        """Weekday run resets the pause flag so NEXT weekend announces again."""
        mock_feeds.return_value = [_make_article("e2", "News")]
        mock_score.return_value = {"direction": "neutral", "confidence": 0.2, "reason": "x"}
        state = {"seen_guids": [], "weekend_pause_notified": True}
        state = rss_scorer.check_once(self._cfg(), state, tmp_vote, now=_WEEKDAY_EVE)
        assert state["weekend_pause_notified"] is False


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
