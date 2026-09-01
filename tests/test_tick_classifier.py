"""Tests for tick classification: history→live transition + stale-tick drop.

Covers the bugs these tests are designed to prevent:
- Issue #50: COM mis-labels history ticks as is_history=False, causing
  suppress_strategy to clear on a stale tick and the strategy to trade
  on hours-old data.
- Issue #105: the mirror image — COM delivered the whole post-reconnect
  live stream via OnNotifyHistoryTicksLONG (is_history=True) and never
  switched to the live callback. The transition never fired and the bot
  ran suppressed all session (bars flowed, strategy never traded).
- Bot 271: after watchdog resubscribe, COM replays yesterday's ticks
  with the transition flag already flipped — without per-tick staleness
  drop, those stale ticks build fake "live" bars that feed the strategy.

Contract: tick AGE is the single source of truth in both directions;
the is_history flag never decides anything on its own.
"""

from __future__ import annotations

import pytest

from src.live.tick_classifier import classify_tick, HISTORY_STALENESS_SECONDS


class TestInitialPhase:
    """Pre-transition: live_history_done=False."""

    def test_fresh_live_tick_transitions(self):
        """First fresh tick with is_history=False should transition."""
        verdict = classify_tick(
            tick_age_seconds=1.0,
            is_history_flag=False,
            live_history_done=False,
        )
        assert verdict == "transition"

    def test_stale_mislabelled_tick_kept_not_transitioned(self):
        """Issue #50: COM sends history with is_history=False. Age > 120s
        means we must NOT transition — keep suppressing."""
        verdict = classify_tick(
            tick_age_seconds=3600.0,  # 1 hour old
            is_history_flag=False,
            live_history_done=False,
        )
        assert verdict == "keep"  # bars still build, but no transition

    def test_history_flag_tick_kept(self):
        """Explicit history tick during initial phase — build bars silently."""
        verdict = classify_tick(
            tick_age_seconds=3600.0,
            is_history_flag=True,
            live_history_done=False,
        )
        assert verdict == "keep"

    def test_fresh_history_flag_tick_transitions(self):
        """Issue #105: a FRESH tick must transition even when COM flags it
        is_history=True.

        This assertion previously read ``== "keep"`` — it pinned the bug.
        COM delivered every post-reconnect tick through
        OnNotifyHistoryTicksLONG, so under the old rule no tick could ever
        transition and suppress_strategy latched True for the whole session.
        """
        verdict = classify_tick(
            tick_age_seconds=5.0,
            is_history_flag=True,
            live_history_done=False,
        )
        assert verdict == "transition"

    @pytest.mark.parametrize("is_history_flag", [True, False])
    def test_boundary_at_threshold(self, is_history_flag):
        """Exactly at threshold counts as fresh (uses <=), either flag."""
        verdict = classify_tick(
            tick_age_seconds=float(HISTORY_STALENESS_SECONDS),
            is_history_flag=is_history_flag,
            live_history_done=False,
        )
        assert verdict == "transition"

    @pytest.mark.parametrize("is_history_flag", [True, False])
    def test_just_over_threshold(self, is_history_flag):
        """Just above threshold is stale → keep, whatever the flag says."""
        verdict = classify_tick(
            tick_age_seconds=float(HISTORY_STALENESS_SECONDS + 1),
            is_history_flag=is_history_flag,
            live_history_done=False,
        )
        assert verdict == "keep"


class TestLivePhase:
    """Post-transition: live_history_done=True."""

    def test_fresh_live_tick_kept(self):
        """Normal live tick processed normally."""
        verdict = classify_tick(
            tick_age_seconds=2.0,
            is_history_flag=False,
            live_history_done=True,
        )
        assert verdict == "keep"

    def test_stale_tick_dropped_bot_271_scenario(self):
        """Bot 271: after watchdog resubscribe, COM replays yesterday's
        15:00 ticks during today's 08:45 AM open. These must be dropped,
        not kept — otherwise BarBuilder creates fake 15:00 live bars
        that feed the strategy."""
        age_17h = 17 * 3600.0  # yesterday afternoon → this morning
        verdict = classify_tick(
            tick_age_seconds=age_17h,
            is_history_flag=False,  # COM may lie here
            live_history_done=True,
        )
        assert verdict == "drop"

    def test_stale_history_flag_tick_dropped(self):
        """Even correctly-flagged history ticks are dropped post-transition."""
        verdict = classify_tick(
            tick_age_seconds=3600.0,
            is_history_flag=True,
            live_history_done=True,
        )
        assert verdict == "drop"

    def test_boundary_at_threshold_kept(self):
        """Exactly at threshold is kept (uses >)."""
        verdict = classify_tick(
            tick_age_seconds=float(HISTORY_STALENESS_SECONDS),
            is_history_flag=False,
            live_history_done=True,
        )
        assert verdict == "keep"

    def test_just_over_threshold_dropped(self):
        verdict = classify_tick(
            tick_age_seconds=float(HISTORY_STALENESS_SECONDS + 1),
            is_history_flag=False,
            live_history_done=True,
        )
        assert verdict == "drop"


class TestZombieScenarios:
    """End-to-end scenarios from real bot logs."""

    def test_bot_271_resubscribe_replay(self):
        """Real scenario from TMF00_271_dynamic_pull_back_v2 bot on
        2026-04-16 08:45:

        - Deployed 00:07 (night session), transitioned to live at 00:07:29
        - Ran through night 00:07 → 04:09 (live ticks, live_history_done=True)
        - Market closed 04:09 → 08:45 (3h45m silence)
        - Watchdog detects no ticks for 5m at 08:45:39 → resubscribe
        - Resubscribe resets live_history_done=False
        - COM replays: first a fresh 08:45 tick, then a FLOOD of old
          15:00-yesterday ticks
        """
        # Step 1: fresh 08:45 tick arrives after resubscribe
        verdict = classify_tick(
            tick_age_seconds=0.5,  # fresh
            is_history_flag=False,
            live_history_done=False,  # reset by resubscribe
        )
        assert verdict == "transition"

        # Step 2: now live_history_done=True. Old 15:00 tick from
        # yesterday arrives (COM flood replay)
        live_history_done = True
        yesterday_15_00_age = 17.75 * 3600.0  # ≈17h45m
        verdict = classify_tick(
            tick_age_seconds=yesterday_15_00_age,
            is_history_flag=False,  # COM lies again
            live_history_done=live_history_done,
        )
        # Before the fix: this would have been "keep" (processed as live),
        # feeding fake 15:00 bars to strategy. After fix: drop.
        assert verdict == "drop"

    def test_initial_deploy_premarket_builds_bars_silently(self):
        """Deploying pre-market (07:22 like issue #59):
        COM replays overnight session's history with is_history=True.
        We keep processing (bars build silently via BarBuilder) but
        don't transition until a fresh tick arrives after market opens."""
        # Overnight history replay — 12h old
        verdict = classify_tick(
            tick_age_seconds=12 * 3600.0,
            is_history_flag=True,
            live_history_done=False,
        )
        assert verdict == "keep"  # bars build; strategy stays suppressed

    def test_initial_deploy_midsession_transitions_immediately(self):
        """Normal deploy during market hours: COM sends replay then live
        ticks. Any tick already inside the freshness window transitions.

        The first assertion used to expect "keep" (it pinned the issue #105
        bug). A 1-second-old tick is current data by any measure — the
        documented trade-off is that the tail end of a legitimate replay
        transitions slightly early, on essentially-current prices."""
        # Tail of the replay burst, still flagged history but current
        assert classify_tick(1.0, True, False) == "transition"
        # A fresh tick on the live callback transitions too
        assert classify_tick(0.5, False, False) == "transition"

    def test_issue_105_all_ticks_flagged_history(self):
        """Real scenario from TMF00 DynamicExitPullbackStrategyV2 on
        2026-09-01 (v2.17.21):

        - Quote reconnect at 08:47:52 → _resubscribe_ticks resets
          live_history_done=False, suppress_strategy=True
        - First tick arrives 0.84s later — but COM routes the ENTIRE
          stream (24,666 ticks, ~20/s) through OnNotifyHistoryTicksLONG
        - Old rule: is_history=True → "keep" forever, no transition ever.
          The bot ran suppressed all session; the 600s safety valve cleared
          only _is_reloading, leaving suppress_strategy latched.
        """
        # The very first post-reconnect tick, 0.84s old but flagged history
        assert classify_tick(0.84, True, False) == "transition"

        # Everything after it is a normal fresh tick in live mode
        assert classify_tick(0.9, True, True) == "keep"
        assert classify_tick(1.2, False, True) == "keep"

    def test_stale_only_feed_stays_suppressed(self):
        """Counterpart to #105: when every tick really IS old (dead feed /
        pure replay), no transition fires. The 600s valve then clears only
        the reload window — keeping the strategy suppressed is correct."""
        for flag in (True, False):
            assert classify_tick(4000.0, flag, False) == "keep"

    def test_custom_threshold(self):
        """Threshold is overridable for tests."""
        verdict = classify_tick(
            tick_age_seconds=50.0,
            is_history_flag=False,
            live_history_done=False,
            staleness_threshold=30,  # tighter
        )
        assert verdict == "keep"  # 50 > 30 → stale
