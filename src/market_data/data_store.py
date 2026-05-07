"""Ring buffer for bars with optional SQLite persistence."""

from __future__ import annotations

import logging
import sqlite3
from collections import deque
from pathlib import Path

from .models import Bar

logger = logging.getLogger(__name__)


class DataStore:
    """Stores completed bars in a fixed-size ring buffer.

    Optionally persists to SQLite for recovery across restarts.
    """

    def __init__(self, max_bars: int = 5000, db_path: str | None = None):
        self._bars: deque[Bar] = deque(maxlen=max_bars)
        self._max_bars = max_bars
        # Higher-timeframe (HTF) stores keyed by interval in seconds.
        # Empty for single-TF strategies — zero-cost when unused.
        self._htf_stores: dict[int, deque[Bar]] = {}
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

        if db_path:
            self._init_db(db_path)

    def _init_db(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS bars (
                symbol TEXT,
                dt TEXT,
                open INTEGER,
                high INTEGER,
                low INTEGER,
                close INTEGER,
                volume INTEGER,
                interval INTEGER,
                PRIMARY KEY (symbol, dt, interval)
            )
        """)
        self._conn.commit()

    def add_bar(self, bar: Bar) -> None:
        self._bars.append(bar)
        if self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bar.symbol, bar.dt.isoformat(), bar.open, bar.high,
                 bar.low, bar.close, bar.volume, bar.interval),
            )
            self._conn.commit()

    def get_bars(self, n: int | None = None) -> list[Bar]:
        """Return the last n bars (or all if n is None)."""
        if n is None:
            return list(self._bars)
        return list(self._bars)[-n:]

    def get_closes(self, n: int | None = None) -> list[int]:
        """Return the last n close prices."""
        bars = self.get_bars(n)
        return [b.close for b in bars]

    def get_highs(self, n: int | None = None) -> list[int]:
        """Return the last n high prices."""
        bars = self.get_bars(n)
        return [b.high for b in bars]

    def get_lows(self, n: int | None = None) -> list[int]:
        """Return the last n low prices."""
        bars = self.get_bars(n)
        return [b.low for b in bars]

    def __len__(self) -> int:
        return len(self._bars)

    # ── Higher-timeframe (HTF) accessors ──
    #
    # HTF data is opt-in. The engine populates these via _register_htf and
    # _add_htf_bar; strategies read them via htf_bars/htf_closes/etc. For
    # single-TF strategies _htf_stores stays empty and these methods are
    # never called.

    def _register_htf(self, interval: int, max_bars: int | None = None) -> None:
        """Register an HTF store for *interval* seconds."""
        if interval in self._htf_stores:
            return
        cap = max_bars if max_bars is not None else self._max_bars
        self._htf_stores[interval] = deque(maxlen=cap)

    def _add_htf_bar(self, interval: int, bar: Bar) -> None:
        """Append a completed HTF bar. Auto-registers store if needed."""
        store = self._htf_stores.get(interval)
        if store is None:
            self._register_htf(interval)
            store = self._htf_stores[interval]
        store.append(bar)

    def _htf_len(self, interval: int) -> int:
        store = self._htf_stores.get(interval)
        return 0 if store is None else len(store)

    def htf_bars(self, interval: int, n: int | None = None) -> list[Bar]:
        """Return the last n completed HTF bars at *interval* seconds."""
        store = self._htf_stores.get(interval)
        if store is None:
            return []
        if n is None:
            return list(store)
        return list(store)[-n:]

    def htf_closes(self, interval: int, n: int | None = None) -> list[int]:
        return [b.close for b in self.htf_bars(interval, n)]

    def htf_highs(self, interval: int, n: int | None = None) -> list[int]:
        return [b.high for b in self.htf_bars(interval, n)]

    def htf_lows(self, interval: int, n: int | None = None) -> list[int]:
        return [b.low for b in self.htf_bars(interval, n)]

    def htf_opens(self, interval: int, n: int | None = None) -> list[int]:
        return [b.open for b in self.htf_bars(interval, n)]

    def load_from_db(self, symbol: str, interval: int, limit: int = 5000) -> int:
        """Load bars from SQLite into the ring buffer. Returns count loaded."""
        if not self._conn:
            return 0
        cursor = self._conn.execute(
            "SELECT symbol, dt, open, high, low, close, volume, interval "
            "FROM bars WHERE symbol = ? AND interval = ? "
            "ORDER BY dt DESC LIMIT ?",
            (symbol, interval, limit),
        )
        rows = cursor.fetchall()
        from datetime import datetime
        for row in reversed(rows):
            bar = Bar(
                symbol=row[0],
                dt=datetime.fromisoformat(row[1]),
                open=row[2], high=row[3], low=row[4], close=row[5],
                volume=row[6], interval=row[7],
            )
            self._bars.append(bar)
        logger.info("Loaded %d bars from database for %s/%ds", len(rows), symbol, interval)
        return len(rows)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
