"""Dashboard-requested live trading-mode switch (paper <-> auto).

Exchange behaviour these tests are written from:
- Capital answers GetOpenInterestGW asynchronously through OnOpenInterest.
  A flat book is ``001,查無資料`` then ``##``; a held position is a ``TF,...``
  row. A non-zero GetOpenInterestGW code means no callback is coming.
- ``auto`` sends real orders. The bot must refuse it unless the real
  account is verified flat NOW (owner rule: any open position refuses,
  no override), and must re-check its own state when the reply lands.

The glue is driven for real: request file -> ``_service_mode_request`` ->
``GetOpenInterestGW`` -> ``OnOpenInterest`` -> ``_process_ui_queue`` ->
next poll -> ``LiveRunner._apply_mode_switch``.
"""

from __future__ import annotations

import csv
import glob
import json
import os
import queue
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.backtest.strategy import BacktestStrategy
from src.live import mode_request as mreq
from src.live.account_monitor import AccountMonitor
from src.live.fill_poller import FillPoller
from src.live.headless import build_deploy_request, build_parser
from src.live.headless_app import DeployDriver
from src.live.live_runner import LiveRunner
from src.live.trading_guard import TradingGuard

ACCT = "F1111111"
_NOW = datetime(2026, 11, 2, 10, 0, tzinfo=rb._TZ_TAIPEI)
POSITION_ROW = f"TF,{ACCT},TXFD0,B,1,0,22500.0,15,2,ID"


def _rights(realized=0, fee=0, tax=0, float_pnl=0):
    """An OnFutureRights row; net = realized - fee - tax + float_pnl."""
    vals = ["0"] * 35
    vals[1], vals[2], vals[3], vals[11] = (str(float_pnl), str(fee), str(tax),
                                           str(realized))
    vals[25] = "TWD"
    return ",".join(vals)


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Root:
    def __init__(self):
        self.pending = []

    def after(self, ms, fn, *args):
        self.pending.append((ms, fn, args))
        return len(self.pending)

    def after_cancel(self, token):
        pass


class _Flat(BacktestStrategy):
    name = "Flat"
    kline_type = 0
    kline_minute = 15

    def on_bar(self, bar, data_store, broker):
        pass

    def required_bars(self):
        return 2


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Discord:
    enabled = True

    def __init__(self):
        self.switched = []
        self.refused = []

    def mode_switched(self, old, new):
        self.switched.append((old, new))

    def mode_switch_refused(self, mode, reason, requested_by=""):
        self.refused.append((mode, reason, requested_by))


def _drain_ui_queue():
    while True:
        try:
            rb._ui_queue.get_nowait()
        except queue.Empty:
            return


class _Harness:
    def __init__(self, app, runner, clock, discord, com_calls):
        self.app = app
        self.runner = runner
        self.clock = clock
        self.discord = discord
        self.com_calls = com_calls
        self.bot_dir = runner.bot_dir

    def request(self, mode="auto", loss_limit=3000, request_id="a1b2c3", **extra):
        body = {"request_id": request_id, "mode": mode, "loss_limit": loss_limit,
                "requested_by": "tester",
                "requested_at": _NOW.isoformat(timespec="seconds")}
        body.update(extra)
        self.write_raw(json.dumps(body))

    def write_raw(self, text):
        with open(mreq.request_path(self.bot_dir), "w", encoding="utf-8") as fh:
            fh.write(text)

    def poll(self):
        self.app._service_mode_request(self.bot_dir)

    def oi(self, *rows):
        events = rb.SKOrderLibEvents()
        for row in rows:
            events.OnOpenInterest(row)
        rb.BacktestApp._process_ui_queue(self.app)

    def rights(self, row=None):
        rb.SKOrderLibEvents().OnFutureRights(row or _rights())
        rb.BacktestApp._process_ui_queue(self.app)

    def flat(self):
        """Both replies of a flat account with no loss today, then a poll."""
        self.oi("001,查無資料", "##")
        self.rights()
        self.poll()

    def result(self):
        with open(mreq.result_path(self.bot_dir), encoding="utf-8") as fh:
            return json.load(fh)

    def decisions(self):
        self.runner.csv_logger.close()
        rows = []
        for path in glob.glob(os.path.join(self.bot_dir, "*decision*.csv")):
            with open(path, encoding="utf-8") as fh:
                rows.extend(r for r in csv.reader(fh) if len(r) > 3)
        return [r for r in rows if r[3] == "MODE_SWITCH_REQUEST"]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    runner = LiveRunner(_Flat(), "TX00", log_dir=str(tmp_path))
    runner._is_reloading = False
    runner.suppress_strategy = False
    runner.trading_mode = "paper"
    runner.broker.trade_source = rb._mode_to_source("paper")
    runner._allow_live_override = True
    runner._dashboard_auto_opt_in = True
    runner._mode_switch_loss_cap = mreq.MAX_MODE_SWITCH_LOSS_LIMIT_NTD

    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app.root = _Root()
    app.lines = []
    app._live_log_msg = lambda msg, tag="status": app.lines.append(msg)
    guard = TradingGuard(daily_loss_limit=1000)
    guard.reset()
    app._trading_guard = guard
    app._fill_poller = FillPoller(guard, timeout=10.0)
    app._trading_mode = "paper"
    app._futures_account = ACCT
    app._logged_in = True
    app.login_user_var = _Var("F1111111111")
    clock = _Clock()
    app._account_monitor = AccountMonitor(clock=clock)
    app.real_pos_var = _Var()
    app._update_real_account_display = lambda: None
    app.trading_mode_var = _Var()
    app._mode_key_to_combo = {"paper": "模擬 Paper", "semi_auto": "半自動 Semi-Auto",
                              "auto": "全自動 Auto"}
    app._live_runner = runner
    app._mode_req_tracker = mreq.ModeRequestTracker(clock=clock)
    app._mode_request_clock = lambda: _NOW

    # Same wiring as _deploy_live_continue.
    runner.daily_loss_limit = guard.daily_loss_limit
    runner.on_mode_changed = app._on_mode_changed
    runner.mode_switch_veto = (
        lambda: "真實委託等待成交中 real order fill pending"
        if guard.fill_pending else None)

    com_calls = []
    code_box = {"code": 0}

    def _call_com(name, fn, *args, **kwargs):
        com_calls.append((name, args))
        if isinstance(code_box["code"], Exception):
            raise code_box["code"]
        return code_box["code"]

    discord = _Discord()
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", SimpleNamespace(GetOpenInterestGW=lambda *a: 0,
                                                   GetFutureRights=lambda *a: 0))
    monkeypatch.setattr(rb, "call_com", _call_com)
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(rb, "_app", app)
    _drain_ui_queue()
    h = _Harness(app, runner, clock, discord, com_calls)
    h.code_box = code_box
    yield h
    _drain_ui_queue()
    runner.csv_logger.close()


def _assert_paper(h):
    assert h.app._trading_mode == "paper"
    assert h.runner.trading_mode == "paper"
    assert h.runner.broker.trade_source == rb._mode_to_source("paper")


# ── auto: the account check ──

def test_flat_account_accepts_auto_and_arms_real_orders(harness):
    h = harness
    h.app._trading_guard.real_entry_confirmed = True  # stale from an earlier stint
    h.request(loss_limit=3000)
    h.poll()
    assert [c[0] for c in h.com_calls] == ["GetOpenInterestGW", "GetFutureRights"]
    assert h.com_calls[0][1] == h.com_calls[1][1] == ("F1111111111", ACCT, 1)
    res = h.result()
    assert res["status"] == "checking" and res["request_id"] == "a1b2c3"
    assert not os.path.exists(mreq.request_path(h.bot_dir)), "request not consumed"
    _assert_paper(h)  # never switched before the reply

    h.oi("001,查無資料", "##")
    h.poll()
    assert h.result()["status"] == "checking"  # equity not in yet
    _assert_paper(h)
    h.rights(_rights(realized=-1200, fee=50, tax=30, float_pnl=200))  # net -1,080
    h.poll()

    res = h.result()
    assert res["status"] == "accepted", res
    assert res["mode"] == "auto" and res["request_id"] == "a1b2c3"
    assert res["reason"] and res["at"].endswith("+08:00")
    guard = h.app._trading_guard
    assert guard.daily_loss_limit == 3000
    assert h.runner.daily_loss_limit == 3000
    assert h.app._trading_mode == "auto"
    assert h.runner.trading_mode == "auto"
    assert h.runner.broker.trade_source == rb._mode_to_source("auto")
    assert h.discord.switched == [("paper", "auto")]
    assert h.discord.refused == []
    # Real-order path is the one an --mode auto deploy gets.
    assert guard.decide("auto", "ENTRY_FILL", "LONG")[0] == guard.SEND_ENTRY
    # No real entry confirmed on this flat book: a sim exit must not send
    # a sNewClose=2 order that would open a naked position.
    assert guard.decide("auto", "TRADE_CLOSE", "LONG")[0] == guard.SKIP_EXIT
    routed = []
    h.app._handle_semi_auto_order = routed.append
    h.app._live_chart = None
    h.app._on_live_decision({"action": "ENTRY_FILL", "side": "LONG", "tag": "t",
                             "price": 22500, "reason": "r"})
    assert len(routed) == 1
    rows = h.decisions()
    assert len(rows) == 1 and "accepted" in rows[0][7] and "tester" in rows[0][7]
    # Session save records the mode, but resume uses the launch mode.
    with open(os.path.join(h.bot_dir, "session.json"), encoding="utf-8") as fh:
        assert json.load(fh)["daily_loss_limit"] == 3000


def test_account_with_position_refuses_auto(harness):
    h = harness
    h.request()
    h.poll()
    h.oi(POSITION_ROW, "##")
    h.poll()
    res = h.result()
    assert res["status"] == "refused"
    assert "未平倉" in res["reason"] and "多 x1 TXFD0" in res["reason"]
    assert ACCT not in res["reason"]
    _assert_paper(h)
    assert h.app._trading_guard.daily_loss_limit == 1000
    assert h.runner.daily_loss_limit == 1000
    assert h.discord.refused and h.discord.refused[0][0] == "auto"
    assert ACCT not in h.discord.refused[0][1]
    assert h.discord.switched == []
    rows = h.decisions()
    assert len(rows) == 1 and "refused" in rows[0][7]


@pytest.mark.parametrize("code", [5, 1001])
def test_rejected_query_refuses_without_waiting(harness, code):
    h = harness
    h.code_box["code"] = code
    h.request()
    h.poll()
    res = h.result()
    assert res["status"] == "refused"
    assert "持倉未知" in res["reason"] and str(code) in res["reason"]
    assert h.app._mode_req_tracker.pending is None
    _assert_paper(h)


def test_query_exception_refuses(harness):
    h = harness
    h.code_box["code"] = RuntimeError("gw down")
    h.request()
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "持倉未知" in res["reason"]
    _assert_paper(h)


def test_snapshot_timeout_refuses(harness):
    h = harness
    h.request()
    h.poll()
    h.clock.t += rb.OI_SNAPSHOT_TIMEOUT_S - 0.5
    h.poll()
    assert h.result()["status"] == "checking"
    h.clock.t += 1.0
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "逾時" in res["reason"]
    _assert_paper(h)
    # A reply that lands after the timeout must not switch anything.
    h.oi("001,查無資料", "##")
    h.poll()
    _assert_paper(h)


def test_bare_hash_is_unknown_and_times_out(harness):
    h = harness
    h.request()
    h.poll()
    h.oi("##")
    h.poll()
    assert h.result()["status"] == "checking"
    h.clock.t += rb.OI_SNAPSHOT_TIMEOUT_S + 1
    h.poll()
    assert h.result()["status"] == "refused"
    _assert_paper(h)


def test_stale_reply_from_older_query_does_not_answer(harness):
    """A ``001`` queued before our query belongs to the older query."""
    h = harness
    rb.SKOrderLibEvents().OnOpenInterest("001,查無資料")  # older generation
    h.request()
    h.poll()
    h.oi("##")
    h.poll()
    assert h.result()["status"] == "checking"
    _assert_paper(h)


def test_sim_position_opened_while_waiting_refuses(harness):
    h = harness
    h.request()
    h.poll()
    h.runner.broker.position_size = 1  # a sim entry filled meanwhile
    h.flat()
    res = h.result()
    assert res["status"] == "refused" and "模擬部位" in res["reason"]
    assert h.app._trading_mode == "paper" and h.runner.trading_mode == "paper"
    assert h.app._trading_guard.daily_loss_limit == 1000


def test_replay_started_while_waiting_refuses(harness):
    h = harness
    h.request()
    h.poll()
    h.runner.suppress_strategy = True
    h.flat()
    res = h.result()
    assert res["status"] == "refused" and "回放" in res["reason"]
    _assert_paper(h)


def test_account_past_the_new_loss_limit_refuses_auto(harness):
    """A paper bot never evaluated the account's day P&L against a limit.
    Accepting auto here would let a real entry go out before the next 30 s
    account poll paused it."""
    h = harness
    h.request(loss_limit=3000)
    h.poll()
    h.oi("001,查無資料", "##")
    h.rights(_rights(realized=-3500, fee=60, tax=40, float_pnl=500))  # net -3,100
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "虧損上限" in res["reason"], res
    _assert_paper(h)
    assert h.app._trading_guard.daily_loss_limit == 1000
    assert h.runner.daily_loss_limit == 1000
    assert h.discord.switched == []


def test_account_at_the_new_loss_limit_is_accepted_and_recorded(harness):
    h = harness
    h.request(loss_limit=3000)
    h.poll()
    h.oi("001,查無資料", "##")
    h.rights(_rights(realized=-3000))                                   # net -3,000
    h.poll()
    assert h.result()["status"] == "accepted"
    guard = h.app._trading_guard
    assert guard.paused is False
    # The guard starts the stint knowing today's net, not a stale value.
    guard.update_pnl(-3001)
    assert guard.paused is True


def test_equity_stored_before_the_request_does_not_answer(harness):
    h = harness
    h.rights(_rights())                       # an earlier 30 s poll's reply
    h.request()
    h.poll()
    h.oi("001,查無資料", "##")
    h.poll()
    assert h.result()["status"] == "checking"
    _assert_paper(h)


def test_unparseable_equity_refuses(harness):
    h = harness
    h.request()
    h.poll()
    h.oi("001,查無資料", "##")
    bad = _rights().split(",")
    bad[11] = "abc"
    h.rights(",".join(bad))
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "權益" in res["reason"]
    _assert_paper(h)


def test_equity_timeout_refuses(harness):
    h = harness
    h.request()
    h.poll()
    h.oi("001,查無資料", "##")
    h.poll()
    h.clock.t += rb.OI_SNAPSHOT_TIMEOUT_S + 1
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "權益" in res["reason"] and "逾時" in res["reason"]
    _assert_paper(h)


@pytest.mark.parametrize("code", [5, RuntimeError("gw down")])
def test_rejected_equity_query_refuses(harness, code):
    h = harness
    real = h.code_box

    def _call_com(name, fn, *args, **kwargs):
        h.com_calls.append((name, args))
        if name == "GetFutureRights":
            if isinstance(code, Exception):
                raise code
            return code
        return 0

    rb.call_com = _call_com
    h.request()
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "權益" in res["reason"]
    assert h.app._mode_req_tracker.pending is None
    assert real["code"] == 0
    _assert_paper(h)


def test_position_reply_refuses_without_waiting_for_equity(harness):
    h = harness
    h.request()
    h.poll()
    h.oi(POSITION_ROW, "##")
    h.poll()
    assert h.result()["status"] == "refused"


def test_request_waits_while_an_earlier_position_query_is_unanswered(harness):
    """Rows are tagged with the query current when they are queued, so a
    late reply to the 30 s poll's query would answer the switch's query."""
    h = harness
    h.app._account_monitor.begin_oi_query()   # the 30 s account poll
    h.request()
    h.poll()
    assert h.com_calls == []
    assert os.path.exists(mreq.request_path(h.bot_dir)), "consumed while in flight"
    h.clock.t += rb.OI_SNAPSHOT_TIMEOUT_S     # that query never answered
    h.poll()
    assert [c[0] for c in h.com_calls] == ["GetOpenInterestGW", "GetFutureRights"]
    assert h.result()["status"] == "checking"


def test_request_proceeds_once_the_earlier_query_answered(harness):
    h = harness
    h.app._account_monitor.begin_oi_query()
    h.oi("001,查無資料", "##")
    h.request()
    h.poll()
    assert [c[0] for c in h.com_calls] == ["GetOpenInterestGW", "GetFutureRights"]


def test_failed_final_result_write_is_retried(harness, monkeypatch):
    h = harness
    h.request()
    h.poll()
    real_write = mreq.write_result
    fail = {"n": 1}

    def _flaky(bot_dir, payload):
        if payload["status"] != "checking" and fail["n"]:
            fail["n"] -= 1
            raise PermissionError("locked by a reader")
        return real_write(bot_dir, payload)

    monkeypatch.setattr(mreq, "write_result", _flaky)
    h.flat()
    assert h.result()["status"] == "checking"     # the write failed
    assert h.app._trading_mode == "auto"
    h.poll()
    assert h.result()["status"] == "accepted"


def test_refused_auto_discord_notices_are_rate_limited(harness):
    h = harness
    h.runner.broker.position_size = 1
    for i in range(3):
        h.request(request_id=f"r{i}")
        h.poll()
        assert h.result()["status"] == "refused"
    assert len(h.discord.refused) == 1
    h.clock.t += mreq.REFUSED_NOTICE_INTERVAL_S
    h.request(request_id="r9")
    h.poll()
    assert len(h.discord.refused) == 2


# ── gates checked before any query ──

def test_fill_pending_refuses(harness):
    h = harness
    h.app._trading_guard.on_fill_pending("entry")
    h.request()
    h.poll()
    assert h.com_calls == []
    res = h.result()
    assert res["status"] == "refused" and "成交確認" in res["reason"]
    _assert_paper(h)


@pytest.mark.parametrize("attr", ["suppress_strategy", "_is_reloading"])
def test_replay_refuses(harness, attr):
    h = harness
    setattr(h.runner, attr, True)
    h.request()
    h.poll()
    assert h.com_calls == []
    assert h.result()["status"] == "refused"
    _assert_paper(h)


def test_sim_position_refuses_auto(harness):
    h = harness
    h.runner.broker.position_size = 1
    h.request()
    h.poll()
    assert h.com_calls == []
    assert h.result()["status"] == "refused"


@pytest.mark.parametrize("setup, needle", [
    (lambda h: setattr(h.app, "_futures_account", ""), "期貨帳號"),
    (lambda h: setattr(rb, "skO", None), "下單服務"),
    (lambda h: setattr(h.app._trading_guard, "halted", True), "已停止"),
    (lambda h: setattr(h.app._trading_guard, "paused", True), "虧損上限"),
])
def test_auto_only_gates_refuse(harness, setup, needle):
    h = harness
    setup(h)
    h.request()
    h.poll()
    assert h.com_calls == []
    res = h.result()
    assert res["status"] == "refused" and needle in res["reason"]
    _assert_paper(h)


@pytest.mark.parametrize("limit", [None, 0, -5, "3000", 12.5, True])
def test_invalid_loss_limit_refuses(harness, limit):
    h = harness
    h.request(loss_limit=limit)
    h.poll()
    assert h.com_calls == []
    res = h.result()
    assert res["status"] == "refused" and "loss_limit" in res["reason"]
    assert res["request_id"] == "a1b2c3"


def test_already_in_mode_refuses(harness):
    h = harness
    h.request(mode="paper")
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "已經是" in res["reason"]


def test_no_runner_refuses(harness):
    h = harness
    h.app._live_runner = None
    h.request()
    h.poll()
    assert h.com_calls == []
    assert h.result()["status"] == "refused"


def test_second_request_while_checking_refuses(harness):
    h = harness
    h.request(request_id="first")
    h.poll()
    h.request(request_id="second")
    h.poll()
    res = h.result()
    assert res["request_id"] == "second" and res["status"] == "refused"
    assert "處理中" in res["reason"]
    assert len(h.com_calls) == 2
    h.flat()
    res = h.result()
    assert res["request_id"] == "first" and res["status"] == "accepted"


# ── malformed / duplicate ──

@pytest.mark.parametrize("text, rid", [
    ("{not json", ""),
    ("[1, 2]", ""),
    (json.dumps({"mode": "auto", "loss_limit": 3000}), ""),
    (json.dumps({"request_id": "abc", "mode": "semi_auto"}), "abc"),
    (json.dumps({"request_id": "abc", "mode": "turbo"}), "abc"),
    (json.dumps({"request_id": "abc", "mode": "paper", "requested_by": 7}), "abc"),
])
def test_malformed_request_refused_and_consumed(harness, text, rid):
    h = harness
    h.write_raw(text)
    h.poll()
    assert not os.path.exists(mreq.request_path(h.bot_dir))
    res = h.result()
    assert res["status"] == "refused" and res["request_id"] == rid and res["reason"]
    assert h.com_calls == []
    _assert_paper(h)


def test_duplicate_request_id_is_ignored(harness):
    h = harness
    h.request()
    h.poll()
    h.flat()
    assert h.result()["status"] == "accepted"
    # Switch back to paper by hand, then replay the same request id.
    h.runner._apply_mode_switch("paper")
    before = h.result()
    h.request()
    h.poll()
    assert not os.path.exists(mreq.request_path(h.bot_dir))
    assert h.result() == before
    assert len(h.com_calls) == 2
    _assert_paper(h)


# ── paper ──

def test_paper_request_accepted_from_auto(harness):
    h = harness
    h.runner._apply_mode_switch("auto", user_confirmed=True)
    assert h.app._trading_mode == "auto"
    h.request(mode="paper", loss_limit=None, request_id="p1")
    h.poll()
    assert h.com_calls == []
    res = h.result()
    assert res == {**res, "request_id": "p1", "mode": "paper", "status": "accepted"}
    _assert_paper(h)
    assert h.discord.switched[-1] == ("auto", "paper")


def test_paper_request_refused_with_open_position(harness):
    h = harness
    h.runner._apply_mode_switch("auto", user_confirmed=True)
    h.runner.broker.position_size = 1
    h.request(mode="paper", request_id="p2")
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "模擬部位" in res["reason"]
    assert h.app._trading_mode == "auto"
    assert h.discord.refused == []  # Discord notice is for refused auto only


def test_paper_apply_refusal_reports_runner_reason(harness):
    """Gates pass but ``_apply_mode_switch`` itself refuses: report its reason."""
    h = harness
    h.runner._apply_mode_switch("auto", user_confirmed=True)
    h.runner.mode_switch_veto = lambda: "外部否決 veto"
    h.request(mode="paper", request_id="p3")
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "veto" in res["reason"]
    assert h.app._trading_mode == "auto"
    assert h.runner._callbacks.get("on_status", []) == []  # capture hook removed


# ── headless driver: deploy start / capabilities / poll ──

def _driver(tmp_path, app):
    driver = DeployDriver.__new__(DeployDriver)
    driver.app = app
    driver.root = app.root
    driver.bot_dir = str(tmp_path)
    driver.exit_code = None
    driver._stop_flag = False
    driver._stop_via_us = False
    driver.args = SimpleNamespace(heartbeat_min=0)
    driver.req = SimpleNamespace()
    driver.finish = lambda code, message="": setattr(driver, "exit_code", code)
    return driver


def test_deploy_clears_stale_request_and_marks_the_headless_file(tmp_path):
    for name in (mreq.REQUEST_FILENAME, mreq.RESULT_FILENAME):
        (tmp_path / name).write_text('{"request_id": "old", "mode": "auto"}',
                                     encoding="utf-8")
    seen = {}
    app = SimpleNamespace(root=_Root(), alerts=[], _live_runner=None,
                          status_var=_Var())

    def _deploy_live_from(req):
        seen["stale"] = [os.path.exists(tmp_path / n) for n in
                         (mreq.REQUEST_FILENAME, mreq.RESULT_FILENAME)]
        app._live_runner = SimpleNamespace(bot_dir=str(tmp_path))
        return True

    app._deploy_live_from = _deploy_live_from
    driver = _driver(tmp_path, app)
    driver._deploy()
    assert seen["stale"] == [False, False]
    marker = json.loads((tmp_path / "headless.json").read_text(encoding="utf-8"))
    assert marker["mode_request"] == 1 and marker["pid"] == os.getpid()
    assert "started_at" in marker
    assert not (tmp_path / "capabilities.json").exists()


def test_refused_deploy_writes_no_marker_capability(tmp_path):
    app = SimpleNamespace(root=_Root(), alerts=[("x", "y")], _live_runner=None,
                          status_var=_Var(), _deploy_live_from=lambda req: False)
    driver = _driver(tmp_path, app)
    driver._deploy()
    assert not (tmp_path / "headless.json").exists()
    assert not (tmp_path / "capabilities.json").exists()


def test_result_write_never_creates_a_bot_dir(tmp_path):
    missing = tmp_path / "not-a-bot"
    with pytest.raises(FileNotFoundError):
        mreq.write_result(str(missing), mreq.result_payload("r", "auto", "refused", "x", "t"))
    assert not missing.exists()


def test_poll_services_mode_request_and_survives_errors(tmp_path):
    calls = []

    def _service(bot_dir):
        calls.append(bot_dir)
        raise RuntimeError("boom")

    reconnects = []
    app = SimpleNamespace(root=_Root(), _live_runner=object(),
                          _service_mode_request=_service,
                          _manual_reconnect=lambda: reconnects.append(1))
    (tmp_path / "CONTROL").write_text("reconnect\n", encoding="utf-8")
    driver = _driver(tmp_path, app)
    driver._poll()
    assert calls == [str(tmp_path)]
    assert reconnects == [1], "a mode-request error skipped the CONTROL channel"
    assert driver.exit_code is None
    assert app.root.pending, "poll did not reschedule after a mode-request error"


def test_resume_comes_back_in_launch_mode():
    """Owner rule: an auto switch is not persisted across restarts."""
    args = build_parser().parse_args(
        ["deploy", "--symbol", "TMF00", "--bot", "b1", "--mode", "paper",
         "--resume"])
    req = build_deploy_request(args, {"trading_mode": "auto", "strategy": "X",
                                      "daily_loss_limit": 3000})
    assert req.trading_mode == "paper"


# ── pure module ──

def test_parse_request_contract():
    req = mreq.parse_request(json.dumps({
        "request_id": "deadbeef", "mode": "auto", "loss_limit": 2500,
        "requested_by": "owner", "requested_at": "2026-10-28T10:00:00+08:00"}))
    assert req == mreq.ModeRequest("deadbeef", "auto", 2500, "owner",
                                   "2026-10-28T10:00:00+08:00")
    paper = mreq.parse_request(json.dumps({"request_id": "p", "mode": "paper"}))
    assert paper.loss_limit is None


def test_tracker_answer_wins_over_deadline():
    clock = _Clock()
    t = mreq.ModeRequestTracker(clock=clock)
    assert t.poll(False) == t.IDLE
    t.start(mreq.ModeRequest("x", "auto", 1), 5.0)
    assert t.poll(False) == t.WAITING
    clock.t += 6
    assert t.poll(True) == t.ANSWERED
    assert t.poll(False) == t.TIMEOUT


def test_format_positions_redacts_account():
    text = mreq.format_positions([
        {"account": ACCT, "product": "TXFD0", "side": "S", "qty": 2},
        {"account": ACCT, "product": "MXFD0", "side": "B", "qty": 0},
    ])
    assert text == "空 x2 TXFD0"


def test_result_written_atomically_leaves_no_temp(tmp_path):
    mreq.write_result(str(tmp_path), mreq.result_payload("r", "auto", "refused", "原因", "t"))
    assert sorted(os.listdir(tmp_path)) == [mreq.RESULT_FILENAME]
    data = json.loads((tmp_path / mreq.RESULT_FILENAME).read_text(encoding="utf-8"))
    assert data == {"request_id": "r", "mode": "auto", "status": "refused",
                    "reason": "原因", "at": "t"}


# ── opt-in, cap, expiry, one owner, P&L clear ──

def test_allow_live_override_false_is_refused_before_any_broker_query(harness):
    """The opt-in neighbour check refuses before GetOpenInterestGW."""
    h = harness
    h.runner._allow_live_override = False
    h.runner._dashboard_auto_opt_in = True
    h.request()
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "allow_live_override" in res["reason"]
    assert h.com_calls == []
    assert h.runner.trading_mode == "paper"
    assert h.app._trading_mode == "paper"


def test_auto_refused_when_the_bot_is_not_opted_in(harness):
    h = harness
    h.runner._dashboard_auto_opt_in = False
    h.runner._allow_live_override = True
    h.request()
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "設定" in res["reason"]
    assert h.com_calls == []
    assert h.runner.trading_mode == "paper"


def test_loss_cap_constant_is_twenty_thousand():
    assert mreq.MAX_MODE_SWITCH_LOSS_LIMIT_NTD == 20_000


def test_literal_20001_is_refused_and_20000_is_accepted(harness):
    """Literals, not constant+1, so a 10_000_000 cap mutant fails here."""
    h = harness
    h.request(loss_limit=20_001, request_id="over-literal")
    h.poll()
    assert h.result()["status"] == "refused", h.result()
    assert h.com_calls == []
    assert h.runner.trading_mode == "paper"
    h.request(loss_limit=20_000, request_id="at-literal")
    h.poll()
    h.flat()
    assert h.result()["status"] == "accepted", h.result()
    assert h.app._trading_guard.daily_loss_limit == 20_000


def test_loss_cap_accepts_the_cap_and_refuses_above_zero_and_missing(harness):
    h = harness
    cap = mreq.MAX_MODE_SWITCH_LOSS_LIMIT_NTD
    h.request(loss_limit=cap + 1, request_id="over")
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and str(cap) in res["reason"].replace(",", "")
    assert h.com_calls == []
    h.request(loss_limit=0, request_id="zero")
    h.poll()
    assert h.result()["status"] == "refused" and "loss_limit" in h.result()["reason"]
    h.write_raw(json.dumps({"request_id": "miss", "mode": "auto",
                            "requested_at": _NOW.isoformat(timespec="seconds")}))
    h.poll()
    assert h.result()["status"] == "refused" and "loss_limit" in h.result()["reason"]
    assert h.com_calls == []
    h.request(loss_limit=cap, request_id="atcap")
    h.poll()
    h.flat()
    assert h.result()["status"] == "accepted", h.result()
    assert h.app._trading_guard.daily_loss_limit == cap


def test_per_bot_settings_can_lower_the_cap_but_not_raise_it(harness):
    assert mreq.effective_loss_cap(None) == mreq.MAX_MODE_SWITCH_LOSS_LIMIT_NTD
    assert mreq.effective_loss_cap(50_000_000) == mreq.MAX_MODE_SWITCH_LOSS_LIMIT_NTD
    assert mreq.effective_loss_cap(mreq.MAX_MODE_SWITCH_LOSS_LIMIT_NTD) == (
        mreq.MAX_MODE_SWITCH_LOSS_LIMIT_NTD)
    assert mreq.effective_loss_cap(10_000) == 10_000
    assert mreq.effective_loss_cap(True) == mreq.MAX_MODE_SWITCH_LOSS_LIMIT_NTD
    assert mreq.effective_loss_cap(0) == mreq.MAX_MODE_SWITCH_LOSS_LIMIT_NTD
    assert mreq.parse_dashboard_auto_opt_in(
        {"A": True, "B": "true", "C": 1, "D": False}) == frozenset({"A"})
    h = harness
    h.runner._mode_switch_loss_cap = mreq.effective_loss_cap(10_000)
    h.request(loss_limit=10_001, request_id="above")
    h.poll()
    assert h.result()["status"] == "refused" and h.com_calls == []
    h.request(loss_limit=10_000, request_id="at")
    h.poll()
    h.flat()
    assert h.result()["status"] == "accepted"


@pytest.mark.parametrize("age,accepted", [(59, True), (60, True), (61, False)])
def test_request_age_boundary(harness, age, accepted):
    h = harness
    at = (_NOW - timedelta(seconds=age)).isoformat(timespec="seconds")
    h.request(requested_at=at, request_id=f"age{age}")
    h.poll()
    if not accepted:
        assert h.result()["status"] == "refused" and "60" in h.result()["reason"]
        assert h.com_calls == []
        return
    h.flat()
    assert h.result()["status"] == "accepted", h.result()


@pytest.mark.parametrize("skew,accepted", [(5, True), (6, False)])
def test_request_future_skew(harness, skew, accepted):
    h = harness
    at = (_NOW + timedelta(seconds=skew)).isoformat(timespec="seconds")
    h.request(requested_at=at, request_id=f"fut{skew}")
    h.poll()
    if not accepted:
        assert h.result()["status"] == "refused" and "超前" in h.result()["reason"]
        assert h.com_calls == []
        return
    h.flat()
    assert h.result()["status"] == "accepted", h.result()


def test_naive_requested_at_is_refused(harness):
    """The dashboard always writes a timezone. A bare local timestamp is not one."""
    h = harness
    h.request(requested_at="2026-11-02T10:00:00", request_id="naive")
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "時區" in res["reason"]
    assert h.com_calls == []
    assert h.runner.trading_mode == "paper"


def test_missing_requested_at_is_refused(harness):
    h = harness
    h.request(requested_at="", request_id="notime")
    h.poll()
    assert h.result()["status"] == "refused" and "requested_at" in h.result()["reason"]
    assert h.com_calls == []
    h.write_raw(json.dumps({"request_id": "absent", "mode": "auto", "loss_limit": 1000}))
    h.poll()
    assert h.result()["status"] == "refused" and "requested_at" in h.result()["reason"]


def test_refused_switch_clears_the_stored_pnl(harness):
    h = harness
    h.runner.mode_switch_veto = lambda: "外部否決 veto"
    h.app._trading_guard._last_net_pnl = 0
    h.request(loss_limit=3000)
    h.poll()
    h.oi("001,查無資料", "##")
    h.rights(_rights(realized=-500))
    h.poll()
    assert h.result()["status"] == "refused" and "veto" in h.result()["reason"]
    assert h.runner.trading_mode == "paper"
    assert h.app._trading_guard._last_net_pnl == 0
    assert h.app._trading_guard.daily_loss_limit == 1000


def test_control_file_pending_refuses_mode_request_on_the_same_tick(harness):
    h = harness
    h.runner._apply_mode_switch("auto", user_confirmed=True)
    assert h.runner.trading_mode == "auto"
    driver = _driver(h.bot_dir, h.app)
    reconnects = []
    h.app._manual_reconnect = lambda: reconnects.append(1)
    with open(os.path.join(h.bot_dir, "CONTROL"), "w", encoding="utf-8") as fh:
        fh.write("reconnect\n")
    h.request(mode="paper", request_id="tick1")
    driver._poll_once()
    res = h.result()
    assert res["status"] == "refused" and "CONTROL" in res["reason"]
    assert h.runner.trading_mode == "auto"
    assert reconnects == [1]
    assert not os.path.exists(os.path.join(h.bot_dir, "CONTROL"))


def test_example_settings_opt_in_nobody():
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "settings.example.yaml"), encoding="utf-8") as fh:
        trading = yaml.safe_load(fh)["trading"]
    assert trading["allow_live_override"] is False
    assert mreq.parse_dashboard_auto_opt_in(trading["dashboard_auto_opt_in"]) == frozenset()
    assert mreq.parse_mode_switch_loss_caps(trading["mode_switch_max_loss_ntd"]) == {}
