"""TAIFEX index-futures margins, kept in sync from the exchange.

The real-order entry gate (``BacktestApp._send_real_order``) blocks an
entry when the account's 可用餘額 is below one lot's 原始保證金. That
number was a constant in ``SYMBOL_CONFIG`` and went stale: TAIFEX moved
TMF from 16,100 to 35,050 while the constant stayed, so the gate passed
entries the broker then rejected (980).

Rule: the gate uses the newest valid table, by TAIFEX's own 更新日期, of
(a) the downloaded copy cached in ``data/taifex_margin.json`` and (b)
``BUILTIN`` below. A cache that is missing, unreadable or fails
``validate`` is ignored, so the worst case is the built-in table.

Download: ``MARGIN_URL`` serves a small CSV (cp950 on the live site):
``更新日期:YYYY/MM/DD``, the header ``商品別,結算保證金,維持保證金,原始保證金``
and one row per product. The parser finds the header and rows by
pattern, not by line, so it does not depend on the line breaks.

Refresh: ``MarginSync.poll`` runs on the Tk thread with the 30 s status
poll. When the cache is older than ``REFRESH_S`` it starts one daemon
thread. That thread takes a non-blocking host-wide lock next to the
cache (one refresh per host however many bots run), downloads, validates,
writes the cache atomically and, when the effective table changed, logs
and sends one notice. The Tk thread only stats and reads the small cache.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.utils import fileio
from src.utils.file_lock import _lock_nb, _open_lock_fd, _unlock_fd

MARGIN_URL = "https://www.taifex.com.tw/cht/5/indexMargingDown"
CACHE_NAME = "taifex_margin.json"
CACHE_VERSION = 1
REFRESH_S = 3 * 3600          # cache age that triggers a download
RETRY_S = 15 * 60             # wait after a started attempt, in this process
FETCH_TIMEOUT_S = 20
MAX_DOWNLOAD_BYTES = 512_000
CLOCK_SKEW_S = 300            # a cache stamped this far ahead still counts

_TPE = timezone(timedelta(hours=8))

# TAIFEX row name -> SYMBOL_CONFIG ``taifex_id``. Exact names only:
# 客製化小型臺指期貨 contains the text 小型臺指 and must not match it.
TAIFEX_NAMES = {"臺股期貨": "TX", "小型臺指": "MTX", "微型臺指期貨": "TMF"}
PRODUCT_LABELS = {"TX": "大台 TX", "MTX": "小台 MTX", "TMF": "微台 TMF"}

# TAIFEX sets the MTX and TMF margins as fixed fractions of TX (SPAN
# parameters TX×0.25 and TX×0.05; every published table so far matches
# exactly). A download that breaks this is a page or parse error.
TX_FRACTION = {"TX": 1.0, "MTX": 0.25, "TMF": 0.05}
FRACTION_TOLERANCE = 0.02

# Plausible TX 原始保證金. Wide on purpose (306,000 -> 701,000 within a
# year); only nonsense falls outside.
TX_INITIAL_MIN = 50_000
TX_INITIAL_MAX = 10_000_000

_COLUMNS = {
    "結算保證金": "settlement",
    "維持保證金": "maintenance",
    "原始保證金": "initial",
}
_DATE_RE = re.compile(
    r"更新日期\s*[:：]\s*(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})")
_HEADER_RE = re.compile(
    r"商品別\s*,\s*([^,\s]+)\s*,\s*([^,\s]+)\s*,\s*([^,\s]+)")


@dataclass(frozen=True)
class Margin:
    settlement: int
    maintenance: int
    initial: int


@dataclass(frozen=True)
class MarginTable:
    published: str                         # TAIFEX 更新日期, "YYYY/MM/DD"
    margins: dict = field(default_factory=dict)   # taifex_id -> Margin
    source: str = "builtin"                # "builtin" or "taifex"
    fetched_at: float = 0.0                # epoch seconds (taifex only)

    def label(self) -> str:
        origin = "期交所" if self.source == "taifex" else "內建"
        return f"{origin} {self.published}"


# Values TAIFEX published on 2026/08/12 (in force 2026-08-12 after the
# day session). Keep in step with SYMBOL_CONFIG ``init_margin``
# (tests/test_margin_table.py pins both).
BUILTIN = MarginTable(
    published="2026/08/12",
    margins={
        "TX": Margin(519_000, 538_000, 701_000),
        "MTX": Margin(129_750, 134_500, 175_250),
        "TMF": Margin(25_950, 26_900, 35_050),
    },
)


def _norm_date(y, m, d) -> str | None:
    try:
        dt = datetime(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None
    return dt.strftime("%Y/%m/%d")


def _is_amount(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(table: MarginTable) -> str:
    """Empty when ``table`` is usable, otherwise the reason it is not."""
    published = table.published if isinstance(table.published, str) else ""
    parts = published.split("/")
    if len(parts) != 3 or _norm_date(*parts) != published:
        return f"bad 更新日期 {table.published!r}"
    margins = table.margins if isinstance(table.margins, dict) else {}
    if set(margins) != set(TX_FRACTION):
        return f"products {sorted(margins)} != {sorted(TX_FRACTION)}"
    for pid, m in margins.items():
        if not isinstance(m, Margin):
            return f"{pid}: not a margin row"
        if not all(_is_amount(v) for v in
                   (m.settlement, m.maintenance, m.initial)):
            return f"{pid}: amounts must be positive integers"
        if not m.initial > m.maintenance > m.settlement:
            return f"{pid}: expected 原始 > 維持 > 結算"
    tx = margins["TX"].initial
    if not TX_INITIAL_MIN <= tx <= TX_INITIAL_MAX:
        return f"TX 原始保證金 {tx:,} outside {TX_INITIAL_MIN:,}..{TX_INITIAL_MAX:,}"
    for pid, frac in TX_FRACTION.items():
        expect = tx * frac
        got = margins[pid].initial
        if abs(got - expect) > expect * FRACTION_TOLERANCE:
            return f"{pid} 原始保證金 {got:,} is not TX×{frac} ({expect:,.0f})"
    return ""


def decode_download(raw: bytes) -> str | None:
    """Text of a download: UTF-8 (with or without BOM) or cp950."""
    if not raw:
        return None
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw[3:].decode("utf-8")
        except UnicodeDecodeError:
            return None
    for encoding in ("utf-8", "cp950"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def parse_margin_text(text: str) -> tuple[MarginTable | None, str]:
    """Validated table from the download text, or ``(None, reason)``."""
    if not text or not text.strip():
        return None, "empty download"
    head = text.lstrip()[:20].lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        return None, "got an HTML page, not the CSV"
    m = _DATE_RE.search(text)
    if not m:
        return None, "no 更新日期"
    published = _norm_date(*m.groups())
    if published is None:
        return None, f"bad 更新日期 {m.group(0)!r}"
    h = _HEADER_RE.search(text)
    if not h:
        return None, "no 商品別 header"
    cols = [_COLUMNS.get(name) for name in h.groups()]
    if sorted(c for c in cols if c) != sorted(_COLUMNS.values()):
        return None, f"unexpected columns {list(h.groups())}"
    body = text[h.end():]
    margins = {}
    for name, pid in TAIFEX_NAMES.items():
        row_re = re.compile(
            r"(?:^|[\s,])" + re.escape(name)
            + r"\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?=\s*(?:,|$))",
            re.MULTILINE)
        hits = row_re.findall(body)
        if len(hits) != 1:
            return None, f"{name}: {len(hits)} rows"
        values = dict(zip(cols, (int(v) for v in hits[0])))
        margins[pid] = Margin(values["settlement"], values["maintenance"],
                              values["initial"])
    table = MarginTable(published=published, margins=margins, source="taifex")
    reason = validate(table)
    if reason:
        return None, reason
    return table, ""


def default_cache_path() -> str:
    """``project_root()/data/taifex_margin.json``; ``TAI_MARGIN_CACHE`` overrides
    (tests point it off the repo ``data/`` tree)."""
    override = os.environ.get("TAI_MARGIN_CACHE", "").strip()
    if override:
        return override
    from src.paths import project_root
    return os.path.join(project_root(), "data", CACHE_NAME)


def table_to_json(table: MarginTable) -> dict:
    return {
        "version": CACHE_VERSION,
        "published": table.published,
        "fetched_at": float(table.fetched_at),
        "fetched_at_tpe": datetime.fromtimestamp(
            table.fetched_at, _TPE).strftime("%Y-%m-%d %H:%M:%S"),
        "source_url": MARGIN_URL,
        "margins": {
            pid: {"settlement": m.settlement, "maintenance": m.maintenance,
                  "initial": m.initial}
            for pid, m in table.margins.items()
        },
    }


def table_from_json(data) -> MarginTable | None:
    """Cached table, or None for anything malformed or implausible."""
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    fetched_at = data.get("fetched_at")
    if (not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool)
            or fetched_at != fetched_at or fetched_at <= 0):
        return None
    rows = data.get("margins")
    if not isinstance(rows, dict):
        return None
    margins = {}
    for pid, row in rows.items():
        if not isinstance(row, dict):
            return None
        margins[pid] = Margin(row.get("settlement"), row.get("maintenance"),
                              row.get("initial"))
    table = MarginTable(published=data.get("published"), margins=margins,
                        source="taifex", fetched_at=float(fetched_at))
    return None if validate(table) else table


# path -> (stat key, table). Every open of the cache makes a concurrent
# os.replace fail on Windows, so the 30 s poll re-reads only on change.
_MEMO: dict[str, tuple[tuple, MarginTable | None]] = {}
_MEMO_LOCK = threading.Lock()


def load_cache(path: str) -> MarginTable | None:
    if not path:
        return None
    key = os.path.abspath(path)
    try:
        st = os.stat(path)
    except OSError:
        with _MEMO_LOCK:
            _MEMO.pop(key, None)
        return None
    # os.replace gives the file a new identity, so a rewrite of the same
    # size inside one mtime tick still reads as a change.
    stat_key = (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
    with _MEMO_LOCK:
        hit = _MEMO.get(key)
    if hit is not None and hit[0] == stat_key:
        return hit[1]
    table = table_from_json(fileio.read_json(path))
    with _MEMO_LOCK:
        _MEMO[key] = (stat_key, table)
    return table


def current_table(path: str | None = None) -> MarginTable:
    """Newest valid table by 更新日期: the cache, else ``BUILTIN``."""
    cached = load_cache(path) if path else None
    if cached is not None and cached.published >= BUILTIN.published:
        return cached
    return BUILTIN


def initial_margin(taifex_id: str, *, path: str | None = None,
                   fallback: int = 0) -> tuple[int, str]:
    """One lot's 原始保證金 for the entry gate, and where it came from.

    Never raises: the gate runs on the order path. An unknown product
    returns ``fallback`` (SYMBOL_CONFIG ``init_margin``).
    """
    try:
        table = current_table(path)
    except Exception:
        table = BUILTIN
    margin = table.margins.get(taifex_id)
    if margin is not None:
        return margin.initial, table.label()
    try:
        return int(fallback or 0), "SYMBOL_CONFIG"
    except (TypeError, ValueError):
        return 0, "SYMBOL_CONFIG"


def write_cache(path: str, table: MarginTable, *, attempts: int = 5,
                sleep: Callable[[float], None] = time.sleep) -> bool:
    """Atomic replace of the cache. False when it could not be written."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(table_to_json(table), f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        _remove_quietly(tmp)
        return False
    for attempt in range(max(1, attempts)):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            # A reader holds the target open for one read (Windows).
            sleep(0.01 * (attempt + 1))
        except OSError:
            break
    _remove_quietly(tmp)
    return False


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def fetch_table(*, opener=None, timeout_s: float = FETCH_TIMEOUT_S,
                clock: Callable[[], float] = time.time
                ) -> tuple[MarginTable | None, str]:
    """Download and validate the TAIFEX table. ``(None, reason)`` on failure."""
    req = urllib.request.Request(MARGIN_URL, headers={
        "User-Agent": "Mozilla/5.0 (tai-robot margin sync)",
        "Connection": "close",
    })
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(req, timeout=timeout_s) as resp:
            raw = resp.read(MAX_DOWNLOAD_BYTES + 1)
    except Exception as exc:
        return None, f"download failed: {type(exc).__name__}: {exc}"
    if len(raw) > MAX_DOWNLOAD_BYTES:
        return None, f"download larger than {MAX_DOWNLOAD_BYTES} bytes"
    text = decode_download(raw)
    if text is None:
        return None, "download is neither UTF-8 nor cp950"
    table, reason = parse_margin_text(text)
    if table is None:
        return None, reason
    return replace(table, fetched_at=float(clock())), ""


def describe_change(before: MarginTable, after: MarginTable) -> str:
    """Discord/log text for a change of the effective table."""
    lines = [f"📐 期交所保證金調整 TAIFEX margin change（更新日期 {after.published}）"]
    for pid in ("TX", "MTX", "TMF"):
        old, new = before.margins.get(pid), after.margins.get(pid)
        if old is None or new is None or old == new:
            continue
        lines.append(
            f"{PRODUCT_LABELS[pid]}：原始 {old.initial:,} → {new.initial:,}，"
            f"維持 {old.maintenance:,} → {new.maintenance:,}")
    lines.append("實單進場的保證金檢查已改用新數字。帳戶權益數請保持在"
                 "「實單 bot 數 × 原始保證金」以上。")
    lines.append("Entry margin check now uses the new values. Keep account "
                 "equity at or above (real bots × initial margin).")
    return "\n".join(lines)


class MarginSync:
    """Per-process refresher. ``poll`` is cheap and safe on the Tk thread."""

    def __init__(self, cache_path: str, *, fetch=None,
                 notify: Callable[[str], None] | None = None,
                 log: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time,
                 start_thread: Callable[[Callable[[], None]], object] | None = None,
                 refresh_s: float = REFRESH_S, retry_s: float = RETRY_S):
        self.cache_path = cache_path
        self._fetch = fetch or fetch_table
        self._notify = notify
        self._log = log or (lambda _msg: None)
        self._clock = clock
        self._start_thread = start_thread or self._start_daemon
        self.refresh_s = float(refresh_s)
        self.retry_s = float(retry_s)
        self._mutex = threading.Lock()
        self._running = False
        self._last_attempt: float | None = None

    @staticmethod
    def _start_daemon(target: Callable[[], None]) -> object:
        thread = threading.Thread(target=target, name="taifex-margin-sync",
                                  daemon=True)
        thread.start()
        return thread

    def cache_fresh(self, now: float) -> bool:
        cached = load_cache(self.cache_path)
        if cached is None:
            return False
        age = now - cached.fetched_at
        return -CLOCK_SKEW_S <= age < self.refresh_s

    def poll(self) -> bool:
        """Start a background refresh when due. True when one was started."""
        now = self._clock()
        with self._mutex:
            if self._running:
                return False
            if (self._last_attempt is not None
                    and now - self._last_attempt < self.retry_s):
                return False
            if self.cache_fresh(now):
                return False
            self._last_attempt = now
            self._running = True
        try:
            self._start_thread(self._run)
        except Exception:
            with self._mutex:
                self._running = False
            raise
        return True

    def _run(self) -> None:
        try:
            self._refresh_under_lock()
        except Exception as exc:
            self._log(f"[MARGIN] sync error: {type(exc).__name__}: {exc}")
        finally:
            with self._mutex:
                self._running = False

    def _refresh_under_lock(self) -> None:
        fd = _open_lock_fd(self.cache_path + ".lock")
        try:
            if not _lock_nb(fd):
                return  # another bot on this host is refreshing
            try:
                if self.cache_fresh(self._clock()):
                    return
                before = current_table(self.cache_path)
                table, reason = self._fetch()
                if table is None:
                    self._log(f"[MARGIN] TAIFEX margin download not used: "
                              f"{reason} (still {before.label()})")
                    return
                if not write_cache(self.cache_path, table):
                    self._log("[MARGIN] could not write the margin cache "
                              f"(still {before.label()})")
                    return
                after = current_table(self.cache_path)
                tmf = after.margins["TMF"]
                self._log(f"[MARGIN] synced TAIFEX {table.published}; using "
                          f"{after.label()} (TMF 原始 {tmf.initial:,})")
                if after.margins != before.margins:
                    msg = describe_change(before, after)
                    self._log("[MARGIN] " + msg.replace("\n", " | "))
                    if self._notify is not None:
                        try:
                            self._notify(msg)
                        except Exception as exc:
                            self._log(f"[MARGIN] notify failed: {exc}")
            finally:
                try:
                    _unlock_fd(fd)
                except OSError:
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
