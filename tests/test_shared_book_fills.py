"""Shared book: several order sources on one product, one account.

The OpenInterest book has one net row per product, so it proves this
bot's fill only while the bot holds that whole row. One rule:

* the account net may confirm this bot's order only when it equals the
  bot's own position (``PositionLedger``) and the bot is not opted in to
  ``trading.shared_book_opt_in``;
* otherwise only the order's own seq-matched deal row confirms, and a
  timeout assumes nothing: real orders halt, position unknown.

A bot alone on its product, or with the net unknown / own fills still
pending, keeps the master behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import run_backtest as rb
from tests.test_fill_timeout_restore import SEQ, _deal, _make_app


class _Ledger:
    def __init__(self, position=0, pending=0):
        self.position = position
        self.pending_count = pending


class _Monitor:
    """OI book stand-in: ``net`` is the account's one row for TM."""

    def __init__(self, net=0, received=True):
        self.net = net
        self.oi_snapshot_received = received
        self.oi_query_generation = 1
        self.rights = None
        self.positions = []
        self.prefixes = []

    def get_signed_position(self, prefix, market=None):
        self.prefixes.append((prefix, market))
        return self.net

    def update_fill_poll_position(self, raw, parsed, prefix):
        return self.net


class _Discord:
    def __init__(self):
        self.enabled = True
        self.unknown = []
        self.downgrades = []

    def fill_timeout_position_unknown(self, action_type, timeout_s, order_seq):
        self.unknown.append((action_type, timeout_s, order_seq))

    def fill_timeout_downgrade(self, action_type, timeout_s):
        self.downgrades.append((action_type, timeout_s))

    def fill_timeout_restored(self, *_a):
        raise AssertionError("a halted shared-book timeout must not restore")


def _app(monkeypatch, *, net, own=0, pending=0, received=True, opt_in=False):
    app = _make_app(monkeypatch)
    discord = _Discord()
    monkeypatch.setattr(rb, "_discord", discord)
    monkeypatch.setattr(rb, "_com_available", True)
    monkeypatch.setattr(rb, "skO", object())
    app._discord_spy = discord
    app._futures_account = "F0000000"
    app._account_monitor = _Monitor(net=net, received=received)
    app._position_ledger = _Ledger(position=own, pending=pending)
    app._live_runner._shared_book_opt_in = opt_in
    app._confirms = []
    app._on_fill_confirmed = lambda: app._confirms.append(app._fill_poller.action_type)
    return app


def _send(app, action, seq=SEQ):
    """The auto-mode order path after SendFutureOrderCLR returned OK."""
    app._trading_guard.on_fill_pending(action)
    app._start_fill_polling(action, order_seq=seq)


def _oi(app):
    app._update_fill_poll_position("raw", {"product": "TMF", "qty": 1, "side": "B"})


# ── the reason ──

@pytest.mark.parametrize("net, own, pending, received, opt_in, shared", [
    (0, 0, 0, True, False, False),     # alone and flat
    (1, 1, 0, True, False, False),     # alone, own lot = whole net
    (1, 0, 0, True, False, True),      # another bot holds +1
    (0, 1, 0, True, False, True),      # own +1, another -1 → net 0
    (2, 1, 0, True, False, True),      # both long
    (1, 0, 1, True, False, False),     # own fill pending → net not comparable
    (1, 0, 0, False, False, False),    # no snapshot → net unknown
    (0, 0, 0, True, True, True),       # opted in: always shared
])
def test_shared_book_reason(monkeypatch, net, own, pending, received, opt_in, shared):
    app = _app(monkeypatch, net=net, own=own, pending=pending,
               received=received, opt_in=opt_in)
    assert bool(app._shared_book_reason()) is shared


def test_reason_reads_the_futures_book_for_this_product(monkeypatch):
    app = _app(monkeypatch, net=1, own=0)
    assert "net +1" in app._shared_book_reason()
    assert ("TM", rb.FUTURES_OI_MARKET) in app._account_monitor.prefixes


def test_no_ledger_or_monitor_is_master(monkeypatch):
    app = _app(monkeypatch, net=1, own=0)
    app._position_ledger = None
    assert app._shared_book_reason() == ""
    app._position_ledger = _Ledger()
    app._account_monitor = None
    assert app._shared_book_reason() == ""


def test_opt_in_must_be_boolean_true(monkeypatch):
    app = _app(monkeypatch, net=0, own=0, opt_in="yes")
    assert app._shared_book_reason() == ""


def test_broken_ledger_never_raises(monkeypatch):
    class _Bad:
        @property
        def pending_count(self):
            raise RuntimeError("boom")
    app = _app(monkeypatch, net=1, own=0)
    app._position_ledger = _Bad()
    assert app._shared_book_reason() == ""


# ── C2: another bot's lot must not confirm this bot's unfilled entry ──

def test_foreign_lot_does_not_confirm_entry_and_timeout_halts(monkeypatch):
    app = _app(monkeypatch, net=1, own=0)
    _send(app, "entry")
    assert app._fill_poller.shared_book is True
    assert any(a == "FILL_POLL_SHARED_BOOK" for a, _r in app._decisions)

    _oi(app)                                  # B's +1 arrives on the OI row
    assert app._confirms == []                # pre-fix: confirmed here
    assert app._trading_guard.real_entry_confirmed is False

    app._on_fill_timeout()
    guard = app._trading_guard
    assert guard.halted is True
    assert guard.real_entry_confirmed is False
    assert guard.fill_pending is False
    assert app._fill_timeout_restore_marker is None
    assert any(a == "FILL_TIMEOUT_POSITION_UNKNOWN" and SEQ in r
               for a, r in app._decisions)
    assert not any(a == "FILL_TIMEOUT_DOWNGRADE" for a, _r in app._decisions)
    assert app._discord_spy.unknown == [("entry", 10.0, SEQ)]
    assert app._discord_spy.downgrades == []
    assert app._trading_mode == "semi_auto"
    # Nothing can go out against B's lot afterwards.
    assert guard.decide("semi_auto", "TRADE_CLOSE", "LONG")[0] == guard.BLOCK_HALTED
    assert guard.decide("semi_auto", "FORCE_CLOSE", "LONG")[0] == guard.SKIP_EXIT


# ── C3: a flat net made of two opposite lots is not "exit already flat" ──

def test_opposite_lots_netting_to_zero_do_not_confirm_exit(monkeypatch):
    app = _app(monkeypatch, net=0, own=1)
    app._trading_guard.on_entry_sent()
    _send(app, "exit")
    assert app._confirms == []                # pre-fix: already_confirmed
    assert app._fill_poller.active


# ── the bot's own deal row still confirms on a shared book ──

def test_own_deal_row_confirms_on_shared_book(monkeypatch):
    app = _app(monkeypatch, net=1, own=0)
    _send(app, "entry")
    _deal(app, SEQ, "entry")
    assert app._confirms == ["entry"]


def test_other_seq_deal_row_does_not_confirm(monkeypatch):
    app = _app(monkeypatch, net=1, own=0)
    _send(app, "entry")
    # Bot B's deal row: never registered here, so the tracker ignores it.
    from tests.test_fill_report import make_deal_row
    app._on_new_data(make_deal_row(seq="2315699999999", price="39626.000000"))
    assert app._confirms == []


# ── alone on the product: master behaviour, byte for byte ──

def test_alone_net_confirms_entry_as_before(monkeypatch):
    app = _app(monkeypatch, net=0, own=0)
    _send(app, "entry")
    # getattr: this test also passes on pre-fix master (behaviour unchanged).
    assert getattr(app._fill_poller, "shared_book", False) is False
    assert not any(a == "FILL_POLL_SHARED_BOOK" for a, _r in app._decisions)
    app._account_monitor.net = 1
    _oi(app)
    assert app._confirms == ["entry"]


def test_alone_exit_already_flat_as_before(monkeypatch):
    app = _app(monkeypatch, net=0, own=0)
    _send(app, "exit")
    assert app._confirms == ["exit"]


def test_alone_timeout_still_assumes_fill_and_downgrades(monkeypatch):
    app = _app(monkeypatch, net=0, own=0)
    _send(app, "entry")
    app._on_fill_timeout()
    assert app._trading_guard.halted is False
    assert app._trading_guard.real_entry_confirmed is True
    assert app._discord_spy.downgrades == [("entry", 10.0)]
    assert app._discord_spy.unknown == []
    assert app._fill_timeout_restore_marker == (SEQ, "entry")


# ── settings ──

def test_settings_key_parsed_like_other_opt_ins(monkeypatch, tmp_path):
    from src.live import mode_request as mreq
    assert mreq.parse_dashboard_auto_opt_in(
        {"A": True, "B": "true", "C": 1, "D": False}) == frozenset({"A"})


def test_example_settings_opt_in_nobody():
    import os
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "settings.example.yaml"), encoding="utf-8") as fh:
        trading = yaml.safe_load(fh)["trading"]
    assert trading["shared_book_opt_in"] == {}


def test_load_settings_reads_shared_book_opt_in(monkeypatch, tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "trading:\n  shared_book_opt_in:\n    BotA: true\n    BotB: yes-ish\n",
        encoding="utf-8")
    monkeypatch.setattr(rb, "project_root", str(tmp_path))
    cfg = rb._load_settings()
    assert cfg["shared_book_opt_in"] == frozenset({"BotA"})
