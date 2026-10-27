"""Tests for scripts/dashboard — the read-only bot dashboard.

The dashboard polls live bot directories every few seconds, so the
properties that matter most are: it never blocks a bot's own writes, it
never changes a bot directory, it agrees with the monitor about
liveness, and nothing sensitive leaves through the page.
"""

import http.client
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

import pytest

from scripts.dashboard import fileio, server, snapshot
from scripts.monitor.common import TZ_TPE

NOW = datetime(2026, 10, 26, 10, 0, tzinfo=TZ_TPE)
ALIVE_PID = 4242
DEAD_PID = 9999
FAKE_CHANNEL = "1111111111111111111"     # placeholder snowflake
FAKE_USER_ID = "F111111111"              # placeholder TW-ID shape


def _alive(pid):
    return pid == ALIVE_PID


def make_bot(base, name="TMF00_alpha", *, lock_pid=ALIVE_PID, hb_age_s=3.0,
             position=None, trades=None, point_value=10):
    d = base / name
    d.mkdir(parents=True)
    broker = {"trades": trades or [], "_cumulative_pnl": sum(
        t["pnl"] for t in (trades or [])), "position_size": 0}
    if position:
        broker.update(position)
    (d / "session.json").write_text(json.dumps({
        "strategy": "Demo", "symbol": "TMF00", "bot_name": name.split("_", 1)[1],
        "point_value": point_value, "target_interval": 900,
        "trading_mode": "paper", "broker": broker}), encoding="utf-8")
    if lock_pid is not None:
        (d / ".lock").write_text(str(lock_pid))
    if hb_age_s is not None:
        (d / "heartbeat.json").write_text(json.dumps({
            "ts": NOW.timestamp() - hb_age_s, "pid": lock_pid,
            "inflight_com_call": None}))
    (d / "decisions.csv").write_text(
        "datetime,bar_dt,strategy,action,side,tag,price,reason\n"
        # column 0 is the machine clock: deliberately nonsense here
        "1999-01-01 00:00:00,2026-10-26 09:15,Demo,ENTRY_FILL,LONG,Long,45000,"
        "filled at next-open\n", encoding="utf-8")
    (d / "bars_1m_20261026.csv").write_text(
        "datetime,open,high,low,close,volume\n"
        "2026/10/26 09:16,45010,45020,45000,45015,9\n"
        "2026/10/26 09:15,45000,45010,44990,45005,7\n"   # out of order
        "2026/10/26 09:16,45010,45030,45000,45100,11\n",  # duplicate: last wins
        encoding="utf-8")
    (d / "debug_20261026.log").write_text(
        f"[2026-10-26 09:00:00.000 TPE] Discord send attempt: channel={FAKE_CHANNEL}\n"
        "[2026-10-26 09:00:01.000 TPE] Traceback (most recent call last)\n"
        f"[2026-10-26 09:00:02.000 TPE] login user {FAKE_USER_ID} ok\n",
        encoding="utf-8")
    return d


# ── fileio: never block the bot's writes ────────────────────────────────

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")


@windows_only
def test_plain_open_reader_makes_save_session_raise(tmp_path):
    """The hazard the shared opener exists for (fail-first baseline)."""
    from src.live.session_store import save_session
    target = tmp_path / "session.json"
    save_session(str(target), {"old": 1})
    with open(target, "rb"):
        with pytest.raises(PermissionError):
            save_session(str(target), {"new": 1})


@windows_only
def test_save_session_succeeds_while_dashboard_reads(tmp_path):
    from src.live.session_store import load_session, save_session
    target = tmp_path / "session.json"
    save_session(str(target), {"old": 1})
    with fileio.open_shared(str(target)) as f:
        save_session(str(target), {"new": 1})
        assert json.loads(f.read()) == {"old": 1}    # reader keeps old data
    assert load_session(str(target)) == {"new": 1}


@windows_only
def test_lock_removal_succeeds_while_dashboard_reads(tmp_path):
    lock = tmp_path / ".lock"
    lock.write_text("1")
    with fileio.open_shared(str(lock)):
        os.remove(lock)                              # clean-stop lock removal
    assert not lock.exists()


@windows_only
def test_heartbeat_replace_retry_outlasts_one_read(tmp_path):
    """os.replace fails against any open handle; the writer's retry wins
    once the dashboard's single read closes its handle."""
    from src.live.main_heartbeat import atomic_write_json
    target = tmp_path / "heartbeat.json"
    target.write_text('{"ts": 1}')
    reader = fileio.open_shared(str(target))
    sleeps = []

    def _sleep(s):
        sleeps.append(s)
        reader.close()

    atomic_write_json(str(target), {"ts": 2}, sleep=_sleep)
    assert sleeps and json.loads(target.read_text()) == {"ts": 2}


def test_fileio_missing_and_garbage(tmp_path):
    assert fileio.read_json(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "half.json"
    bad.write_text('{"broker": {"tra')
    assert fileio.read_json(str(bad)) is None


def test_tail_lines_drops_partial_first_line(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("".join(f"line-{i:04d}\n" for i in range(1000)))
    lines = fileio.tail_lines(str(p), 5, max_bytes=100)
    assert lines == [f"line-{i:04d}" for i in range(995, 1000)]
    assert all(line.startswith("line-")
               for line in fileio.tail_lines(str(p), 50, max_bytes=95))


# ── liveness agrees with the monitor ────────────────────────────────────

def test_state_running(tmp_path):
    d = make_bot(tmp_path)
    assert snapshot.bot_state(NOW, str(d), pid_alive_fn=_alive)["state"] == "RUNNING"


def test_state_hung_when_heartbeat_old(tmp_path):
    d = make_bot(tmp_path, hb_age_s=600)
    st = snapshot.bot_state(NOW, str(d), pid_alive_fn=_alive)
    assert st["state"] == "HUNG"
    assert st["heartbeat_age_s"] == pytest.approx(600)


def test_state_lagging(tmp_path):
    d = make_bot(tmp_path, hb_age_s=60)
    assert snapshot.bot_state(NOW, str(d), pid_alive_fn=_alive)["state"] == "LAGGING"


def test_state_no_heartbeat(tmp_path):
    d = make_bot(tmp_path, hb_age_s=None)
    assert snapshot.bot_state(NOW, str(d), pid_alive_fn=_alive)["state"] == "NO_HEARTBEAT"


def test_state_stopped_without_lock(tmp_path):
    d = make_bot(tmp_path, lock_pid=None, hb_age_s=None)
    assert snapshot.bot_state(NOW, str(d), pid_alive_fn=_alive)["state"] == "STOPPED"


def test_dead_lock_is_crash_only_when_recent(tmp_path):
    d = make_bot(tmp_path, lock_pid=DEAD_PID)
    recent = NOW.timestamp() - 3600
    old = NOW.timestamp() - 30 * 86400
    assert snapshot.bot_state(NOW, str(d), pid_alive_fn=_alive,
                              activity_ts=recent)["state"] == "CRASHED"
    assert snapshot.bot_state(NOW, str(d), pid_alive_fn=_alive,
                              activity_ts=old)["state"] == "STOPPED"


# ── summary numbers ─────────────────────────────────────────────────────

def _trade(pnl, exit_dt):
    return {"side": "LONG", "qty": 1, "entry_price": 45000,
            "exit_price": 45000 + pnl // 10, "entry_dt": "2026-10-23 09:00:00",
            "exit_dt": exit_dt, "pnl": pnl, "exit_tag": "force_close",
            "source": "paper"}


def test_summary_pnl_today_and_win_rate(tmp_path):
    trades = [_trade(500, "2026-10-26 09:30:00"), _trade(-200, "2026-10-26 09:50:00"),
              _trade(1000, "2026-10-23 13:40:00")]
    d = make_bot(tmp_path, trades=trades)
    s = snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)
    assert s["pnl_today"] == 300
    assert s["trades_today"] == 2
    assert s["pnl_total"] == 1300
    assert s["win_rate"] == pytest.approx(0.667)
    assert s["last_decision"]["bar_dt"] == "2026-10-26 09:15"   # bar_dt, not col 0


def test_non_dict_trades_do_not_drop_the_fleet(tmp_path):
    """One malformed trades entry must not 500 /api/fleet for every bot."""
    d = make_bot(tmp_path, trades=[_trade(500, "2026-10-26 09:30:00")])
    sess_path = d / "session.json"
    sess = json.loads(sess_path.read_text(encoding="utf-8"))
    sess["broker"]["trades"] = ["x", sess["broker"]["trades"][0], None]
    sess_path.write_text(json.dumps(sess), encoding="utf-8")
    make_bot(tmp_path, "TX00_beta", trades=[_trade(-100, "2026-10-26 09:40:00")])
    data = snapshot.fleet(NOW, str(tmp_path), pid_alive_fn=_alive)
    by_id = {b["id"]: b for b in data["bots"]}
    assert set(by_id) == {"TMF00_alpha", "TX00_beta"}
    assert by_id["TMF00_alpha"]["trades"] == 1
    assert by_id["TMF00_alpha"]["trades_today"] == 1
    assert by_id["TMF00_alpha"]["pnl_today"] == 500
    assert by_id["TMF00_alpha"]["win_rate"] == pytest.approx(1)
    assert by_id["TX00_beta"]["pnl_today"] == -100
    det = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)
    assert [t["pnl"] for t in det["trades"]] == [500]


@pytest.mark.parametrize("side,real,expected", [
    ("LONG", 0, (45100 - 45000) * 2 * 10),
    ("LONG", 45050, (45100 - 45050) * 2 * 10),   # confirmed real fill wins
    ("SHORT", 0, (45000 - 45100) * 2 * 10),
])
def test_unrealized_uses_last_bar_close(tmp_path, side, real, expected):
    d = make_bot(tmp_path, position={"position_size": 2, "position_side": side,
                                     "entry_price": 45000, "real_entry_price": real})
    s = snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)
    assert s["last_price"] == 45100
    assert s["position"]["unrealized"] == expected


def test_retired_bot_is_inactive(tmp_path):
    d = make_bot(tmp_path, lock_pid=None, hb_age_s=None)
    old = NOW.timestamp() - 30 * 86400
    for name in os.listdir(d):
        os.utime(d / name, (old, old))
    assert snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)["active"] is False


# ── detail ──────────────────────────────────────────────────────────────

def test_bars_sorted_and_deduplicated(tmp_path):
    d = make_bot(tmp_path)
    bars = snapshot.read_bars(str(d / "bars_1m_20261026.csv"))
    times = [b["time"] for b in bars]
    assert times == sorted(times) and len(times) == len(set(times)) == 2
    assert bars[-1]["close"] == 45100


def test_chart_time_is_tpe_wall_clock():
    t = snapshot.chart_time(datetime(2026, 10, 26, 9, 15, tzinfo=TZ_TPE))
    assert (t // 60) % (24 * 60) == 9 * 60 + 15


def test_markers_only_inside_charted_day():
    trades = [snapshot._trade_row(_trade(500, "2026-10-26 09:16:30")),
              snapshot._trade_row(_trade(500, "2026-10-20 09:16:00"))]
    t0 = snapshot.chart_time(datetime(2026, 10, 26, 9, 0, tzinfo=TZ_TPE))
    t1 = snapshot.chart_time(datetime(2026, 10, 26, 13, 45, tzinfo=TZ_TPE))
    marks = snapshot.trade_markers(trades, t0, t1)
    assert [m["kind"] for m in marks] == ["exit"]
    assert marks[0]["time"] % 60 == 0


def test_detail_log_is_redacted_and_flagged(tmp_path):
    d = make_bot(tmp_path)
    det = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)
    text = "\n".join(line["text"] for line in det["log"])
    assert FAKE_CHANNEL not in text
    assert FAKE_USER_ID not in text
    assert [line["sev"] for line in det["log"]] == ["", "P1", ""]
    assert det["day"] == "20261026" and det["days"] == ["20261026"]


# Placeholder shapes. Not a live webhook, key, or token.
_WEBHOOK = ("https://discord.com/api/webhooks/123456789012345678/"
            "placeholderToken")
_KEY = "sk-ant-placeholder-key"
_TOKEN = "P" * 50


def test_detail_log_applies_monitor_redact(tmp_path):
    """Replacing redact(text) with text must fail: webhooks, keys, long tokens."""
    assert len(_TOKEN) >= 50
    d = make_bot(tmp_path)
    log = d / "debug_20261026.log"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"[2026-10-26 09:00:03.000 TPE] webhook {_WEBHOOK}\n")
        fh.write(f"[2026-10-26 09:00:04.000 TPE] key {_KEY}\n")
        fh.write(f"[2026-10-26 09:00:05.000 TPE] token {_TOKEN}\n")
    det = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)
    text = "\n".join(line["text"] for line in det["log"])
    assert _WEBHOOK not in text
    assert "discord.com/api/webhooks" not in text
    assert "<webhook>" in text
    assert _KEY not in text and "<key>" in text
    assert _TOKEN not in text and "<token>" in text


def test_find_bot_dir_exact_match_only(tmp_path):
    make_bot(tmp_path)
    assert snapshot.find_bot_dir(str(tmp_path), "TMF00_alpha")
    for bad in ("../TMF00_alpha", "TMF00_alpha/..", "TMF00_", "", "..", "C:\\Windows"):
        assert snapshot.find_bot_dir(str(tmp_path), bad) is None


def test_snapshot_never_modifies_bot_dir(tmp_path):
    d = make_bot(tmp_path, lock_pid=DEAD_PID)     # a stale lock must survive
    before = {n: (os.path.getmtime(d / n), os.path.getsize(d / n)) for n in os.listdir(d)}
    snapshot.fleet(NOW, str(tmp_path), pid_alive_fn=_alive)
    snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)
    after = {n: (os.path.getmtime(d / n), os.path.getsize(d / n)) for n in os.listdir(d)}
    assert after == before


def _try_symlink(src, dst):
    try:
        os.symlink(os.fspath(src), os.fspath(dst),
                   target_is_directory=os.path.isdir(src))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")


def test_symlink_log_outside_base_is_not_served(tmp_path):
    base = tmp_path / "live"
    d = make_bot(base)
    outside = tmp_path / "secret.txt"
    outside.write_text("OUTSIDE-FILE-MARKER\n", encoding="utf-8")
    log = d / "debug_20261026.log"
    log.unlink()
    _try_symlink(outside, log)
    det = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)
    blob = json.dumps(det)
    assert "OUTSIDE-FILE-MARKER" not in blob
    assert det["log"] == []
    assert det["log_file"] == ""
    assert det["bot"]["strategy"] == "Demo"


def test_symlink_session_outside_base_is_not_read(tmp_path):
    base = tmp_path / "live"
    d = make_bot(base)
    outside = tmp_path / "outside-session.json"
    outside.write_text(json.dumps({
        "strategy": "LEAKED", "trading_mode": "paper", "point_value": 10,
        "broker": {"trades": [], "_cumulative_pnl": 99999, "position_size": 0},
    }), encoding="utf-8")
    sess = d / "session.json"
    sess.unlink()
    _try_symlink(outside, sess)
    summary = snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)
    assert summary["strategy"] == ""
    assert summary["pnl_total"] == 0
    assert summary["state"] == "RUNNING"
    assert "LEAKED" not in json.dumps(summary)


def test_symlinked_bot_dir_is_skipped(tmp_path):
    base = tmp_path / "live"
    base.mkdir()
    make_bot(tmp_path / "elsewhere")
    _try_symlink(tmp_path / "elsewhere" / "TMF00_alpha", base / "TMF00_alpha")
    make_bot(base, "TX00_real")
    data = snapshot.fleet(NOW, str(base), pid_alive_fn=_alive)
    assert [b["id"] for b in data["bots"]] == ["TX00_real"]
    assert snapshot.find_bot_dir(str(base), "TMF00_alpha") is None


# ── server ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("addr,ok", [
    ("127.0.0.1", True), ("::1", True), ("100.101.102.103", True),
    ("fd7a:115c:a1e0::1", True), ("::ffff:100.64.0.1", True),
    ("192.168.1.20", False), ("10.0.0.5", False), ("8.8.8.8", False), ("junk", False),
])
def test_client_allowed(addr, ok):
    assert server.client_allowed(addr) is ok


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    make_bot(tmp_path)
    monkeypatch.setattr(server.DashboardHandler, "base_dir", str(tmp_path))
    monkeypatch.setattr(server.DashboardHandler, "cache", server._Cache(0))
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_server_routes(live_server):
    status, ctype, body = _get(live_server + "/")
    assert status == 200 and ctype.startswith("text/html")
    assert "機器人監控".encode("utf-8") in body
    _, _, body = _get(live_server + "/api/fleet")
    assert [b["id"] for b in json.loads(body)["bots"]] == ["TMF00_alpha"]
    _, _, body = _get(live_server + "/api/bot?id=TMF00_alpha")
    assert json.loads(body)["bot"]["id"] == "TMF00_alpha"


@pytest.mark.parametrize("path", ["/api/bot?id=../TMF00_alpha", "/api/bot?id=",
                                  "/etc/passwd", "/static/../server.py"])
def test_server_rejects_unknown_paths(live_server, path):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(live_server + path)
    assert exc.value.code == 404


def test_server_is_get_only(live_server):
    req = urllib.request.Request(live_server + "/api/fleet", data=b"x", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 501


def test_refuses_non_tailscale_bind(capsys):
    assert server.main(["--bind", "0.0.0.0"]) == 2
    assert "refusing" in capsys.readouterr().err


_LWC_SRI = "sha384-JZigAjwiaZtkUbA44CWkPaT3iBb/mU5pO6QOANp+OqHd4q+1+7MG1kzp2OOP9ZfP"


def _dashboard_html() -> str:
    path = os.path.join(os.path.dirname(server.__file__), "static", "index.html")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_cdn_fallback_defined_before_local_chart_script():
    """onerror on /static/lwc.js fires before later <script> blocks parse,
    so the fallback must already exist (blank chart on Steven otherwise)."""
    html = _dashboard_html()
    assert html.index("function loadCdnCharts") < html.index('src="/static/lwc.js"')


def test_cdn_fallback_has_sri():
    html = _dashboard_html()
    assert _LWC_SRI in html
    assert 's.crossOrigin = "anonymous"' in html
    assert html.index(_LWC_SRI) < html.index("document.head.appendChild(s)")
    assert html.index('s.crossOrigin = "anonymous"') < html.index(_LWC_SRI)


def test_missing_local_chart_bundle_is_404(live_server, monkeypatch):
    monkeypatch.setattr(server.DashboardHandler, "lwc_path", None)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(live_server + "/static/lwc.js")
    assert exc.value.code == 404


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _FakeServer:
    def __init__(self, addr):
        self.addr, self.closed = addr, False

    def server_close(self):
        self.closed = True


def test_bind_waits_for_tailscale_ip_at_boot():
    clock, attempts = _FakeClock(), []

    def make(addr, port):
        attempts.append(addr)
        if addr.startswith("100.") and clock.t < 20:
            raise OSError(10049, "address not available yet")
        return _FakeServer(addr)

    servers = server.bind_servers(["127.0.0.1", "100.74.0.1"], 8765, 60, make=make,
                                  sleep=clock.sleep, monotonic=clock.monotonic)
    assert [s.addr for s in servers] == ["127.0.0.1", "100.74.0.1"]
    assert attempts.count("100.74.0.1") == 5


def test_bind_gives_up_after_wait_and_closes_bound_servers():
    clock, made = _FakeClock(), []

    def make(addr, port):
        if addr.startswith("100."):
            raise OSError(10049, "address not available")
        made.append(_FakeServer(addr))
        return made[-1]

    with pytest.raises(OSError):
        server.bind_servers(["127.0.0.1", "100.74.0.1"], 8765, 30, make=make,
                            sleep=clock.sleep, monotonic=clock.monotonic)
    assert made and all(s.closed for s in made)


def test_bind_without_wait_fails_fast():
    clock = _FakeClock()

    def make(addr, port):
        raise OSError(10049, "address not available")

    with pytest.raises(OSError):
        server.bind_servers(["100.74.0.1"], 8765, 0, make=make,
                            sleep=clock.sleep, monotonic=clock.monotonic)
    assert clock.t == 0


# ── host allowlist (DNS rebinding) and the per-request peer gate ────────

def test_host_header_allowlist_matrix():
    kw = dict(binds=["127.0.0.1", "::1"], port=8765,
              magic_dns="Machine.Tailnet.ts.net")
    assert server.host_header_allowed("127.0.0.1:8765", **kw)
    assert server.host_header_allowed("localhost:8765", **kw)
    assert server.host_header_allowed("[::1]:8765", **kw)
    assert server.host_header_allowed("machine.tailnet.ts.net:8765", **kw)
    assert server.host_header_allowed(None, **kw) is False
    assert server.host_header_allowed("", **kw) is False
    assert server.host_header_allowed("evil.example", **kw) is False
    assert server.host_header_allowed("evil.example:8765", **kw) is False
    assert server.host_header_allowed("127.0.0.1:1", **kw) is False
    assert server.host_header_allowed("localhost.evil.example:8765", **kw) is False
    # Documentation address (RFC 5737), not a bound address.
    assert server.host_header_allowed("192.0.2.8:8765", **kw) is False
    bare = dict(binds=["127.0.0.1"], port=8765, magic_dns="")
    assert server.host_header_allowed("machine.tailnet.ts.net:8765", **bare) is False


def test_magic_dns_flag_rejects_non_names(capsys):
    assert server.normalize_magic_dns("machine.tailnet.ts.net") == "machine.tailnet.ts.net"
    assert server.normalize_magic_dns("Machine.Tailnet.ts.net.") == "machine.tailnet.ts.net"
    assert server.normalize_magic_dns("127.0.0.1") == ""
    assert server.normalize_magic_dns("not a host") == ""
    assert server.normalize_magic_dns("") == ""
    assert server.main(["--magic-dns", "not a host"]) == 2
    assert "magic-dns" in capsys.readouterr().err


def _server_port(live_server: str) -> int:
    return urlparse(live_server).port


def _http(port: int, host_header: str | None, path: str = "/api/fleet") -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        if host_header is None:
            conn.putrequest("GET", path, skip_host=True)
            conn.putheader("Connection", "close")
            conn.endheaders()
        else:
            conn.request("GET", path, headers={
                "Host": host_header, "Connection": "close"})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_evil_host_is_rejected(live_server):
    port = _server_port(live_server)
    for host in ("evil.example", f"evil.example:{port}"):
        status, body = _http(port, host)
        assert status == 421
        assert b"TMF00_alpha" not in body
        assert b"misdirected" in body


def test_allowed_hosts_return_fleet(live_server):
    port = _server_port(live_server)
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"LocalHost:{port}"):
        status, body = _http(port, host)
        assert status == 200
        assert json.loads(body)["bots"][0]["id"] == "TMF00_alpha"


def test_missing_host_header_is_rejected(live_server):
    status, body = _http(_server_port(live_server), None)
    assert status == 421
    assert b"TMF00_alpha" not in body


def test_duplicate_host_header_is_rejected(live_server):
    port = _server_port(live_server)
    raw = (f"GET /api/fleet HTTP/1.1\r\n"
           f"Host: 127.0.0.1:{port}\r\n"
           f"Host: evil.example:{port}\r\n"
           f"Connection: close\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
    status = int(data.split(b"\r\n", 1)[0].split()[1])
    assert status == 421
    assert b"TMF00_alpha" not in data


def test_configured_magic_dns_host_is_allowed(live_server, monkeypatch):
    monkeypatch.setattr(server.DashboardHandler, "magic_dns", "machine.tailnet.ts.net")
    port = _server_port(live_server)
    status, body = _http(port, f"machine.tailnet.ts.net:{port}")
    assert status == 200
    assert json.loads(body)["bots"][0]["id"] == "TMF00_alpha"
    status, body = _http(port, f"Machine.Tailnet.ts.net:{port}")
    assert status == 200 and b"TMF00_alpha" in body
    status, body = _http(port, f"other.tailnet.ts.net:{port}")
    assert status == 421 and b"TMF00_alpha" not in body


def test_magic_dns_host_rejected_when_unset(live_server):
    port = _server_port(live_server)
    status, body = _http(port, f"machine.tailnet.ts.net:{port}")
    assert status == 421
    assert b"TMF00_alpha" not in body


class _DisallowedPeer(server.ThreadingHTTPServer):
    def get_request(self):
        request, addr = super().get_request()
        # RFC 5737 documentation address. Not a host, and outside the peer allowlist.
        return request, ("192.0.2.8", addr[1])


def test_disallowed_peer_gets_403(tmp_path, monkeypatch):
    """HTTP-level peer gate. A non-allowlisted client_address is 403."""
    assert server.client_allowed("192.0.2.8") is False
    make_bot(tmp_path)
    monkeypatch.setattr(server.DashboardHandler, "base_dir", str(tmp_path))
    monkeypatch.setattr(server.DashboardHandler, "cache", server._Cache(0))
    srv = _DisallowedPeer(("127.0.0.1", 0), server.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        status, body = _http(port, f"127.0.0.1:{port}")
        assert status == 403
        assert b"forbidden" in body
        assert b"TMF00_alpha" not in body
    finally:
        srv.shutdown()
        srv.server_close()
