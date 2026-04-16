"""Tests for ``TickFlowState`` — the per-session tick-flow state machine.

These tests cover the state mutations around ``classify_tick`` which used
to live inline in ``run_backtest._on_com_tick`` and therefore had no
unit-test coverage.  They complement (do not replace) the existing
``test_tick_classifier`` suite: the classifier tests pin the pure
verdict function, these tests pin the state-machine wiring.

Key invariants we protect:

1. ``live_history_done`` is one-way within a session (never flips back
   to False except via an explicit ``reset()``).
2. ``history_tick_count`` is captured at the exact tick that transitions
   — critical for downstream "first few live ticks" logging.
3. ``should_unsuppress`` is True ONLY on the single transition tick —
   if this ever leaked onto subsequent keeps, ``LiveRunner`` would flip
   ``suppress_strategy`` repeatedly and lose its meaning.
4. ``stale_drops`` increments exactly once per "drop" verdict (bot 271
   diagnostic counter).
5. ``reset()`` returns every field to its init default — this is the
   piece that used to drift across three separate sites.
"""

from __future__ import annotations

import pytest

from src.live.tick_flow_state import TickFlowState
from src.live.tick_classifier import HISTORY_STALENESS_SECONDS


# ─────────────────────── helpers ──────────────────────────

FRESH = 1.0                              # tick_age_seconds: fresh
STALE = float(HISTORY_STALENESS_SECONDS + 1)  # tick_age_seconds: stale


def _feed(state: TickFlowState, tick_age: float, is_history: bool):
    """Simulate the pre_classify + process pair that _on_com_tick runs."""
    state.pre_classify()
    return state.process(tick_age_seconds=tick_age, is_history=is_history)


# ─────────────────── init & reset ────────────────────────

class TestInit:
    def test_default_fields_zero_and_false(self):
        s = TickFlowState()
        assert s.live_history_done is False
        assert s.tick_count == 0
        assert s.history_tick_count == 0
        assert s.stale_drops == 0

    def test_no_logger_does_not_crash(self):
        s = TickFlowState(log_fn=None)
        s.pre_classify()
        # transition path exercises the log call
        verdict, unsup = s.process(FRESH, is_history=False)
        assert verdict == "transition"
        assert unsup is True
        # drop path also exercises the log call
        _feed(s, STALE, is_history=False)


class TestReset:
    def test_reset_restores_every_default(self):
        s = TickFlowState()
        # dirty every field
        _feed(s, FRESH, is_history=False)       # transition: flips done + snapshot
        _feed(s, STALE, is_history=False)       # drop: bumps stale_drops
        _feed(s, FRESH, is_history=False)       # keep: bumps tick_count
        assert s.live_history_done is True
        assert s.tick_count > 0
        assert s.history_tick_count > 0
        assert s.stale_drops > 0

        s.reset()
        assert s.live_history_done is False
        assert s.tick_count == 0
        assert s.history_tick_count == 0
        assert s.stale_drops == 0

    def test_reset_then_transition_fires_again(self):
        """Resubscribe scenario: after reset, the next fresh tick must
        re-trigger the transition. Without this, watchdog reconnect
        would permanently keep the strategy suppressed."""
        s = TickFlowState()
        _feed(s, FRESH, is_history=False)
        assert s.live_history_done is True

        s.reset()
        verdict, unsup = _feed(s, FRESH, is_history=False)
        assert verdict == "transition"
        assert unsup is True
        assert s.live_history_done is True
        assert s.tick_count == 1
        assert s.history_tick_count == 1


# ─────────────── pre_classify / process ──────────────────

class TestPreClassify:
    def test_pre_classify_increments_before_process(self):
        """history_tick_count captured on transition must INCLUDE the
        transition tick itself — this is what ``pre_classify`` before
        ``process`` guarantees."""
        s = TickFlowState()
        _feed(s, STALE, is_history=True)    # keep, tick 1
        _feed(s, STALE, is_history=True)    # keep, tick 2
        verdict, _ = _feed(s, FRESH, is_history=False)  # transition, tick 3
        assert verdict == "transition"
        assert s.tick_count == 3
        assert s.history_tick_count == 3  # includes the transition tick

    def test_counter_grows_monotonically_across_verdicts(self):
        s = TickFlowState()
        for i in range(1, 6):
            _feed(s, STALE, is_history=True)  # keep pre-transition
            assert s.tick_count == i


class TestProcessVerdicts:
    def test_transition_sets_flag_and_snapshots_count(self):
        s = TickFlowState()
        s.pre_classify()  # tick 1
        verdict, unsup = s.process(FRESH, is_history=False)
        assert verdict == "transition"
        assert unsup is True
        assert s.live_history_done is True
        assert s.history_tick_count == 1

    def test_keep_mutates_nothing_but_tick_count(self):
        s = TickFlowState()
        verdict, unsup = _feed(s, STALE, is_history=True)
        assert verdict == "keep"
        assert unsup is False
        assert s.live_history_done is False
        assert s.history_tick_count == 0
        assert s.stale_drops == 0
        assert s.tick_count == 1  # only pre_classify effect

    def test_drop_increments_stale_drops_and_signals_nothing_else(self):
        s = TickFlowState()
        _feed(s, FRESH, is_history=False)         # transition first
        initial_hist = s.history_tick_count
        verdict, unsup = _feed(s, STALE, is_history=False)
        assert verdict == "drop"
        assert unsup is False
        assert s.stale_drops == 1
        assert s.live_history_done is True           # unchanged
        assert s.history_tick_count == initial_hist  # unchanged


class TestOneWayInvariant:
    def test_done_never_flips_back_after_many_ticks(self):
        """Post-transition, feed every conceivable mix: the flag must
        stay True and no subsequent tick must report should_unsuppress."""
        s = TickFlowState()
        _feed(s, FRESH, is_history=False)
        assert s.live_history_done is True

        for i in range(100):
            if i % 3 == 0:
                _, unsup = _feed(s, STALE, is_history=False)  # drop
            elif i % 3 == 1:
                _, unsup = _feed(s, STALE, is_history=True)   # drop
            else:
                _, unsup = _feed(s, FRESH, is_history=False)  # keep
            assert s.live_history_done is True, f"flag regressed at iter {i}"
            assert unsup is False, f"spurious unsuppress at iter {i}"

    def test_should_unsuppress_true_only_on_transition_tick(self):
        s = TickFlowState()
        # Pre-transition keeps never unsuppress
        for _ in range(5):
            _, unsup = _feed(s, STALE, is_history=True)
            assert unsup is False

        # Transition unsuppresses exactly once
        _, unsup = _feed(s, FRESH, is_history=False)
        assert unsup is True

        # Every subsequent tick must NOT unsuppress
        for _ in range(20):
            _, unsup = _feed(s, FRESH, is_history=False)
            assert unsup is False


class TestStaleDropsCounter:
    def test_one_increment_per_drop_verdict(self):
        s = TickFlowState()
        _feed(s, FRESH, is_history=False)  # transition
        for expected in range(1, 8):
            _feed(s, STALE, is_history=False)
            assert s.stale_drops == expected

    def test_keep_after_drop_does_not_bump_counter(self):
        s = TickFlowState()
        _feed(s, FRESH, is_history=False)   # transition → done
        _feed(s, STALE, is_history=False)   # drop
        assert s.stale_drops == 1
        _feed(s, FRESH, is_history=False)   # keep (live tick)
        assert s.stale_drops == 1


# ────────────────── logger injection ─────────────────────

class RecordingLogger:
    def __init__(self):
        self.messages: list[str] = []

    def __call__(self, msg: str) -> None:
        self.messages.append(msg)


class TestLoggerInjection:
    def test_transition_logs_once(self):
        log = RecordingLogger()
        s = TickFlowState(log_fn=log)
        _feed(s, FRESH, is_history=False)
        assert any("transition" in m for m in log.messages)
        transitions = [m for m in log.messages if "transition" in m]
        assert len(transitions) == 1

    def test_drop_logs_initial_drops_and_periodic_samples(self):
        log = RecordingLogger()
        s = TickFlowState(log_fn=log)
        _feed(s, FRESH, is_history=False)  # transition (1 log)
        # first 5 drops should each log
        for _ in range(5):
            _feed(s, STALE, is_history=False)
        drop_logs = [m for m in log.messages if "stale drop" in m]
        assert len(drop_logs) == 5

    def test_keep_does_not_log(self):
        log = RecordingLogger()
        s = TickFlowState(log_fn=log)
        _feed(s, STALE, is_history=True)   # keep
        assert log.messages == []
