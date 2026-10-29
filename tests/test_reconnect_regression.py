"""Regression tests for the three reconnect bugs found in bot 0422.

These are source-level pins for invariants that don't lend themselves to
runtime testing — exercising them properly requires Tkinter, comtypes,
and a live SKCOM session. The pins are tight enough to detect regressions
without re-running real reconnect flows.

Bugs covered:

* **Bug A** — premature "connected" on Quote (3002). ``_on_reconnected``
  must only fire on Ready (3003), and a Quote (3002) without a follow-up
  Ready must schedule a watchdog timer.

* **Bug B** — superseded by #157 P2. The 0422 "cleanup" LeaveMonitor +
  LoginSetQuote sequence is the crash. ``_attempt_reconnect`` must not
  call it. ``SKCenterLib_LogOut`` stays absent (not in Capital API 2.13.57).

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

class TestBugB_NoTeardownOnReconnect:
    """#157 P2. The old LeaveMonitor-then-login body is the crash sequence."""

    def _attempt_reconnect_body(self, rb_source: str) -> str:
        match = re.search(
            r"def _attempt_reconnect\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert match, "could not locate _attempt_reconnect body"
        return match.group(1)

    def test_reconnect_does_not_leave_or_login(self, rb_source: str):
        body = self._attempt_reconnect_body(rb_source)
        for forbidden in (
            "SKQuoteLib_LeaveMonitor",
            "SKCenterLib_LoginSetQuote",
            "SKCenterLib_LogOut",
            'call_com("LeaveMonitor"',
            'call_com("LoginSetQuote"',
            'call_com("LogOut"',
            "SKQuoteLib_EnterMonitorLONG",
            "SKReplyLib_ConnectByID",
        ):
            assert forbidden not in body, forbidden
        assert "_link_host_tick" in body

    def test_logout_is_not_called(self, rb_source: str):
        """LogOut is absent from the reconnect body. 2.13.57 has no such method."""
        body = self._attempt_reconnect_body(rb_source)
        assert "SKCenterLib_LogOut" not in body
        assert 'call_com("LogOut"' not in body

    def test_process_start_login_remains(self, rb_source: str):
        """The one LoginSetQuote stays on the process-start path."""
        match = re.search(
            r"def _do_login\(self.*?(?=\n    def )",
            rb_source,
            re.DOTALL,
        )
        assert match, "could not locate _do_login"
        body = match.group(0)
        assert "SKCenterLib_LoginSetQuote" in body
        assert "_adopt_link_machine" in body


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
        ic_idx = warn.find("skQ.SKQuoteLib_IsConnected")
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


# ---------------------------------------------------------------------------
# Diagnostics kept from #158, plus P0 return timings
# ---------------------------------------------------------------------------

class TestKeptReconnectDiagnostics:

    def test_reconnect_calls_are_logged_before_they_run(self, rb_source: str):
        assert (
            "[RECONNECT] about to call {name} tid={threading.get_ident()}"
            in rb_source
        )
        match = re.search(
            r"def _attempt_reconnect\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert match
        body = match.group(1)
        # The broker calls moved into ComBrokerPort. The reconnect body
        # only ticks the machine, so it does not log a call of its own.
        assert "_log_reconnect_call(" not in body
        assert "SKQuoteLib_IsConnected" not in body
        assert "SKReplyLib_IsConnectedByID" not in body
        assert "def _check_reconnection(" not in rb_source

    def test_duration_line_is_installed_on_call_com(self, rb_source: str):
        assert "def _log_com_return(" in rb_source
        assert "[COM] {name} {status} in {elapsed_ms:.0f} ms" in rb_source
        assert "set_com_return_logger(_log_com_return)" in rb_source

    def test_reply_link_state_helper_does_not_call_skcom(self, rb_source: str):
        """The reconnect helper logs a value the caller already holds.

        Issue #219 reads it from ``_read_is_connected_by_id`` only
        (the 30s log and the 60s poll). Reconnect and this helper still
        must not call it.
        """
        match = re.search(
            r"def _log_reply_link_state\(.*?\n(?=def )",
            rb_source,
            re.DOTALL,
        )
        assert match
        body = match.group(0)
        assert "call_com(" not in body
        assert "SKReplyLib_" not in body
        assert "reply-link IsConnectedByID=" in body
        assert rb_source.count("SKReplyLib_IsConnectedByID") == 1
        probe = re.search(
            r"def _read_is_connected_by_id\(self.*?(?=\n    def )",
            rb_source,
            re.DOTALL,
        )
        assert probe and "SKReplyLib_IsConnectedByID" in probe.group(0)
        reconnect = re.search(
            r"def _attempt_reconnect\(self\):(.*?)\n    def ",
            rb_source,
            re.DOTALL,
        )
        assert reconnect
        assert "SKReplyLib_IsConnectedByID" not in reconnect.group(1)

    def test_faulthandler_armed_at_deploy(self, rb_source: str):
        assert "def enable_reconnect_faulthandler(" in rb_source
        assert "faulthandler.enable(" in rb_source
        assert "_faulthandler_log" in rb_source
        assert "faulthandler.log" in rb_source
        deploy = re.search(
            r"def _deploy_live_continue\(.*?(?=\n    def )",
            rb_source,
            re.DOTALL,
        )
        assert deploy, "could not locate _deploy_live_continue"
        body = deploy.group(0)
        assert "enable_reconnect_faulthandler(" in body
        open_pos = body.find("_open_debug_log(")
        arm_pos = body.find("enable_reconnect_faulthandler(")
        assert open_pos < arm_pos
        assert "try:" in body[open_pos:arm_pos]
        assert "except Exception" in body[arm_pos:arm_pos + 400]


def test_faulthandler_handle_stays_open(tmp_path):
    """The dump file handle must outlive enable(); closing it drops the hook."""
    import faulthandler

    import run_backtest as rb

    faulthandler.disable()
    rb._faulthandler_log = None
    handle = None
    try:
        rb.enable_reconnect_faulthandler(str(tmp_path))
        handle = rb._faulthandler_log
        assert handle is not None
        assert not handle.closed
        handle.write("armed\n")
        handle.flush()
        text = (tmp_path / "faulthandler.log").read_text(encoding="utf-8")
        assert "armed" in text
        rb.enable_reconnect_faulthandler(str(tmp_path))
        assert rb._faulthandler_log is handle
        assert not handle.closed
    finally:
        faulthandler.disable()
        rb._faulthandler_log = None
        if handle is not None and not handle.closed:
            handle.close()


def test_faulthandler_open_failure_does_not_abort_deploy(tmp_path, monkeypatch, capsys):
    """A log-file OSError is a warning. Deploy must continue."""
    import run_backtest as rb

    def boom(*args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(rb, "open", boom, raising=False)
    rb._faulthandler_log = None
    rb.enable_reconnect_faulthandler(str(tmp_path))
    assert rb._faulthandler_log is None
    out = capsys.readouterr().out
    assert "faulthandler not armed" in out
    assert "OSError" in out
