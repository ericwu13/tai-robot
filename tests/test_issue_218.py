"""Exit fill price from GetFulfillReport(1) (issue #218).

The format-1 rows below follow the production column layout. Account,
book, and seq fields are fake values in the same shape as the live
rows. Prices and timestamps are the six exits the issue reported.

These tests fail against the pre-fix parser: it read a 13-digit seq
from field 0 (the row says TF), side from field 15, and price from
field 19, so every match returned 0.
"""

from __future__ import annotations

import ast
import os
from datetime import datetime

from src.backtest.broker import OrderSide, SimulatedBroker, Trade
from src.live.discord_notify import DiscordNotifier
from src.live.fill_report import (
    FR_MIN_GAP_MS,
    FR_RETRY_WINDOW_MS,
    FillQuery,
    RealFillTracker,
    front_month_contract,
    is_fulfill_rate_limited,
    mask_new_data_row,
    match_fill_in_report,
    next_exit_fill_retry,
    parse_fulfill_report,
    product_family,
    report_row_key,
)
from scripts.dashboard import real_pnl, snapshot


def gfr(book="a1006", book_long="0000001006", date="20261030", time="041606",
        product="TMFK6", side="S", price="45906.0000", qty="1", nc="O",
        account="***0000"):
    """One format-1 row. Indices are the production layout."""
    f = [""] * 52
    f[0] = "TF"
    f[1] = "FUT"
    f[2] = "TAIFEX"
    f[3] = "F020000"
    f[4] = "000"
    f[5] = account
    f[6] = "0000000"
    f[7] = book
    f[8] = book_long
    f[9] = date
    f[10] = time
    f[11] = date
    f[12] = product
    f[13] = "FITM"
    f[14] = "202611"
    f[15] = "0"            # old side index — not the side
    f[16] = "0.0000"
    f[18] = "0"
    f[19] = "0"            # old price index — not the price
    f[20] = "0.0000"       # old qty index — not the qty
    f[21] = side
    f[24] = "3"
    f[25] = price
    f[26] = qty
    f[27] = nc
    f[28] = "B"
    f[29] = "y"
    return ",".join(f)


def _old_synthetic_row():
    """The pre-#218 fixture: 24 fields, seq at [0], price at [19]."""
    f = [""] * 24
    f[0] = "1000000000001"
    f[15] = "S"
    f[19] = "45081.000000"
    f[20] = "1"
    f[21] = "O"
    f[23] = "20260723"
    return ",".join(f)


# label, send, date, time, price, book, book_long, extra noise lines
_EXITS = [
    ("A", datetime(2026, 10, 28, 22, 1, 2), "20261028", "220102", 47101,
     "a1001", "0000001001", [
         gfr(book="b2001", book_long="0000002001", date="20261028",
             time="201612", side="B", nc="N", price="47130.0000"),
     ]),
    ("B", datetime(2026, 10, 29, 1, 31, 6), "20261029", "013107", 47164,
     "a1002", "0000001002", [
         gfr(book="b2002", book_long="0000002002", date="20261028",
             time="221600", side="B", nc="N", price="47110.0000"),
         gfr(book="a1001", book_long="0000001001", date="20261028",
             time="220102", price="47101.0000"),
     ]),
    ("C", datetime(2026, 10, 29, 3, 1, 7), "20261029", "030107", 47226,
     "a1003", "0000001003", [
         gfr(book="b2003", book_long="0000002003", date="20261029",
             time="014706", side="B", nc="N", price="47205.0000"),
         gfr(book="a1002", book_long="0000001002", date="20261029",
             time="013107", price="47164.0000"),
     ]),
    ("D", datetime(2026, 10, 29, 4, 31, 3), "20261029", "043103", 47024,
     "a1004", "0000001004", [
         gfr(book="b2004", book_long="0000002004", date="20261029",
             time="031607", side="B", nc="N", price="47217.0000"),
         gfr(book="a1003", book_long="0000001003", date="20261029",
             time="030107", price="47226.0000"),
     ]),
    ("E", datetime(2026, 10, 30, 1, 16, 8), "20261030", "011609", 46370,
     "a1005", "0000001005", [
         gfr(book="b2005", book_long="0000002005", date="20261029",
             time="231602", side="B", nc="N", price="46518.0000"),
         gfr(book="a1004", book_long="0000001004", date="20261029",
             time="043103", price="47024.0000"),
     ]),
    ("F", datetime(2026, 10, 30, 4, 16, 6), "20261030", "041606", 45906,
     "a1006", "0000001006", [
         gfr(book="b2006", book_long="0000002006", date="20261030",
             time="040101", side="B", nc="N", price="46457.0000"),
         gfr(book="a1005", book_long="0000001005", date="20261030",
             time="011609", price="46370.0000"),
     ]),
]


def _report(spec) -> str:
    _label, _send, date, clock, price, book, book_long, noise = spec
    own = gfr(book=book, book_long=book_long, date=date, time=clock,
              price=f"{price}.0000")
    return "\n".join([own, *noise])


def _query(send, product="TM0000", book="") -> FillQuery:
    return FillQuery(book_no=book, product=product, side="S", new_close="O",
                     send_time=send)


def test_parser_reads_price_side_and_qty_from_the_real_columns():
    row = parse_fulfill_report(_report(_EXITS[5]))[0]
    assert row.price == 45906.0
    assert row.side == "S"
    assert row.qty == 1
    assert row.new_close == "O"
    assert row.product == "TMFK6"
    assert row.book_no == "a1006"
    assert row.book_long == "0000001006"
    assert row.date == "20261030"
    assert row.time == "041606"


def test_six_exits_match_on_fallback_without_a_book_number():
    """No OnNewData book number. Each report still yields its own fill."""
    for spec in _EXITS:
        label, send, _date, _clock, price, *_rest = spec
        rows = parse_fulfill_report(_report(spec))
        match = match_fill_in_report(rows, _query(send))
        assert match.price == price, label
        assert match.reason == "fallback", label


def test_tmf00_order_symbol_matches_the_same_contract():
    spec = _EXITS[0]
    rows = parse_fulfill_report(_report(spec))
    match = match_fill_in_report(rows, _query(spec[1], product="TMF00"))
    assert match.price == 47101
    assert match.reason == "fallback"


def test_product_family_of_order_symbols_and_contracts():
    assert product_family("TM0000") == "TMF"
    assert product_family("TMF00") == "TMF"
    assert product_family("TMFK6") == "TMF"
    assert product_family("TXFK6") == "TXF"
    assert front_month_contract("TMF", datetime(2026, 10, 28, 22, 1, 2)) == "TMFK6"


def test_other_family_in_the_window_is_ignored():
    spec = _EXITS[5]
    foreign = gfr(book="c3001", book_long="0000003001", product="TXFK6",
                  price="11111.0000", time="041606", date="20261030")
    raw = foreign + "\n" + _report(spec)
    match = match_fill_in_report(parse_fulfill_report(raw), _query(spec[1]))
    assert match.price == 45906
    assert match.reason == "fallback"


def test_two_months_of_the_same_family_are_ambiguous():
    send = datetime(2026, 10, 30, 4, 16, 6)
    raw = "\n".join([
        gfr(book="a1006", price="45906.0000", product="TMFK6", time="041606"),
        gfr(book="a1007", book_long="0000001007", price="45800.0000",
            product="TMFJ6", time="041607"),
    ])
    match = match_fill_in_report(parse_fulfill_report(raw), _query(send))
    assert match.price == 0
    assert match.reason == "ambiguous"


def test_a_single_back_month_is_not_the_front_month():
    send = datetime(2026, 10, 30, 4, 16, 6)
    raw = gfr(product="TMFJ6", price="45800.0000", time="041606")
    match = match_fill_in_report(parse_fulfill_report(raw), _query(send))
    assert match.price == 0
    assert match.reason == "none"


def test_book_number_equality_picks_one_row_when_fallback_is_ambiguous():
    send = datetime(2026, 10, 30, 4, 16, 6)
    raw = "\n".join([
        gfr(book="a1006", price="45906.0000", time="041606"),
        gfr(book="a1007", book_long="0000001007", price="45850.0000",
            time="041607"),
    ])
    rows = parse_fulfill_report(raw)
    hit = match_fill_in_report(rows, _query(send, book="a1006"))
    assert hit.price == 45906 and hit.reason == "book"
    other = match_fill_in_report(rows, _query(send, book="a1007"))
    assert other.price == 45850 and other.reason == "book"
    # One digit off is not a match, and the two fallback rows are ambiguous.
    miss = match_fill_in_report(rows, _query(send, book="a1008"))
    assert miss.price == 0 and miss.reason == "ambiguous"


def test_accept_row_stores_the_book_number_used_for_the_match():
    send = datetime(2026, 10, 30, 4, 16, 6)
    tracker = RealFillTracker()
    tracker.register_order(
        "1000000000001", "exit", bar_index=10, sim_price=45928,
        product="TM0000", side="S", new_close="O", send_time=send)
    fields = [""] * 26
    fields[0] = "1000000000001"
    fields[2] = "N"
    fields[3] = "N"
    fields[10] = "a1006"
    assert tracker.on_new_data(",".join(fields)) is None
    assert tracker.book_for("1000000000001") == "a1006"
    raw = "\n".join([
        gfr(book="a1007", book_long="0000001007", price="45850.0000",
            time="041607"),
        gfr(book="a1006", price="45906.0000", time="041606"),
    ])
    match = tracker.match_report("1000000000001", parse_fulfill_report(raw))
    assert match.price == 45906 and match.reason == "book"


def test_fallback_still_matches_when_no_accept_row_arrived():
    spec = _EXITS[5]
    tracker = RealFillTracker()
    tracker.register_order(
        "1000000000006", "exit", bar_index=10, sim_price=45928,
        product="TM0000", side="S", new_close="O", send_time=spec[1])
    assert tracker.book_for("1000000000006") == ""
    match = tracker.match_report("1000000000006",
                                 parse_fulfill_report(_report(spec)))
    assert match.price == 45906 and match.reason == "fallback"


def test_fill_stamped_one_second_before_send_matches():
    send = datetime(2026, 10, 30, 4, 16, 6)
    raw = gfr(time="041605", price="45906.0000")
    match = match_fill_in_report(parse_fulfill_report(raw), _query(send))
    assert match.price == 45906 and match.reason == "fallback"


def test_window_lower_bound_is_two_seconds_before_send():
    send = datetime(2026, 10, 30, 4, 16, 6, 900000)
    inside = gfr(time="041604", price="45906.0000")  # send second minus 2s
    outside = gfr(time="041603", price="45906.0000")
    assert match_fill_in_report(
        parse_fulfill_report(inside), _query(send)).price == 45906
    assert match_fill_in_report(
        parse_fulfill_report(outside), _query(send)).price == 0


def test_window_upper_bound_is_ninety_seconds_after_send():
    send = datetime(2026, 10, 30, 4, 16, 6)
    inside = gfr(time="041736", price="45906.0000")  # 04:17:36 = send+90s
    outside = gfr(time="041737", price="45906.0000")
    assert match_fill_in_report(
        parse_fulfill_report(inside), _query(send)).price == 45906
    assert match_fill_in_report(
        parse_fulfill_report(outside), _query(send)).price == 0


def test_already_credited_row_is_excluded():
    send = datetime(2026, 10, 30, 4, 16, 6)
    raw = "\n".join([
        gfr(book="a1006", price="45906.0000", time="041606"),
        gfr(book="a1007", book_long="0000001007", price="45850.0000",
            time="041607"),
    ])
    rows = parse_fulfill_report(raw)
    assert match_fill_in_report(rows, _query(send)).reason == "ambiguous"
    credited = {report_row_key(rows[0])}
    match = match_fill_in_report(rows, _query(send), credited=credited)
    assert match.price == 45850 and match.reason == "fallback"
    both = credited | {report_row_key(rows[1])}
    assert match_fill_in_report(rows, _query(send), credited=both).price == 0


def test_old_synthetic_row_is_rejected():
    assert parse_fulfill_report(_old_synthetic_row()) == []
    assert parse_fulfill_report("") == []
    assert parse_fulfill_report(None) == []
    assert parse_fulfill_report("001,no data") == []


def test_m999_is_a_rate_limit_not_a_row():
    raw = "M999 有查詢尚未結束或前次查詢後尚未等候五秒，請稍後重新查詢"
    assert is_fulfill_rate_limited(raw)
    assert parse_fulfill_report(raw) == []


def test_retry_gap_is_five_and_a_half_seconds():
    delay, attempt, exhausted = next_exit_fill_retry(
        0, 0, rate_limited=False, matched=False)
    assert delay == 5500
    assert delay == FR_MIN_GAP_MS
    assert attempt == 1 and not exhausted


def test_m999_does_not_consume_an_attempt_and_the_window_still_ends():
    attempt = 0
    elapsed = 0
    exhausted = False
    for _ in range(30):
        delay, attempt, exhausted = next_exit_fill_retry(
            attempt, elapsed, rate_limited=True, matched=False)
        if exhausted:
            break
        assert delay == 5500
        elapsed += delay
    assert exhausted
    assert attempt == 0
    assert 80_000 <= elapsed <= FR_RETRY_WINDOW_MS


def test_retry_window_schedules_at_ninety_seconds_and_not_past_it():
    delay, _attempt, exhausted = next_exit_fill_retry(
        1, 84_500, rate_limited=False, matched=False)
    assert delay == 5500 and not exhausted
    delay, _attempt, exhausted = next_exit_fill_retry(
        1, 85_000, rate_limited=False, matched=False)
    assert delay is None and exhausted


def test_counted_attempt_budget_still_stops_a_chain_inside_the_window():
    delay, attempt, exhausted = next_exit_fill_retry(
        15, 1_000, rate_limited=False, matched=False)
    assert delay is None and exhausted and attempt == 16
    # The same spot, but M999, still has an attempt left.
    delay, attempt, exhausted = next_exit_fill_retry(
        15, 1_000, rate_limited=True, matched=False)
    assert delay == 5500 and not exhausted and attempt == 15


def test_exhaustion_marks_the_trade_and_round_trips():
    broker = SimulatedBroker(point_value=10)
    broker.trades.append(Trade(
        tag="Long", side=OrderSide.LONG, qty=1, entry_price=46000,
        exit_price=45928, entry_bar_index=1, exit_bar_index=2, pnl=-720,
        source="real", real_entry_price=46010))
    assert broker.mark_real_exit_estimated()
    trade = broker.trades[-1]
    assert trade.real_exit_estimated is True
    assert trade.real_exit_price == 0
    assert trade.exit_price == 45928
    restored = SimulatedBroker.from_dict(broker.to_dict())
    assert restored.trades[-1].real_exit_estimated is True
    assert restored.trades[-1].real_exit_price == 0
    # A late real fill clears the flag.
    assert broker.try_set_real_exit_price(45906, exit_bar_index=2, fill_dt="x")
    assert broker.trades[-1].real_exit_price == 45906
    assert broker.trades[-1].real_exit_estimated is False


def test_discord_unconfirmed_warning_is_distinct():
    notifier = DiscordNotifier("token", "channel", bot_name="bot", symbol="TMF00")
    sent = []
    notifier._send = lambda content: sent.append(content)
    notifier.exit_fill_unconfirmed("45928", strategy="DynamicExitPullbackStrategyV2")
    assert len(sent) == 1
    assert "出場成交價未確認" in sent[0]
    assert "Exit Fill Unconfirmed" in sent[0]
    assert "est." in sent[0]
    assert "45,928" in sent[0]
    assert "成交確認" not in sent[0]


def test_dashboard_shows_est_next_to_estimated_real_pnl():
    trade = {
        "side": "LONG", "qty": 1, "entry_price": 46000, "exit_price": 45928,
        "pnl": -720, "entry_dt": "2026-10-30 04:00:00",
        "exit_dt": "2026-10-30 04:16:00", "real_entry_price": 46010,
        "real_exit_price": 0, "real_entry_dt": "2026-10-30 04:00:02",
        "source": "real", "real_exit_estimated": True,
    }
    classified = real_pnl.classify(trade, 10)
    assert classified["status"] == "estimated"
    assert "est." in classified["reason"]
    assert "may still be open" not in classified["reason"]
    row = snapshot._trade_row(trade, point_value=10)
    assert row["real_exit_estimated"] is True
    assert row["real_status"] == "estimated"
    import scripts.dashboard.server as server
    html = open(os.path.join(os.path.dirname(server.__file__),
                             "static", "index.html"), encoding="utf-8").read()
    assert 't.real_exit_estimated ? " est."' in html


def test_mask_keeps_the_price_and_drops_the_account():
    raw = "1000000000001,TF,D,N,,,,,F0000000009999,,a1006,45906.000000"
    masked = mask_new_data_row(raw, "F0000000009999")
    assert "F0000000009999" not in masked
    assert "45906.000000" in masked
    assert "***" in masked


def _method_source(name: str) -> str:
    """Source of a BacktestApp method without importing the Tk module."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "run_backtest.py"), encoding="utf-8").read()
    tree = ast.parse(text)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "BacktestApp":
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == name:
                return ast.get_source_segment(text, item) or ""
    raise AssertionError(f"BacktestApp.{name} not found")


def test_retry_chain_is_wired_to_the_scheduler():
    start = _method_source("_start_exit_fill_retry")
    step = _method_source("_poll_exit_fill_step")
    assert "FR_MIN_GAP_MS" in start
    assert "next_exit_fill_retry" in step
    assert "mark_real_exit_estimated" in step
    assert "exit_fill_unconfirmed" in step
    assert "1000, 2000, 4000" not in step
    deal = _method_source("_on_new_data")
    assert "OnNewData D exit (masked)" in deal
