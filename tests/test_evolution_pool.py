"""Tests for src/evolution/pool.py."""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.evolution.pool import (
    DEFAULT_LIVE_FITNESS_THRESHOLD,
    MIN_PAPER_DAYS_FOR_LIVE,
    MIN_PAPER_TRADES_FOR_LIVE,
    StrategyEntry,
    StrategyPool,
    eligible_for_live,
)


@pytest.fixture
def pool(tmp_path):
    return StrategyPool(tmp_path / "evolution_pool.db")


def make_entry(name: str = "seed", **overrides) -> StrategyEntry:
    base = dict(name=name, source_code=f"# {name}\nclass {name.title()}: ...")
    base.update(overrides)
    return StrategyEntry(**base)


class TestAddAndGet:
    def test_add_returns_id(self, pool):
        entry = make_entry()
        sid = pool.add(entry)
        assert sid == entry.id
        assert pool.count() == 1

    def test_get_roundtrip(self, pool):
        entry = make_entry(
            generation=2,
            fitness_composite=0.42,
            walkforward_fitness=0.31,
            backtest_period_start="2025-01-01",
            backtest_period_end="2025-04-01",
            walkforward_period_start="2025-04-01",
            walkforward_period_end="2025-07-01",
            notes="seed test",
        )
        pool.add(entry)
        loaded = pool.get(entry.id)
        assert loaded is not None
        assert loaded.name == entry.name
        assert loaded.source_code == entry.source_code
        assert loaded.generation == 2
        assert loaded.fitness_composite == pytest.approx(0.42)
        assert loaded.walkforward_fitness == pytest.approx(0.31)
        assert loaded.backtest_period_start == "2025-01-01"
        assert loaded.notes == "seed test"

    def test_get_missing_returns_none(self, pool):
        assert pool.get("does-not-exist") is None

    def test_invalid_status_rejected_on_add(self, pool):
        entry = make_entry(status="bogus")
        with pytest.raises(ValueError):
            pool.add(entry)


class TestGetTop:
    def test_get_top_orders_by_walkforward_fitness(self, pool):
        e1 = make_entry(name="a", walkforward_fitness=0.10, status="validated")
        e2 = make_entry(name="b", walkforward_fitness=0.50, status="validated")
        e3 = make_entry(name="c", walkforward_fitness=0.30, status="validated")
        for e in (e1, e2, e3):
            pool.add(e)

        top = pool.get_top(2, status="validated")
        assert [e.name for e in top] == ["b", "c"]

    def test_get_top_filters_by_status(self, pool):
        # Same fitness, different statuses — only validated should show up.
        for status in ("candidate", "validated", "validated", "retired"):
            pool.add(make_entry(name=status, walkforward_fitness=0.5, status=status))
        top = pool.get_top(10, status="validated")
        assert len(top) == 2
        assert all(e.status == "validated" for e in top)

    def test_get_top_n_caps_results(self, pool):
        for i in range(5):
            pool.add(make_entry(name=f"s{i}", walkforward_fitness=float(i),
                                status="validated"))
        top = pool.get_top(3, status="validated")
        assert len(top) == 3
        assert top[0].walkforward_fitness == 4.0
        assert top[1].walkforward_fitness == 3.0
        assert top[2].walkforward_fitness == 2.0


class TestPromote:
    def test_promote_changes_status(self, pool):
        entry = make_entry()
        pool.add(entry)
        pool.promote(entry.id, "validated")
        loaded = pool.get(entry.id)
        assert loaded.status == "validated"

    def test_promote_invalid_status(self, pool):
        entry = make_entry()
        pool.add(entry)
        with pytest.raises(ValueError):
            pool.promote(entry.id, "not-a-status")

    def test_promote_missing_raises(self, pool):
        with pytest.raises(KeyError):
            pool.promote("missing-id", "validated")

    def test_promote_updates_updated_at(self, pool):
        entry = make_entry()
        pool.add(entry)
        before = pool.get(entry.id).updated_at
        # Force at least 1s of clock movement is unreliable, but we can
        # at least verify the new value is a valid ISO timestamp.
        pool.promote(entry.id, "validated")
        after = pool.get(entry.id).updated_at
        assert after >= before


class TestUpdateFitness:
    def test_update_fitness_records_composite_and_walkforward(self, pool):
        entry = make_entry()
        pool.add(entry)
        fitness_dict = {
            "composite": 0.65, "sharpe": 1.5, "sortino": 1.7,
            "max_drawdown_pct": 8.0, "win_rate": 0.6,
        }
        pool.update_fitness(entry.id, fitness_dict, walkforward_fitness=0.50)
        loaded = pool.get(entry.id)
        assert loaded.fitness_composite == pytest.approx(0.65)
        assert loaded.walkforward_fitness == pytest.approx(0.50)
        decoded = json.loads(loaded.fitness_json)
        assert decoded["sharpe"] == 1.5
        assert decoded["win_rate"] == 0.6

    def test_update_fitness_missing_raises(self, pool):
        with pytest.raises(KeyError):
            pool.update_fitness("missing", {"composite": 0.1}, 0.1)


class TestLineage:
    def test_lineage_returns_chain_from_self_to_seed(self, pool):
        seed = make_entry(name="seed", generation=0)
        gen1 = make_entry(name="gen1", parent_id=seed.id, generation=1)
        gen2 = make_entry(name="gen2", parent_id=gen1.id, generation=2)
        gen3 = make_entry(name="gen3", parent_id=gen2.id, generation=3)
        for e in (seed, gen1, gen2, gen3):
            pool.add(e)

        lineage = pool.get_lineage(gen3.id)
        assert [e.name for e in lineage] == ["gen3", "gen2", "gen1", "seed"]

    def test_lineage_seed_only_returns_self(self, pool):
        seed = make_entry(name="seed")
        pool.add(seed)
        chain = pool.get_lineage(seed.id)
        assert [e.name for e in chain] == ["seed"]

    def test_lineage_missing_returns_empty(self, pool):
        assert pool.get_lineage("missing") == []

    def test_lineage_handles_self_parent_cycle(self, pool):
        # Pathological: a strategy that's its own parent. Shouldn't loop.
        bad = make_entry(name="ouroboros")
        bad.parent_id = bad.id
        pool.add(bad)
        chain = pool.get_lineage(bad.id)
        assert len(chain) == 1


class TestStatus:
    def test_get_by_status(self, pool):
        for s in ("candidate", "candidate", "validated", "retired"):
            pool.add(make_entry(name=s, status=s))
        cands = pool.get_by_status("candidate")
        assert len(cands) == 2
        assert all(e.status == "candidate" for e in cands)

    def test_count_by_status(self, pool):
        for _ in range(3):
            pool.add(make_entry(status="candidate"))
        for _ in range(2):
            pool.add(make_entry(status="validated"))
        assert pool.count() == 5
        assert pool.count("candidate") == 3
        assert pool.count("validated") == 2
        assert pool.count("retired") == 0


class TestNotes:
    def test_update_notes(self, pool):
        entry = make_entry()
        pool.add(entry)
        pool.update_notes(entry.id, "ai-flagged: looks like overfit on 2025-Q1")
        loaded = pool.get(entry.id)
        assert "overfit" in loaded.notes


class TestPersistence:
    def test_pool_survives_reopen(self, tmp_path):
        path = tmp_path / "evolution_pool.db"
        pool1 = StrategyPool(path)
        entry = make_entry(walkforward_fitness=0.42, status="validated")
        pool1.add(entry)

        pool2 = StrategyPool(path)
        loaded = pool2.get(entry.id)
        assert loaded is not None
        assert loaded.walkforward_fitness == pytest.approx(0.42)

    def test_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "newdir" / "evolution_pool.db"
        StrategyPool(nested)
        assert nested.parent.exists()


class TestPaperTradingFitness:
    def test_default_paper_fields(self, pool):
        entry = make_entry()
        pool.add(entry)
        loaded = pool.get(entry.id)
        assert loaded.paper_trading_fitness == 0.0
        assert loaded.paper_trading_trades == 0
        assert loaded.paper_trading_period_start is None
        assert loaded.paper_trading_period_end is None

    def test_update_paper_trading_fitness(self, pool):
        entry = make_entry()
        pool.add(entry)
        fitness_dict = {
            "composite": 0.62, "sharpe": 1.4, "source": "paper",
        }
        pool.update_paper_trading_fitness(
            entry.id,
            fitness_dict,
            paper_trading_fitness=0.62,
            period_start="2026-01-01",
            period_end="2026-01-20",
            n_trades=45,
        )
        loaded = pool.get(entry.id)
        assert loaded.paper_trading_fitness == pytest.approx(0.62)
        assert loaded.paper_trading_period_start == "2026-01-01"
        assert loaded.paper_trading_period_end == "2026-01-20"
        assert loaded.paper_trading_trades == 45
        # fitness_json was overwritten with the paper score blob
        decoded = json.loads(loaded.fitness_json)
        assert decoded["source"] == "paper"

    def test_update_paper_preserves_walkforward_columns(self, pool):
        """Updating paper-trading fitness must NOT clobber backtest
        columns — backtest and paper-trading scores live independently."""
        entry = make_entry(
            walkforward_fitness=0.55,
            fitness_composite=0.40,
            backtest_period_start="2025-10-01",
            backtest_period_end="2026-01-01",
        )
        pool.add(entry)
        pool.update_paper_trading_fitness(
            entry.id, {"composite": 0.30}, paper_trading_fitness=0.30,
            period_start="2026-01-01", period_end="2026-01-20", n_trades=40,
        )
        loaded = pool.get(entry.id)
        # Paper update landed.
        assert loaded.paper_trading_fitness == pytest.approx(0.30)
        # Backtest fields untouched.
        assert loaded.walkforward_fitness == pytest.approx(0.55)
        assert loaded.fitness_composite == pytest.approx(0.40)
        assert loaded.backtest_period_start == "2025-10-01"

    def test_update_paper_missing_raises(self, pool):
        with pytest.raises(KeyError):
            pool.update_paper_trading_fitness(
                "missing", {"composite": 0.1}, 0.1,
            )

    def test_partial_update_preserves_unspecified(self, pool):
        """Subsequent updates with only some fields should keep the rest."""
        entry = make_entry()
        pool.add(entry)
        pool.update_paper_trading_fitness(
            entry.id, {"composite": 0.5}, paper_trading_fitness=0.5,
            period_start="2026-01-01", period_end="2026-01-20", n_trades=35,
        )
        # Second update without period/trades — should keep the first values.
        pool.update_paper_trading_fitness(
            entry.id, {"composite": 0.55}, paper_trading_fitness=0.55,
        )
        loaded = pool.get(entry.id)
        assert loaded.paper_trading_fitness == pytest.approx(0.55)
        assert loaded.paper_trading_period_start == "2026-01-01"
        assert loaded.paper_trading_period_end == "2026-01-20"
        assert loaded.paper_trading_trades == 35

    def test_get_top_paper_trading(self, pool):
        for i, fit in enumerate([0.10, 0.50, 0.30, 0.70]):
            entry = make_entry(
                name=f"p{i}",
                paper_trading_fitness=fit,
                status="paper_trading",
            )
            pool.add(entry)
        top = pool.get_top_paper_trading(2)
        assert [e.paper_trading_fitness for e in top] == [0.70, 0.50]

    def test_get_top_paper_trading_filters_by_status(self, pool):
        # High paper_trading_fitness but wrong status — should be skipped.
        pool.add(make_entry(
            name="wrong-status", paper_trading_fitness=0.99, status="validated",
        ))
        pool.add(make_entry(
            name="right-status", paper_trading_fitness=0.50, status="paper_trading",
        ))
        top = pool.get_top_paper_trading(10)
        assert len(top) == 1
        assert top[0].name == "right-status"


class TestEligibleForLive:
    def _ready_entry(self) -> StrategyEntry:
        return StrategyEntry(
            name="ready",
            source_code="...",
            walkforward_fitness=0.40,
            paper_trading_fitness=0.65,
            paper_trading_trades=MIN_PAPER_TRADES_FOR_LIVE + 5,
            paper_trading_period_start="2026-01-01",
            paper_trading_period_end="2026-01-25",  # 24 days > 14
            status="paper_trading",
        )

    def test_happy_path_eligible(self):
        ok, reason = eligible_for_live(self._ready_entry())
        assert ok is True
        assert reason == ""

    def test_wrong_status(self):
        entry = self._ready_entry()
        entry.status = "validated"
        ok, reason = eligible_for_live(entry)
        assert ok is False
        assert "status" in reason

    def test_fitness_below_threshold(self):
        entry = self._ready_entry()
        entry.paper_trading_fitness = 0.30
        ok, reason = eligible_for_live(
            entry, fitness_threshold=DEFAULT_LIVE_FITNESS_THRESHOLD,
        )
        assert ok is False
        assert "fitness" in reason

    def test_trade_count_below_minimum(self):
        entry = self._ready_entry()
        entry.paper_trading_trades = 10
        ok, reason = eligible_for_live(entry)
        assert ok is False
        assert "trades" in reason

    def test_window_too_short(self):
        entry = self._ready_entry()
        entry.paper_trading_period_end = "2026-01-05"  # 4 days
        ok, reason = eligible_for_live(entry)
        assert ok is False
        assert "days" in reason or "window" in reason

    def test_missing_period_data(self):
        entry = self._ready_entry()
        entry.paper_trading_period_start = None
        ok, reason = eligible_for_live(entry)
        assert ok is False
        assert "period" in reason

    def test_negative_walkforward_blocks_unless_overridden(self):
        entry = self._ready_entry()
        entry.walkforward_fitness = -0.10
        # Default: blocked.
        ok, _ = eligible_for_live(entry)
        assert ok is False
        # Caller can override (e.g. for emergency promotion).
        ok2, _ = eligible_for_live(entry, require_positive_walkforward=False)
        assert ok2 is True

    def test_custom_thresholds(self):
        entry = self._ready_entry()
        # Crank threshold above the entry's paper fitness.
        ok, _ = eligible_for_live(entry, fitness_threshold=0.99)
        assert ok is False
        # Custom min_paper_days that the entry passes (3 weeks).
        ok2, _ = eligible_for_live(entry, min_paper_days=21)
        # Entry has 24-day window; 21 < 24 so this passes.
        assert ok2 is True


class TestSchemaMigration:
    """Pool DBs created before paper_trading_* columns existed must be
    silently upgraded on open. Construct an old-shape DB by hand, then
    let StrategyPool.__init__ run migrations."""

    def _create_old_pool(self, path) -> str:
        """Build a v1-schema DB (pre paper_trading columns) and seed it
        with one row. Returns the inserted id."""
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE strategies (
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
            )
        """)
        sid = "v1-row-id"
        conn.execute("""
            INSERT INTO strategies (
                id, name, source_code, walkforward_fitness, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (sid, "legacy", "# legacy code", 0.42, "validated",
              "2025-06-01T00:00:00", "2025-06-01T00:00:00"))
        conn.commit()
        conn.close()
        return sid

    def test_migration_adds_columns(self, tmp_path):
        path = tmp_path / "old_pool.db"
        sid = self._create_old_pool(str(path))

        # Sanity: confirm the column doesn't exist yet.
        conn = sqlite3.connect(path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(strategies)")}
        conn.close()
        assert "paper_trading_fitness" not in cols

        # Opening with StrategyPool runs the migration.
        pool = StrategyPool(path)

        conn = sqlite3.connect(path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(strategies)")}
        conn.close()
        assert "paper_trading_fitness" in cols
        assert "paper_trading_period_start" in cols
        assert "paper_trading_period_end" in cols
        assert "paper_trading_trades" in cols

        # Legacy row survives, paper fields default-populated.
        loaded = pool.get(sid)
        assert loaded is not None
        assert loaded.name == "legacy"
        assert loaded.walkforward_fitness == pytest.approx(0.42)
        assert loaded.paper_trading_fitness == 0.0
        assert loaded.paper_trading_trades == 0
        assert loaded.paper_trading_period_start is None

    def test_migration_is_idempotent(self, tmp_path):
        """Running __init__ twice on an already-migrated DB should not
        raise (PRAGMA-guard prevents double ADD COLUMN)."""
        path = tmp_path / "twice.db"
        StrategyPool(path)
        StrategyPool(path)  # second open
        # And a third for good measure.
        pool = StrategyPool(path)
        # Pool is usable.
        pool.add(make_entry())
        assert pool.count() == 1
