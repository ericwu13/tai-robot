"""SQLite-backed strategy gene pool for the Evolution Engine.

Each row is a candidate strategy with its source code, lineage pointer
to the parent it was mutated from, both in-sample and walk-forward
fitness, and a status that gates promotion through the pipeline:

    candidate -> validated -> paper_trading -> live -> retired

``walkforward_fitness`` is the number that actually drives promotion —
in-sample composite is informational only (overfits trivially).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


# Allowed status values. A typo here would silently fail a SQL filter,
# so we validate at the API boundary.
STATUSES = (
    "candidate",
    "validated",
    "paper_trading",
    "live",
    "retired",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class StrategyEntry:
    """One row in the pool.

    ``id`` and ``created_at`` / ``updated_at`` default-fill on insert if
    left blank, so callers usually only supply ``name`` + ``source_code``
    (+ optional parent_id / generation) when adding a fresh candidate.
    """
    name: str
    source_code: str
    id: str = field(default_factory=_new_id)
    parent_id: str | None = None
    generation: int = 0
    fitness_composite: float = 0.0
    fitness_json: str = "{}"
    backtest_period_start: str | None = None
    backtest_period_end: str | None = None
    walkforward_period_start: str | None = None
    walkforward_period_end: str | None = None
    walkforward_fitness: float = 0.0
    status: str = "candidate"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    notes: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_code TEXT NOT NULL,
    parent_id TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    fitness_composite REAL NOT NULL DEFAULT 0.0,
    fitness_json TEXT NOT NULL DEFAULT '{}',
    backtest_period_start TEXT,
    backtest_period_end TEXT,
    walkforward_period_start TEXT,
    walkforward_period_end TEXT,
    walkforward_fitness REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status);
CREATE INDEX IF NOT EXISTS idx_strategies_walkforward
    ON strategies(walkforward_fitness DESC);
CREATE INDEX IF NOT EXISTS idx_strategies_parent ON strategies(parent_id);
"""


class StrategyPool:
    """SQLite-backed pool. Connection is opened per-call (cheap on SQLite)
    so the pool is safe to share across threads — each call gets a fresh
    connection rather than juggling a single one across the GIL."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> StrategyEntry:
        return StrategyEntry(
            id=row["id"],
            name=row["name"],
            source_code=row["source_code"],
            parent_id=row["parent_id"],
            generation=row["generation"],
            fitness_composite=row["fitness_composite"],
            fitness_json=row["fitness_json"],
            backtest_period_start=row["backtest_period_start"],
            backtest_period_end=row["backtest_period_end"],
            walkforward_period_start=row["walkforward_period_start"],
            walkforward_period_end=row["walkforward_period_end"],
            walkforward_fitness=row["walkforward_fitness"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            notes=row["notes"],
        )

    def add(self, entry: StrategyEntry) -> str:
        if entry.status not in STATUSES:
            raise ValueError(f"Invalid status: {entry.status!r}")
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO strategies (
                    id, name, source_code, parent_id, generation,
                    fitness_composite, fitness_json,
                    backtest_period_start, backtest_period_end,
                    walkforward_period_start, walkforward_period_end,
                    walkforward_fitness, status, created_at, updated_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id, entry.name, entry.source_code,
                    entry.parent_id, entry.generation,
                    entry.fitness_composite, entry.fitness_json,
                    entry.backtest_period_start, entry.backtest_period_end,
                    entry.walkforward_period_start, entry.walkforward_period_end,
                    entry.walkforward_fitness, entry.status,
                    entry.created_at, entry.updated_at, entry.notes,
                ),
            )
        return entry.id

    def get(self, id_: str) -> StrategyEntry | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM strategies WHERE id = ?", (id_,),
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def get_top(self, n: int, status: str = "validated") -> list[StrategyEntry]:
        """Top-N by walkforward_fitness (descending). Promotion is driven
        by walk-forward, never by in-sample composite."""
        if status not in STATUSES:
            raise ValueError(f"Invalid status: {status!r}")
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT * FROM strategies
                WHERE status = ?
                ORDER BY walkforward_fitness DESC
                LIMIT ?
                """,
                (status, n),
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_by_status(self, status: str) -> list[StrategyEntry]:
        if status not in STATUSES:
            raise ValueError(f"Invalid status: {status!r}")
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM strategies WHERE status = ? "
                "ORDER BY walkforward_fitness DESC",
                (status,),
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def promote(self, id_: str, new_status: str) -> None:
        if new_status not in STATUSES:
            raise ValueError(f"Invalid status: {new_status!r}")
        with self._conn() as c:
            cursor = c.execute(
                "UPDATE strategies SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, _now_iso(), id_),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"No strategy with id {id_!r}")

    def update_fitness(
        self,
        id_: str,
        fitness_dict: dict,
        walkforward_fitness: float,
    ) -> None:
        """Record a fitness evaluation. ``fitness_composite`` is read from
        ``fitness_dict['composite']`` and the full dict is stored as JSON
        for later inspection / debugging."""
        composite = float(fitness_dict.get("composite", 0.0))
        with self._conn() as c:
            cursor = c.execute(
                """
                UPDATE strategies
                SET fitness_composite = ?,
                    fitness_json = ?,
                    walkforward_fitness = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    composite,
                    json.dumps(fitness_dict, default=str),
                    float(walkforward_fitness),
                    _now_iso(),
                    id_,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"No strategy with id {id_!r}")

    def update_notes(self, id_: str, notes: str) -> None:
        with self._conn() as c:
            cursor = c.execute(
                "UPDATE strategies SET notes = ?, updated_at = ? WHERE id = ?",
                (notes, _now_iso(), id_),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"No strategy with id {id_!r}")

    def get_lineage(self, id_: str) -> list[StrategyEntry]:
        """Walk parent_id pointers from this strategy back to the seed.

        Returns ``[entry, parent, grandparent, ..., seed]``. Cycles are
        defensively guarded against — a malformed pool with a self-parent
        would otherwise loop forever.
        """
        chain: list[StrategyEntry] = []
        seen: set[str] = set()
        current = self.get(id_)
        while current is not None and current.id not in seen:
            chain.append(current)
            seen.add(current.id)
            if current.parent_id is None:
                break
            current = self.get(current.parent_id)
        return chain

    def count(self, status: str | None = None) -> int:
        with self._conn() as c:
            if status is None:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM strategies"
                ).fetchone()
            else:
                if status not in STATUSES:
                    raise ValueError(f"Invalid status: {status!r}")
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM strategies WHERE status = ?",
                    (status,),
                ).fetchone()
        return int(row["n"])

    def delete(self, id_: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM strategies WHERE id = ?", (id_,))


def default_pool_path() -> Path:
    """Conventional location: ``data/evolution_pool.db`` under the repo
    root (``src/evolution/pool.py`` → parents[2] is the repo root)."""
    return Path(__file__).resolve().parents[2] / "data" / "evolution_pool.db"
