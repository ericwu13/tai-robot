"""Tests for BarAggregator: 1-min → N-min bar aggregation.

Bar boundaries use session-start alignment:
- AM session (08:45-13:45): epoch = 08:45
- Night session (15:00-05:00): epoch = 15:00
"""

from datetime import datetime, timedelta

from src.market_data.models import Bar
from src.market_data.sessions import session_align
from src.live.bar_aggregator import BarAggregator


def _bar(dt_str, o=100, h=110, l=90, c=105, v=10, interval=60):
    """Helper to create a 1-min bar."""
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    return Bar(symbol="TX00", dt=dt, open=o, high=h, low=l, close=c,
               volume=v, interval=interval)


# ── session_align() unit tests ──

class TestSessionAlign:
    """Direct tests for the session_align() function."""

    def test_1h_am_session_start(self):
        """First 1H bar of AM session starts at 08:45."""
        dt = datetime(2026, 3, 2, 8, 45)
        assert session_align(dt, 3600) == datetime(2026, 3, 2, 8, 45)

    def test_1h_am_mid_bar(self):
        """09:30 falls within the 08:45-09:45 bar."""
        dt = datetime(2026, 3, 2, 9, 30)
        assert session_align(dt, 3600) == datetime(2026, 3, 2, 8, 45)

    def test_1h_am_second_bar(self):
        """09:45 starts the second 1H bar."""
        dt = datetime(2026, 3, 2, 9, 45)
        assert session_align(dt, 3600) == datetime(2026, 3, 2, 9, 45)

    def test_1h_am_last_bar(self):
        """12:45 starts the last 1H bar of AM session (12:45-13:45)."""
        dt = datetime(2026, 3, 2, 12, 45)
        assert session_align(dt, 3600) == datetime(2026, 3, 2, 12, 45)

    def test_4h_am_first_bar(self):
        """4H AM: first bar covers 08:45-12:45."""
        dt = datetime(2026, 3, 2, 10, 0)
        assert session_align(dt, 14400) == datetime(2026, 3, 2, 8, 45)

    def test_4h_am_second_bar(self):
        """4H AM: second bar starts at 12:45 (shorter: 12:45-13:45)."""
        dt = datetime(2026, 3, 2, 12, 45)
        assert session_align(dt, 14400) == datetime(2026, 3, 2, 12, 45)

    def test_1h_night_session_start(self):
        """First 1H bar of night session starts at 15:00."""
        dt = datetime(2026, 3, 2, 15, 0)
        assert session_align(dt, 3600) == datetime(2026, 3, 2, 15, 0)

    def test_1h_night_second_bar(self):
        dt = datetime(2026, 3, 2, 16, 0)
        assert session_align(dt, 3600) == datetime(2026, 3, 2, 16, 0)

    def test_1h_night_after_midnight(self):
        """01:00 next day still belongs to night session (epoch=prev 15:00)."""
        dt = datetime(2026, 3, 3, 1, 0)
        assert session_align(dt, 3600) == datetime(2026, 3, 3, 1, 0)

    def test_1h_night_last_bar(self):
        """04:00 is the last 1H bar of night session (04:00-05:00)."""
        dt = datetime(2026, 3, 3, 4, 0)
        assert session_align(dt, 3600) == datetime(2026, 3, 3, 4, 0)

    def test_4h_night_first_bar(self):
        """4H Night: first bar 15:00-19:00."""
        dt = datetime(2026, 3, 2, 17, 30)
        assert session_align(dt, 14400) == datetime(2026, 3, 2, 15, 0)

    def test_4h_night_second_bar(self):
        """4H Night: second bar 19:00-23:00."""
        dt = datetime(2026, 3, 2, 20, 0)
        assert session_align(dt, 14400) == datetime(2026, 3, 2, 19, 0)

    def test_4h_night_third_bar(self):
        """4H Night: third bar 23:00-03:00 (crosses midnight)."""
        dt = datetime(2026, 3, 3, 1, 0)
        assert session_align(dt, 14400) == datetime(2026, 3, 2, 23, 0)

    def test_4h_night_fourth_bar(self):
        """4H Night: fourth bar 03:00-05:00 (shorter: 2 hours)."""
        dt = datetime(2026, 3, 3, 3, 30)
        assert session_align(dt, 14400) == datetime(2026, 3, 3, 3, 0)

    def test_1min_uses_midnight_alignment(self):
        """1-min bars use fast-path midnight alignment."""
        dt = datetime(2026, 3, 2, 9, 30)
        assert session_align(dt, 60) == datetime(2026, 3, 2, 9, 30)

    def test_15m_am_session(self):
        """15-min bars in AM session: 08:45, 09:00, 09:15, ..."""
        assert session_align(datetime(2026, 3, 2, 8, 45), 900) == datetime(2026, 3, 2, 8, 45)
        assert session_align(datetime(2026, 3, 2, 8, 55), 900) == datetime(2026, 3, 2, 8, 45)
        assert session_align(datetime(2026, 3, 2, 9, 0), 900) == datetime(2026, 3, 2, 9, 0)

    def test_30m_am_session(self):
        """30-min bars in AM session: 08:45, 09:15, 09:45, ..."""
        assert session_align(datetime(2026, 3, 2, 8, 45), 1800) == datetime(2026, 3, 2, 8, 45)
        assert session_align(datetime(2026, 3, 2, 9, 0), 1800) == datetime(2026, 3, 2, 8, 45)
        assert session_align(datetime(2026, 3, 2, 9, 15), 1800) == datetime(2026, 3, 2, 9, 15)


# ── BarAggregator H4 tests (session-aligned) ──

class TestBarAggregator1mToH4:
    """1-min bars → H4 (14400s) bars with session alignment."""

    def test_single_bar_no_output(self):
        agg = BarAggregator("TX00", 14400)
        result = agg.on_bar(_bar("2026-03-01 09:00"))
        assert result is None

    def test_same_period_no_output(self):
        """Both 08:45 and 09:00 fall in the same 4H AM bar (08:45-12:45)."""
        agg = BarAggregator("TX00", 14400)
        agg.on_bar(_bar("2026-03-02 08:45", o=100, h=110, l=90, c=105))
        result = agg.on_bar(_bar("2026-03-02 09:00", o=106, h=120, l=85, c=115))
        assert result is None

    def test_boundary_cross_emits_bar(self):
        """4H AM boundary at 12:45: bars before → first 4H bar, 12:45 starts second."""
        agg = BarAggregator("TX00", 14400)
        agg.on_bar(_bar("2026-03-02 08:45", o=100, h=110, l=90, c=105, v=10))
        agg.on_bar(_bar("2026-03-02 09:00", o=106, h=120, l=85, c=115, v=20))
        agg.on_bar(_bar("2026-03-02 10:00", o=115, h=125, l=95, c=110, v=15))

        # 12:45 crosses from 08:45 period into 12:45 period
        result = agg.on_bar(_bar("2026-03-02 12:45", o=112, h=118, l=100, c=108, v=5))

        assert result is not None
        assert result.dt == datetime(2026, 3, 2, 8, 45)
        assert result.open == 100
        assert result.high == 125
        assert result.low == 85
        assert result.close == 110
        assert result.volume == 45  # 10+20+15
        assert result.interval == 14400

    def test_ohlcv_aggregation_correct(self):
        agg = BarAggregator("TX00", 14400)
        # Feed bars in the 08:45-12:44 H4 window
        bars_data = [
            ("2026-03-02 08:45", 100, 105, 98, 103, 10),
            ("2026-03-02 08:46", 103, 108, 101, 107, 20),
            ("2026-03-02 08:47", 107, 107, 95, 96, 30),
        ]
        for dt_str, o, h, l, c, v in bars_data:
            agg.on_bar(_bar(dt_str, o, h, l, c, v))

        result = agg.flush()
        assert result is not None
        assert result.open == 100   # first bar's open
        assert result.high == 108   # max high
        assert result.low == 95     # min low
        assert result.close == 96   # last bar's close
        assert result.volume == 60  # sum

    def test_flush_returns_partial_bar(self):
        agg = BarAggregator("TX00", 14400)
        agg.on_bar(_bar("2026-03-02 09:00", o=100, h=110, l=90, c=105, v=50))

        result = agg.flush()
        assert result is not None
        assert result.open == 100
        assert result.close == 105
        assert result.volume == 50

    def test_flush_on_empty_returns_none(self):
        agg = BarAggregator("TX00", 14400)
        assert agg.flush() is None

    def test_flush_clears_state(self):
        agg = BarAggregator("TX00", 14400)
        agg.on_bar(_bar("2026-03-02 09:00"))
        agg.flush()
        assert agg.flush() is None  # second flush returns nothing

    def test_reset_clears_state(self):
        agg = BarAggregator("TX00", 14400)
        agg.on_bar(_bar("2026-03-02 09:00"))
        agg.reset()
        assert agg.flush() is None


class TestBarAggregator1mTo15m:
    """1-min bars → 15-min (900s) bars."""

    def test_15m_boundary(self):
        agg = BarAggregator("TX00", 900)
        # Feed 15 one-minute bars: 09:00 through 09:14
        for m in range(15):
            agg.on_bar(_bar(f"2026-03-02 09:{m:02d}", o=100+m, h=100+m+5,
                            l=100+m-3, c=100+m+2, v=10))

        # 09:15 crosses the boundary
        result = agg.on_bar(_bar("2026-03-02 09:15", o=120, h=130, l=110, c=125, v=5))

        assert result is not None
        assert result.dt == datetime(2026, 3, 2, 9, 0)
        assert result.interval == 900
        assert result.open == 100     # first bar's open
        assert result.volume == 150   # 15 * 10

    def test_multiple_15m_bars(self):
        agg = BarAggregator("TX00", 900)
        completed = []

        # Feed 45 minutes of 1-min bars (09:00-09:44) → should emit 2 completed bars
        for m in range(45):
            result = agg.on_bar(_bar(f"2026-03-02 09:{m:02d}", v=1))
            if result is not None:
                completed.append(result)

        assert len(completed) == 2
        assert completed[0].dt == datetime(2026, 3, 2, 9, 0)
        assert completed[1].dt == datetime(2026, 3, 2, 9, 15)


# ── Session-aligned aggregation scenarios ──

class TestBarAggregatorSessionAlignment:
    """Verify bars align to session boundaries, not midnight."""

    def test_h4_am_session_two_bars(self):
        """AM session produces two 4H bars: 08:45-12:45 and 12:45-13:45."""
        agg = BarAggregator("TX00", 14400)
        completed = []

        # Feed 1m bars from 08:45 to 13:44 (5 hours = 300 bars)
        base = datetime(2026, 3, 2, 8, 45)
        for i in range(300):
            dt = base + timedelta(minutes=i)
            dt_str = dt.strftime("%Y-%m-%d %H:%M")
            result = agg.on_bar(_bar(dt_str, o=100, h=110, l=90, c=105, v=1))
            if result is not None:
                completed.append(result)

        # One boundary cross at 12:45 → 1 completed bar
        assert len(completed) == 1
        assert completed[0].dt == datetime(2026, 3, 2, 8, 45)

        # Flush the second (partial) bar: 12:45-13:44
        partial = agg.flush()
        assert partial is not None
        assert partial.dt == datetime(2026, 3, 2, 12, 45)

    def test_h4_night_session_four_bars(self):
        """Night session: 15:00-19:00, 19:00-23:00, 23:00-03:00, 03:00-05:00."""
        agg = BarAggregator("TX00", 14400)
        completed = []

        # Feed 1m bars from 15:00 to 04:59 (14 hours = 840 bars)
        base = datetime(2026, 3, 2, 15, 0)
        for i in range(840):
            dt = base + timedelta(minutes=i)
            dt_str = dt.strftime("%Y-%m-%d %H:%M")
            result = agg.on_bar(_bar(dt_str, o=100, h=110, l=90, c=105, v=1))
            if result is not None:
                completed.append(result)

        # 3 boundary crosses: 19:00, 23:00, 03:00
        assert len(completed) == 3
        assert completed[0].dt == datetime(2026, 3, 2, 15, 0)
        assert completed[1].dt == datetime(2026, 3, 2, 19, 0)
        assert completed[2].dt == datetime(2026, 3, 2, 23, 0)

        # Flush the last partial bar (03:00-04:59)
        partial = agg.flush()
        assert partial is not None
        assert partial.dt == datetime(2026, 3, 3, 3, 0)

    def test_1h_am_session_boundaries(self):
        """1H AM: 08:45, 09:45, 10:45, 11:45, 12:45."""
        agg = BarAggregator("TX00", 3600)
        completed = []

        base = datetime(2026, 3, 2, 8, 45)
        for i in range(300):  # 08:45 to 13:44
            dt = base + timedelta(minutes=i)
            dt_str = dt.strftime("%Y-%m-%d %H:%M")
            result = agg.on_bar(_bar(dt_str, v=1))
            if result is not None:
                completed.append(result)

        assert len(completed) == 4  # 08:45, 09:45, 10:45, 11:45 completed
        assert completed[0].dt == datetime(2026, 3, 2, 8, 45)
        assert completed[1].dt == datetime(2026, 3, 2, 9, 45)
        assert completed[2].dt == datetime(2026, 3, 2, 10, 45)
        assert completed[3].dt == datetime(2026, 3, 2, 11, 45)

        partial = agg.flush()
        assert partial is not None
        assert partial.dt == datetime(2026, 3, 2, 12, 45)

    def test_1h_night_cross_midnight(self):
        """1H night: bar at 23:00 and 00:00 are separate bars."""
        agg = BarAggregator("TX00", 3600)

        # Bar in the 23:00-23:59 window
        agg.on_bar(_bar("2026-03-02 23:00", o=100, h=110, l=90, c=105, v=10))
        # 00:00 next day crosses into next hour
        result = agg.on_bar(_bar("2026-03-03 00:00", o=106, h=115, l=95, c=110, v=20))

        assert result is not None
        assert result.dt == datetime(2026, 3, 2, 23, 0)
        assert result.open == 100


class TestBarAggregatorEdgeCases:
    """Edge cases: cross-midnight, symbol preservation."""

    def test_cross_midnight_h4_night(self):
        """H4 night session: 23:00 bar and 00:00 bar are in the SAME 4H period (23:00-03:00)."""
        agg = BarAggregator("TX00", 14400)
        agg.on_bar(_bar("2026-03-02 23:00", o=100, h=110, l=90, c=105, v=10))
        # 00:00 is still in the 23:00-03:00 window
        result = agg.on_bar(_bar("2026-03-03 00:00", o=106, h=115, l=95, c=110, v=20))

        assert result is None  # same 4H bar, no boundary crossed

        # 03:00 crosses into the next 4H bar
        result = agg.on_bar(_bar("2026-03-03 03:00", o=112, h=118, l=100, c=108, v=5))
        assert result is not None
        assert result.dt == datetime(2026, 3, 2, 23, 0)

    def test_symbol_preserved(self):
        agg = BarAggregator("MTX00", 900)
        agg.on_bar(_bar("2026-03-02 09:00", v=5))
        result = agg.flush()
        assert result.symbol == "MTX00"

    def test_target_interval_preserved(self):
        agg = BarAggregator("TX00", 3600)  # 1-hour
        agg.on_bar(_bar("2026-03-02 09:00"))
        result = agg.flush()
        assert result.interval == 3600


# ── Regression: issue #44 — 1-min pass-through ──

class TestBarAggregator1mPassThrough:
    """Regression tests for issue #44.

    Before the fix, BarAggregator(target_interval=60) held each incoming
    1-min bar as ``_current`` and only returned it when the NEXT 1-min bar
    arrived — leaving the live chart and strategy permanently 1 bar behind
    real time. The fix passes 1-min bars through immediately.
    """

    def test_single_bar_returns_immediately(self):
        """First 1-min bar must be returned on the same on_bar() call."""
        agg = BarAggregator("TX00", 60)
        bar = _bar("2026-03-02 09:00", o=100, h=110, l=90, c=105, v=10)
        result = agg.on_bar(bar)
        assert result is not None
        assert result.dt == datetime(2026, 3, 2, 9, 0)
        assert result.close == 105

    def test_sequential_bars_all_returned_immediately(self):
        """Each 1-min bar in sequence returns without lag."""
        agg = BarAggregator("TX00", 60)
        returned = []
        for m in range(5):
            bar = _bar(f"2026-03-02 09:{m:02d}", c=100 + m)
            r = agg.on_bar(bar)
            assert r is not None, f"bar {m} was held instead of emitted"
            returned.append(r)
        # Each bar returned matches the input (no lag)
        assert [r.dt.minute for r in returned] == [0, 1, 2, 3, 4]
        assert [r.close for r in returned] == [100, 101, 102, 103, 104]

    def test_no_partial_after_passthrough(self):
        """Pass-through keeps _current empty: get_partial_bar() is None."""
        agg = BarAggregator("TX00", 60)
        agg.on_bar(_bar("2026-03-02 09:00"))
        assert agg.get_partial_bar() is None

    def test_flush_is_noop_after_passthrough(self):
        """Pass-through leaves nothing to flush."""
        agg = BarAggregator("TX00", 60)
        agg.on_bar(_bar("2026-03-02 09:00"))
        agg.on_bar(_bar("2026-03-02 09:01"))
        assert agg.flush() is None

    def test_passthrough_preserves_data(self):
        """The returned bar carries through the input bar's OHLCV intact."""
        agg = BarAggregator("TX00", 60)
        bar = _bar("2026-03-02 09:00", o=100, h=115, l=95, c=110, v=42)
        result = agg.on_bar(bar)
        assert result is not None
        assert result.open == 100
        assert result.high == 115
        assert result.low == 95
        assert result.close == 110
        assert result.volume == 42

    def test_h1_still_aggregates(self):
        """Sanity: non-60s targets still aggregate as before (no regression)."""
        agg = BarAggregator("TX00", 3600)
        # First 1-min bar: held (not returned)
        result = agg.on_bar(_bar("2026-03-02 08:45", c=100))
        assert result is None
        # Second 1-min bar in same H1 boundary: still held
        result = agg.on_bar(_bar("2026-03-02 08:46", c=101))
        assert result is None
        # Partial is populated
        partial = agg.get_partial_bar()
        assert partial is not None
        assert partial.close == 101
