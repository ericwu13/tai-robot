"""Tests for kchart_fetcher — KChartFetcher class, TV data conversion, dedup."""

from datetime import datetime

import pandas as pd
import pytest

from src.market_data.models import Bar
from src.market_data.kchart_fetcher import (
    KChartFetcher, resolve_kline_symbol, resolve_tv_symbol, dedup_bars,
    parse_and_dedup_kline, tv_dataframe_to_bars, tv_dataframe_to_kline_strings,
)


# ── Symbol resolution ──

class TestResolveKlineSymbol:
    def test_tx00_returns_itself(self):
        assert resolve_kline_symbol("TX00") == "TX00"

    def test_mtx00_returns_tx00(self):
        assert resolve_kline_symbol("MTX00") == "TX00"

    def test_tmf00_returns_tx00(self):
        assert resolve_kline_symbol("TMF00") == "TX00"

    def test_unknown_returns_itself(self):
        assert resolve_kline_symbol("UNKNOWN") == "UNKNOWN"


class TestResolveTvSymbol:
    def test_tx00(self):
        assert resolve_tv_symbol("TX00") == "TXF1!"

    def test_mtx00(self):
        assert resolve_tv_symbol("MTX00") == "TMF1!"

    def test_unknown_returns_itself(self):
        assert resolve_tv_symbol("UNKNOWN") == "UNKNOWN"


# ── Dedup ──

class TestDedupBars:
    def test_no_duplicates(self):
        bars = [
            Bar("S", datetime(2026, 1, 1, 9, 0), 100, 101, 99, 100, 10, 60),
            Bar("S", datetime(2026, 1, 1, 9, 1), 101, 102, 100, 101, 20, 60),
        ]
        assert len(dedup_bars(bars)) == 2

    def test_removes_duplicates(self):
        dt = datetime(2026, 1, 1, 9, 0)
        bars = [
            Bar("S", dt, 100, 101, 99, 100, 10, 60),
            Bar("S", dt, 100, 101, 99, 100, 10, 60),
            Bar("S", datetime(2026, 1, 1, 9, 1), 101, 102, 100, 101, 20, 60),
        ]
        assert len(dedup_bars(bars)) == 2

    def test_preserves_order(self):
        bars = [
            Bar("S", datetime(2026, 1, 1, 9, 1), 101, 102, 100, 101, 20, 60),
            Bar("S", datetime(2026, 1, 1, 9, 0), 100, 101, 99, 100, 10, 60),
        ]
        result = dedup_bars(bars)
        assert result[0].dt == datetime(2026, 1, 1, 9, 1)

    def test_empty(self):
        assert dedup_bars([]) == []


# ── parse_and_dedup_kline ──

class TestParseAndDedupKline:
    def test_basic(self):
        kline_data = [
            "01/02/2026 09:00, 20000, 20050, 19950, 20010, 100",
            "01/02/2026 09:01, 20010, 20060, 19960, 20020, 200",
        ]
        bars = parse_and_dedup_kline(kline_data, symbol="TXF1!", interval=60)
        assert len(bars) == 2
        assert bars[0].open == 20000

    def test_dedup(self):
        kline_data = [
            "01/02/2026 09:00, 20000, 20050, 19950, 20010, 100",
            "01/02/2026 09:00, 20000, 20050, 19950, 20010, 100",
        ]
        bars = parse_and_dedup_kline(kline_data, symbol="TXF1!", interval=60)
        assert len(bars) == 1

    def test_empty(self):
        assert parse_and_dedup_kline([], symbol="TXF1!", interval=60) == []


# ── KChartFetcher: COM API path ──

class TestKChartFetcherApi:
    def test_start_returns_chunks(self):
        f = KChartFetcher()
        chunks = f.start_api_fetch("TX00", 0, 240, "20260101", "20260301")
        assert len(chunks) >= 1
        assert chunks[0][0] == "20260101"
        assert chunks[-1][1] == "20260301"

    def test_next_chunk_returns_request(self):
        f = KChartFetcher()
        f.start_api_fetch("TX00", 0, 240, "20260101", "20260301")
        chunk = f.next_chunk()
        assert chunk is not None
        assert chunk.kline_symbol == "TX00"
        assert chunk.kline_type == 0
        assert chunk.minute_num == 240
        assert chunk.chunk_index == 0

    def test_next_chunk_mtx00_resolves_to_tx00(self):
        """MTX00 shares TX00's KLine data."""
        f = KChartFetcher()
        f.start_api_fetch("MTX00", 0, 240, "20260101", "20260301")
        chunk = f.next_chunk()
        assert chunk.kline_symbol == "TX00"

    def test_advance_and_done(self):
        f = KChartFetcher()
        f.start_api_fetch("TX00", 4, 1, "20260101", "20260301")
        # Daily = 1 chunk for 2 months
        assert not f.chunks_done
        chunk1 = f.next_chunk()
        assert chunk1.chunk_index == 0
        f.advance_chunk()
        assert f.chunks_done
        assert f.next_chunk() is None

    def test_on_kline_data_accumulates(self):
        f = KChartFetcher()
        f.start_api_fetch("TX00", 0, 240, "20260101", "20260301")
        f.on_kline_data("01/02/2026 09:00, 20000, 20050, 19950, 20010, 100")
        f.on_kline_data("01/02/2026 13:00, 20010, 20060, 19960, 20020, 200")
        assert f.chunk_bar_count == 2
        assert f.total_bar_count == 2

    def test_advance_resets_chunk_bar_count(self):
        f = KChartFetcher()
        f.start_api_fetch("TX00", 0, 15, "20260101", "20260301")
        f.on_kline_data("data1")
        f.on_kline_data("data2")
        assert f.chunk_bar_count == 2
        f.advance_chunk()
        assert f.chunk_bar_count == 0
        assert f.total_bar_count == 2  # total preserved

    def test_get_api_bars(self):
        f = KChartFetcher()
        f.start_api_fetch("TX00", 0, 240, "20260101", "20260301")
        f.on_kline_data("01/02/2026 09:00, 20000, 20050, 19950, 20010, 100")
        f.on_kline_data("01/02/2026 13:00, 20010, 20060, 19960, 20020, 200")
        bars = f.get_api_bars()
        assert len(bars) == 2
        assert bars[0].symbol == "TXF1"  # resolved from SYMBOL_CONFIG prefix
        assert bars[0].interval == 14400  # H4

    def test_get_api_bars_dedup(self):
        f = KChartFetcher()
        f.start_api_fetch("TX00", 0, 240, "20260101", "20260301")
        f.on_kline_data("01/02/2026 09:00, 20000, 20050, 19950, 20010, 100")
        f.on_kline_data("01/02/2026 09:00, 20000, 20050, 19950, 20010, 100")
        bars = f.get_api_bars()
        assert len(bars) == 1

    def test_get_api_bars_custom_symbol(self):
        f = KChartFetcher()
        f.start_api_fetch("TX00", 0, 240, "20260101", "20260301")
        f.on_kline_data("01/02/2026 09:00, 20000, 20050, 19950, 20010, 100")
        bars = f.get_api_bars(symbol="CustomSym", interval=3600)
        assert bars[0].symbol == "CustomSym"
        assert bars[0].interval == 3600

    def test_chunk_request_fields(self):
        f = KChartFetcher()
        f.start_api_fetch("TX00", 0, 15, "20260101", "20260115")
        chunk = f.next_chunk()
        assert chunk.session == 1       # AM session
        assert chunk.trade_session == 0  # full
        assert chunk.total_chunks >= 1
        assert chunk.start_date == "20260101"

    def test_multiple_chunks_iteration(self):
        """1-minute bars should produce multiple chunks."""
        f = KChartFetcher()
        f.start_api_fetch("TX00", 0, 1, "20260101", "20260131")
        count = 0
        while not f.chunks_done:
            chunk = f.next_chunk()
            assert chunk is not None
            f.on_kline_data("fake data")
            f.advance_chunk()
            count += 1
        assert count > 1

    def test_properties(self):
        f = KChartFetcher()
        f.start_api_fetch("MTX00", 0, 60, "20260101", "20260301")
        assert f.symbol == "MTX00"
        assert f.kline_type == 0
        assert f.minute_num == 60

    def test_reset(self):
        f = KChartFetcher()
        f.start_api_fetch("TX00", 0, 240, "20260101", "20260301")
        f.on_kline_data("data")
        f.reset()
        assert f.total_bar_count == 0
        assert f.chunks_done  # no chunks after reset
        assert f.next_chunk() is None
        assert f.symbol == ""

    def test_empty_range(self):
        f = KChartFetcher()
        chunks = f.start_api_fetch("TX00", 0, 240, "20260101", "20260101")
        assert len(chunks) == 0
        assert f.chunks_done


# ── tv_dataframe_to_bars ──

def _make_tv_df(datetimes, tz=None):
    """Create a minimal tvDatafeed-like DataFrame."""
    idx = pd.DatetimeIndex(datetimes)
    if tz:
        idx = idx.tz_localize(tz)
    n = len(datetimes)
    return pd.DataFrame({
        "open": [20000.0] * n, "high": [20050.0] * n,
        "low": [19950.0] * n, "close": [20010.0] * n, "volume": [100] * n,
    }, index=idx)


class TestTvDataframeToBars:
    def test_basic_conversion(self):
        dts = [datetime(2026, 1, 2, 9, 0), datetime(2026, 1, 2, 9, 15)]
        df = _make_tv_df(dts)
        result = tv_dataframe_to_bars(df, symbol="TXF1!", interval=900)
        assert result.ok
        assert len(result.bars) == 2
        assert result.bars[0].symbol == "TXF1!"
        assert result.bars[0].open == 20000
        assert result.bars[0].interval == 900

    def test_sorted_output(self):
        dts = [datetime(2026, 1, 2, 10, 0), datetime(2026, 1, 2, 9, 0)]
        df = _make_tv_df(dts)
        result = tv_dataframe_to_bars(df, symbol="TXF1!", interval=900)
        assert result.bars[0].dt < result.bars[1].dt

    def test_none_df(self):
        result = tv_dataframe_to_bars(None, symbol="TXF1!", interval=900)
        assert not result.ok
        assert result.error == "No data"

    def test_empty_df(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df.index = pd.DatetimeIndex([])
        result = tv_dataframe_to_bars(df, symbol="TXF1!", interval=900)
        assert not result.ok

    def test_tz_aware_input(self):
        dts = [datetime(2026, 1, 2, 1, 0)]  # 01:00 UTC = 09:00 TWT
        df = _make_tv_df(dts, tz="UTC")
        result = tv_dataframe_to_bars(df, symbol="TXF1!", interval=900)
        assert result.ok
        assert result.bars[0].dt.hour == 9
        assert result.bars[0].dt.tzinfo is None

    def test_twt_source_no_conversion(self):
        dts = [datetime(2026, 1, 2, 9, 0), datetime(2026, 1, 2, 10, 0)]
        df = _make_tv_df(dts)
        result = tv_dataframe_to_bars(df, symbol="TXF1!", interval=900)
        assert result.ok
        assert result.source_tz is None


# ── tv_dataframe_to_kline_strings ──

class TestTvDataframeToKlineStrings:
    def test_format(self):
        dts = [datetime(2026, 1, 2, 9, 0)]
        df = _make_tv_df(dts)
        strings = tv_dataframe_to_kline_strings(df)
        assert len(strings) == 1
        assert strings[0] == "01/02/2026 09:00,20000,20050,19950,20010,100"

    def test_multiple(self):
        dts = [datetime(2026, 1, 2, 9, 0), datetime(2026, 1, 2, 9, 1)]
        df = _make_tv_df(dts)
        strings = tv_dataframe_to_kline_strings(df)
        assert len(strings) == 2

    def test_empty(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df.index = pd.DatetimeIndex([])
        strings = tv_dataframe_to_kline_strings(df)
        assert strings == []
