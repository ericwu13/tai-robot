"""Dashboard W2 ticker strip, W4 籌碼 panel and the 工作流程 view.

The bridges on the trading PC run an older pinned checkout, so every
reader here must tolerate files without the new fields (no sidecar, no
``daily``, no run log) and still say something true.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.dashboard import bridge_panels as bp
from scripts.dashboard import news_pulse, server, snapshot, workflows as wf
from src.news import bridge_runs

TPE = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 6, 22, 30, tzinfo=TPE)       # Fri night session


def reader(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run_lines(path):
    p = Path(path)
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def bridge(tmp_path):
    d = tmp_path / "n8n-bridge"
    d.mkdir()
    return d


def paths_for(d):
    return {"signal_path": str(d / "signal.json"),
            "regime_vote_path": str(d / "regime_vote.json"),
            "rss_state_file": str(d / "rss_scorer_state.json"),
            "events_path": str(d / "events.json")}


def iso(dt):
    return dt.isoformat(timespec="seconds")


def quote_row(tier, pct, *, fresh=True, down=-2.0, up=2.0, vote=(-2.0, 2.0), reason=None):
    return {"tier": tier, "price": 100 + pct, "prev_close": 100.0, "pct": pct,
            "quote_time": 1, "age_s": 30, "fresh": fresh, "stale_reason": reason,
            "down": down, "up": up, "vote_down": vote[0], "vote_up": vote[1]}


# ── W2 staleness window ─────────────────────────────────────────────────

@pytest.mark.parametrize("now,last,expect", [
    (datetime(2026, 11, 6, 22, 30, tzinfo=TPE), datetime(2026, 11, 6, 22, 26, tzinfo=TPE), (False, False)),
    (datetime(2026, 11, 6, 22, 30, tzinfo=TPE), datetime(2026, 11, 6, 22, 23, tzinfo=TPE), (True, False)),
    # 05:00-08:00 idle gap: an 04:58 pass is not stale at 07:30
    (datetime(2026, 11, 7, 7, 30, tzinfo=TPE), datetime(2026, 11, 7, 4, 58, tzinfo=TPE), (False, True)),
    # window just reopened at 08:00: 08:04 is still within the first 6 min
    (datetime(2026, 11, 7, 8, 4, tzinfo=TPE), datetime(2026, 11, 7, 4, 58, tzinfo=TPE), (False, False)),
    (datetime(2026, 11, 7, 8, 10, tzinfo=TPE), datetime(2026, 11, 7, 4, 58, tzinfo=TPE), (True, False)),
    # after midnight, still the window that opened yesterday 08:00
    (datetime(2026, 11, 7, 2, 0, tzinfo=TPE), datetime(2026, 11, 7, 1, 50, tzinfo=TPE), (True, False)),
])
def test_w2_stale_rule(now, last, expect):
    assert bp.w2_stale(now, last) == expect


# ── W2 strip ────────────────────────────────────────────────────────────

def test_strip_groups_tiers_and_flags_breaches(bridge):
    put(bridge / "crossmarket_quotes.json", {
        "version": 1, "asof": iso(NOW - timedelta(minutes=2)),
        "quorum": {"direction": "trending-down", "up": [], "down": ["SOXX", "QQQ"], "min_symbols": 2},
        "fired": ["signal-down"], "last_fired": {"at": iso(NOW), "what": ["signal-down"]},
        "fetch_failed": ["^HSI"],
        "quotes": {
            "SOXX": quote_row("signal", -3.0, down=-2.5, up=2.5, vote=(-2.5, 2.5)),
            "QQQ": quote_row("signal", -2.1),
            "^N225": quote_row("alert", 0.4, fresh=False, reason="STALE > 1500s allowance"),
            "^HSI": {"tier": "alert", "price": None, "pct": None, "fresh": False,
                     "stale_reason": "fetch_failed"},
        }})
    out = bp.crossmarket(NOW, paths_for(bridge), reader)
    assert out["known"] and not out["stale"] and out["age_s"] == 120
    signal, alert = out["groups"]
    assert (signal["label"], alert["label"]) == ("訊號層", "警示層")
    soxx = signal["rows"][0]
    assert soxx["symbol"] == "SOXX" and soxx["name"] == "費半 ETF"
    assert soxx["breach"] == "down" and soxx["vote"] == "down"
    assert signal["rows"][1]["breach"] == "down"          # -2.1 vs -2.0
    n225, hsi = alert["rows"]
    assert n225["fresh"] is False and n225["breach"] == "" and n225["stale_label"].startswith("報價過舊")
    assert hsi["stale_label"] == "抓取失敗"
    assert out["quorum"]["down"] == ["SOXX", "QQQ"]
    assert out["last_fired"]["labels"] == ["訊號層下跌（risk_off）"]
    assert out["fetch_failed"] == 1


def test_strip_old_checkout_falls_back_to_last_result(bridge):
    put(bridge / "monitor_state.json", {"last_check": iso(NOW - timedelta(minutes=1)),
                                        "last_result": "fresh 2/11 (SOXX -0.50%)"})
    out = bp.crossmarket(NOW, paths_for(bridge), reader)
    assert out["known"] is False and out["configured"] is True
    assert out["last_result"].startswith("fresh 2/11") and out["stale"] is False


def test_strip_tolerates_junk(bridge):
    put(bridge / "crossmarket_quotes.json", {"quotes": {"X": "junk", "Y": {"pct": "1"}},
                                              "quorum": [], "last_fired": "x"})
    out = bp.crossmarket(NOW, paths_for(bridge), reader)
    assert out["known"] and out["last_fired"] is None
    assert [r["symbol"] for g in out["groups"] for r in g["rows"]] == ["Y"]
    assert out["groups"][1]["rows"][0]["pct"] is None


def test_strip_unconfigured():
    assert bp.crossmarket(NOW, {}, reader) == {"configured": False, "known": False}


# ── W4 panel ────────────────────────────────────────────────────────────

def test_chips_panel_reads_daily_rows(bridge):
    put(bridge / ".chips_state.json", {
        "history": {"20261104": -80_000.0, "20261105": -81_000.0},
        "daily": {"20261105": {"net_oi": -81_000.0, "delta_oi": -1_000.0, "decision": None,
                               "reason": "weak ΔOI and no confirming equity flow — no vote",
                               "final": True, "session": "2026-11-05|NIGHT"},
                  "20261106": {"net_oi": -74_000.0, "delta_oi": 7_000.0, "prev_date": "20261105",
                               "equity_net": 6e9, "decision": "trending-up",
                               "reason": "strong: |ΔOI| >= 5,000", "final": True,
                               "session": "2026-11-06|NIGHT", "at": iso(NOW)}},
        "last_check": iso(NOW), "last_result": "nothing new"})
    out = bp.chips(NOW, paths_for(bridge), reader)
    assert out["has_daily"] is True
    assert out["latest"] == {"date": "2026-11-06", "net_oi": -74_000.0, "delta_oi": 7_000.0}
    first = out["days"][0]
    assert first["decision_label"] == "偏多投票"
    assert first["reason_zh"].startswith("ΔOI 達 5,000")
    assert out["days"][1]["decision_label"] == "不投票"
    assert [p["date"] for p in out["spark"]] == ["2026-11-04", "2026-11-05", "2026-11-06"]


def test_chips_panel_history_only_for_old_checkout(bridge):
    put(bridge / ".chips_state.json", {"history": {"20261104": -80_000.0, "20261105": -82_500.0},
                                       "last_check": iso(NOW), "last_result": "ΔOI -2,500 ..."})
    out = bp.chips(NOW, paths_for(bridge), reader)
    assert out["has_daily"] is False and out["days"] == []
    assert out["latest"] == {"date": "2026-11-05", "net_oi": -82_500.0, "delta_oi": -2_500.0}


def test_chips_panel_drops_delta_across_a_long_gap(bridge):
    put(bridge / ".chips_state.json", {"history": {"20261020": -80_000.0, "20261105": -82_500.0}})
    assert bp.chips(NOW, paths_for(bridge), reader)["latest"]["delta_oi"] is None


@pytest.mark.parametrize("reason,needle", [
    ("|ΔOI| 500 < 1,000 hysteresis band — too weak", "未達 1,000"),
    ("futures vs equity contradiction (likely institutional hedging) — no vote", "避險"),
    ("weak ΔOI but equity flow agrees", "同向確認"),
    ("no recent OI history for ΔOI (need previous trading day) — recording only", "僅記錄"),
    ("TWSE equity flow unavailable — cannot rule out hedging, no vote", "證交所"),
    ("something new", "something new"),
])
def test_reason_zh(reason, needle):
    assert needle in bp.reason_zh(reason)


# ── 工作流程 ────────────────────────────────────────────────────────────

def by_id(out):
    return {r["id"]: r for r in out["rows"]}


def test_workflows_from_old_checkout_state_files(bridge):
    put(bridge / "events.json", {"updated_at": iso(NOW - timedelta(hours=12)), "events": [{}, {}]})
    put(bridge / "monitor_state.json", {"last_check": iso(NOW - timedelta(minutes=2)),
                                        "last_result": "fresh 3/11"})
    put(bridge / "rss_scorer_state.json", {"last_check": iso(NOW - timedelta(minutes=10)),
                                           "error_streak": 0, "last_articles_scored": 4,
                                           "session_net": -0.8})
    put(bridge / ".chips_state.json", {"last_check": iso(NOW - timedelta(minutes=15)),
                                       "last_result": "nothing new"})
    out = wf.workflows(NOW, paths_for(bridge), reader, run_lines, host="PC")
    rows = by_id(out)
    assert [r["id"] for r in out["rows"]] == ["W1", "W2", "W3", "W4", "W5"]
    assert {k: rows[k]["status"] for k in rows} == {
        "W1": "ok", "W2": "ok", "W3": "ok", "W4": "ok", "W5": "off"}
    assert rows["W5"]["status_label"] == "未部署"
    assert rows["W1"]["result"] == "2 筆事件"
    assert "盤別累計 -0.80" in rows["W3"]["result"]
    assert all(r["source"] == "state" for r in out["rows"][:4])
    assert out["data_hosts"] == [] and out["foreign_host"] is False
    assert rows["W4"]["next_due"].startswith("2026-11-07T04:15")
    assert rows["W2"]["next_due"].startswith("2026-11-06T22:32")


def test_frozen_bridge_dir_is_all_stale(bridge):
    frozen = datetime(2026, 10, 24, 14, 0, tzinfo=TPE)
    put(bridge / "events.json", {"updated_at": iso(frozen)})
    put(bridge / "monitor_state.json", {"last_check": iso(frozen)})
    put(bridge / "rss_scorer_state.json", {"last_check": iso(frozen)})
    put(bridge / ".chips_state.json", {"last_check": iso(frozen)})
    out = wf.workflows(NOW, paths_for(bridge), reader, run_lines, host="PC")
    assert out["all_stale"] is True
    assert {r["status"] for r in out["rows"][:4]} == {"stale"}


def test_w3_failing_is_bad(bridge):
    put(bridge / "rss_scorer_state.json", {"last_check": iso(NOW), "error_streak": 3,
                                           "last_error": "HTTP 429"})
    row = by_id(wf.workflows(NOW, paths_for(bridge), reader, run_lines))["W3"]
    assert row["status"] == "bad" and "連續 3 輪" in row["error"]


def test_w4_missed_slot_is_stale(bridge):
    put(bridge / ".chips_state.json", {"last_check": iso(datetime(2026, 11, 6, 18, 15, tzinfo=TPE))})
    at = datetime(2026, 11, 6, 20, 40, tzinfo=TPE)          # 20:15 slot missed
    assert by_id(wf.workflows(at, paths_for(bridge), reader, run_lines))["W4"]["status"] == "stale"
    at = datetime(2026, 11, 6, 20, 25, tzinfo=TPE)          # still inside grace
    assert by_id(wf.workflows(at, paths_for(bridge), reader, run_lines))["W4"]["status"] == "ok"


def test_run_log_wins_and_reports_host(bridge):
    put(bridge / "monitor_state.json", {"last_check": iso(NOW - timedelta(hours=3))})
    bridge_runs.record_run(bridge, "W2", ok=True, result="fresh 4/11",
                           finished_at=NOW - timedelta(minutes=1), host="HOST-A")
    bridge_runs.record_run(bridge, "W4", ok=False, error="ValueError: x",
                           finished_at=NOW - timedelta(minutes=5), host="HOST-A")
    bridge_runs.record_run(bridge, "W5", ok=True, result="force-fire clear",
                           finished_at=NOW - timedelta(days=2), host="HOST-A")
    out = wf.workflows(NOW, paths_for(bridge), reader, run_lines, host="PC")
    rows = by_id(out)
    assert rows["W2"]["status"] == "ok" and rows["W2"]["source"] == "runs"
    assert rows["W2"]["host"] == "HOST-A" and rows["W2"]["runs_24h"] == 1
    assert rows["W4"]["status"] == "bad" and rows["W4"]["error"] == "ValueError: x"
    assert rows["W4"]["fails_24h"] == 1
    assert rows["W5"]["status"] == "ok"
    assert out["data_hosts"] == ["HOST-A"] and out["foreign_host"] is True


def test_same_host_is_not_foreign(bridge):
    bridge_runs.record_run(bridge, "W2", ok=True, finished_at=NOW, host=socket.gethostname())
    out = wf.workflows(NOW, paths_for(bridge), reader, run_lines, host=socket.gethostname())
    assert out["foreign_host"] is False


def test_events_path_missing_reads_bridge_default(bridge):
    put(bridge / "events.json", {"updated_at": iso(NOW)})
    p = paths_for(bridge)
    p["events_path"] = ""
    row = by_id(wf.workflows(NOW, p, reader, run_lines))["W1"]
    assert row["status"] == "ok" and "events_path" in row["note"]


def test_no_files_is_unknown_not_dead(bridge):
    out = wf.workflows(NOW, paths_for(bridge), reader, run_lines)
    assert {r["status"] for r in out["rows"][:4]} == {"unknown"}
    assert out["all_stale"] is False


def test_no_news_settings():
    out = wf.workflows(NOW, None, reader, run_lines)
    assert out["available"] is False and out["rows"] == []


def test_snapshot_run_reader_is_stat_gated(bridge, monkeypatch):
    bridge_runs.record_run(bridge, "W2", ok=True, finished_at=NOW)
    path = str(bridge_runs.runs_path(bridge, "W2"))
    calls = []
    real = snapshot.fileio.tail_lines
    monkeypatch.setattr(snapshot.fileio, "tail_lines",
                        lambda p, n, **k: calls.append(p) or real(p, n, **k))
    assert len(snapshot._read_run_lines(path)) == 1
    assert len(snapshot._read_run_lines(path)) == 1
    assert len(calls) == 1, "unchanged file must not be reopened"
    bridge_runs.record_run(bridge, "W2", ok=True, finished_at=NOW)
    assert len(snapshot._read_run_lines(path)) == 2


# ── payload + HTTP ──────────────────────────────────────────────────────

def test_pulse_carries_w2_and_w4(bridge):
    out = news_pulse.pulse(NOW, None, paths_for(bridge), reader)
    assert out["w2"]["configured"] is True and out["w4"]["configured"] is True


@pytest.fixture
def wf_server(bridge, monkeypatch):
    put(bridge / "monitor_state.json", {"last_check": iso(NOW)})
    monkeypatch.setattr(server.DashboardHandler, "base_dir", str(bridge))
    monkeypatch.setattr(server.DashboardHandler, "cache", server._Cache(0))
    monkeypatch.setattr(server.DashboardHandler, "news_paths", paths_for(bridge))
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_workflows_endpoint(wf_server):
    with urllib.request.urlopen(wf_server + "/api/workflows", timeout=10) as r:
        assert r.status == 200
        data = json.load(r)
    assert data["available"] is True
    assert [row["id"] for row in data["rows"]] == ["W1", "W2", "W3", "W4", "W5"]
    assert data["host"] == socket.gethostname()
    assert "n8n-bridge" not in json.dumps(data), "paths never reach a client"


def test_local_paths_never_reach_the_client(bridge):
    bridge_runs.record_run(bridge, "W4", ok=False, finished_at=NOW,
                           error=r"FileNotFoundError: [Errno 2] No such file: 'C:\Users\someuser\n8n-bridge\x.json'")
    bridge_runs.record_run(bridge, "W1", ok=False, finished_at=NOW,
                           error="OSError: /home/bot/n8n-bridge/events.json busy")
    rows = by_id(wf.workflows(NOW, paths_for(bridge), reader, run_lines))
    assert "someuser" not in rows["W4"]["error"] and "x.json" in rows["W4"]["error"]
    assert "/home/bot" not in rows["W1"]["error"] and "events.json" in rows["W1"]["error"]
    assert wf.scrub_paths("HTTP 429 quota") == "HTTP 429 quota"
