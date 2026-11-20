"""籌碼走勢 vs 台指 (GET /api/chips/trend): W4's daily foreign flow
history plus day-session index-futures candles on one daily axis."""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from scripts.dashboard import chips_trend, server, snapshot
from tests.test_chips_monitor import chips as chips_monitor
from tests.test_dashboard_news_trend import _bot


def _ct(y, mo, d) -> int:
    return int(datetime(y, mo, d, tzinfo=timezone.utc).timestamp())


def _row(date, net_oi, delta, equity=None, *, decision=None, reason="", final=True, src="live"):
    return {"v": 1, "date": date, "src": src, "net_oi": net_oi, "delta_oi": delta,
            "prev_date": None, "equity_net": equity, "decision": decision,
            "reason": reason, "final": final, "session": f"{date[:4]}-{date[4:6]}-{date[6:]}|NIGHT",
            "at": "x"}


def _bridge(tmp_path, lines=(), daily=None):
    d = tmp_path / "bridge"
    d.mkdir(exist_ok=True)
    if lines:
        (d / chips_trend.HISTORY_NAME).write_text(
            "".join(json.dumps(x) + "\n" for x in lines) + "not json\n", encoding="utf-8")
    if daily is not None:
        (d / ".chips_state.json").write_text(json.dumps({"daily": daily}), encoding="utf-8")
    return {"regime_vote_path": str(d / "regime_vote.json")}


def test_thresholds_mirror_chips_monitor():
    assert chips_trend.HISTORY_NAME == chips_monitor.HISTORY_NAME
    assert chips_trend.HYSTERESIS_CONTRACTS == chips_monitor.HYSTERESIS_CONTRACTS
    assert chips_trend.STRONG_CONTRACTS == chips_monitor.STRONG_CONTRACTS
    assert chips_trend.EQUITY_FLOW_THRESHOLD_NTD == chips_monitor.EQUITY_FLOW_THRESHOLD_NTD
    assert chips_trend.MAX_PREV_GAP_DAYS == chips_monitor.MAX_PREV_GAP_DAYS


def test_merge_live_beats_state_beats_backfill_and_later_line_wins():
    lines = [_row("20261110", 1, 10, src="backfill"),
             _row("20261111", 2, 20, final=False), _row("20261111", 2, 21),
             _row("20261112", 3, 30, src="backfill")]
    state = {"20261110": {"net_oi": 9, "delta_oi": 90}, "20261112": {"net_oi": 8, "delta_oi": 80}}
    rows = chips_trend.merge_rows(lines, state)
    assert rows["20261110"]["delta_oi"] == 90 and rows["20261110"]["src"] == "state"
    assert rows["20261111"]["delta_oi"] == 21 and rows["20261111"]["final"] is True
    assert rows["20261112"]["delta_oi"] == 80


def test_rolling_flow_restarts_on_missing_delta_and_long_gaps():
    rows = {d: _row(d, 0, delta, 1e9) for d, delta in [
        ("20261102", 1), ("20261103", 2), ("20261104", 3), ("20261105", 4),
        ("20261106", 5), ("20261109", 6)]}           # weekend gap (3 days) keeps the run
    rows["20261110"] = _row("20261110", 0, None)     # baseline row: restarts
    rows["20261111"] = _row("20261111", 0, 7)
    rows["20261120"] = _row("20261120", 0, 8)        # 9-day gap > MAX_PREV_GAP_DAYS
    days = {d["date"]: d for d in chips_trend.build_days(rows)}
    assert days["2026-11-06"]["flow"] == 15 and days["2026-11-06"]["flow_full"]
    assert days["2026-11-06"]["flow_avg"] == 3
    assert days["2026-11-09"]["flow"] == 2 + 3 + 4 + 5 + 6
    assert days["2026-11-09"]["equity_flow"] == pytest.approx(5e9)
    assert days["2026-11-09"]["equity_avg"] == pytest.approx(1e9)
    assert days["2026-11-10"]["flow"] is None and days["2026-11-10"]["flow_avg"] is None
    assert days["2026-11-10"]["verdict"] == ""
    assert days["2026-11-11"]["flow"] == 7 and days["2026-11-11"]["flow_days"] == 1
    assert days["2026-11-11"]["flow_avg"] == 7 and days["2026-11-11"]["equity_avg"] is None
    assert days["2026-11-20"]["flow"] == 8
    assert days["2026-11-02"]["time"] == _ct(2026, 11, 2)


def test_kinds_settlement_and_labels():
    rows = {
        "20261116": _row("20261116", -96787, 4083, -7e10,
                         reason="futures vs equity contradiction (likely institutional hedging) — no vote"),
        "20261117": _row("20261117", -90000, 6787, 6e9, decision="trending-up",
                         reason="strong: |ΔOI| >= 5,000"),
        "20261118": _row("20261118", -91000, -1000, None, final=False,
                         reason="TWSE equity flow unavailable"),
    }
    days = {d["date"]: d for d in chips_trend.build_days(rows)}
    assert days["2026-11-16"]["kind"] == "hedge"
    assert days["2026-11-16"]["reason_zh"].startswith("期貨與現貨方向相反")
    assert days["2026-11-17"]["kind"] == "vote" and days["2026-11-17"]["decision_label"] == "偏多投票"
    assert days["2026-11-18"]["kind"] == "pending"
    # TAIFEX settlement: 3rd Wednesday, 2026-11-18.
    assert days["2026-11-18"]["settlement"] and not days["2026-11-17"]["settlement"]


@pytest.mark.parametrize("flow_avg, equity_avg, want", [
    (1500, 6e9, "agree_in"), (-1500, -6e9, "agree_out"),
    (1500, -6e9, "diverge"), (-1000, 5.1e9, "diverge"),
    (1000, 5e9, "fut_in"), (-1500, 0, "fut_out"),          # equity must EXCEED 50 億 (decide)
    (999, 6e9, "eq_in"), (0, -6e9, "eq_out"),               # |ΔOI| 999 < 1,000: too weak
    (999, 5e9, "quiet"), (None, 6e9, ""), (1500, None, ""),
])
def test_flow_verdict_applies_w4_daily_thresholds_to_the_average(flow_avg, equity_avg, want):
    assert chips_trend.flow_verdict(flow_avg, equity_avg) == want


def test_verdict_boundaries_match_chips_monitor_decide():
    """On a window whose every day equals its average, "diverge" is exactly
    decide()'s contradiction and agree_* exactly a vote the cash side
    confirmed, at W4's own boundaries (|ΔOI| >= 1,000, |equity| > 50 億)."""
    for delta, equity in [(999, 6e9), (1000, 6e9), (1000, 5e9), (1000, -5e9), (1000, -5.1e9),
                          (-4999, -6e9), (-1000, 5.1e9), (6000, 0), (-6000, 5e9)]:
        direction, reason = chips_monitor.decide(delta, equity)
        verdict = chips_trend.flow_verdict(delta, equity)
        assert (verdict == "diverge") == ("contradiction" in reason), (delta, equity)
        confirmed = direction is not None and abs(equity) > chips_monitor.EQUITY_FLOW_THRESHOLD_NTD
        assert verdict.startswith("agree_") == confirmed, (delta, equity)


def test_page_has_words_for_every_verdict():
    html = open(os.path.join(os.path.dirname(server.__file__), "static", "index.html"),
                encoding="utf-8").read()
    table = html[html.index("const CHIPS_VERDICT = {"):]
    table = table[:table.index("};")]
    for key in ("agree_in", "agree_out", "diverge", "fut_in", "fut_out", "eq_in", "eq_out", "quiet"):
        assert f"{key}: [" in table, key


def test_price_is_one_day_session_candle_per_date(tmp_path):
    base = tmp_path / "live"
    _bot(base, "TX00_a", {"20261117": [
        ("2026/11/17 08:45", 100, 105, 99, 104),
        ("2026/11/17 13:44", 104, 120, 90, 110),
        ("2026/11/17 15:00", 110, 200, 50, 150)]})          # night session: excluded
    files = chips_trend.news_trend.symbol_files(str(base))
    price = chips_trend.build_price(files["TX00"], "20261101")
    assert price == [{"time": _ct(2026, 11, 17), "open": 100, "high": 120, "low": 90, "close": 110}]


def test_trend_payload_and_summary(tmp_path):
    lines = [_row(f"202611{d:02d}", -90000, delta, eq) for d, delta, eq in
             [(2, 2000, 6e9), (3, 1000, 1e9), (4, 3000, 2e9), (5, -500, 1e9), (6, 1500, 1e9)]]
    paths = _bridge(tmp_path, lines, daily={})
    _bot(tmp_path / "live", "TX00_a", {"20261106": [("2026/11/06 09:00", 1, 2, 0, 1)]})
    body = chips_trend.trend(paths, snapshot._read_json_dict, str(tmp_path / "live"))
    assert body["available"] and len(body["days"]) == 5 and body["symbol"] == "TX00"
    assert body["summary"] == {"date": "2026-11-06", "flow": 7000, "equity_flow": pytest.approx(11e9),
                               "flow_days": 5, "flow_avg": 1400, "equity_avg": pytest.approx(2.2e9),
                               "agree": True, "verdict": "fut_in"}
    assert body["sources"] == {"live": 5} and len(body["price"]) == 1
    assert body["thresholds"]["strong"] == 5000


def test_state_daily_alone_still_draws(tmp_path):
    paths = _bridge(tmp_path, daily={"20261116": {"net_oi": -96787, "delta_oi": 4083,
                                                  "equity_net": -7e10, "final": True}})
    body = chips_trend.trend(paths, snapshot._read_json_dict, "")
    assert [d["date"] for d in body["days"]] == ["2026-11-16"]
    assert body["sources"] == {"state": 1} and body["summary"]["agree"] is False


def test_not_configured_and_empty(tmp_path):
    assert chips_trend.trend({}, snapshot._read_json_dict, "")["available"] is False
    body = chips_trend.trend(_bridge(tmp_path), snapshot._read_json_dict, "")
    assert body["available"] and body["reason"] == "empty" and body["summary"] is None


def test_history_is_reparsed_only_when_it_changes(tmp_path, monkeypatch):
    paths = _bridge(tmp_path, [_row("20261102", 1, 1)])
    path = os.path.join(os.path.dirname(paths["regime_vote_path"]), chips_trend.HISTORY_NAME)
    calls = []
    real = chips_trend._parse_lines
    monkeypatch.setattr(chips_trend, "_parse_lines", lambda t: calls.append(1) or real(t))
    chips_trend.read_history(path)
    chips_trend.read_history(path)
    assert len(calls) == 1
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_row("20261103", 2, 2)) + "\n")
    assert len(chips_trend.read_history(path)) == 2 and len(calls) == 2


@pytest.fixture
def chips_server(tmp_path, monkeypatch):
    paths = _bridge(tmp_path, [_row("20261117", -90000, 6787, 6e9, decision="trending-up")])
    _bot(tmp_path / "live", "TX00_a", {"20261117": [("2026/11/17 09:00", 1, 2, 0, 1)]})
    monkeypatch.setattr(server.DashboardHandler, "base_dir", str(tmp_path / "live"))
    monkeypatch.setattr(server.DashboardHandler, "cache", server._Cache(0))
    monkeypatch.setattr(server.DashboardHandler, "news_paths", paths)
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "bridge"
    srv.shutdown()
    srv.server_close()


def test_endpoint_is_read_only_and_leaks_no_paths(chips_server):
    url, bridge = chips_server
    before = sorted(os.listdir(bridge))
    with urllib.request.urlopen(url + "/api/chips/trend?symbol=TX00", timeout=10) as r:
        assert r.status == 200
        body = json.loads(r.read())
    assert body["days"][0]["kind"] == "vote" and body["price"]
    assert str(bridge) not in json.dumps(body)
    assert sorted(os.listdir(bridge)) == before


@pytest.mark.parametrize("query", ["symbol=../x", "symbol=" + "A" * 17])
def test_endpoint_rejects_bad_symbol(chips_server, query):
    url, _ = chips_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(url + "/api/chips/trend?" + query, timeout=10)
    assert exc.value.code == 400
