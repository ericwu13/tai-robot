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
    assert "bars" not in det and det["log_file"] == "debug_20261026.log"


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


def test_trading_day_pnl_label_is_ben_jiaoyi_ri(live_server):
    """pnl_today is the TAIFEX trading day (night session + day session).

    The zh-TW label is 本交易日. The calendar-day word 今日 must not
    name that column, and there is no English "Today" label for it.
    """
    status, ctype, body = _get(live_server + "/")
    assert status == 200 and ctype.startswith("text/html")
    page = body.decode("utf-8")
    assert page.count("本交易日") == 5          # 4 simulated + the real-order tile
    assert 'title="自最近一次夜盤開盤（15:00）起">本交易日</th>' in page
    assert 'tile("本交易日實單"' in page
    assert "本交易日已實現" not in page
    assert "今日" not in page
    assert "Today" not in page


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
    assert exc.value.code == 405


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
    snapshot._stat_cache.clear()
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
    snapshot._stat_cache.clear()
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


# ── continuous K-lines (bars_window) ────────────────────────────────────

def _bars_file(d, day, rows):
    (d / f"bars_1m_{day}.csv").write_text(
        "datetime,open,high,low,close,volume\n"
        + "".join(f"{t},100,101,99,{c},1\n" for t, c in rows), encoding="utf-8")


def test_bars_window_stitches_the_night_across_midnight(tmp_path):
    d = make_bot(tmp_path)
    _bars_file(d, "20261022", [("2026/10/22 23:58", 1), ("2026/10/22 23:59", 2)])
    _bars_file(d, "20261023", [("2026/10/23 00:00", 3), ("2026/10/23 00:01", 4)])
    w = snapshot.bars_window(str(d), before="20261024", days=2)
    assert [b["close"] for b in w["bars"]] == [1, 2, 3, 4]
    times = [b["time"] for b in w["bars"]]
    assert times == sorted(times) and len(set(times)) == 4
    assert (w["oldest_day"], w["newest_day"], w["has_more"]) == ("20261022", "20261023", False)


def test_bars_window_pages_backwards_without_overlap(tmp_path):
    d = make_bot(tmp_path)
    for day in ("20261020", "20261021", "20261022", "20261023"):
        _bars_file(d, day, [(f"{day[:4]}/{day[4:6]}/{day[6:]} 09:00", int(day[-2:]))])
    first = snapshot.bars_window(str(d), days=2)
    assert first["newest_day"] == "20261026" and first["has_more"] is True
    seen = {b["time"] for b in first["bars"]}
    older = snapshot.bars_window(str(d), before=first["oldest_day"], days=2)
    assert not seen & {b["time"] for b in older["bars"]}
    last = snapshot.bars_window(str(d), before=older["oldest_day"], days=2)
    assert last["has_more"] is False and [b["close"] for b in last["bars"]] == [20]


def test_bars_window_caps_days_and_carries_markers(tmp_path):
    d = make_bot(tmp_path, trades=[_trade(500, "2026-10-26 09:16:20")])
    w = snapshot.bars_window(str(d), days=999)
    assert [m["kind"] for m in w["markers"]] == ["exit"]
    for day in range(1, 16):
        _bars_file(d, f"202610{day:02d}", [(f"2026/10/{day:02d} 09:00", day)])
    w = snapshot.bars_window(str(d), days=999)
    assert w["has_more"] is True
    assert len({b["time"] for b in w["bars"]}) <= snapshot.BARS_DAYS_MAX * 2


@pytest.mark.parametrize("query,code", [
    ("id=TMF00_alpha", 200), ("id=TMF00_alpha&before=20261027&days=2", 200),
    ("id=TMF00_alpha&days=x", 200), ("id=TMF00_alpha&before=../etc", 400),
    ("id=TMF00_alpha&before=2026102", 400), ("id=../TMF00_alpha", 404),
])
def test_bars_route_validates_input(live_server, query, code):
    url = live_server + "/api/bars?" + query
    if code == 200:
        assert _get(url)[0] == 200
    else:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(url)
        assert exc.value.code == code


# ── regime panel ────────────────────────────────────────────────────────

REGIME_HEADER = ("date,session,adx,plus_di,minus_di,atr_ratio,ema_slope,close,raw_regime,"
                 "effective_regime,confirm_count,decision,strategy_deployed,dry_run,override,"
                 "pnl,trades,strategy_active,applied,applied_at,trading_mode,votes,vote_rule")


def make_regime_bot(base, *, state=None, history_rows=(), **kw):
    d = make_bot(base, name="TMF00_regime", **kw)
    sess = json.loads((d / "session.json").read_text(encoding="utf-8"))
    sess.update(regime_mode=True, active_leg="short", long_strategy="LongS",
                short_strategy="ShortS", news_enabled=False)
    (d / "session.json").write_text(json.dumps(sess), encoding="utf-8")
    st = {"effective_regime": "trending-down", "effective_since": "2026-10-07",
          "raw_regime": "trending-down", "pending_label": "trending-down",
          "pending_count": 2, "paused_until_session": 50, "session_count": 49,
          "manual_override": "auto", "last_assessed": "2026-10-23|NIGHT",
          "key_format": "open-date",
          "last_features": {"adx": 57.2, "plus_di": 8.8, "minus_di": 34.7,
                            "atr_ratio": 0.94, "ema_slope": -337.5, "_vote_sources": []},
          "next_session": {"date": "2026-10-23", "action": "hold", "strategy": "",
                           "reason": "pause", "executed": False}}
    st.update(state or {})
    (d / "regime_state.json").write_text(json.dumps(st), encoding="utf-8")
    (d / "regime_history.csv").write_text(
        REGIME_HEADER + "\n" + "".join(r + "\n" for r in history_rows), encoding="utf-8")
    return d


def test_non_regime_bot_has_no_regime_view(tmp_path):
    d = make_bot(tmp_path)
    (d / "regime_state.json").write_text('{"effective_regime": "trending-up"}')  # leftover
    assert snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)["regime"] is None
    assert snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)["regime"] is None


def test_regime_summary_for_fleet_row(tmp_path):
    d = make_regime_bot(tmp_path)
    rg = snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)["regime"]
    assert rg == {"effective": "trending-down", "since": "2026-10-07", "active_leg": "short",
                  "pause": {"until_session": 50, "session_count": 49},
                  "next_action": "hold", "classified": True, "news_suppressed": False}


def test_pause_matches_monitor_rule():
    assert snapshot._pause({"paused_until_session": 49, "session_count": 49}) is not None
    assert snapshot._pause({"paused_until_session": 48, "session_count": 49}) is None
    assert snapshot._pause({"paused_until_session": True, "session_count": 0}) is None


def test_regime_detail_history_band_and_monitor_flags(tmp_path):
    rows = ["2026-10-22,NIGHT,48.4,10.2,47.6,0.92,-347.7,46291.0,trending-down,trending-down,1,"
            "hold,,,auto,0.0,0,ShortS,false,,paper,,",
            "2026-10-23,DAY,,,,,,,,,,,,,,1200.0,2,ShortS,,,paper,,",
            "2026-10-23,NIGHT,57.2,8.8,34.7,0.94,-337.5,45654.0,trending-down,trending-down,2,"
            "hold,,,auto,-300.0,1,ShortS,false,,paper,,"]
    d = make_regime_bot(tmp_path, history_rows=rows)
    r = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)["regime"]
    assert [h["date"] + h["session"] for h in r["history"]] == \
        ["2026-10-23NIGHT", "2026-10-23DAY", "2026-10-22NIGHT"]          # newest first
    assert r["history"][1]["pnl"] == 1200
    # NIGHT classifications take effect at that night's close (open + 14 h).
    assert [b["time"] for b in r["band"]] == [
        snapshot.chart_time(datetime(2026, 10, 23, 5, 0, tzinfo=TZ_TPE)),
        snapshot.chart_time(datetime(2026, 10, 24, 5, 0, tzinfo=TZ_TPE))]
    assert r["features"]["adx"] == 57.2 and "_vote_sources" not in r["features"]
    texts = " ".join(f["text"] for f in r["flags"])
    assert "flip-counter pause active" in texts                      # check_regime's own rule
    assert {f["sev"] for f in r["flags"]} <= {"P1", "P2", "P3"}


def _age_bot(d, day="2026-10-20"):
    """Make every activity marker predate the last completed night (10-23)."""
    old = datetime.fromisoformat(f"{day}T09:00:00+08:00").timestamp()
    (d / "debug_20261026.log").write_text(f"[{day} 09:00:00.000 TPE] last line\n",
                                          encoding="utf-8")
    for p in d.iterdir():
        os.utime(p, (old, old))


def _monitor_findings(base, bot):
    from scripts.monitor.check_regime import check_regime
    findings, _ = check_regime(NOW, str(base))
    return {(f.level, snapshot.redact_line(f.message)) for f in findings
            if f.message.split(": ", 1)[0].endswith(bot) or f"{bot}:" in f.message}


@pytest.mark.parametrize("scenario", ["active", "stopped-before-the-night",
                                      "active-corrupt", "stopped-corrupt"])
def test_regime_flags_match_the_monitor(tmp_path, scenario):
    """Every flag the page shows is a finding check_regime reports for the
    same directory, at the same severity (including its stopped-bot demotion,
    which keys on activity since the last completed night, not on the lock)."""
    d = make_regime_bot(tmp_path)
    if "corrupt" in scenario:
        (d / "regime_state.json").write_text("{not json", encoding="utf-8")
    if scenario.startswith("stopped"):
        (d / ".lock").unlink()
        _age_bot(d)
    flags = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)["regime"]["flags"]
    assert flags
    monitor = _monitor_findings(tmp_path, "TMF00_regime")
    for f in flags:
        assert (f["sev"], f["text"]) in monitor, (f, monitor)
    if scenario.startswith("stopped"):
        assert {f["sev"] for f in flags} == {"P3"}
    else:
        assert {f["sev"] for f in flags} != {"P3"}
    if scenario == "active-corrupt":
        assert flags[0]["sev"] == "P1"


def test_clean_stop_during_the_night_is_not_demoted(tmp_path):
    """No lock, but active since the last night opened: the monitor still
    grades it (a bot that stopped mid-night can have missed its run)."""
    d = make_regime_bot(tmp_path, lock_pid=None, hb_age_s=None)
    flags = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)["regime"]["flags"]
    assert any(f["sev"] != "P3" for f in flags)


def test_regime_files_are_reread_only_when_they_change(tmp_path, monkeypatch):
    d = make_regime_bot(tmp_path, history_rows=[
        "2026-10-23,NIGHT,,,,,,,x,trending-up,1,hold,,,auto,0,0,S,false,,paper,,"])
    snapshot._stat_cache.clear()
    opened = []
    real_json, real_head = fileio.read_json, fileio.head_line
    monkeypatch.setattr(fileio, "read_json", lambda p: opened.append(os.path.basename(p)) or real_json(p))
    monkeypatch.setattr(fileio, "head_line", lambda p, *a: opened.append(os.path.basename(p)) or real_head(p, *a))
    for _ in range(3):
        snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)
        snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)
    assert opened.count("regime_state.json") == 1
    assert opened.count("regime_history.csv") == 1
    st = d / "regime_state.json"
    st.write_text(st.read_text(encoding="utf-8").replace("2026-10-07", "2026-10-08"),
                  encoding="utf-8")
    os.utime(st, ns=(os.stat(st).st_atime_ns, os.stat(st).st_mtime_ns + 10**9))
    assert snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)["regime"]["since"] == "2026-10-08"
    assert opened.count("regime_state.json") == 2


def test_regime_reason_and_history_are_redacted(tmp_path):
    d = make_regime_bot(tmp_path, state={"next_session": {
        "action": "hold", "reason": f"note channel={FAKE_CHANNEL}"}},
        history_rows=[f"2026-10-23,NIGHT,,,,,,,x,trending-up,1,hold,,,auto,0,0,"
                      f"user {FAKE_USER_ID},false,,paper,,"])
    r = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)["regime"]
    blob = json.dumps(r, ensure_ascii=False)
    assert FAKE_CHANNEL not in blob and FAKE_USER_ID not in blob


def test_corrupt_regime_files_do_not_break_detail(tmp_path):
    d = make_regime_bot(tmp_path)
    (d / "regime_state.json").write_text("{not json", encoding="utf-8")
    (d / "regime_history.csv").write_bytes(b"\xff\xfe garbage\n,,\n")
    r = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)["regime"]
    assert r["flags"][0]["sev"] == "P1" and r["history"] == []     # as check_regime


def test_bars_after_returns_every_day_since_and_flags_long_gaps(tmp_path):
    """The 10 s refresh asks for every file from its newest loaded day on, so
    a tab that slept across midnight fills the gap instead of skipping it."""
    d = make_bot(tmp_path)
    _bars_file(d, "20261024", [("2026/10/24 04:58", 7)])
    w = snapshot.bars_window(str(d), after="20261024", days=10)
    assert [b["close"] for b in w["bars"]][0] == 7 and w["complete"] is True
    assert w["newest_day"] == "20261026"
    for day in range(1, 12):
        _bars_file(d, f"202610{day:02d}", [(f"2026/10/{day:02d} 09:00", day)])
    w = snapshot.bars_window(str(d), after="20261001", days=10)
    assert w["complete"] is False             # page reloads instead of leaving a hole


def test_head_line_reads_csv_header_with_bom(tmp_path):
    p = tmp_path / "h.csv"
    p.write_bytes("﻿date,session\n1,2\n".encode("utf-8"))
    assert fileio.head_line(str(p)) == "date,session"
    assert fileio.head_line(str(tmp_path / "missing.csv")) is None


# ── controls (--enable-control) ─────────────────────────────────────────

def _set_mode(d, mode, position=None):
    sess = json.loads((d / "session.json").read_text(encoding="utf-8"))
    sess["trading_mode"] = mode
    if position:
        sess["broker"].update(position)
    (d / "session.json").write_text(json.dumps(sess), encoding="utf-8")


@pytest.fixture
def control_server(tmp_path, monkeypatch):
    base = tmp_path / "live"
    base.mkdir()
    make_bot(base)                                    # paper, running
    auto = make_bot(base, name="TMF00_real")
    _set_mode(auto, "auto")
    make_bot(base, name="TMF00_gone", lock_pid=None, hb_age_s=None)
    # Controls are judged against the real clock: give the live fixtures a
    # heartbeat that is fresh now, not relative to the fixed test NOW.
    import time as _time
    for hb in base.glob("*/heartbeat.json"):
        data = json.loads(hb.read_text())
        data["ts"] = _time.time()
        hb.write_text(json.dumps(data))
    log = tmp_path / "control_log.csv"
    monkeypatch.setattr(server.DashboardHandler, "base_dir", str(base))
    monkeypatch.setattr(server.DashboardHandler, "cache", server._Cache(0))
    monkeypatch.setattr(server.DashboardHandler, "control_enabled", True)
    monkeypatch.setattr(server.DashboardHandler, "control_log", str(log))
    # The fixtures' lock PID is fake; treat it as alive for both modules.
    monkeypatch.setattr(snapshot, "pid_alive", _alive)
    monkeypatch.setattr(snapshot.bot_summary, "__kwdefaults__",
                        {"pid_alive_fn": _alive})
    from scripts.dashboard import control
    monkeypatch.setattr(control, "process_command_line",
                        lambda pid: r'"C:\bots\tai_backtest.exe" deploy --symbol TMF00 --bot x')
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", base, log
    srv.shutdown()
    srv.server_close()


def _post(url, payload, *, headers=None, raw=None):
    h = {"Content-Type": "application/json", "X-Dashboard-Control": "1"}
    h.update(headers or {})
    req = urllib.request.Request(url + "/api/control", method="POST", headers=h,
                                 data=raw if raw is not None else json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {}


def _log_rows(log):
    import csv
    with open(log, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_controls_are_off_by_default(live_server):
    status, _ = _post(live_server, {"id": "TMF00_alpha", "action": "stop", "confirm": "alpha", "position_ack": "flat"})
    assert status == 405
    assert json.loads(_get(live_server + "/api/fleet")[2])["control_enabled"] is False


def test_stop_writes_the_headless_stop_file(control_server):
    from src.live.headless import stop_requested
    url, base, log = control_server
    status, body = _post(url, {"id": "TMF00_alpha", "action": "stop", "confirm": "alpha", "position_ack": "flat"})
    assert (status, body["ok"]) == (200, True)
    assert stop_requested(str(base / "TMF00_alpha"))          # what headless_app polls
    fleet = json.loads(_get(url + "/api/fleet")[2])
    alpha = next(b for b in fleet["bots"] if b["id"] == "TMF00_alpha")
    assert alpha["pending"] == {"stop": True, "mode": None}
    assert fleet["control_enabled"] is True
    rows = _log_rows(log)
    assert [(r["bot"], r["action"], r["result"]) for r in rows] == [("TMF00_alpha", "stop", "accepted")]


def test_stop_needs_the_typed_bot_name(control_server):
    url, base, log = control_server
    status, body = _post(url, {"id": "TMF00_alpha", "action": "stop", "confirm": "yes"})
    assert (status, body["ok"]) == (409, False)
    assert not (base / "TMF00_alpha" / "STOP").exists()
    assert _log_rows(log)[0]["result"] == "refused"


def test_stop_refused_for_a_bot_that_is_not_running(control_server):
    url, base, _ = control_server
    status, _ = _post(url, {"id": "TMF00_gone", "action": "stop", "confirm": "gone"})
    assert status == 409 and not (base / "TMF00_gone" / "STOP").exists()


def test_switch_to_paper_writes_what_live_runner_consumes(control_server):
    from src.live.live_runner import LiveRunner
    url, base, _ = control_server
    status, body = _post(url, {"id": "TMF00_real", "action": "paper", "confirm": "paper"})
    assert (status, body["ok"]) == (200, True)
    real = base / "TMF00_real"
    assert snapshot.pending(str(real))["mode"] == "paper"
    # The bot's own reader accepts the file and removes it.
    runner = object.__new__(LiveRunner)
    runner.bot_dir = str(real)
    runner._reloading_flag = False
    runner._reload_started_at = None
    assert runner._check_mode_override() == "paper"
    assert not (real / "mode_override.json").exists()


@pytest.mark.parametrize("action", ["auto", "semi_auto", "real", "full_auto", "AUTO", ""])
def test_nothing_can_switch_a_bot_up_to_real_orders(control_server, action):
    url, base, log = control_server
    for bot in ("TMF00_alpha", "TMF00_real"):
        for confirm in (action, "paper", bot.split("_", 1)[1]):   # every confirm the UI sends
            status, body = _post(url, {"id": bot, "action": action, "confirm": confirm})
            assert status == 409 and body["ok"] is False
            assert not (base / bot / "mode_override.json").exists()
            assert not (base / bot / "STOP").exists()


def test_apply_writes_nothing_for_an_unknown_action(tmp_path):
    from scripts.dashboard import control
    with pytest.raises(control.ControlRefused):
        control.apply("auto", str(tmp_path), "127.0.0.1")
    assert list(tmp_path.iterdir()) == []


def test_paper_refused_when_already_paper_pending_or_holding(control_server):
    url, base, _ = control_server
    assert _post(url, {"id": "TMF00_alpha", "action": "paper", "confirm": "paper"})[0] == 409
    assert _post(url, {"id": "TMF00_real", "action": "paper", "confirm": "paper"})[0] == 200
    assert _post(url, {"id": "TMF00_real", "action": "paper", "confirm": "paper"})[0] == 409
    (base / "TMF00_real" / "mode_override.json").unlink()
    _set_mode(base / "TMF00_real", "auto", position={
        "position_size": 1, "position_side": "LONG", "entry_price": 45000})
    status, body = _post(url, {"id": "TMF00_real", "action": "paper", "confirm": "paper"})
    assert status == 409 and "持倉" in body["message"]
    assert not (base / "TMF00_real" / "mode_override.json").exists()


@pytest.mark.parametrize("headers,raw,code", [
    ({"X-Dashboard-Control": ""}, None, 403),                     # no custom header
    ({"Content-Type": "text/plain"}, None, 403),                  # form-able content type
    ({"Origin": "http://attacker.example"}, None, 403),           # cross-site page
    ({"Host": "attacker.example"}, None, 403),                    # DNS rebinding
    ({}, b"{not json", 400),
    ({}, b"x" * 5000, 400),
    ({}, json.dumps({"id": "TMF00_alpha", "action": "stop", "confirm": 1}).encode(), 400),
])
def test_control_rejects_cross_site_and_malformed_requests(control_server, headers, raw, code):
    url, base, _ = control_server
    status, _ = _post(url, {"id": "TMF00_alpha", "action": "stop", "confirm": "alpha", "position_ack": "flat"},
                      headers=headers, raw=raw)
    assert status == code
    assert not (base / "TMF00_alpha" / "STOP").exists()


def test_same_origin_request_is_accepted(control_server):
    url, base, _ = control_server
    status, _ = _post(url, {"id": "TMF00_alpha", "action": "stop", "confirm": "alpha", "position_ack": "flat"},
                      headers={"Origin": url})
    assert status == 200 and (base / "TMF00_alpha" / "STOP").exists()


def test_control_cannot_reach_outside_the_bot_root(control_server):
    url, base, _ = control_server
    for bot in ("../TMF00_alpha", "..", "TMF00_alpha/..", "C:\\Windows"):
        status, body = _post(url, {"id": bot, "action": "stop", "confirm": "alpha"})
        assert status == 409 and body["ok"] is False
    assert not any(p.name == "STOP" for p in base.rglob("STOP"))


def test_control_log_defaults_next_to_the_bot_root(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(server, "bind_servers",
                        lambda *a, **k: calls.setdefault("bound", True) and (_ for _ in ()).throw(OSError("stop")))
    assert server.main(["--base-dir", str(tmp_path / "data" / "live"), "--enable-control"]) == 3
    assert server.DashboardHandler.control_enabled is True
    assert server.DashboardHandler.control_log == str(tmp_path / "data" / "dashboard_control_log.csv")
    monkeypatch.setattr(server.DashboardHandler, "control_enabled", False)


def test_control_log_redacts_and_survives_unwritable_path(tmp_path):
    from scripts.dashboard import control
    log = tmp_path / "log.csv"
    control.log_request(str(log), client="127.0.0.1", bot_id="b", action="stop",
                        result="error", detail=f"channel={FAKE_CHANNEL}")
    assert FAKE_CHANNEL not in log.read_text(encoding="utf-8")
    control.log_request(str(tmp_path / "missing" / "dir" / "log.csv"), client="x",
                        bot_id="b", action="stop", result="ok", detail="")   # no raise


# ── #212 review fixes ───────────────────────────────────────────────────

def test_stop_refused_when_position_changed_since_the_dialog(control_server):
    """The market-close warning came from a snapshot; a stale ack is refused."""
    url, base, _ = control_server
    real = base / "TMF00_real"
    _set_mode(real, "auto", position={"position_size": 1, "position_side": "LONG",
                                      "entry_price": 45000})
    status, body = _post(url, {"id": "TMF00_real", "action": "stop", "confirm": "real",
                               "position_ack": "flat"})
    assert status == 409 and "持倉" in body["message"]
    assert not (real / "STOP").exists()
    status, _ = _post(url, {"id": "TMF00_real", "action": "stop", "confirm": "real",
                            "position_ack": "LONG:1"})
    assert status == 200 and (real / "STOP").exists()


@pytest.mark.parametrize("cmdline,expect", [
    (r'"C:\x\tai_backtest.exe"', "GUI"),                    # GUI EXE, no deploy
    ("python run_backtest.py", "GUI"),
    (None, "無法確認"),                                        # unreadable
])
def test_stop_refused_for_bots_that_never_read_stop(control_server, monkeypatch,
                                                    cmdline, expect):
    from scripts.dashboard import control
    monkeypatch.setattr(control, "process_command_line", lambda pid: cmdline)
    url, base, _ = control_server
    status, body = _post(url, {"id": "TMF00_alpha", "action": "stop", "confirm": "alpha",
                               "position_ack": "flat"})
    assert status == 409 and expect in body["message"]
    assert not (base / "TMF00_alpha" / "STOP").exists()


@pytest.mark.parametrize("cmdline,headless", [
    (r'"C:\b\tai_robot_v2199\tai_backtest\tai_backtest.exe" deploy --symbol TMF00 --bot A', True),
    (r"C:\venv\python.exe run_bot_cli.py deploy --bot A --mode paper", True),
    (r'"C:\b\tai_backtest.exe"', False),
    ("python run_backtest.py", False),
    ("", None), (None, None),
])
def test_is_headless(cmdline, headless):
    from scripts.dashboard import control
    assert control.is_headless(cmdline) is headless


def test_hung_bot_is_refused(control_server):
    url, base, _ = control_server
    hb = base / "TMF00_alpha" / "heartbeat.json"
    data = json.loads(hb.read_text())
    data["ts"] -= 3600
    hb.write_text(json.dumps(data))
    status, body = _post(url, {"id": "TMF00_alpha", "action": "stop", "confirm": "alpha",
                               "position_ack": "flat"})
    assert status == 409 and "卡住" in body["message"]
    assert not (base / "TMF00_alpha" / "STOP").exists()


@pytest.mark.parametrize("origin,code", [
    ("http://localhost:5678", 403),      # same host, other port
    ("https://{host}", 403),             # other scheme
    ("null", 403),                       # sandboxed / file:// page
    ("http://{host}", 200),              # this page
])
def test_origin_must_be_exactly_this_page(control_server, origin, code):
    url, base, _ = control_server
    host = url.split("://", 1)[1]
    status, _ = _post(url, {"id": "TMF00_alpha", "action": "stop", "confirm": "alpha",
                            "position_ack": "flat"},
                      headers={"Origin": origin.format(host=host)})
    assert status == code
    assert (base / "TMF00_alpha" / "STOP").exists() is (code == 200)


def test_every_rejected_request_is_audited(control_server):
    url, _, log = control_server
    _post(url, {"id": "nope", "action": "stop", "confirm": "x"})                  # unknown bot
    _post(url, {"id": "TMF00_alpha", "action": "stop", "confirm": "alpha"},
          headers={"X-Dashboard-Control": ""})                                     # 403
    _post(url, None, raw=b"{bad")                                                  # 400
    results = [r["result"] for r in _log_rows(log)]
    assert results == ["refused", "forbidden", "bad request"]


def test_audit_log_neutralises_spreadsheet_formulas(tmp_path):
    from scripts.dashboard import control
    log = tmp_path / "log.csv"
    control.log_request(str(log), client="127.0.0.1", bot_id="=HYPERLINK(1)",
                        action="+cmd", result="refused", detail="@x")
    rows = _log_rows(log)
    assert rows[0]["bot"] == "'=HYPERLINK(1)" and rows[0]["action"] == "'+cmd"
    assert rows[0]["detail"] == "'@x"


def test_unwritable_audit_log_is_reported(tmp_path, capsys):
    from scripts.dashboard import control
    control.log_request(str(tmp_path), client="c", bot_id="b", action="stop",
                        result="accepted", detail="d")
    assert "control log" in capsys.readouterr().err
