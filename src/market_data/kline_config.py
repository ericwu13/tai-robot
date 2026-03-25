"""KLine configuration — symbol mappings, interval constants, and pure helpers.

Extracted from run_backtest.py to enable unit testing and reuse across
backtest, live, and data-fetching modules.
"""

from __future__ import annotations

import calendar
import os
from datetime import datetime, timedelta


# ── Interval mappings ──

TV_INTERVALS = {
    (0, 1): "in_1_minute", (0, 5): "in_5_minute", (0, 15): "in_15_minute",
    (0, 30): "in_30_minute", (0, 60): "in_1_hour", (0, 120): "in_2_hour",
    (0, 180): "in_3_hour", (0, 240): "in_4_hour",
    (4, 1): "in_daily", (5, 1): "in_weekly", (6, 1): "in_monthly",
}

INTERVAL_SECONDS = {
    (0, 240): 14400,
    (0, 60): 3600,
    (0, 30): 1800,
    (0, 15): 900,
    (0, 5): 300,
    (0, 1): 60,
    (4, 1): 86400,
}

CACHE_SUFFIXES = {
    (0, 15): "_15m.csv",
    (0, 60): "_1H.csv",
    (0, 240): "_H4.csv",
    (4, 1): "_1D.csv",
}

LIVE_CHART_TIMEFRAMES = {
    "Native": None,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1H": 3600,
    "4H": 14400,
}


# ── Symbol configuration ──

SYMBOL_CONFIG = {
    "TX00": {"prefix": "TXF1", "tv": "TXF1!", "pv": 200, "tick_divisor": 100,
             "taifex_id": "TX", "order_symbol": "TXFD0", "init_margin": 322000},
    "MTX00": {"prefix": "TMF1", "tv": "TMF1!", "pv": 50, "tick_divisor": 100,
              "taifex_id": "MTX", "order_symbol": "MTXFD0",
              "kline_symbol": "TX00", "tick_symbol": "TX00", "init_margin": 80500},
    "TMF00": {"prefix": "IMF1", "tv": "IMF1!", "pv": 10, "tick_divisor": 100,
              "taifex_id": "TMF", "order_symbol": "TM0000",
              "kline_symbol": "TX00", "tick_symbol": "TX00", "init_margin": 16100},
}

_MONTH_CODES = "ABCDEFGHIJKL"  # A=Jan .. L=Dec


# ── Pure helper functions ──

def get_near_month_symbol(product_code: str, now: datetime | None = None) -> str:
    """Compute near-month futures order symbol like TMFC6.

    Format: {product}{month_letter}{year_digit}
    Month letters: A=Jan, B=Feb, C=Mar, D=Apr, ... L=Dec
    Year digit: last digit of year (6=2026)

    Taiwan futures settle on the 3rd Wednesday of the expiry month.
    If today is past the 3rd Wednesday, use next month.
    """
    if now is None:
        from src.live.live_runner import _taipei_now
        now = _taipei_now()

    year, month = now.year, now.month
    cal = calendar.monthcalendar(year, month)
    wed_count = 0
    third_wed_day = None
    for week in cal:
        if week[2] != 0:  # Wednesday exists in this week
            wed_count += 1
            if wed_count == 3:
                third_wed_day = week[2]
                break

    if now.day > third_wed_day:
        month += 1
        if month > 12:
            month = 1
            year += 1

    month_letter = _MONTH_CODES[month - 1]
    year_digit = year % 10
    return f"{product_code}{month_letter}{year_digit}"


def resolve_order_symbol(symbol: str) -> str:
    """Resolve the order symbol for a given COM quote symbol."""
    cfg = SYMBOL_CONFIG.get(symbol, {})
    order_sym = cfg.get("order_symbol", symbol)
    if order_sym == "auto":
        product_code = cfg.get("taifex_id", symbol)
        order_sym = get_near_month_symbol(product_code)
    return order_sym


def get_cache_file(symbol: str, kline_key: tuple) -> str | None:
    """Return the cache CSV filename for a given symbol and kline key, or None."""
    cfg = SYMBOL_CONFIG.get(symbol)
    if not cfg:
        return None
    suffix = CACHE_SUFFIXES.get(kline_key)
    if not suffix:
        return None
    return cfg["prefix"] + suffix


def should_reuse_bars(
    raw_bars: list, raw_bars_key: tuple,
    symbol: str, kline_type: int, kline_minute: int,
) -> bool:
    """Return True if raw_bars can be reused for the given symbol and timeframe."""
    if not raw_bars:
        return False
    return raw_bars_key == (symbol, kline_type, kline_minute)


def filter_bars_by_date(bars: list, start_date: str, end_date: str) -> list:
    """Filter bars to [start_date, end_date] inclusive. Dates are YYYYMMDD strings."""
    dt_start = datetime.strptime(start_date, "%Y%m%d")
    dt_end = datetime.strptime(end_date, "%Y%m%d") + timedelta(days=1)
    return [b for b in bars if dt_start <= b.dt < dt_end]


def compute_chunk_ranges(
    start_date: str, end_date: str,
    kline_type: int, minute_num: int,
) -> list[tuple[str, str]]:
    """Split a date range into adaptive chunks for the Capital API.

    The API returns max ~316 bars per request. This function sizes chunks
    to target ~250 bars each based on the timeframe.

    Returns a list of (start_YYYYMMDD, end_YYYYMMDD) tuples.
    """
    dt_start = datetime.strptime(start_date, "%Y%m%d")
    dt_end = datetime.strptime(end_date, "%Y%m%d")

    if kline_type == 4:       # Daily
        bars_per_tday = 1
    elif minute_num >= 240:   # H4
        bars_per_tday = 6
    elif minute_num >= 60:    # 1H
        bars_per_tday = 14
    elif minute_num >= 30:    # 30m
        bars_per_tday = 28
    elif minute_num >= 15:    # 15m
        bars_per_tday = 56
    elif minute_num >= 5:     # 5m
        bars_per_tday = 60
    else:                     # 1m
        bars_per_tday = 300

    trading_days = 250 // bars_per_tday
    chunk_days = max(5, int(trading_days * 7 / 5))

    chunks = []
    cursor = dt_start
    while cursor < dt_end:
        chunk_end = min(cursor + timedelta(days=chunk_days), dt_end)
        chunks.append((cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        cursor = chunk_end + timedelta(days=1)

    return chunks


def detect_tv_source_tz(df):
    """Detect the source timezone of tvDatafeed timestamps.

    All bar.dt must be naive Taiwan time (TWT/UTC+8).

    Generalized detection using TAIFEX gap hours — no assumptions
    about bar alignment, session start times, or minute offsets.

    TAIFEX has known gaps where no trading occurs (TWT):
    - 05:01–08:44  (night close → day open)
    - 13:46–14:59  (day close → night open)

    Try candidate timezones.  The correct one produces ZERO bars
    in gap hours after conversion to TWT.

    Returns a ZoneInfo for the source timezone, or None if already TWT.
    """
    if df is None or df.empty:
        return None

    from zoneinfo import ZoneInfo
    _tz_taipei = ZoneInfo("Asia/Taipei")

    if df.index[0].to_pydatetime().tzinfo is not None:
        return None

    def _in_gap(h, m):
        """True if (h, m) in TWT falls in a TAIFEX no-trade gap."""
        t = h * 60 + m
        return (301 <= t <= 524) or (826 <= t <= 899)

    bar_dts = [dt.to_pydatetime() for dt in df.index]

    candidates = [
        None,
        ZoneInfo("America/Los_Angeles"),
        ZoneInfo("UTC"),
        ZoneInfo("America/New_York"),
        ZoneInfo("America/Chicago"),
    ]

    for tz in candidates:
        has_gap_bar = False
        for dt in bar_dts:
            if tz is None:
                h, m = dt.hour, dt.minute
            else:
                c = dt.replace(tzinfo=tz).astimezone(_tz_taipei)
                h, m = c.hour, c.minute
            if _in_gap(h, m):
                has_gap_bar = True
                break
        if not has_gap_bar:
            return tz  # None means already TWT

    return None  # can't detect — assume TWT
