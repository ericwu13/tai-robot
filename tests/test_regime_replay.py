"""Replay the live regime history through the engine under both rule sets.

The fixture is the TMF00_08-04-rss bot's own ``regime_history.csv`` NIGHT
rows for 2026-08-04 .. 2026-09-08 — market features (ADX / DI / ATR ratio
/ EMA slope / close) and the external vote column, no credentials.

Two jobs:

1. **Harness validation.** Under the LEGACY rules the replay must
   reproduce the recorded ``raw_regime`` / ``effective_regime`` /
   ``decision`` columns exactly. If it cannot reproduce what the bot did,
   its claims about the new rules are worthless.
2. **The change.** Under the current rules the engine leaves the stale
   ``trending-down`` on 2026-08-21 instead of 2026-08-28, never arms a
   pause at all, and opens no half-size vote probe out of a flat range
   (``vote_range_probe`` defaults to False).
"""

from src.regime.replay import HistoryRow, diff_sessions, parse_votes, replay
from src.regime.state_machine import RegimeConfig

LEGACY_RULES = dict(pause_freezes_exits=True,
                    vote_range_probe=True,
                    exits_count_as_flips=True,
                    transitional_resets_streak=True)

# date, adx, +DI, -DI, atr_ratio, ema_slope, close, votes,
# recorded raw_regime, recorded effective_regime, recorded decision
LIVE_NIGHTS = [
    ("2026-08-04", 26.5, 25.8, 18.3, 0.94, 233.1, 44537.0, "", "trending-up", "unknown", "hold"),
    ("2026-08-05", 15.3, 18.1, 19.4, 0.88, 98.8, 44278.0, "", "range-bound", "unknown", "hold"),
    ("2026-08-06", 14.0, 14.8, 20.9, 0.92, 91.0, 44473.0, "", "range-bound", "range-bound", "sit_out"),
    ("2026-08-07", 18.7, 25.1, 16.6, 0.87, 101.1, 45021.0, "", "range-bound", "range-bound", "sit_out"),
    ("2026-08-10", 18.6, 15.2, 24.0, 0.81, 13.6, 44700.0, "", "range-bound", "range-bound", "sit_out"),
    ("2026-08-11", 16.5, 21.1, 17.9, 0.90, 63.6, 45287.0, "", "range-bound", "range-bound", "sit_out"),
    ("2026-08-12", 44.6, 30.2, 10.6, 0.88, 148.1, 46122.0, "", "trending-up", "trending-up", "deploy_long"),
    ("2026-08-13", 34.6, 27.0, 15.9, 0.91, 118.8, 46378.0, "", "trending-up", "trending-up", "deploy_long"),
    ("2026-08-14", 32.6, 13.3, 24.4, 0.85, -37.3, 45729.0, "", "trending-down", "trending-down", "deploy_short"),
    ("2026-08-17", 17.7, 21.1, 25.1, 0.98, 25.2, 45956.0, "", "range-bound", "trending-down", "hold"),
    ("2026-08-18", 48.3, 10.7, 36.1, 0.94, -180.4, 44597.0, "W2:trending-down+W3:trending-down", "trending-down", "trending-down", "hold"),
    ("2026-08-19", 31.6, 15.0, 21.6, 1.00, -61.7, 44731.0, "W2:trending-down+W3:trending-up", "trending-down", "trending-down", "hold"),
    ("2026-08-20", 13.2, 21.0, 19.8, 0.95, -24.6, 44817.0, "W3:trending-up", "range-bound", "trending-down", "hold"),
    ("2026-08-21", 16.7, 19.9, 20.5, 0.84, 14.2, 45028.0, "W3:trending-down", "range-bound", "trending-down", "hold"),
    ("2026-08-24", 19.2, 15.7, 24.0, 1.03, -73.0, 44512.0, "W2:trending-down+W3:trending-down", "range-bound", "trending-down", "deploy_short"),
    ("2026-08-25", 21.8, 25.6, 15.3, 0.81, 73.0, 45380.0, "W3:trending-up", "transitional", "trending-down", "deploy_short"),
    ("2026-08-26", 20.6, 25.5, 17.9, 0.83, 87.0, 45873.0, "W3:trending-down", "transitional", "trending-down", "deploy_short"),
    ("2026-08-27", 16.9, 28.4, 20.7, 0.97, 106.4, 46431.0, "W2:trending-up+W3:trending-up", "range-bound", "trending-down", "deploy_short"),
    ("2026-08-28", 19.2, 14.3, 24.4, 1.02, -26.2, 45913.0, "W2:trending-down+W3:trending-down", "range-bound", "range-bound", "deploy_short_half"),
    ("2026-08-31", 21.7, 16.3, 23.6, 0.92, -23.9, 45944.0, "W2:trending-down+W3:trending-down", "transitional", "range-bound", "deploy_short_half"),
    ("2026-09-01", 22.2, 25.3, 26.1, 0.89, 63.1, 46612.0, "W2:trending-down+W3:trending-down", "transitional", "range-bound", "sit_out"),
    ("2026-09-02", 17.8, 21.4, 21.2, 0.91, 6.2, 46392.0, "W2:trending-down+W3:trending-down+W4:trending-up", "range-bound", "range-bound", "sit_out"),
    ("2026-09-03", 18.7, 26.2, 21.1, 0.96, 45.8, 46503.0, "W3:trending-up+W4:trending-down", "range-bound", "range-bound", "sit_out"),
    ("2026-09-04", 15.2, 25.0, 18.3, 0.93, 131.6, 47224.0, "W2:trending-up+W3:trending-down", "range-bound", "range-bound", "sit_out"),
    ("2026-09-07", 22.6, 22.6, 15.4, 0.71, 76.2, 47326.0, "W2:trending-up+W3:trending-up", "transitional", "range-bound", "deploy_long_half"),
    ("2026-09-08", 15.9, 22.3, 20.4, 1.02, 19.1, 47041.0, "W2:trending-up+W3:trending-up+W4:trending-up", "range-bound", "range-bound", "deploy_long_half"),
]

# The bot ran on the RegimeConfig defaults (adx 25/20/30, confirm 2,
# max_flips 3 in a 10-session window, 5-session pause, quorum 2/1).
DEPLOYED = dict(enabled=True, long_strategy="LONG", short_strategy="SHORT")


def rows():
    return [HistoryRow(date=d, adx=adx, plus_di=pdi, minus_di=mdi,
                       atr_ratio=atr_r, ema_slope=slope, close=close,
                       vote_sources=parse_votes(votes),
                       recorded_raw=raw, recorded_effective=eff,
                       recorded_decision=decision)
            for (d, adx, pdi, mdi, atr_r, slope, close, votes,
                 raw, eff, decision) in LIVE_NIGHTS]


def legacy_run():
    return replay(rows(), RegimeConfig(**DEPLOYED, **LEGACY_RULES))


def new_run():
    return replay(rows(), RegimeConfig(**DEPLOYED))


# ── 1. Harness validation against the recording ─────────────────────────

def test_legacy_replay_reproduces_recorded_raw_regime():
    for s, row in zip(legacy_run(), rows()):
        assert s.raw == row.recorded_raw, row.date


def test_legacy_replay_reproduces_recorded_effective_regime():
    for s in legacy_run():
        assert s.effective == s.recorded_effective, s.date


def test_legacy_replay_reproduces_recorded_decisions():
    for s in legacy_run():
        assert s.decision == s.recorded_decision, s.date


def test_legacy_replay_reproduces_the_five_frozen_sessions():
    """08-17 .. 08-21: five rows the live bot recorded as decision=hold,
    confirm_count=0, while raw read range-bound on three of them."""
    frozen = [s.date for s in legacy_run() if s.paused]
    assert frozen == ["2026-08-17", "2026-08-18", "2026-08-19",
                      "2026-08-20", "2026-08-21"]


# ── 2. What the new rules do to the same 26 sessions ────────────────────

def test_new_rules_exit_the_stale_trend_on_08_21():
    by_date = {s.date: s for s in new_run()}
    assert by_date["2026-08-20"].effective == "trending-down"
    assert by_date["2026-08-21"].effective == "range-bound"
    # ... a week earlier than the recording managed.
    assert by_date["2026-08-27"].effective == "range-bound"


def test_new_rules_never_arm_a_pause():
    """Of the three legacy 'flips', one (08-06 unknown -> range-bound) was
    an exit. Dropping it leaves 2 entries in the window — below max_flips,
    so no pause is armed and none can be re-armed later either."""
    assert not any(s.paused for s in new_run())
    assert max(s.flips for s in new_run()) == 2


def test_new_rules_keep_the_streak_through_transitional_reads():
    """08-25 / 08-26 are dead-zone reads between range-bound nights; the
    streak they used to zero is what delayed the exit to 08-28."""
    by_date = {s.date: s for s in new_run()}
    assert by_date["2026-08-25"].raw == "transitional"
    assert by_date["2026-08-25"].pending_count == 1
    assert by_date["2026-08-26"].pending_count == 1


def test_diff_between_the_two_rule_sets():
    """The exact sessions that change, as published in the PR body."""
    pairs = diff_sessions(legacy_run(), new_run())
    assert [(a.date, a.effective, b.effective, a.decision, b.decision)
            for a, b in pairs] == [
        ("2026-08-17", "trending-down", "trending-down", "hold", "deploy_short"),
        ("2026-08-18", "trending-down", "trending-down", "hold", "deploy_short"),
        ("2026-08-19", "trending-down", "trending-down", "hold", "deploy_short"),
        ("2026-08-20", "trending-down", "trending-down", "hold", "deploy_short"),
        ("2026-08-21", "trending-down", "range-bound", "hold", "sit_out"),
        ("2026-08-24", "trending-down", "range-bound", "deploy_short", "sit_out"),
        ("2026-08-25", "trending-down", "range-bound", "deploy_short", "sit_out"),
        ("2026-08-26", "trending-down", "range-bound", "deploy_short", "sit_out"),
        ("2026-08-27", "trending-down", "range-bound", "deploy_short", "sit_out"),
        ("2026-08-28", "range-bound", "range-bound", "deploy_short_half", "sit_out"),
        ("2026-08-31", "range-bound", "range-bound", "deploy_short_half", "sit_out"),
        ("2026-09-07", "range-bound", "range-bound", "deploy_long_half", "sit_out"),
        ("2026-09-08", "range-bound", "range-bound", "deploy_long_half", "sit_out"),
    ]


def test_the_two_rule_sets_agree_on_effective_from_08_28_onward():
    """The regime change is bounded to the frozen stretch — once the
    legacy run catches up on 08-28 both read range-bound. What still
    differs after that is only the vote probe (see the test below)."""
    tail = [(a, b) for a, b in zip(legacy_run(), new_run())
            if a.date >= "2026-08-28"]
    assert tail
    for a, b in tail:
        assert a.effective == b.effective, a.date


def test_new_rules_never_open_a_probe_from_a_flat_range():
    """External votes may only add in the direction the regime leg
    already holds. Every half-size probe the recording opened out of a
    flat range — including 08-24 and 08-27, both of which moved AGAINST
    the vote — becomes sit_out."""
    probes = [s.date for s in legacy_run() if s.decision.endswith("_half")]
    assert probes == ["2026-08-28", "2026-08-31", "2026-09-07", "2026-09-08"]
    assert not [s for s in new_run() if s.decision.endswith("_half")]
