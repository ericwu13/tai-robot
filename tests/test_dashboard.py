"""Tests for scripts/dashboard — the read-only bot dashboard.

The dashboard polls live bot directories every few seconds, so the
properties that matter most are: it never blocks a bot's own writes, it
never changes a bot directory, it agrees with the monitor about
liveness, and nothing sensitive leaves through the page.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime

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


def test_cdn_fallback_defined_before_local_chart_script():
    """onerror on /static/lwc.js fires before later <script> blocks parse,
    so the fallback must already exist (blank chart on Steven otherwise)."""
    html = open(os.path.join(os.path.dirname(server.__file__), "static", "index.html"),
                encoding="utf-8").read()
    assert html.index("function loadCdnCharts") < html.index('src="/static/lwc.js"')


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


# ── review fixes (PR #200) ──────────────────────────────────────────────

@pytest.mark.parametrize("text,leak", [
    ("acct F1111111112222 ok", "1111111112222"),          # futures account shape
    ("order L11112222 sent", "L11112222"),                 # client/order id
    (f"channel_{FAKE_CHANNEL} send", FAKE_CHANNEL),        # no \b before the id
    ("user f111111111 login", "f111111111"),               # lowercase TW-id shape
])
def test_redaction_covers_repo_id_shapes(text, leak):
    assert leak not in snapshot.redact_line(text)


def test_dead_lock_without_any_activity_is_crash(tmp_path):
    d = make_bot(tmp_path, lock_pid=DEAD_PID)
    assert snapshot.bot_state(NOW, str(d), pid_alive_fn=_alive,
                              activity_ts=None)["state"] == "CRASHED"


def test_one_malformed_bot_does_not_blank_the_fleet(tmp_path):
    make_bot(tmp_path, name="TMF00_good")
    bad = make_bot(tmp_path, name="TMF00_bad")
    (bad / "session.json").write_text(
        '{"point_value": 10, "broker": {"_cumulative_pnl": Infinity, '
        '"position_size": 1, "position_side": "LONG", "entry_price": 45000, '
        '"trades": [null, {"pnl": Infinity, "exit_dt": "2026-10-26 09:00"}]}}',
        encoding="utf-8")
    (bad / "bars_1m_20261026.csv").write_text(
        "datetime,open,high,low,close,volume\n2026/10/26 09:15,inf,inf,inf,inf,1\n",
        encoding="utf-8")
    bots = {b["id"]: b for b in snapshot.fleet(NOW, str(tmp_path), pid_alive_fn=_alive)["bots"]}
    assert bots["TMF00_good"]["state"] == "RUNNING"
    assert "TMF00_bad" in bots
    snapshot.bot_detail(NOW, str(bad), pid_alive_fn=_alive)


def test_one_bot_raising_is_isolated(tmp_path, monkeypatch):
    make_bot(tmp_path, name="TMF00_good")
    make_bot(tmp_path, name="TMF00_bad")
    real = snapshot.bot_summary

    def flaky(now, bot_dir, **kw):
        if bot_dir.endswith("TMF00_bad"):
            raise RuntimeError(f"boom {FAKE_CHANNEL}")
        return real(now, bot_dir, **kw)

    monkeypatch.setattr(snapshot, "bot_summary", flaky)
    bots = {b["id"]: b for b in snapshot.fleet(NOW, str(tmp_path), pid_alive_fn=_alive)["bots"]}
    assert bots["TMF00_bad"]["state"] == "UNKNOWN"
    assert FAKE_CHANNEL not in bots["TMF00_bad"]["error"]
    assert bots["TMF00_good"]["state"] == "RUNNING"


def test_pnl_today_is_the_taifex_trading_day(tmp_path):
    # Monday 10:00: the trading day began Friday 15:00 (night session).
    trades = [_trade(700, "2026-10-23 16:30:00"),     # Fri night → counts
              _trade(-200, "2026-10-26 09:50:00"),    # Mon day → counts
              _trade(1000, "2026-10-23 13:40:00")]    # Fri day → previous day
    d = make_bot(tmp_path, trades=trades)
    s = snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)
    assert (s["pnl_today"], s["trades_today"]) == (500, 2)
    assert s["trading_day_start"] == "2026-10-23 15:00"


def test_session_json_reopened_only_when_it_changes(tmp_path, monkeypatch):
    d = make_bot(tmp_path)
    snapshot._session_cache.clear()
    opens = []
    real = fileio.read_json
    monkeypatch.setattr(fileio, "read_json", lambda p: opens.append(p) or real(p))
    for _ in range(3):
        snapshot.read_session(str(d))
    assert len(opens) == 1
    sj = d / "session.json"
    sj.write_text(json.dumps({"strategy": "Changed", "broker": {}}), encoding="utf-8")
    os.utime(sj, ns=(os.stat(sj).st_atime_ns, os.stat(sj).st_mtime_ns + 10**9))
    assert snapshot.read_session(str(d))["strategy"] == "Changed"
    assert len(opens) == 2


def test_session_garbage_keeps_last_good_copy(tmp_path):
    d = make_bot(tmp_path)
    snapshot._session_cache.clear()
    assert snapshot.read_session(str(d))["strategy"] == "Demo"
    sj = d / "session.json"
    sj.write_text('{"strategy": "Dem', encoding="utf-8")
    os.utime(sj, ns=(os.stat(sj).st_atime_ns, os.stat(sj).st_mtime_ns + 10**9))
    assert snapshot.read_session(str(d))["strategy"] == "Demo"


@pytest.mark.parametrize("host,ok", [
    (None, True), ("localhost:8765", True), ("127.0.0.1:8765", True), ("[::1]:8765", True),
    ("100.74.0.1:8765", True), ("steven:8765", True), ("Steven.tail1234.ts.net", True),
    ("attacker.example", False), ("attacker.example:8765", False),
    ("steven.attacker.example", False), ("evil.ts.net", False),
])
def test_host_allowed_blocks_dns_rebinding(host, ok):
    assert server.host_allowed(host, {"127.0.0.1", "100.74.0.1"}, "Steven") is ok


def test_server_rejects_foreign_host_header(live_server):
    req = urllib.request.Request(live_server + "/api/fleet",
                                 headers={"Host": "attacker.example"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 403


def test_bound_address_serves_before_later_ones_bind():
    clock, served = _FakeClock(), []

    def make(addr, port):
        if addr.startswith("100.") and clock.t < 20:
            raise OSError(10049, "not yet")
        return _FakeServer(addr)

    def on_bound(addr, srv):
        served.append((addr, clock.t))

    server.bind_servers(["127.0.0.1", "100.74.0.1"], 8765, 60, make=make,
                        on_bound=on_bound, sleep=clock.sleep, monotonic=clock.monotonic)
    assert served[0] == ("127.0.0.1", 0)       # localhost up immediately
    assert served[1][0] == "100.74.0.1" and served[1][1] >= 20


def test_tailscale_lookup_waits_at_boot():
    clock, answers = _FakeClock(), iter([None, None, "100.74.0.1"])
    ip = server.wait_for_tailscale_ip(60, lookup=lambda: next(answers),
                                      sleep=clock.sleep, monotonic=clock.monotonic)
    assert ip == "100.74.0.1" and clock.t == 10


def test_tailscale_lookup_gives_up_after_wait():
    clock = _FakeClock()
    assert server.wait_for_tailscale_ip(12, lookup=lambda: None, sleep=clock.sleep,
                                        monotonic=clock.monotonic) is None


def test_cdn_fallback_is_pinned_with_sri():
    html = open(os.path.join(os.path.dirname(server.__file__), "static", "index.html"),
                encoding="utf-8").read()
    assert 'integrity = "sha384-' in html and 'crossOrigin = "anonymous"' in html
    assert "lightweight-charts@4.1.3/" in html


def test_detail_log_applies_monitor_redact(tmp_path):
    d = make_bot(tmp_path)
    secret_hook = "https://discord.com/api/webhooks/" + FAKE_CHANNEL + "/" + "X" * 60
    (d / "debug_20261026.log").write_text(
        f"[2026-10-26 09:00:00.000 TPE] post {secret_hook}\n"
        "[2026-10-26 09:00:01.000 TPE] key sk-ant-placeholder-value\n", encoding="utf-8")
    text = "\n".join(line["text"] for line in
                     snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)["log"])
    assert "discord.com/api/webhooks" not in text
    assert "sk-ant-placeholder-value" not in text
