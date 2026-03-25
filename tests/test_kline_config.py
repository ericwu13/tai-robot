"""Tests for kline_config — symbol mappings, interval constants, chunk calculation."""

from datetime import datetime

import pytest

from src.market_data.kline_config import (
    TV_INTERVALS, INTERVAL_SECONDS, SYMBOL_CONFIG, CACHE_SUFFIXES,
    LIVE_CHART_TIMEFRAMES, resolve_order_symbol, get_near_month_symbol,
    get_cache_file, should_reuse_bars, filter_bars_by_date,
    compute_chunk_ranges,
)


# ── Constants sanity ──

class TestConstants:
    def test_tv_intervals_has_common_timeframes(self):
        assert (0, 1) in TV_INTERVALS      # 1m
        assert (0, 240) in TV_INTERVALS     # H4
        assert (4, 1) in TV_INTERVALS       # daily

    def test_interval_seconds_values(self):
        assert INTERVAL_SECONDS[(0, 1)] == 60
        assert INTERVAL_SECONDS[(0, 240)] == 14400
        assert INTERVAL_SECONDS[(4, 1)] == 86400

    def test_symbol_config_all_have_required_keys(self):
        for sym, cfg in SYMBOL_CONFIG.items():
            assert "prefix" in cfg, f"{sym} missing prefix"
            assert "tv" in cfg, f"{sym} missing tv"
            assert "pv" in cfg, f"{sym} missing pv"

    def test_live_chart_timeframes(self):
        assert LIVE_CHART_TIMEFRAMES["Native"] is None
        assert LIVE_CHART_TIMEFRAMES["1m"] == 60


# ── get_near_month_symbol ──

class TestGetNearMonthSymbol:
    def test_before_settlement(self):
        # March 10 is before 3rd Wednesday (March 18, 2026)
        now = datetime(2026, 3, 10)
        assert get_near_month_symbol("TMF", now=now) == "TMFC6"  # C=March

    def test_after_settlement(self):
        # March 20 is after 3rd Wednesday (March 18, 2026)
        now = datetime(2026, 3, 20)
        assert get_near_month_symbol("TMF", now=now) == "TMFD6"  # D=April

    def test_december_rollover(self):
        # Dec 25 is after 3rd Wednesday → rolls to Jan next year
        now = datetime(2026, 12, 25)
        assert get_near_month_symbol("TX", now=now) == "TXA7"  # A=Jan 2027

    def test_on_settlement_day(self):
        # On the 3rd Wednesday itself (day == third_wed_day), NOT past it
        now = datetime(2026, 3, 18)  # 3rd Wednesday of March 2026
        assert get_near_month_symbol("TX", now=now) == "TXC6"  # still March


# ── resolve_order_symbol ──

class TestResolveOrderSymbol:
    def test_tx00(self):
        assert resolve_order_symbol("TX00") == "TXFD0"

    def test_mtx00(self):
        assert resolve_order_symbol("MTX00") == "MTXFD0"

    def test_tmf00(self):
        assert resolve_order_symbol("TMF00") == "TM0000"

    def test_unknown_returns_itself(self):
        assert resolve_order_symbol("UNKNOWN") == "UNKNOWN"


# ── compute_chunk_ranges ──

class TestComputeChunkRanges:
    def test_daily_single_chunk(self):
        """Daily bars: 250 bars/chunk = 250 trading days ≈ 350 calendar days."""
        chunks = compute_chunk_ranges("20260101", "20260301", 4, 1)
        # 59 days fits in one chunk for daily
        assert len(chunks) == 1
        assert chunks[0] == ("20260101", "20260301")

    def test_1m_many_chunks(self):
        """1-minute bars: very small chunks (300 bars/tday → ~5 calendar days each)."""
        chunks = compute_chunk_ranges("20260101", "20260131", 0, 1)
        assert len(chunks) > 1
        # Each chunk should be small
        assert chunks[0][0] == "20260101"

    def test_h4_moderate_chunks(self):
        """H4: 6 bars/tday → 41 trading days ≈ 57 calendar days per chunk."""
        chunks = compute_chunk_ranges("20250101", "20260101", 0, 240)
        assert len(chunks) >= 1
        # Should cover the full range
        assert chunks[0][0] == "20250101"
        assert chunks[-1][1] == "20260101"

    def test_empty_range(self):
        """Same start and end date → no chunks."""
        chunks = compute_chunk_ranges("20260101", "20260101", 0, 240)
        assert len(chunks) == 0

    def test_contiguous_coverage(self):
        """Chunks should cover the entire range without gaps."""
        chunks = compute_chunk_ranges("20260101", "20260301", 0, 15)
        # First chunk starts at start
        assert chunks[0][0] == "20260101"
        # Last chunk ends at end
        assert chunks[-1][1] == "20260301"

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError):
            compute_chunk_ranges("not-a-date", "20260101", 0, 240)


# ── should_reuse_bars ──

class TestShouldReuseBars:
    def test_matching(self):
        assert should_reuse_bars([1, 2], ("TX00", 0, 240), "TX00", 0, 240)

    def test_empty_bars(self):
        assert not should_reuse_bars([], ("TX00", 0, 240), "TX00", 0, 240)

    def test_different_symbol(self):
        assert not should_reuse_bars([1], ("TX00", 0, 240), "MTX00", 0, 240)

    def test_different_timeframe(self):
        assert not should_reuse_bars([1], ("TX00", 0, 240), "TX00", 0, 60)


# ── filter_bars_by_date ──

class TestFilterBarsByDate:
    def test_basic_filter(self):
        class FakeBar:
            def __init__(self, dt):
                self.dt = dt
        bars = [
            FakeBar(datetime(2026, 1, 1, 9, 0)),
            FakeBar(datetime(2026, 1, 15, 9, 0)),
            FakeBar(datetime(2026, 2, 1, 9, 0)),
        ]
        result = filter_bars_by_date(bars, "20260101", "20260115")
        assert len(result) == 2

    def test_empty_bars(self):
        assert filter_bars_by_date([], "20260101", "20260115") == []
