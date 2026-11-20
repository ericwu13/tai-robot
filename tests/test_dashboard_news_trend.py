"""情緒走勢 vs 價格 (GET /api/news/trend): the whole W3 run history and
30-min index-futures candles on one time axis."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from scripts.dashboard import news_feed, news_trend, server


def _ct(y, mo, d, h, mi) -> int:
    """Chart time: TPE wall clock encoded as UTC."""
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp())


def _row(scored_at, score, *, session="2026-08-04|NIGHT", run_net=0.0, session_net=0.0):
    direction = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    return {"v": 1, "scored_at": scored_at, "published_at": scored_at, "session": session,
            "source": "Wire", "title": f"t{scored_at}{score}", "link": "https://news.example/x",
            "summary": "s", "direction": direction, "confidence": abs(score), "score": score,
            "reason": "r", "run_net": run_net, "session_net": session_net}


def _log(tmp_path, rows):
    p = tmp_path / "news" / "news_log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(p)


def _bot(base, name, files):
    d = base / name
    d.mkdir(parents=True)
    (d / "session.json").write_text("{}", encoding="utf-8")
    for day, rows in files.items():
        (d / f"bars_1m_{day}.csv").write_text(
            "datetime,open,high,low,close,volume\n"
            + "".join(f"{t},{o},{h},{lo},{c},1\n" for t, o, h, lo, c in rows), encoding="utf-8")
    return d


def test_runs_cover_the_whole_log_not_a_window(tmp_path):
    """The chart is no longer capped at 3 days or the table's newest lines."""
    start = datetime(2026, 6, 1, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    rows = []
    for i in range(1200):                        # 1200 runs x 5 articles > WINDOW_LINES
        at = (start + timedelta(minutes=30 * i)).isoformat()
        rows += [_row(at, 0.4, run_net=0.4, session_net=float(i))] * 5
    assert len(rows) > news_feed.WINDOW_LINES
    runs = news_trend.read_runs(_log(tmp_path, rows))
    assert len(runs) == 1200
    assert runs[0]["t"] == _ct(2026, 6, 1, 20, 0) and runs[0]["n"] == 5
    assert runs[-1]["session_net"] == 1199.0
    assert [r["t"] for r in runs] == sorted(r["t"] for r in runs)


def test_runs_group_by_pass_and_keep_one_point_per_time(tmp_path):
    log = _log(tmp_path, [
        _row("2026-08-04T20:00:00+08:00", 0.9, run_net=0.8, session_net=0.8),
        _row("2026-08-04T20:00:00+08:00", -0.3, run_net=0.8, session_net=0.8),
        _row("2026-08-04T20:30:00+08:00", 0.0, run_net=0.0, session_net=0.673),
        "not json",
    ])
    runs = news_trend.read_runs(log)
    assert [r["n"] for r in runs] == [2, 1]
    assert runs[0]["bullish"] == 1 and runs[0]["bearish"] == 1
    assert runs[1]["session_net"] == 0.673


def test_candles_are_session_aligned_and_merged_across_bots(tmp_path):
    # Night opens 15:00 -> buckets 15:00, 15:30. Day opens 08:45 -> 08:45, 09:15.
    _bot(tmp_path, "TX00_a", {"20260804": [
        ("2026/08/04 15:00", 100, 105, 99, 104),
        ("2026/08/04 15:29", 104, 110, 103, 108),
        ("2026/08/04 15:30", 108, 109, 107, 107)]})
    # Same symbol, another bot: starts later, has a minute bot a missed.
    _bot(tmp_path, "TX00_b", {"20260804": [
        ("2026/08/04 15:10", 102, 120, 101, 103),
        ("2026/08/04 15:31", 107, 107, 90, 95)],
        "20260805": [("2026/08/05 08:45", 200, 201, 199, 200),
                     ("2026/08/05 09:14", 200, 202, 198, 201),
                     ("2026/08/05 09:15", 201, 203, 200, 202)]})
    files = news_trend.symbol_files(str(tmp_path))
    candles = news_trend.build_candles(files["TX00"])
    assert candles == [
        {"time": _ct(2026, 8, 4, 15, 0), "open": 100, "high": 120, "low": 99, "close": 108},
        {"time": _ct(2026, 8, 4, 15, 30), "open": 108, "high": 109, "low": 90, "close": 95},
        {"time": _ct(2026, 8, 5, 8, 45), "open": 200, "high": 202, "low": 198, "close": 201},
        {"time": _ct(2026, 8, 5, 9, 15), "open": 201, "high": 203, "low": 200, "close": 202},
    ]


def test_pick_symbol():
    files = {"TMF00": [("20260801", "a"), ("20260802", "b"), ("20260803", "c")],
             "TX00": [("20260801", "d"), ("20260802", "e")],
             "MTX00": [("20260801", "f"), ("20260802", "g")],
             "2330": [(f"202607{d:02d}", "x") for d in range(1, 30)]}
    assert news_trend.pick_symbol(files) == "TMF00"            # most days of index futures
    del files["TMF00"]
    assert news_trend.pick_symbol(files) == "TX00"             # tie -> TX before MTX
    assert news_trend.pick_symbol(files, "MTX00") == "MTX00"
    assert news_trend.pick_symbol(files, "NOPE") == "TX00"
    assert news_trend.pick_symbol({}) == ""


def test_trend_reads_only_bar_days_the_runs_cover_and_tails_with_after(tmp_path):
    log = _log(tmp_path, [_row("2026-08-04T20:00:00+08:00", 0.5, run_net=0.5, session_net=0.5),
                          _row("2026-08-05T09:00:00+08:00", -0.5, run_net=-0.5, session_net=-0.5)])
    _bot(tmp_path / "live", "TX00_a", {
        "20260701": [("2026/07/01 09:00", 1, 1, 1, 1)],          # long before the first run
        "20260804": [("2026/08/04 20:01", 100, 101, 99, 100)],
        "20260805": [("2026/08/05 08:50", 110, 111, 109, 110),
                     ("2026/08/05 09:20", 111, 112, 110, 111)]})
    full = news_trend.trend(log, str(tmp_path / "live"))
    assert full["symbol"] == "TX00" and full["symbols"] == ["TX00"]
    assert [c["time"] for c in full["candles"]] == [
        _ct(2026, 8, 4, 20, 0), _ct(2026, 8, 5, 8, 45), _ct(2026, 8, 5, 9, 15)]
    assert [r["t"] for r in full["runs"]] == [_ct(2026, 8, 4, 20, 0), _ct(2026, 8, 5, 9, 0)]

    tail = news_trend.trend(log, str(tmp_path / "live"), after=_ct(2026, 8, 5, 9, 0))
    assert [r["t"] for r in tail["runs"]] == [_ct(2026, 8, 5, 9, 0)]
    assert [c["time"] for c in tail["candles"]] == [_ct(2026, 8, 5, 9, 15)]
    # A tail past the newest candle still re-sends it (it may be forming).
    late = news_trend.trend(log, str(tmp_path / "live"), after=_ct(2026, 8, 6, 0, 0))
    assert late["runs"] == [] and [c["time"] for c in late["candles"]] == [_ct(2026, 8, 5, 9, 15)]


def test_trend_without_log_or_bots(tmp_path):
    assert news_trend.trend(None, str(tmp_path)) == {
        "symbol": "", "symbols": [], "interval": 1800, "runs": [], "candles": []}


@pytest.fixture
def trend_server(tmp_path, monkeypatch):
    log = _log(tmp_path, [_row("2026-08-04T20:00:00+08:00", 0.5)])
    _bot(tmp_path / "live", "TX00_a", {"20260804": [("2026/08/04 20:01", 100, 101, 99, 100)]})
    monkeypatch.setattr(server.DashboardHandler, "base_dir", str(tmp_path / "live"))
    monkeypatch.setattr(server.DashboardHandler, "cache", server._Cache(0))
    monkeypatch.setattr(server.DashboardHandler, "news_log_path", log)
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_trend_endpoint(trend_server):
    with urllib.request.urlopen(trend_server + "/api/news/trend?symbol=TX00", timeout=10) as r:
        assert r.status == 200
        body = json.loads(r.read())
    assert body["symbol"] == "TX00" and len(body["runs"]) == 1 and len(body["candles"]) == 1
    assert str(trend_server) not in json.dumps(body)


@pytest.mark.parametrize("query", ["symbol=../x", "symbol=" + "A" * 17, "after=-1", "after=1e9"])
def test_trend_endpoint_rejects_bad_queries(trend_server, query):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(trend_server + "/api/news/trend?" + query, timeout=10)
    assert exc.value.code == 400


# ── monthly history files ────────────────────────────────────────────────

def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_runs_and_table_span_the_legacy_file_and_monthly_files(tmp_path):
    base = tmp_path / "news" / "news_log.jsonl"
    _write(base, [_row("2026-10-30T20:00:00+08:00", 0.5)])                      # pre-split
    _write(tmp_path / "news" / "news_log_202611.jsonl", [_row("2026-11-02T20:00:00+08:00", -0.5)])
    runs = news_trend.read_runs(str(base))
    assert [r["t"] for r in runs] == [_ct(2026, 10, 30, 20, 0), _ct(2026, 11, 2, 20, 0)]
    titles = [r["title"] for r in news_feed.read_items(str(base))]
    assert len(titles) == 2 and titles[0].startswith("t2026-11-02")             # newest first
    # A new month appears later: both readers pick it up.
    _write(tmp_path / "news" / "news_log_202612.jsonl", [_row("2026-12-01T09:00:00+08:00", 0.2)])
    assert len(news_trend.read_runs(str(base))) == 3
    assert len(news_feed.read_items(str(base))) == 3


def test_table_window_fills_from_older_months_only_as_needed(tmp_path, monkeypatch):
    monkeypatch.setattr(news_feed, "WINDOW_LINES", 5)
    d = tmp_path / "news"
    _write(d / "news_log_202610.jsonl", [_row(f"2026-10-01T{h:02d}:00:00+08:00", 0.1) for h in range(10, 14)])
    _write(d / "news_log_202611.jsonl", [_row(f"2026-11-01T{h:02d}:00:00+08:00", 0.1) for h in range(10, 13)])
    items = news_feed.read_items(str(d / "news_log.jsonl"))
    assert len(items) == 5                                      # 3 from Nov + newest 2 from Oct
    assert sorted(r["scored_at"][:13] for r in items)[:2] == ["2026-10-01T12", "2026-10-01T13"]
