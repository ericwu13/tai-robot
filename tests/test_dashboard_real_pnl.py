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


def test_today_real_excludes_the_estimated_trade_that_pnl_today_counts():
    """今日實單 is real-ONLY, so it must differ from ``pnl_today`` here.

    Same fixture as the totals test: today holds one clean real trade
    (+1000) and one estimate (+1000, exit price never recorded). The
    running 實單累計 counts both by design; a two-trade day would be half
    invented if the day column did the same.
    """
    trades = [
        trade(real_entry=45000, real_exit=44900, exit_dt="2026-10-26 09:30:00",
              entry_dt="2026-10-26 09:00:00"),                        # real, today
        trade(real_entry=45000, real_exit=0, exit_dt="2026-10-26 10:00:00",
              entry_dt="2026-10-26 09:40:00"),                        # estimated, today
    ]
    s = real_pnl.summarize(trades, PV, DAY_START)
    assert (s["pnl_today"], s["trades_today"]) == (2000, 2)
    assert (s["pnl_today_real"], s["trades_today_real"]) == (1000, 1)


@pytest.mark.parametrize("exit_dt, counted", [
    ("2026-10-23 15:00:00", True),    # exactly the night open — the day starts here
    ("2026-10-23 14:59:00", False),   # one minute before it
    ("2026-10-24 04:58:00", True),    # after midnight, still the same trading day
])
def test_today_window_starts_at_the_night_open(exit_dt, counted):
    s = real_pnl.summarize(
        [trade(real_entry=45000, real_exit=44900, exit_dt=exit_dt,
               entry_dt="2026-10-23 09:00:00")], PV, DAY_START)
    assert s["trades_today_real"] == (1 if counted else 0)
    assert s["pnl_today_real"] == (1000 if counted else 0)


def test_today_real_is_zero_when_only_untrusted_trades_closed_today():
    """The page renders 「—」 off ``trades_today_real``, not off the sum."""
    trades = [
        trade(real_entry=45000, real_exit=200, exit_dt="2026-10-26 09:30:00",
              entry_dt="2026-10-26 09:00:00"),                        # unreliable
        trade(source="real", exit_dt="2026-10-26 10:00:00",
              entry_dt="2026-10-26 09:40:00"),                        # missing
    ]
    s = real_pnl.summarize(trades, PV, DAY_START)
    assert (s["pnl_today_real"], s["trades_today_real"]) == (0, 0)


def test_fleet_row_carries_the_today_real_fields(tmp_path):
    d = make_bot(tmp_path, trades=[
        trade(real_entry=45000, real_exit=44900, exit_dt="2026-10-26 09:30:00",
              entry_dt="2026-10-26 09:00:00")])
    s = snapshot.bot_summary(NOW, str(d), pid_alive_fn=_alive)
    assert s["real"]["trades_today_real"] == 1
    json.dumps(s["real"])                                     # serialisable for the page


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


def _index_html() -> str:
    import os

    from scripts.dashboard import server
    return open(os.path.join(os.path.dirname(server.__file__), "static", "index.html"),
                encoding="utf-8").read()


def test_today_real_column_is_wired_into_the_fleet_row():
    html = _index_html()
    # 今日 is banned as a label (test_trading_day_pnl_label_is_ben_jiaoyi_ri):
    # the window is the TAIFEX trading day from the 15:00 night open, not a
    # calendar day, so the column reuses the existing 本交易日 wording.
    assert "今日" not in html
    assert ">本交易日實單</th>" in html, "fleet header is missing the column"
    assert 'td("本交易日實單", realDayCell(b.real), "num")' in html
    assert "pnl_today_real" in html and "trades_today_real" in html


def test_empty_fleet_colspan_matches_the_header_count():
    """Adding a column without bumping the colspan leaves a short empty row."""
    import re

    html = _index_html()
    start = html.index('<table id="fleet">')
    thead = html[start:html.index("</thead>", start)]
    columns = len(re.findall(r"<th[\s>]", thead))      # not <thead>
    assert columns == 13, thead
    span = re.search(r'colspan: "(\d+)", class: "empty" \}, "目前沒有機器人"', html)
    assert span and int(span.group(1)) == columns, (span and span.group(1), columns)


def test_trade_list_groups_today_separately():
    html = _index_html()
    assert "tradeRows(d)" in html, "detail trade table no longer uses the grouper"
    assert "本交易日（" in html and '"先前"' in html
    assert "tr.grp td" in html, "group header has no style"


def _group_trades_under_node(day_start, exit_dts):
    """Run the page's own day-partition over ``exit_dts``.

    ``bot_detail`` only reverses the stored list, so the grouper must not
    assume the history is sorted by close time. Counting matches and then
    slicing by that count put a 3-day-old trade under 本交易日 while the
    header count still read correctly — caught on the rendered page, so
    the partition is pinned here.
    """
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable — cannot execute the page grouper")
    html = _index_html()
    body = html[html.index("  tradeRows(d) {"):]
    body = body[:body.index("\n  },")]
    pick = re.search(r"(const inDay = [\s\S]*?const older = [^;]+;)", body)
    assert pick, body[:400]
    prog = (f"const dayStart = {json.dumps(day_start)};\n"
            f"const trades = JSON.parse(process.argv[1]);\n"
            f"{pick.group(1)}\n"
            "console.log(JSON.stringify({today: today.map(t => t.exit_dt),"
            " older: older.map(t => t.exit_dt)}));\n")
    run = subprocess.run([node, "-e", prog,
                          json.dumps([{"exit_dt": d} for d in exit_dts])],
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_day_grouping_survives_an_unsorted_history():
    out = _group_trades_under_node("2026-11-02 15:00", [
        "2026-10-30 15:00",    # older, but FIRST in the list
        "2026-11-02 18:00",    # today
        "2026-11-02 17:00",    # today
    ])
    assert out["today"] == ["2026-11-02 18:00", "2026-11-02 17:00"]
    assert out["older"] == ["2026-10-30 15:00"]


def test_day_grouping_boundary_is_the_night_open():
    out = _group_trades_under_node("2026-11-02 15:00", [
        "2026-11-02 15:00",    # exactly the open — counts
        "2026-11-02 14:59",    # one minute before — does not
        "2026-11-03 04:58",    # after midnight, same trading day
    ])
    assert out["today"] == ["2026-11-02 15:00", "2026-11-03 04:58"]
    assert out["older"] == ["2026-11-02 14:59"]
