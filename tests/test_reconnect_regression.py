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
        leave_pos = body.find("SKQuoteLib_LeaveMonitor")
        login_pos = body.find("SKCenterLib_LoginSetQuote")
        assert leave_pos != -1, (
            "Bug B: _attempt_reconnect must call SKQuoteLib_LeaveMonitor "
            "to tear down the prior COM session before re-login.")
        assert login_pos != -1, "LoginSetQuote unexpectedly removed"
        assert leave_pos < login_pos, (
            "Bug B: LeaveMonitor must happen BEFORE LoginSetQuote.")

    def test_logout_called_before_login(self, rb_source: str):
        body = self._attempt_reconnect_body(rb_source)
        logout_pos = body.find("SKCenterLib_LogOut")
        login_pos = body.find("SKCenterLib_LoginSetQuote")
        assert logout_pos != -1, (
            "Bug B: _attempt_reconnect must call SKCenterLib_LogOut "
            "before re-login to ensure a clean COM state.")
        assert logout_pos < login_pos, (
            "Bug B: LogOut must happen BEFORE LoginSetQuote.")

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
