"""Tick-flow state machine for live COM tick processing.

Encapsulates the four state variables that together gate the
history -> live transition and the post-transition stale-tick drop:

- ``live_history_done``  — one-way flag flipped to True on the first fresh
  tick (or first fresh tick after a resubscribe reset).
- ``tick_count``         — total ticks received this session.
- ``history_tick_count`` — snapshot of ``tick_count`` taken at the transition
  moment, used by downstream logging to decide which ticks are "the first
  few live ticks".
- ``stale_drops``        — diagnostic counter for stale replay ticks that
  arrived after the transition (bot 271 watchdog-resubscribe scenario).

The wiring of ``classify_tick`` into ``_on_com_tick`` used to live inline
in the GUI app, which made the state mutations untestable.  This class
lifts those mutations into a single method (``process``) with a tight
invariant so they can be exercised without a Tkinter / COM environment.

Note: ``suppress_strategy`` is deliberately NOT owned here — it's a
``LiveRunner`` attribute.  ``process`` reports ``should_unsuppress=True``
on the transition tick so the caller can flip the runner flag itself.
"""

from __future__ import annotations

from typing import Callable, Optional

from src.live.tick_classifier import classify_tick


class TickFlowState:
    """Owns the per-session tick-flow state for the live runner."""

    def __init__(self, log_fn: Optional[Callable[[str], None]] = None) -> None:
        self.live_history_done: bool = False
        self.tick_count: int = 0
        self.history_tick_count: int = 0
        self.stale_drops: int = 0
        self._log: Callable[[str], None] = log_fn or (lambda msg: None)

    def reset(self) -> None:
        """Reset every field back to its init default.

        Called on tick resubscribe (watchdog reconnect path) and on stop,
        so each subscription gets a clean transition state.
        """
        self.live_history_done = False
        self.tick_count = 0
        self.history_tick_count = 0
        self.stale_drops = 0

    def pre_classify(self) -> None:
        """Must be called FIRST on every tick, before ``process``.

        Increments ``tick_count`` so that the snapshot captured by a
        "transition" verdict in ``process`` includes the current tick.
        """
        self.tick_count += 1

    def process(
        self,
        tick_age_seconds: float,
        is_history: bool,
    ) -> tuple[str, bool]:
        """Classify a tick and mutate state accordingly.

        Args:
            tick_age_seconds: wall-clock age of the tick in seconds.
            is_history: the ``is_history`` flag from the COM callback.

        Returns:
            ``(verdict, should_unsuppress_strategy)`` where ``verdict`` is
            one of ``"transition" | "drop" | "keep"`` and
            ``should_unsuppress_strategy`` is True ONLY on the transition
            tick — the caller is responsible for flipping
            ``LiveRunner.suppress_strategy``.

        IMPORTANT: ``pre_classify()`` must have been called before this.
        """
        verdict = classify_tick(
            tick_age_seconds=tick_age_seconds,
            is_history_flag=is_history,
            live_history_done=self.live_history_done,
        )

        should_unsuppress = False
        if verdict == "transition":
            # Order is load-bearing: flip the flag BEFORE taking the
            # snapshot / signalling unsuppress, so any re-entry can see
            # the true post-transition state.
            self.live_history_done = True
            self.history_tick_count = self.tick_count
            should_unsuppress = True
            self._log(f"[TickFlowState] transition at tick {self.tick_count}")
        elif verdict == "drop":
            self.stale_drops += 1
            if self.stale_drops <= 5 or self.stale_drops % 50 == 0:
                self._log(f"[TickFlowState] stale drop #{self.stale_drops}")

        return verdict, should_unsuppress
