"""Tests for OnNewData per-order fill tracking (issue #92).

The known-bad behavior these tests are designed against: real exit prices
used to come from GetFulfillReport(4), which merges fills by commodity into
a day-cumulative average — the second exit of the day was recorded at the
average of ALL the day's sells (44,794) instead of its own fill (45,081 /
44,622). The tracker must return each order's OWN deal price, matched by
seq no, and ignore fills from other bots on the same account.
"""

from src.backtest.broker import OrderSide, SimulatedBroker, Trade
from src.live.discord_notify import DiscordNotifier
from src.live.fill_report import RealFillTracker, parse_new_data


def make_row(seq="2315604562747", typ="D", err="N", product="TM0000",
             book="j0224", price="45081.000000", qty="1",
             date="20260723", time="04:16:01"):
    """Build a minimal OnNewData bstrData string (26 comma fields)."""
    f = [""] * 26
    f[0] = seq
    f[1] = "TF"
    f[2] = typ
    f[3] = err
    f[8] = product
    f[10] = book
    f[11] = price
    f[20] = qty
    f[23] = date
    f[24] = time
    return ",".join(f)


# ── parse_new_data ──

def test_parse_deal_row():
    row = parse_new_data(make_row())
    assert row is not None
    assert row.seq_no == "2315604562747"
    assert row.row_type == "D"
    assert row.product == "TM0000"
    assert row.price == 45081.0
    assert row.qty == 1
    assert row.is_fill


def test_parse_order_accepted_row_is_not_fill():
    row = parse_new_data(make_row(typ="N"))
    assert row is not None
    assert not row.is_fill


def test_parse_error_row_is_not_fill():
    row = parse_new_data(make_row(err="Y"))
    assert row is not None
    assert not row.is_fill


def test_parse_bad_price_is_not_fill():
    row = parse_new_data(make_row(price="garbage"))
    assert row is not None
    assert row.price == 0.0
    assert not row.is_fill


def test_parse_rejects_short_and_empty_input():
    assert parse_new_data("a,b,c") is None
    assert parse_new_data("") is None
    assert parse_new_data(None) is None
    assert parse_new_data(("tuple",)) is None


# ── RealFillTracker ──

def test_tracked_exit_fill_returns_own_price():
    t = RealFillTracker()
    t.register_order("2315604562747", "exit", bar_index=19024, sim_price=45062)
    fill = t.on_new_data(make_row(price="45081.000000"))
    assert fill is not None
    assert fill.action_type == "exit"
    assert fill.price == 45081       # actual deal price, NOT the sim 45062
    assert fill.bar_index == 19024
    assert fill.sim_price == 45062


def test_second_exit_of_day_gets_own_price_not_day_average():
    # The exact issue #92 scenario: two exits in one day. GetFulfillReport(4)
    # would report both at the merged average (44,794); the tracker must not.
    t = RealFillTracker()
    t.register_order("111", "exit", bar_index=100, sim_price=44500)
    t.register_order("222", "exit", bar_index=200, sim_price=45062)
    f1 = t.on_new_data(make_row(seq="111", price="44506.000000"))
    f2 = t.on_new_data(make_row(seq="222", price="45081.000000"))
    assert f1.price == 44506
    assert f2.price == 45081


def test_foreign_seq_is_ignored():
    # Another bot (or a manual order) on the same account fills — no match.
    t = RealFillTracker()
    t.register_order("111", "exit", bar_index=100, sim_price=44500)
    assert t.on_new_data(make_row(seq="999", price="44506.000000")) is None


def test_non_deal_rows_are_ignored_then_deal_matches():
    t = RealFillTracker()
    t.register_order("111", "exit", bar_index=100, sim_price=44500)
    assert t.on_new_data(make_row(seq="111", typ="N")) is None
    fill = t.on_new_data(make_row(seq="111", typ="D"))
    assert fill is not None and fill.price == 45081


def test_duplicate_deal_row_returns_none():
    t = RealFillTracker()
    t.register_order("111", "exit", bar_index=100, sim_price=44500)
    assert t.on_new_data(make_row(seq="111")) is not None
    assert t.on_new_data(make_row(seq="111")) is None


def test_entry_order_tracked_with_entry_action():
    t = RealFillTracker()
    t.register_order("333", "entry", bar_index=50, sim_price=44535)
    fill = t.on_new_data(make_row(seq="333", price="44536.000000"))
    assert fill.action_type == "entry"
    assert fill.price == 44536


def test_fill_for_lookup_and_reset():
    t = RealFillTracker()
    t.register_order("111", "exit", bar_index=100, sim_price=44500)
    assert t.fill_for("111") is None
    t.on_new_data(make_row(seq="111"))
    assert t.fill_for("111").price == 45081
    t.reset()
    assert t.fill_for("111") is None


def test_order_book_is_bounded():
    t = RealFillTracker()
    for i in range(40):
        t.register_order(str(i), "exit", bar_index=i, sim_price=0)
    # Oldest evicted, newest still tracked
    assert t.on_new_data(make_row(seq="0")) is None
    assert t.on_new_data(make_row(seq="39")) is not None


# ── Broker integration: guarded write with the tracked fill ──

def _closed_trade(exit_bar_index: int) -> Trade:
    return Trade(tag="Long", side=OrderSide.LONG, qty=1,
                 entry_price=44535, exit_price=45062,
                 entry_bar_index=19000, exit_bar_index=exit_bar_index,
                 pnl=5270)


def test_tracked_fill_writes_real_exit_price():
    broker = SimulatedBroker(point_value=10)
    broker.trades.append(_closed_trade(exit_bar_index=19024))
    t = RealFillTracker()
    t.register_order("111", "exit", bar_index=19024, sim_price=45062)
    fill = t.on_new_data(make_row(seq="111", price="45081.000000"))
    assert broker.try_set_real_exit_price(
        fill.price, exit_bar_index=fill.bar_index, fill_dt="2026-07-23 04:16:02")
    assert broker.trades[-1].real_exit_price == 45081


def test_late_fill_for_previous_trade_is_rejected():
    # A late ROD fill whose bar-index snapshot doesn't match the latest
    # trade must be dropped (issue #45 guard family).
    broker = SimulatedBroker(point_value=10)
    broker.trades.append(_closed_trade(exit_bar_index=20000))
    t = RealFillTracker()
    t.register_order("111", "exit", bar_index=19024, sim_price=45062)
    fill = t.on_new_data(make_row(seq="111"))
    assert not broker.try_set_real_exit_price(
        fill.price, exit_bar_index=fill.bar_index, fill_dt="")
    assert broker.trades[-1].real_exit_price == 0


# ── Discord correction message ──

def test_fill_price_correction_message_shape():
    n = DiscordNotifier("token", "channel", bot_name="0422", symbol="TMF00")
    sent = []
    n._send = lambda content: sent.append(content)
    n.fill_price_correction("exit", 44622, 44589,
                            strategy="AI: DynamicExitPullbackStrategyV2")
    assert len(sent) == 1
    msg = sent[0]
    assert "成交價更正 Fill Price Correction" in msg
    assert "(exit)" in msg
    assert "44,622" in msg          # the real price, prominent
    assert "44,589" in msg          # the previously-notified estimate
    assert "DynamicExitPullbackStrategyV2" in msg
