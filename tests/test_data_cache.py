"""Tests for data source cache."""

import tempfile
from datetime import date, datetime
from pathlib import Path

from src.data_sources.cache import (
    get_cache_path,
    save_bars_csv,
    load_bars_csv,
    cache_covers_range,
)
from src.market_data.models import Bar


def _make_bars():
    return [
        Bar("TXF1", datetime(2025, 1, 2), 22935, 22995, 22689, 22842, 78968, 86400),
        Bar("TXF1", datetime(2025, 1, 3), 22900, 23050, 22800, 23000, 85000, 86400),
        Bar("TXF1", datetime(2025, 1, 6), 23100, 23200, 23000, 23150, 90000, 86400),
    ]


def test_get_cache_path():
    p = get_cache_path("taifex", "TX_daily", ".csv")
    assert p.name == "TX_daily.csv"
    assert "taifex" in str(p)


def test_save_and_load_roundtrip():
    bars = _make_bars()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.csv"
        save_bars_csv(bars, path)
        loaded = load_bars_csv(path, symbol="TXF1", interval=86400)
        assert loaded is not None
        assert len(loaded) == 3
        assert loaded[0].open == 22935
        assert loaded[0].close == 22842
        assert loaded[0].volume == 78968
        assert loaded[0].symbol == "TXF1"
        assert loaded[0].interval == 86400
        assert loaded[0].dt == datetime(2025, 1, 2)


def test_load_nonexistent_returns_none():
    result = load_bars_csv(Path("/tmp/nonexistent_xyz.csv"), "X", 60)
    assert result is None


def test_save_creates_parent_dirs():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "sub" / "deep" / "test.csv"
        save_bars_csv(_make_bars(), path)
        assert path.exists()


def test_cache_covers_range_true():
    bars = _make_bars()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.csv"
        save_bars_csv(bars, path)
        assert cache_covers_range(path, date(2025, 1, 2), date(2025, 1, 6))
        assert cache_covers_range(path, date(2025, 1, 3), date(2025, 1, 3))


def test_cache_covers_range_false_start():
    bars = _make_bars()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.csv"
        save_bars_csv(bars, path)
        assert not cache_covers_range(path, date(2025, 1, 1), date(2025, 1, 6))


def test_cache_covers_range_false_end():
    bars = _make_bars()
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.csv"
        save_bars_csv(bars, path)
        assert not cache_covers_range(path, date(2025, 1, 2), date(2025, 1, 10))


def test_cache_covers_range_nonexistent():
    assert not cache_covers_range(Path("/tmp/nonexistent_xyz.csv"), date(2025, 1, 1), date(2025, 1, 31))


def test_load_preserves_sort_order():
    bars = list(reversed(_make_bars()))
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.csv"
        save_bars_csv(bars, path)
        loaded = load_bars_csv(path, "TXF1", 86400)
        assert loaded[0].dt < loaded[1].dt < loaded[2].dt


def test_save_empty_bars():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.csv"
        save_bars_csv([], path)
        loaded = load_bars_csv(path, "TXF1", 86400)
        assert loaded == []
