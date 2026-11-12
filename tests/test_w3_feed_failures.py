"""W3 feed list, fetch failures, and TAIFEX scoring rule.

Each test in this module fails on master (dc8b72b) before the W3 fix:
dead feeds stay in DEFAULT_FEEDS, a dead fetch is silent, and the scoring
prompt has no TAIFEX-only administrative-notice rule.

No test hits the network. ``getaddrinfo`` is blocked for the whole module;
feeds are served from tests/fixtures/news or from bytes the test builds.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "news_bridge"))
import rss_scorer  # noqa: E402

from scripts.monitor.check_bridge import check_bridge  # noqa: E402
from src.news.vote_status import _w3_status  # noqa: E402

_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "news"
_TPE = timezone(timedelta(hours=8))
# Wednesday evening — outside the Sat 05:00–Mon 05:00 scoring pause.
_NOW = datetime(2026, 11, 11, 20, 0, tzinfo=_TPE)
_REMOVED = (
    "https://finance.yahoo.com/news/rssindex",
    "https://technews.tw/feed/",
)
_ADDED = (
    "https://tw.stock.yahoo.com/rss?category=tw-market",
    "https://news.cnyes.com/rss/v1/news/category/tw_stock",
    "https://www.taifex.com.tw/cht/11/RSS1",
)
_ADMIN_MARKER = "exchange administrative notices"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Block DNS so a missed urlopen mock cannot leave the machine."""
    def blocked(*_a, **_k):
        raise socket.gaierror(11001, "getaddrinfo failed")
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


class _Body:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self):
        return self._payload

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _recent_rss(title: str, link: str, guid: str | None = None) -> bytes:
    stamp = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=30))
    guid_xml = f"<guid>{guid}</guid>" if guid else ""
    return (
        "<?xml version='1.0'?><rss version='2.0'><channel><item>"
        f"<title>{title}</title><link>{link}</link>{guid_xml}"
        f"<pubDate>{stamp}</pubDate><description>summary {title}</description>"
        "</item></channel></rss>"
    ).encode()


def _empty_rss() -> bytes:
    return b"<?xml version='1.0'?><rss version='2.0'><channel></channel></rss>"


def _gaierror():
    return urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))


def _cfg(tmp_path, feeds, **extra):
    state = tmp_path / "bridge" / "rss_scorer_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    cfg = {
        "feeds": list(feeds),
        "gemini_api_key": "k",
        "discord_webhook_url": "",
        "discord_mode": "off",
        "news_log_path": "",
        "state_file": str(state),
    }
    cfg.update(extra)
    return cfg, str(state)


def _runs(state_path):
    path = Path(state_path).parent / "runs" / "W3.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _w3_vote(base: str) -> str:
    stem, ext = os.path.splitext(base)
    return f"{stem}_w3{ext}"


# ── 1. settings.example.yaml mirrors DEFAULT_FEEDS ───────────────────────

def test_settings_example_rss_feeds_equal_default_feeds():
    root = Path(__file__).resolve().parents[1]
    example = yaml.safe_load((root / "settings.example.yaml").read_text(encoding="utf-8"))
    listed = (example.get("news") or {}).get("rss_feeds")
    assert listed == list(rss_scorer.DEFAULT_FEEDS)
    blob = " ".join(rss_scorer.DEFAULT_FEEDS).lower()
    for dead in _REMOVED:
        assert dead not in rss_scorer.DEFAULT_FEEDS
        assert dead not in listed
    assert "technews" not in blob
    assert "moneydj" not in blob
    for url in _ADDED:
        assert url in rss_scorer.DEFAULT_FEEDS
        assert url in listed


# ── 2. every feed down: vote, EMA and seen list stay; the run fails ──────

def test_all_feeds_network_error_keeps_vote_and_records_failed(tmp_path, monkeypatch):
    feeds = ["https://a.example/rss", "https://b.example/rss"]
    cfg, state_path = _cfg(tmp_path, feeds, discord_mode="errors",
                           discord_webhook_url="https://discord.example/hook")
    vote = _w3_vote(str(tmp_path / "bridge" / "regime_vote.json"))
    Path(vote).write_bytes(b'{"direction":"trending-up","source":"W3"}\n')
    before_vote = Path(vote).read_bytes()
    seeded = {
        "seen_guids": ["already-scored"],
        "session_net": 1.25,
        "session_peak": 2.5,
        "session_key": "2026-11-11|NIGHT",
        "recent_runs": [0.4],
        "error_streak": 3,
        "last_error": "HTTP 429: old gemini",
    }
    notes = []

    def post(*_a, **kwargs):
        notes.append(kwargs.get("note") or "")
        return True

    def boom(_req, timeout=None, **_k):
        raise _gaierror()

    monkeypatch.setattr(rss_scorer, "post_discord_embed", post)
    monkeypatch.setattr(rss_scorer.urllib.request, "urlopen", boom)
    state = rss_scorer._recorded_pass(cfg, dict(seeded), str(tmp_path / "bridge" / "regime_vote.json"),
                                       state_path)
    saved = json.loads(Path(state_path).read_text(encoding="utf-8"))

    assert saved["session_net"] == 1.25
    assert saved["session_peak"] == 2.5
    assert saved["seen_guids"] == ["already-scored"]
    assert saved["recent_runs"] == [0.4]
    assert saved["error_streak"] == 3  # Gemini streak, not the fetch
    assert Path(vote).read_bytes() == before_vote
    assert saved["fetch_status"] == "failed"
    assert saved["feeds_ok"] == 0 and saved["feeds_failed"] == 2
    assert saved["fetch_error_streak"] == 1
    assert len(saved["last_feed_errors"]) == 2
    assert all(item["kind"] == "network" for item in saved["last_feed_errors"])
    assert "last_fetch_ok_at" not in saved
    row = _runs(state_path)[-1]
    assert row["ok"] is False
    assert row["error"].startswith("all 2 feeds failed")
    assert "gemini" not in row["error"].lower()
    assert notes and "fetch failed" in notes[0].lower()
    assert "gemini" not in notes[0].lower()

    rss_scorer._recorded_pass(cfg, state, str(tmp_path / "bridge" / "regime_vote.json"), state_path)
    assert notes == [notes[0]]  # errors mode fires once
    assert json.loads(Path(state_path).read_text(encoding="utf-8"))["fetch_error_streak"] == 2


def test_transport_http_and_bozo_count_as_failed(tmp_path, monkeypatch):
    """DNS, HTTP >= 400, and bozo-with-zero-entries are failures, not quiet feeds."""
    cases = {
        "network": lambda req, timeout=None, **_k: (_ for _ in ()).throw(_gaierror()),
        "http": lambda req, timeout=None, **_k: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=None)),
        "bozo": lambda req, timeout=None, **_k: _Body(b"this is not an rss document"),
    }
    for kind, fake in cases.items():
        cfg, state_path = _cfg(tmp_path / kind, ["https://feed.example/rss"])
        monkeypatch.setattr(rss_scorer.urllib.request, "urlopen", fake)
        state = rss_scorer._recorded_pass(cfg, {"session_net": 0.5, "error_streak": 1}, "", state_path)
        assert state["fetch_status"] == "failed", kind
        assert state["session_net"] == 0.5, kind
        assert state["error_streak"] == 1, kind
        assert state["last_feed_errors"][0]["kind"] == kind
        assert _runs(state_path)[-1]["ok"] is False


def test_feed_errors_are_capped_at_five(tmp_path, monkeypatch):
    feeds = [f"https://feed{i}.example/rss" for i in range(7)]
    cfg, state_path = _cfg(tmp_path, feeds)
    monkeypatch.setattr(rss_scorer.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(_gaierror()))
    state = rss_scorer.check_once(cfg, {}, "", now=_NOW)
    assert state["feeds_failed"] == 7
    assert len(state["last_feed_errors"]) == 5


# ── 3. one feed down still scores, and the pass is degraded ─────────────

def test_partial_failure_still_scores(tmp_path, monkeypatch):
    feeds = [
        "https://ok-1.example/rss",
        "https://down.example/rss",
        "https://ok-2.example/rss",
    ]
    cfg, state_path = _cfg(tmp_path, feeds)

    def fake_open(req, timeout=None, **_k):
        if "down.example" in req.full_url:
            raise _gaierror()
        return _Body(_recent_rss("TSMC beats", req.full_url + "/item", guid=req.full_url))

    monkeypatch.setattr(rss_scorer.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(rss_scorer, "score_article",
                        lambda *_a, **_k: {"direction": "bullish", "confidence": 0.9, "reason": "beat"})
    seeded = {"seen_guids": [], "session_net": 0.4, "session_peak": 0.4,
              "session_key": "2026-11-11|NIGHT", "error_streak": 0}
    state = rss_scorer._recorded_pass(cfg, dict(seeded), "", state_path)
    assert state["fetch_status"] == "degraded"
    assert state["feeds_ok"] == 2 and state["feeds_failed"] == 1
    assert state["fetch_error_streak"] == 0
    assert state["last_fetch_ok_at"]
    assert state["session_net"] != 0.4  # EMA moved
    assert state["error_streak"] == 0  # Gemini streak still means scoring
    assert _runs(state_path)[-1]["ok"] is True


# ── 4. a 200 with nothing to score is a quiet feed ───────────────────────

def test_all_200_empty_is_quiet(tmp_path, monkeypatch):
    cfg, state_path = _cfg(tmp_path, ["https://a.example/rss", "https://b.example/rss"])
    monkeypatch.setattr(rss_scorer.urllib.request, "urlopen",
                        lambda *_a, **_k: _Body(_empty_rss()))
    state = rss_scorer._recorded_pass(
        cfg, {"session_net": 0.8, "session_peak": 0.8, "error_streak": 0}, "", state_path)
    assert state["fetch_status"] == "ok"
    assert state["feeds_ok"] == 2 and state["feeds_failed"] == 0
    assert state["session_net"] == 0.8
    assert state["fetch_error_streak"] == 0
    row = _runs(state_path)[-1]
    assert row["ok"] is True and row["exit_code"] == 0


def test_nothing_recent_is_quiet_not_failed(tmp_path, monkeypatch):
    old = format_datetime(datetime.now(timezone.utc) - timedelta(days=10))
    body = (
        "<?xml version='1.0'?><rss version='2.0'><channel><item>"
        f"<title>old</title><link>https://a.example/old</link><guid>old-1</guid>"
        f"<pubDate>{old}</pubDate><description>stale</description>"
        "</item></channel></rss>"
    ).encode()
    cfg, state_path = _cfg(tmp_path, ["https://a.example/rss"])
    monkeypatch.setattr(rss_scorer.urllib.request, "urlopen", lambda *_a, **_k: _Body(body))
    state = rss_scorer.check_once(cfg, {"session_net": 0.3}, "", now=_NOW)
    assert state["fetch_status"] == "ok"
    assert state["session_net"] == 0.3


# ── 5. vote_status and check_bridge: P2 failed, P3 degraded ─────────────

def _bridge(tmp_path, rss_state):
    d = tmp_path / "bridge"
    d.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 8, 25, 6, 0, tzinfo=_TPE)
    fresh = (now - timedelta(minutes=20)).isoformat(timespec="seconds")
    (d / "monitor_state.json").write_text(json.dumps({
        "last_check": (now - timedelta(minutes=10)).isoformat(timespec="seconds"),
        "last_result": "fresh 5/11 | fired: none",
    }), encoding="utf-8")
    rss_state = {"last_check": fresh, "session_net": 0.4, "error_streak": 0, **rss_state}
    (d / "rss_scorer_state.json").write_text(json.dumps(rss_state), encoding="utf-8")
    (d / ".chips_state.json").write_text(json.dumps({
        "last_check": (now - timedelta(hours=14)).isoformat(timespec="seconds"),
        "last_result": "vote trending-up",
    }), encoding="utf-8")
    (d / "events.json").write_text(json.dumps({
        "version": 1,
        "updated_at": (now - timedelta(hours=6)).isoformat(timespec="seconds"),
        "events": [],
    }), encoding="utf-8")
    (d / "monitor.log").write_text(
        f"{(now - timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')} TPE | fresh 5/11\n",
        encoding="utf-8")
    (d / "chips_monitor.log").write_text(
        "2026-08-24 16:15:04 TPE | vote trending-up\n"
        "2026-08-24 18:15:04 TPE | no vote — hysteresis\n",
        encoding="utf-8")
    settings = {"news": {
        "signal_path": str(d / "signal.json"),
        "regime_vote_path": str(d / "regime_vote.json"),
        "events_path": str(d / "events.json"),
        "rss_state_file": str(d / "rss_scorer_state.json"),
    }}
    return now, settings, d / "rss_scorer_state.json"


def test_vote_status_and_check_bridge_p2_failed_p3_degraded(tmp_path):
    now, settings, rss_path = _bridge(tmp_path / "failed", {
        "fetch_status": "failed",
        "feeds_ok": 0,
        "feeds_failed": 12,
        "fetch_error_streak": 4,
        "last_feed_errors": [{
            "url": "https://feeds.bloomberg.com/markets/news.rss",
            "kind": "network",
            "message": "getaddrinfo failed",
        }],
    })
    w3 = _w3_status(str(rss_path), now)
    assert w3.fetch_failing is True
    assert w3.fetch_error_streak == 4
    assert w3.degraded is False
    assert w3.failing is False  # Gemini error_streak is still 0
    findings, _lines = check_bridge(now, settings, str(tmp_path / "live"))
    p2 = [f.message for f in findings if f.level == "P2"]
    p3 = [f.message for f in findings if f.level == "P3"]
    assert any(m.startswith("W3 every feed failed ×4:") and "getaddrinfo failed" in m for m in p2), p2
    assert not any(m.startswith("W3 degraded") for m in p3)

    now, settings, rss_path = _bridge(tmp_path / "degraded", {
        "fetch_status": "degraded",
        "feeds_ok": 2,
        "feeds_failed": 1,
        "fetch_error_streak": 0,
        "last_feed_errors": [{
            "url": "https://finance.yahoo.com/news/rssindex",
            "kind": "http",
            "message": "HTTP 404: Not Found",
        }],
    })
    w3 = _w3_status(str(rss_path), now)
    assert w3.fetch_failing is False
    assert w3.degraded is True
    assert w3.feeds_failed == 1 and w3.feeds_ok == 2
    findings, _lines = check_bridge(now, settings, str(tmp_path / "live"))
    p2 = [f.message for f in findings if f.level == "P2"]
    p3 = [f.message for f in findings if f.level == "P3"]
    assert any(m.startswith("W3 degraded 1/3 feeds failing (finance.yahoo.com)") for m in p3), p3
    assert not any("every feed failed" in m for m in p2)


# ── 8. recorded fixtures: counts, dates, titles, distinct GUIDs ─────────

def _parse_fixture(monkeypatch, name: str, url: str):
    raw = (_FIXTURES / name).read_bytes()
    monkeypatch.setattr(rss_scorer.urllib.request, "urlopen",
                        lambda *_a, **_k: _Body(raw))
    return raw, rss_scorer.fetch_feed(url)


def test_parser_fixtures_taifex_yahoo_cnyes(monkeypatch):
    taifex_raw, taifex = _parse_fixture(
        monkeypatch, "taifex_rss1.xml", "https://www.taifex.com.tw/cht/11/RSS1")
    assert not taifex_raw.lstrip().startswith(b"<?xml")
    assert taifex_raw.split(b"<rss", 1)[0].count(b"\n") >= 13
    assert len(taifex) == 30
    assert all(a["feed_tag"] == "taifex" for a in taifex)
    shared = "https://www.taifex.com.tw/cht/5/sSOFSP"
    same_link = [a for a in taifex if a["link"] == shared]
    assert len(same_link) >= 2
    assert len({a["guid"] for a in same_link}) == len(same_link)
    assert all(a["guid"] == f"{a['link']}|{a['title']}" for a in same_link)
    guf = next(a for a in taifex if "全新期貨(GUF)" in a["title"])
    assert guf["title"] == "公告調整本公司全新期貨(GUF)保證金適用比例，請查照。"
    assert guf["published_dt"] == datetime(2026, 11, 11, 7, 44, 30, tzinfo=timezone.utc)

    _yahoo_raw, yahoo = _parse_fixture(
        monkeypatch, "yahoo_tw_market.xml",
        "https://tw.stock.yahoo.com/rss?category=tw-market")
    assert len(yahoo) == 50
    assert all(a["feed_tag"] == "" for a in yahoo)
    assert all(a["guid"] == f"{a['link']}|{a['title']}" and a["guid"] != a["link"] for a in yahoo)
    assert yahoo[0]["title"] == "【Yahoo早盤】台股多空拉鋸...一度跌378點失守4萬4！分析師黃紫東：收紅有難"
    assert yahoo[0]["published_dt"] == datetime(2026, 11, 12, 1, 36, 4, tzinfo=timezone.utc)

    _cnyes_raw, cnyes = _parse_fixture(
        monkeypatch, "cnyes_tw_stock.xml",
        "https://news.cnyes.com/rss/v1/news/category/tw_stock")
    assert len(cnyes) == 90
    assert all(a["feed_tag"] == "" for a in cnyes)
    assert cnyes[0]["title"] == "〈台股開盤〉台積電跌逾1%大盤平盤震盪 聯發科領軍ASIC、光通訊股走強"
    assert cnyes[0]["guid"] == "https://news.cnyes.com/news/id/6660068"
    assert "|" not in cnyes[0]["guid"]
    assert cnyes[0]["published_dt"] == datetime(2026, 11, 12, 1, 34, 32, tzinfo=timezone.utc)


# ── 9. urllib timeout and the existing User-Agent ────────────────────────

def test_urlopen_called_with_timeout_and_existing_ua(monkeypatch):
    seen = {}

    def fake(req, timeout=None, **_k):
        seen["timeout"] = timeout
        seen["ua"] = req.get_header("User-agent")
        raise _gaierror()

    monkeypatch.setattr(rss_scorer.urllib.request, "urlopen", fake)
    rss_scorer.fetch_all_feeds(["https://example.test/rss"])
    assert seen.get("timeout") == 20
    assert seen.get("ua") == "Mozilla/5.0 (tai-robot news bridge)"
    assert seen["ua"] == rss_scorer._UA["User-Agent"]


# ── Q3. administrative-notice rule is TAIFEX-only ────────────────────────

def test_taifex_admin_rule_applies_only_to_taifex_items(monkeypatch):
    assert _ADMIN_MARKER not in rss_scorer.SCORING_PROMPT
    prompt_for = getattr(rss_scorer, "scoring_prompt", None)
    assert prompt_for is not None, "scoring_prompt missing"
    other = prompt_for("TSMC beats", "guidance raised", source="Bloomberg")
    blank = prompt_for("TSMC beats", "guidance raised", source="")
    taifex = prompt_for("公告結算價", "", source="taifex")
    assert other == rss_scorer.SCORING_PROMPT.format(headline="TSMC beats", summary="guidance raised")
    assert _ADMIN_MARKER not in other and _ADMIN_MARKER not in blank
    assert _ADMIN_MARKER in taifex and "TAIFEX" in taifex
    assert _ADMIN_MARKER in prompt_for("x", "y", source="TAIFEX")

    captured = []

    def fake_open(req, timeout=None, **_k):
        captured.append(json.loads(req.data.decode("utf-8"))["contents"][0]["parts"][0]["text"])
        raise urllib.error.URLError("stop")

    monkeypatch.setattr(rss_scorer.urllib.request, "urlopen", fake_open)
    for args in (("公告結算價", "", "k"), ("TSMC beats", "guidance raised", "k")):
        source = "taifex" if args[0].startswith("公告") else ""
        try:
            rss_scorer.score_article(*args, source=source)
        except rss_scorer.GeminiAPIError:
            pass
    assert _ADMIN_MARKER in captured[0]
    assert _ADMIN_MARKER not in captured[1]

    seen = []

    def fake_score(headline, summary, api_key, source=""):
        seen.append((summary, source))
        return {"direction": "neutral", "confidence": 0.2, "reason": "admin"}

    monkeypatch.setattr(rss_scorer, "score_article", fake_score)
    rss_scorer.score_articles([
        {"guid": "t", "title": "公告結算價", "summary": "公告結算價", "feed_tag": "taifex"},
        {"guid": "o", "title": "TSMC", "summary": "TSMC beats", "feed_tag": ""},
    ], "k")
    assert seen == [("", "taifex"), ("TSMC beats", "")]
