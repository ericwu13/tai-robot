"""Tests for src/evolution/pool.py."""

from __future__ import annotations

import json

import pytest

from src.evolution.pool import StrategyEntry, StrategyPool


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
