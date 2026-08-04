"""Tests for chart-boundary bar sanitizing and timestamp-anchored markers (issue #97).

lightweight-charts silently blanks the whole candle pane when chart.set()
receives a non-ascending series, and trade markers anchored by LIFETIME bar
index land on the wrong candles once the in-memory window has rolled.
"""

import calendar
from datetime import datetime

from src.backtest.broker import Trade, OrderSide
from src.backtest.chart import (
    LiveChart,
    _ensure_ascending,
    _marker_candle_index,
    _trade_dt_to_epoch,
)
from src.market_data.models import Bar


# ── Helpers ──

def _bar(dt_str, close=22500, volume=100, interval=900):
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    return Bar(symbol="TX00", dt=dt, open=close - 5, high=close + 10,
               low=close - 10, close=close, volume=volume, interval=interval)


def _epoch(dt_str):
    return calendar.timegm(
        datetime.strptime(dt_str, "%Y-%m-%d %H:%M").timetuple())


def _is_ascending(bars):
    return all(bars[i].dt > bars[i - 1].dt for i in range(1, len(bars)))


# ── _ensure_ascending ──

class TestEnsureAscending:
    def test_clean_list_returned_unchanged(self):
        bars = [_bar("2026-08-01 04:15"), _bar("2026-08-01 04:30"),
                _bar("2026-08-01 04:45")]
        assert _ensure_ascending(bars) is bars

    def test_empty_and_single_returned_unchanged(self):
        empty = []
        single = [_bar("2026-08-01 04:45")]
        assert _ensure_ascending(empty) is empty
        assert _ensure_ascending(single) is single

    def test_descending_pair_midlist_is_reordered(self):
        """One descending timestamp anywhere blanks the chart — must be fixed."""
        bars = [
            _bar("2026-08-01 04:15", 22500),
            _bar("2026-08-01 04:45", 22530),
            _bar("2026-08-01 04:30", 22510),   # backwards
            _bar("2026-08-01 05:00", 22540),
        ]
        out = _ensure_ascending(bars)

        assert _is_ascending(out)
        assert [b.dt.strftime("%H:%M") for b in out] == [
            "04:15", "04:30", "04:45", "05:00"]
        assert len(out) == 4  # distinct timestamps: nothing dropped

    def test_duplicate_timestamp_keeps_last_occurrence(self):
        """Mirrors lightweight-charts series.update() last-write-wins."""
        real = _bar("2026-08-01 04:45", 22530, volume=620)
        stub = _bar("2026-08-01 04:45", 22531, volume=29)   # orphan-flush stub
        bars = [_bar("2026-08-01 04:30", 22510), real, stub,
                _bar("2026-08-01 04:15", 22500)]

        out = _ensure_ascending(bars)

        assert _is_ascending(out)
        assert len(out) == 3
        kept = [b for b in out if b.dt.strftime("%H:%M") == "04:45"]
        assert len(kept) == 1
        assert kept[0] is stub

    def test_input_list_not_mutated(self):
        bars = [_bar("2026-08-01 04:45"), _bar("2026-08-01 04:30")]
        original = list(bars)
        _ensure_ascending(bars)
        assert bars == original

    def test_warning_logged_with_counts(self, caplog):
        bars = [_bar("2026-08-01 04:45", 1), _bar("2026-08-01 04:30", 2),
                _bar("2026-08-01 04:30", 3)]
        with caplog.at_level("WARNING", logger="src.backtest.chart"):
            _ensure_ascending(bars)
        assert "issue #97" in caplog.text
        assert "3 in, 2 out" in caplog.text


# ── _trade_dt_to_epoch / _marker_candle_index ──

class TestTradeDtToEpoch:
    def test_matches_lightweight_charts_epoch_convention(self):
        """Candle times = pd.to_datetime(...).astype('int64') // 10**9.

        Naive datetimes are therefore treated as UTC; datetime.timestamp()
        would apply the local offset and shift every marker.
        """
        import pandas as pd

        dt_str = "2026-08-01 04:45"
        expected = int(
            pd.to_datetime([dt_str]).as_unit('ns').astype('int64')[0] // 10 ** 9)
        assert _trade_dt_to_epoch(dt_str) == expected
        assert _trade_dt_to_epoch(dt_str + ":00") == expected

    def test_seconds_precision_parsed(self):
        assert (_trade_dt_to_epoch("2026-08-01 09:15:59")
                == _epoch("2026-08-01 09:15") + 59)

    def test_empty_and_garbage_return_none(self):
        assert _trade_dt_to_epoch("") is None
        assert _trade_dt_to_epoch("not-a-date") is None


class TestMarkerCandleIndex:
    CANDLES = [_epoch("2026-08-01 09:00"), _epoch("2026-08-01 09:15"),
               _epoch("2026-08-01 09:30")]

    def test_moment_inside_window_maps_to_that_candle(self):
        assert _marker_candle_index(self.CANDLES, "2026-08-01 09:20") == 1

    def test_exact_candle_open_maps_to_that_candle(self):
        assert _marker_candle_index(self.CANDLES, "2026-08-01 09:15") == 1

    def test_seconds_precision_maps_to_containing_candle(self):
        assert _marker_candle_index(self.CANDLES, "2026-08-01 09:15:59") == 1

    def test_before_first_candle_returns_none(self):
        assert _marker_candle_index(self.CANDLES, "2026-08-01 08:59") is None

    def test_after_last_candle_clamps_to_last(self):
        """An exit landing past the last candle's window still belongs on the
        last candle — the chart simply hasn't drawn the next bar yet."""
        assert _marker_candle_index(self.CANDLES, "2026-08-01 10:30") == 2

    def test_empty_candles_returns_none(self):
        assert _marker_candle_index([], "2026-08-01 09:20") is None

    def test_missing_dt_returns_none(self):
        assert _marker_candle_index(self.CANDLES, "") is None

    def test_lifetime_index_would_have_missed_it(self):
        """Regression: trade indices are lifetime counters, candle list is a
        rolling window. Index 28_600 is out of bounds; the timestamp isn't."""
        t = Trade(tag="x", side=OrderSide.LONG, qty=1, entry_price=22500,
                  exit_price=22600, entry_bar_index=28_600,
                  exit_bar_index=28_602, entry_dt="2026-08-01 09:15",
                  exit_dt="2026-08-01 09:30")
        assert not (0 <= t.entry_bar_index < len(self.CANDLES))
        assert _marker_candle_index(self.CANDLES, t.entry_dt) == 1
        assert _marker_candle_index(self.CANDLES, t.exit_dt) == 2


# ── LiveChart integration ──

class TestLiveChartSanitize:
    def test_initial_bars_sanitized_and_closes_match(self):
        bars = [
            _bar("2026-08-01 04:15", 22500),
            _bar("2026-08-01 04:45", 22530),
            _bar("2026-08-01 04:30", 22510),   # backwards → blanks the pane
            _bar("2026-08-01 04:45", 22531),   # orphan-flush stub duplicate
        ]
        lc = LiveChart(initial_bars=bars, initial_trades=[])

        assert _is_ascending(lc._initial_bars)
        assert [b.dt.strftime("%H:%M") for b in lc._initial_bars] == [
            "04:15", "04:30", "04:45"]
        assert lc._closes == [b.close for b in lc._initial_bars]
        assert lc._closes == [22500, 22510, 22531]  # duplicate keeps LAST
