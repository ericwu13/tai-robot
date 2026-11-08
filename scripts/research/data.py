"""Bar sources for research backtests — all return ``list[Bar]`` in TPE (naive).

Sources, best first for recent history:

* bot-dir 1-min CSVs (``bars_1m_YYYYMMDD.csv``) — real live data, TPE.
  Read locally (``load_bot_dirs``) or fetched from the Steven dashboard
  (``dashboard_bars`` → ``/api/bars``; read-only GET).
* TradingView via tvDatafeed (``fetch_tv``) — no login, 5,000-bar cap per
  request (30m ≈ 6 months, 1H ≈ 13 months, 4H ≈ 3.4 years, 1D ≈ 2012).
  ``tv_dataframe_to_bars`` detects the source zone and converts to TPE.
* Capital COM KLine — needs a login + the SDK; use
  ``run_bot_cli.py backtest --source api`` instead of re-implementing it.

Never use the committed ``data/*.csv`` exports: they stop at 2026-03 and
mix time zones (hand exports are stamped in the exporter's browser zone,
US-Pacific for the owner; ``load_bars_from_csv`` does NOT correct that).

    python -m scripts.research.data dirs <bot_dir>... --out bars.pkl
    python -m scripts.research.data dashboard <url> <bot_id> --days 60 --out bars.pkl
    python -m scripts.research.data tv TMF00 1H --out tv_1h.pkl
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

from src.market_data.models import Bar

# Pure 1-min → N-min aggregation (same aggregator the live bots use).
INTERVALS = (300, 900, 1800, 3600, 14400)
DASHBOARD_DAYS_PER_CALL = 10   # scripts/dashboard/snapshot.py BARS_DAYS_MAX

TV_INTERVAL_NAMES = {
    "1m": ("in_1_minute", 60), "5m": ("in_5_minute", 300),
    "15m": ("in_15_minute", 900), "30m": ("in_30_minute", 1800),
    "1H": ("in_1_hour", 3600), "4H": ("in_4_hour", 14400),
    "1D": ("in_daily", 86400),
}


def merge_1m(*bar_lists: list[Bar]) -> list[Bar]:
    """Union several 1-min series; on a duplicate timestamp keep the higher volume.

    Two bots recording the same contract can disagree on a bar (one missed
    ticks); the fuller bar is the better record.
    """
    by_dt: dict[datetime, Bar] = {}
    for bars in bar_lists:
        for b in bars:
            old = by_dt.get(b.dt)
            if old is None or b.volume > old.volume:
                by_dt[b.dt] = b
    return [by_dt[k] for k in sorted(by_dt)]


def load_bot_dirs(dirs: list[str], symbol: str = "TMF00") -> list[Bar]:
    """All saved 1-min bars from one or more bot data dirs, merged."""
    from src.live.live_runner import load_1m_bars_from_csvs
    return merge_1m(*(load_1m_bars_from_csvs(d, symbol) for d in dirs))


def aggregate_all(bars_1m: list[Bar], intervals=INTERVALS) -> dict[int, list[Bar]]:
    """{interval_seconds: bars}; key 60 is the input itself."""
    from src.live.bar_aggregator import aggregate_bars
    out = {60: list(bars_1m)}
    for iv in intervals:
        out[iv] = aggregate_bars(bars_1m, iv)
    return out


def dashboard_rows_to_bars(rows: list[dict], symbol: str) -> list[Bar]:
    """Convert ``/api/bars`` rows to Bars.

    The dashboard encodes TPE wall-clock as if it were UTC epoch seconds
    (``snapshot.chart_time``), so decoding as UTC gives back the TPE wall time.
    """
    out = []
    for r in rows:
        dt = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).replace(tzinfo=None)
        out.append(Bar(symbol=symbol, dt=dt, open=int(r["open"]), high=int(r["high"]),
                       low=int(r["low"]), close=int(r["close"]),
                       volume=int(r.get("volume", 0) or 0), interval=60))
    return out


def dashboard_bars(base_url: str, bot_id: str, days: int = 30, before: str = "",
                   symbol: str = "TMF00", timeout: float = 30.0) -> list[Bar]:
    """Read a Steven bot's recorded 1-min bars over the dashboard API (GET only).

    ``base_url`` is ``http://<Steven tailnet IP>:8765`` (from hosts.local.yaml).
    ``days`` counts daily CSV files; the server serves at most
    ``DASHBOARD_DAYS_PER_CALL`` per request, so this pages backwards with
    ``before=<oldest_day>`` until ``days`` files are read or history ends.
    """
    rows: list[dict] = []
    remaining = days
    while remaining > 0:
        q = {"id": bot_id, "days": str(min(remaining, DASHBOARD_DAYS_PER_CALL))}
        if before:
            q["before"] = before
        url = base_url.rstrip("/") + "/api/bars?" + urlencode(q)
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — tailnet URL from the operator
            payload = json.loads(resp.read().decode("utf-8"))
        page = payload.get("bars", [])
        oldest = payload.get("oldest_day", "")
        if not page or not oldest or oldest == before:
            break
        rows = page + rows
        remaining -= min(remaining, DASHBOARD_DAYS_PER_CALL)
        before = oldest
    return merge_1m(dashboard_rows_to_bars(rows, symbol))


def fetch_tv(symbol: str, interval: str, exchange: str = "TAIFEX"):
    """Download from TradingView (no login). Returns (bars, source_tz).

    ``symbol`` is a COM symbol (TMF00/MTX00/TX00) resolved through
    ``SYMBOL_CONFIG`` — see draft PR #260 for the TMF00/MTX00 mapping fix —
    or a raw TradingView symbol ending in ``!`` (e.g. ``TMF1!``).
    """
    from tvDatafeed import Interval
    from src.market_data.kchart_fetcher import (
        fetch_tv_dataframe, resolve_tv_symbol, tv_dataframe_to_bars)
    attr, seconds = TV_INTERVAL_NAMES[interval]
    tv_symbol = symbol if symbol.endswith("!") else resolve_tv_symbol(symbol)
    df, err = fetch_tv_dataframe(tv_symbol, exchange, getattr(Interval, attr))
    if err:
        raise RuntimeError(err.message)
    res = tv_dataframe_to_bars(df, symbol=symbol, interval=seconds)
    if res.error:
        raise RuntimeError(res.error)
    return res.bars, res.source_tz


def describe(bars: list[Bar]) -> str:
    if not bars:
        return "0 bars"
    days = sorted({b.dt.date() for b in bars})
    gaps = [(days[i - 1], days[i]) for i in range(1, len(days))
            if (days[i] - days[i - 1]).days > 4]
    return (f"{len(bars)} bars {bars[0].dt} → {bars[-1].dt}, {len(days)} days"
            + (f", gaps>4d: {gaps}" if gaps else ""))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dirs")
    d.add_argument("dirs", nargs="+")
    w = sub.add_parser("dashboard")
    w.add_argument("url")
    w.add_argument("bot_id")
    w.add_argument("--days", type=int, default=30)
    w.add_argument("--before", default="")
    t = sub.add_parser("tv")
    t.add_argument("symbol")
    t.add_argument("interval", choices=sorted(TV_INTERVAL_NAMES))
    for s in (d, w, t):
        s.add_argument("--symbol-label", default="TMF00")
        s.add_argument("--out", default="", help="pickle path for the bars list")
    a = p.parse_args(argv)
    if a.cmd == "dirs":
        bars = load_bot_dirs(a.dirs, a.symbol_label)
    elif a.cmd == "dashboard":
        bars = dashboard_bars(a.url, a.bot_id, a.days, a.before, a.symbol_label)
    else:
        bars, tz = fetch_tv(a.symbol, a.interval)
        print(f"source_tz={tz}")
    print(describe(bars))
    if a.out:
        with open(a.out, "wb") as fh:
            pickle.dump(bars, fh)
        print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    # cp950 consoles cannot print every char in the help/report text.
    sys.stdout.reconfigure(errors="replace")
    sys.exit(main())
