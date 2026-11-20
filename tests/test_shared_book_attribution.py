"""Shared book, part 2: deploy, resume and dashboard auto read bots' own logs.

For a bot in ``trading.shared_book_opt_in``, lots on the account that the
bots' own ``position_log.jsonl`` files account for are not orphans:

* account lots on the product == sum of every bot dir's own logged position
  → the pre-deploy "Existing Position" prompt is skipped, resume checks the
  bot's OWN logged lot against the restored sim, and the dashboard switch
  to auto is not refused for other bots' lots;
* anything else (another product or options row, an unreadable log, a
  mismatch, own lot on a new deploy / paper→auto) keeps master's path.

Bots not opted in keep master's behaviour on all three paths.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import run_backtest as rb
from src.live import headless as hl
from src.live import shared_book as sb
from src.live.account_monitor import FUTURES_OI_MARKET, AccountMonitor
from src.live.position_log import POSITION_LOG_NAME, PositionLedger, replay_position

PRODUCT = "TMFL6"


def _row(side="B", qty=1, product=PRODUCT, market=FUTURES_OI_MARKET):
    return {"product": product, "side": side, "qty": qty, "market": market}


def _journal(bot_dir, fills, product=PRODUCT, account=""):
    """Write a real journal: ``fills`` is a list of "BUY"/"SELL"."""
    os.makedirs(bot_dir, exist_ok=True)
    led = PositionLedger(str(bot_dir), bot_id=os.path.basename(str(bot_dir)),
                         account=account)
    for i, side in enumerate(fills):
        seq = f"23156000000{i:02d}"
        action = "entry" if i % 2 == 0 else "exit"
        led.register_order(seq, action, side, 1, product, oi_generation=1)
        led.record_fill(seq, 39000 + i, source="deal", oi_generation=1, qty=1)
    return led


# ── replay_position ──

def test_replay_no_journal_is_flat(tmp_path):
    assert replay_position(str(tmp_path)) == (0, "", "")


def test_replay_long_short_and_closed(tmp_path):
    _journal(tmp_path / "a", ["BUY"])
    _journal(tmp_path / "b", ["SELL"])
    _journal(tmp_path / "c", ["BUY", "SELL"])
    assert replay_position(str(tmp_path / "a"))[:2] == (1, PRODUCT)
    assert replay_position(str(tmp_path / "b"))[:2] == (-1, PRODUCT)
    assert replay_position(str(tmp_path / "c"))[:2] == (0, PRODUCT)


def test_replay_unreadable_is_unknown_not_flat(tmp_path):
    _journal(tmp_path / "a", ["BUY"])

    def _deny(_path):
        raise PermissionError("locked")
    assert replay_position(str(tmp_path / "a"), opener=_deny) is None


def test_replay_matches_the_live_ledger_recover(tmp_path):
    live = _journal(tmp_path / "a", ["BUY", "SELL", "SELL"])
    fresh = PositionLedger(str(tmp_path / "a"), bot_id="a")
    fresh.recover()
    assert replay_position(str(tmp_path / "a"))[0] == fresh.position == live.position == -1


def test_replay_legacy_line_uses_its_baseline(tmp_path):
    d = tmp_path / "old"
    d.mkdir()
    (d / POSITION_LOG_NAME).write_text(
        '{"action":"ENTRY","order_seq":"2315600000001","side":"BUY","qty":1,'
        f'"product":"{PRODUCT}","bot_position_after":1}}\n', encoding="utf-8")
    assert replay_position(str(d))[:2] == (1, PRODUCT)


# ── fleet_positions ──

def test_fleet_positions_sums_only_this_product(tmp_path):
    _journal(tmp_path / "TMF00_a", ["BUY"])
    _journal(tmp_path / "TMF00_b", ["SELL"])
    (tmp_path / "TMF00_paper").mkdir()                    # never sent an order
    _journal(tmp_path / "TX00_c", ["BUY"], product="TXFL6")
    (tmp_path / "TX00_1m_20260302.csv").write_text("x", encoding="utf-8")
    assert sb.fleet_positions(str(tmp_path), "TM") == {"TMF00_a": 1, "TMF00_b": -1}


def test_fleet_positions_one_unreadable_log_is_unknown(tmp_path):
    _journal(tmp_path / "TMF00_a", ["BUY"])
    _journal(tmp_path / "TMF00_b", ["SELL"])
    real_open = open

    def _opener(path):
        if "TMF00_b" in path:
            raise PermissionError("locked")
        return real_open(path, "rb")
    assert sb.fleet_positions(str(tmp_path), "TM", opener=_opener) is None


def test_replay_half_written_line_is_unknown(tmp_path):
    _journal(tmp_path / "a", ["BUY"])
    with open(tmp_path / "a" / POSITION_LOG_NAME, "a", encoding="utf-8") as fh:
        fh.write('{"event":"FILL","order_seq":"23')
    assert replay_position(str(tmp_path / "a")) is None


def test_fleet_positions_skips_dead_siblings_but_never_own(tmp_path):
    _journal(tmp_path / "TMF00_me", ["BUY"])
    _journal(tmp_path / "TMF00_dead", ["SELL"])
    _journal(tmp_path / "TMF00_live", ["SELL"])
    alive = lambda d: d.endswith("TMF00_live")
    assert sb.fleet_positions(str(tmp_path), "TM", own_name="TMF00_me",
                              alive=alive) == {"TMF00_me": 1, "TMF00_live": -1}


def test_fleet_positions_skips_other_accounts(tmp_path):
    _journal(tmp_path / "TMF00_a", ["BUY"], account="F0000001")
    _journal(tmp_path / "TMF00_b", ["SELL"], account="F0000002")
    _journal(tmp_path / "TMF00_legacy", ["SELL"])          # no account field
    assert replay_position(str(tmp_path / "TMF00_b"))[2] == "F0000002"
    assert sb.fleet_positions(str(tmp_path), "TM", account="F0000001") == {
        "TMF00_a": 1, "TMF00_legacy": -1}


def test_fleet_positions_missing_base_or_prefix_is_unknown(tmp_path):
    assert sb.fleet_positions(str(tmp_path / "nope"), "TM") is None
    assert sb.fleet_positions(str(tmp_path), "") is None


# ── account_lots / attribute ──

def test_account_lots_sums_split_rows_and_refuses_other_rows():
    assert sb.account_lots([_row("B", 2), _row("S", 1)], "TM", FUTURES_OI_MARKET) == 1
    assert sb.account_lots([_row(qty=0)], "TM", FUTURES_OI_MARKET) == 0
    assert sb.account_lots([_row(product="TXFL6")], "TM", FUTURES_OI_MARKET) is None
    assert sb.account_lots([_row(market="TO")], "TM", FUTURES_OI_MARKET) is None
    assert sb.account_lots([_row(side="?")], "TM", FUTURES_OI_MARKET) is None


@pytest.mark.parametrize("rows, positions, own_may_hold, ok", [
    ([_row("B")], {"other": 1}, False, True),                 # B's lot only
    ([_row("S")], {"other": -1, "me": 0}, False, True),
    ([_row("B")], {"me": 1}, True, True),                     # resume: own lot
    ([_row("B")], {"me": 1}, False, False),                   # paper→auto: own lot
    ([_row("B", 2)], {"other": 1}, False, False),             # one lot unaccounted
    ([_row("B"), _row("B")], {"other": 1}, False, False),     # repeated row
    ([_row("B"), _row(product="TXFL6")], {"other": 1}, False, False),
    ([_row("B")], None, False, False),                        # logs unreadable
    ([], {"other": 1}, False, False),                         # log says lot, book empty
])
def test_attribute(rows, positions, own_may_hold, ok):
    att = sb.attribute(rows, "TM", FUTURES_OI_MARKET, positions, "me",
                       own_may_hold=own_may_hold)
    assert att.ok is ok
    assert att.reason


# ── path 1: pre-deploy "Existing Position" prompt ──

class _HoldingMonitor(AccountMonitor):
    """Answers the deploy's GetOpenInterestGW with ``ROWS`` instead of flat."""

    ROWS: list[str] = []

    def apply_open_interest(self, raw, *, generation=None):
        if raw.startswith("001") and self.ROWS:
            for row in self.ROWS:
                super().apply_open_interest(row, generation=generation)
            return "position"
        return super().apply_open_interest(raw, generation=generation)


def _deploy(monkeypatch, tmp_path, *, rows, opted_in, journals, mode="auto",
            dead=()):
    import tests.test_issue_202_predeploy_position as t202
    live = tmp_path / "data" / "live"
    live.mkdir(parents=True)
    for name, fills in journals.items():
        _journal(live / name, fills)
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    _HoldingMonitor.ROWS = rows
    monkeypatch.setattr(t202, "AccountMonitor", _HoldingMonitor)
    monkeypatch.setattr(rb.BacktestApp, "_is_shared_book_bot",
                        lambda self, name: opted_in)
    monkeypatch.setattr(rb, "_bot_process_alive",
                        lambda d: os.path.basename(d) not in dead)
    return t202._run_gate(monkeypatch, mode=mode, policy=hl.HeadlessPolicy(),
                          code=0, answer_snapshot=True)


B_LONG_ROW = "TF,ACCT,TMFL6,B,1,0,22500.0,15,2,ID"


def test_deploy_skips_prompt_when_other_bots_log_the_lot(monkeypatch, tmp_path):
    gate = _deploy(monkeypatch, tmp_path, rows=[B_LONG_ROW], opted_in=True,
                   journals={"TMF00_other": ["BUY"]})
    assert gate.app._account_monitor.has_open_positions()
    assert hl.TITLE_EXISTING_POSITION not in [t for t, _d in gate.confirms]
    assert gate.continued                            # pre-fix: refused here
    assert gate.exit_code is None


def test_deploy_prompts_when_the_logging_bot_is_dead(monkeypatch, tmp_path):
    """A stopped bot's journal is stale or names an unmanaged lot."""
    gate = _deploy(monkeypatch, tmp_path, rows=[B_LONG_ROW], opted_in=True,
                   journals={"TMF00_other": ["BUY"]}, dead=("TMF00_other",))
    assert [t for t, _d in gate.confirms] == [hl.TITLE_EXISTING_POSITION]
    assert gate.continued == []


def test_deploy_still_prompts_for_an_unlogged_lot(monkeypatch, tmp_path):
    gate = _deploy(monkeypatch, tmp_path, rows=[B_LONG_ROW], opted_in=True,
                   journals={})
    assert [t for t, _d in gate.confirms] == [hl.TITLE_EXISTING_POSITION]
    assert gate.continued == []
    assert gate.exit_code == hl.EXIT_DEPLOY_REFUSED


def test_new_deploy_over_own_logged_lot_still_prompts(monkeypatch, tmp_path):
    """``--new`` over a dir whose old journal still holds the lot: this
    process would not manage it, so it is an orphan as before."""
    gate = _deploy(monkeypatch, tmp_path, rows=[B_LONG_ROW], opted_in=True,
                   journals={"TMF00_issue202": ["BUY"]})
    assert [t for t, _d in gate.confirms] == [hl.TITLE_EXISTING_POSITION]
    assert gate.continued == []


def test_deploy_not_opted_in_keeps_master_prompt(monkeypatch, tmp_path):
    gate = _deploy(monkeypatch, tmp_path, rows=[B_LONG_ROW], opted_in=False,
                   journals={"TMF00_other": ["BUY"]})
    assert [t for t, _d in gate.confirms] == [hl.TITLE_EXISTING_POSITION]
    assert gate.continued == []


# ── path 2: resume reconcile ──

def _resume_app(monkeypatch, tmp_path, *, opted_in, rows, journals, sim_side="LONG",
                received=True):
    from tests.test_issue_168_restore_fidelity import _enable_fill_com, _resume_guard_app
    live = tmp_path / "live"
    live.mkdir()
    for name, fills in journals.items():
        _journal(live / name, fills)
    app = _resume_guard_app(position_size=1)
    app._live_runner.symbol = "TMF00"
    app._live_runner.bot_dir = str(live / "TMF00_me")
    app._live_runner.broker.position_side = SimpleNamespace(value=sim_side)
    app._live_runner._shared_book_opt_in = opted_in
    net = sb.account_lots(rows, "TM", FUTURES_OI_MARKET) or 0
    app._account_monitor = SimpleNamespace(
        positions=rows, oi_snapshot_received=received,
        get_signed_position=lambda prefix, market=None: net,
        oi_query_generation=1)
    app._position_ledger = None
    monkeypatch.setattr(rb, "_bot_process_alive", lambda d: True)
    _enable_fill_com(monkeypatch)
    monkeypatch.setattr(rb, "_discord", None)
    monkeypatch.setattr(rb, "_log", lambda msg: None)
    return app


def test_resume_does_not_adopt_another_bots_same_side_lot(monkeypatch, tmp_path):
    """Sim LONG, own log flat, bot B long 1. Master confirmed against B's
    lot; A's exits would then offset it."""
    app = _resume_app(monkeypatch, tmp_path, opted_in=True, rows=[_row("B")],
                      journals={"TMF00_other": ["BUY"]})
    app._reconcile_resumed_guard({}, "auto", "TMF00")
    assert app._trading_guard.real_entry_confirmed is False


def test_resume_confirms_own_lot_under_an_opposite_net(monkeypatch, tmp_path):
    """A long 1 (own log), B short 2 → net -1. Master read "opposite" and
    left A's own lot unmanaged."""
    app = _resume_app(monkeypatch, tmp_path, opted_in=True,
                      rows=[_row("S", 1)],
                      journals={"TMF00_me": ["BUY"],
                                "TMF00_b": ["SELL", "SELL"]})
    assert sb.fleet_positions(str(tmp_path / "live"), "TM") == {
        "TMF00_me": 1, "TMF00_b": -2}
    app._reconcile_resumed_guard({}, "auto", "TMF00")
    assert app._trading_guard.real_entry_confirmed is True


def test_resume_unaccounted_book_is_unknown(monkeypatch, tmp_path):
    app = _resume_app(monkeypatch, tmp_path, opted_in=True, rows=[_row("B", 2)],
                      journals={"TMF00_me": ["BUY"]})
    app._reconcile_resumed_guard({}, "auto", "TMF00")
    assert app._trading_guard.real_entry_confirmed is False
    assert app._resume_book_check("TMF00", "TM", "LONG") == (False, "unknown")


def test_resume_without_snapshot_is_unknown(monkeypatch, tmp_path):
    app = _resume_app(monkeypatch, tmp_path, opted_in=True, rows=[],
                      journals={"TMF00_me": ["BUY"]}, received=False)
    assert app._resume_book_check("TMF00", "TM", "LONG") == (False, "unknown")


def test_resume_not_opted_in_is_master(monkeypatch, tmp_path):
    app = _resume_app(monkeypatch, tmp_path, opted_in=False, rows=[_row("B")],
                      journals={"TMF00_other": ["BUY"]})
    app._reconcile_resumed_guard({}, "auto", "TMF00")
    assert app._trading_guard.real_entry_confirmed is True   # net side match


# ── path 3: dashboard switch to auto ──

def _auto_app(monkeypatch, tmp_path, *, opted_in, rows, journals):
    live = tmp_path / "live"
    live.mkdir()
    for name, fills in journals.items():
        _journal(live / name, fills)
    refused, accepted = [], []
    tracker = SimpleNamespace(
        rights_seq_at_start=0, WAITING="w", IDLE="i", TIMEOUT="t", DONE="d",
        timeout_s=10.0, poll=lambda done: "d",
        finish=lambda: SimpleNamespace(request_id="r1", mode="auto", loss_limit=1000))
    monkeypatch.setattr(rb, "_bot_process_alive", lambda d: True)
    app = rb.BacktestApp.__new__(rb.BacktestApp)
    app._mode_request_tracker = lambda: tracker
    app._account_monitor = SimpleNamespace(
        positions=rows, oi_snapshot_received=True, rights_seq=1,
        has_open_positions=lambda: any(r.get("qty") for r in rows),
        compute_display=lambda: SimpleNamespace(valid=False))
    app._live_runner = SimpleNamespace(
        bot_dir=str(live / "TMF00_me"), symbol="TMF00",
        _shared_book_opt_in=opted_in)

    def _finish(bot_dir, rid, mode, status, reason, req):
        (refused if status == rb._mreq.STATUS_REFUSED else accepted).append(reason)
    app._finish_mode_request = _finish
    return app, refused


def test_auto_not_refused_for_other_bots_logged_lot(monkeypatch, tmp_path):
    app, refused = _auto_app(monkeypatch, tmp_path, opted_in=True, rows=[_row("B")],
                             journals={"TMF00_other": ["BUY"]})
    app._continue_auto_mode_request("botdir")
    # Passes the position gate; the next gate (equity unparseable here)
    # is what refuses this stub.
    assert refused and "帳戶權益數未知" in refused[0]


def test_auto_refused_for_unlogged_lot(monkeypatch, tmp_path):
    app, refused = _auto_app(monkeypatch, tmp_path, opted_in=True, rows=[_row("B")],
                             journals={})
    app._continue_auto_mode_request("botdir")
    assert refused and "未平倉部位" in refused[0] and "共用帳戶" in refused[0]


def test_auto_refused_when_own_log_holds_a_lot(monkeypatch, tmp_path):
    app, refused = _auto_app(monkeypatch, tmp_path, opted_in=True, rows=[_row("B")],
                             journals={"TMF00_me": ["BUY"]})
    app._continue_auto_mode_request("botdir")
    assert refused and "未平倉部位" in refused[0]


def test_auto_not_opted_in_is_master(monkeypatch, tmp_path):
    app, refused = _auto_app(monkeypatch, tmp_path, opted_in=False, rows=[_row("B")],
                             journals={"TMF00_other": ["BUY"]})
    app._continue_auto_mode_request("botdir")
    assert refused and "未平倉部位" in refused[0] and "共用帳戶" not in refused[0]
