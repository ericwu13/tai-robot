"""TAIFEX (期交所) daily futures data downloader.

Downloads daily OHLCV bars from the Taiwan Futures Exchange public CSV endpoint.
No API key or broker account needed.

Endpoint: POST https://www.taifex.com.tw/cht/3/futDataDown
- Max ~1 month per request (monthly chunking required)
- Response encoding: cp950 (Big5 extension)
- CSV columns: Date,Contract,ContractMonth,Open,High,Low,Close,Change,Change%,
               Volume,Settlement,OI,BestBid,BestAsk,HistHigh,HistLow,
               TradingHalt,Session,SpreadVol
"""

from __future__ import annotations

import csv
import io
import time
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Callable

from ..market_data.models import Bar

_URL = "https://www.taifex.com.tw/cht/3/futDataDown"

# Column indices in the CSV (0-based, after header)
_COL_DATE = 0
_COL_CONTRACT = 1
_COL_MONTH = 2
_COL_OPEN = 3
_COL_HIGH = 4
_COL_LOW = 5
_COL_CLOSE = 6
_COL_VOLUME = 9
_COL_SESSION = 17

_SESSION_REGULAR = "\u4e00\u822c"  # 一般 (regular/full session)

_DAILY_INTERVAL = 86400


def _month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split a date range into monthly chunks (max ~1 month each)."""
    chunks = []
    cur = start
    while cur <= end:
        # End of current month
        if cur.month == 12:
            month_end = date(cur.year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(cur.year, cur.month + 1, 1) - timedelta(days=1)
        chunk_end = min(month_end, end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def _fetch_csv_chunk(commodity_id: str, start: date, end: date) -> str:
    """Fetch one month chunk of CSV data from TAIFEX."""
    params = {
        "down_type": "1",
        "commodity_id": commodity_id,
        "queryStartDate": start.strftime("%Y/%m/%d"),
        "queryEndDate": end.strftime("%Y/%m/%d"),
    }
    data = urllib.parse.urlencode(params).encode("ascii")
    req = urllib.request.Request(_URL, data=data, method="POST")
    req.add_header("User-Agent", "Mozilla/5.0")

    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()

    text = raw.decode("cp950", errors="replace")
    # Check if we got an HTML error page instead of CSV
    if text.strip().startswith("<!DOCTYPE") or text.strip().startswith("<HTML"):
        return ""
    return text


def _parse_near_month(rows_for_date: list[list[str]]) -> str | None:
    """Pick the near-month contract from rows sharing the same date.

    Near-month = smallest ContractMonth that is a single month (no '/').
    """
    months = set()
    for row in rows_for_date:
        m = row[_COL_MONTH].strip()
        if "/" not in m and m:
            months.add(m)
    return min(months) if months else None


def parse_taifex_csv(
    csv_text: str,
    commodity_id: str,
    symbol: str,
    price_multiplier: int = 1,
) -> list[Bar]:
    """Parse TAIFEX CSV text into Bar objects.

    Filters to: matching contract, regular session (一般), near-month only.
    Spread contracts (month contains '/') are excluded.

    Args:
        csv_text: Raw CSV text decoded from cp950.
        commodity_id: e.g. "TX", "MTX" — must match Contract column.
        symbol: Symbol name for the Bar objects (e.g. "TXF1").
        price_multiplier: Multiply prices by this factor (1 = keep as-is).

    Returns:
        List of Bar objects sorted by date.
    """
    if not csv_text.strip():
        return []

    reader = csv.reader(io.StringIO(csv_text))
    # Skip header
    try:
        next(reader)
    except StopIteration:
        return []

    # Group rows by date, filtering to commodity + regular session + non-spread
    date_rows: dict[str, list[list[str]]] = {}
    for row in reader:
        if len(row) < 18:
            continue
        contract = row[_COL_CONTRACT].strip()
        if contract != commodity_id:
            continue
        month = row[_COL_MONTH].strip()
        if "/" in month:  # skip spread contracts
            continue
        session = row[_COL_SESSION].strip()
        if session != _SESSION_REGULAR:
            continue
        dt_str = row[_COL_DATE].strip()
        if dt_str not in date_rows:
            date_rows[dt_str] = []
        date_rows[dt_str].append(row)

    bars = []
    for dt_str, rows in date_rows.items():
        near_month = _parse_near_month(rows)
        if not near_month:
            continue
        # Pick the near-month row
        for row in rows:
            if row[_COL_MONTH].strip() != near_month:
                continue
            try:
                o = row[_COL_OPEN].strip()
                h = row[_COL_HIGH].strip()
                lo = row[_COL_LOW].strip()
                c = row[_COL_CLOSE].strip()
                v = row[_COL_VOLUME].strip()
                # Skip rows with missing prices
                if "-" in (o, h, lo, c) or not o:
                    continue
                dt = datetime.strptime(dt_str, "%Y/%m/%d")
                bars.append(Bar(
                    symbol=symbol,
                    dt=dt,
                    open=int(float(o)) * price_multiplier,
                    high=int(float(h)) * price_multiplier,
                    low=int(float(lo)) * price_multiplier,
                    close=int(float(c)) * price_multiplier,
                    volume=int(float(v)),
                    interval=_DAILY_INTERVAL,
                ))
            except (ValueError, IndexError):
                continue
            break  # only one row per date

    bars.sort(key=lambda b: b.dt)
    return bars


def fetch_futures_daily(
    commodity_id: str,
    start_date: date,
    end_date: date,
    symbol: str = "TXF1",
    price_multiplier: int = 1,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[Bar]:
    """Download daily futures OHLCV from TAIFEX exchange.

    Args:
        commodity_id: "TX" for TAIEX futures, "MTX" for mini, "TE" for electronics.
        start_date, end_date: Date range to fetch.
        symbol: Symbol name for Bar objects.
        price_multiplier: Multiply prices (1 = keep display prices like 22500).
        on_progress: Optional callback(current_chunk, total_chunks).

    Returns:
        Sorted list[Bar] with interval=86400 (daily).
    """
    chunks = _month_chunks(start_date, end_date)
    all_bars: list[Bar] = []

    for i, (cs, ce) in enumerate(chunks):
        if on_progress:
            on_progress(i, len(chunks))

        csv_text = _fetch_csv_chunk(commodity_id, cs, ce)
        bars = parse_taifex_csv(csv_text, commodity_id, symbol, price_multiplier)
        all_bars.extend(bars)

        # Rate limit: 2s between requests (skip after last chunk)
        if i < len(chunks) - 1:
            time.sleep(2)

    if on_progress:
        on_progress(len(chunks), len(chunks))

    all_bars.sort(key=lambda b: b.dt)
    return all_bars
