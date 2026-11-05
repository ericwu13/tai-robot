"""The real-order daily loss limit must reset at the trading-day boundary.

2026-11-04 12:55 a bot hit the limit (net -10,042 < -10,000) and paused
real orders. At 2026-11-05 12:46 it was still refusing entries, on a
process that had been up since 08-28 — ``paused`` was a latch that only
``reset()`` on a fresh deploy ever cleared. Meanwhile the broker's own
day figure (``GetFutureRights`` net) had already rolled back to 0, so
the account itself was nowhere near the limit.

The simulated strategy kept trading through the pause, so the bot showed
an open SHORT that did not exist on the account.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.live.trading_guard import (
    DEFAULT_DAILY_LOSS_LIMIT_NTD,
    TradingGuard,
    trading_day_key,
)

D4 = "2026-11-04"
D5 = "2026-11-05"


# ── trading_day_key ────────────────────────────────────────────────────

@pytest.mark.parametrize("when, expected", [
    (datetime(2026, 11, 4, 15, 0), D4),      # night open — the day starts
    (datetime(2026, 11, 4, 14, 59), "2026-11-03"),   # one minute before
    (datetime(2026, 11, 4, 20, 0), D4),      # night session
    (datetime(2026, 11, 5, 4, 58), D4),      # after midnight, SAME day
    (datetime(2026, 11, 5, 8, 45), D4),      # day session of that key
    (datetime(2026, 11, 5, 13, 45), D4),     # day close, still the same key
    (datetime(2026, 11, 5, 15, 0), D5),      # next night open — rolls
])
def test_trading_day_key_rolls_at_1500_tpe(when, expected):
    assert trading_day_key(when) == expected


# ── the latch ──────────────────────────────────────────────────────────

def test_pause_releases_on_the_next_trading_day():
    """The incident, replayed."""
    g = TradingGuard(daily_loss_limit=10000)
    assert g.update_pnl(-10042, day_key=D4) is True
    assert g.paused
    assert g.check_entry("auto")[0] is False

    # Next trading day; the broker's day figure has rolled back to 0.
    assert g.update_pnl(0, day_key=D5) is False
    assert not g.paused
    assert g.check_entry("auto")[0] is True


def test_pause_holds_for_the_rest_of_the_same_trading_day():
    """Must not clear mid-session — including across midnight."""
    g = TradingGuard(daily_loss_limit=10000)
    g.update_pnl(-10042, day_key=D4)
    for net in (-10042, -9000, -500, 0, 250):
        g.update_pnl(net, day_key=D4)       # 20:00, 23:00, 04:58, 08:45 …
        assert g.paused, f"released mid-session at net={net}"


def test_release_then_retrip_pauses_again_on_the_new_day():
    g = TradingGuard(daily_loss_limit=10000)
    g.update_pnl(-10042, day_key=D4)
    g.update_pnl(0, day_key=D5)
    assert not g.paused
    assert g.update_pnl(-10500, day_key=D5) is True
    assert g.paused and g._pause_day_key == D5


def test_no_day_key_keeps_the_old_latch_behaviour():
    """Callers without a clock (and the backtest side) are unaffected."""
    g = TradingGuard(daily_loss_limit=10000)
    g.update_pnl(-10042)
    assert g.paused
    g.update_pnl(0)
    assert g.paused, "a keyless caller must not get a surprise release"


def test_reset_clears_the_pause_and_its_day():
    g = TradingGuard(daily_loss_limit=10000)
    g.update_pnl(-10042, day_key=D4)
    g.reset()
    assert not g.paused and g._pause_day_key == ""


def test_a_pause_set_without_a_key_is_not_released_by_one():
    """Mixed callers must not clear a pause whose day is unknown."""
    g = TradingGuard(daily_loss_limit=10000)
    g.update_pnl(-10042)                     # paused, no key recorded
    g.update_pnl(0, day_key=D5)
    assert g.paused


# ── default ────────────────────────────────────────────────────────────

def test_default_limit_is_20000():
    assert DEFAULT_DAILY_LOSS_LIMIT_NTD == 20000
    assert TradingGuard().daily_loss_limit == 20000


def test_limit_of_zero_disables_the_gate():
    g = TradingGuard(daily_loss_limit=0)
    assert g.update_pnl(-999999, day_key=D4) is False
    assert not g.paused


# ── the production timeline, replayed ──────────────────────────────────

def test_the_2026_11_04_incident_timeline():
    """End to end on the real log timestamps.

    Assumes the broker's ``GetFutureRights`` net has reset by the first
    poll of the new trading day (Steven read -10,042 on 11-04 and 0 on
    11-05 12:48; the exact reset time is not observed). If it has not,
    see ``test_a_still_losing_account_re_pauses_on_the_new_day``.

    Note the 12:55 trip keys to 2026-11-03 — 12:55 is before the 15:00
    open, so it belongs to the session that opened the previous day.
    """
    g = TradingGuard(daily_loss_limit=10000)
    timeline = [
        ("2026-11-04 10:46", -2000),
        ("2026-11-04 12:55", -10042),   # LIMIT HIT (real log line)
        ("2026-11-04 13:44", -10042),
        ("2026-11-04 20:00", 0),        # new trading day, broker net reset
        ("2026-11-05 04:58", 0),
        ("2026-11-05 12:46", 0),        # the entry production blocked
    ]
    seen = []
    for ts, net in timeline:
        now = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        g.update_pnl(net, day_key=trading_day_key(now))
        seen.append((ts, trading_day_key(now), g.paused))

    assert seen[1] == ("2026-11-04 12:55", "2026-11-03", True), seen
    assert seen[2][2] is True, "must hold for the rest of that trading day"
    assert seen[3] == ("2026-11-04 20:00", "2026-11-04", False), seen
    assert g.check_entry("auto")[0] is True


def test_a_still_losing_account_re_pauses_on_the_new_day():
    """The release is not an amnesty: a genuinely down account re-trips."""
    g = TradingGuard(daily_loss_limit=10000)
    g.update_pnl(-10042, day_key=trading_day_key(datetime(2026, 11, 4, 12, 55)))
    # New trading day, but the broker figure has NOT recovered.
    g.update_pnl(-10042, day_key=trading_day_key(datetime(2026, 11, 4, 20, 0)))
    assert g.paused, "a new day must not unblock an account still past the limit"
    assert g._pause_day_key == "2026-11-04"


# ── roll_day: the release without a P&L update (paper mode) ────────────

def test_roll_day_releases_a_pause_carried_into_paper():
    """Auto trips, the bot is switched to paper (no update_pnl there):
    the next trading day must still end the pause, or switching back to
    auto is refused until a redeploy."""
    g = TradingGuard(daily_loss_limit=10000)
    g.update_pnl(-10042, day_key=D4)
    assert g.roll_day(D4) is False and g.paused      # same day: holds
    assert g.roll_day(D5) is True
    assert not g.paused and g._pause_day_key == ""


def test_roll_day_never_releases_a_keyless_pause():
    g = TradingGuard(daily_loss_limit=10000)
    g.update_pnl(-10042)
    assert g.roll_day(D5) is False and g.paused


def test_roll_day_with_empty_key_is_a_no_op():
    g = TradingGuard(daily_loss_limit=10000)
    g.update_pnl(-10042, day_key=D4)
    assert g.roll_day("") is False and g.paused
