"""新聞訊號: W3 per-article history (writer) and GET /api/news (reader).

Before this, the W3 scorer persisted only GUIDs and session aggregates, so
nothing could show WHICH articles drove a regime vote.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "news_bridge"))
import rss_scorer  # noqa: E402

from scripts.dashboard import news_feed, server  # noqa: E402
from scripts.news_bridge import rss_news_log  # noqa: E402

_TPE = timezone(timedelta(hours=8))
_EVE = datetime(2026, 8, 4, 20, 0, tzinfo=_TPE)       # Tue night session, no pause


def _article(guid, title, *, link="https://news.example/a", source="Example Wire",
             summary="<p>Chip <b>stocks</b> &amp; futures rise</p>"):
    return {"guid": guid, "title": title, "summary": summary, "link": link,
            "source": source, "published_dt": datetime(2026, 8, 4, 11, 30, tzinfo=timezone.utc)}


def _record(title, score, *, session="2026-08-04|NIGHT", source="Example Wire",
            published="2026-08-04T19:30:00+08:00", link="https://news.example/x"):
    direction = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    return {"v": 1, "scored_at": published, "published_at": published, "session": session,
            "source": source, "title": title, "link": link, "summary": "s",
            "direction": direction, "confidence": abs(score), "score": score,
            "reason": "r", "run_net": score, "session_net": score}


def _write_log(path, records, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        for line in extra_lines:
            f.write(line + "\n")


# ── writer helpers ─────────────────────────────────────────────────────

def test_default_path_sits_beside_the_state_file(tmp_path):
    state = tmp_path / "data" / "rss_scorer_state.json"
    assert rss_news_log.default_path(str(state)) == str(tmp_path / "data" / "news" / "news_log.jsonl")


def test_summary_html_is_flattened_and_truncated():
    assert rss_news_log.clean_text("<p>Chip <b>stocks</b> &amp; futures</p>") == "Chip stocks & futures"
    long = rss_news_log.clean_text("x" * 1000)
    assert len(long) == rss_news_log.SUMMARY_MAX_CHARS and long.endswith("…")


@pytest.mark.parametrize("url, ok", [
    ("https://a.example/x", True), ("http://a.example/x", True),
    ("javascript:alert(1)", False), ("JavaScript:alert(1)", False),
    ("data:text/html,<script>", False), ("/relative", False), (None, False), (42, False),
])
def test_only_http_links_survive(url, ok):
    assert (rss_news_log.safe_link(url) != "") is ok


def test_signed_score():
    assert rss_news_log.signed_score("bullish", 0.7) == 0.7
    assert rss_news_log.signed_score("BEARISH", 0.9) == -0.9
    assert rss_news_log.signed_score("neutral", 0.9) == 0.0
    assert rss_news_log.signed_score("bullish", 3) == 1.0          # clamped
    assert rss_news_log.signed_score("bullish", "junk") == 0.0


def test_source_name_prefers_the_publisher():
    assert rss_news_log.source_name({"title": "Reuters"}, "Google News", "https://g.example") == "Reuters"
    assert rss_news_log.source_name(None, "Bloomberg Markets", "https://b.example") == "Bloomberg Markets"
    assert rss_news_log.source_name(None, "", "https://feeds.example.com/rss") == "feeds.example.com"


def test_append_trims_oldest_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(rss_news_log, "MAX_BYTES", 2000)
    monkeypatch.setattr(rss_news_log, "KEEP_LINES", 5)
    path = tmp_path / "news" / "news_log.jsonl"
    for i in range(30):
        assert rss_news_log.append(str(path), [{"i": i, "pad": "x" * 100}])
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["i"] for x in lines][-1] == 29
    assert len(lines) <= 5 + 15                    # trimmed at least once


def test_append_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert rss_news_log.append(str(blocker / "sub" / "log.jsonl"), [{"a": 1}]) is False


# ── scorer integration ─────────────────────────────────────────────────

def _cfg(log_path):
    return {"feeds": ["https://example.com/rss"], "interval_minutes": 30,
            "state_file": "data/state.json", "gemini_api_key": "k",
            "discord_webhook_url": "", "news_log_path": log_path}


@patch("rss_scorer.fetch_all_feeds")
@patch("rss_scorer.score_article")
def test_every_scored_article_is_logged_once(mock_score, mock_feeds, tmp_path):
    log = tmp_path / "news" / "news_log.jsonl"
    mock_feeds.return_value = [_article("g1", "TSMC beats"), _article("g2", "Fed holds",
                                                                     link="javascript:alert(1)")]
    mock_score.side_effect = [
        {"direction": "bullish", "confidence": 0.8, "reason": "beat"},
        {"direction": "Neutral", "confidence": 0.2, "reason": "as expected"},
    ]
    state = rss_scorer.check_once(_cfg(str(log)), {"seen_guids": []}, "", now=_EVE)

    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [r["title"] for r in rows] == ["TSMC beats", "Fed holds"]
    first = rows[0]
    assert first["session"] == "2026-08-04|NIGHT"
    assert first["score"] == 0.8 and first["direction"] == "bullish"
    assert first["source"] == "Example Wire" and first["link"] == "https://news.example/a"
    assert first["summary"] == "Chip stocks & futures rise"
    assert first["published_at"] == "2026-08-04T19:30:00+08:00"
    assert first["scored_at"] == "2026-08-04T20:00:00+08:00"
    assert first["session_net"] == state["session_net"]
    assert rows[1]["direction"] == "neutral" and rows[1]["link"] == ""

    # Next run: same GUIDs are deduped by the scorer, so nothing is re-logged.
    mock_score.side_effect = None
    rss_scorer.check_once(_cfg(str(log)), state, "", now=_EVE + timedelta(minutes=30))
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


@patch("rss_scorer.fetch_all_feeds")
@patch("rss_scorer.score_article")
def test_no_log_path_writes_nothing(mock_score, mock_feeds, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mock_feeds.return_value = [_article("g1", "x")]
    mock_score.return_value = {"direction": "bullish", "confidence": 0.5, "reason": ""}
    cfg = _cfg(None)
    rss_scorer.check_once(cfg, {"seen_guids": []}, "", now=_EVE)
    assert not any(tmp_path.rglob("*.jsonl"))


def test_fetch_feed_keeps_link_and_publisher():
    entry = {"id": "g1", "title": "T", "summary": "S", "link": "https://pub.example/1",
             "source": {"title": "Reuters"}, "published_parsed": (2026, 8, 4, 11, 0, 0, 0, 0, 0)}
    fake = SimpleNamespace(entries=[entry], feed={"title": "Google News"})
    with patch.dict(sys.modules, {"feedparser": SimpleNamespace(parse=lambda *a, **k: fake)}):
        [art] = rss_scorer.fetch_feed("https://news.google.com/rss")
    assert art["link"] == "https://pub.example/1" and art["source"] == "Reuters"


def test_main_defaults_the_log_beside_the_state_file(tmp_path, monkeypatch):
    settings = tmp_path / "settings.yaml"
    state = tmp_path / "bridge" / "rss_state.json"
    settings.write_text("ai:\n  google_api_key: k\n", encoding="utf-8")
    seen = {}

    def fake_check(cfg, st, vote_out, now=None):
        seen["log"] = cfg["news_log_path"]
        return st
    monkeypatch.setattr(rss_scorer, "check_once", fake_check)
    monkeypatch.setattr(sys, "argv", ["rss_scorer.py", "--settings", str(settings),
                                      "--state", str(state), "--once"])
    assert rss_scorer.main() == 0
    assert seen["log"] == str(tmp_path / "bridge" / "news" / "news_log.jsonl")


# ── reader ─────────────────────────────────────────────────────────────

def test_reader_newest_first_skips_garbage_and_resanitises_links(tmp_path):
    log = tmp_path / "news_log.jsonl"
    _write_log(log, [
        _record("old", 0.4, published="2026-08-04T10:00:00+08:00"),
        _record("evil", -0.9, published="2026-08-04T12:00:00+08:00", link="javascript:alert(1)"),
        _record("new", 0.0, published="2026-08-04T13:00:00+08:00"),
    ], extra_lines=["not json", "[]", '{"title": ""}'])
    out = news_feed.news_feed(str(log))
    assert [r["title"] for r in out["items"]] == ["new", "evil", "old"]
    assert out["items"][1]["link"] == ""
    assert out["total"] == 3


def test_filters_and_facets(tmp_path):
    log = tmp_path / "news_log.jsonl"
    _write_log(log, [
        _record("a", 0.9, source="Reuters", session="2026-08-03|NIGHT"),
        _record("b", 0.3, source="Reuters"),
        _record("c", -0.8, source="CNBC"),
        _record("d", 0.0, source="CNBC"),
    ])

    def run(**q):
        return news_feed.news_feed(str(log), news_feed.parse_query({k: [v] for k, v in q.items()}))

    everything = run()
    assert everything["sessions"] == ["2026-08-04|NIGHT", "2026-08-03|NIGHT"]
    assert everything["sources"] == [{"name": "CNBC", "count": 2}, {"name": "Reuters", "count": 2}]
    assert {r["title"] for r in run(session="2026-08-03|NIGHT")["items"]} == {"a"}
    assert {r["title"] for r in run(source="CNBC")["items"]} == {"c", "d"}
    assert {r["title"] for r in run(min="0.7")["items"]} == {"a"}
    assert {r["title"] for r in run(max="-0.001")["items"]} == {"c"}
    assert {r["title"] for r in run(min="0", max="0")["items"]} == {"d"}
    narrowed = run(source="Reuters")
    assert narrowed["matched"] == 2 and narrowed["total"] == 4
    assert narrowed["sources"] == everything["sources"]          # facets stay full
    # net = raw sum; net_clipped = what the vote sums (each article ±0.5)
    assert narrowed["summary"] == {"bullish": 2, "bearish": 0, "neutral": 0, "net": 1.2,
                                   "net_clipped": 0.8}
    assert len(run(limit="1")["items"]) == 1


@pytest.mark.parametrize("q", [{"min": ["x"]}, {"max": ["nan"]}, {"limit": ["-1"]},
                               {"source": ["s" * 200]}])
def test_bad_query_is_rejected(q):
    with pytest.raises(news_feed.BadQuery):
        news_feed.parse_query(q)


def test_missing_log_is_empty_not_an_error(tmp_path):
    out = news_feed.news_feed(str(tmp_path / "nope.jsonl"))
    assert out["available"] is True and out["total"] == 0 and out["reason"] == "empty"
    assert news_feed.news_feed(None)["available"] is False


def test_load_news_paths_derives_the_history_beside_the_state_file(tmp_path):
    install = tmp_path / "tai_backtest"
    install.mkdir()
    sp = install / "settings.yaml"
    sp.write_text("news:\n  rss_state_file: bridge/w3_state.json\n", encoding="utf-8")
    paths = server.load_news_paths(str(sp))
    assert paths["rss_news_log"] == str(install / "bridge" / "news" / "news_log.jsonl")
    sp.write_text("news:\n  rss_news_log: logs/w3.jsonl\n", encoding="utf-8")
    assert server.load_news_paths(str(sp))["rss_news_log"] == os.path.join(str(install), "logs/w3.jsonl")


# ── endpoint ───────────────────────────────────────────────────────────

@pytest.fixture
def news_server(tmp_path, monkeypatch):
    log = tmp_path / "news" / "news_log.jsonl"
    _write_log(log, [_record("bull", 0.9, source="Reuters"), _record("bear", -0.6, source="CNBC")])
    monkeypatch.setattr(server.DashboardHandler, "base_dir", str(tmp_path))
    monkeypatch.setattr(server.DashboardHandler, "cache", server._Cache(0))
    monkeypatch.setattr(server.DashboardHandler, "news_log_path", str(log))
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        assert r.headers.get("Content-Type").startswith("application/json")
        return r.status, json.loads(r.read())


def test_endpoint_returns_the_feed(news_server):
    status, body = _get_json(news_server + "/api/news")
    assert status == 200 and body["available"] is True
    assert [r["title"] for r in body["items"]] == ["bull", "bear"]
    assert set(body["items"][0]) == {"published_at", "scored_at", "session", "source", "title",
                                      "link", "summary", "direction", "confidence", "score",
                                      "reason", "run_net", "session_net"}


def test_endpoint_filters(news_server):
    _, body = _get_json(news_server + "/api/news?source=CNBC")
    assert [r["title"] for r in body["items"]] == ["bear"]
    _, body = _get_json(news_server + "/api/news?min=0.5")
    assert [r["title"] for r in body["items"]] == ["bull"]


def test_endpoint_rejects_bad_filters(news_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(news_server + "/api/news?min=abc", timeout=10)
    assert exc.value.code == 400


def test_endpoint_without_a_configured_log(news_server, monkeypatch):
    monkeypatch.setattr(server.DashboardHandler, "news_log_path", None)
    _, body = _get_json(news_server + "/api/news")
    assert body["available"] is False and body["items"] == []


def test_page_has_the_news_view(news_server):
    with urllib.request.urlopen(news_server + "/", timeout=10) as r:
        page = r.read().decode("utf-8")
    assert "新聞訊號" in page and 'id="news-view"' in page and "/api/news" in page
    assert 'rel="noopener noreferrer"' in page or "noopener noreferrer" in page
