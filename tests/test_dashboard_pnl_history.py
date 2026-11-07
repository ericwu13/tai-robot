"""損益走勢: cumulative P&L per trading session, sim vs real-only.

A trade belongs to the trading day it exited in (night open 15:00 TPE, on
the TAIFEX calendar). The real-only line counts ``classify() == "real"``
trades only; estimated / unreliable / missing ones are reported beside it.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from scripts.dashboard import pnl_history, server, snapshot
from scripts.monitor.common import TZ_TPE

PV = 10                                         # TMF point value
NOW = datetime(2026, 11, 6, 10, 0, tzinfo=TZ_TPE)   # Fri day session → day opened Thu 11-05 15:00


def _trade(exit_dt, pnl, *, real=None, side="LONG", entry=45000, exit_=None,
           real_entry=0, real_exit=0, source="paper"):
    """``real``: None = paper, "real" / "estimated" / "unreliable" / "missing"."""
    exit_ = exit_ if exit_ is not None else entry + (pnl // PV if side == "LONG" else -(pnl // PV))
    t = {"side": side, "entry_dt": exit_dt, "entry_price": entry, "exit_dt": exit_dt,
         "exit_price": exit_, "pnl": pnl, "qty": 1, "source": source}
    if real == "real":
        t.update(source="real", real_entry_price=real_entry or entry,
                 real_exit_price=real_exit or exit_)
    elif real == "estimated":
        t.update(source="real", real_entry_price=real_entry or entry, real_exit_price=0)
    elif real == "unreliable":
        t.update(source="real", real_entry_price=entry, real_exit_price=exit_ + 5000)
    elif real == "missing":
        t.update(source="real")
    return t


def _bot(base, name, trades, *, retired=False):
    d = base / name
    d.mkdir(parents=True)
    (d / "session.json").write_text(json.dumps({
        "strategy": "Demo", "symbol": name.split("_")[0], "bot_name": name.split("_", 1)[1],
        "point_value": PV, "target_interval": 900, "trading_mode": "auto",
        "broker": {"trades": trades, "position_size": 0}}), encoding="utf-8")
    if retired:
        old = time.time() - 30 * 86400
        for f in d.iterdir():
            os.utime(f, (old, old))
    return d


def _never_alive(pid):
    return False


# ── session assignment ─────────────────────────────────────────────────

@pytest.mark.parametrize("exit_dt, session", [
    ("2026-11-04 20:30", "2026-11-04"),     # Wed night
    ("2026-11-05 02:10", "2026-11-04"),     # after midnight: same trading day
    ("2026-11-05 10:00", "2026-11-04"),     # Thu day session closes Wed's trading day
    ("2026-11-05 15:05", "2026-11-05"),     # Thu night opens the next one
    ("2026-11-07 03:00", "2026-11-06"),     # Fri night into Sat
    ("2026-11-09 09:00", "2026-11-06"),     # Mon day session: no weekend nights
    ("2026-10-27 10:00", "2026-10-23"),     # Tue after the 10-26 holiday (光復節 observed)
])
def test_trade_is_assigned_to_the_trading_day_it_exited_in(exit_dt, session):
    assert pnl_history._SessionOf()(exit_dt) == session


# ── per-bot cumulative sums ────────────────────────────────────────────

def test_bot_history_cumulates_sim_and_real_only(tmp_path):
    d = _bot(tmp_path, "TMF00_alpha", [
        _trade("2026-11-04 21:00", 1000, real="real"),                  # real +1000
        _trade("2026-11-05 09:30", -300),                               # paper only
        _trade("2026-11-05 16:00", 500, real="real", real_exit=45040),  # real +400 (slippage)
        _trade("2026-11-05 20:00", 200, real="estimated"),              # NOT on the real line
        _trade("2026-11-06 09:00", -700, real="unreliable"),            # NOT on the real line
        _trade("2026-11-06 09:30", 100, real="missing"),                # NOT on the real line
    ])
    pts = pnl_history.bot_history(NOW, str(d))
    assert [p["session"] for p in pts] == ["2026-11-04", "2026-11-05"]
    first, second = pts
    assert (first["sim_pnl"], first["sim_cumulative"]) == (700, 700)
    assert (first["real_pnl"], first["real_cumulative"]) == (1000, 1000)
    assert (second["sim_pnl"], second["sim_cumulative"]) == (100, 800)
    # real-only: the slipped real trade counts at its REAL price (+400);
    # estimated / unreliable / missing do not move the line.
    assert (second["real_pnl"], second["real_cumulative"]) == (400, 1400)
    assert second["real_est_cumulative"] == 1000 + 400 + 200
    assert (second["real"], second["estimated"], second["unreliable"], second["missing"]) == (1, 1, 1, 1)
    assert second["trades"] == 4
    assert [p["is_current"] for p in pts] == [False, True]


def test_real_only_line_excludes_estimated_and_unreliable(tmp_path):
    d = _bot(tmp_path, "TMF00_beta", [
        _trade("2026-11-05 16:00", 900, real="estimated"),
        _trade("2026-11-05 17:00", -400, real="unreliable"),
    ])
    [p] = pnl_history.bot_history(NOW, str(d))
    assert p["sim_cumulative"] == 500
    assert p["real_cumulative"] == 0 and p["real"] == 0
    assert p["real_est_cumulative"] == 900           # estimated only, never unreliable


def test_paper_bot_has_no_real_line(tmp_path):
    d = _bot(tmp_path, "TMF00_paper", [_trade("2026-11-05 16:00", 300)])
    [p] = pnl_history.bot_history(NOW, str(d))
    assert (p["sim_cumulative"], p["real_cumulative"], p["real"]) == (300, 0, 0)


def test_undated_trades_are_skipped_and_empty_bot_is_empty(tmp_path):
    d = _bot(tmp_path, "TMF00_gamma", [_trade("garbage", 999)])
    assert pnl_history.bot_history(NOW, str(d)) == []


# ── fleet ──────────────────────────────────────────────────────────────

def test_fleet_sums_bots_per_session_and_skips_retired(tmp_path):
    _bot(tmp_path, "TMF00_a", [_trade("2026-11-04 21:00", 100, real="real"),
                              _trade("2026-11-05 16:00", 50)])
    _bot(tmp_path, "TXF00_b", [_trade("2026-11-05 17:00", 1000, real="real")])
    _bot(tmp_path, "TMF00_old", [_trade("2026-11-05 18:00", -9999)], retired=True)

    pts = pnl_history.fleet_history(NOW, str(tmp_path), pid_alive_fn=_never_alive)
    assert [(p["session"], p["sim_cumulative"], p["real_cumulative"]) for p in pts] == [
        ("2026-11-04", 100, 100), ("2026-11-05", 1150, 1100)]

    all_ = pnl_history.fleet_history(NOW, str(tmp_path), include_retired=True,
                                     pid_alive_fn=_never_alive)
    assert all_[-1]["sim_cumulative"] == 1150 - 9999


def test_fleet_survives_a_broken_bot(tmp_path):
    _bot(tmp_path, "TMF00_a", [_trade("2026-11-05 16:00", 10)])
    bad = tmp_path / "TMF00_bad"
    bad.mkdir()
    (bad / "session.json").write_text("{not json", encoding="utf-8")
    pts = pnl_history.fleet_history(NOW, str(tmp_path), pid_alive_fn=_never_alive)
    assert [p["sim_cumulative"] for p in pts] == [10]


def test_bot_detail_trades_carry_their_session(tmp_path):
    d = _bot(tmp_path, "TMF00_alpha", [_trade("2026-11-05 02:10", 10)])
    detail = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_never_alive)
    assert detail["trades"][0]["session"] == "2026-11-04"


# ── endpoints ──────────────────────────────────────────────────────────

@pytest.fixture
def pnl_server(tmp_path, monkeypatch):
    _bot(tmp_path, "TMF00_alpha", [_trade("2026-11-04 21:00", 1000, real="real"),
                                  _trade("2026-11-05 16:00", 200, real="estimated")])
    _bot(tmp_path, "TXF00_beta", [_trade("2026-11-05 17:00", -300)])
    _bot(tmp_path, "TMF00_old", [_trade("2026-11-05 18:00", 5000)], retired=True)
    monkeypatch.setattr(server.DashboardHandler, "base_dir", str(tmp_path))
    monkeypatch.setattr(server.DashboardHandler, "cache", server._Cache(0))
    monkeypatch.setattr(server, "now_tpe", lambda: NOW)
    monkeypatch.setattr(snapshot, "pid_alive", _never_alive)
    monkeypatch.setattr(pnl_history.snapshot, "pid_alive", _never_alive)
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        assert r.headers.get("Content-Type").startswith("application/json")
        return json.loads(r.read())


def test_bot_endpoint(pnl_server):
    pts = _get(pnl_server + "/pnl-history/TMF00_alpha")
    assert [(p["session"], p["sim_cumulative"], p["real_cumulative"], p["real_est_cumulative"])
            for p in pts] == [("2026-11-04", 1000, 1000, 1000), ("2026-11-05", 1200, 1000, 1200)]
    assert set(pts[0]) >= {"session", "sim_cumulative", "real_cumulative"}


def test_fleet_endpoint_and_retired_toggle(pnl_server):
    pts = _get(pnl_server + "/pnl-history/fleet")
    assert [(p["session"], p["sim_cumulative"]) for p in pts] == [
        ("2026-11-04", 1000), ("2026-11-05", 900)]
    pts = _get(pnl_server + "/pnl-history/fleet?retired=1")
    assert pts[-1]["sim_cumulative"] == 900 + 5000


@pytest.mark.parametrize("path", ["/pnl-history/nope", "/pnl-history/", "/pnl-history/..%2Fx"])
def test_unknown_bot_is_404(pnl_server, path):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(pnl_server + path, timeout=10)
    assert exc.value.code == 404


def test_page_has_both_charts(pnl_server):
    with urllib.request.urlopen(pnl_server + "/", timeout=10) as r:
        page = r.read().decode("utf-8")
    assert 'id="f-pnl"' in page and 'id="d-pnl"' in page and "/pnl-history/" in page
    assert "今日" not in page
