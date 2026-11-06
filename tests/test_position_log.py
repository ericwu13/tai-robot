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
    assert _log(tmp_path) == []                       # waits for a post-fill net

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
    led = _ledger(tmp_path)
    led.register_order("a", "entry", 0, 1, "TM0000")
    led.record_fill("a", 100, source="deal", oi_generation=1)
    led.flush_all()
    assert _ledger(tmp_path).position == 0           # new deploy = 0


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
