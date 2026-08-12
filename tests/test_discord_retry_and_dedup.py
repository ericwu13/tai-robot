"""Tests for issue #95: Discord 429 retry and session-level report dedup."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from src.live.discord_notify import DiscordNotifier


# ---------------------------------------------------------------------------
# Bug 1: HTTP 429 retry with exponential backoff
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _extract_post_fn(notifier, content):
    """Extract the _post closure from _send without starting a thread."""
    captured = []
    original_thread_init = threading.Thread.__init__

    def capture_target(self_t, *args, **kwargs):
        original_thread_init(self_t, *args, **kwargs)
        if kwargs.get("target"):
            captured.append(kwargs["target"])

    with patch.object(threading.Thread, "__init__", capture_target), \
         patch.object(threading.Thread, "start", lambda self: None):
        notifier._send(content)

    assert captured, "_send did not create a thread"
    return captured[0]


def _mock_client(post_fn):
    """Build a mock httpx.Client whose .post() calls post_fn."""
    client = MagicMock()
    client.post = MagicMock(side_effect=post_fn)
    return client


class TestDiscordRetry429:
    """_send retries up to 3 times on 429 with exponential backoff."""

    def _make_notifier(self):
        return DiscordNotifier("token", "channel", bot_name="test")

    def test_success_on_first_try_no_retry(self):
        """200 on first attempt -- no retry, no sleep."""
        n = self._make_notifier()
        calls = []

        def fake_post(*a, **kw):
            calls.append(1)
            return _FakeResponse(200)

        n._http = _mock_client(fake_post)
        with patch("time.sleep") as mock_sleep:
            target_fn = _extract_post_fn(n, "hello")
            target_fn()

        assert len(calls) == 1
        mock_sleep.assert_not_called()

    def test_retry_on_429_then_success(self):
        """429 on first attempt, 200 on second -- one retry, one sleep."""
        n = self._make_notifier()
        responses = [_FakeResponse(429), _FakeResponse(200)]
        call_idx = [0]
        sleeps = []

        def fake_post(*a, **kw):
            resp = responses[call_idx[0]]
            call_idx[0] += 1
            return resp

        n._http = _mock_client(fake_post)
        with patch("time.sleep", side_effect=lambda d: sleeps.append(d)):
            target_fn = _extract_post_fn(n, "hello")
            target_fn()

        assert call_idx[0] == 2
        assert sleeps == [1.0]

    def test_all_retries_exhausted_logs_warning(self):
        """429 on all 3 attempts -- logs warning after exhaustion."""
        n = self._make_notifier()
        sleeps = []

        def fake_post(*a, **kw):
            return _FakeResponse(429)

        n._http = _mock_client(fake_post)
        with patch("time.sleep", side_effect=lambda d: sleeps.append(d)), \
             patch("src.live.discord_notify.logger") as mock_logger:
            target_fn = _extract_post_fn(n, "hello")
            target_fn()

        assert sleeps == [1.0, 2.0, 4.0]
        mock_logger.warning.assert_called_once()
        assert "429" in mock_logger.warning.call_args[0][0]

    def test_non_429_error_not_retried(self):
        """A 403 error is logged but NOT retried."""
        n = self._make_notifier()
        calls = []

        def fake_post(*a, **kw):
            calls.append(1)
            return _FakeResponse(403, "Forbidden")

        n._http = _mock_client(fake_post)
        with patch("time.sleep") as mock_sleep, \
             patch("src.live.discord_notify.logger") as mock_logger:
            target_fn = _extract_post_fn(n, "hello")
            target_fn()

        assert len(calls) == 1
        mock_sleep.assert_not_called()
        mock_logger.error.assert_called_once()

    def test_429_then_non_429_error_stops(self):
        """429 first, then 500 -- retries once then stops on non-429."""
        n = self._make_notifier()
        responses = [_FakeResponse(429), _FakeResponse(500, "Server Error")]
        call_idx = [0]
        sleeps = []

        def fake_post(*a, **kw):
            resp = responses[call_idx[0]]
            call_idx[0] += 1
            return resp

        n._http = _mock_client(fake_post)
        with patch("time.sleep", side_effect=lambda d: sleeps.append(d)), \
             patch("src.live.discord_notify.logger") as mock_logger:
            target_fn = _extract_post_fn(n, "hello")
            target_fn()

        assert call_idx[0] == 2
        assert sleeps == [1.0]
        mock_logger.error.assert_called_once()

    def test_exponential_backoff_delays(self):
        """Backoff delays are 1s, 2s, 4s."""
        n = self._make_notifier()
        sleeps = []

        n._http = _mock_client(lambda *a, **kw: _FakeResponse(429))
        with patch("time.sleep", side_effect=lambda d: sleeps.append(d)), \
             patch("src.live.discord_notify.logger"):
            target_fn = _extract_post_fn(n, "hello")
            target_fn()

        assert sleeps == [1.0, 2.0, 4.0]


# ---------------------------------------------------------------------------
# Bug 2: Session-level dedup (date + session type)
# ---------------------------------------------------------------------------

class TestDailyReportSessionDedup:
    """_generate_daily_report deduplicates by {date}_{session}, not date alone."""

    def _make_runner(self, session_key=("2026-07-24", "DAY")):
        """Build a minimal LiveRunner-like object for testing dedup."""
        from src.live.live_runner import LiveRunner

        class StubRunner(LiveRunner):
            def __init__(self, sk):
                # Skip LiveRunner.__init__ -- only need the dedup fields
                self._last_report_key = None
                self._session_path = None
                self._events = {}
                self._sk = sk
                self.broker = MagicMock()
                self.data_store = None
                self.strategy_display_name = "TestStrat"
                self.strategy = MagicMock(params=None)
                self.point_value = 1
                self.symbol = "TXF1"
                self.bot_name = "test_bot"
                self._started_at = "2026-07-24T08:45:00"

            def _session_key(self):
                return self._sk

            def _auto_save_session(self):
                pass

            def _emit(self, event, *args):
                self._events.setdefault(event, []).append(args)

        return StubRunner(session_key)

    def test_day_and_night_both_fire(self):
        """Same date, different sessions -- both reports emitted."""
        report = {"date": "2026-07-24", "strategy": {"name": "T"}}

        # DAY session
        runner = self._make_runner(("2026-07-24", "DAY"))
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ):
            runner._generate_daily_report()
        assert runner._last_report_key == "2026-07-24_DAY"
        assert "on_daily_report" in runner._events

        # NIGHT session -- same date, different session
        runner._events.clear()
        runner._sk = ("2026-07-24", "NIGHT")
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ):
            runner._generate_daily_report()
        assert runner._last_report_key == "2026-07-24_NIGHT"
        assert "on_daily_report" in runner._events

    def test_same_session_deduped(self):
        """Same date + same session -- second call is suppressed."""
        report = {"date": "2026-07-24", "strategy": {"name": "T"}}

        runner = self._make_runner(("2026-07-24", "DAY"))
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ):
            runner._generate_daily_report()
        assert "on_daily_report" in runner._events

        runner._events.clear()
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ):
            runner._generate_daily_report()
        assert "on_daily_report" not in runner._events

    def test_restore_session_reads_new_key(self):
        """restore_session loads last_report_key from session data."""
        runner = self._make_runner()
        mock_broker = MagicMock()
        mock_broker.trades = []
        runner.broker = mock_broker
        runner.trading_mode = "paper"

        session_data = {
            "broker": {},
            "bar_index": 10,
            "last_report_key": "2026-07-24_DAY",
        }

        from src.backtest.broker import SimulatedBroker
        with patch.object(SimulatedBroker, "from_dict",
                          return_value=mock_broker):
            runner.restore_session(session_data)

        assert runner._last_report_key == "2026-07-24_DAY"

    def test_restore_session_migrates_old_date_key(self):
        """restore_session falls back to old last_report_date for compat."""
        runner = self._make_runner()
        mock_broker = MagicMock()
        mock_broker.trades = []
        runner.broker = mock_broker
        runner.trading_mode = "paper"

        session_data = {
            "broker": {},
            "bar_index": 10,
            "last_report_date": "2026-07-24",
        }

        from src.backtest.broker import SimulatedBroker
        with patch.object(SimulatedBroker, "from_dict",
                          return_value=mock_broker):
            runner.restore_session(session_data)

        assert runner._last_report_key == "2026-07-24"

    def test_session_key_persisted(self):
        """_auto_save_session persists last_report_key, not last_report_date."""
        from src.live.live_runner import LiveRunner

        runner = self._make_runner(("2026-07-24", "NIGHT"))
        runner._last_report_key = "2026-07-24_NIGHT"
        runner.target_interval = 60
        runner.trading_mode = "paper"
        runner.daily_loss_limit = 0
        runner._bar_index = 5
        runner.broker = MagicMock()

        saved = {}

        def fake_save(path, data):
            saved.update(data)

        runner._session_path = "fake_path"
        with patch("src.live.live_runner.save_session", side_effect=fake_save):
            # Call the real _auto_save_session (not the stub override)
            LiveRunner._auto_save_session(runner)

        assert "last_report_key" in saved
        assert saved["last_report_key"] == "2026-07-24_NIGHT"
        assert "last_report_date" not in saved
