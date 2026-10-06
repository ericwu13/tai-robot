"""Regression tests for the three reconnect bugs found in bot 0422.

These are source-level pins for invariants that don't lend themselves to
runtime testing — exercising them properly requires Tkinter, comtypes,
and a live SKCOM session. The pins are tight enough to detect regressions
without re-running real reconnect flows.

Bugs covered:

* **Bug A** — premature "connected" on Quote (3002). ``_on_reconnected``
  must only fire on Ready (3003), and a Quote (3002) without a follow-up
  Ready must schedule a watchdog timer.

* **Bug B** — no COM teardown before re-login. ``_attempt_reconnect``
  must call ``SKQuoteLib_LeaveMonitor`` AND ``SKCenterLib_LogOut`` before
  the fresh ``LoginSetQuote``.

* **Bug C** — watchdog "warn" handler bypasses the resubscribe/reconnect
  ladder. The handler must require TWO consecutive bad ``IsConnected()``
  readings before forcing a disconnect, tracked via
  ``_warn_disconnect_count``.

Also pins the logging-tag prefixes the next debugging session is expected
to rely on (``[CONN]``, ``[RECONNECT]``, ``[TICKS]``, ``[WATCHDOG]``,
``[STATE]``).
"""

from __future__ import annotations

import os
import re

import pytest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RB_PATH = os.path.join(PROJECT_ROOT, "run_backtest.py")


@pytest.fixture(scope="module")
def rb_source() -> str:
    with open(RB_PATH, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Bug A — Quote (3002) is intermediate; only Ready (3003) marks connected
# ---------------------------------------------------------------------------

class TestBugA_QuoteIsIntermediate:

    def test_quote_branch_does_not_call_on_reconnected(self, rb_source: str):
        """Bug A: the `data == "quote"` branch in `_drain_ui_queue` must
        not call `_on_reconnected()` and must not set `_quote_connected = True`.

        The pre-bug code did both, which fired `_resubscribe_ticks()` on a
        half-connected session that produced zombie subscriptions.
        """
        # Find the "quote" branch — everything between `elif data == "quote"`
        # and the next `elif data ==` or end of the surrounding block.
        match = re.search(
            r'elif data == "quote".*?(?=\n\s{20}elif data ==|\n\s{16}elif kind ==)',
            rb_source,
            re.DOTALL,
        )
        assert match, "could not locate `elif data == \"quote\"` branch"
        branch = match.group(0)
        assert "_on_reconnected" not in branch, (
            "Bug A regression: Quote (3002) branch still calls "
            "_on_reconnected; only Ready (3003) should fire it.")
        assert "_quote_connected = True" not in branch, (
            "Bug A regression: Quote (3002) branch sets "
            "_quote_connected = True; only Ready (3003) should.")

    def test_quote_branch_schedules_ready_timeout(self, rb_source: str):
        """The Quote branch must hand off to a method that arms a
        Ready (3003) watchdog timer."""
        assert "_on_quote_intermediate" in rb_source, (
            "Bug A: Quote (3002) branch should delegate to "
            "_on_quote_intermediate which schedules a Ready timeout")

    def test_quote_ready_timeout_handler_exists(self, rb_source: str):
        assert "_on_quote_ready_timeout" in rb_source, (
            "Bug A: missing _on_quote_ready_timeout handler — without "
            "this a half-connected session would sit silently forever.")

    def test_quote_intermediate_arms_15s_timer(self, rb_source: str):
        """The Quote→Ready timer should fire after ~15s."""
        match = re.search(
            r"def _on_quote_intermediate\(self\).*?self\.root\.after\(\s*(\d+)",
            rb_source,
            re.DOTALL,
        )
        assert match, "could not find timer arm in _on_quote_intermediate"
        delay_ms = int(match.group(1))
        assert 5000 <= delay_ms <= 30000, (
            f"Bug A: Ready timeout {delay_ms}ms is outside the "
            "5-30s window from the issue spec.")


# ---------------------------------------------------------------------------
# Bug B — clean COM state before re-login
# ---------------------------------------------------------------------------

class TestBugB_CleanupBeforeRelogin:

    def _attempt_reconnect_body(self, rb_source: str) -> str:
        match = re.search(
            r"def _attempt_reconnect\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert match, "could not locate _attempt_reconnect body"
        return match.group(1)

    def test_leave_monitor_called_before_login(self, rb_source: str):
        body = self._attempt_reconnect_body(rb_source)
        # Call sites only — the docstring also names these functions.
        leave_pos = body.find("skQ.SKQuoteLib_LeaveMonitor()")
        login_pos = body.find("skC.SKCenterLib_LoginSetQuote(")
        assert leave_pos != -1, (
            "Bug B: _attempt_reconnect must call SKQuoteLib_LeaveMonitor "
            "to tear down the prior COM session before re-login.")
        assert login_pos != -1, "LoginSetQuote unexpectedly removed"
        assert leave_pos < login_pos, (
            "Bug B: LeaveMonitor must happen BEFORE LoginSetQuote.")

    def test_logout_called_before_login(self, rb_source: str):
        """Inverted for #157.

        The old pin required an unconditional ``skC.SKCenterLib_LogOut(``
        before LoginSetQuote. Capital API 2.13.57 has no such method, so
        an unguarded call always raises AttributeError. LogOut is allowed
        only inside ``hasattr``, and removing the call entirely is also
        fine. An unconditional call site fails this test.
        """
        body = self._attempt_reconnect_body(rb_source)
        guard = body.find('hasattr(skC, "SKCenterLib_LogOut")')
        call = body.find("skC.SKCenterLib_LogOut(")
        login = body.find("skC.SKCenterLib_LoginSetQuote(")
        assert login != -1, "LoginSetQuote unexpectedly removed"
        if call == -1:
            assert "SKCenterLib_LogOut(" not in body
            return
        assert guard != -1 and guard < call < login, (
            "Issue #157: SKCenterLib_LogOut( must not run unless hasattr "
            "says the COM object actually has it.")

    def test_cleanup_calls_swallow_errors(self, rb_source: str):
        """Cleanup calls must be best-effort — a prior session may already
        be half-torn-down."""
        body = self._attempt_reconnect_body(rb_source)
        # Each cleanup call should be inside its own try/except
        for fn in ("SKQuoteLib_LeaveMonitor", "SKCenterLib_LogOut"):
            # Match `try:` ... fn ... `except` within 5 lines
            pat = rf"try:\s*\n[^\n]*{fn}[^\n]*\n(?:[^\n]*\n){{0,5}}\s*except"
            assert re.search(pat, body), (
                f"Bug B: {fn} must be wrapped in try/except so a partial "
                "tear-down does not abort the reconnect attempt.")


class TestIssue157_GuardBeforeLeaveMonitor:
    """Issue #157: LeaveMonitor on a half-up pre-session session AVs.

    The guard decision must run, and the skip branch must return, before
    ``SKQuoteLib_LeaveMonitor``. Bug B still requires the teardown call
    on the path that does log in.
    """

    def _attempt_reconnect_body(self, rb_source: str) -> str:
        match = re.search(
            r"def _attempt_reconnect\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert match, "could not locate _attempt_reconnect body"
        return match.group(1)

    def test_decide_runs_before_leave_monitor(self, rb_source: str):
        body = self._attempt_reconnect_body(rb_source)
        decide_pos = body.find("_reconnect_controller.decide(")
        leave_pos = body.find("skQ.SKQuoteLib_LeaveMonitor()")
        assert decide_pos != -1, (
            "Issue #157: _attempt_reconnect must consult ReconnectController "
            "before any COM teardown.")
        assert leave_pos != -1
        assert decide_pos < leave_pos, (
            "Issue #157: decide() must run BEFORE SKQuoteLib_LeaveMonitor.")
        assert "seconds_since_open=" in body[decide_pos:leave_pos], (
            "Issue #157: the guard must see seconds since the session "
            "open. A half-up handshake skips until open+90s even when "
            "the market is already open.")

    def test_skip_branch_returns_before_leave_monitor(self, rb_source: str):
        body = self._attempt_reconnect_body(rb_source)
        skip_pos = body.find("ACTION_SKIP_TEARDOWN_WAIT")
        leave_pos = body.find("skQ.SKQuoteLib_LeaveMonitor()")
        assert skip_pos != -1, "skip action not handled in _attempt_reconnect"
        assert skip_pos < leave_pos
        between = body[skip_pos:leave_pos]
        assert "return" in between, (
            "Issue #157: the skip branch must return before LeaveMonitor. "
            "Falling through calls the COM teardown the guard just refused.")

    def test_check_reconnection_also_skips(self, rb_source: str):
        """The Ready poll is the path that used to schedule attempt #2."""
        match = re.search(
            r"def _check_reconnection\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert match, "could not locate _check_reconnection body"
        body = match.group(1)
        skip_pos = body.find("ACTION_SKIP_TEARDOWN_WAIT")
        schedule_pos = body.find("self._schedule_reconnect()")
        assert skip_pos != -1, (
            "Issue #157: _check_reconnection must honour the skip decision "
            "instead of always scheduling another teardown attempt.")
        assert schedule_pos != -1
        assert skip_pos < schedule_pos
        assert "return" in body[skip_pos:schedule_pos]

    def test_ready_wait_is_the_short_poll(self, rb_source: str):
        """Open+90s is the skip loop, not a long Ready timer.

        ``secs_until_open + 90`` was not used. A holiday gap can last
        days, and one timer would not re-check IsConnected()==2.
        """
        body = self._attempt_reconnect_body(rb_source)
        assert "ready_wait_seconds()" in body
        assert "seconds_since_open=" not in body[
            body.find("ready_wait_seconds()"):body.find("ready_wait_seconds()") + 80
        ]
        assert "root.after(3000, self._check_reconnection)" not in body

    def test_schedule_uses_holiday_aware_session(self, rb_source: str):
        match = re.search(
            r"def _schedule_reconnect\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert match
        body = match.group(1)
        assert "in_live_session(" in body
        assert "seconds_until_next_session_open(" in body
        assert "is_market_open(" not in body
        assert "seconds_until_market_open(" not in body
        attempt = self._attempt_reconnect_body(rb_source)
        assert "is_market_open(" not in attempt
        check = re.search(
            r"def _check_reconnection\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert check and "is_market_open(" not in check.group(1)


class TestIssue157_Amendments:
    """LogOut guard, faulthandler, broker-call log, single timer."""

    def test_reconnect_calls_are_logged_with_thread_id(self, rb_source: str):
        assert (
            '[RECONNECT] about to call {name} tid={threading.get_ident()}'
            in rb_source
        ), "missing [RECONNECT] about to call <name> tid=<thread id> helper"
        match = re.search(
            r"def _attempt_reconnect\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert match
        body = match.group(1)
        for name in (
            "IsConnected",
            "LeaveMonitor",
            "LoginSetQuote",
            "ConnectByID",
            "EnterMonitorLONG",
        ):
            assert f'_log_reconnect_call("{name}")' in body, name

    def test_forced_teardown_log_line(self, rb_source: str):
        ctrl_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "live", "reconnect_controller.py",
        )
        ctrl_source = open(ctrl_path, encoding="utf-8").read()
        assert "P1 forced teardown of half-up session" in ctrl_source
        assert "_log_debug(FORCED_TEARDOWN_LOG)" in rb_source
        assert "note_forced_teardown()" in rb_source

    def test_faulthandler_armed_at_deploy(self, rb_source: str):
        assert "def enable_reconnect_faulthandler(" in rb_source
        assert "faulthandler.enable(" in rb_source
        assert "_faulthandler_log" in rb_source
        assert 'faulthandler.log' in rb_source
        # Deploy must arm it once the bot directory exists.
        deploy = re.search(
            r"def _deploy_live_continue\(.*?(?=\n    def )",
            rb_source,
            re.DOTALL,
        )
        assert deploy, "could not locate _deploy_live_continue"
        body = deploy.group(0)
        assert "enable_reconnect_faulthandler(" in body
        assert body.find("_open_debug_log(") < body.find(
            "enable_reconnect_faulthandler(")

    def test_disconnect_cancels_then_arms_one_timer(self, rb_source: str):
        disc = re.search(
            r"def _on_disconnected\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert disc
        assert "_cancel_reconnect_timer()" in disc.group(1)
        action = re.search(
            r"def _execute_reconnect_action\(self, action\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert action
        assert "_arm_reconnect_callback(" in action.group(1)
        assert "cancel_all(" in rb_source
        stop = re.search(
            r"def _stop_live\(self\):(.*?)(?=\ndef |\Z)",
            rb_source,
            re.DOTALL,
        )
        manual = re.search(
            r"def _manual_reconnect\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert stop and "_cancel_reconnect_timer()" in stop.group(1)
        assert manual and "_cancel_reconnect_timer()" in manual.group(1)
        # Ready-wait and skip reschedule use the same slot as reconnect.
        attempt = re.search(
            r"def _attempt_reconnect\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert attempt
        assert '_arm_reconnect_callback(\n                    "ready"' in attempt.group(1) \
            or '_arm_reconnect_callback(\n                "ready"' in attempt.group(1) \
            or '"ready"' in attempt.group(1)
        assert '"skip"' in attempt.group(1)


def test_faulthandler_handle_stays_open(tmp_path):
    """The dump file handle must outlive enable(); closing it drops the hook."""
    import faulthandler

    import run_backtest as rb

    faulthandler.disable()
    rb._faulthandler_log = None
    rb.enable_reconnect_faulthandler(str(tmp_path))
    handle = rb._faulthandler_log
    assert handle is not None
    assert not handle.closed
    handle.write("armed\n")
    handle.flush()
    text = (tmp_path / "faulthandler.log").read_text(encoding="utf-8")
    assert "armed" in text
    # A second deploy must not replace the live handle.
    rb.enable_reconnect_faulthandler(str(tmp_path))
    assert rb._faulthandler_log is handle
    assert not handle.closed


# ---------------------------------------------------------------------------
# Bug C — watchdog warn handler must not shortcut the ladder
# ---------------------------------------------------------------------------

class TestBugC_WarnLadderRespected:

    def _warn_branch(self, rb_source: str) -> str:
        # Grab everything inside `elif action == "warn":` until the next
        # `def ` (method boundary).
        match = re.search(
            r'elif action == "warn":(.*?)\n    def ',
            rb_source,
            re.DOTALL,
        )
        assert match, "could not locate `elif action == \"warn\"` branch"
        return match.group(1)

    def test_warn_branch_uses_consecutive_counter(self, rb_source: str):
        warn = self._warn_branch(rb_source)
        assert "_warn_disconnect_count" in warn, (
            "Bug C: warn handler must use a consecutive-bad-read counter "
            "(self._warn_disconnect_count) rather than disconnecting on "
            "the first IsConnected()!=1 reading.")

    def test_warn_branch_requires_two_consecutive(self, rb_source: str):
        """The disconnect short-circuit must require >= 2 consecutive bad reads."""
        warn = self._warn_branch(rb_source)
        # Look for the threshold check — must be >= 2 or > 1.
        assert re.search(r"_warn_disconnect_count\s*>=?\s*2", warn) \
            or re.search(r"_warn_disconnect_count\s*>\s*1", warn), (
            "Bug C: warn handler must require AT LEAST 2 consecutive "
            "IsConnected()!=1 reads before forcing disconnect.")

    def test_warn_branch_does_not_call_on_disconnected_unconditionally(
            self, rb_source: str):
        """The pre-bug code called `_on_disconnected()` directly inside the
        try-block right after one bad read. The fix must guard that call
        behind the counter threshold.
        """
        warn = self._warn_branch(rb_source)
        # Find the `_on_disconnected` call site(s) inside warn.
        # Each call must be preceded by the counter check on the same path.
        # Simplest pin: between the IsConnected() call and the _on_disconnected
        # call, the counter check must appear.
        ic_idx = warn.find("skQ.SKQuoteLib_IsConnected()")
        disc_idx = warn.find("self._on_disconnected()")
        assert ic_idx != -1, "IsConnected() probe missing from warn branch"
        assert disc_idx != -1, "_on_disconnected() call missing from warn branch"
        between = warn[ic_idx:disc_idx]
        assert "_warn_disconnect_count" in between, (
            "Bug C: _on_disconnected() must be guarded by "
            "_warn_disconnect_count check, not called on every bad read.")

    def test_warn_counter_reset_on_real_tick(self, rb_source: str):
        """A real tick is the strongest signal that the connection is alive
        — it must reset the warn counter so transient bad reads from
        earlier don't accumulate forever."""
        # _on_com_tick should reset _warn_disconnect_count
        match = re.search(
            r"def _on_com_tick\(.*?(?=\n    def )",
            rb_source,
            re.DOTALL,
        )
        assert match, "could not locate _on_com_tick"
        body = match.group(0)
        assert "_warn_disconnect_count" in body, (
            "Bug C: _on_com_tick must reset _warn_disconnect_count when "
            "a real tick arrives.")


# ---------------------------------------------------------------------------
# Logging — pin the structured tag prefixes
# ---------------------------------------------------------------------------

class TestStructuredLoggingTags:

    @pytest.mark.parametrize("tag", ["[CONN]", "[RECONNECT]", "[TICKS]",
                                     "[WATCHDOG]", "[STATE]"])
    def test_tag_present(self, rb_source: str, tag: str):
        """Every structured tag prefix the spec requires must appear in
        the source — a debugger should be able to ``grep '\\[CONN\\]'`` and
        get a useful slice of the log."""
        assert tag in rb_source, (
            f"Logging tag {tag} not found in run_backtest.py — the next "
            "debugging session won't be able to filter by it.")

    def test_log_debug_helper_exists(self, rb_source: str):
        assert "def _log_debug(" in rb_source, (
            "Missing _log_debug helper — DEBUG-level structured logging "
            "should go through a single helper so file capture and the "
            "[DEBUG] prefix stay consistent.")

    def test_log_debug_includes_wallclock(self, rb_source: str):
        """Per spec, every line should include wall-clock timestamp."""
        match = re.search(
            r"def _log_debug\(.*?(?=\n\s*\ndef )",
            rb_source,
            re.DOTALL,
        )
        assert match, "could not locate _log_debug body"
        body = match.group(0)
        assert "_taipei_now" in body and "datetime.now" in body, (
            "_log_debug must emit BOTH Taipei and local wall-clock times "
            "so cross-region correlation works.")

    def test_set_quote_connected_helper(self, rb_source: str):
        """_quote_connected must be mutated through a helper so every
        transition is logged."""
        assert "def _set_quote_connected(" in rb_source, (
            "Missing _set_quote_connected helper — direct assignment of "
            "self._quote_connected loses the state-transition log line.")


# ---------------------------------------------------------------------------
# Issue #78 — bar/tick monotonicity guard reset on reconnect
# ---------------------------------------------------------------------------

class TestIssue78_MonotonicityResetOnReconnect:
    """After a COM dropout, RequestTicks replays history in a burst whose
    timestamps can go backwards. `_resubscribe_ticks` must reset the
    out-of-order guards so the first tick/bar after the gap is accepted,
    then let the guards drop the stale remainder."""

    def test_resubscribe_resets_bar_builder_guard(self, rb_source: str):
        match = re.search(
            r"def _resubscribe_ticks\(self.*?(?=\n    def )",
            rb_source,
            re.DOTALL,
        )
        assert match, "could not locate _resubscribe_ticks body"
        body = match.group(0)
        assert "reset_stale_tracking" in body, (
            "Issue #78 regression: _resubscribe_ticks must reset the "
            "BarBuilder out-of-order tick guard on reconnect.")

    def test_resubscribe_resets_aggregator_guards(self, rb_source: str):
        match = re.search(
            r"def _resubscribe_ticks\(self.*?(?=\n    def )",
            rb_source,
            re.DOTALL,
        )
        assert match, "could not locate _resubscribe_ticks body"
        body = match.group(0)
        assert "reset_bar_monotonicity" in body, (
            "Issue #78 regression: _resubscribe_ticks must reset the "
            "aggregator out-of-order bar guards on reconnect.")
