"""Tests for TAIFEX data source."""

from datetime import date, datetime
from unittest.mock import patch, MagicMock

from src.data_sources.taifex import (
    _month_chunks,
    parse_taifex_csv,
    fetch_futures_daily,
)
from src.market_data.models import Bar


# -- _month_chunks --

def test_month_chunks_single_month():
    chunks = _month_chunks(date(2025, 1, 5), date(2025, 1, 20))
    assert chunks == [(date(2025, 1, 5), date(2025, 1, 20))]


def test_month_chunks_spans_two_months():
    chunks = _month_chunks(date(2025, 1, 15), date(2025, 2, 10))
    assert chunks == [
        (date(2025, 1, 15), date(2025, 1, 31)),
        (date(2025, 2, 1), date(2025, 2, 10)),
    ]


def test_month_chunks_full_year():
    chunks = _month_chunks(date(2025, 1, 1), date(2025, 12, 31))
    assert len(chunks) == 12
    assert chunks[0] == (date(2025, 1, 1), date(2025, 1, 31))
    assert chunks[-1] == (date(2025, 12, 1), date(2025, 12, 31))


def test_month_chunks_cross_year():
    chunks = _month_chunks(date(2024, 12, 15), date(2025, 1, 10))
    assert chunks == [
        (date(2024, 12, 15), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 1, 10)),
    ]


def test_month_chunks_ten_years():
    chunks = _month_chunks(date(2016, 1, 1), date(2025, 12, 31))
    assert len(chunks) == 120


def test_month_chunks_feb_leap_year():
    chunks = _month_chunks(date(2024, 2, 1), date(2024, 2, 29))
    assert chunks == [(date(2024, 2, 1), date(2024, 2, 29))]


# -- parse_taifex_csv --

_SAMPLE_CSV = """\u4ea4\u6613\u65e5\u671f,\u5951\u7d04,\u5230\u671f\u6708\u4efd(\u9031\u5225),\u958b\u76e4\u50f9,\u6700\u9ad8\u50f9,\u6700\u4f4e\u50f9,\u6700\u5f8c\u6210\u4ea4\u50f9,\u6f32\u8dcc\u50f9,\u6f32\u8dcc%,\u6210\u4ea4\u91cf,\u7d50\u7b97\u50f9,\u672a\u6c96\u92b7\u5951\u7d04\u6578,\u6700\u5f8c\u6700\u4f73\u8cb7\u50f9,\u6700\u5f8c\u6700\u4f73\u8ce3\u50f9,\u6b77\u53f2\u6700\u9ad8\u50f9,\u6b77\u53f2\u6700\u4f4e\u50f9,\u662f\u5426\u56e0\u8a0a\u606f\u9762\u66ab\u505c\u4ea4\u6613,\u4ea4\u6613\u6642\u6bb5,\u50f9\u5dee\u5c0d\u55ae\u5f0f\u59d4\u8a17\u6210\u4ea4\u91cf
2025/01/02,TX,202501  ,22935,22995,22689,22842,-202,-0.88%,78968,22843,73861,22842,22847,23996,22039,,\u4e00\u822c,,
2025/01/02,TX,202501  ,23023,23168,22931,22986,-58,-0.25%,37987,-,-,22986,22987,23996,22039,,\u76e4\u5f8c,,
2025/01/02,TX,202502  ,23003,23060,22763,22906,-201,-0.87%,1137,22910,1883,22910,22916,23550,22084,,\u4e00\u822c,,
2025/01/02,TX,202501/202502  ,68,72,67,68,-1,-1.45%,266,-,-,-,-,-,-,,\u4e00\u822c,,
2025/01/03,TX,202501  ,22900,23050,22800,23000,158,0.69%,85000,23010,74000,22999,23001,23996,22039,,\u4e00\u822c,,
2025/01/03,TX,202501  ,23100,23200,23000,23100,114,0.50%,40000,-,-,23099,23101,23996,22039,,\u76e4\u5f8c,,
2025/01/03,TX,202502  ,22950,23100,22850,23050,144,0.63%,1200,23060,1900,23049,23055,23550,22084,,\u4e00\u822c,,
"""


def test_parse_filters_near_month_regular_session():
    bars = parse_taifex_csv(_SAMPLE_CSV, "TX", "TXF1")
    assert len(bars) == 2
    assert bars[0].dt == datetime(2025, 1, 2)
    assert bars[0].open == 22935
    assert bars[0].close == 22842
    assert bars[0].volume == 78968
    assert bars[1].dt == datetime(2025, 1, 3)
    assert bars[1].open == 22900


def test_parse_excludes_spread_contracts():
    bars = parse_taifex_csv(_SAMPLE_CSV, "TX", "TXF1")
    # Spread row (202501/202502) should be excluded
    for b in bars:
        assert b.volume != 266


def test_parse_excludes_after_hours():
    bars = parse_taifex_csv(_SAMPLE_CSV, "TX", "TXF1")
    # After-hours row has volume 37987, should be excluded
    for b in bars:
        assert b.volume != 37987


def test_parse_price_multiplier():
    bars = parse_taifex_csv(_SAMPLE_CSV, "TX", "TXF1", price_multiplier=100)
    assert bars[0].open == 2293500
    assert bars[0].close == 2284200


def test_parse_sets_daily_interval():
    bars = parse_taifex_csv(_SAMPLE_CSV, "TX", "TXF1")
    for b in bars:
        assert b.interval == 86400


def test_parse_sets_symbol():
    bars = parse_taifex_csv(_SAMPLE_CSV, "TX", "MY_SYMBOL")
    for b in bars:
        assert b.symbol == "MY_SYMBOL"


def test_parse_empty_csv():
    assert parse_taifex_csv("", "TX", "TXF1") == []
    assert parse_taifex_csv("header\n", "TX", "TXF1") == []


def test_parse_wrong_commodity():
    bars = parse_taifex_csv(_SAMPLE_CSV, "MTX", "TMF1")
    assert bars == []


def test_parse_missing_prices():
    csv_with_dash = """\u4ea4\u6613\u65e5\u671f,\u5951\u7d04,\u5230\u671f\u6708\u4efd(\u9031\u5225),\u958b\u76e4\u50f9,\u6700\u9ad8\u50f9,\u6700\u4f4e\u50f9,\u6700\u5f8c\u6210\u4ea4\u50f9,\u6f32\u8dcc\u50f9,\u6f32\u8dcc%,\u6210\u4ea4\u91cf,\u7d50\u7b97\u50f9,\u672a\u6c96\u92b7\u5951\u7d04\u6578,\u6700\u5f8c\u6700\u4f73\u8cb7\u50f9,\u6700\u5f8c\u6700\u4f73\u8ce3\u50f9,\u6b77\u53f2\u6700\u9ad8\u50f9,\u6b77\u53f2\u6700\u4f4e\u50f9,\u662f\u5426\u56e0\u8a0a\u606f\u9762\u66ab\u505c\u4ea4\u6613,\u4ea4\u6613\u6642\u6bb5,\u50f9\u5dee\u5c0d\u55ae\u5f0f\u59d4\u8a17\u6210\u4ea4\u91cf
2025/01/02,TX,202501  ,-,-,-,-,0,0%,0,-,-,-,-,-,-,,\u4e00\u822c,,
"""
    bars = parse_taifex_csv(csv_with_dash, "TX", "TXF1")
    assert bars == []


def test_parse_sorted_output():
    # Reversed date order in CSV
    csv_reversed = """\u4ea4\u6613\u65e5\u671f,\u5951\u7d04,\u5230\u671f\u6708\u4efd,\u958b\u76e4\u50f9,\u6700\u9ad8\u50f9,\u6700\u4f4e\u50f9,\u6700\u5f8c\u6210\u4ea4\u50f9,\u6f32\u8dcc\u50f9,\u6f32\u8dcc%,\u6210\u4ea4\u91cf,\u7d50\u7b97\u50f9,\u672a\u6c96\u92b7,\u8cb7\u50f9,\u8ce3\u50f9,\u9ad8,\u4f4e,\u66ab\u505c,\u6642\u6bb5,\u50f9\u5dee
2025/01/10,TX,202501  ,23100,23200,23000,23150,50,0.2%,80000,23150,70000,23149,23151,24000,22000,,\u4e00\u822c,,
2025/01/02,TX,202501  ,22935,22995,22689,22842,-202,-0.88%,78968,22843,73861,22842,22847,23996,22039,,\u4e00\u822c,,
"""
    bars = parse_taifex_csv(csv_reversed, "TX", "TXF1")
    assert bars[0].dt < bars[1].dt


# -- fetch_futures_daily (mocked) --

@patch("src.data_sources.taifex._fetch_csv_chunk")
@patch("src.data_sources.taifex.time.sleep")
def test_fetch_futures_daily_combines_chunks(mock_sleep, mock_fetch):
    mock_fetch.return_value = _SAMPLE_CSV
    bars = fetch_futures_daily("TX", date(2025, 1, 1), date(2025, 2, 28), symbol="TXF1")
    assert mock_fetch.call_count == 2  # 2 monthly chunks
    assert mock_sleep.call_count == 1  # sleep between chunks
    # Bars from both chunks (same CSV returned twice, so dedup by date)
    assert len(bars) == 4  # 2 dates * 2 chunks


@patch("src.data_sources.taifex._fetch_csv_chunk")
@patch("src.data_sources.taifex.time.sleep")
def test_fetch_calls_progress(mock_sleep, mock_fetch):
    mock_fetch.return_value = ""
    progress_calls = []
    fetch_futures_daily(
        "TX", date(2025, 1, 1), date(2025, 1, 31),
        on_progress=lambda cur, total: progress_calls.append((cur, total)),
    )
    assert progress_calls == [(0, 1), (1, 1)]


@patch("src.data_sources.taifex._fetch_csv_chunk")
@patch("src.data_sources.taifex.time.sleep")
def test_fetch_no_sleep_single_chunk(mock_sleep, mock_fetch):
    mock_fetch.return_value = ""
    fetch_futures_daily("TX", date(2025, 1, 1), date(2025, 1, 15))
    mock_sleep.assert_not_called()
