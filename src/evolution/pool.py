"""SQLite-backed strategy gene pool for the Evolution Engine.

Each row is a candidate strategy with its source code, lineage pointer
to the parent it was mutated from, three independent fitness slots
(in-sample, walk-forward, paper-trading), and a status that gates
promotion through the pipeline:

    candidate -> validated -> paper_trading -> live -> retired

Promotion gates:
  - ``candidate``    -> ``validated``     gated on ``walkforward_fitness``
    (in-sample composite overfits trivially and is informational only)
  - ``validated``    -> ``paper_trading`` operator deploys the strategy
    as a paper bot; SEE doesn't auto-flip this status — running paper
    is a deliberate human action.
  - ``paper_trading``-> ``live``          gated on ``paper_trading_fitness``,
    plus minimum-period / minimum-trade gates enforced by
    :func:`eligible_for_live` (default: 14 days + 30 trades).

Paper trading data is the closer-to-live signal: no look-ahead bias,
real market conditions, real fills. ``paper_trading_fitness`` therefore
carries more weight than ``walkforward_fitness`` for the live gate.
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

    Three independent fitness fields:

    * ``fitness_composite`` — in-sample backtest composite (informational)
    * ``walkforward_fitness`` — out-of-sample backtest composite (gate
      for promotion to ``validated``)
    * ``paper_trading_fitness`` — composite on accumulated paper-trading
      results (gate for promotion to ``live``, plus period/trade-count
      minimums in :func:`eligible_for_live`)
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
    paper_trading_fitness: float = 0.0
    paper_trading_period_start: str | None = None
    paper_trading_period_end: str | None = None
    paper_trading_trades: int = 0
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
    paper_trading_fitness REAL NOT NULL DEFAULT 0.0,
    paper_trading_period_start TEXT,
    paper_trading_period_end TEXT,
    paper_trading_trades INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status);
CREATE INDEX IF NOT EXISTS idx_strategies_walkforward
    ON strategies(walkforward_fitness DESC);
CREATE INDEX IF NOT EXISTS idx_strategies_parent ON strategies(parent_id);
-- idx_strategies_paper_trading is created in _apply_migrations after
-- the paper_trading_fitness column is guaranteed to exist. Trying to
-- create it here would fail on legacy DBs that pre-date the column.
"""

# Additive column migrations for pools created by earlier versions of
# this module. SQLite's ``ADD COLUMN`` can't be wrapped in
# ``CREATE TABLE IF NOT EXISTS``, so we check ``PRAGMA table_info`` and
# add anything missing one-by-one. Each entry: (column_name, DDL fragment).
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("paper_trading_fitness",
     "ALTER TABLE strategies ADD COLUMN paper_trading_fitness REAL NOT NULL DEFAULT 0.0"),
    ("paper_trading_period_start",
     "ALTER TABLE strategies ADD COLUMN paper_trading_period_start TEXT"),
    ("paper_trading_period_end",
     "ALTER TABLE strategies ADD COLUMN paper_trading_period_end TEXT"),
    ("paper_trading_trades",
     "ALTER TABLE strategies ADD COLUMN paper_trading_trades INTEGER NOT NULL DEFAULT 0"),
)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring an existing pool up to the current schema. No-op when every
    column already exists (fresh DB or already-migrated)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(strategies)")}
    for col, ddl in _MIGRATIONS:
        if col not in existing:
            conn.execute(ddl)
    # Indexes are idempotent via CREATE INDEX IF NOT EXISTS — add the
    # paper-trading one explicitly here for migrated DBs that have the
    # column but missed the index in _SCHEMA's executescript pass.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategies_paper_trading "
        "ON strategies(paper_trading_fitness DESC)"
    )


class StrategyPool:
    """SQLite-backed pool. Connection is opened per-call (cheap on SQLite)
    so the pool is safe to share across threads — each call gets a fresh
    connection rather than juggling a single one across the GIL."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # Bring older pools forward. Cheap (single PRAGMA + a few
            # ALTER TABLEs at most) so we run it on every open.
            _apply_migrations(c)

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
            paper_trading_fitness=row["paper_trading_fitness"],
            paper_trading_period_start=row["paper_trading_period_start"],
            paper_trading_period_end=row["paper_trading_period_end"],
            paper_trading_trades=row["paper_trading_trades"],
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
                    walkforward_fitness,
                    paper_trading_fitness, paper_trading_period_start,
                    paper_trading_period_end, paper_trading_trades,
                    status, created_at, updated_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id, entry.name, entry.source_code,
                    entry.parent_id, entry.generation,
                    entry.fitness_composite, entry.fitness_json,
                    entry.backtest_period_start, entry.backtest_period_end,
                    entry.walkforward_period_start, entry.walkforward_period_end,
                    entry.walkforward_fitness,
                    entry.paper_trading_fitness,
                    entry.paper_trading_period_start,
                    entry.paper_trading_period_end,
                    entry.paper_trading_trades,
                    entry.status,
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

    def update_paper_trading_fitness(
        self,
        id_: str,
        fitness_dict: dict,
        paper_trading_fitness: float,
        period_start: str | None = None,
        period_end: str | None = None,
        n_trades: int | None = None,
    ) -> None:
        """Record a paper-trading fitness evaluation.

        Unlike :meth:`update_fitness` (which records the backtest pass),
        this updates the paper-trading slot. ``fitness_json`` is replaced
        with the full new dict so paper-vs-backtest comparisons are
        possible later by reading the JSON.

        ``period_start`` / ``period_end`` should be ISO date strings
        bracketing the paper-trading window the score covers.
        ``n_trades`` is the number of paper trades that fed into the
        score — used by :func:`eligible_for_live` to enforce a minimum
        sample size before live promotion.
        """
        composite = float(fitness_dict.get("composite", 0.0))
        with self._conn() as c:
            cursor = c.execute(
                """
                UPDATE strategies
                SET paper_trading_fitness = ?,
                    fitness_json = ?,
                    paper_trading_period_start = COALESCE(?, paper_trading_period_start),
                    paper_trading_period_end = COALESCE(?, paper_trading_period_end),
                    paper_trading_trades = COALESCE(?, paper_trading_trades),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    float(paper_trading_fitness),
                    json.dumps(fitness_dict, default=str),
                    period_start,
                    period_end,
                    int(n_trades) if n_trades is not None else None,
                    _now_iso(),
                    id_,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"No strategy with id {id_!r}")

    def get_top_paper_trading(self, n: int) -> list[StrategyEntry]:
        """Top-N by paper_trading_fitness (descending). The signal that
        drives live promotion — see :func:`eligible_for_live` for the
        full gate including minimum period / trade-count."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT * FROM strategies
                WHERE status = 'paper_trading'
                ORDER BY paper_trading_fitness DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

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


# ---------------------------------------------------------------------------
# Promotion gates
# ---------------------------------------------------------------------------

# Minimum paper-trading exposure before a strategy is considered for live.
# 14 calendar days catches both day-session-only and night-session bots
# through at least one full week of each Taiwan futures regime cycle.
MIN_PAPER_DAYS_FOR_LIVE = 14
MIN_PAPER_TRADES_FOR_LIVE = 30

# Below this paper-trading fitness, no amount of duration / sample size
# qualifies a strategy for live. Pickable per-deployment, but the floor
# reflects the spec: paper-trading composite is the primary live gate.
DEFAULT_LIVE_FITNESS_THRESHOLD = 0.50


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def eligible_for_live(
    entry: StrategyEntry,
    fitness_threshold: float = DEFAULT_LIVE_FITNESS_THRESHOLD,
    min_paper_days: int = MIN_PAPER_DAYS_FOR_LIVE,
    min_paper_trades: int = MIN_PAPER_TRADES_FOR_LIVE,
    require_positive_walkforward: bool = True,
) -> tuple[bool, str]:
    """Decide whether a paper-trading strategy is ready for live promotion.

    Paper-trading composite is the primary signal (closer-to-live: no
    look-ahead, real market conditions, real fills). Walk-forward acts as
    a sanity floor — a strategy that LOOKED great in paper but couldn't
    pass any out-of-sample backtest is probably the wrong kind of lucky.

    Returns ``(eligible, reason)`` so callers can surface a human-readable
    explanation rather than just a bool. Reason is empty when eligible.
    """
    if entry.status != "paper_trading":
        return False, (
            f"status is {entry.status!r}, must be 'paper_trading'"
        )
    if entry.paper_trading_fitness < fitness_threshold:
        return False, (
            f"paper_trading_fitness {entry.paper_trading_fitness:.3f} "
            f"< threshold {fitness_threshold:.3f}"
        )
    if entry.paper_trading_trades < min_paper_trades:
        return False, (
            f"paper_trading_trades {entry.paper_trading_trades} "
            f"< minimum {min_paper_trades}"
        )
    start = _parse_date(entry.paper_trading_period_start)
    end = _parse_date(entry.paper_trading_period_end)
    if start is None or end is None:
        return False, "paper_trading period start/end not recorded"
    span_days = (end - start).days
    if span_days < min_paper_days:
        return False, (
            f"paper-trading window is {span_days} days, "
            f"minimum {min_paper_days}"
        )
    if require_positive_walkforward and entry.walkforward_fitness <= 0:
        return False, (
            f"walkforward_fitness {entry.walkforward_fitness:.3f} "
            f"is not positive — backtest sanity check failed"
        )
    return True, ""
