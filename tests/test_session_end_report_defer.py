"""Regression tests for the session-end REAL-EXIT-PRICE persistence race (issue #92).

The bug (recurrence, v2.17.15): at session end the force-close's REAL exit
fill arrives asynchronously — a ``SKReplyLib.OnNewData`` deal row lands ~1-6s
AFTER the sim position closes. But ``_check_session_end_report`` generates the
daily report (and its ``_auto_save_session``) in the SAME 30s poll that triggers
the force-close, BEFORE that write. The report snapshots ``real_exit_price`` and
its ``{date}_{session}`` debounce blocks any later correction, so both the daily
report JSON and ``session.json`` freeze ``real_exit_price=0`` while Discord's
independent fill-confirm timer shows the correct price — exactly the
"交易頻道正確 / 每日報告與交易頁籤錯誤" mismatch reported.

Proven incident (SHORT TM0000, 2026-08-07 13:43, real money):
    session.json saved_at 13:43:16  -> real_exit_price = 0 persisted
    daily report generated 13:43:17 -> real_exit_price = 0 snapshotted
    REAL FILL (exit) 13:43:17.921   -> 44,286 written, accepted=True  (too late)
    Discord "EXIT fill confirmed @44,286" 13:43:23 (correct)

The real ENTRY price survives because the position is open for hours and later
saves catch it; the exit is the session's last event, so nothing re-persists it.

The fix has two seams, both pinned here without a Tkinter GUI:
  1. ``should_defer_session_end_report`` — hold the report while the force-close's
     real exit fill is in flight; generate once it is recorded, or at the final
     minute before close (never lose the report).
  2. the guarded ``try_set_real_exit_price`` write + broker serialization — the
     late write is accepted and a re-save then persists the real price
     (mirrors the belt-and-braces re-save in ``_on_new_data``).
"""

from src.backtest.broker import OrderSide, SimulatedBroker, Trade
from src.live.fill_report import RealFillTracker
from src.live.live_runner import should_defer_session_end_report


# ── Seam 1: the pure defer decision ──

# ``_SESSION_END_REPORT_MIN_MINUTES`` in run_backtest.py; kept local so the
# test states the fallback boundary it asserts against.
MIN_MINUTES = 1


class TestShouldDeferSessionEndReport:
    def test_incident_defers_while_real_exit_in_flight(self):
        # The 13:43:16 poll: force-close done, real fill NOT yet recorded,
        # 2 min to close. The known-bad code generated the report here and
        # froze real_exit_price=0. The fix holds it.
        assert should_defer_session_end_report(
            exit_pending=True, real_exit_recorded=False,
            minutes_to_close=2, min_minutes_before_close=MIN_MINUTES) is True

    def test_generates_once_real_exit_recorded(self):
        # The next poll after the fill landed: real price is in, so proceed.
        assert should_defer_session_end_report(
            exit_pending=True, real_exit_recorded=True,
            minutes_to_close=2, min_minutes_before_close=MIN_MINUTES) is False

    def test_final_minute_fallback_never_loses_report(self):
        # A genuinely stuck fill: at the final minute no further poll is
        # guaranteed (after AM close minutes_until_session_close -> None), so
        # generate anyway — degrade to the sim price rather than lose the
        # report entirely.
        assert should_defer_session_end_report(
            exit_pending=True, real_exit_recorded=False,
            minutes_to_close=1, min_minutes_before_close=MIN_MINUTES) is False

    def test_no_force_close_never_defers(self):
        # Flat at session end (closed early) or paper mode: no exit fill is
        # pending, so the report generates immediately as before.
        assert should_defer_session_end_report(
            exit_pending=False, real_exit_recorded=False,
            minutes_to_close=2, min_minutes_before_close=MIN_MINUTES) is False

    def test_settlement_window_defers_with_headroom(self):
        # Settlement day fires the report up to 5 min before the early close;
        # a pending exit fill defers across that wider window too.
        assert should_defer_session_end_report(
            exit_pending=True, real_exit_recorded=False,
            minutes_to_close=5, min_minutes_before_close=MIN_MINUTES) is True

    def test_recorded_short_circuits_before_fallback(self):
        # Once recorded, deep in the window is irrelevant — generate.
        assert should_defer_session_end_report(
            exit_pending=True, real_exit_recorded=True,
            minutes_to_close=5, min_minutes_before_close=MIN_MINUTES) is False


# ── Seam 2: late write is accepted and a re-save persists the real price ──

def _incident_trade(exit_bar_index: int = 12233) -> Trade:
    """The 2026-08-07 force-close trade at the instant the session-end report
    snapshots it: closed, source 'real', entry real-price already confirmed
    (long open window), but the exit real-price NOT yet recorded."""
    return Trade(
        tag="Short", side=OrderSide.SHORT, qty=1,
        entry_price=44264, exit_price=44282,
        entry_bar_index=12229, exit_bar_index=exit_bar_index,
        pnl=-180, exit_tag="force_close",
        entry_dt="2026-08-07 09:46:00", exit_dt="2026-08-07 13:43:16",
        real_entry_price=44310, real_entry_dt="2026-08-07T09:46:00+08:00",
        real_exit_price=0, real_exit_dt="", source="real",
        strategy="AI: BbandSmaShortV3",
    )


def _deal_row(seq: str, price: str) -> str:
    """A type-D OnNewData row (empty field[0], seq at field[-1])."""
    f = [""] * 48
    f[1] = "TF"
    f[2] = "D"
    f[3] = "N"
    f[8] = "TM0000"
    f[10] = "j0224"
    f[11] = price
    f[20] = "1"
    f[23] = "20260807"
    f[24] = "13:43:17"
    f[-1] = seq
    return ",".join(f)


class TestIncidentPersistence:
    def test_report_held_then_generated_with_real_price(self):
        broker = SimulatedBroker(point_value=10)
        broker.trades.append(_incident_trade())
        last = broker.trades[-1]

        # (a) Report-snapshot moment: real exit not recorded -> the fix HOLDS
        #     the report. Under the known-bad code it generated here.
        assert should_defer_session_end_report(
            exit_pending=True,
            real_exit_recorded=(last.real_exit_price != 0),
            minutes_to_close=2, min_minutes_before_close=MIN_MINUTES) is True
        # A save taken now would persist the stale 0 -> the reported bug.
        assert broker.to_dict()["trades"][-1]["real_exit_price"] == 0

        # (b) The late OnNewData deal row lands and the guarded write accepts.
        tracker = RealFillTracker()
        tracker.register_order("2315607669597", "exit",
                               bar_index=12233, sim_price=44282)
        fill = tracker.on_new_data(
            _deal_row(seq="2315607669597", price="44286.000000"))
        assert fill is not None and fill.price == 44286
        assert broker.try_set_real_exit_price(
            fill.price, exit_bar_index=fill.bar_index,
            fill_dt="2026-08-07 13:43:17")
        assert last.real_exit_price == 44286

        # (c) Now the fill is recorded -> the next poll generates the report.
        assert should_defer_session_end_report(
            exit_pending=True,
            real_exit_recorded=(last.real_exit_price != 0),
            minutes_to_close=2, min_minutes_before_close=MIN_MINUTES) is False

        # (d) The re-save (belt-and-braces in _on_new_data, and the deferred
        #     report's own save) persists the real price through a round-trip
        #     — this is what the Trades tab reads on reload.
        restored = SimulatedBroker.from_dict(broker.to_dict())
        assert restored.trades[-1].real_exit_price == 44286

    def test_stuck_fill_reaches_fallback_but_late_write_still_persists(self):
        # If the fill never arrives before the final-minute fallback, the
        # report generates with the sim price (report unavoidably stale), but
        # a still-later OnNewData write is accepted and a re-save persists the
        # real price to session.json / the Trades tab.
        broker = SimulatedBroker(point_value=10)
        broker.trades.append(_incident_trade())
        # Fallback fired (mins == 1): decision is "generate", not "defer".
        assert should_defer_session_end_report(
            exit_pending=True, real_exit_recorded=False,
            minutes_to_close=1, min_minutes_before_close=MIN_MINUTES) is False
        # ...then the real fill lands; the guarded write still lands it.
        assert broker.try_set_real_exit_price(
            44286, exit_bar_index=12233, fill_dt="2026-08-07 13:44:30")
        assert SimulatedBroker.from_dict(
            broker.to_dict()).trades[-1].real_exit_price == 44286
