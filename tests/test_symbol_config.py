"""Tests for symbol configuration logic in kline_config."""

import pytest

from src.market_data.kline_config import SYMBOL_CONFIG, CACHE_SUFFIXES, get_cache_file


class TestGetCacheFile:
    def test_tx00_h4(self):
        assert get_cache_file("TX00", (0, 240)) == "TXF1_H4.csv"

    def test_mtx00_h4(self):
        assert get_cache_file("MTX00", (0, 240)) == "TMF1_H4.csv"

    def test_all_timeframes_tx00(self):
        expected = {
            (0, 15): "TXF1_15m.csv",
            (0, 60): "TXF1_1H.csv",
            (0, 240): "TXF1_H4.csv",
            (4, 1): "TXF1_1D.csv",
        }
        for key, filename in expected.items():
            assert get_cache_file("TX00", key) == filename

    def test_all_timeframes_mtx00(self):
        expected = {
            (0, 15): "TMF1_15m.csv",
            (0, 60): "TMF1_1H.csv",
            (0, 240): "TMF1_H4.csv",
            (4, 1): "TMF1_1D.csv",
        }
        for key, filename in expected.items():
            assert get_cache_file("MTX00", key) == filename

    def test_unknown_symbol(self):
        assert get_cache_file("UNKNOWN", (0, 240)) is None

    def test_unknown_timeframe(self):
        assert get_cache_file("TX00", (0, 999)) is None


class TestSymbolConfig:
    def test_point_values(self):
        assert SYMBOL_CONFIG["TX00"]["pv"] == 200
        assert SYMBOL_CONFIG["MTX00"]["pv"] == 50

    def test_tv_symbols(self):
        assert SYMBOL_CONFIG["TX00"]["tv"] == "TXF1!"
        assert SYMBOL_CONFIG["MTX00"]["tv"] == "TMF1!"


