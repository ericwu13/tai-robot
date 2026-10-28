"""External regime votes and the news breaker on the dashboard.

The dashboard shows what the desktop regime tab shows
(``src.news.vote_status``), read through the share-delete opener so it
never blocks the bot deleting a vote file it consumed. Vote paths come
from the install's settings.yaml and never reach a client.
"""

from __future__ import annotations

import json
import os

import pytest

from scripts.dashboard import fileio, server, snapshot
from src.news import vote_status
from src.regime.switch_logic import upcoming_night_session
from tests.test_dashboard import (
    NOW, REGIME_HEADER, _alive, live_server, make_regime_bot,  # noqa: F401
)

TONIGHT = upcoming_night_session(NOW).key


def make_bridge(tmp_path, *, votes=(), w2_check="2026-10-26T09:50:00+08:00",
                w3_check="2026-10-26T09:55:00+08:00", w4_check="2026-10-25T16:15:00+08:00"):
    """A news-bridge folder laid out like Steven's n8n-bridge."""
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    for source, direction, target in votes:
        (bridge / f"regime_vote_{source.lower()}.json").write_text(json.dumps(
            {"source": source, "direction": direction, "expires_after_session": target}))
    (bridge / "monitor_state.json").write_text(json.dumps(
        {"last_check": w2_check, "last_result": "SOX -0.4% no signal"}))
    (bridge / "rss_state.json").write_text(json.dumps(
        {"last_check": w3_check, "session_net": -1.25, "session_peak": -2.0}))
    (bridge / ".chips_state.json").write_text(json.dumps(
        {"last_check": w4_check, "last_result": "foreign net short +3200"}))
    return {"regime_vote_path": str(bridge / "regime_vote.json"),
            "signal_path": str(bridge / "signal.json"),
            "rss_state_file": str(bridge / "rss_state.json")}


def _news_bot(tmp_path, *, news=None, feats=None):
    d = make_regime_bot(tmp_path, state={"last_features": feats or {
        "adx": 30.0, "_vote_sources": ["W3:trending-down", "W4:trending-down"],
        "_vote_rule": "quorum", "_vote_accelerated": True}})
    sess = json.loads((d / "session.json").read_text(encoding="utf-8"))
    sess["news_enabled"] = True
    sess["news"] = news or {"signal_suppressed": False, "event_suppressed": False}
    (d / "session.json").write_text(json.dumps(sess), encoding="utf-8")
    snapshot._stat_cache.clear()
    return d


def test_votes_match_the_desktop_regime_tab(tmp_path):
    paths = make_bridge(tmp_path, votes=[("W3", "trending-down", TONIGHT),
                                         ("W4", "trending-up", "2026-10-20|NIGHT")])
    d = _news_bot(tmp_path)
    v = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive, news_paths=paths)["regime"]["vote_status"]
    expected = vote_status.collect_vote_status(
        paths["regime_vote_path"], paths["signal_path"], paths["rss_state_file"], TONIGHT, NOW)
    assert v["tonight"] == TONIGHT
    assert [(s["source"], s["vote"], s["vote_expired"], s["stale"], s["known"]) for s in v["sources"]] \
        == [(s.source, s.vote, s.vote_expired, s.stale, s.known) for s in expected.sources]
    by = {s["source"]: s for s in v["sources"]}
    assert by["W3"]["vote"] == "trending-down" and by["W4"]["vote_expired"] is True
    assert by["W2"]["vote"] == "" and "SOX" in by["W2"]["context"]
    assert "W3" in v["consumed"] and "加速" in v["consumed"] and v["consumed_rule"] == "quorum"
    assert "accelerated" not in v["consumed"] and by["W3"]["label"] == "W3 RSS 新聞"


def test_stale_bridge_is_flagged(tmp_path):
    paths = make_bridge(tmp_path, w3_check="2026-10-25T20:00:00+08:00")     # W3 stale after 2 h
    d = _news_bot(tmp_path)
    v = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive, news_paths=paths)["regime"]["vote_status"]
    assert {s["source"]: s["stale"] for s in v["sources"]}["W3"] is True


def test_news_breaker_state_and_suppressed_count(tmp_path):
    paths = make_bridge(tmp_path)
    d = _news_bot(tmp_path, news={"signal_suppressed": True, "signal_direction": "trending-down",
                                  "signal_severity": "high", "suppressed_reason": "SOX -3.1%",
                                  "signal_suppressed_at": "2026-10-26T09:31:00",
                                  "event_suppressed": True, "event_name": "FOMC"})
    with open(d / "decisions.csv", "a", encoding="utf-8") as f:
        f.write("1999-01-01 00:00:00,2026-10-26 09:30,Demo,NEWS_SUPPRESSED,,,0,news\n")
    r = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive, news_paths=paths)["regime"]
    b = r["vote_status"]["breaker"]
    assert b["signal_suppressed"] and b["event_suppressed"] and b["event_name"] == "FOMC"
    assert (b["direction"], b["severity"], b["since"]) == ("trending-down", "high", "2026-10-26 09:31")
    assert b["suppressed_bars_24h"] == 1
    assert snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)["regime"]["news_suppressed"] is True


def test_paths_never_reach_the_client(tmp_path):
    paths = make_bridge(tmp_path, votes=[("W3", "trending-down", TONIGHT)])
    d = _news_bot(tmp_path)
    blob = json.dumps(snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive, news_paths=paths),
                      ensure_ascii=False)
    assert str(tmp_path) not in blob and "regime_vote" not in blob and "bridge" not in blob


def test_vote_files_are_read_through_the_injected_reader(tmp_path, monkeypatch):
    """The dashboard's share-delete reader replaces every vote-file read."""
    paths = make_bridge(tmp_path, votes=[("W3", "trending-down", TONIGHT)])
    seen = []

    def reader(p):
        seen.append(os.path.basename(p))
        return snapshot._read_json_dict(p)

    monkeypatch.setattr(vote_status, "_read_json",
                        lambda p: pytest.fail(f"plain open() of {p}"))
    vote_status.collect_vote_status(paths["regime_vote_path"], paths["signal_path"],
                                    paths["rss_state_file"], TONIGHT, NOW, read_json=reader)
    assert {"regime_vote_w3.json", "monitor_state.json", "rss_state.json",
            ".chips_state.json"} <= set(seen)


def test_desktop_default_reader_is_unchanged(tmp_path):
    paths = make_bridge(tmp_path, votes=[("W3", "trending-down", TONIGHT)])
    report = vote_status.collect_vote_status(paths["regime_vote_path"], paths["signal_path"],
                                             paths["rss_state_file"], TONIGHT, NOW)
    assert {s.source: s.vote for s in report.sources}["W3"] == "trending-down"


def test_history_shows_formatted_votes(tmp_path):
    d = make_regime_bot(tmp_path, history_rows=[
        "2026-10-23,NIGHT,,,,,,,x,trending-down,2,hold,,,auto,0,0,S,false,,paper,"
        "W3:trending-down+W4:trending-down*,quorum"])
    rows = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)["regime"]["history"]
    assert rows[0]["votes"] == "W3↓+W4↓ 加速"


def test_no_settings_means_no_vote_rows_but_consumed_still_shown(tmp_path):
    d = _news_bot(tmp_path)
    v = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive, news_paths=None)["regime"]["vote_status"]
    assert v["configured"] is False and v["sources"] == [] and "W3" in v["consumed"]


# ── settings.yaml: only the three vote paths ────────────────────────────

def test_load_news_paths_reads_only_vote_paths(tmp_path):
    install = tmp_path / "tai_backtest"
    (install / "data" / "live").mkdir(parents=True)
    (install / "settings.yaml").write_text(
        "login:\n  user_id: F111111111\n  password: hunter2\n"
        "news:\n  regime_vote_path: C:\\bridge\\regime_vote.json\n"
        "  signal_path: bridge/signal.json\n", encoding="utf-8")
    sp = server.default_settings_path(str(install / "data" / "live"))
    assert sp == str(install / "settings.yaml")
    paths = server.load_news_paths(sp)
    assert set(paths) == {"regime_vote_path", "signal_path", "rss_state_file"}
    assert paths["regime_vote_path"] == "C:\\bridge\\regime_vote.json"
    assert paths["signal_path"] == os.path.join(str(install), "bridge/signal.json")
    assert paths["rss_state_file"] == os.path.join(str(install), "data/rss_scorer_state.json")
    assert "hunter2" not in json.dumps(paths)


@pytest.mark.parametrize("content", [None, "news: [not, a, mapping]\n", ":::\n- bad yaml: [\n"])
def test_load_news_paths_tolerates_missing_or_bad_settings(tmp_path, content):
    sp = tmp_path / "settings.yaml"
    if content is not None:
        sp.write_text(content, encoding="utf-8")
    assert server.load_news_paths(str(sp)) is None
