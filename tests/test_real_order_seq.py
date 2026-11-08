"""The real-order seq stamped on each trade (entry_order_seq / exit_order_seq).

Incident shape (fleet audit, 2026-11): the broker relabels a trade "paper"
whenever its real entry fill was never confirmed, so a real order whose
confirmation was lost left no proof on the trade; and pre-#92 paper trades
carry a planted day-average exit price that made the dashboard call them
real. The seq from SendFutureOrderCLR, stamped when the order is accepted,
is proof that does not depend on either.
"""

from __future__ import annotations

from scripts.dashboard import real_pnl
from src.backtest.broker import Order, OrderSide, SimulatedBroker

PV = 10
SEQ_E = "2315600000001"
SEQ_X = "2315600000002"


def _open(broker, bar=5, price=45000, side=OrderSide.SHORT):
    broker.queue_entry(Order(tag="S", side=side, qty=1))
    broker.on_bar_close(bar_index=bar, close=price)


def test_entry_and_exit_seqs_land_on_the_trade():
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    assert b.note_real_order_sent("entry", SEQ_E, 5)
    b.force_close(9, 44900)
    assert b.note_real_order_sent("exit", SEQ_X, 9)
    t = b.trades[-1]
    assert (t.entry_order_seq, t.exit_order_seq) == (SEQ_E, SEQ_X)
    # No confirmed fill price: source is downgraded, the seq still proves it.
    assert t.source == "" or t.source == "paper"
    assert b.entry_order_seq == ""            # reset for the next trade


def test_stamp_guards_reject_stale_or_duplicate_writes():
    b = SimulatedBroker(point_value=PV)
    assert not b.note_real_order_sent("entry", SEQ_E, 5)      # flat
    assert not b.note_real_order_sent("exit", SEQ_X, 5)       # no trade yet
    _open(b, bar=5)
    assert not b.note_real_order_sent("entry", SEQ_E, 4)      # other bar
    assert not b.note_real_order_sent("entry", "  ", 5)       # empty seq
    assert b.note_real_order_sent("entry", SEQ_E, 5)
    assert not b.note_real_order_sent("entry", "2315600000009", 5)  # retry keeps first
    assert b.entry_order_seq == SEQ_E
    b.force_close(9, 44900)
    assert not b.note_real_order_sent("exit", SEQ_X, 8)       # previous trade's bar
    assert b.trades[-1].exit_order_seq == ""


def test_new_entry_never_inherits_the_previous_seq():
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.note_real_order_sent("entry", SEQ_E, 5)
    b.force_close(9, 44900)
    _open(b, bar=12)                          # paper entry: no order sent
    b.force_close(15, 44800)
    assert b.trades[-1].entry_order_seq == ""


def test_seqs_survive_a_session_round_trip():
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.note_real_order_sent("entry", SEQ_E, 5)
    b.force_close(9, 44900)
    b.note_real_order_sent("exit", SEQ_X, 9)
    _open(b, bar=12)
    b.note_real_order_sent("entry", "2315600000003", 12)
    r = SimulatedBroker.from_dict(b.to_dict())
    assert r.entry_order_seq == "2315600000003"
    assert (r.trades[0].entry_order_seq, r.trades[0].exit_order_seq) == (SEQ_E, SEQ_X)


def test_old_session_files_load_with_empty_seqs():
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    d = b.to_dict()
    d.pop("entry_order_seq")
    for t in d["trades"]:
        t.pop("entry_order_seq")
        t.pop("exit_order_seq")
    r = SimulatedBroker.from_dict(d)
    assert r.trades[0].entry_order_seq == "" and r.entry_order_seq == ""


def _row(**kw):
    base = {"side": "SHORT", "qty": 1, "entry_price": 45000, "exit_price": 44900,
            "pnl": 1000, "entry_dt": "2026-11-02 09:00:00",
            "exit_dt": "2026-11-02 13:43:00", "real_entry_price": 0,
            "real_exit_price": 0, "source": "paper"}
    base.update(kw)
    return base


def test_order_sent_with_lost_confirm_is_a_real_order_not_paper():
    # source downgraded to "paper", no price recorded, but the seq is there.
    t = _row(entry_order_seq=SEQ_E, exit_order_seq=SEQ_X, seq_tracked=True)
    assert real_pnl.is_real_order(t)
    assert real_pnl.classify(t, PV)["status"] == "missing"


def test_planted_price_on_a_stamped_paper_row_is_not_proof():
    t = _row(real_exit_price=44794, entry_order_seq="", exit_order_seq="",
             seq_tracked=True)
    assert not real_pnl.is_real_order(t)
    assert real_pnl.classify(t, PV)["status"] == ""


def test_legacy_rows_without_seq_keys_keep_the_price_rule():
    assert real_pnl.is_real_order(_row(real_exit_price=44921))
    assert not real_pnl.is_real_order(_row())


def test_legacy_real_trade_survives_a_save_by_the_new_build():
    """#258 review P2: to_dict writes the seq keys ("") on every trade, so
    key presence must not switch a legacy row to the seq rule. A real order
    whose entry confirm was lost (source "paper", real exit recorded) must
    still be a real order after from_dict -> to_dict."""
    legacy = {"point_value": PV, "trades": [{
        "tag": "S", "side": "SHORT", "qty": 1, "entry_price": 45000,
        "exit_price": 44900, "entry_bar_index": 1, "exit_bar_index": 4,
        "pnl": 1000, "entry_dt": "2026-08-20 09:46:00",
        "exit_dt": "2026-08-20 10:46:00", "real_exit_price": 44910,
        "real_exit_dt": "2026-08-20 10:46:02", "source": "paper"}]}
    assert real_pnl.is_real_order(legacy["trades"][0])
    saved = SimulatedBroker.from_dict(legacy).to_dict()["trades"][0]
    assert saved["entry_order_seq"] == "" and saved["seq_tracked"] is False
    assert real_pnl.is_real_order(saved)
    assert real_pnl.classify(saved, PV)["status"] == "estimated"


def test_new_build_trades_are_seq_tracked_and_round_trip():
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)
    r = SimulatedBroker.from_dict(b.to_dict())
    assert r.trades[0].seq_tracked is True


def test_position_restored_from_an_old_file_closes_untracked():
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    d = b.to_dict()
    d.pop("entry_seq_tracked")            # written by an older build
    r = SimulatedBroker.from_dict(d)
    r.force_close(9, 44900)
    assert r.trades[-1].seq_tracked is False


def test_stop_close_exit_never_lands_on_the_previous_paper_trade():
    """#258 review P2-1: _stop_live sends the real EXIT while the sim
    position is still open, with bar_idx read from trades[-1] (the previous
    trade). That seq belongs to the OPEN position."""
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.force_close(9, 44900)               # trade 1: paper, no orders
    _open(b, bar=12)
    assert b.note_real_order_sent("entry", SEQ_E, 12)
    caller_bar_idx = b.trades[-1].exit_bar_index      # what run_backtest passes
    assert b.note_real_order_sent("exit", SEQ_X, caller_bar_idx)
    assert b.trades[0].exit_order_seq == ""
    assert not real_pnl.is_real_order(b.to_dict()["trades"][0])
    b.force_close(15, 44800)              # the stop's sim force-close
    t2 = b.trades[-1]
    assert (t2.entry_order_seq, t2.exit_order_seq) == (SEQ_E, SEQ_X)
    assert not b.note_real_order_sent("exit", "2315600000009", 15)  # first wins


def test_pending_exit_seq_survives_a_save_before_the_sim_close():
    b = SimulatedBroker(point_value=PV)
    _open(b, bar=5)
    b.note_real_order_sent("exit", SEQ_X, -1)
    r = SimulatedBroker.from_dict(b.to_dict())
    r.force_close(9, 44900)
    assert r.trades[-1].exit_order_seq == SEQ_X
    _open(r, bar=12)
    r.force_close(15, 44800)
    assert r.trades[-1].exit_order_seq == ""        # never inherited
