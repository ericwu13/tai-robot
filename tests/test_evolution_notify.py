"""Tests for src.evolution.notify — baseline + improvement detection +
end-to-end glue used by the live daily-report hook.

The fitness math itself is covered by test_evolution_fitness.py; here
we only care about:
  * baseline file I/O (load, save, atomic replace, corrupt-file recovery)
  * detect_improvement decision table
  * check_and_notify_after_report wiring (send/persist ordering)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta

import pytest

from src.evolution.fitness import FitnessResult, SOURCE_BACKTEST, SOURCE_PAPER
from src.evolution.notify import (
    Baseline,
    BASELINE_FILENAME,
    IMPROVEMENT_DELTA,
    ImprovementVerdict,
    baseline_path,
    check_and_notify_after_report,
    detect_improvement,
    load_baseline,
    save_baseline,
    score_session_for_notification,
)


_TZ = timezone(timedelta(hours=8))


def _fit(composite: float, gated: bool = False, n_trades: int = 50,
         source: str = SOURCE_PAPER) -> FitnessResult:
    """Build a FitnessResult with only the fields detect_improvement reads."""
    return FitnessResult(
        composite=composite,
        sharpe=1.0,
        sortino=1.5,
        max_drawdown_pct=10.0,
        profit_factor=1.8,
        win_rate=0.55,
        consistency=0.6,
        regime_bull=0.5,
        regime_bear=0.5,
        regime_sideways=0.5,
        total_trades=n_trades,
        gated=gated,
        source=source,
    )


class TestBaselineSerialization:
    def test_to_dict_roundtrip(self):
        b = Baseline(best_composite=0.62, best_recorded_at="2026-05-20 13:30:00",
                     n_updates=3)
        b2 = Baseline.from_dict(b.to_dict())
        assert b2 == b

    def test_from_dict_tolerates_missing_keys(self):
        # Future-proofing: forward-compat with older or partial files
        b = Baseline.from_dict({})
        assert b.best_composite == 0.0
        assert b.best_recorded_at == ""
        assert b.n_updates == 0

    def test_from_dict_coerces_types(self):
        b = Baseline.from_dict(
            {"best_composite": "0.5", "best_recorded_at": 123, "n_updates": "7"}
        )
        assert b.best_composite == 0.5
        assert b.best_recorded_at == "123"
        assert b.n_updates == 7


class TestLoadSaveBaseline:
    def test_load_returns_none_when_file_missing(self, tmp_path):
        assert load_baseline(tmp_path) is None

    def test_load_returns_none_on_corrupt_file(self, tmp_path):
        # detect_improvement falls back to "first run" semantics rather
        # than raising — the user should still get a notification on
        # next improvement even if the file got mangled.
        (tmp_path / BASELINE_FILENAME).write_text("not json", encoding="utf-8")
        assert load_baseline(tmp_path) is None

    def test_save_creates_directory(self, tmp_path):
        nested = tmp_path / "deep" / "bot_dir"
        b = Baseline(best_composite=0.5, best_recorded_at="now", n_updates=1)
        save_baseline(nested, b)
        assert (nested / BASELINE_FILENAME).exists()

    def test_save_load_roundtrip(self, tmp_path):
        original = Baseline(best_composite=0.73,
                            best_recorded_at="2026-05-20 13:30:00",
                            n_updates=5)
        save_baseline(tmp_path, original)
        loaded = load_baseline(tmp_path)
        assert loaded == original

    def test_save_is_atomic_no_temp_file_left(self, tmp_path):
        save_baseline(tmp_path, Baseline(0.5, "x", 1))
        # The .tmp file used for atomic replace must not remain
        assert not (tmp_path / (BASELINE_FILENAME + ".tmp")).exists()


class TestDetectImprovement:
    def test_first_run_notifies_and_writes_baseline(self):
        v = detect_improvement(_fit(0.42), previous=None)
        assert v.send_notification is True
        assert v.new_baseline is not None
        assert v.new_baseline.best_composite == 0.42
        assert v.new_baseline.n_updates == 1
        assert "first scored" in v.reason

    def test_gated_never_notifies(self):
        # Even on first run, a gated fitness (composite forced to 0)
        # should NOT bootstrap a baseline of 0.0 — that would suppress
        # all future notifications until composite climbed past 0.05.
        v = detect_improvement(_fit(0.0, gated=True, n_trades=5),
                               previous=None)
        assert v.send_notification is False
        assert v.new_baseline is None
        assert "gated" in v.reason

    def test_no_improvement_below_delta(self):
        prev = Baseline(best_composite=0.5, best_recorded_at="ts", n_updates=1)
        # 0.50 → 0.54 = +0.04, less than default 0.05 threshold
        v = detect_improvement(_fit(0.54), previous=prev)
        assert v.send_notification is False
        assert v.new_baseline is None
        assert "no improvement" in v.reason

    def test_no_improvement_when_identical_composite(self):
        # Idempotency comes from the baseline rewrite (once a score is
        # notified it becomes the new prev_best → next call with same
        # score has delta == 0), not from the strict-> operator. The
        # exact-delta boundary itself is undefined under FP arithmetic
        # (e.g. 0.55 - 0.5 == 0.05000…044) so we don't pin it.
        prev = Baseline(best_composite=0.55, best_recorded_at="ts", n_updates=1)
        v = detect_improvement(_fit(0.55), previous=prev)
        assert v.send_notification is False
        assert v.delta == 0.0

    def test_improvement_above_delta_notifies(self):
        prev = Baseline(best_composite=0.5, best_recorded_at="ts", n_updates=1)
        v = detect_improvement(_fit(0.60), previous=prev)
        assert v.send_notification is True
        assert v.new_baseline is not None
        assert v.new_baseline.best_composite == 0.60
        assert v.new_baseline.n_updates == 2
        assert v.delta == pytest.approx(0.10)

    def test_regression_below_baseline_no_notify(self):
        # Composite went DOWN — should never notify (and never overwrite
        # the baseline with a worse score).
        prev = Baseline(best_composite=0.7, best_recorded_at="ts", n_updates=4)
        v = detect_improvement(_fit(0.5), previous=prev)
        assert v.send_notification is False
        assert v.new_baseline is None
        assert v.delta < 0

    def test_custom_delta_threshold(self):
        prev = Baseline(best_composite=0.5, best_recorded_at="ts", n_updates=1)
        # A stricter threshold means +0.06 isn't enough
        v_strict = detect_improvement(_fit(0.56), previous=prev, delta=0.10)
        assert v_strict.send_notification is False
        # A looser threshold lets +0.02 through
        v_loose = detect_improvement(_fit(0.52), previous=prev, delta=0.01)
        assert v_loose.send_notification is True


class TestScoreSessionForNotification:
    def test_paper_mode_tagged_paper(self):
        trades = [{"pnl": 100, "entry_dt": "2026-05-01 10:00",
                   "exit_dt": "2026-05-01 11:00"}] * 40
        fit = score_session_for_notification(
            trades, equity_curve=None, trading_mode="paper",
        )
        assert fit.source == SOURCE_PAPER

    def test_semi_auto_mode_also_tagged_paper(self):
        # Notification semantics: semi_auto runs real fills with no
        # look-ahead, so the trades are paper-equivalent for scoring.
        trades = [{"pnl": 100, "entry_dt": "2026-05-01 10:00",
                   "exit_dt": "2026-05-01 11:00"}] * 40
        fit = score_session_for_notification(
            trades, equity_curve=None, trading_mode="semi_auto",
        )
        assert fit.source == SOURCE_PAPER

    def test_auto_mode_also_tagged_paper(self):
        trades = [{"pnl": 100, "entry_dt": "2026-05-01 10:00",
                   "exit_dt": "2026-05-01 11:00"}] * 40
        fit = score_session_for_notification(
            trades, equity_curve=None, trading_mode="auto",
        )
        assert fit.source == SOURCE_PAPER

    def test_backtest_mode_tagged_backtest(self):
        trades = [{"pnl": 100, "entry_dt": "2026-05-01 10:00",
                   "exit_dt": "2026-05-01 11:00"}] * 40
        fit = score_session_for_notification(
            trades, equity_curve=None, trading_mode="backtest",
        )
        assert fit.source == SOURCE_BACKTEST


class TestCheckAndNotifyAfterReport:
    """End-to-end glue test the GUI hook depends on."""

    def _trades(self, n: int):
        # >=30 to clear the MIN_TRADES gate; mix winners/losers so
        # the fitness composite is non-trivial.
        return [
            {"pnl": 100 if i % 2 == 0 else -50,
             "entry_dt": f"2026-05-{(i % 28) + 1:02d} 10:00",
             "exit_dt": f"2026-05-{(i % 28) + 1:02d} 11:00",
             "entry_price": 1000, "exit_price": 1100 if i % 2 == 0 else 950,
             "entry_bar_index": i * 2, "exit_bar_index": i * 2 + 1}
            for i in range(n)
        ]

    def test_first_run_writes_baseline_and_invokes_callback(self, tmp_path):
        captured: list[ImprovementVerdict] = []
        verdict = check_and_notify_after_report(
            bot_dir=tmp_path,
            trades=self._trades(50),
            equity_curve=None,
            trading_mode="semi_auto",
            send_notification=captured.append,
        )
        assert verdict.send_notification is True
        assert len(captured) == 1
        assert captured[0] is verdict
        # Baseline file MUST exist after the send
        assert (tmp_path / BASELINE_FILENAME).exists()

    def test_callback_exception_does_not_crash(self, tmp_path):
        def _bad(_v):
            raise RuntimeError("discord down")

        # No assertion error — the exception must be swallowed
        verdict = check_and_notify_after_report(
            bot_dir=tmp_path,
            trades=self._trades(50),
            equity_curve=None,
            trading_mode="semi_auto",
            send_notification=_bad,
        )
        assert verdict.send_notification is True
        # Baseline still got persisted (we save BEFORE the send call)
        assert (tmp_path / BASELINE_FILENAME).exists()

    def test_no_trades_returns_gated_no_baseline(self, tmp_path):
        captured: list[ImprovementVerdict] = []
        verdict = check_and_notify_after_report(
            bot_dir=tmp_path,
            trades=[],
            equity_curve=[],
            trading_mode="semi_auto",
            send_notification=captured.append,
        )
        # Empty trades → gated; no notification, no baseline.
        assert verdict.send_notification is False
        assert captured == []
        assert not (tmp_path / BASELINE_FILENAME).exists()

    def test_second_call_no_improvement_keeps_old_baseline(self, tmp_path):
        # First call writes baseline at composite ~X
        v1 = check_and_notify_after_report(
            bot_dir=tmp_path,
            trades=self._trades(50),
            equity_curve=None,
            trading_mode="semi_auto",
        )
        assert v1.send_notification is True
        first_baseline = load_baseline(tmp_path)
        assert first_baseline is not None

        # Second call with the SAME trades → same composite → no
        # improvement → baseline file must not be touched.
        v2 = check_and_notify_after_report(
            bot_dir=tmp_path,
            trades=self._trades(50),
            equity_curve=None,
            trading_mode="semi_auto",
        )
        assert v2.send_notification is False
        second_baseline = load_baseline(tmp_path)
        assert second_baseline == first_baseline

    def test_no_send_when_callback_is_none(self, tmp_path):
        # Callback is optional — code path must work for "dry-run"
        # callers (e.g. tests or a GUI mode with Discord disabled).
        verdict = check_and_notify_after_report(
            bot_dir=tmp_path,
            trades=self._trades(50),
            equity_curve=None,
            trading_mode="semi_auto",
            send_notification=None,
        )
        assert verdict.send_notification is True
        assert (tmp_path / BASELINE_FILENAME).exists()
