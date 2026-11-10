"""Per-bot position log: the bot's own position from its own order seqs.

Data collection only. The account's OpenInterest book is one NET row per
product, so with a second order source on the same product the bot can no
longer see its own position there. ``bot_position_after`` is built purely
from fills of orders this bot sent; ``net_position_after`` is the account
book after the fill. These tests pin that the two agree for a bot alone on
its product and that a disagreement is detectable.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from scripts.dashboard import position_log_view, server
from src.live.position_log import (
    POSITION_LOG_NAME, PositionLedger, is_mismatch, last_record, read_records,
)

ACCT = "F0000000001234"          # placeholder, not a real account


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _ledger(tmp_path, clock=None, bot_id="TMF00_alpha"):
    return PositionLedger(str(tmp_path), bot_id=bot_id, account=lambda: ACCT,
                          now_iso=lambda: "2026-11-05T20:00:00+08:00",
                          monotonic=clock or _Clock())


def _log(tmp_path):
    return read_records(str(tmp_path / POSITION_LOG_NAME))


# ── synthetic position through a full cycle ────────────────────────────

def test_entry_then_exit_tracks_the_bots_own_position(tmp_path):
    led = _ledger(tmp_path)
    led.register_order("0000000000001", "entry", 0, 1, "TM0000")    # BUY
    rec = led.record_fill("0000000000001", 44774, source="deal", oi_generation=10)
    assert rec["bot_position_after"] == 1 and led.position == 1
    # The fill is durable immediately (issue #252). The account net is
    # still pending; it is not invented as 0.
    [pending] = _log(tmp_path)
    assert pending["action"] == "ENTRY" and pending["bot_position_after"] == 1
    assert pending["net_status"] == "pending" and pending["net_position_after"] is None
    assert "event" not in pending

    assert led.on_oi_complete(11, 1) == 1             # query started after the fill
    led.register_order("0000000000002", "exit", 1, 1, "TM0000")     # SELL
    led.record_fill("0000000000002", 44622, source="deal", oi_generation=12)
    led.on_oi_complete(13, 0)

    entry, exit_ = _log(tmp_path)
    assert entry == {
        "ts": "2026-11-05T20:00:00+08:00", "bot_id": "TMF00_alpha", "account": ACCT,
        "product": "TM0000", "order_seq": "0000000000001", "side": "BUY", "qty": 1,
        "fill_price": 44774, "price_source": "deal", "action": "ENTRY",
        "net_position_after": 1, "net_status": "answered", "bot_position_after": 1}
    assert (exit_["action"], exit_["side"], exit_["fill_price"]) == ("EXIT", "SELL", 44622)
    assert exit_["bot_position_after"] == 0 and exit_["net_position_after"] == 0
    assert not any(is_mismatch(r) for r in (entry, exit_))


def test_short_cycle_goes_negative_then_flat(tmp_path):
    led = _ledger(tmp_path)
    led.register_order("s1", "entry", 1, 1, "TM0000")
    led.record_fill("s1", 44000, source="deal", oi_generation=1)
    assert led.position == -1
    led.register_order("s2", "exit", 0, 1, "TM0000")
    led.record_fill("s2", 43900, source="deal", oi_generation=1)
    assert led.position == 0
    led.on_oi_complete(2, 0)
    assert [r["bot_position_after"] for r in _log(tmp_path)] == [-1, 0]


def test_each_order_seq_counts_once(tmp_path):
    led = _ledger(tmp_path)
    led.register_order("a", "entry", 0, 1, "TM0000")
    led.record_fill("a", 100, source="deal", oi_generation=1)
    assert led.record_fill("a", 100, source="deal", oi_generation=1) is None
    assert led.confirm_latest("entry", 100, source="fill_poller", oi_generation=1) is None
    assert led.position == 1


def test_poller_fallback_counts_a_fill_whose_deal_row_never_came(tmp_path):
    led = _ledger(tmp_path)
    led.register_order("a", "entry", 0, 1, "TM0000")
    rec = led.confirm_latest("entry", 101, source="fill_poller", oi_generation=1)
    assert rec["price_source"] == "fill_poller" and led.position == 1
    # The deal row lands while the record still waits for a net: price
    # upgraded, position NOT counted twice.
    assert led.record_fill("a", 105, source="deal", oi_generation=1) is None
    led.on_oi_complete(2, 1)
    [r] = _log(tmp_path)
    assert (r["fill_price"], r["price_source"], r["bot_position_after"]) == (105, "deal", 1)


def test_fills_of_orders_this_bot_did_not_send_are_ignored(tmp_path):
    led = _ledger(tmp_path)
    assert led.record_fill("someone-else", 100, source="deal", oi_generation=1) is None
    assert led.position == 0


def test_a_snapshot_queried_before_the_fill_does_not_supply_the_net(tmp_path):
    led = _ledger(tmp_path)
    led.register_order("a", "entry", 0, 1, "TM0000")
    led.record_fill("a", 100, source="deal", oi_generation=5)
    assert led.on_oi_complete(5, 0) == 0              # same query: pre-fill book
    assert led.on_oi_complete(4, 0) == 0
    assert led.on_oi_complete(6, 1) == 1


def test_no_net_within_the_timeout_is_written_as_unanswered(tmp_path):
    clock = _Clock()
    led = _ledger(tmp_path, clock)
    led.register_order("a", "entry", 0, 1, "TM0000")
    led.record_fill("a", 100, source="deal", oi_generation=1)
    clock.t = 119
    assert led.flush_expired() == 0
    clock.t = 120
    assert led.flush_expired() == 1
    [r] = _log(tmp_path)
    assert r["net_position_after"] is None and r["net_status"] == "unanswered"
    assert not is_mismatch(r)                         # unknown is not a mismatch


def test_stop_writes_whatever_is_still_pending(tmp_path):
    led = _ledger(tmp_path)
    led.register_order("a", "exit", 1, 1, "TM0000")
    led.record_fill("a", 100, source="deal", oi_generation=1)
    assert led.flush_all() == 1 and len(_log(tmp_path)) == 1


# ── mismatch: the point of the field ───────────────────────────────────

def test_second_bot_on_the_same_product_shows_as_a_mismatch(tmp_path):
    """Bot A goes long 1 while bot B is short 1 on the same account: the
    account net reads 0 after A's fill, A's own position reads +1."""
    a = _ledger(tmp_path / "A", bot_id="TMF00_long")
    a.register_order("A1", "entry", 0, 1, "TM0000")
    a.record_fill("A1", 100, source="deal", oi_generation=1)
    a.on_oi_complete(2, 0)                            # B's short nets it away
    [r] = _log(tmp_path / "A")
    assert r["bot_position_after"] == 1 and r["net_position_after"] == 0
    assert is_mismatch(r)


def test_manual_order_between_fills_is_detectable(tmp_path):
    led = _ledger(tmp_path)
    led.register_order("a", "entry", 0, 1, "TM0000")
    led.record_fill("a", 100, source="deal", oi_generation=1)
    led.on_oi_complete(2, 2)                          # someone else bought one more
    assert is_mismatch(_log(tmp_path)[0])


# ── deploy / resume ────────────────────────────────────────────────────

def test_fresh_deploy_starts_flat(tmp_path):
    """A new ledger is flat, including when the directory already has a journal.

    Explicit resume() is what loads that journal. A plain construct does
    not, matching a non-resume deploy on 49268a0.
    """
    led = _ledger(tmp_path)
    assert led.position == 0
    assert not (tmp_path / POSITION_LOG_NAME).exists()
    led.register_order("a", "entry", 0, 1, "TM0000")
    led.record_fill("a", 100, source="deal", oi_generation=1)
    led.flush_all()
    fresh = _ledger(tmp_path)
    assert fresh.position == 0
    assert fresh._orders == {} and fresh._pending == [] and fresh._applied == set()


def test_clean_restart_restores_from_the_last_record_and_logs_resume(tmp_path):
    led = _ledger(tmp_path)
    led.register_order("a", "entry", 1, 1, "TM0000")
    led.record_fill("a", 100, source="deal", oi_generation=1)
    led.on_oi_complete(2, -1)

    again = _ledger(tmp_path)
    rec = again.resume(net=-1, product="TM0000", fill_price=100)
    assert again.position == -1
    assert rec["action"] == "RESUME" and rec["bot_position_after"] == -1
    assert last_record(str(tmp_path / POSITION_LOG_NAME))["action"] == "RESUME"
    assert not is_mismatch(rec)
    # A RESUME whose account book disagrees is flagged too.
    assert is_mismatch(_ledger(tmp_path).resume(net=0, product="TM0000"))


def test_resume_with_no_log_starts_flat(tmp_path):
    rec = _ledger(tmp_path).resume(net=None, product="TM0000")
    assert rec["bot_position_after"] == 0 and rec["net_status"] == "unanswered"


def test_a_write_failure_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    led = PositionLedger(str(blocker / "bot"), bot_id="x")
    led.register_order("a", "entry", 0, 1, "TM0000")
    led.record_fill("a", 1, source="deal", oi_generation=1)
    assert led.flush_all() == 0


def test_app_glue_swallows_ledger_errors():
    """The run_backtest hook can never raise into the trading path."""
    import run_backtest

    class _Boom:
        def record_fill(self, *a, **k):
            raise RuntimeError("disk gone")
    app = SimpleNamespace(_position_ledger=_Boom())
    assert run_backtest.BacktestApp._position_log(app, "record_fill", "x") is None
    assert run_backtest.BacktestApp._position_log(
        SimpleNamespace(_position_ledger=None), "flush_all") is None


# ── dashboard endpoint ─────────────────────────────────────────────────

def test_view_masks_the_account_and_counts_mismatches(tmp_path):
    led = _ledger(tmp_path)
    led.register_order("a", "entry", 0, 1, "TM0000")
    led.record_fill("a", 100, source="deal", oi_generation=1)
    led.on_oi_complete(2, 0)
    out = position_log_view.position_log(str(tmp_path), "TMF00_alpha")
    assert out["records"][0]["account"] == "**********1234"
    assert ACCT not in json.dumps(out)
    assert out["mismatch_count"] == 1 and out["bot_position"] == 1 and out["net_position"] == 0


@pytest.fixture
def poslog_server(tmp_path, monkeypatch):
    bot = tmp_path / "TMF00_alpha"
    bot.mkdir()
    (bot / "session.json").write_text("{}", encoding="utf-8")
    led = _ledger(bot)
    for seq, side, action in (("1", 0, "entry"), ("2", 1, "exit")):
        led.register_order(seq, action, side, 1, "TM0000")
        led.record_fill(seq, 100, source="deal", oi_generation=1)
    led.on_oi_complete(2, 0)
    monkeypatch.setattr(server.DashboardHandler, "base_dir", str(tmp_path))
    monkeypatch.setattr(server.DashboardHandler, "cache", server._Cache(0))
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_endpoint_returns_the_log(poslog_server):
    with urllib.request.urlopen(poslog_server + "/position-log/TMF00_alpha", timeout=10) as r:
        body = json.loads(r.read())
    assert [x["action"] for x in body["records"]] == ["ENTRY", "EXIT"]
    assert [x["bot_position_after"] for x in body["records"]] == [1, 0]
    assert body["mismatch_count"] == 1                # entry: own +1 vs net 0
    assert ACCT not in json.dumps(body)
    with urllib.request.urlopen(poslog_server + "/position-log/TMF00_alpha?limit=1",
                                timeout=10) as r:
        assert [x["action"] for x in json.loads(r.read())["records"]] == ["EXIT"]


@pytest.mark.parametrize("path, code", [
    ("/position-log/nope", 404), ("/position-log/", 404),
    ("/position-log/..%2F..%2Fsettings", 404), ("/position-log/TMF00_alpha?limit=x", 400),
])
def test_endpoint_rejects(poslog_server, path, code):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(poslog_server + path, timeout=10)
    assert exc.value.code == code


def test_on_new_data_glue_feeds_the_ledger(tmp_path):
    """The real OnNewData handler counts our deal row in the ledger, even
    when the broker's per-trade guard rejects it as a late fill."""
    import run_backtest
    from unittest.mock import MagicMock

    led = _ledger(tmp_path)
    led.register_order("0000000000007", "entry", 0, 1, "TM0000")
    fill = SimpleNamespace(seq_no="0000000000007", price=44774, qty=1,
                           action_type="entry", bar_index=3, sim_price=44770)
    broker = MagicMock()
    broker.try_set_real_entry_price.return_value = False      # late fill
    app = SimpleNamespace(
        _futures_account=ACCT, _position_ledger=led,
        _fill_tracker=SimpleNamespace(on_new_data=lambda raw: fill),
        _live_runner=SimpleNamespace(broker=broker),
        _account_monitor=SimpleNamespace(oi_query_generation=40),
        _exit_fill_unresolved=lambda: False, _real_fill_count=0,
        real_fills_var=MagicMock(), _live_log_msg=MagicMock(),
        _log_order_decision=MagicMock(),
        _trading_guard=SimpleNamespace(fill_pending=False))
    app._position_log = lambda *a, **k: run_backtest.BacktestApp._position_log(app, *a, **k)

    run_backtest.BacktestApp._on_new_data(app, "raw-row")
    assert led.position == 1
    led.on_oi_complete(41, 1)
    [r] = _log(tmp_path)
    assert (r["order_seq"], r["fill_price"], r["net_position_after"]) == ("0000000000007", 44774, 1)


# ── issue #252: crash-durable fills and accepted orders ────────────────
#
# These tests are the P1-1 / P1-2 losses from #251's review of #250.
# They use temp directories only. No production loss is claimed.

def _events(tmp_path):
    """Raw journal lines, oldest first. Empty when nothing was written."""
    path = tmp_path / POSITION_LOG_NAME
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _reopen(tmp_path, clock=None):
    """A new ledger on the same directory: the process was destroyed."""
    return _ledger(tmp_path, clock)


def test_issue_252_fill_is_durable_before_oi_and_restores_once(tmp_path):
    """P1-1: a fill written before the OI wait survives process death.

    BUY 1 and SELL 1 each come back exactly once. A clean RESUME appends
    a RESUME line and does not count the fill again. The net stays
    pending until a correlated answer, and that answer is not a second fill.
    """
    led = _ledger(tmp_path)
    led.register_order("b1", "entry", 0, 1, "TM0000")
    led.record_fill("b1", 44774, source="deal", oi_generation=10)
    # Destroyed with no stop and no flush. The fill is already on disk.
    raw = _events(tmp_path)
    fills = [e for e in raw if e.get("event") == "FILL"]
    assert len(fills) == 1
    assert fills[0]["net_status"] == "pending"
    assert fills[0]["net_position_after"] is None
    assert fills[0]["side"] == "BUY" and fills[0]["qty"] == 1
    assert fills[0]["bot_position_after"] == 1

    again = _reopen(tmp_path)
    assert again.position == 0
    rec = again.resume(net=None, product="TM0000")
    assert again.position == 1
    assert rec["action"] == "RESUME" and rec["bot_position_after"] == 1
    assert rec["net_position_after"] is None
    # The same seq must not count a second time, including via the poller.
    assert again.record_fill("b1", 44774, source="deal", oi_generation=10) is None
    assert again.confirm_latest("entry", 44774, source="fill_poller", oi_generation=10) is None
    assert again.position == 1
    logical = _log(tmp_path)
    entries = [r for r in logical if r["action"] == "ENTRY"]
    assert len(entries) == 1 and entries[0]["qty"] == 1
    assert [r["action"] for r in logical].count("RESUME") == 1

    # Correlated answer after the reopen. Still one fill, position unchanged.
    assert again.on_oi_complete(11, 1) == 1
    assert again.position == 1
    [entry] = [r for r in _log(tmp_path) if r["action"] == "ENTRY"]
    assert entry["net_status"] == "answered" and entry["net_position_after"] == 1
    assert entry["bot_position_after"] == 1
    assert sum(1 for e in _events(tmp_path) if e.get("event") == "FILL") == 1
    nets = [e for e in _events(tmp_path) if e.get("event") == "NET_UPDATE"]
    assert len(nets) == 1 and nets[0]["order_seq"] == "b1"

    # A separate SELL 1 survives the same destroy/reopen, exactly once.
    sell_dir = tmp_path / "sell"
    sell_dir.mkdir()
    short = _ledger(sell_dir)
    short.register_order("s1", "entry", 1, 1, "TM0000")
    short.record_fill("s1", 44000, source="deal", oi_generation=3)
    restored = _reopen(sell_dir)
    assert restored.position == 0
    restored.resume(net=None, product="TM0000")
    assert restored.position == -1
    assert restored.record_fill("s1", 44000, source="deal", oi_generation=3) is None
    assert restored.position == -1
    [row] = [r for r in _log(sell_dir) if r["action"] == "ENTRY"]
    assert row["side"] == "SELL" and row["qty"] == 1 and row["bot_position_after"] == -1


def test_issue_252_timeout_and_stop_flush_stay_unknown(tmp_path):
    """Pending, then a correlated timeout or stop. Unknown is not 0 or an average."""
    clock = _Clock()
    led = _ledger(tmp_path / "timeout", clock)
    led.register_order("a", "entry", 0, 1, "TM0000")
    led.record_fill("a", 100, source="deal", oi_generation=1)
    led.register_order("b", "entry", 0, 1, "TM0000")
    led.record_fill("b", 300, source="deal", oi_generation=1)
    clock.t = 120
    assert led.flush_expired() == 2
    rows = [r for r in _log(tmp_path / "timeout") if r["action"] == "ENTRY"]
    assert [r["net_status"] for r in rows] == ["unanswered", "unanswered"]
    assert all(r["net_position_after"] is None for r in rows)
    assert all(r["net_position_after"] != 0 for r in rows)
    # Two prices must not be collapsed to their average.
    assert sorted(r["fill_price"] for r in rows) == [100, 300]
    raw_timeout = _events(tmp_path / "timeout")
    assert sum(1 for e in raw_timeout if e.get("event") == "FILL") == 2
    assert sum(1 for e in raw_timeout if e.get("event") == "NET_UPDATE") == 2

    stop = _ledger(tmp_path / "stop")
    stop.register_order("c", "exit", 1, 1, "TM0000")
    stop.record_fill("c", 90, source="deal", oi_generation=4)
    assert stop.flush_all() == 1
    [row] = [r for r in _log(tmp_path / "stop") if r["action"] == "EXIT"]
    assert row["net_status"] == "flush_unknown"
    assert row["net_position_after"] is None
    assert not is_mismatch(row)
    assert sum(1 for e in _events(tmp_path / "stop") if e.get("event") == "FILL") == 1


def test_issue_252_unfilled_orders_restore_and_order_alone_does_not_fill(tmp_path):
    """P1-2: accepted entry and exit survive restart; ORDER itself is not a fill."""
    led = _ledger(tmp_path)
    led.register_order("e1", "entry", 0, 1, "TM0000", oi_generation=8)
    led.register_order("x1", "exit", 1, 2, "TXFD0", oi_generation=8)
    assert led.position == 0
    orders = [e for e in _events(tmp_path) if e.get("event") == "ORDER"]
    assert [(o["order_seq"], o["action"], o["side"], o["qty"], o["product"]) for o in orders] == [
        ("e1", "ENTRY", "BUY", 1, "TM0000"),
        ("x1", "EXIT", "SELL", 2, "TXFD0"),
    ]
    assert all(o.get("oi_generation") == 8 for o in orders)

    again = _reopen(tmp_path)
    assert again.position == 0
    assert again.confirm_latest("entry", 101, source="fill_poller", oi_generation=1) is None
    again.resume(net=None, product="TM0000")
    assert again.position == 0
    assert [r for r in _log(tmp_path) if r["action"] in ("ENTRY", "EXIT")] == []
    # Account net must not invent an own fill for an order that never filled.
    assert again.on_oi_complete(99, 5) == 0
    assert again.position == 0
    assert [r for r in _log(tmp_path) if r["action"] in ("ENTRY", "EXIT")] == []

    entry = again.confirm_latest("entry", 101, source="fill_poller", oi_generation=8)
    assert entry["order_seq"] == "e1"
    assert (entry["action"], entry["side"], entry["qty"], entry["product"]) == (
        "ENTRY", "BUY", 1, "TM0000")
    assert again.position == 1
    exit_ = again.record_fill("x1", 90, source="deal", oi_generation=9, qty=2)
    assert exit_["order_seq"] == "x1"
    assert (exit_["action"], exit_["side"], exit_["qty"], exit_["product"]) == (
        "EXIT", "SELL", 2, "TXFD0")
    assert again.position == -1


def test_issue_252_restart_keeps_once_only_and_pending_deal_refine(tmp_path):
    """Duplicate seq / poll fallback still count once. A late deal upgrades only that fill."""
    led = _ledger(tmp_path)
    led.register_order("a", "entry", 0, 1, "TM0000")
    # Poller counts A. Registering B afterwards must not steal that seq.
    led.confirm_latest("entry", 101, source="fill_poller", oi_generation=4)
    # Deal lands while OI is still open: price upgrade, not a second lot.
    assert led.record_fill("a", 105, source="deal", oi_generation=4) is None
    led.register_order("b", "entry", 0, 1, "TXFD0")
    led.record_fill("b", 50, source="deal", oi_generation=4)
    assert led.position == 2
    assert led.on_oi_complete(4, 9) == 0          # same process: query is not newer
    assert led.position == 2

    again = _reopen(tmp_path)
    assert again.position == 0
    again.resume(net=None, product="TM0000")
    assert again.position == 2
    assert again.record_fill("a", 999, source="deal", oi_generation=4) is None
    assert again.confirm_latest("entry", 999, source="fill_poller", oi_generation=4) is None
    assert again.position == 2
    logical = {r["order_seq"]: r for r in _log(tmp_path)}
    assert (logical["a"]["fill_price"], logical["a"]["price_source"]) == (105, "deal")
    assert (logical["b"]["fill_price"], logical["b"]["price_source"]) == (50, "deal")
    # The other fill is untouched by A's late deal.
    assert logical["b"]["bot_position_after"] == 2

    # The stored generation is from the previous process. The first snapshot
    # in this one answers both pending fills and does not move position.
    assert again.on_oi_complete(1, 2) == 2
    assert again.position == 2
    answered = {r["order_seq"]: r for r in _log(tmp_path)}
    assert answered["a"]["net_status"] == "answered" and answered["a"]["net_position_after"] == 2
    assert answered["a"]["fill_price"] == 105
    assert answered["b"]["fill_price"] == 50
    assert sum(1 for e in _events(tmp_path) if e.get("event") == "FILL") == 2


def test_issue_252_completed_seq_does_not_count_again_after_restart(tmp_path):
    led = _ledger(tmp_path)
    led.register_order("a", "entry", 0, 1, "TM0000")
    led.record_fill("a", 100, source="deal", oi_generation=1)
    led.on_oi_complete(2, 1)
    again = _reopen(tmp_path)
    assert again.position == 0
    again.resume(net=1, product="TM0000")
    assert again.position == 1
    assert again.record_fill("a", 100, source="deal", oi_generation=3) is None
    assert again.confirm_latest("entry", 100, source="fill_poller", oi_generation=3) is None
    assert again.position == 1
    assert again.on_oi_complete(4, 1) == 0
    [row] = [r for r in _log(tmp_path) if r["action"] == "ENTRY"]
    assert row["fill_price"] == 100 and row["net_status"] == "answered"


_LEGACY = [
    {
        "ts": "2026-11-01T08:00:00+08:00", "bot_id": "TMF00_alpha", "account": ACCT,
        "product": "TM0000", "order_seq": "leg-buy", "side": "BUY", "qty": 1,
        "fill_price": 44774, "price_source": "deal", "action": "ENTRY",
        "net_position_after": 1, "net_status": "answered", "bot_position_after": 1,
    },
    {
        "ts": "2026-11-01T08:05:00+08:00", "bot_id": "TMF00_alpha", "account": ACCT,
        "product": "TM0000", "order_seq": "leg-sell", "side": "SELL", "qty": 1,
        "fill_price": 44622, "price_source": "deal", "action": "EXIT",
        "net_position_after": 0, "net_status": "answered", "bot_position_after": 0,
    },
    {
        "ts": "2026-11-01T08:06:00+08:00", "bot_id": "TMF00_alpha", "account": ACCT,
        "product": "TM0000", "order_seq": "", "side": "", "qty": 0,
        "fill_price": 44622, "price_source": "", "action": "RESUME",
        "net_position_after": 0, "net_status": "answered", "bot_position_after": 0,
    },
]


def _write_legacy(tmp_path):
    path = tmp_path / POSITION_LOG_NAME
    text = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for row in _LEGACY)
    path.write_text(text, encoding="utf-8")
    return text


def test_issue_252_legacy_finalized_lines_keep_position_and_are_not_rewritten(tmp_path):
    """Pre-change ENTRY/EXIT/RESUME lines replay to the same position and net.

    Opening the ledger must not invent ORDER events or rewrite the file.
    """
    original = _write_legacy(tmp_path)
    assert _log(tmp_path) == _LEGACY
    assert all(row.get("event") != "ORDER" for row in _events(tmp_path))
    plain = _reopen(tmp_path)
    assert plain.position == 0 and plain._applied == set()
    assert (tmp_path / POSITION_LOG_NAME).read_text(encoding="utf-8") == original
    led = _reopen(tmp_path)
    led.resume(net=0, product="TM0000", fill_price=44622)
    assert led.position == 0
    # Both finalized seqs are already counted, so a replayed deal cannot add a lot.
    assert led._applied >= {"leg-buy", "leg-sell"}
    assert _log(tmp_path)[:3] == _LEGACY
    assert led.record_fill("leg-buy", 44774, source="deal", oi_generation=1) is None
    assert led.position == 0
    # A journal that ends long restores +1 on resume, not on a plain open.
    long_dir = tmp_path / "long"
    long_dir.mkdir()
    (long_dir / POSITION_LOG_NAME).write_text(
        json.dumps(_LEGACY[0], ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8")
    assert _reopen(long_dir).position == 0
    long = _reopen(long_dir)
    long.resume(net=1, product="TM0000")
    assert long.position == 1
    # A later explicit resume appends; it does not rewrite the history.
    assert (tmp_path / POSITION_LOG_NAME).read_text(encoding="utf-8").startswith(original)
    assert '"event":"ORDER"' not in (tmp_path / POSITION_LOG_NAME).read_text(encoding="utf-8")
    historical = _log(tmp_path)[:3]
    assert historical == _LEGACY


def test_issue_252_dashboard_projects_logical_fills_not_the_account_net(tmp_path):
    """The reader shows the bot's own fill, the answered net, and the public names.

    A contradictory bot_position on the net line must not win. A pending-only
    line and a malformed line stay sensible. Masking and the tail bound hold.
    """
    bot = tmp_path / "bot"
    bot.mkdir()
    path = bot / POSITION_LOG_NAME
    pending = {
        "event": "FILL", "ts": "2026-11-05T20:00:00+08:00", "bot_id": "TMF00_alpha",
        "account": ACCT, "product": "TM0000", "order_seq": "p1", "side": "BUY",
        "qty": 1, "fill_price": 100, "price_source": "deal", "action": "ENTRY",
        "net_position_after": None, "net_status": "pending", "bot_position_after": 1,
        "oi_generation": 3,
    }
    path.write_text("this is not json\n" + json.dumps(pending) + "\n", encoding="utf-8")
    out = position_log_view.position_log(str(bot), "TMF00_alpha")
    assert out["bot_position"] == 1
    assert out["net_position"] is None
    assert out["mismatch_count"] == 0
    [rec] = out["records"]
    assert rec["net_status"] == "pending" and rec["bot_position_after"] == 1
    assert rec["account"] == "**********1234"
    assert "event" not in rec and "oi_generation" not in rec
    assert ACCT not in json.dumps(out)

    path.write_text(
        json.dumps(pending) + "\n"
        + json.dumps({
            "event": "NET_UPDATE", "ts": "2026-11-05T20:00:00+08:00",
            "bot_id": "TMF00_alpha", "account": ACCT, "product": "TM0000",
            "order_seq": "p1", "side": "BUY", "qty": 1, "fill_price": 100,
            "price_source": "deal", "action": "ENTRY",
            "net_position_after": 5, "net_status": "answered",
            # A bad net line must not replace the fill's own position.
            "bot_position_after": 99,
        }) + "\n",
        encoding="utf-8")
    out = position_log_view.position_log(str(bot), "TMF00_alpha")
    [rec] = out["records"]
    assert rec["bot_position_after"] == 1 and rec["net_position_after"] == 5
    assert rec["net_status"] == "answered" and rec["ts"] == "2026-11-05T20:00:00+08:00"
    assert out["bot_position"] == 1 and out["net_position"] == 5
    assert set(rec) == {
        "ts", "bot_id", "account", "product", "order_seq", "side", "qty",
        "fill_price", "price_source", "action", "net_position_after",
        "net_status", "bot_position_after", "mismatch",
    }
    # limit is applied to logical rows, so this fill and its net are one record.
    out = position_log_view.position_log(str(bot), "TMF00_alpha", limit=1)
    assert len(out["records"]) == 1
    assert out["records"][0]["order_seq"] == "p1"
    assert out["records"][0]["account"] == "**********1234"


def test_issue_252_replay_skips_damaged_rows_and_a_failed_append_is_not_kept(tmp_path, caplog):
    """A mixed/damaged journal keeps the valid rows.

    A failed append is logged and still moves this process's book. A
    restart with nothing durable does not invent that fill.
    """
    path = tmp_path / POSITION_LOG_NAME
    good = dict(_LEGACY[0])
    path.write_text(
        "not-json\n"
        + json.dumps(["nope"]) + "\n"
        + json.dumps(good) + "\n"
        + "{truncated\n"
        + json.dumps({
            "event": "ORDER", "order_seq": "only-order", "action": "ENTRY",
            "action_type": "entry", "side": "BUY", "qty": 1, "product": "TM0000",
        }) + "\n",
        encoding="utf-8")
    plain = _reopen(tmp_path)
    assert plain.position == 0
    led = _reopen(tmp_path)
    led.resume(net=None, product="TM0000")
    assert led.position == 1
    assert led.record_fill("leg-buy", 1, source="deal", oi_generation=1) is None
    assert led.position == 1
    # The bare ORDER is not a fill. Nothing here raises into the caller.
    assert led.on_oi_complete(3, 9) == 0
    assert led.position == 1

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    with caplog.at_level("WARNING"):
        broken = PositionLedger(str(blocker / "bot"), bot_id="x", account=lambda: ACCT)
        broken.register_order("a", "entry", 0, 1, "TM0000")
        missed = broken.record_fill("a", 100, source="deal", oi_generation=1)
    assert missed is not None and missed["bot_position_after"] == 1
    assert broken.position == 1 and "a" in broken._orders
    assert "position log write failed" in caplog.text
    reopened = PositionLedger(str(blocker / "bot"), bot_id="x", account=lambda: ACCT)
    assert reopened.position == 0 and reopened._orders == {}
    assert not (blocker / "bot" / POSITION_LOG_NAME).exists()
    assert ACCT not in caplog.text


def test_issue_252_accepted_order_glue_survives_restart_into_resume_confirm(tmp_path, monkeypatch):
    """The real accept path persists ORDER. After restart the resumed confirm
    and a credential-free OnNewData stub both use that seq.
    """
    import sys
    import types
    from unittest.mock import MagicMock

    import run_backtest as rb
    from src.live.fill_poller import FillPoller
    from src.live.trading_guard import TradingGuard

    bot = tmp_path / "TMF00_alpha"
    bot.mkdir()
    led = _ledger(bot)
    seqs = ["2520000000001", "2520000000002"]

    def send(*_args, **_kwargs):
        return (seqs.pop(0), 0)

    sk_mod = types.ModuleType("comtypes.gen.SKCOMLib")

    class _Order:
        pass

    sk_mod.FUTUREORDER = _Order
    gen = types.ModuleType("comtypes.gen")
    comtypes_mod = types.ModuleType("comtypes")
    comtypes_mod.gen = gen
    monkeypatch.setitem(sys.modules, "comtypes", comtypes_mod)
    monkeypatch.setitem(sys.modules, "comtypes.gen", gen)
    monkeypatch.setitem(sys.modules, "comtypes.gen.SKCOMLib", sk_mod)
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(SendFutureOrderCLR=send))
    monkeypatch.setattr(rb, "_discord", None)

    class _Var:
        def __init__(self, value=""):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class _Log:
        def __init__(self):
            self.lines = []

        def append(self, line, tag="status"):
            self.lines.append(line)

    class _Root:
        def __init__(self):
            self.pending = {}
            self._next = 1

        def after(self, _ms, fn, *args):
            token = self._next
            self._next += 1
            self.pending[token] = (fn, args)
            return token

        def after_cancel(self, token):
            self.pending.pop(token, None)

    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = _Root()
    app.live_log = _Log()
    app.login_user_var = _Var("user")
    app._futures_account = ACCT
    app._trading_mode = "semi_auto"
    app._trading_guard = TradingGuard()
    app._equity_refresh_owed = False
    app._reply_poller = None
    app._position_ledger = led
    app._account_monitor = SimpleNamespace(rights=None, oi_query_generation=11, positions=[])
    app._fill_tracker = SimpleNamespace(register_order=lambda *_a, **_k: None)
    app._live_runner = SimpleNamespace(
        symbol="TMF00", csv_logger=None, strategy_display_name="Probe",
        broker=SimpleNamespace(
            entry_bar_index=3,
            trades=[SimpleNamespace(exit_bar_index=4)],
        ),
    )
    assert app._send_real_order(0, "TM0000", "entry", 44770) is True
    assert app._send_real_order(1, "TM0000", "exit", 44600) is True
    orders = [e for e in _events(bot) if e.get("event") == "ORDER"]
    assert [(o["order_seq"], o["action"], o["side"], o["qty"], o["product"]) for o in orders] == [
        ("2520000000001", "ENTRY", "BUY", 1, "TM0000"),
        ("2520000000002", "EXIT", "SELL", 1, "TM0000"),
    ]
    assert all(o.get("oi_generation") == 11 for o in orders)

    # Process death. Plain startup does not adopt the orders. Explicit
    # resume does, and the resumed confirm then uses that seq.
    plain = _reopen(bot)
    assert plain.position == 0
    assert plain.confirm_latest("entry", 101, source="fill_poller", oi_generation=1) is None
    restored = _reopen(bot)
    restored.resume(net=None, product="TM0000")
    assert restored.position == 0
    app._position_ledger = restored
    guard = TradingGuard()
    guard.on_fill_pending("entry")
    poller = FillPoller(guard)
    poller.start("entry", 0)
    app._trading_guard = guard
    app._fill_poller = poller
    app._live_runner.broker.real_entry_price = 0
    app._on_fill_confirmed()
    assert restored.position == 1
    fills = [e for e in _events(bot) if e.get("event") == "FILL"]
    assert len(fills) == 1 and fills[0]["order_seq"] == "2520000000001"

    raw = "stub-deal-row"
    assert ACCT not in raw and "password" not in raw.lower()
    fill = SimpleNamespace(
        seq_no="2520000000002", price=44622, qty=1, action_type="exit",
        bar_index=4, sim_price=44600)
    app._fill_tracker = SimpleNamespace(on_new_data=lambda _raw: fill)
    app._real_fill_count = 0
    app.real_fills_var = MagicMock()
    app._exit_fill_unresolved = lambda: False
    app._live_runner.broker.try_set_real_exit_price = lambda *_a, **_k: False
    app._on_new_data(raw)
    assert restored.position == 0
    exit_fills = [e for e in _events(bot) if e.get("event") == "FILL" and e["order_seq"] == "2520000000002"]
    assert len(exit_fills) == 1
    assert exit_fills[0]["side"] == "SELL" and exit_fills[0]["action"] == "EXIT"


# ── issue #254: dashboard limit is logical rows, not a raw-event tail ──

def _view(tmp_path, limit=None):
    if limit is None:
        return position_log_view.position_log(str(tmp_path), "TMF00_alpha")
    return position_log_view.position_log(str(tmp_path), "TMF00_alpha", limit=limit)


def test_issue_254_order_only_raw_tail_still_shows_the_pending_fill(tmp_path):
    """TY repro: limit=2 used to read only the last two ORDER lines and hide s1.

    Orders alone still must not become a fill or a position.
    """
    led = _ledger(tmp_path)
    led.register_order("s1", "entry", 0, 1, "TM0000")
    led.register_order("s2", "exit", 1, 1, "TM0000")
    led.register_order("s3", "entry", 0, 1, "TXFD0")
    bare = _view(tmp_path, 2)
    assert bare["records"] == []
    assert bare["bot_position"] is None and bare["net_position"] is None

    led2 = _ledger(tmp_path / "filled")
    led2.register_order("s1", "entry", 0, 1, "TM0000")
    led2.record_fill("s1", 44774, source="deal", oi_generation=4)
    led2.register_order("s2", "exit", 1, 1, "TM0000")
    led2.register_order("s3", "entry", 0, 1, "TXFD0")
    out = _view(tmp_path / "filled", 2)
    assert out["bot_position"] == 1
    assert out["net_position"] is None
    [rec] = out["records"]
    assert rec["order_seq"] == "s1"
    assert rec["action"] == "ENTRY" and rec["side"] == "BUY" and rec["qty"] == 1
    assert rec["product"] == "TM0000"
    assert rec["net_status"] == "pending" and rec["net_position_after"] is None
    assert rec["bot_position_after"] == 1 and rec["fill_price"] == 44774
    assert rec["account"] == "**********1234"
    dumped = json.dumps(out)
    assert ACCT not in dumped
    assert "TXFD0" not in dumped and '"s2"' not in dumped and '"s3"' not in dumped


def test_issue_254_limits_are_logical_rows_across_products(tmp_path):
    """limit 1, 2, and the default keep logical fills, not the raw ORDER tail.

    A net line cannot move another product's fill, and the account mask
    stays the last four digits.
    """
    led = _ledger(tmp_path)
    led.register_order("tm", "entry", 0, 1, "TM0000")
    led.confirm_latest("entry", 101, source="fill_poller", oi_generation=2)
    assert led.record_fill("tm", 105, source="deal", oi_generation=2) is None
    led.register_order("tx", "entry", 1, 1, "TXFD0")
    led.record_fill("tx", 50, source="deal", oi_generation=2)
    led.on_oi_complete(3, 1)                 # answers both; same generation
    # Hostile follow-up must not be required: trailing orders are the tail.
    led.register_order("tail-a", "exit", 0, 1, "TXFD0")
    led.register_order("tail-b", "entry", 0, 1, "TM0000")

    one = _view(tmp_path, 1)
    assert [r["order_seq"] for r in one["records"]] == ["tx"]
    assert one["records"][0]["product"] == "TXFD0"
    assert one["records"][0]["side"] == "SELL"
    assert one["records"][0]["fill_price"] == 50
    assert one["bot_position"] == 0
    assert one["records"][0]["bot_position_after"] == 0
    assert "TM0000" not in json.dumps(one["records"])

    two = _view(tmp_path, 2)
    assert [(r["order_seq"], r["product"], r["fill_price"], r["bot_position_after"])
            for r in two["records"]] == [
        ("tm", "TM0000", 105, 1),
        ("tx", "TXFD0", 50, 0),
    ]
    assert two["records"][0]["price_source"] == "deal"
    assert two["records"][0]["net_status"] == "answered"
    assert two["records"][0]["net_position_after"] == 1
    assert two["records"][1]["net_status"] == "answered"
    assert two["bot_position"] == 0 and two["net_position"] == 1
    assert two["records"][0]["account"] == position_log_view.mask_account(ACCT)
    assert ACCT not in json.dumps(two)
    assert "tail-a" not in json.dumps(two) and "tail-b" not in json.dumps(two)

    full = _view(tmp_path)
    assert [r["order_seq"] for r in full["records"]] == ["tm", "tx"]
    assert full["count"] == 2


def test_issue_254_default_limit_ignores_a_long_order_tail(tmp_path):
    """More trailing ORDER lines than the default raw cap must not drop the fill."""
    led = _ledger(tmp_path)
    led.register_order("s1", "entry", 0, 1, "TM0000")
    led.record_fill("s1", 100, source="deal", oi_generation=1)
    for i in range(position_log_view.DEFAULT_LIMIT + 1):
        led.register_order(f"o{i}", "entry", 0, 1, "TXFD0")
    out = _view(tmp_path)
    assert [r["order_seq"] for r in out["records"]] == ["s1"]
    assert out["bot_position"] == 1
    assert out["records"][0]["net_status"] == "pending"
    assert out["records"][0]["product"] == "TM0000"
    assert "TXFD0" not in json.dumps(out)


def test_issue_254_net_update_correlates_outside_the_raw_tail(tmp_path):
    """FILL and its NET_UPDATE merge even when the raw tail is later ORDER lines.

    The net line's product and bot_position must not replace the fill's.
    """
    bot = tmp_path / "bot"
    bot.mkdir()
    fill = {
        "event": "FILL", "ts": "2026-11-05T20:00:00+08:00", "bot_id": "TMF00_alpha",
        "account": ACCT, "product": "TM0000", "order_seq": "s1", "side": "BUY",
        "qty": 1, "fill_price": 100, "price_source": "deal", "action": "ENTRY",
        "net_position_after": None, "net_status": "pending", "bot_position_after": 1,
        "oi_generation": 3,
    }
    net = {
        "event": "NET_UPDATE", "ts": "2026-11-05T20:00:30+08:00", "bot_id": "TMF00_alpha",
        "account": ACCT, "product": "TXFD0", "order_seq": "s1", "side": "SELL",
        "qty": 9, "fill_price": 100, "price_source": "deal", "action": "ENTRY",
        "net_position_after": 5, "net_status": "answered", "bot_position_after": 99,
    }
    orders = [
        {"event": "ORDER", "order_seq": "s2", "action": "EXIT", "action_type": "exit",
         "side": "SELL", "qty": 1, "product": "TXFD0", "account": ACCT},
        {"event": "ORDER", "order_seq": "s3", "action": "ENTRY", "action_type": "entry",
         "side": "BUY", "qty": 1, "product": "TXFD0", "account": ACCT},
    ]
    lines = [fill, net, *orders]
    (bot / POSITION_LOG_NAME).write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in lines),
        encoding="utf-8")
    out = _view(bot, 1)
    assert out["bot_position"] == 1 and out["net_position"] == 5
    [rec] = out["records"]
    assert rec["order_seq"] == "s1" and rec["product"] == "TM0000"
    assert rec["side"] == "BUY" and rec["qty"] == 1
    assert rec["bot_position_after"] == 1 and rec["net_position_after"] == 5
    assert rec["net_status"] == "answered" and rec["ts"] == "2026-11-05T20:00:00+08:00"
    assert rec["account"] == "**********1234"
    assert "TXFD0" not in json.dumps(out)
    assert ACCT not in json.dumps(out)


def test_issue_254_legacy_lines_survive_an_order_tail(tmp_path):
    """Finalized ENTRY/EXIT/RESUME stay the logical rows. Later ORDERs add none."""
    original = _write_legacy(tmp_path)
    led = _reopen(tmp_path)
    led.register_order("n1", "entry", 0, 1, "TXFD0")
    led.register_order("n2", "exit", 1, 1, "TXFD0")
    assert (tmp_path / POSITION_LOG_NAME).read_text(encoding="utf-8").startswith(original)

    one = _view(tmp_path, 1)
    assert [r["action"] for r in one["records"]] == ["RESUME"]
    assert one["bot_position"] == 0 and one["net_position"] == 0
    assert one["records"][0]["order_seq"] == ""

    two = _view(tmp_path, 2)
    assert [(r["action"], r["order_seq"], r["product"], r["bot_position_after"],
             r["net_position_after"]) for r in two["records"]] == [
        ("EXIT", "leg-sell", "TM0000", 0, 0),
        ("RESUME", "", "TM0000", 0, 0),
    ]
    assert two["records"][0]["fill_price"] == 44622
    assert "TXFD0" not in json.dumps(two)
    assert "n1" not in json.dumps(two) and "n2" not in json.dumps(two)

    full = _view(tmp_path)
    assert [r["action"] for r in full["records"]] == ["ENTRY", "EXIT", "RESUME"]
    assert [(r["order_seq"], r["bot_position_after"], r["net_position_after"], r["product"])
            for r in full["records"]] == [
        (row["order_seq"], row["bot_position_after"], row["net_position_after"], row["product"])
        for row in _LEGACY
    ]
    assert full["records"][0]["account"] == "**********1234"
    assert ACCT not in json.dumps(full)


def test_issue_254_byte_window_projects_before_it_limits(tmp_path):
    """A byte cap drops events outside the window. Inside it, limit is logical."""
    from src.live.position_log import read_records

    def line(obj):
        return json.dumps(obj, separators=(",", ":"))

    early = line({
        "event": "FILL", "ts": "2026-11-05T19:00:00+08:00", "bot_id": "TMF00_alpha",
        "account": ACCT, "product": "TM0000", "order_seq": "early", "side": "BUY",
        "qty": 1, "fill_price": 1, "price_source": "deal", "action": "ENTRY",
        "net_position_after": None, "net_status": "pending", "bot_position_after": 1,
    })
    late = line({
        "event": "FILL", "ts": "2026-11-05T20:00:00+08:00", "bot_id": "TMF00_alpha",
        "account": ACCT, "product": "TXFD0", "order_seq": "late", "side": "SELL",
        "qty": 1, "fill_price": 50, "price_source": "fill_poller", "action": "ENTRY",
        "net_position_after": None, "net_status": "pending", "bot_position_after": -1,
    })
    refine = line({
        "event": "FILL_REFINE", "order_seq": "late", "fill_price": 55,
        "price_source": "deal",
    })
    net = line({
        "event": "NET_UPDATE", "ts": "2026-11-05T20:00:30+08:00", "bot_id": "TMF00_alpha",
        "account": ACCT, "product": "TM0000", "order_seq": "late", "side": "BUY",
        "qty": 4, "fill_price": 55, "price_source": "deal", "action": "ENTRY",
        "net_position_after": 3, "net_status": "answered", "bot_position_after": 99,
    })
    order = line({
        "event": "ORDER", "order_seq": "after", "action": "EXIT", "side": "BUY",
        "qty": 1, "product": "TM0000", "account": ACCT,
    })
    pad = "\n".join(line({
        "event": "ORDER", "order_seq": f"pad{i}", "action": "ENTRY", "side": "BUY",
        "qty": 1, "product": "TM0000",
    }) for i in range(30))
    path = tmp_path / POSITION_LOG_NAME
    path.write_text(early + "\n" + pad + "\n" + late + "\n" + refine + "\n" + net + "\n" + order + "\n",
                    encoding="utf-8")
    tail = (late + "\n" + refine + "\n" + net + "\n" + order + "\n").encode()
    window = read_records(str(path), 2, max_bytes=len(tail) + 8)
    assert [r["order_seq"] for r in window] == ["late"]
    assert window[0]["product"] == "TXFD0"
    assert window[0]["side"] == "SELL" and window[0]["qty"] == 1
    assert window[0]["fill_price"] == 55 and window[0]["price_source"] == "deal"
    assert window[0]["bot_position_after"] == -1
    assert window[0]["net_position_after"] == 3 and window[0]["net_status"] == "answered"
    whole = read_records(str(path), 5)
    assert [r["order_seq"] for r in whole] == ["early", "late"]


def test_issue_254_dashboard_read_uses_the_byte_bound(tmp_path, monkeypatch):
    """The page must not scan an unbounded journal, and must not raw-tail it."""
    seen = {}
    real = position_log_view.read_records

    def wrapped(path, limit=None, **kwargs):
        seen["limit"] = limit
        seen["max_bytes"] = kwargs.get("max_bytes")
        seen["opener"] = kwargs.get("opener")
        return real(path, limit, **kwargs)

    monkeypatch.setattr(position_log_view, "read_records", wrapped)
    out = _view(tmp_path, 2)
    assert out["records"] == [] and out["bot_position"] is None
    assert seen["limit"] == 2
    assert seen["max_bytes"] == position_log_view._TAIL_BYTES
    from scripts.dashboard import fileio
    assert seen["opener"] is fileio.open_shared
    assert fileio._FILE_SHARE_ALL == (0x1 | 0x2 | 0x4)


def test_issue_254_plain_startup_stays_flat_and_does_not_invent_a_close(tmp_path):
    """A non-resume deploy must not adopt the previous journal.

    Prior long 1, then a new accepted BUY, is +1. No synthetic SELL and
    the old fill is not counted again.
    """
    led = _ledger(tmp_path)
    led.register_order("old", "entry", 0, 1, "TM0000")
    led.record_fill("old", 100, source="deal", oi_generation=4)
    before = (tmp_path / POSITION_LOG_NAME).read_text(encoding="utf-8")

    fresh = _reopen(tmp_path)
    assert fresh.position == 0
    assert fresh._orders == {} and fresh._pending == [] and fresh._applied == set()
    assert fresh.flush_all() == 0
    assert (tmp_path / POSITION_LOG_NAME).read_text(encoding="utf-8") == before
    assert fresh.confirm_latest("entry", 100, source="fill_poller", oi_generation=1) is None

    fresh.register_order("new", "entry", 0, 1, "TM0000")
    rec = fresh.record_fill("new", 110, source="deal", oi_generation=1)
    assert rec["order_seq"] == "new" and rec["bot_position_after"] == 1
    assert fresh.position == 1
    fills = [e for e in _events(tmp_path) if e.get("event") == "FILL"]
    assert [e["order_seq"] for e in fills] == ["old", "new"]
    assert [e for e in _events(tmp_path) if e.get("action") == "EXIT"] == []
    assert (tmp_path / POSITION_LOG_NAME).read_text(encoding="utf-8").startswith(before)


def test_issue_254_resume_restores_long_once_and_the_unfilled_seq(tmp_path):
    """Explicit resume restores the long once. An unfilled seq matches only then."""
    held = tmp_path / "held"
    held.mkdir()
    led = _ledger(held)
    led.register_order("old", "entry", 0, 1, "TM0000")
    led.record_fill("old", 100, source="deal", oi_generation=4)
    resumed = _reopen(held)
    assert resumed.position == 0
    rec = resumed.resume(net=1, product="TM0000", fill_price=100)
    assert resumed.position == 1 and rec["bot_position_after"] == 1
    assert resumed.record_fill("old", 100, source="deal", oi_generation=1) is None
    assert resumed.confirm_latest("entry", 100, source="fill_poller", oi_generation=1) is None
    assert resumed.position == 1
    assert sum(1 for e in _events(held) if e.get("event") == "FILL") == 1

    open_dir = tmp_path / "open"
    open_dir.mkdir()
    waiting = _ledger(open_dir)
    waiting.register_order("e1", "entry", 0, 1, "TM0000", oi_generation=8)
    plain = _reopen(open_dir)
    assert plain.confirm_latest("entry", 101, source="fill_poller", oi_generation=1) is None
    assert plain.position == 0
    back = _reopen(open_dir)
    back.resume(net=None, product="TM0000")
    assert back.position == 0
    hit = back.confirm_latest("entry", 101, source="fill_poller", oi_generation=1)
    assert hit["order_seq"] == "e1" and hit["bot_position_after"] == 1
    assert back.position == 1
    assert back.on_oi_complete(2, 0) == 1
    assert back.position == 1


def test_issue_254_legacy_snapshot_is_the_resume_baseline(tmp_path):
    """Pre-#252 last bot_position_after wins over the signed-qty sum.

    Only a later FILL event moves that baseline. ORDER does not.
    """
    rows = [
        {"ts": "t", "bot_id": "TMF00_alpha", "account": ACCT, "product": "TM0000",
         "order_seq": "h1", "side": "BUY", "qty": 1, "fill_price": 10,
         "price_source": "deal", "action": "ENTRY", "net_position_after": 1,
         "net_status": "answered", "bot_position_after": 1},
        {"ts": "t", "bot_id": "TMF00_alpha", "account": ACCT, "product": "TM0000",
         "order_seq": "h2", "side": "SELL", "qty": 1, "fill_price": 11,
         "price_source": "deal", "action": "EXIT", "net_position_after": 0,
         "net_status": "answered", "bot_position_after": 0},
        {"ts": "t", "bot_id": "TMF00_alpha", "account": ACCT, "product": "TM0000",
         "order_seq": "h3", "side": "BUY", "qty": 1, "fill_price": 12,
         "price_source": "deal", "action": "ENTRY", "net_position_after": 1,
         "net_status": "answered", "bot_position_after": 1},
        {"ts": "t", "bot_id": "TMF00_alpha", "account": ACCT, "product": "TM0000",
         "order_seq": "", "side": "", "qty": 0, "fill_price": None,
         "price_source": "", "action": "RESUME", "net_position_after": 3,
         "net_status": "answered", "bot_position_after": 3},
    ]
    text = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    (tmp_path / POSITION_LOG_NAME).write_text(text, encoding="utf-8")
    plain = _reopen(tmp_path)
    assert plain.position == 0 and plain._applied == set()
    led = _reopen(tmp_path)
    led.resume(net=3, product="TM0000")
    assert led.position == 3
    assert led._applied >= {"h1", "h2", "h3"}
    assert led.record_fill("h1", 10, source="deal", oi_generation=1) is None
    assert led.position == 3
    assert '"event":"ORDER"' not in (tmp_path / POSITION_LOG_NAME).read_text(encoding="utf-8")
    led.register_order("h4", "entry", 0, 1, "TM0000")
    assert led.position == 3
    nxt = led.record_fill("h4", 20, source="deal", oi_generation=1)
    assert nxt["bot_position_after"] == 4 and led.position == 4
    assert (tmp_path / POSITION_LOG_NAME).read_text(encoding="utf-8").startswith(text)


def test_issue_254_restarted_pending_fill_ignores_the_old_oi_generation(tmp_path):
    """A stored generation is not compared with the new process's counter.

    Reed's pair is the contract: a fill saved at generation 900 and one
    saved at generation 2 both accept the first snapshot of a new process
    whose counter is at 3. The snapshot's flat net does not replace the
    own buy, and the number on disk is left as it was written.
    """
    outcomes = []
    for old_gen, new_gen in ((900, 3), (2, 3)):
        folder = tmp_path / f"gen{old_gen}"
        folder.mkdir()
        led = _ledger(folder)
        led.register_order("s", "entry", 0, 1, "TM0000", oi_generation=old_gen)
        led.record_fill("s", 100, source="deal", oi_generation=old_gen)
        plain = _reopen(folder)
        # Ordinary startup has no pending bucket. A flat snapshot and a
        # flush add no leg and do not adopt the old long.
        assert plain.position == 0 and plain._pending == [] and plain._orders == {}
        assert plain.on_oi_complete(new_gen, 0) == 0
        assert plain.flush_all() == 0
        assert plain.position == 0
        resumed = _reopen(folder)
        resumed.resume(net=None, product="TM0000")
        assert resumed.position == 1 and "s" in resumed._orders
        assert resumed._pending and resumed._pending[0]["order_seq"] == "s"
        answered = resumed.on_oi_complete(new_gen, 0)
        outcomes.append(answered)
        assert resumed.position == 1
        assert resumed.on_oi_complete(new_gen + 1, 0) == 0
        [row] = [r for r in _log(folder) if r["action"] == "ENTRY"]
        assert row["bot_position_after"] == 1 and row["net_position_after"] == 0
        assert row["net_status"] == "answered"
        assert [e for e in _events(folder) if e.get("action") == "EXIT"] == []
        stored = [e for e in _events(folder) if e.get("event") == "FILL"]
        assert stored[0]["oi_generation"] == old_gen
    assert outcomes == [1, 1]


def test_issue_254_read_and_replay_do_not_rewrite_the_journal(tmp_path):
    """Recovery and the dashboard reader are read-only.

    A truncated last line stays on disk and is not counted as a fill.
    """
    import hashlib

    led = _ledger(tmp_path)
    led.register_order("s", "entry", 0, 1, "TM0000", oi_generation=4)
    led.record_fill("s", 101, source="fill_poller", oi_generation=4)
    led.record_fill("s", 105, source="deal", oi_generation=4)
    led.on_oi_complete(5, 1)
    legacy = {
        "ts": "t", "bot_id": "TMF00_alpha", "account": ACCT, "product": "TM0000",
        "order_seq": "leg", "side": "BUY", "qty": 1, "fill_price": 9,
        "price_source": "deal", "action": "ENTRY", "net_position_after": 1,
        "net_status": "answered", "bot_position_after": 1,
    }
    path = tmp_path / POSITION_LOG_NAME
    original = path.read_bytes()
    path.write_bytes(original + (json.dumps(legacy, separators=(",", ":")) + "\n").encode())
    intact = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    digest = hashlib.sha256(intact).hexdigest()
    from src.live.position_log import read_records
    read_records(str(path))
    read_records(str(path), 2, max_bytes=2_000_000)
    viewer = _reopen(tmp_path)
    viewer._recover()
    viewer._recover()
    assert path.read_bytes() == intact
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert path.stat().st_mtime_ns == stamp

    cut = intact + b'{"event":"FILL","order_seq":"cut","side":"SELL"'
    path.write_bytes(cut)
    cut_stamp = path.stat().st_mtime_ns
    resumed = _reopen(tmp_path)
    resumed._recover()
    assert path.read_bytes() == cut
    assert path.stat().st_mtime_ns == cut_stamp
    assert "cut" not in resumed._applied
    assert resumed.position == 1  # legacy snapshot, not a fabricated short
    assert [r["order_seq"] for r in read_records(str(path)) if r["action"] == "ENTRY"] == [
        "s", "leg"]


# Full SHA of the #250 reader. A shallow checkout does not have this object
# until it is fetched. The ref is temporary and is not checked out.
_V250_SHA = "49268a08796bf1572a5a9e30dfa83a845cc8582b"
_V250_REF = "refs/temp/tai-robot-49268a0"


def _ensure_v250_object(root: str) -> None:
    """Fetch only 49268a0 into a temporary ref when the blob is missing."""
    import subprocess

    spec = f"{_V250_SHA}:src/live/position_log.py"
    probe = subprocess.run(
        ["git", "cat-file", "-e", spec],
        cwd=root, capture_output=True)
    if probe.returncode == 0:
        return
    fetched = subprocess.run(
        ["git", "fetch", "--depth=1", "origin", f"{_V250_SHA}:{_V250_REF}"],
        cwd=root, capture_output=True, text=True)
    if fetched.returncode != 0:
        raise AssertionError(
            "could not fetch 49268a0 into a temporary ref:\n"
            + fetched.stderr)
    again = subprocess.run(
        ["git", "cat-file", "-e", spec],
        cwd=root, capture_output=True, text=True)
    if again.returncode != 0:
        raise AssertionError("49268a0 is still missing after the fetch")


def _v250_module():
    """The unchanged position-log module from 49268a0.

    Tag v2.19.17 (534c134) has no ``position_log.py``. This is the first
    reader. ``resume`` uses the last line's ``bot_position_after`` or 0.
    """
    import importlib.util
    import inspect
    import os
    import subprocess
    import tempfile

    cached = getattr(_v250_module, "module", None)
    if cached is not None:
        return cached
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _ensure_v250_object(root)
    blob = subprocess.check_output(
        ["git", "show", f"{_V250_SHA}:src/live/position_log.py"], cwd=root)
    fd, path = tempfile.mkstemp(suffix=".py")
    try:
        os.write(fd, blob)
        os.close(fd)
        fd = None
        spec = importlib.util.spec_from_file_location("position_log_49268a0", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = inspect.getsource(module.PositionLedger.resume)
        assert "last_record" in source and "bot_position_after" in source
        _v250_module.module = module
        return module
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass


def _v250_resume_position(path: str) -> int:
    """Exact #250 resume assignment, executed by that commit's module."""
    module = _v250_module()
    last = module.last_record(path)
    return (module._int_or_none(last.get("bot_position_after")) or 0) if last else 0


def test_issue_254_v250_reader_keeps_position_behind_new_tails(tmp_path):
    """49268a0 last_record recovers a fill that is followed by an ORDER.

    A legacy file is unchanged. A FILL tail and a NET_UPDATE tail already
    carry bot_position_after. An ORDER or price-refine tail is followed by
    one copy of that fill, with no event key, so the old reader is not 0
    and this reader does not count a second lot.
    """
    original = _write_legacy(tmp_path)
    assert _v250_resume_position(str(tmp_path / POSITION_LOG_NAME)) == 0
    assert (tmp_path / POSITION_LOG_NAME).read_text(encoding="utf-8") == original

    filled = tmp_path / "fill"
    filled.mkdir()
    led = _ledger(filled)
    led.register_order("s1", "entry", 0, 1, "TM0000")
    led.record_fill("s1", 100, source="deal", oi_generation=4)
    assert _v250_resume_position(str(filled / POSITION_LOG_NAME)) == 1

    led.on_oi_complete(5, 0)
    assert _v250_resume_position(str(filled / POSITION_LOG_NAME)) == 1
    assert _events(filled)[-1]["event"] == "NET_UPDATE"
    assert _events(filled)[-1]["bot_position_after"] == 1

    led.register_order("s2", "exit", 1, 1, "TM0000")
    assert _v250_resume_position(str(filled / POSITION_LOG_NAME)) == 1
    anchor = _events(filled)[-1]
    assert "event" not in anchor
    assert anchor["order_seq"] == "s1" and anchor["bot_position_after"] == 1
    assert anchor["action"] == "ENTRY"
    again = _reopen(filled)
    again.resume(net=0, product="TM0000")
    assert again.position == 1
    assert again._applied == {"s1"}
    assert "s2" in again._orders and "s2" not in again._applied
    view = position_log_view.position_log(str(filled), "TMF00_alpha")
    assert [r["order_seq"] for r in view["records"] if r["action"] == "ENTRY"] == ["s1"]
    assert view["bot_position"] == 1


def test_issue_254_failed_anchor_keeps_the_order_for_the_same_process_fill(tmp_path):
    """The primary ORDER is the registry. The #250 anchor is best-effort.

    A failed first append leaves no line and no registry entry. A failed
    companion leaves the ORDER on disk and still registers it. The old
    reader is 0 for that tail. The poller's confirm_latest then appends
    the fill once; a deal row and the account net do not add another.
    Recover of the unfilled tail restores position 1 and the exit seq.
    A later successful order writes the anchor, and that reader returns 1.
    """
    import shutil

    import run_backtest as rb

    led = _ledger(tmp_path)
    led.register_order("s1", "entry", 0, 1, "TM0000")
    led.record_fill("s1", 100, source="deal", oi_generation=4)
    assert led.position == 1
    real = led._append_line

    def fail_first(_record):
        return False

    led._append_line = fail_first
    led.register_order("gone", "exit", 1, 1, "TM0000")
    assert "gone" in led._orders and led._last_seq["exit"] == "gone"
    assert [e["order_seq"] for e in _events(tmp_path) if e.get("event") == "ORDER"] == ["s1"]

    calls = {"n": 0}

    def fail_second(record):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(record)
        return False

    led._append_line = fail_second
    led.register_order("s2", "exit", 1, 1, "TM0000")
    assert calls["n"] == 2
    assert "s2" in led._orders
    assert led._last_seq["exit"] == "s2"
    assert led.position == 1
    assert _events(tmp_path)[-1]["event"] == "ORDER"
    assert _events(tmp_path)[-1]["order_seq"] == "s2"
    assert "bot_position_after" not in _events(tmp_path)[-1]
    path = str(tmp_path / POSITION_LOG_NAME)
    assert _v250_resume_position(path) == 0
    assert sum(1 for e in _events(tmp_path) if e.get("event") == "FILL") == 1

    held = tmp_path / "unfilled"
    held.mkdir()
    shutil.copy(tmp_path / POSITION_LOG_NAME, held / POSITION_LOG_NAME)
    restored = _reopen(held)
    restored.recover()
    assert restored.position == 1
    assert restored._applied == {"s1"}
    assert "s2" in restored._orders
    assert restored._last_seq["entry"] == "s1"
    assert restored._last_seq["exit"] == "s2"
    restored.register_order("later", "entry", 0, 1, "TM0000")
    assert _events(held)[-1].get("event") is None
    assert _events(held)[-1]["bot_position_after"] == 1
    assert _v250_resume_position(str(held / POSITION_LOG_NAME)) == 1
    assert sum(1 for e in _events(held) if e.get("event") == "FILL") == 1

    led._append_line = real
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._position_ledger = led
    rec = app._position_log(
        "confirm_latest", "exit", 90, source="fill_poller", oi_generation=5)
    assert rec["order_seq"] == "s2" and rec["action"] == "EXIT"
    assert rec["price_source"] == "fill_poller"
    assert rec["net_position_after"] is None and rec["net_status"] == "pending"
    assert rec["bot_position_after"] == 0 and led.position == 0
    assert _events(tmp_path)[-1]["event"] == "FILL"
    assert app._position_log(
        "confirm_latest", "exit", 90, source="fill_poller", oi_generation=6) is None
    assert led.record_fill("s2", 91, source="deal", oi_generation=5) is None
    # s1 and s2 are both still pending. The snapshot writes two status
    # lines and does not append another FILL.
    assert led.on_oi_complete(6, 0) == 2
    assert led.position == 0
    assert [e["order_seq"] for e in _events(tmp_path) if e.get("event") == "FILL"] == ["s1", "s2"]

    again = _reopen(tmp_path)
    resumed = again.resume(net=0, product="TM0000")
    assert again.position == 0
    assert again._applied == {"s1", "s2"}
    assert again._last_seq["entry"] == "s1"
    assert again._last_seq["exit"] == "s2"
    assert resumed["action"] == "RESUME" and resumed["bot_position_after"] == 0
    assert [e["order_seq"] for e in _events(tmp_path) if e.get("event") == "FILL"] == ["s1", "s2"]


def test_issue_254_failed_append_keeps_memory_and_the_next_fill_converges(tmp_path):
    """A failed journal append does not freeze the in-memory book.

    The order stays registered and the fill still moves position. The
    next append that succeeds writes that cumulative own position. A
    restart restores it once, and the fill whose line never landed is
    not counted a second time.
    """
    led = _ledger(tmp_path)

    def fail(_record):
        return False

    led._append_line = fail
    led.register_order("s1", "entry", 0, 1, "TM0000")
    assert "s1" in led._orders and led._last_seq["entry"] == "s1"
    missed = led.record_fill("s1", 100, source="fill_poller", oi_generation=4)
    assert missed is not None and missed["bot_position_after"] == 1
    assert led.position == 1 and led._applied == {"s1"}
    assert led.record_fill("s1", 105, source="deal", oi_generation=4) is None
    assert led._pending[0]["fill_price"] == 105
    assert led._pending[0]["price_source"] == "deal"
    assert _events(tmp_path) == []

    led._append_line = PositionLedger._append_line.__get__(led, PositionLedger)
    led.register_order("s2", "exit", 1, 1, "TM0000")
    landed = led.record_fill("s2", 90, source="deal", oi_generation=5)
    assert landed["order_seq"] == "s2" and landed["bot_position_after"] == 0
    assert led.position == 0
    assert [e["order_seq"] for e in _events(tmp_path) if e.get("event") == "FILL"] == ["s2"]

    again = _reopen(tmp_path)
    again.recover()
    assert again.position == 0
    assert again._applied == {"s1", "s2"}
    assert [e["order_seq"] for e in _events(tmp_path) if e.get("event") == "FILL"] == ["s2"]
    again.recover()
    assert again.position == 0 and again._applied == {"s1", "s2"}


def test_issue_254_failed_read_can_be_retried(tmp_path, monkeypatch):
    """A read that raises does not mark the ledger recovered.

    The next recover() loads the same fill once. A damaged row inside a
    read that did complete is skipped and does not unlock another replay.
    """
    from src.live import position_log as mod

    led = _ledger(tmp_path)
    led.register_order("s1", "entry", 0, 1, "TM0000")
    led.record_fill("s1", 100, source="deal", oi_generation=4)
    path = str(tmp_path / POSITION_LOG_NAME)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not-json\n")
    fresh = _reopen(tmp_path)
    real = mod.read_raw_records

    def boom(file_path, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(mod, "read_raw_records", boom)
    fresh.recover()
    assert fresh._recovered is False
    assert fresh.position == 0 and fresh._applied == set() and fresh._orders == {}
    monkeypatch.setattr(mod, "read_raw_records", real)
    fresh.recover()
    assert fresh._recovered is True
    assert fresh.position == 1 and fresh._applied == {"s1"}
    stamp = fresh._pending[0]["_at"]
    fresh._pending[0]["_gen"] = 7
    fresh.recover()
    assert fresh._pending[0]["_gen"] == 7
    assert fresh._pending[0]["_at"] == stamp
    text = (tmp_path / POSITION_LOG_NAME).read_text(encoding="utf-8")
    assert text.count('"event":"FILL"') == 1
    assert text.endswith("{not-json\n")


def test_issue_254_resume_inputs_fail_after_the_journal_is_loaded(tmp_path, monkeypatch):
    """Replay runs before price, symbol, and net reads. A display failure
    leaves the recovered fill and the accepted order in place, once.
    """
    import run_backtest as rb

    led = _ledger(tmp_path)
    led.register_order("filled", "entry", 0, 1, "TM0000")
    led.record_fill("filled", 100, source="deal", oi_generation=4)
    led.register_order("open", "entry", 0, 1, "TXFD0")

    def _app(ledger, *, price=None, size=0, snapshot=False, net=0):
        app = rb.BacktestApp.__new__(rb.BacktestApp)

        class _Broker:
            position_size = size

            def effective_entry_price(self):
                if isinstance(price, Exception):
                    raise price
                return price

        app._position_ledger = ledger
        app._live_runner = SimpleNamespace(
            symbol="TX00", broker=_Broker(), csv_logger=None)
        app._account_monitor = SimpleNamespace(
            oi_snapshot_received=snapshot,
            get_signed_position=lambda prefix: net,
        )
        app.live_log = SimpleNamespace(append=lambda *a, **k: None)
        return app

    fresh = _reopen(tmp_path)
    app = _app(fresh, price=RuntimeError("price"), size=1)
    rb.BacktestApp._position_log_resume(app, "TX00")
    assert fresh.position == 1
    assert fresh._applied == {"filled"}
    assert set(fresh._orders) == {"filled", "open"}
    assert [p["order_seq"] for p in fresh._pending] == ["filled"]
    assert fresh._pending[0]["_gen"] == -1
    assert [e for e in _events(tmp_path) if e.get("event") == "RESUME"] == []
    assert sum(1 for e in _events(tmp_path) if e.get("event") == "FILL") == 1

    class _BoomSize:
        @property
        def position_size(self):
            raise RuntimeError("size")

    sized = _reopen(tmp_path / "sized")
    _ledger(tmp_path / "sized").register_order("only", "entry", 0, 1, "TM0000")
    # The directory was empty of a fill; rebuild the known journal by copy.
    (tmp_path / "sized" / POSITION_LOG_NAME).write_bytes(
        (tmp_path / POSITION_LOG_NAME).read_bytes())
    app2 = _app(sized)
    app2._live_runner.broker = _BoomSize()
    rb.BacktestApp._position_log_resume(app2, "TX00")
    assert sized.position == 1 and "open" in sized._orders
    assert [e for e in _events(tmp_path / "sized") if e.get("event") == "RESUME"] == []

    real_resolve = rb.resolve_order_symbol
    monkeypatch.setattr(
        rb, "resolve_order_symbol",
        lambda symbol: (_ for _ in ()).throw(RuntimeError("symbol")))
    (tmp_path / "named").mkdir()
    (tmp_path / "named" / POSITION_LOG_NAME).write_bytes(
        (tmp_path / POSITION_LOG_NAME).read_bytes())
    named = _reopen(tmp_path / "named")
    app3 = _app(named, size=0, snapshot=False)
    rb.BacktestApp._position_log_resume(app3, "TX00")
    assert named.position == 1 and named._applied == {"filled"}
    assert [e for e in _events(tmp_path / "named") if e.get("event") == "RESUME"] == []
    monkeypatch.setattr(rb, "resolve_order_symbol", real_resolve)

    def _net_boom(prefix):
        raise RuntimeError("net")

    netted = _reopen(tmp_path / "netted")
    (tmp_path / "netted").mkdir()
    (tmp_path / "netted" / POSITION_LOG_NAME).write_bytes(
        (tmp_path / POSITION_LOG_NAME).read_bytes())
    app4 = _app(netted, size=0, snapshot=True)
    app4._account_monitor.get_signed_position = _net_boom
    rb.BacktestApp._position_log_resume(app4, "TX00")
    assert netted.position == 1 and "open" in netted._orders
    assert netted._pending[0]["net_status"] == "pending"
    # A later call whose inputs succeed writes one RESUME and does not
    # count the fill again or reset the restored generation.
    stamp = netted._pending[0]["_at"]
    app4._account_monitor.get_signed_position = lambda prefix: 1
    app4._account_monitor.oi_snapshot_received = True
    rb.BacktestApp._position_log_resume(app4, "TX00")
    assert netted.position == 1 and netted._pending[0]["_gen"] == -1
    assert netted._pending[0]["_at"] == stamp
    resumes = [e for e in _events(tmp_path / "netted") if e.get("event") == "RESUME"]
    assert len(resumes) == 1 and resumes[0]["bot_position_after"] == 1
    rb.BacktestApp._position_log_resume(app4, "TX00")
    assert len([e for e in _events(tmp_path / "netted") if e.get("event") == "RESUME"]) == 1
    assert sum(1 for e in _events(tmp_path / "netted") if e.get("event") == "FILL") == 1


def test_issue_254_second_reconcile_does_not_replay(tmp_path):
    """The real resume path on one ledger object runs recovery once.

    A later call may still be the same GUI object. It must not reset the
    generation or the timeout clock of a fill this process already holds.
    """
    import run_backtest as rb
    from src.live.fill_poller import FillPoller
    from src.live.trading_guard import TradingGuard

    led = _ledger(tmp_path)
    led.register_order("filled", "entry", 0, 1, "TM0000")
    led.record_fill("filled", 100, source="deal", oi_generation=900)
    original = (tmp_path / POSITION_LOG_NAME).read_bytes()
    clock = _Clock()
    app_led = _ledger(tmp_path, clock)
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.live_log = SimpleNamespace(append=lambda *a, **k: None)
    app._alerts = []
    app._alert = lambda title, msg: app._alerts.append((title, msg))
    guard = TradingGuard()
    guard.reset()
    app._trading_guard = guard
    app._fill_poller = FillPoller(guard, timeout=10.0)
    app._fill_poller.reset()
    app._fill_poll_timer_id = None
    app._futures_account = ACCT
    app._position_ledger = app_led
    app.root = SimpleNamespace(after=lambda *a, **k: None, after_cancel=lambda *a: None)
    app._live_runner = SimpleNamespace(
        symbol="TX00",
        broker=SimpleNamespace(
            position_size=1,
            position_side=SimpleNamespace(value="LONG"),
            entry_bar_index=4,
            effective_entry_price=lambda: 100,
        ),
    )
    state = {"boom": True}

    def price():
        if state["boom"]:
            raise RuntimeError("price")
        return 100

    app._live_runner.broker.effective_entry_price = price
    app._account_monitor = SimpleNamespace(
        get_signed_position=lambda *_a: 1,
        oi_snapshot_received=True,
    )
    rb.BacktestApp._reconcile_resumed_guard(app, {}, "auto", "TX00")
    assert app_led.position == 1
    assert app_led._pending[0]["_gen"] == -1
    assert [e for e in _events(tmp_path) if e.get("event") == "RESUME"] == []
    held_at = app_led._pending[0]["_at"]
    # This process then learns a real generation. A second reconcile must
    # not put the sentinel back or move the clock.
    app_led._pending[0]["_gen"] = 7
    clock.t = 40
    state["boom"] = False
    rb.BacktestApp._reconcile_resumed_guard(app, {}, "auto", "TX00")
    assert app_led._pending[0]["_gen"] == 7
    assert app_led._pending[0]["_at"] == held_at
    assert app_led.position == 1
    assert len([e for e in _events(tmp_path) if e.get("event") == "RESUME"]) == 1
    rb.BacktestApp._reconcile_resumed_guard(app, {}, "paper", "TX00")
    assert len([e for e in _events(tmp_path) if e.get("event") == "RESUME"]) == 1
    # Clearing the once-flag is the bypass: replay assigns a new sentinel
    # and the clock value at that later call.
    app_led._recovered = False
    clock.t = 80
    app_led.recover()
    assert app_led._pending[0]["_gen"] == -1
    assert app_led._pending[0]["_at"] == 80

    paper_led = _reopen(tmp_path / "paper")
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper" / POSITION_LOG_NAME).write_bytes(original)
    app._position_ledger = paper_led
    rb.BacktestApp._reconcile_resumed_guard(app, {}, "paper", "TX00")
    assert paper_led.position == 0 and paper_led._recovered is False
    assert [e for e in _events(tmp_path / "paper") if e.get("event") == "RESUME"] == []


def test_issue_254_duplicate_fill_lines_replay_once(tmp_path):
    """Two event:FILL lines for one seq move position once."""
    path = tmp_path / POSITION_LOG_NAME
    fill = {
        "event": "FILL", "ts": "2026-11-05T20:00:00+08:00", "bot_id": "TMF00_alpha",
        "account": ACCT, "product": "TM0000", "order_seq": "s1", "side": "BUY",
        "qty": 1, "fill_price": 100, "price_source": "deal", "action": "ENTRY",
        "net_position_after": None, "net_status": "pending", "bot_position_after": 1,
        "oi_generation": 4,
    }
    path.write_text(
        json.dumps(fill) + "\n" + json.dumps(fill) + "\n", encoding="utf-8")
    led = _reopen(tmp_path)
    led.recover()
    assert led.position == 1
    assert led._applied == {"s1"}
    assert len(led._pending) == 1


def test_issue_254_resumed_flat_exit_journals_the_saved_order(tmp_path, monkeypatch):
    """An already-flat resumed exit confirms the saved order.

    ``_position_log_resume`` runs before ``_arm_resumed_fill_poll``. The
    poller confirms synchronously when the OI snapshot is flat, so the
    registry has to be loaded first or the exit FILL is never written.
    """
    import run_backtest as rb
    from src.live.fill_poller import FillPoller
    from src.live.trading_guard import TradingGuard

    led = _ledger(tmp_path)
    led.register_order("s1", "entry", 0, 1, "TM0000")
    led.record_fill("s1", 100, source="deal", oi_generation=4)
    led.register_order("s2", "exit", 1, 1, "TM0000")
    fresh = _reopen(tmp_path)

    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", object())
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.live_log = SimpleNamespace(append=lambda *a, **k: None)
    app._alerts = []
    app._alert = lambda title, msg: app._alerts.append((title, msg))
    guard = TradingGuard()
    guard.reset()
    app._trading_guard = guard
    app._fill_poller = FillPoller(guard, timeout=10.0)
    app._fill_poller.reset()
    app._fill_poll_timer_id = None
    app._futures_account = ACCT
    app._position_ledger = fresh
    app.root = SimpleNamespace(after=lambda *a, **k: None, after_cancel=lambda *a: None)
    app._live_runner = SimpleNamespace(
        symbol="TX00",
        csv_logger=None,
        strategy_display_name="probe",
        broker=SimpleNamespace(
            position_size=0,
            position_side=None,
            entry_bar_index=4,
            trades=[SimpleNamespace(real_exit_price=90, exit_price=90)],
            effective_entry_price=lambda: None,
        ),
    )
    app._account_monitor = SimpleNamespace(
        get_signed_position=lambda *a, **k: 0,
        oi_snapshot_received=True,
        oi_query_generation=6,
        positions=[],
    )
    rb.BacktestApp._reconcile_resumed_guard(
        app,
        {"trading_guard": {
            "fill_pending": True,
            "fill_pending_type": "exit",
            "deferred_close": None,
        }},
        "auto",
        "TX00",
    )
    fills = [e["order_seq"] for e in _events(tmp_path) if e.get("event") == "FILL"]
    assert fills == ["s1", "s2"]
    assert fresh.position == 0 and fresh._applied == {"s1", "s2"}
    exit_line = [e for e in _events(tmp_path) if e.get("event") == "FILL" and e["order_seq"] == "s2"]
    assert len(exit_line) == 1 and exit_line[0]["bot_position_after"] == 0


def test_issue_254_shared_opener_reads_the_bounded_tail(tmp_path):
    """A caller-supplied binary opener is what the bounded read uses."""
    from src.live.position_log import read_raw_records

    body = b'{"event":"ORDER","order_seq":"a","action":"ENTRY"}\n{"event":"FILL","order_seq":"b","side":"BUY","qty":1,"action":"ENTRY","net_status":"pending","bot_position_after":1,"net_position_after":null}\n'
    path = tmp_path / POSITION_LOG_NAME
    path.write_bytes(b"PARTIAL" + body)
    seen = []

    class _Handle:
        def __init__(self, blob):
            self._blob = blob
            self._pos = 0

        def seek(self, offset, whence=0):
            if whence == 2:
                self._pos = len(self._blob) + offset
            else:
                self._pos = offset
            return self._pos

        def tell(self):
            return self._pos

        def read(self, n=-1):
            if n is None or n < 0:
                chunk = self._blob[self._pos:]
                self._pos = len(self._blob)
                return chunk
            chunk = self._blob[self._pos:self._pos + n]
            self._pos += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(name):
        seen.append(name)
        return _Handle(path.read_bytes())

    rows = read_raw_records(str(path), max_bytes=len(body), opener=opener)
    assert seen == [str(path)]
    assert [r["order_seq"] for r in rows] == ["b"]
