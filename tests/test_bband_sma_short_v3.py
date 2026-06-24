"""Tests for BbandSmaShortV3 exit-timing improvements (issue #75).

Covers: trailing stop activation/ratchet, break-even move, bounce exit,
and interaction with the original BB-lower TP / fixed SL / EOD exits.
"""

from datetime import datetime, timedelta

import pytest

from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.backtest.broker import SimulatedBroker, OrderSide
from src.strategy.examples.bband_sma_short_v3 import BbandSmaShortV3


# ── helpers ──

BASE_PRICE = 46000


def _make_bar(dt, o, h, l, c, vol=1000):
    return Bar(symbol="TMF00", dt=dt, open=o, high=h, low=l, close=c,
               volume=vol, interval=3600)


def _flat_warmup(data_store, n=22, base=BASE_PRICE):
    """Seed with n flat bars at *base*.  BB bands will be very narrow."""
    dt = datetime(2026, 6, 20, 8, 45)
    for i in range(n):
        b = _make_bar(dt + timedelta(hours=i),
                      base, base + 10, base - 10, base)
        data_store.add_bar(b)


def _setup_short(strategy, data_store, broker, entry_price, sl_price):
    """Directly set up a short position, bypassing entry logic.

    This lets exit tests focus on exit behavior without being coupled to
    the specific warmup data needed to satisfy entry conditions.
    """
    # Add one more bar to keep data_store growing
    bar = _make_bar(datetime(2026, 6, 23, 10, 45),
                    entry_price - 20, entry_price + 50,
                    entry_price - 30, entry_price)
    data_store.add_bar(bar)

    # Manually fill the entry in the broker
    broker.position_size = 1
    broker.position_side = OrderSide.SHORT
    broker.entry_price = entry_price
    broker.entry_tag = "Short"
    broker.entry_bar_index = len(data_store) - 1
    broker._entry_dt = bar.dt.isoformat()

    # Set strategy state as _check_entry would
    strategy.stop_loss_price = sl_price
    strategy.lowest_low = entry_price - 30  # entry bar's low
    strategy.trail_active = False
    strategy.breakeven_applied = False


@pytest.fixture
def strategy():
    return BbandSmaShortV3(
        bb_period=20, bb_std=2.0, sma_period=20, atr_period=14,
        atr_sl_mult=0.1,
        trail_activate_pts=150,
        trail_atr_mult=1.5,
        breakeven_pts=100,
        bounce_pts=200,
    )


@pytest.fixture
def broker():
    return SimulatedBroker(point_value=50)


@pytest.fixture
def data_store():
    return DataStore(max_bars=200)


# ── entry ──

class TestEntry:
    def test_short_entry_above_bb_upper_and_sma(self, strategy, broker, data_store):
        """With flat warmup, a spike far above BB upper triggers entry."""
        _flat_warmup(data_store)
        # Flat warmup at 46000 → BB upper ≈ 46014. Spike to 46500.
        entry_price = BASE_PRICE + 500
        spike = _make_bar(datetime(2026, 6, 23, 10, 45),
                          entry_price - 20, entry_price + 50,
                          entry_price - 30, entry_price)
        data_store.add_bar(spike)
        strategy.on_bar(spike, data_store, broker.context)
        broker.on_bar_close(len(data_store) - 1, entry_price,
                            bar_dt=spike.dt.isoformat())

        assert broker.position_side == OrderSide.SHORT
        assert strategy.stop_loss_price > 0

    def test_no_entry_below_bb_upper(self, strategy, broker, data_store):
        _flat_warmup(data_store)
        mild_bar = _make_bar(datetime(2026, 6, 23, 10, 45),
                             BASE_PRICE, BASE_PRICE + 5,
                             BASE_PRICE - 5, BASE_PRICE)
        data_store.add_bar(mild_bar)
        strategy.on_bar(mild_bar, data_store, broker.context)
        assert broker.position_size == 0


# ── original exits (regression) ──

class TestOriginalExits:
    def test_tp_at_bb_lower(self, strategy, broker, data_store):
        """BB lower band touch should close with tag=TP."""
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        # BB lower for flat data at 46000 ≈ 45986. A bar with low=45900
        # is well below BB lower. Keep bar tight to avoid bounce trigger.
        tp_bar = _make_bar(datetime(2026, 6, 23, 11, 45),
                           BASE_PRICE - 50, BASE_PRICE,
                           BASE_PRICE - 100, BASE_PRICE - 70)
        data_store.add_bar(tp_bar)
        strategy.on_bar(tp_bar, data_store, broker.context)
        broker.on_bar_close(len(data_store) - 1, tp_bar.close,
                            bar_dt=tp_bar.dt.isoformat())

        assert broker.position_size == 0
        assert broker.trades[-1].exit_tag == "TP"

    def test_fixed_sl_hit(self, broker, data_store):
        """Fixed SL works when trailing/breakeven/bounce disabled."""
        strat = BbandSmaShortV3(
            bb_period=20, bb_std=2.0, sma_period=20, atr_period=14,
            atr_sl_mult=0.1,
            trail_activate_pts=99999,
            trail_atr_mult=1.5,
            breakeven_pts=99999,
            bounce_pts=99999,
        )
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100  # 46300
        _setup_short(strat, data_store, broker, entry_price, sl_price)

        # Bar that spikes above SL (high > 46300)
        sl_bar = _make_bar(datetime(2026, 6, 23, 11, 45),
                           entry_price + 20, sl_price + 50,
                           entry_price - 10, sl_price + 30)
        data_store.add_bar(sl_bar)
        strat.on_bar(sl_bar, data_store, broker.context)
        broker.on_bar_close(len(data_store) - 1, sl_bar.close,
                            bar_dt=sl_bar.dt.isoformat())

        assert broker.position_size == 0
        assert broker.trades[-1].exit_tag == "SL"

    def test_eod_exit(self, strategy, broker, data_store):
        """Session-end bar should close the position."""
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        # 12:45 is last 60-min bar of AM session.
        # Price stays between entry and SL, no TP/bounce trigger.
        mid = entry_price - 30
        eod_bar = _make_bar(datetime(2026, 6, 23, 12, 45),
                            mid, mid + 20, mid - 20, mid)
        data_store.add_bar(eod_bar)
        strategy.on_bar(eod_bar, data_store, broker.context)
        broker.on_bar_close(len(data_store) - 1, eod_bar.close,
                            bar_dt=eod_bar.dt.isoformat())

        assert broker.position_size == 0
        assert broker.trades[-1].exit_tag == "EOD"


# ── break-even move ──

class TestBreakeven:
    def test_sl_moves_to_entry_after_breakeven_pts(self, strategy, broker, data_store):
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        # Price drops 120 points (> breakeven_pts=100)
        target = entry_price - 120
        drop_bar = _make_bar(datetime(2026, 6, 23, 11, 45),
                             entry_price - 30, entry_price - 10,
                             target, target + 20)
        data_store.add_bar(drop_bar)
        strategy.on_bar(drop_bar, data_store, broker.context)

        assert strategy.breakeven_applied is True
        assert strategy.stop_loss_price == entry_price

    def test_breakeven_not_applied_below_threshold(self, strategy, broker, data_store):
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        # Price drops only 50 points (< breakeven_pts=100).
        # lowest_low starts at entry_price - 30 from _setup_short.
        # Bar low must bring total drop (entry - lowest_low) below 100.
        drop_bar = _make_bar(datetime(2026, 6, 23, 11, 45),
                             entry_price - 10, entry_price + 5,
                             entry_price - 80, entry_price - 40)
        data_store.add_bar(drop_bar)
        strategy.on_bar(drop_bar, data_store, broker.context)

        # favorable_move = entry(46200) - lowest_low(46120) = 80 < 100
        assert strategy.breakeven_applied is False


# ── trailing stop ──

class TestTrailingStop:
    def test_trailing_activates_after_threshold(self, strategy, broker, data_store):
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        assert strategy.trail_active is False

        # Price drops 200 points (> trail_activate_pts=150)
        target = entry_price - 200
        drop_bar = _make_bar(datetime(2026, 6, 23, 11, 45),
                             entry_price - 50, entry_price - 20,
                             target, target + 30)
        data_store.add_bar(drop_bar)
        strategy.on_bar(drop_bar, data_store, broker.context)

        assert strategy.trail_active is True

    def test_trailing_stop_ratchets_down(self, strategy, broker, data_store):
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        # Bar 1: activate trailing (drop 200 pts)
        bar1 = _make_bar(datetime(2026, 6, 23, 11, 45),
                         entry_price - 50, entry_price - 20,
                         entry_price - 200, entry_price - 180)
        data_store.add_bar(bar1)
        strategy.on_bar(bar1, data_store, broker.context)
        sl_after_1 = strategy.stop_loss_price

        # Bar 2: price drops further
        bar2 = _make_bar(datetime(2026, 6, 23, 15, 0),
                         entry_price - 180, entry_price - 160,
                         entry_price - 350, entry_price - 320)
        data_store.add_bar(bar2)
        strategy.on_bar(bar2, data_store, broker.context)
        sl_after_2 = strategy.stop_loss_price

        assert sl_after_2 < sl_after_1

    def test_trailing_stop_does_not_widen(self, strategy, broker, data_store):
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        # Big drop: activate trailing
        bar1 = _make_bar(datetime(2026, 6, 23, 11, 45),
                         entry_price - 50, entry_price - 20,
                         entry_price - 300, entry_price - 260)
        data_store.add_bar(bar1)
        strategy.on_bar(bar1, data_store, broker.context)
        sl_tight = strategy.stop_loss_price

        # Slight further drop (but no new low)
        bar2 = _make_bar(datetime(2026, 6, 23, 15, 0),
                         entry_price - 260, entry_price - 200,
                         entry_price - 280, entry_price - 230)
        data_store.add_bar(bar2)
        strategy.on_bar(bar2, data_store, broker.context)
        sl_after = strategy.stop_loss_price

        assert sl_after <= sl_tight

    def test_trail_sl_exit_tag(self, strategy, broker, data_store):
        """When trailing stop is active and hit, exit tag is TrailSL."""
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        # Activate trailing with a big drop
        bar1 = _make_bar(datetime(2026, 6, 23, 11, 45),
                         entry_price - 50, entry_price - 20,
                         entry_price - 200, entry_price - 180)
        data_store.add_bar(bar1)
        strategy.on_bar(bar1, data_store, broker.context)
        assert strategy.trail_active is True
        sl = strategy.stop_loss_price

        # Bar that spikes above trailing SL
        hit_bar = _make_bar(datetime(2026, 6, 23, 15, 0),
                            sl - 50, sl + 50, sl - 70, sl + 20)
        data_store.add_bar(hit_bar)
        strategy.on_bar(hit_bar, data_store, broker.context)
        broker.on_bar_close(len(data_store) - 1, hit_bar.close,
                            bar_dt=hit_bar.dt.isoformat())

        assert broker.position_size == 0
        assert broker.trades[-1].exit_tag == "TrailSL"


# ── bounce exit ──

class TestBounceExit:
    def test_bounce_exit_triggers(self, strategy, broker, data_store):
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        # Drop to set lowest_low well below entry (stay above BB lower)
        drop_bar = _make_bar(datetime(2026, 6, 23, 11, 45),
                             entry_price - 50, entry_price - 20,
                             entry_price - 250, entry_price - 200)
        data_store.add_bar(drop_bar)
        strategy.on_bar(drop_bar, data_store, broker.context)

        lowest = strategy.lowest_low

        # Bounce bar: high bounces 220+ points from lowest_low.
        # Keep high below SL so bounce fires (not SL).
        bounce_high = lowest + 220
        assert bounce_high < sl_price, "test setup: bounce should fire before SL"
        bounce_bar = _make_bar(datetime(2026, 6, 23, 15, 0),
                               lowest + 100, bounce_high,
                               lowest + 50, lowest + 180)
        data_store.add_bar(bounce_bar)
        strategy.on_bar(bounce_bar, data_store, broker.context)
        broker.on_bar_close(len(data_store) - 1, bounce_bar.close,
                            bar_dt=bounce_bar.dt.isoformat())

        assert broker.position_size == 0
        assert broker.trades[-1].exit_tag == "Bounce"

    def test_no_bounce_exit_below_threshold(self, strategy, broker, data_store):
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        # Drop to set lowest_low
        drop_bar = _make_bar(datetime(2026, 6, 23, 11, 45),
                             entry_price - 50, entry_price - 20,
                             entry_price - 250, entry_price - 200)
        data_store.add_bar(drop_bar)
        strategy.on_bar(drop_bar, data_store, broker.context)

        lowest = strategy.lowest_low

        # Small bounce: only 100 points (< bounce_pts=200)
        small_bounce = _make_bar(datetime(2026, 6, 23, 15, 0),
                                 lowest + 50, lowest + 100,
                                 lowest + 20, lowest + 80)
        data_store.add_bar(small_bounce)
        strategy.on_bar(small_bounce, data_store, broker.context)

        assert broker.position_size > 0


# ── exit_levels ──

class TestExitLevels:
    def test_exit_levels_returns_stop(self, strategy, broker, data_store):
        _flat_warmup(data_store)
        _setup_short(strategy, data_store, broker, BASE_PRICE + 200,
                     BASE_PRICE + 300)
        levels = strategy.exit_levels()
        assert "stop" in levels
        assert levels["stop"] == strategy.stop_loss_price

    def test_exit_levels_empty_when_flat(self, strategy):
        assert strategy.exit_levels() == {}


# ── state reset ──

class TestStateReset:
    def test_state_resets_on_flat(self, strategy, broker, data_store):
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        # EOD exit
        mid = entry_price - 30
        eod_bar = _make_bar(datetime(2026, 6, 23, 12, 45),
                            mid, mid + 20, mid - 20, mid)
        data_store.add_bar(eod_bar)
        strategy.on_bar(eod_bar, data_store, broker.context)
        broker.on_bar_close(len(data_store) - 1, eod_bar.close,
                            bar_dt=eod_bar.dt.isoformat())
        assert broker.position_size == 0

        # Feed a bar while flat
        flat_bar = _make_bar(datetime(2026, 6, 23, 15, 0),
                             BASE_PRICE, BASE_PRICE + 5,
                             BASE_PRICE - 5, BASE_PRICE)
        data_store.add_bar(flat_bar)
        strategy.on_bar(flat_bar, data_store, broker.context)

        assert strategy.stop_loss_price == 0
        assert strategy.trail_active is False
        assert strategy.breakeven_applied is False
        assert strategy.lowest_low == 0


# ── exit priority ──

class TestExitPriority:
    def test_tp_takes_priority_over_bounce(self, strategy, broker, data_store):
        """TP (BB lower) should fire before bounce check."""
        _flat_warmup(data_store)
        entry_price = BASE_PRICE + 200
        sl_price = entry_price + 100
        _setup_short(strategy, data_store, broker, entry_price, sl_price)

        # Bar with low well below BB lower (TP) AND high that triggers bounce.
        # For flat data at 46000, BB lower ≈ 45986.
        # Set low=45800 (triggers TP). high=46100 (bounce from lowest_low
        # would need high - lowest_low >= 200; lowest_low starts at ~46170,
        # so 46100 - 45800 = 300 >= 200 would trigger bounce — but TP is
        # checked FIRST).
        low_val = BASE_PRICE - 200
        combo_bar = _make_bar(datetime(2026, 6, 23, 11, 45),
                              low_val + 100, low_val + 250,
                              low_val, low_val + 80)
        data_store.add_bar(combo_bar)
        strategy.on_bar(combo_bar, data_store, broker.context)
        broker.on_bar_close(len(data_store) - 1, combo_bar.close,
                            bar_dt=combo_bar.dt.isoformat())

        assert broker.position_size == 0
        assert broker.trades[-1].exit_tag == "TP"
