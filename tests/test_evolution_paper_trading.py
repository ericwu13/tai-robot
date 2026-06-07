"""End-to-end tests for the paper-trading scoring flow.

Covers the integration between LiveRunner's session.json shape, the
input-agnostic fitness function, and the pool's paper_trading_* columns.
The key invariant: paper trades produce the IDENTICAL data shape as
backtest trades (both flow through SimulatedBroker), so the fitness math
shouldn't care. Only the ``source`` tag and the destination columns differ.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.backtest.broker import OrderSide, SimulatedBroker, Trade
from src.evolution.evaluator import (
    score_paper_session,
    score_paper_trading_results,
)
from src.evolution.fitness import (
    SOURCE_BACKTEST,
    SOURCE_PAPER,
    compute_fitness_from_trades,
)
from src.evolution.pool import (
    StrategyEntry,
    StrategyPool,
    eligible_for_live,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trades(n: int, win_rate: float = 0.6, pv: int = 200,
                 base_date: str = "2025") -> list[Trade]:
    """Build n Trade dataclass instances spread across 6 months.

    Same fixture pattern as test_evolution_fitness so a strategy scoring
    well in backtest should produce the same composite when the trades
    are reframed as paper-trading data — which is exactly the property
    we want to test."""
    out: list[Trade] = []
    months = [f"{base_date}-0{i+1}" for i in range(6)]
    n_wins = int(round(n * win_rate))
    for i in range(n):
        is_win = i < n_wins
        entry, exit_ = 20000, 20000 + (100 if is_win else -50)
        m = months[i % len(months)]
        out.append(Trade(
            tag="L", side=OrderSide.LONG, qty=1,
            entry_price=entry, exit_price=exit_,
            entry_bar_index=i, exit_bar_index=i + 1,
            pnl=(exit_ - entry) * pv,
            entry_dt=f"{m}-15 09:00:00",
            exit_dt=f"{m}-15 10:00:00",
        ))
    return out


def _make_session_dict(trades: list[Trade], started_at: str = "2026-01-01T08:45:00",
                       saved_at: str = "2026-01-21T13:45:00",
                       trading_mode: str = "paper") -> dict:
    """Mirror the session.json shape written by LiveRunner._auto_save_session."""
    # Build a real SimulatedBroker, push trades through to_dict for a
    # faithful serialization — guards against future drift in either
    # the live save format or the broker's serialization.
    broker = SimulatedBroker(point_value=200)
    broker.trades = list(trades)
    broker._cumulative_pnl = sum(t.pnl for t in trades)
    broker.equity_curve = []
    cum = 0
    for t in trades:
        cum += t.pnl
        broker.equity_curve.append(cum)

    return {
        "strategy": "test-strategy",
        "symbol": "TXF1",
        "bot_name": "test-bot",
        "point_value": 200,
        "trading_mode": trading_mode,
        "started_at": started_at,
        "saved_at": saved_at,
        "bar_index": len(trades) + 100,
        "broker": broker.to_dict(),
    }


@pytest.fixture
def pool(tmp_path):
    return StrategyPool(tmp_path / "pool.db")


@pytest.fixture
def candidate_id(pool):
    entry = StrategyEntry(
        name="candidate", source_code="# candidate",
        walkforward_fitness=0.40, status="paper_trading",
    )
    pool.add(entry)
    return entry.id


# ---------------------------------------------------------------------------
# Input-agnostic fitness: backtest vs paper produces identical math
# ---------------------------------------------------------------------------


class TestSourceAgnosticFitness:
    def test_backtest_and_paper_match_when_trades_identical(self):
        """The whole point of the source field: same trades → same
        composite, with only the source tag differing."""
        trades = _make_trades(40, win_rate=0.6)
        bt = compute_fitness_from_trades(trades, source=SOURCE_BACKTEST)
        paper = compute_fitness_from_trades(trades, source=SOURCE_PAPER)
        assert bt.composite == paper.composite
        assert bt.sharpe == paper.sharpe
        assert bt.total_trades == paper.total_trades
        assert bt.source == "backtest"
        assert paper.source == "paper"

    def test_rejects_invalid_source(self):
        with pytest.raises(ValueError):
            compute_fitness_from_trades([], source="garbage")

    def test_accepts_trade_dicts_and_dataclasses(self):
        """Paper sessions can hand us either shape — Trade instances
        from a live SimulatedBroker, or dicts after JSON round-trip."""
        trades = _make_trades(40)
        as_dicts = [
            {
                "pnl": t.pnl, "entry_price": t.entry_price,
                "exit_price": t.exit_price, "entry_dt": t.entry_dt,
                "exit_dt": t.exit_dt, "entry_bar_index": t.entry_bar_index,
                "exit_bar_index": t.exit_bar_index,
            }
            for t in trades
        ]
        fit_a = compute_fitness_from_trades(trades, source=SOURCE_PAPER)
        fit_b = compute_fitness_from_trades(as_dicts, source=SOURCE_PAPER)
        assert fit_a.total_trades == fit_b.total_trades
        assert fit_a.composite == pytest.approx(fit_b.composite)

    def test_rebuilds_equity_curve_when_omitted(self):
        trades = _make_trades(30)
        fit = compute_fitness_from_trades(trades, source=SOURCE_PAPER)
        # Should not crash and total_trades should still be 30.
        assert fit.total_trades == 30

    def test_gating_still_applies(self):
        # Below MIN_TRADES (30), paper fitness gates to 0 too.
        trades = _make_trades(10)
        fit = compute_fitness_from_trades(trades, source=SOURCE_PAPER)
        assert fit.gated is True
        assert fit.composite == 0.0


# ---------------------------------------------------------------------------
# score_paper_trading_results: updates the pool's paper_trading columns
# ---------------------------------------------------------------------------


class TestScorePaperTradingResults:
    def test_updates_pool_columns(self, pool, candidate_id):
        trades = _make_trades(40, win_rate=0.7)
        fit = score_paper_trading_results(
            pool, candidate_id, trades,
            period_start="2026-01-01", period_end="2026-01-20",
        )
        assert fit.source == "paper"
        loaded = pool.get(candidate_id)
        assert loaded.paper_trading_fitness == pytest.approx(fit.composite)
        assert loaded.paper_trading_period_start == "2026-01-01"
        assert loaded.paper_trading_period_end == "2026-01-20"
        assert loaded.paper_trading_trades == 40

    def test_does_not_touch_walkforward(self, pool, candidate_id):
        """Backtest columns are sacred — paper scoring must never
        accidentally overwrite walkforward_fitness."""
        before = pool.get(candidate_id).walkforward_fitness
        score_paper_trading_results(
            pool, candidate_id, _make_trades(40),
            period_start="2026-01-01", period_end="2026-01-20",
        )
        after = pool.get(candidate_id).walkforward_fitness
        assert before == after

    def test_missing_id_raises(self, pool):
        with pytest.raises(KeyError):
            score_paper_trading_results(
                pool, "no-such-id", _make_trades(40),
            )


# ---------------------------------------------------------------------------
# score_paper_session: load session.json -> score -> update pool
# ---------------------------------------------------------------------------


class TestScorePaperSession:
    def _write_session(self, dir_: Path, trades: list[Trade], **kwargs) -> str:
        session = _make_session_dict(trades, **kwargs)
        path = dir_ / "session.json"
        path.write_text(json.dumps(session, default=str), encoding="utf-8")
        return str(path)

    def test_end_to_end_paper_session(self, pool, candidate_id, tmp_path):
        path = self._write_session(
            tmp_path, _make_trades(40, win_rate=0.6),
            started_at="2026-01-01T08:45:00",
            saved_at="2026-01-20T13:45:00",
        )
        fit = score_paper_session(pool, candidate_id, path)
        assert fit is not None
        assert fit.source == "paper"
        assert fit.total_trades == 40

        loaded = pool.get(candidate_id)
        assert loaded.paper_trading_fitness == pytest.approx(fit.composite)
        # Period derived from started_at / saved_at, date portion only.
        assert loaded.paper_trading_period_start == "2026-01-01"
        assert loaded.paper_trading_period_end == "2026-01-20"

    def test_missing_session_returns_none(self, pool, candidate_id, tmp_path):
        """Sessions can be rotated/deleted; that shouldn't be a hard
        error from the SEE side."""
        path = tmp_path / "does-not-exist.json"
        result = score_paper_session(pool, candidate_id, str(path))
        assert result is None
        # Pool was NOT touched.
        loaded = pool.get(candidate_id)
        assert loaded.paper_trading_fitness == 0.0

    def test_refuses_non_paper_mode(self, pool, candidate_id, tmp_path):
        """semi_auto / auto sessions carry real-money trades — refuse
        to score them as paper. They need a separate, deliberate path."""
        path = self._write_session(
            tmp_path, _make_trades(40), trading_mode="auto",
        )
        with pytest.raises(ValueError, match="trading_mode"):
            score_paper_session(pool, candidate_id, path)

    def test_accepts_legacy_session_without_trading_mode(
            self, pool, candidate_id, tmp_path):
        """Sessions saved before the trading_mode field existed are
        treated as paper by default."""
        session = _make_session_dict(_make_trades(40))
        del session["trading_mode"]
        path = tmp_path / "session.json"
        path.write_text(json.dumps(session, default=str), encoding="utf-8")
        result = score_paper_session(pool, candidate_id, str(path))
        assert result is not None


# ---------------------------------------------------------------------------
# Full pipeline: candidate -> validated -> paper_trading -> eligible -> live
# ---------------------------------------------------------------------------


class TestPromotionPipeline:
    def test_full_promotion_flow(self, pool, tmp_path):
        # 1. Add as candidate.
        entry = StrategyEntry(
            name="pipeline-strategy", source_code="# code",
            status="candidate",
        )
        pool.add(entry)
        assert pool.get(entry.id).status == "candidate"

        # 2. Record backtest fitness, promote to validated.
        pool.update_fitness(
            entry.id, {"composite": 0.55, "sharpe": 1.4}, walkforward_fitness=0.45,
        )
        pool.promote(entry.id, "validated")
        loaded = pool.get(entry.id)
        assert loaded.status == "validated"
        assert loaded.walkforward_fitness == pytest.approx(0.45)

        # 3. Operator deploys as paper bot — flips status manually.
        pool.promote(entry.id, "paper_trading")

        # 4. Score accumulated paper trades.
        score_paper_trading_results(
            pool, entry.id,
            _make_trades(40, win_rate=0.7, base_date="2026"),
            period_start="2026-01-01", period_end="2026-01-22",
        )

        # 5. Check eligibility for live.
        loaded = pool.get(entry.id)
        ok, reason = eligible_for_live(loaded, fitness_threshold=0.20)
        assert ok is True, f"should be eligible, got: {reason}"

        # 6. Final promotion.
        pool.promote(entry.id, "live")
        assert pool.get(entry.id).status == "live"

    def test_paper_score_below_threshold_blocks_live(self, pool):
        # 40 trades but mostly losers — paper fitness should be near 0
        # and eligibility should fail on the threshold check.
        entry = StrategyEntry(
            name="bad", source_code="# code",
            walkforward_fitness=0.45, status="paper_trading",
        )
        pool.add(entry)
        # All-losing trades → paper fitness will be very low.
        losing = _make_trades(40, win_rate=0.0, base_date="2026")
        score_paper_trading_results(
            pool, entry.id, losing,
            period_start="2026-01-01", period_end="2026-01-22",
        )
        loaded = pool.get(entry.id)
        ok, reason = eligible_for_live(loaded)
        assert ok is False
        assert "fitness" in reason
