"""Tests for TradingGuard — the actual safety logic used by _handle_semi_auto_order.

TradingGuard.decide() is the single decision function that controls whether
_send_real_order is called. These tests verify that decide() returns the
correct verdict for every scenario, ensuring real orders are only sent when
all safety checks pass.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from src.live.live_runner import (
    is_market_open,
    minutes_until_session_close,
    _TZ_TAIPEI,
)
from src.live.trading_guard import TradingGuard


# ── Helper ──

def _taipei_dt(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=_TZ_TAIPEI)


def _patch_now(dt):
    return patch("src.live.live_runner._taipei_now", return_value=dt)


# ── minutes_until_session_close ──

class TestMinutesUntilSessionClose:

    def test_am_session_mid(self):
        with _patch_now(_taipei_dt(2026, 3, 17, 10, 0)):
            assert minutes_until_session_close() == 225

    def test_am_session_near_close(self):
        with _patch_now(_taipei_dt(2026, 3, 17, 13, 43)):
            assert minutes_until_session_close() == 2

    def test_night_session_early(self):
        with _patch_now(_taipei_dt(2026, 3, 17, 15, 30)):
            assert minutes_until_session_close() == 810

    def test_night_session_after_midnight(self):
        with _patch_now(_taipei_dt(2026, 3, 18, 2, 0)):
            assert minutes_until_session_close() == 180

    def test_night_near_close(self):
        with _patch_now(_taipei_dt(2026, 3, 18, 4, 58)):
            assert minutes_until_session_close() == 2

    def test_market_closed_returns_none(self):
        with _patch_now(_taipei_dt(2026, 3, 17, 6, 0)):
            assert minutes_until_session_close() is None

    def test_sunday_returns_none(self):
        with _patch_now(_taipei_dt(2026, 3, 22, 12, 0)):
            assert minutes_until_session_close() is None

    def test_saturday_before_5am(self):
        dt = _taipei_dt(2026, 3, 21, 3, 0)
        with _patch_now(dt):
            assert is_market_open(dt)
            assert minutes_until_session_close() == 120

    def test_between_am_and_pm(self):
        with _patch_now(_taipei_dt(2026, 3, 17, 14, 0)):
            assert minutes_until_session_close() is None


# ── TradingGuard.decide() — the function _handle_semi_auto_order calls ──

class TestDecideEntryBlocked:
    """When daily loss limit is hit, decide() must return BLOCK_ENTRY for entries."""

    def test_entry_blocked_when_paused_semi_auto(self):
        g = TradingGuard(daily_loss_limit=1000)
        g.update_pnl(-1500)
        verdict, details = g.decide("semi_auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_ENTRY
        assert "daily loss limit" in details["reason"]

    def test_entry_blocked_when_paused_auto(self):
        g = TradingGuard(daily_loss_limit=1000)
        g.update_pnl(-2000)
        verdict, _ = g.decide("auto", "ENTRY_FILL", "SHORT")
        assert verdict == g.BLOCK_ENTRY

    def test_exit_not_blocked_when_paused(self):
        """Even when paused, exits go through if real position exists."""
        g = TradingGuard(daily_loss_limit=1000)
        g.on_entry_sent()
        g.update_pnl(-5000)
        assert g.paused is True
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT  # NOT blocked


class TestDecideExitSkipped:
    """When no real entry was confirmed, decide() must return SKIP_EXIT."""

    def test_trade_close_skipped_no_real_entry(self):
        g = TradingGuard()
        verdict, details = g.decide("semi_auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SKIP_EXIT
        assert "no real entry" in details["reason"]

    def test_force_close_skipped_no_real_entry(self):
        g = TradingGuard()
        verdict, _ = g.decide("auto", "FORCE_CLOSE", "SHORT")
        assert verdict == g.SKIP_EXIT

    def test_exit_skipped_after_entry_was_skipped(self):
        g = TradingGuard()
        g.on_entry_skipped()  # user timed out
        verdict, _ = g.decide("semi_auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SKIP_EXIT


class TestDecideExitSent:
    """When real entry was confirmed, decide() must return SEND_EXIT."""

    def test_trade_close_sent_with_real_entry(self):
        g = TradingGuard()
        g.on_entry_sent()
        verdict, details = g.decide("semi_auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT
        assert details["buy_sell"] == 1  # sell to close long
        assert details["new_close"] == 2  # auto (exchange decides)

    def test_force_close_sent_with_real_entry(self):
        g = TradingGuard()
        g.on_entry_sent()
        verdict, details = g.decide("auto", "FORCE_CLOSE", "SHORT")
        assert verdict == g.SEND_EXIT
        assert details["buy_sell"] == 0  # buy to close short
        assert details["new_close"] == 2  # auto

    def test_exit_resets_entry_flag_via_on_exit_sent(self):
        """After SEND_EXIT, calling on_exit_sent() should block next exit."""
        g = TradingGuard()
        g.on_entry_sent()
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT
        g.on_exit_sent()
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SKIP_EXIT


class TestDecideEntrySent:
    """In auto mode, decide() must return SEND_ENTRY for entries."""

    def test_auto_mode_sends_entry(self):
        g = TradingGuard()
        verdict, details = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.SEND_ENTRY
        assert details["buy_sell"] == 0  # buy for long
        assert details["new_close"] == 0  # new position only

    def test_auto_mode_sends_short_entry(self):
        g = TradingGuard()
        verdict, details = g.decide("auto", "ENTRY_FILL", "SHORT")
        assert verdict == g.SEND_ENTRY
        assert details["buy_sell"] == 1  # sell for short


class TestDecideConfirmEntry:
    """In semi-auto mode, decide() must return CONFIRM_ENTRY for entries."""

    def test_semi_auto_shows_dialog(self):
        g = TradingGuard()
        verdict, details = g.decide("semi_auto", "ENTRY_FILL", "LONG")
        assert verdict == g.CONFIRM_ENTRY
        assert details["buy_sell"] == 0
        assert details["new_close"] == 0

    def test_semi_auto_short_entry_shows_dialog(self):
        g = TradingGuard()
        verdict, details = g.decide("semi_auto", "ENTRY_FILL", "SHORT")
        assert verdict == g.CONFIRM_ENTRY
        assert details["buy_sell"] == 1


class TestDecideBuySellDirection:
    """Verify buy/sell direction is correct for all action/side combos."""

    def test_entry_long_buys(self):
        g = TradingGuard()
        _, d = g.decide("auto", "ENTRY_FILL", "LONG")
        assert d["buy_sell"] == 0  # BUY

    def test_entry_short_sells(self):
        g = TradingGuard()
        _, d = g.decide("auto", "ENTRY_FILL", "SHORT")
        assert d["buy_sell"] == 1  # SELL

    def test_close_long_sells(self):
        g = TradingGuard()
        g.on_entry_sent()
        _, d = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert d["buy_sell"] == 1  # SELL to close long

    def test_close_short_buys(self):
        g = TradingGuard()
        g.on_entry_sent()
        _, d = g.decide("auto", "TRADE_CLOSE", "SHORT")
        assert d["buy_sell"] == 0  # BUY to close short


class TestDecideNewClose:
    """Verify sNewClose is never 2 (auto) — always explicit 0 or 1."""

    def test_entry_new_close_zero(self):
        g = TradingGuard()
        _, d = g.decide("auto", "ENTRY_FILL", "LONG")
        assert d["new_close"] == 0  # new position only

    def test_exit_new_close_one(self):
        g = TradingGuard()
        g.on_entry_sent()
        _, d = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert d["new_close"] == 2  # auto

    def test_entry_never_auto_newclose(self):
        """Entry orders should always use new_close=0 (new position only)."""
        g = TradingGuard()
        for mode in ("semi_auto", "auto"):
            _, d = g.decide(mode, "ENTRY_FILL", "LONG")
            assert d["new_close"] == 0, f"{mode}/ENTRY_FILL"

    def test_exit_uses_auto_newclose(self):
        """Exit orders use new_close=2 (auto) to avoid 980 when already flat."""
        g = TradingGuard()
        g.on_entry_sent()
        for action in ("TRADE_CLOSE", "FORCE_CLOSE"):
            _, d = g.decide("auto", action, "LONG")
            assert d["new_close"] == 2, f"auto/{action}"


# ── TradingGuard: margin check ──

class TestGuardMargin:

    def test_sufficient(self):
        allowed, _ = TradingGuard.check_margin(20000, 16100)
        assert allowed is True

    def test_insufficient(self):
        allowed, reason = TradingGuard.check_margin(15000, 16100)
        assert allowed is False
        assert "insufficient margin" in reason

    def test_exact_amount_passes(self):
        allowed, _ = TradingGuard.check_margin(16100, 16100)
        assert allowed is True

    def test_zero_requirement_always_passes(self):
        allowed, _ = TradingGuard.check_margin(0, 0)
        assert allowed is True

    def test_negative_available_blocked(self):
        allowed, _ = TradingGuard.check_margin(-5000, 16100)
        assert allowed is False

    def test_large_margin_tx(self):
        allowed, _ = TradingGuard.check_margin(300000, 322000)
        assert allowed is False

    def test_large_margin_tx_sufficient(self):
        allowed, _ = TradingGuard.check_margin(350000, 322000)
        assert allowed is True


# ── TradingGuard: daily loss limit via update_pnl ──

class TestGuardDailyLoss:

    def test_within_limit_not_paused(self):
        g = TradingGuard(daily_loss_limit=1000)
        triggered = g.update_pnl(-500)
        assert triggered is False
        assert g.paused is False

    def test_exceeds_limit_pauses(self):
        g = TradingGuard(daily_loss_limit=1000)
        triggered = g.update_pnl(-1001)
        assert triggered is True
        assert g.paused is True

    def test_at_limit_does_not_pause(self):
        g = TradingGuard(daily_loss_limit=1000)
        triggered = g.update_pnl(-1000)
        assert triggered is False
        assert g.paused is False

    def test_trigger_fires_once(self):
        g = TradingGuard(daily_loss_limit=1000)
        assert g.update_pnl(-1500) is True
        assert g.update_pnl(-2000) is False
        assert g.update_pnl(-3000) is False

    def test_zero_limit_never_pauses(self):
        g = TradingGuard(daily_loss_limit=0)
        g.update_pnl(-99999)
        assert g.paused is False

    def test_reset_clears_pause(self):
        g = TradingGuard(daily_loss_limit=1000)
        g.update_pnl(-2000)
        assert g.paused is True
        g.reset()
        assert g.paused is False


# ── Integration: full scenario replays using decide() ──

class TestScenarioIssue17:
    """Replay the exact issue #17 sequence through decide() to verify
    the guard prevents unintended orders."""

    def test_skipped_entry_then_exit_blocked(self):
        """The exact #17 bug: entry skipped → exit should NOT send."""
        g = TradingGuard()

        # 16:13 — user confirms entry
        g.on_entry_sent()

        # 16:13 — strategy closes, exit sent
        verdict, _ = g.decide("semi_auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT
        g.on_exit_sent()

        # 21:42 — new entry, user times out
        g.on_entry_skipped()

        # 21:42 — SL1 fires TRADE_CLOSE
        verdict, details = g.decide("semi_auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SKIP_EXIT
        # ↑ Without the guard, _send_real_order would have been called here,
        #   sending a SELL with sNewClose=2 that opened an unintended short!

    def test_repeated_exits_all_blocked(self):
        """After #17 bug, repeated TRADE_CLOSEs kept firing. All must be blocked."""
        g = TradingGuard()
        for _ in range(15):
            verdict, _ = g.decide("semi_auto", "TRADE_CLOSE", "LONG")
            assert verdict == g.SKIP_EXIT

    def test_issue_17_with_auto_mode(self):
        """Same scenario in auto mode — still must block exit."""
        g = TradingGuard()
        g.on_entry_sent()
        g.on_exit_sent()
        # Entry auto-sent but exchange rejects (simulated by not calling on_entry_sent again)
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SKIP_EXIT


class TestScenarioAutoFullCycle:
    """Auto mode full trade cycle."""

    def test_entry_exit_clean(self):
        g = TradingGuard()

        verdict, d = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.SEND_ENTRY
        assert d["buy_sell"] == 0
        assert d["new_close"] == 0
        g.on_entry_sent()

        verdict, d = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT
        assert d["buy_sell"] == 1
        assert d["new_close"] == 2  # auto
        g.on_exit_sent()

        assert g.real_entry_confirmed is False


class TestScenarioLossLimitDuringTrade:
    """Loss limit hit while holding a real position."""

    def test_exit_still_works_when_paused(self):
        g = TradingGuard(daily_loss_limit=1000)
        g.on_entry_sent()
        g.update_pnl(-5000)  # massive loss
        assert g.paused is True

        # Exit must still work — can't leave position hanging
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT

        # But next entry is blocked
        g.on_exit_sent()
        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_ENTRY

    def test_force_close_works_when_paused(self):
        g = TradingGuard(daily_loss_limit=1000)
        g.on_entry_sent()
        g.update_pnl(-9999)

        verdict, _ = g.decide("auto", "FORCE_CLOSE", "SHORT")
        assert verdict == g.SEND_EXIT


# ── Fill confirmation gate ──

class TestFillPendingBlocks:
    """While fill_pending is True, all orders must be blocked."""

    def test_fill_pending_blocks_entry(self):
        g = TradingGuard()
        g.on_fill_pending("entry")
        verdict, details = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_FILL_PENDING
        assert "waiting for entry fill" in details["reason"]

    def test_fill_pending_blocks_exit(self):
        g = TradingGuard()
        g.on_entry_sent()
        g.on_fill_pending("exit")
        verdict, details = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.BLOCK_FILL_PENDING
        assert "waiting for exit fill" in details["reason"]

    def test_fill_pending_blocks_semi_auto_too(self):
        """fill_pending is mode-agnostic — blocks semi_auto if ever set."""
        g = TradingGuard()
        g.on_fill_pending("entry")
        verdict, _ = g.decide("semi_auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_FILL_PENDING

    def test_force_close_bypasses_fill_pending(self):
        """FORCE_CLOSE is an emergency exit — must not be blocked by fill gate."""
        g = TradingGuard()
        g.on_entry_sent()
        g.on_fill_pending("exit")
        verdict, _ = g.decide("auto", "FORCE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT  # NOT blocked

    def test_force_close_bypasses_halted(self):
        """FORCE_CLOSE bypasses halted state too."""
        g = TradingGuard()
        g.on_entry_sent()
        g.on_fill_pending("exit")
        g.on_fill_timeout()
        verdict, _ = g.decide("auto", "FORCE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT  # NOT blocked


class TestFillConfirmedResumes:
    """After on_fill_confirmed(), orders should flow normally again."""

    def test_confirmed_resumes_entry(self):
        g = TradingGuard()
        g.on_fill_pending("entry")
        assert g.fill_pending is True
        g.on_fill_confirmed()
        assert g.fill_pending is False
        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.SEND_ENTRY

    def test_confirmed_resumes_exit(self):
        g = TradingGuard()
        g.on_entry_sent()
        g.on_fill_pending("exit")
        g.on_fill_confirmed()
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT

    def test_deferred_state_transition_entry(self):
        """Simulate the full auto flow: pending → confirmed → on_entry_sent."""
        g = TradingGuard()
        # Order sent, enter pending
        g.on_fill_pending("entry")
        assert g.real_entry_confirmed is False  # NOT set yet
        # Fill confirmed
        g.on_fill_confirmed()
        g.on_entry_sent()  # NOW set
        assert g.real_entry_confirmed is True
        # Exit should now work
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT

    def test_deferred_state_transition_exit(self):
        """Simulate: entry confirmed → exit sent → pending → confirmed → on_exit_sent."""
        g = TradingGuard()
        g.on_entry_sent()
        assert g.real_entry_confirmed is True
        # Exit order sent, enter pending
        g.on_fill_pending("exit")
        assert g.real_entry_confirmed is True  # still True during pending
        # Fill confirmed
        g.on_fill_confirmed()
        g.on_exit_sent()  # NOW cleared
        assert g.real_entry_confirmed is False


class TestHaltedBlocks:
    """After fill timeout, halted=True blocks everything permanently."""

    def test_halted_blocks_entry(self):
        g = TradingGuard()
        g.on_fill_pending("entry")
        g.on_fill_timeout()
        verdict, details = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_HALTED
        assert "system halted" in details["reason"]

    def test_halted_blocks_exit(self):
        g = TradingGuard()
        g.on_entry_sent()
        g.on_fill_pending("exit")
        g.on_fill_timeout()
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.BLOCK_HALTED

    def test_halted_takes_priority_over_paused(self):
        """Halted overrides daily loss limit (stronger block)."""
        g = TradingGuard(daily_loss_limit=1000)
        g.update_pnl(-5000)
        g.on_fill_pending("entry")
        g.on_fill_timeout()
        verdict, details = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_HALTED  # not BLOCK_ENTRY

    def test_halted_survives_pnl_update(self):
        g = TradingGuard()
        g.on_fill_pending("entry")
        g.on_fill_timeout()
        g.update_pnl(9999)  # big profit shouldn't clear halt
        assert g.halted is True

    def test_clear_halt_resumes(self):
        g = TradingGuard()
        g.on_fill_pending("entry")
        g.on_fill_timeout()
        assert g.halted is True
        g.clear_halt()
        assert g.halted is False
        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.SEND_ENTRY

    def test_fill_pending_cleared_on_timeout(self):
        """on_fill_timeout clears fill_pending (only halted remains)."""
        g = TradingGuard()
        g.on_fill_pending("entry")
        assert g.fill_pending is True
        g.on_fill_timeout()
        assert g.fill_pending is False
        assert g.halted is True


class TestResetClearsAll:
    """reset() must clear fill_pending and halted."""

    def test_reset_clears_fill_pending(self):
        g = TradingGuard()
        g.on_fill_pending("entry")
        g.reset()
        assert g.fill_pending is False
        assert g.fill_pending_type == ""

    def test_reset_clears_halted(self):
        g = TradingGuard()
        g.on_fill_pending("entry")
        g.on_fill_timeout()
        g.reset()
        assert g.halted is False
        assert g.halt_reason == ""


class TestScenarioAutoFillGating:
    """Full auto mode scenarios with fill gating."""

    def test_entry_pending_then_exit_blocked(self):
        """Entry sent but not filled → sim fires exit → must be blocked."""
        g = TradingGuard()
        # Auto mode sends entry, enters pending
        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.SEND_ENTRY
        g.on_fill_pending("entry")
        # Sim fires exit while entry pending
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.BLOCK_FILL_PENDING

    def test_entry_confirmed_then_exit_allowed(self):
        """Full cycle: entry → pending → confirmed → exit works."""
        g = TradingGuard()
        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.SEND_ENTRY
        g.on_fill_pending("entry")
        g.on_fill_confirmed()
        g.on_entry_sent()
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT

    def test_timeout_blocks_everything(self):
        """Entry timeout → both entry and exit blocked."""
        g = TradingGuard()
        g.on_fill_pending("entry")
        g.on_fill_timeout()
        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_HALTED
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.BLOCK_HALTED


# ── Regression: issue #50 — deferred close after BLOCK_FILL_PENDING ──

class TestDeferredCloseIssue50:
    """Regression tests for the deferred close mechanism (issue #50).

    When a TRADE_CLOSE is blocked by BLOCK_FILL_PENDING (entry fill
    not yet confirmed), the decision is stored via defer_close(). When
    _on_fill_confirmed("entry") fires, the caller pops and replays it
    so the real exit order isn't permanently lost.

    Without this fix, rapid-fire historical bar replay could enter a
    trade AND close it within the same ~1-second burst. The close
    gets blocked because the entry fill confirmation hasn't arrived
    yet (takes ~3s via OpenInterest polling), and without the deferred
    replay mechanism the real position is left open forever.
    """

    def test_defer_and_pop(self):
        g = TradingGuard()
        decision = {"action": "TRADE_CLOSE", "side": "LONG", "price": 35222}
        g.defer_close(decision)
        assert g.pop_deferred_close() == decision

    def test_pop_clears_storage(self):
        g = TradingGuard()
        g.defer_close({"action": "TRADE_CLOSE"})
        g.pop_deferred_close()
        assert g.pop_deferred_close() is None

    def test_pop_returns_none_when_empty(self):
        g = TradingGuard()
        assert g.pop_deferred_close() is None

    def test_reset_clears_deferred(self):
        g = TradingGuard()
        g.defer_close({"action": "TRADE_CLOSE"})
        g.reset()
        assert g.pop_deferred_close() is None

    def test_timeout_clears_deferred(self):
        """Fill timeout → system halts, deferred close discarded."""
        g = TradingGuard()
        g.on_fill_pending("entry")
        g.defer_close({"action": "TRADE_CLOSE"})
        g.on_fill_timeout()
        assert g.pop_deferred_close() is None

    def test_deferred_close_replayed_after_entry_confirm(self):
        """Full flow: entry pending → close blocked & deferred → entry
        confirmed → deferred close popped → decide() allows exit."""
        g = TradingGuard()
        g.on_fill_pending("entry")

        # TRADE_CLOSE is blocked
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.BLOCK_FILL_PENDING

        # Store the blocked decision
        decision = {"action": "TRADE_CLOSE", "side": "LONG", "price": 35222}
        g.defer_close(decision)

        # Entry fill confirmed — clears fill_pending + sets real_entry_confirmed
        g.on_fill_confirmed()
        g.on_entry_sent()

        # Pop and verify the deferred close is ready to replay
        deferred = g.pop_deferred_close()
        assert deferred is not None
        assert deferred["action"] == "TRADE_CLOSE"

        # Now the replayed close should pass decide()
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT

    def test_entry_fill_not_deferred(self):
        """ENTRY_FILL blocked by fill_pending should NOT be stored."""
        g = TradingGuard()
        g.on_fill_pending("exit")

        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_FILL_PENDING
        # Entry decisions are not deferred — only closes
        assert g.pop_deferred_close() is None

    def test_force_close_bypasses_fill_pending(self):
        """FORCE_CLOSE bypasses the gate entirely, never needs deferral."""
        g = TradingGuard()
        g.on_entry_sent()
        g.on_fill_pending("exit")

        verdict, _ = g.decide("auto", "FORCE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT  # not blocked
        assert g.pop_deferred_close() is None  # nothing deferred

    def test_second_defer_overwrites_first(self):
        """If two closes are deferred (shouldn't happen normally), last wins."""
        g = TradingGuard()
        g.defer_close({"action": "TRADE_CLOSE", "tag": "first"})
        g.defer_close({"action": "TRADE_CLOSE", "tag": "second"})
        d = g.pop_deferred_close()
        assert d["tag"] == "second"


# ── Session-end pending flag ──

class TestSessionEndPending:
    """Session-end pending blocks new entries but allows exits."""

    def test_blocks_entry_when_set(self):
        g = TradingGuard()
        g.session_end_pending = True
        verdict, details = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_SESSION_END
        assert "session end" in details["reason"]

    def test_blocks_entry_semi_auto(self):
        g = TradingGuard()
        g.session_end_pending = True
        verdict, _ = g.decide("semi_auto", "ENTRY_FILL", "SHORT")
        assert verdict == g.BLOCK_SESSION_END

    def test_allows_exit_when_set(self):
        """Exits must still work when session end is approaching."""
        g = TradingGuard()
        g.on_entry_sent()
        g.session_end_pending = True
        verdict, _ = g.decide("auto", "TRADE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT

    def test_allows_force_close_when_set(self):
        """FORCE_CLOSE bypasses session_end_pending."""
        g = TradingGuard()
        g.on_entry_sent()
        g.session_end_pending = True
        verdict, _ = g.decide("auto", "FORCE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT

    def test_entry_allowed_when_not_set(self):
        g = TradingGuard()
        g.session_end_pending = False
        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.SEND_ENTRY

    def test_reset_clears_session_end_pending(self):
        g = TradingGuard()
        g.session_end_pending = True
        g.reset()
        assert g.session_end_pending is False
        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.SEND_ENTRY

    def test_session_end_takes_priority_over_loss_limit(self):
        """Session-end check runs before daily loss limit check."""
        g = TradingGuard(daily_loss_limit=1000)
        g.session_end_pending = True
        g.update_pnl(-5000)
        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_SESSION_END  # not BLOCK_ENTRY

    def test_halted_takes_priority_over_session_end(self):
        """Halted state is checked before session_end_pending."""
        g = TradingGuard()
        g.on_fill_pending("entry")
        g.on_fill_timeout()
        g.session_end_pending = True
        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_HALTED  # not BLOCK_SESSION_END

    def test_fill_pending_takes_priority_over_session_end(self):
        """fill_pending is checked before session_end_pending."""
        g = TradingGuard()
        g.on_fill_pending("entry")
        g.session_end_pending = True
        verdict, _ = g.decide("auto", "ENTRY_FILL", "LONG")
        assert verdict == g.BLOCK_FILL_PENDING


# ── Deferred close cleared by force-close ──

class TestDeferredCloseAndForceClose:
    """Force-close should clear deferred close to prevent double-fire."""

    def test_deferred_close_cleared_by_session_manager(self):
        """When force-close starts, deferred close should be popped and discarded."""
        g = TradingGuard()
        g.defer_close({"action": "TRADE_CLOSE", "side": "LONG", "price": 22000})
        # Simulate force-close clearing the deferred close
        d = g.pop_deferred_close()
        assert d is not None
        # Now it's gone
        assert g.pop_deferred_close() is None

    def test_force_close_while_deferred_pending(self):
        """FORCE_CLOSE bypasses all gates, even with deferred close stored."""
        g = TradingGuard()
        g.on_entry_sent()
        g.on_fill_pending("exit")
        g.defer_close({"action": "TRADE_CLOSE"})
        # Force close bypasses fill_pending
        verdict, _ = g.decide("auto", "FORCE_CLOSE", "LONG")
        assert verdict == g.SEND_EXIT
