"""Real-order P&L on the dashboard (scripts/dashboard/real_pnl.py).

The cases come from the live bots' own trade history (2026-10-28):
pre-#92 exits recorded as day-average prices (off by 500-800 points but
plausible-looking), early rows holding a parsed field instead of a price
(exit "200" on a 45,000 market), clean post-#92 fills within tens of
points, trades with only one fill recorded, and paper trades that still
carry a late real price.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from scripts.dashboard import real_pnl, snapshot
from scripts.monitor.common import TZ_TPE
from tests.test_dashboard import NOW, _alive, make_bot

PV = 10                                   # TMF point value
DAY_START = datetime(2026, 10, 23, 15, 0, tzinfo=TZ_TPE)


def trade(side="SHORT", entry=45000, exit_=44900, real_entry=0, real_exit=0,
          entry_dt="2026-10-20 09:00:00", exit_dt="2026-10-20 13:43:00",
          real_entry_dt="", real_exit_dt="", source="real", qty=1):
    sign = 1 if side == "LONG" else -1
    return {"side": side, "qty": qty, "entry_price": entry, "exit_price": exit_,
            "pnl": (exit_ - entry) * sign * qty * PV, "entry_dt": entry_dt,
            "exit_dt": exit_dt, "real_entry_price": real_entry,
            "real_exit_price": real_exit, "real_entry_dt": real_entry_dt,
            "real_exit_dt": real_exit_dt, "source": source}


def test_clean_post_92_fills_give_real_pnl_and_slippage():
    t = trade(side="SHORT", entry=45000, exit_=44900, real_entry=44980, real_exit=44921,
              real_entry_dt="2026-10-20 09:00:02", real_exit_dt="2026-10-20 13:43:03")
    c = real_pnl.classify(t, PV)
    assert c["status"] == "real"
    assert c["real_pnl"] == (44980 - 44921) * PV          # short: entry - exit
    assert c["slippage"] == c["real_pnl"] - t["pnl"]       # 590 - 1000 = -410


def test_long_side_sign():
    t = trade(side="LONG", entry=45000, exit_=45100, real_entry=45010, real_exit=45090)
    assert real_pnl.classify(t, PV)["real_pnl"] == 80 * PV


def test_pre_92_day_average_exit_is_unreliable_even_when_plausible():
    t = trade(exit_dt="2026-06-27 04:58:00", real_exit_dt="2026-06-27 04:58:38",
              entry_dt="2026-06-26 15:01:00", real_entry_dt="2026-06-26 15:01:02",
              entry=45100, exit_=45012, real_entry=45104, real_exit=45811)
    c = real_pnl.classify(t, PV)
    assert c["status"] == "unreliable" and "#92" in c["reason"]
    assert c["real_pnl"] is None


def test_absurd_price_after_92_is_a_data_error_not_slippage():
    t = trade(side="LONG", entry=46572, exit_=46600, real_entry=46570, real_exit=200)
    c = real_pnl.classify(t, PV)
    assert c["status"] == "unreliable" and "implausible" in c["reason"]


def test_genuine_large_slippage_is_kept():
    """A 300-point gap fill is real money lost, not a data error."""
    t = trade(side="SHORT", entry=45000, exit_=44900, real_entry=45000, real_exit=45200)
    c = real_pnl.classify(t, PV)
    assert c["status"] == "real" and c["real_pnl"] == -200 * PV


def test_one_recorded_fill_is_an_estimate_with_the_sim_price():
    t = trade(side="SHORT", entry=45000, exit_=44900, real_entry=44990, real_exit=0)
    c = real_pnl.classify(t, PV)
    assert c["status"] == "estimated" and c["real_pnl"] == (44990 - 44900) * PV
    assert "exit" in c["reason"]


def test_paper_labelled_trade_with_a_real_fill_is_still_a_real_order():
    """The broker relabels a trade "paper" when its ENTRY confirmation was
    lost, even though the order reached the account. A recorded real exit
    proves it went out, so it must not vanish from the real figures."""
    t = trade(source="paper", side="SHORT", entry=45000, exit_=44900, real_exit=44950)
    c = real_pnl.classify(t, PV)
    assert c["status"] == "estimated" and c["real_pnl"] == (45000 - 44950) * PV
    assert real_pnl.classify(trade(source="paper"), PV)["status"] == ""
    s = real_pnl.summarize([t], PV, DAY_START)
    assert s is not None and s["trades"] == 1


def test_unknown_side_is_unreliable_not_short():
    t = trade(side="", real_entry=45000, real_exit=44950)
    c = real_pnl.classify(t, PV)
    assert c["status"] == "unreliable" and "side" in c["reason"]


def test_missing_exit_fill_says_the_position_may_be_open():
    c = real_pnl.classify(trade(real_entry=45000), PV)
    assert c["status"] == "estimated" and "may still be open" in c["reason"]


def test_pre_source_field_trades_count_by_their_prices():
    t = trade(source="", real_entry=45001, real_exit=44899)
    assert real_pnl.classify(t, PV)["status"] == "real"
    assert real_pnl.classify(trade(source=""), PV)["status"] == ""


def test_real_order_without_any_recorded_fill_is_missing():
    assert real_pnl.classify(trade(source="real"), PV)["status"] == "missing"


def test_fill_time_falls_back_to_the_trade_time():
    t = trade(real_entry=45001, real_exit=44899, real_entry_dt="", real_exit_dt="",
              exit_dt="2026-07-10 04:58:00", entry_dt="2026-07-09 15:00:00")
    assert real_pnl.classify(t, PV)["status"] == "unreliable"


def test_summary_totals_today_and_exclusions():
    trades = [
        trade(real_entry=45000, real_exit=44950, exit_dt="2026-10-20 13:43:00"),   # +500
        trade(real_entry=45000, real_exit=44900, exit_dt="2026-10-26 09:30:00",    # +1000, today
              entry_dt="2026-10-26 09:00:00"),
        trade(real_entry=45000, real_exit=0, exit_dt="2026-10-26 10:00:00",        # est +1000, today
              entry_dt="2026-10-26 09:40:00"),
        trade(real_entry=45000, real_exit=45811, exit_dt="2026-06-27 04:58:00",    # unreliable
              entry_dt="2026-06-26 15:00:00"),
        trade(source="real"),                                                      # missing
        trade(source="paper", real_exit=44000),                                    # real order, absurd exit
        trade(source="paper"),                                                     # not real
    ]
    s = real_pnl.summarize(trades, PV, DAY_START)
    assert s["pnl_total"] == 500 + 1000 + 1000
    assert (s["pnl_today"], s["trades_today"]) == (2000, 2)
    assert (s["trades"], s["estimated"], s["unreliable"], s["missing"]) == (3, 1, 2, 1)
    assert s["sim_pnl_same_trades"] == 3 * 1000
    assert s["slippage_total"] == s["pnl_total"] - s["sim_pnl_same_trades"]


def test_bot_that_never_placed_a_real_order_has_no_real_block():
    assert real_pnl.summarize([trade(source="paper")], PV, DAY_START) is None


def test_fleet_and_detail_carry_real_pnl(tmp_path):
    trades = [trade(real_entry=45000, real_exit=44950, exit_dt="2026-10-20 13:43:00"),
              trade(real_entry=45000, real_exit=200, exit_dt="2026-10-20 13:43:00")]
    d = make_bot(tmp_path, trades=trades)
    s = snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)
    assert s["real"]["pnl_total"] == 500 and s["real"]["unreliable"] == 1
    rows = snapshot.bot_detail(NOW, str(d), pid_alive_fn=_alive)["trades"]
    assert sorted(r["real_status"] for r in rows) == ["real", "unreliable"]
    json.dumps(rows)                                          # serialisable for the page


def test_paper_only_bot_shows_no_real_column_value(tmp_path):
    d = make_bot(tmp_path, trades=[trade(source="paper")])
    assert snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)["real"] is None
