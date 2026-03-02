"""Tests for bar reuse logic and date filtering."""

import pytest
from datetime import datetime

from src.market_data.models import Bar
from run_backtest import should_reuse_bars, filter_bars_by_date


def _make_bar(dt_str: str, close: int = 20000) -> Bar:
    """Create a Bar with the given datetime string (YYYY-MM-DD HH:MM)."""
    return Bar(
        symbol="TXF1!", dt=datetime.strptime(dt_str, "%Y-%m-%d %H:%M"),
        open=close, high=close + 50, low=close - 50, close=close,
        volume=100, interval=900,
    )


class TestShouldReuseBars:
    """Tests for should_reuse_bars()."""

    def test_same_timeframe_reuses(self):
        bars = [_make_bar("2026-01-01 09:00")]
        assert should_reuse_bars(bars, (0, 15), kline_type=0, kline_minute=15) is True

    def test_different_minute_no_reuse(self):
        bars = [_make_bar("2026-01-01 09:00")]
        assert should_reuse_bars(bars, (0, 15), kline_type=0, kline_minute=60) is False

    def test_different_kline_type_no_reuse(self):
        bars = [_make_bar("2026-01-01 09:00")]
        assert should_reuse_bars(bars, (0, 240), kline_type=4, kline_minute=240) is False

    def test_empty_bars_no_reuse(self):
        assert should_reuse_bars([], (0, 15), kline_type=0, kline_minute=15) is False

    def test_h4_matches_h4(self):
        bars = [_make_bar("2026-01-01 09:00")]
        assert should_reuse_bars(bars, (0, 240), kline_type=0, kline_minute=240) is True

    def test_daily_matches_daily(self):
        bars = [_make_bar("2026-01-01 09:00")]
        assert should_reuse_bars(bars, (4, 0), kline_type=4, kline_minute=0) is True

    def test_daily_vs_weekly_no_reuse(self):
        bars = [_make_bar("2026-01-01 09:00")]
        assert should_reuse_bars(bars, (4, 0), kline_type=5, kline_minute=0) is False

    def test_empty_key_no_reuse(self):
        bars = [_make_bar("2026-01-01 09:00")]
        assert should_reuse_bars(bars, (), kline_type=0, kline_minute=15) is False


class TestFilterBarsByDate:
    """Tests for filter_bars_by_date()."""

    @pytest.fixture
    def bars(self):
        """Bars spanning Jan-Mar 2026."""
        return [
            _make_bar("2026-01-15 09:00", 20000),
            _make_bar("2026-01-20 09:00", 20100),
            _make_bar("2026-02-10 09:00", 20200),
            _make_bar("2026-02-20 09:00", 20300),
            _make_bar("2026-03-01 09:00", 20400),
        ]

    def test_full_range_returns_all(self, bars):
        result = filter_bars_by_date(bars, "20260101", "20260301")
        assert len(result) == 5

    def test_narrow_range_filters(self, bars):
        result = filter_bars_by_date(bars, "20260201", "20260228")
        assert len(result) == 2
        assert all(b.dt.month == 2 for b in result)

    def test_single_day(self, bars):
        result = filter_bars_by_date(bars, "20260115", "20260115")
        assert len(result) == 1
        assert result[0].close == 20000

    def test_range_before_data_returns_empty(self, bars):
        result = filter_bars_by_date(bars, "20250101", "20251231")
        assert len(result) == 0

    def test_range_after_data_returns_empty(self, bars):
        result = filter_bars_by_date(bars, "20260401", "20260430")
        assert len(result) == 0

    def test_end_date_inclusive(self, bars):
        """End date should include bars ON that date (end_date + 1 day)."""
        result = filter_bars_by_date(bars, "20260301", "20260301")
        assert len(result) == 1
        assert result[0].close == 20400

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError):
            filter_bars_by_date([], "not-a-date", "20260301")
