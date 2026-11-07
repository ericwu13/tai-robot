"""市場脈動 (GET /api/news/pulse): what the W3 Discord embed used to say,
plus votes, events, the run series and the drivers of tonight's net.

The drivers must reproduce the scorer's own arithmetic (per-article ±0.5
clip, one EMA decay per scoring run) — a page that disagrees with the
number that fires the vote is worse than no page.
"""
from __future__ import annotations

import json
import math
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from scripts.dashboard import news_feed, news_pulse, server
from scripts.news_bridge import rss_scorer

TPE = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 4, 21, 0, tzinfo=TPE)          # Tue night session
SESSION = "2026-08-04|NIGHT"


def _row(title, score, scored_at, *, session=SESSION, run_net=0.0, session_net=0.0):
    direction = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    return {"v": 1, "scored_at": scored_at, "published_at": scored_at, "session": session,
            "source": "Wire", "title": title, "link": "https://news.example/x",
            "summary": "s", "direction": direction, "confidence": abs(score),
            "score": score, "reason": f"why {title}", "run_net": run_net,
            "session_net": session_net}


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _reader(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _items(tmp_path, rows):
    log = tmp_path / "news" / "news_log.jsonl"
    _write(log, rows)
    return news_feed.read_items(str(log)), str(log)


RUN1 = "2026-08-04T20:00:00+08:00"
RUN2 = "2026-08-04T20:30:00+08:00"


def test_runs_group_rows_by_scoring_pass(tmp_path):
    items, _ = _items(tmp_path, [
        _row("a", 0.9, RUN1, run_net=0.8, session_net=0.8),
        _row("b", -0.3, RUN1, run_net=0.8, session_net=0.8),
        _row("c", 0.0, RUN2, run_net=0.0, session_net=0.673),
        _row("old", 0.5, "2026-07-01T20:00:00+08:00", session="2026-07-01|NIGHT"),
    ])
    runs = news_pulse.build_runs(items, NOW)
    assert [r["scored_at"] for r in runs] == [RUN1, RUN2]           # oldest first, old run cut
    assert runs[0]["n"] == 2 and runs[0]["bullish"] == 1 and runs[0]["bearish"] == 1
    assert runs[1]["session_net"] == 0.673
    # chart time = TPE wall clock encoded as UTC
    assert runs[0]["t"] == int(datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc).timestamp())


def test_drivers_match_the_scorers_ema(tmp_path):
    """Replay the scorer's own formula: net = prev*decay + Σclip(score)."""
    run1 = [0.9, -0.3]              # clipped 0.5, -0.3 → run net 0.2
    run2 = [-0.8]                   # clipped -0.5
    items, _ = _items(tmp_path, [_row(f"r1-{i}", s, RUN1) for i, s in enumerate(run1)]
                      + [_row("r2-0", run2[0], RUN2)])
    decay = rss_scorer.EMA_DECAY
    expected_net = (0.5 - 0.3) * decay + (-0.5)
    drivers = news_pulse.build_drivers(items, SESSION, decay)
    assert math.isclose(drivers["total"], expected_net, abs_tol=0.002)
    assert drivers["runs"] == 2
    assert [d["title"] for d in drivers["down"]] == ["r2-0", "r1-1"]
    assert drivers["up"][0]["title"] == "r1-0"
    assert math.isclose(drivers["up"][0]["contribution"], 0.5 * decay, abs_tol=0.001)
    assert drivers["up"][0]["reason"] == "why r1-0"
    assert drivers["rest_count"] == 0 and drivers["rest"] == 0


def test_drivers_ignore_other_sessions_and_neutral(tmp_path):
    items, _ = _items(tmp_path, [
        _row("other", 0.9, RUN1, session="2026-08-03|NIGHT"),
        _row("flat", 0.0, RUN2),
    ])
    d = news_pulse.build_drivers(items, SESSION, 0.84)
    assert d["up"] == [] and d["down"] == [] and d["total"] == 0


def test_w3_state_failing_eroding_and_threshold():
    st = news_pulse.w3_state({
        "session_key": SESSION, "session_net": 1.1, "session_peak": 2.9,
        "recent_runs": [0.4, -0.9, -0.2], "last_check": "2026-08-04T20:30:00+08:00",
        "error_streak": 2, "last_error": "HTTP 404: model not found",
        "last_fetched": 30, "last_new": 3, "last_articles_scored": 0, "last_skipped": 0,
    }, NOW, SESSION, 1.5)
    assert st["current"] and st["session_net"] == 1.1
    assert st["eroding"]                                   # 2.9 → 1.1 dropped > 1.5
    assert st["to_threshold"] == 0.4 and st["direction"] == ""
    assert st["momentum"] == -0.7
    assert st["failing"] and st["error_streak"] == 2
    assert st["last_error"] == "HTTP 404: model not found"
    assert st["last_fetched"] == 30 and st["last_scored"] == 0


def test_w3_state_from_an_earlier_session_reads_as_zero_tonight():
    st = news_pulse.w3_state({"session_key": "2026-08-03|NIGHT", "session_net": -2.0,
                              "session_peak": -2.5, "recent_runs": [-1.0]},
                             NOW, SESSION, 1.5)
    assert not st["current"]
    assert st["session_net"] == 0 and st["recent_runs"] == [] and not st["eroding"]
    assert not st["failing"] and st["last_error"] == ""


def test_w3_state_missing_file():
    assert news_pulse.w3_state(None, NOW, SESSION, 1.5) == {"known": False}


def _bridge(tmp_path):
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "rss_scorer_state.json").write_text(json.dumps({
        "session_key": SESSION, "session_net": -1.7, "session_peak": -1.9,
        "recent_runs": [-0.6], "last_check": NOW.isoformat(timespec="seconds"),
        "error_streak": 0}), encoding="utf-8")
    (bridge / "regime_vote_w3.json").write_text(json.dumps({
        "version": 1, "direction": "trending-down", "source": "W3",
        "expires_after_session": SESSION}), encoding="utf-8")
    (bridge / "events.json").write_text(json.dumps({
        "version": 1, "updated_at": NOW.isoformat(), "events": [
            {"date": "2026-08-01", "name": "past", "severity": "high", "sessions": ["NIGHT"]},
            {"date": "2026-08-12", "name": "CPI", "severity": "high", "sessions": ["NIGHT"]},
            {"date": "2026-08-05", "name": "FOMC minutes", "severity": "medium",
             "sessions": ["NIGHT"]},
        ]}), encoding="utf-8")
    return bridge


def _paths(bridge, log):
    return {"regime_vote_path": str(bridge / "regime_vote.json"),
            "signal_path": str(bridge / "signal.json"),
            "rss_state_file": str(bridge / "rss_scorer_state.json"),
            "rss_news_log": log, "events_path": str(bridge / "events.json")}


def test_pulse_payload(tmp_path):
    bridge = _bridge(tmp_path)
    _, log = _items(tmp_path, [_row("a", -0.9, RUN1)])
    out = news_pulse.pulse(NOW, log, _paths(bridge, log), _reader)
    assert out["available"] and out["threshold"] == rss_scorer.SESSION_VOTE_THRESHOLD
    assert out["session"] == SESSION and out["weekend_pause"] is False
    assert out["w3"]["session_net"] == -1.7 and out["w3"]["direction"] == "trending-down"
    w3 = next(v for v in out["votes"] if v["source"] == "W3")
    assert w3["vote"] == "trending-down"
    assert {v["source"] for v in out["votes"]} == {"W2", "W3", "W4"}
    assert [e["name"] for e in out["events"]["items"]] == ["FOMC minutes", "CPI"]
    assert len(out["runs"]) == 1 and out["drivers"]["down"][0]["title"] == "a"
    assert str(bridge) not in json.dumps(out)              # paths never reach a client


def test_pulse_without_settings_or_log():
    out = news_pulse.pulse(NOW, None, None, _reader)
    assert out["available"] is False
    assert out["w3"] == {"known": False} and out["votes"] == [] and out["runs"] == []
    assert out["events"] == {"configured": False, "items": [], "error": ""}


def test_pulse_weekend_flag():
    sat = datetime(2026, 8, 8, 10, 0, tzinfo=TPE)
    assert news_pulse.pulse(sat, None, None, _reader)["weekend_pause"] is True


@pytest.fixture
def pulse_server(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    _, log = _items(tmp_path, [_row("a", -0.9, RUN1)])
    monkeypatch.setattr(server.DashboardHandler, "base_dir", str(tmp_path))
    monkeypatch.setattr(server.DashboardHandler, "cache", server._Cache(0))
    monkeypatch.setattr(server.DashboardHandler, "news_log_path", log)
    monkeypatch.setattr(server.DashboardHandler, "news_paths", _paths(bridge, log))
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_pulse_endpoint(pulse_server):
    with urllib.request.urlopen(pulse_server + "/api/news/pulse", timeout=10) as r:
        assert r.status == 200
        assert r.headers.get("Content-Type").startswith("application/json")
        body = json.loads(r.read())
    assert body["threshold"] == rss_scorer.SESSION_VOTE_THRESHOLD
    assert {"w3", "votes", "events", "runs", "drivers"} <= set(body)
