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
from datetime import datetime
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
POSITION_ROW = f"TF,{ACCT},TXFD0,B,1,0,22500.0,15,2,ID"


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
                "requested_by": "tester", "requested_at": "2026-10-28T10:00:00+08:00"}
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
    app._account_monitor = AccountMonitor()
    app.real_pos_var = _Var()
    app._update_real_account_display = lambda: None
    app.trading_mode_var = _Var()
    app._mode_key_to_combo = {"paper": "模擬 Paper", "semi_auto": "半自動 Semi-Auto",
                              "auto": "全自動 Auto"}
    app._live_runner = runner
    clock = _Clock()
    app._mode_req_tracker = mreq.ModeRequestTracker(clock=clock)

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
    monkeypatch.setattr(rb, "skO", SimpleNamespace(GetOpenInterestGW=lambda *a: 0))
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
    assert [c[0] for c in h.com_calls] == ["GetOpenInterestGW"]
    assert h.com_calls[0][1] == ("F1111111111", ACCT, 1)
    res = h.result()
    assert res["status"] == "checking" and res["request_id"] == "a1b2c3"
    assert not os.path.exists(mreq.request_path(h.bot_dir)), "request not consumed"
    _assert_paper(h)  # never switched before the reply

    h.oi("001,查無資料", "##")
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
    h.oi("001,查無資料", "##")
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "模擬部位" in res["reason"]
    assert h.app._trading_mode == "paper" and h.runner.trading_mode == "paper"
    assert h.app._trading_guard.daily_loss_limit == 1000


def test_replay_started_while_waiting_refuses(harness):
    h = harness
    h.request()
    h.poll()
    h.runner.suppress_strategy = True
    h.oi("001,查無資料", "##")
    h.poll()
    res = h.result()
    assert res["status"] == "refused" and "回放" in res["reason"]
    _assert_paper(h)


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
    assert len(h.com_calls) == 1
    h.oi("001,查無資料", "##")
    h.poll()
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
    h.oi("001,查無資料", "##")
    h.poll()
    assert h.result()["status"] == "accepted"
    # Switch back to paper by hand, then replay the same request id.
    h.runner._apply_mode_switch("paper")
    before = h.result()
    h.request()
    h.poll()
    assert not os.path.exists(mreq.request_path(h.bot_dir))
    assert h.result() == before
    assert len(h.com_calls) == 1
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


def test_deploy_clears_stale_request_and_writes_capabilities(tmp_path):
    for name in (mreq.REQUEST_FILENAME, mreq.RESULT_FILENAME, mreq.CAPABILITIES_FILENAME):
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
    caps = json.loads((tmp_path / mreq.CAPABILITIES_FILENAME).read_text(encoding="utf-8"))
    assert caps == {"mode_request": 1, "pid": os.getpid(), "version": rb.APP_VERSION}


def test_refused_deploy_writes_no_capabilities(tmp_path):
    app = SimpleNamespace(root=_Root(), alerts=[("x", "y")], _live_runner=None,
                          status_var=_Var(), _deploy_live_from=lambda req: False)
    driver = _driver(tmp_path, app)
    driver._deploy()
    assert not (tmp_path / mreq.CAPABILITIES_FILENAME).exists()


def test_capabilities_never_create_a_bot_dir(tmp_path):
    missing = tmp_path / "not-a-bot"
    app = SimpleNamespace(root=_Root(), alerts=[], status_var=_Var(),
                          _live_runner=object())
    app._deploy_live_from = lambda req: True
    driver = _driver(missing, app)
    driver._deploy()
    assert not missing.exists()
    with pytest.raises(FileNotFoundError):
        mreq.write_result(str(missing), mreq.result_payload("r", "auto", "refused", "x", "t"))
    assert not missing.exists()


def test_poll_services_mode_request_and_survives_errors(tmp_path):
    calls = []

    def _service(bot_dir):
        calls.append(bot_dir)
        raise RuntimeError("boom")

    app = SimpleNamespace(root=_Root(), _live_runner=object(),
                          _service_mode_request=_service)
    driver = _driver(tmp_path, app)
    driver._poll()
    assert calls == [str(tmp_path)]
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
