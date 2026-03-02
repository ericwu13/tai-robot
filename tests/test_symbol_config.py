"""Tests for symbol configuration logic in run_backtest.py."""

import os
import sys
import pytest

# Ensure project root is importable
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from run_backtest import _SYMBOL_CONFIG, _CACHE_SUFFIXES, _get_cache_file


class TestGetCacheFile:
    def test_tx00_h4(self):
        assert _get_cache_file("TX00", (0, 240)) == "TXF1_H4.csv"

    def test_mtx00_h4(self):
        assert _get_cache_file("MTX00", (0, 240)) == "TMF1_H4.csv"

    def test_all_timeframes_tx00(self):
        expected = {
            (0, 15): "TXF1_15m.csv",
            (0, 60): "TXF1_1H.csv",
            (0, 240): "TXF1_H4.csv",
            (4, 1): "TXF1_1D.csv",
        }
        for key, filename in expected.items():
            assert _get_cache_file("TX00", key) == filename

    def test_all_timeframes_mtx00(self):
        expected = {
            (0, 15): "TMF1_15m.csv",
            (0, 60): "TMF1_1H.csv",
            (0, 240): "TMF1_H4.csv",
            (4, 1): "TMF1_1D.csv",
        }
        for key, filename in expected.items():
            assert _get_cache_file("MTX00", key) == filename

    def test_unknown_symbol(self):
        assert _get_cache_file("UNKNOWN", (0, 240)) is None

    def test_unknown_timeframe(self):
        assert _get_cache_file("TX00", (0, 999)) is None


class TestSymbolConfig:
    def test_point_values(self):
        assert _SYMBOL_CONFIG["TX00"]["pv"] == 200
        assert _SYMBOL_CONFIG["MTX00"]["pv"] == 50

    def test_tv_symbols(self):
        assert _SYMBOL_CONFIG["TX00"]["tv"] == "TXF1!"
        assert _SYMBOL_CONFIG["MTX00"]["tv"] == "TMF1!"


class TestCacheFilesExist:
    """Verify actual CSV files exist in data/ for both symbols."""

    data_dir = os.path.join(project_root, "data")

    @pytest.mark.parametrize("symbol", ["TX00", "MTX00"])
    @pytest.mark.parametrize("kline_key", list(_CACHE_SUFFIXES.keys()))
    def test_csv_exists(self, symbol, kline_key):
        filename = _get_cache_file(symbol, kline_key)
        assert filename is not None
        path = os.path.join(self.data_dir, filename)
        assert os.path.exists(path), f"Missing data file: {path}"
