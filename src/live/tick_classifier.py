"""Classify incoming COM ticks: transition signal, drop, or keep.

Extracted from ``run_backtest.py._on_com_tick`` for unit-testability.
Two independent concerns handled here:

1. **Issue #50 defense** — COM sometimes mis-labels history ticks as
   ``is_history=False``.  We rely on wall-clock age (> staleness
   threshold) instead of trusting the flag alone.

2. **Bot 271 fix** — after the one-way ``live_history_done`` flag
   flips to True, any stale tick arriving later is a replay from a
   resubscribe (e.g., watchdog forced resubscribe after an overnight
   gap). Without dropping these, BarBuilder creates fake "live" bars
   with yesterday's prices and the strategy trades on stale data.

Future work: the WIRING of classify_tick into ``_on_com_tick`` (the
state mutations that follow each verdict) is not directly unit-tested.
Recommended follow-up: extract a ``TickFlowState`` class that owns
``live_history_done``, ``tick_count``, ``stale_drops``, and exposes a
``process(tick_age, is_history) -> verdict`` method.  Then
``_on_com_tick`` becomes a thin adapter and the state machine is
100% unit-testable.
"""

from __future__ import annotations


HISTORY_STALENESS_SECONDS = 120  # 2 minutes


def classify_tick(
    tick_age_seconds: float,
    is_history_flag: bool,
    live_history_done: bool,
    staleness_threshold: int = HISTORY_STALENESS_SECONDS,
) -> str:
    """Decide what to do with a newly-arrived COM tick.

    Args:
        tick_age_seconds: seconds between wall-clock now and tick.dt
            (positive = tick is in the past).
        is_history_flag: the ``is_history`` param from the COM callback.
            Not fully trusted — see issue #50.
        live_history_done: session-level flag; True means a fresh tick
            has already transitioned the session to live mode.
        staleness_threshold: ticks older than this are "stale".

    Returns:
        ``"transition"`` — this is the first fresh tick, unsuppress strategy.
        ``"drop"``       — stale tick arriving after live transition; ignore.
        ``"keep"``       — process normally (live tick, or pre-transition
                           history tick that builds bars silently).
    """
    if not is_history_flag and not live_history_done:
        # Pre-transition phase: fresh tick → transition; stale → keep
        # processing silently (BarBuilder still builds bars for warmup).
        if tick_age_seconds <= staleness_threshold:
            return "transition"
        return "keep"

    # Post-transition: drop stale replay ticks to prevent contamination.
    if live_history_done and tick_age_seconds > staleness_threshold:
        return "drop"

    return "keep"
