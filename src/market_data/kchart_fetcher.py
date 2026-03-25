"""KChart data fetching — unified fetcher for COM API, TradingView, and TAIFEX.

Manages fetch state (chunk tracking, data accumulation) and provides pure
data-conversion functions. No Tkinter or COM dependencies — the GUI layer
calls COM/Tkinter and feeds results back here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from src.market_data.models import Bar
from src.market_data.kline_config import (
    TV_INTERVALS, INTERVAL_SECONDS, SYMBOL_CONFIG, detect_tv_source_tz,
    compute_chunk_ranges,
)
from src.backtest.data_loader import parse_kline_strings

_log = logging.getLogger(__name__)


# ── Result types ──

@dataclass
class FetchResult:
    """Result of any data fetch."""
    bars: list[Bar] = field(default_factory=list)
    source_tz: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.bars) > 0


@dataclass
class TvFetchError:
    """Error from a TradingView data fetch."""
    message: str
    is_network: bool = False


@dataclass
class ChunkRequest:
    """Parameters for a single COM API KLine request."""
    kline_symbol: str       # actual COM symbol (e.g. TX00 for MTX00)
    kline_type: int
    session: int            # 1=AM session, 0=full
    trade_session: int      # 0=full
    start_date: str         # YYYYMMDD
    end_date: str           # YYYYMMDD
    minute_num: int
    chunk_index: int        # 0-based
    total_chunks: int


# ── Pure helpers ──

def resolve_kline_symbol(symbol: str) -> str:
    """Resolve the actual KLine symbol (e.g. MTX00 → TX00 for shared index)."""
    cfg = SYMBOL_CONFIG.get(symbol, {})
    return cfg.get("kline_symbol", symbol)


def resolve_tv_symbol(symbol: str) -> str:
    """Resolve the TradingView symbol for a COM symbol."""
    cfg = SYMBOL_CONFIG.get(symbol)
    return cfg["tv"] if cfg else symbol


def dedup_bars(bars: list[Bar]) -> list[Bar]:
    """Remove duplicate bars by datetime, preserving order."""
    seen = set()
    unique = []
    for b in bars:
        if b.dt not in seen:
            seen.add(b.dt)
            unique.append(b)
    return unique


def parse_and_dedup_kline(kline_data: list[str], symbol: str,
                          interval: int) -> list[Bar]:
    """Parse KLine strings into Bar objects and deduplicate."""
    bars = parse_kline_strings(kline_data, symbol=symbol, interval=interval)
    return dedup_bars(bars)


def tv_dataframe_to_bars(df, symbol: str, interval: int) -> FetchResult:
    """Convert a tvDatafeed DataFrame to a list of Bar objects.

    Handles timezone detection and conversion to naive TWT (UTC+8).
    """
    if df is None or df.empty:
        return FetchResult(error="No data")

    from zoneinfo import ZoneInfo
    _tz_taipei = ZoneInfo("Asia/Taipei")
    source_tz = detect_tv_source_tz(df)

    bars = []
    for dt_idx, row in df.iterrows():
        dt_raw = dt_idx.to_pydatetime()
        if dt_raw.tzinfo is not None:
            dt_twt = dt_raw.astimezone(_tz_taipei).replace(tzinfo=None)
        elif source_tz:
            dt_twt = dt_raw.replace(tzinfo=source_tz).astimezone(
                _tz_taipei).replace(tzinfo=None)
        else:
            dt_twt = dt_raw
        bars.append(Bar(
            symbol=symbol, dt=dt_twt,
            open=round(row["open"]), high=round(row["high"]),
            low=round(row["low"]), close=round(row["close"]),
            volume=int(row.get("volume", 0)),
            interval=interval,
        ))

    bars.sort(key=lambda b: b.dt)
    tz_name = str(source_tz) if source_tz else None
    return FetchResult(bars=bars, source_tz=tz_name)


def tv_dataframe_to_kline_strings(df) -> list[str]:
    """Convert a tvDatafeed DataFrame to KLine format strings.

    Used for live warmup where LiveRunner expects KLine strings
    in "MM/DD/YYYY HH:MM,O,H,L,C,V" format.
    """
    strings = []
    for dt_idx, row in df.iterrows():
        dt = dt_idx.to_pydatetime()
        strings.append(
            f"{dt.strftime('%m/%d/%Y %H:%M')},"
            f"{round(row['open'])},{round(row['high'])},"
            f"{round(row['low'])},{round(row['close'])},"
            f"{int(row.get('volume', 0))}"
        )
    return strings


def fetch_tv_dataframe(tv_symbol: str, exchange: str, tv_interval,
                       max_retries: int = 3, retry_delay: float = 2.0):
    """Fetch a DataFrame from TradingView with retry logic.

    Returns:
        (df, error) tuple. df is the DataFrame or None.
        error is a TvFetchError or None.
    """
    from tvDatafeed import TvDatafeed

    net_errors = (ConnectionError, OSError, TimeoutError)
    try:
        import websocket
        net_errors = (ConnectionError, OSError, TimeoutError,
                      websocket.WebSocketException)
    except ImportError:
        pass

    df = None
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            tv = TvDatafeed()
            df = tv.get_hist(symbol=tv_symbol, exchange=exchange,
                             interval=tv_interval, n_bars=5000)
            if df is not None and not df.empty:
                return df, None
            _log.info("TV attempt %d/%d: no data", attempt, max_retries)
        except net_errors as e:
            last_err = e
            _log.info("TV attempt %d/%d network error: [%s] %s",
                       attempt, max_retries, type(e).__name__, e)
            if attempt < max_retries:
                time.sleep(retry_delay)
        except Exception as e:
            return None, TvFetchError(
                message=f"TradingView error: [{type(e).__name__}] {e}",
                is_network=False)

    if last_err:
        return None, TvFetchError(
            message=f"Network error after {max_retries} retries: {last_err}",
            is_network=True)
    return None, TvFetchError(message="No data from TradingView")


# ── KChartFetcher class ──

class KChartFetcher:
    """Unified KLine data fetcher managing state for COM API chunked fetches
    and providing stateless helpers for TradingView and TAIFEX sources.

    Usage (COM API path):
        fetcher = KChartFetcher()
        fetcher.start_api_fetch("TX00", 0, 240, "20260101", "20260301")
        while (chunk := fetcher.next_chunk()):
            # GUI calls COM: skQ.SKQuoteLib_RequestKLineAMByDate(
            #     chunk.kline_symbol, chunk.kline_type, chunk.session,
            #     chunk.trade_session, chunk.start_date, chunk.end_date,
            #     chunk.minute_num)
            # COM callback feeds data: fetcher.on_kline_data(bstrData)
            # COM complete callback: fetcher.advance_chunk()
        bars = fetcher.get_api_bars()

    Usage (TradingView path):
        fetcher = KChartFetcher()
        result = fetcher.fetch_tv("TX00", 0, 240)
        if result.ok:
            bars = result.bars

    Usage (live warmup via TV):
        fetcher = KChartFetcher()
        kline_strings = fetcher.fetch_tv_kline_strings("TX00", 0, 240)
    """

    def __init__(self):
        # ── COM API chunk state ──
        self.kline_data: list[str] = []
        self._chunks: list[tuple[str, str]] = []
        self._chunk_idx: int = 0
        self._symbol: str = ""
        self._kline_type: int = 0
        self._minute_num: int = 0
        self._chunk_bar_count: int = 0

    # ── COM API path ──

    def start_api_fetch(self, symbol: str, kline_type: int, minute_num: int,
                        start_date: str, end_date: str) -> list[tuple[str, str]]:
        """Prepare a chunked COM API fetch. Returns the chunk date ranges."""
        self.kline_data = []
        self._chunks = compute_chunk_ranges(start_date, end_date,
                                            kline_type, minute_num)
        self._chunk_idx = 0
        self._symbol = symbol
        self._kline_type = kline_type
        self._minute_num = minute_num
        self._chunk_bar_count = 0
        return list(self._chunks)

    def next_chunk(self) -> ChunkRequest | None:
        """Get parameters for the next COM API KLine request, or None if done."""
        if self._chunk_idx >= len(self._chunks):
            return None
        start, end = self._chunks[self._chunk_idx]
        kline_sym = resolve_kline_symbol(self._symbol)
        return ChunkRequest(
            kline_symbol=kline_sym,
            kline_type=self._kline_type,
            session=1,          # AM session
            trade_session=0,    # full
            start_date=start,
            end_date=end,
            minute_num=self._minute_num,
            chunk_index=self._chunk_idx,
            total_chunks=len(self._chunks),
        )

    def advance_chunk(self):
        """Mark the current chunk as complete, advance to next."""
        self._chunk_idx += 1
        self._chunk_bar_count = 0

    @property
    def chunks_done(self) -> bool:
        """True when all chunks have been fetched."""
        return self._chunk_idx >= len(self._chunks)

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def kline_type(self) -> int:
        return self._kline_type

    @property
    def minute_num(self) -> int:
        return self._minute_num

    def on_kline_data(self, data: str):
        """Accumulate a KLine data string from COM OnNotifyKLineData callback."""
        self.kline_data.append(data)
        self._chunk_bar_count += 1

    @property
    def chunk_bar_count(self) -> int:
        return self._chunk_bar_count

    @property
    def total_bar_count(self) -> int:
        return len(self.kline_data)

    def get_api_bars(self, symbol: str | None = None,
                     interval: int | None = None) -> list[Bar]:
        """Parse and dedup the accumulated KLine data into Bar objects.

        If symbol/interval are not given, uses SYMBOL_CONFIG to resolve them.
        """
        if symbol is None:
            cfg = SYMBOL_CONFIG.get(self._symbol, {})
            symbol = cfg.get("prefix", self._symbol)
        if interval is None:
            interval = INTERVAL_SECONDS.get(
                (self._kline_type, self._minute_num), 14400)
        return parse_and_dedup_kline(self.kline_data, symbol=symbol,
                                     interval=interval)

    # ── TradingView path ──

    def fetch_tv(self, symbol: str, kline_type: int,
                 kline_minute: int) -> FetchResult:
        """Fetch data from TradingView and return as Bar objects.

        Handles symbol resolution, interval mapping, retry, timezone
        detection, and DataFrame→Bar conversion.
        """
        from tvDatafeed import Interval as TvInterval

        tv_interval_name = TV_INTERVALS.get((kline_type, kline_minute))
        if not tv_interval_name:
            return FetchResult(
                error=f"Unsupported interval: type={kline_type} min={kline_minute}")

        tv_interval = getattr(TvInterval, tv_interval_name)
        tv_symbol = resolve_tv_symbol(symbol)
        interval = INTERVAL_SECONDS.get((kline_type, kline_minute), 14400)

        df, err = fetch_tv_dataframe(tv_symbol, "TAIFEX", tv_interval)
        if err:
            return FetchResult(error=err.message)

        return tv_dataframe_to_bars(df, symbol=tv_symbol, interval=interval)

    @staticmethod
    def fetch_tv_kline_strings(symbol: str, kline_type: int,
                               kline_minute: int):
        """Fetch TV data and return as KLine format strings (for live warmup).

        Returns:
            (kline_strings, error) — strings list or None, error message or None.
        """
        from tvDatafeed import Interval as TvInterval

        tv_interval_name = TV_INTERVALS.get((kline_type, kline_minute))
        if not tv_interval_name:
            return None, f"Unsupported interval: type={kline_type} min={kline_minute}"

        tv_interval = getattr(TvInterval, tv_interval_name)
        tv_symbol = resolve_tv_symbol(symbol)

        df, err = fetch_tv_dataframe(tv_symbol, "TAIFEX", tv_interval)
        if err:
            return None, err.message

        return tv_dataframe_to_kline_strings(df), None

    # ── Reset ──

    def reset(self):
        """Clear all fetch state."""
        self.kline_data = []
        self._chunks = []
        self._chunk_idx = 0
        self._symbol = ""
        self._kline_type = 0
        self._minute_num = 0
        self._chunk_bar_count = 0
