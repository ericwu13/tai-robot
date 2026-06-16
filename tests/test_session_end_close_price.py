"""Regression tests for the session-end / stop force-close STALE PRICE bug (issue #61).

The bug: ``_check_session_end_close`` (and the two manual-stop/teardown paths)
force-closed at ``_aggregated_bars[-1].close`` — the strategy's native-timeframe
bar close, read at an arbitrary wall-clock moment (a 30s poll, or a manual stop)
that has nothing to do with a bar boundary. On a higher-timeframe strategy that
close can be many minutes stale, so the SIMULATED broker filled (and the
decision log / chart marker / real-order label showed) a price far from where the
REAL IOC-market order actually filled.

Incident (LONG TM0000, 2026-06-16 13:43, real money):
    aggregated bar close  = 45,879   <-- stale, what the bot force-closed at
    last 1-min bar close  ~ 45,790   (fresh)
    live tick / real fill = 45,778   (true market)
    => exit-price gap ~101 pts == the bulk of the "real loses ~100pts more
       than paper" divergence users reported.

The fix extracts a pure ``select_freshest_price(tick_price, bars_1m, agg_bars)``
helper (live tick > last 1-min close > last aggregated close > (0, "N/A")) and
routes every force-close through it, so paper fills at the same price the real
market gives. These tests pin the helper on the domain seam — no Tkinter GUI —
mirroring tests/test_session_end_close_direction.py.
"""

from collections import deque
from datetime import datetime

import pytest

from src.live.live_runner import select_freshest_price
from src.market_data.models import Bar
from src.backtest.broker import SimulatedBroker, BrokerContext, OrderSide


# ── Helpers ──

def _bar(close: int) -> Bar:
    """Minimal Bar fixture. select_freshest_price only reads ``[-1].close``,
    so OHLCV beyond ``close`` is filler."""
    return Bar("TM0000", datetime(2026, 6, 16, 13, 43), close, close, close, close, 1, 60)


def _bars(*closes: int) -> list[Bar]:
    return [_bar(c) for c in closes]


# ── Unit: the pure price-selection ladder ──

class TestSelectFreshestPrice:
    """Pin every rung of the (tick > 1m > agg > N/A) freshness ladder."""

    def test_tick_wins_when_present(self):
        """Live tick has top priority even when 1m/agg are present and differ."""
        assert select_freshest_price(45778, _bars(45790), _bars(45879)) == (45778, "即時 tick")

    def test_1m_fallback_when_no_tick(self):
        """No tick -> last 1-min bar close wins over the agg close."""
        assert select_freshest_price(0, _bars(45790), _bars(45879)) == (45790, "1m bar")

    def test_agg_fallback_when_no_1m(self):
        """No tick AND no 1-min bars -> only then is the agg close used."""
        assert select_freshest_price(0, deque(), _bars(45879)) == (45879, "agg bar")

    def test_na_when_all_empty(self):
        """Nothing available -> (0, "N/A") sentinel so the caller can fall back."""
        assert select_freshest_price(0, deque(), []) == (0, "N/A")

    def test_picks_last_element_not_first(self):
        """Uses bars[-1] (most recent), never bars[0]. Catches a [-1]->[0] regression."""
        assert select_freshest_price(0, _bars(45700, 45750, 45790), []) == (45790, "1m bar")

    def test_works_on_deque_and_list(self):
        """Accepts both the deque LiveRunner uses (_1m_bars) and a plain list."""
        assert select_freshest_price(0, deque(_bars(45790), maxlen=5000), []) == (45790, "1m bar")
        assert select_freshest_price(0, list(_bars(45790)), []) == (45790, "1m bar")

    def test_zero_scaled_tick_treated_as_no_tick(self):
        """A tick that scaled to 0 (falsy) is skipped to the 1m fallback —
        documents the truthiness contract inherited from _get_latest_price."""
        assert select_freshest_price(0, _bars(45790), _bars(45879)) == (45790, "1m bar")

    def test_agg_used_when_only_stale_available(self):
        """When NOTHING fresher exists, the agg close is the INTENDED last
        resort (not a bug). Guards against an over-correction that would
        return (0,"N/A") and skip the force-close entirely — the caller's
        ``if exit_price <= 0: exit_price = last_bar.close`` mirrors this."""
        assert select_freshest_price(0, deque(), _bars(45879)) == (45879, "agg bar")


# ── Regression: the fix returns the FRESH price, not the stale agg close ──

class TestIncidentRegression:
    """Data is chosen so the stale agg price (45,879) DIFFERS from the fresh
    price (45,778) — these fail against the known-bad code that read agg directly."""

    def test_returns_fresh_not_stale_with_tick(self):
        """The exact incident: agg=45,879 stale, tick/1m=45,778 fresh."""
        price, src = select_freshest_price(45778, _bars(45778), _bars(45879))
        assert price == 45778
        assert price != 45879          # known-bad code force-closed here
        assert src == "即時 tick"

    def test_returns_fresh_not_stale_1m_only_no_tick(self):
        """Tick lost/reset: the 1-min bar is still fresher than the agg bar."""
        price, src = select_freshest_price(0, _bars(45778), _bars(45879))
        assert price == 45778
        assert price != 45879
        assert src == "1m bar"

    @pytest.mark.parametrize("tick,c_1m,c_agg,expected_price,expected_src", [
        (45778, 45790, 45879, 45778, "即時 tick"),  # tick wins over stale agg
        (0,     45790, 45879, 45790, "1m bar"),     # 1m wins over stale agg
        (0,     None,  45879, 45879, "agg bar"),    # agg is the last resort
        (0,     None,  None,  0,     "N/A"),        # nothing available
    ])
    def test_priority_matrix(self, tick, c_1m, c_agg, expected_price, expected_src):
        """Full ladder in one table. The first two rows assert the fresh value
        over the stale 45,879 path, so they fail against the buggy code."""
        bars_1m = _bars(c_1m) if c_1m is not None else deque()
        agg_bars = _bars(c_agg) if c_agg is not None else []
        assert select_freshest_price(tick, bars_1m, agg_bars) == (expected_price, expected_src)


# ── End-to-end: buggy (agg) vs fixed (fresh) reproduces the paper-vs-real gap ──

class TestForceCloseUsesFreshPrice:
    """Build a real SimulatedBroker position (no GUI) and show the stale-agg
    force-close and the fresh force-close diverge — the issue's actual symptom."""

    @staticmethod
    def _long_broker_at(entry: int) -> SimulatedBroker:
        broker = SimulatedBroker(point_value=200)   # TXF/TM point value
        ctx = BrokerContext(broker)
        ctx.entry("current", OrderSide.LONG)
        broker.on_bar_close(0, entry)
        assert broker.position_size == 1
        assert broker.position_side == OrderSide.LONG
        return broker

    def test_buggy_agg_price_differs_from_fixed_fresh_price(self):
        """The buggy path (agg close) and the fixed path (fresh price) pick
        materially different prices in the incident scenario."""
        agg_bars = _bars(45879)              # what _aggregated_bars[-1] held
        bars_1m = _bars(45778)               # fresh
        tick = 45778                          # fresh

        buggy_price = agg_bars[-1].close                      # OLD code
        fixed_price, _ = select_freshest_price(tick, bars_1m, agg_bars)  # NEW code

        assert buggy_price == 45879
        assert fixed_price == 45778
        assert buggy_price != fixed_price
        assert abs(buggy_price - fixed_price) > 50           # ~101pt incident gap

        # Force-close two independent positions at each price; realized P&L diverges.
        paper = self._long_broker_at(45700)
        paper.force_close(paper._bar_index, buggy_price, "2026-06-16 13:43:00")
        real = self._long_broker_at(45700)
        real.force_close(real._bar_index, fixed_price, "2026-06-16 13:43:00")

        paper_pnl = paper.trades[-1].pnl     # LONG 45700->45879 = +179 * 200
        real_pnl = real.trades[-1].pnl       # LONG 45700->45778 =  +78 * 200
        assert paper_pnl != real_pnl
        assert real_pnl < paper_pnl          # real fills worse than paper claimed
        assert paper_pnl - real_pnl == (45879 - 45778) * 200  # the ~101pt * pv gap

    def test_force_close_fills_at_fresh_price(self):
        """The fix wires the fresh price all the way into the recorded fill —
        broker.force_close is not buggy; this pins that the CALLER passes the
        fresh value (45,778), not the stale agg close (45,879)."""
        broker = self._long_broker_at(45700)
        price, _ = select_freshest_price(45778, _bars(45778), _bars(45879))
        broker.force_close(broker._bar_index, price, "2026-06-16 13:43:00")
        assert broker.position_size == 0
        assert broker.trades[-1].exit_price == 45778

    def test_short_position_fresh_price_too(self):
        """Symmetry: a SHORT also force-closes at the fresh price (covers the
        other side of the book)."""
        broker = SimulatedBroker(point_value=200)
        ctx = BrokerContext(broker)
        ctx.entry("current", OrderSide.SHORT)
        broker.on_bar_close(0, 45900)
        assert broker.position_side == OrderSide.SHORT
        price, _ = select_freshest_price(45778, _bars(45778), _bars(45879))
        broker.force_close(broker._bar_index, price, "2026-06-16 13:43:00")
        assert broker.trades[-1].exit_price == 45778
        # SHORT 45900 -> 45778 = +122 * 200
        assert broker.trades[-1].pnl == (45900 - 45778) * 200
