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

    def __len__(self) -> int:
        return len(self._bars)

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
