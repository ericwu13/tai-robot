"""Tests for BarAggregator: 1-min → N-min bar aggregation."""

from datetime import datetime, timedelta

from src.market_data.models import Bar
from src.live.bar_aggregator import BarAggregator


def _bar(dt_str, o=100, h=110, l=90, c=105, v=10, interval=60):
    """Helper to create a 1-min bar."""
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    return Bar(symbol="TX00", dt=dt, open=o, high=h, low=l, close=c,
               volume=v, interval=interval)


class TestBarAggregator1mToH4:
    """1-min bars → H4 (14400s) bars."""

    def test_single_bar_no_output(self):
        agg = BarAggregator("TX00", 14400)
        result = agg.on_bar(_bar("2026-03-01 09:00"))
        assert result is None

    def test_same_period_no_output(self):
        agg = BarAggregator("TX00", 14400)
        agg.on_bar(_bar("2026-03-01 08:00", o=100, h=110, l=90, c=105))
        result = agg.on_bar(_bar("2026-03-01 09:00", o=106, h=120, l=85, c=115))
        assert result is None

    def test_boundary_cross_emits_bar(self):
        agg = BarAggregator("TX00", 14400)  # H4 = 4h boundaries: 00:00, 04:00, 08:00, 12:00, ...
        agg.on_bar(_bar("2026-03-01 08:00", o=100, h=110, l=90, c=105, v=10))
        agg.on_bar(_bar("2026-03-01 09:00", o=106, h=120, l=85, c=115, v=20))
        agg.on_bar(_bar("2026-03-01 10:00", o=115, h=125, l=95, c=110, v=15))

        # 12:00 crosses from 08:00-11:59 period into 12:00-15:59 period
        result = agg.on_bar(_bar("2026-03-01 12:00", o=112, h=118, l=100, c=108, v=5))

        assert result is not None
        assert result.dt == datetime(2026, 3, 1, 8, 0)
        assert result.open == 100
        assert result.high == 125
        assert result.low == 85
        assert result.close == 110
        assert result.volume == 45  # 10+20+15
        assert result.interval == 14400

    def test_ohlcv_aggregation_correct(self):
        agg = BarAggregator("TX00", 14400)
        # Feed bars in the 08:00-11:59 H4 window
        bars_data = [
            ("2026-03-01 08:00", 100, 105, 98, 103, 10),
            ("2026-03-01 08:01", 103, 108, 101, 107, 20),
            ("2026-03-01 08:02", 107, 107, 95, 96, 30),
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
        agg.on_bar(_bar("2026-03-01 09:00", o=100, h=110, l=90, c=105, v=50))

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
        agg.on_bar(_bar("2026-03-01 09:00"))
        agg.flush()
        assert agg.flush() is None  # second flush returns nothing

    def test_reset_clears_state(self):
        agg = BarAggregator("TX00", 14400)
        agg.on_bar(_bar("2026-03-01 09:00"))
        agg.reset()
        assert agg.flush() is None


class TestBarAggregator1mTo15m:
    """1-min bars → 15-min (900s) bars."""

    def test_15m_boundary(self):
        agg = BarAggregator("TX00", 900)
        # Feed 15 one-minute bars: 09:00 through 09:14
        for m in range(15):
            agg.on_bar(_bar(f"2026-03-01 09:{m:02d}", o=100+m, h=100+m+5,
                            l=100+m-3, c=100+m+2, v=10))

        # 09:15 crosses the boundary
        result = agg.on_bar(_bar("2026-03-01 09:15", o=120, h=130, l=110, c=125, v=5))

        assert result is not None
        assert result.dt == datetime(2026, 3, 1, 9, 0)
        assert result.interval == 900
        assert result.open == 100     # first bar's open
        assert result.volume == 150   # 15 * 10

    def test_multiple_15m_bars(self):
        agg = BarAggregator("TX00", 900)
        completed = []

        # Feed 45 minutes of 1-min bars (09:00-09:44) → should emit 2 completed bars
        for m in range(45):
            result = agg.on_bar(_bar(f"2026-03-01 09:{m:02d}", v=1))
            if result is not None:
                completed.append(result)

        assert len(completed) == 2
        assert completed[0].dt == datetime(2026, 3, 1, 9, 0)
        assert completed[1].dt == datetime(2026, 3, 1, 9, 15)


class TestBarAggregatorEdgeCases:
    """Edge cases: cross-midnight, symbol preservation."""

    def test_cross_midnight(self):
        agg = BarAggregator("TX00", 14400)  # H4 boundaries at 00:00, 04:00, ...
        # Evening session bar at 23:00 (in 20:00-23:59 window)
        agg.on_bar(_bar("2026-03-01 23:00", o=100, h=110, l=90, c=105, v=10))
        # Midnight crosses into next day's 00:00-03:59 window
        result = agg.on_bar(_bar("2026-03-02 00:00", o=106, h=115, l=95, c=110, v=20))

        assert result is not None
        assert result.dt == datetime(2026, 3, 1, 20, 0)
        assert result.open == 100

    def test_symbol_preserved(self):
        agg = BarAggregator("MTX00", 900)
        agg.on_bar(_bar("2026-03-01 09:00", v=5))
        result = agg.flush()
        assert result.symbol == "MTX00"

    def test_target_interval_preserved(self):
        agg = BarAggregator("TX00", 3600)  # 1-hour
        agg.on_bar(_bar("2026-03-01 09:00"))
        result = agg.flush()
        assert result.interval == 3600
