"""Tests for the forced-entry news event strategies.

These strategies exist because setup-hunting legs don't trade runaway
news moves: the entry condition IS the external signal. So the contract
under test is narrow and strict — enter once, never again, and manage
risk with an ATR stop plus a time stop.
"""

from datetime import datetime, timedelta

from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.backtest.broker import SimulatedBroker, OrderSide
from src.strategy.examples.news_event import NewsEventLong, NewsEventShort


START = datetime(2026, 8, 4, 15, 0)


def make_bars(ohlc, start_dt=START):
    """1-min bars from (open, high, low, close) tuples."""
    return [
        Bar(symbol="TX00", dt=start_dt + timedelta(minutes=i),
            open=o, high=h, low=l, close=c, volume=100, interval=60)
        for i, (o, h, l, c) in enumerate(ohlc)
    ]


def flat_bars(n, price=20000, half_range=10, start_dt=START):
    """n identical bars: range 20, close == open → ATR settles at 20."""
    return make_bars(
        [(price, price + half_range, price - half_range, price)] * n, start_dt)


def feed(strategy, broker, store, bars, start_index=0):
    """Drive one bar cycle exactly like BacktestEngine.run()."""
    ctx = broker.context
    for i, bar in enumerate(bars):
        idx = start_index + i
        store.add_bar(bar)
        bar_dt = bar.dt.strftime("%Y-%m-%d %H:%M:%S")
        broker.on_bar_open(idx, bar.open, bar_dt)
        if idx > 0 or broker.position_size > 0:
            broker.check_exits(idx, bar.open, bar.high, bar.low, bar.close, bar_dt)
        old_exits = len(broker._pending_exits)
        strategy.on_bar(bar, store, ctx)
        if len(broker._pending_exits) > old_exits and broker.position_size > 0:
            broker.check_exits(idx, bar.open, bar.high, bar.low, bar.close, bar_dt)
        broker.on_bar_close(idx, bar.close, bar_dt)
    return start_index + len(bars)


def setup(cls=NewsEventShort, **kw):
    return cls(**kw), SimulatedBroker(point_value=200), DataStore(max_bars=2000)


# ── Warmup gate ────────────────────────────────────────────────────────

def test_no_entry_before_warmup_bars():
    strat, broker, store = setup(warmup_bars=15)
    feed(strat, broker, store, flat_bars(15))
    assert broker.position_size == 0
    assert strat._entered is False
    assert broker.trades == []


def test_entry_on_the_bar_after_warmup():
    strat, broker, store = setup(warmup_bars=15)
    feed(strat, broker, store, flat_bars(16))
    assert strat._entered is True
    assert broker.position_size == 1
    assert broker.position_side == OrderSide.SHORT


def test_long_variant_enters_long():
    strat, broker, store = setup(NewsEventLong, warmup_bars=15)
    feed(strat, broker, store, flat_bars(16))
    assert broker.position_side == OrderSide.LONG


def test_warmup_counts_history_already_in_the_store():
    """Live warmup bars land in the DataStore without on_bar() firing —
    an event deploy must still be able to enter on its FIRST live bar."""
    strat, broker, store = setup(warmup_bars=15)
    for bar in flat_bars(50):
        store.add_bar(bar)                     # history replay, no on_bar
    live = flat_bars(1, start_dt=START + timedelta(minutes=50))
    feed(strat, broker, store, live, start_index=50)
    assert broker.position_size == 1


# ── Stop distance ──────────────────────────────────────────────────────

def test_stop_uses_atr_when_available():
    strat, broker, store = setup(warmup_bars=15, atr_period=14, stop_atr_mult=2.5)
    feed(strat, broker, store, flat_bars(16, price=20000))
    # Constant TR of 20 → ATR 20 → distance 50, short stop sits ABOVE entry
    assert strat._stop_distance == 50.0
    assert strat._stop_price == 20050
    assert broker._pending_exits[0].stop == 20050


def test_stop_uses_fallback_when_atr_unavailable():
    """A news deploy can land on a nearly empty store — ATR(14) needs 15
    bars, so the entry must fall back to a flat point stop, not go naked."""
    strat, broker, store = setup(warmup_bars=3, atr_period=14,
                                 fallback_stop_points=150)
    feed(strat, broker, store, flat_bars(4, price=20000))
    assert broker.position_size == 1
    assert strat._stop_distance == 150.0
    assert strat._stop_price == 20150
    assert broker._pending_exits[0].stop == 20150


def test_long_and_short_stops_are_symmetric():
    short, sbroker, sstore = setup(NewsEventShort, warmup_bars=15, stop_atr_mult=2.5)
    long_, lbroker, lstore = setup(NewsEventLong, warmup_bars=15, stop_atr_mult=2.5)
    bars = flat_bars(16, price=20000)
    feed(short, sbroker, sstore, bars)
    feed(long_, lbroker, lstore, make_bars([(b.open, b.high, b.low, b.close)
                                            for b in bars]))
    assert short._stop_distance == long_._stop_distance
    assert short._stop_price - 20000 == 20000 - long_._stop_price


def test_stop_tracks_the_real_fill_price():
    """Once the broker confirms the real fill, the stop is measured from
    THERE (slippage-aware), not from the signal bar's close."""
    strat, broker, store = setup(warmup_bars=15, stop_atr_mult=2.5)
    idx = feed(strat, broker, store, flat_bars(16, price=20000))
    assert broker.try_set_real_entry_price(20030, broker.entry_bar_index)
    feed(strat, broker, store, flat_bars(1, start_dt=START + timedelta(minutes=16)),
         start_index=idx)
    assert strat._stop_price == 20080          # 20030 + 50


# ── One-shot: exactly one entry, ever ──────────────────────────────────

def test_exactly_one_entry_ever_after_stop_out():
    strat, broker, store = setup(warmup_bars=15, atr_period=14, stop_atr_mult=2.5)
    idx = feed(strat, broker, store, flat_bars(16, price=20000))
    assert broker.position_size == 1

    # Short entry at 20000, stop 20050 — rip the market up through it.
    spike = make_bars([(20000, 20200, 19990, 20180)],
                      start_dt=START + timedelta(minutes=16))
    idx = feed(strat, broker, store, spike, start_index=idx)
    assert broker.position_size == 0
    assert len(broker.trades) == 1

    # 500 more bars of anything must NOT produce a second trade.
    tail = flat_bars(500, price=20180, start_dt=START + timedelta(minutes=17))
    feed(strat, broker, store, tail, start_index=idx)
    assert len(broker.trades) == 1
    assert broker.position_size == 0
    assert broker._pending_entries == []
    assert broker._pending_exits == []


def test_no_reentry_after_time_stop_exit():
    strat, broker, store = setup(warmup_bars=2, time_stop_bars=3)
    idx = feed(strat, broker, store, flat_bars(3 + 3))
    assert len(broker.trades) == 1
    feed(strat, broker, store,
         flat_bars(100, start_dt=START + timedelta(minutes=6)), start_index=idx)
    assert len(broker.trades) == 1
    assert broker.position_size == 0


# ── Time stop ──────────────────────────────────────────────────────────

def test_time_stop_fires_after_n_bars_in_position():
    strat, broker, store = setup(warmup_bars=2, time_stop_bars=3,
                                 fallback_stop_points=5000)
    # bars 1-2 warm up, bar 3 enters (fills at its close), bars 4-6 are the
    # three bars in position → time stop closes on bar 6.
    feed(strat, broker, store, flat_bars(6))
    assert len(broker.trades) == 1
    trade = broker.trades[0]
    assert trade.exit_tag == "time_stop"
    assert trade.entry_bar_index == 2
    assert trade.exit_bar_index == 5


def test_time_stop_does_not_fire_early():
    strat, broker, store = setup(warmup_bars=2, time_stop_bars=3,
                                 fallback_stop_points=5000)
    feed(strat, broker, store, flat_bars(5))
    assert broker.trades == []
    assert broker.position_size == 1


def test_stop_wins_when_it_hits_before_the_time_stop():
    strat, broker, store = setup(warmup_bars=2, time_stop_bars=50,
                                 fallback_stop_points=100)
    idx = feed(strat, broker, store, flat_bars(3, price=20000))
    assert broker.position_size == 1
    spike = make_bars([(20000, 20300, 19990, 20250)],
                      start_dt=START + timedelta(minutes=3))
    feed(strat, broker, store, spike, start_index=idx)
    assert len(broker.trades) == 1
    assert broker.trades[0].exit_tag == "Exit NewsShort"
    assert broker.trades[0].exit_price == 20100     # the stop level


def test_long_stop_fires_on_a_drop():
    strat, broker, store = setup(NewsEventLong, warmup_bars=2,
                                 fallback_stop_points=100)
    idx = feed(strat, broker, store, flat_bars(3, price=20000))
    drop = make_bars([(20000, 20010, 19800, 19850)],
                     start_dt=START + timedelta(minutes=3))
    feed(strat, broker, store, drop, start_index=idx)
    assert len(broker.trades) == 1
    assert broker.trades[0].exit_price == 19900
    assert broker.trades[0].exit_tag == "Exit NewsLong"


# ── Interface conventions ──────────────────────────────────────────────

def test_timeframe_and_required_bars():
    strat = NewsEventShort()
    assert (strat.kline_type, strat.kline_minute) == (0, 1)
    assert strat.required_bars() == 15          # max(warmup_bars, atr_period + 1)
    assert NewsEventLong(warmup_bars=60).required_bars() == 60


def test_names_are_distinct():
    assert NewsEventShort().name == "NewsEventShort"
    assert NewsEventLong().name == "NewsEventLong"
