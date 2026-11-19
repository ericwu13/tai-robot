"""情緒走勢 vs 台指: the W3 sentiment series and index-futures price on one
time axis (``GET /api/news/trend[?symbol=&after=]``).

Two series, both TPE wall clock encoded as UTC (the page's chart convention):

* ``runs`` — one point per W3 scoring pass, from the WHOLE news log (the
  /api/news table only reads the newest lines). History therefore reaches
  as far back as the log does; the bridge trims it at
  ``rss_news_log.MAX_BYTES`` / ``KEEP_LINES``, not this page.
* ``candles`` — 30-min OHLC of one index-futures symbol, session-aligned
  like the bots' own bars (``session_align``), built from every bot's
  ``bars_1m_*.csv`` for that symbol. Bots on the same symbol hold the same
  1-min bars, so their buckets are merged (union), which also fills a day
  one bot missed. Only files from the day before the oldest run onward are
  read.

``after`` (chart seconds) returns only points at or after it, so the page
loads the full history once and then asks for the tail each refresh. The
newest candle is always re-sent (it is still forming).

Read-only: bot files go through ``fileio`` (share-delete) and are re-parsed
only when their stat changes.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone

from scripts.dashboard import fileio, news_feed, snapshot

TREND_INTERVAL_S = 1800                    # matches the scorer's 30-min cadence
# Index futures, most liquid first: the tie-break when two symbols cover
# the same number of days. Any other symbol can still be asked for.
PREFERRED_SYMBOLS = ("TX00", "MTX00", "TMF00")
_INDEX_PREFIXES = ("TX", "MTX", "TMF")

_lock = threading.Lock()
_runs_cache: dict = {}
_file_cache: dict[str, tuple] = {}


def _wall(t: int) -> datetime:
    """Chart seconds → naive TPE wall clock (what session_align expects)."""
    return datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None)


def _chart(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _stat_key(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


# ── runs: every scoring pass in the log ──────────────────────────────────

def _parse_runs(path: str) -> list[dict]:
    text = fileio.read_text(path)
    if not text:
        return []
    by_t: dict[int, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = news_feed.normalize(json.loads(line))
        except ValueError:
            continue
        if not r or not r["scored_at"]:
            continue
        try:
            dt = datetime.fromisoformat(r["scored_at"])
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=snapshot.TZ_TPE)
        t = snapshot.chart_time(dt)
        run = by_t.get(t)
        if run is None or run["scored_at"] != r["scored_at"] or run["session"] != r["session"]:
            # One point per chart second: a later pass at the same second wins.
            run = by_t[t] = {"t": t, "scored_at": r["scored_at"], "session": r["session"],
                             "run_net": r["run_net"], "session_net": r["session_net"],
                             "n": 0, "bullish": 0, "bearish": 0, "neutral": 0}
        run["n"] += 1
        run[r["direction"]] += 1
    return [by_t[t] for t in sorted(by_t)]


def read_runs(path: str | None) -> list[dict]:
    """Oldest-first scoring passes from the whole log (shared; don't mutate)."""
    if not path:
        return []
    key = _stat_key(path)
    with _lock:
        hit = _runs_cache.get(path)
        if key is not None and hit and hit[0] == key:
            return hit[1]
    runs = _parse_runs(path)
    if key is not None:
        with _lock:
            _runs_cache.clear()
            _runs_cache[path] = (key, runs)
    return runs


# ── candles: 30-min buckets per bar file, merged across bots ─────────────

def _file_buckets(path: str) -> dict[int, list]:
    """``{bucket_t: [first_t, last_t, open, high, low, close]}`` for one file."""
    key = _stat_key(path)
    with _lock:
        hit = _file_cache.get(path)
        if key is not None and hit and hit[0] == key:
            return hit[1]
    from src.market_data.sessions import session_align
    out: dict[int, list] = {}
    for bar in snapshot.read_bars(path):
        t = bar["time"]
        b = _chart(session_align(_wall(t), TREND_INTERVAL_S))
        cur = out.get(b)
        if cur is None:
            out[b] = [t, t, bar["open"], bar["high"], bar["low"], bar["close"]]
            continue
        if t < cur[0]:
            cur[0], cur[2] = t, bar["open"]
        if t >= cur[1]:
            cur[1], cur[5] = t, bar["close"]
        cur[3] = max(cur[3], bar["high"])
        cur[4] = min(cur[4], bar["low"])
    if key is not None:
        with _lock:
            if len(_file_cache) > 4000:
                _file_cache.clear()
            _file_cache[path] = (key, out)
    return out


def _merge(into: dict[int, list], add: dict[int, list]) -> None:
    for b, v in add.items():
        cur = into.get(b)
        if cur is None:
            into[b] = list(v)
            continue
        if v[0] < cur[0]:
            cur[0], cur[2] = v[0], v[2]
        if v[1] >= cur[1]:
            cur[1], cur[5] = v[1], v[5]
        cur[3] = max(cur[3], v[3])
        cur[4] = min(cur[4], v[4])


def symbol_files(base_dir: str) -> dict[str, list[tuple[str, str]]]:
    """``{symbol: [(yyyymmdd, path), ...]}`` over every bot dir."""
    out: dict[str, list[tuple[str, str]]] = {}
    for d in snapshot.discover_bot_dirs(base_dir):
        sym = os.path.basename(os.path.normpath(d)).partition("_")[0]
        if sym:
            out.setdefault(sym, []).extend(snapshot._newest(d, snapshot._BARS_RE))
    return out


def _is_index(sym: str) -> bool:
    return any(sym.upper().startswith(p) for p in _INDEX_PREFIXES)


def pick_symbol(files: dict[str, list], wanted: str = "") -> str:
    """The asked-for symbol when bots have it, else the index-futures symbol
    with the most distinct days of bars (ties: TX, MTX, TMF)."""
    if wanted and wanted in files:
        return wanted
    cands = [s for s in files if _is_index(s)] or list(files)
    if not cands:
        return ""

    def rank(s):
        pref = PREFERRED_SYMBOLS.index(s) if s in PREFERRED_SYMBOLS else len(PREFERRED_SYMBOLS)
        return (-len({d for d, _ in files[s]}), pref, s)
    return min(cands, key=rank)


def build_candles(file_list: list[tuple[str, str]], since_day: str = "") -> list[dict]:
    merged: dict[int, list] = {}
    for day, path in file_list:
        if since_day and day < since_day:
            continue
        _merge(merged, _file_buckets(path))
    return [{"time": b, "open": v[2], "high": v[3], "low": v[4], "close": v[5]}
            for b, v in sorted(merged.items())]


# ── payload ──────────────────────────────────────────────────────────────

def trend(news_log_path: str | None, base_dir: str, symbol: str = "",
          after: int | None = None) -> dict:
    runs = read_runs(news_log_path)
    files = symbol_files(base_dir) if base_dir else {}
    sym = pick_symbol(files, symbol)
    since_day = ""
    if runs:
        since_day = (_wall(runs[0]["t"]) - timedelta(days=1)).strftime("%Y%m%d")
    candles = build_candles(files.get(sym, []), since_day) if sym else []
    if after is not None:
        runs = [r for r in runs if r["t"] >= after]
        # The newest bucket may still be forming: always send it again.
        cut = min(after, candles[-1]["time"]) if candles else after
        candles = [c for c in candles if c["time"] >= cut]
    symbols = sorted(files, key=lambda s: (not _is_index(s), s))
    return {"symbol": sym, "symbols": symbols, "interval": TREND_INTERVAL_S,
            "runs": [{"t": r["t"], "run_net": r["run_net"], "session_net": r["session_net"],
                      "session": r["session"], "n": r["n"]} for r in runs],
            "candles": candles}
