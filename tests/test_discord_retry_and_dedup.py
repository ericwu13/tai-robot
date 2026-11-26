"""Tests for issue #95: Discord 429 retry and session-level report dedup."""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
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
    """Return the delivery body the worker runs for one message.

    HTTP 429 backoff lives here, not in the queue, so a retry already in
    flight is not dropped when later sends fill the queue. Calling it
    directly keeps the backoff assertions off the worker thread.
    """
    target = notifier._resolve_channel("")
    return lambda: notifier._deliver(content, target)


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

    @staticmethod
    def _mock_session(date, slot):
        """Create a mock SessionInfo for the given date and slot."""
        from datetime import datetime, timezone, timedelta
        tz = timezone(timedelta(hours=8))
        if slot == "DAY":
            open_dt = datetime.fromisoformat(f"{date}T08:45:00").replace(tzinfo=tz)
            close_dt = datetime.fromisoformat(f"{date}T13:45:00").replace(tzinfo=tz)
        else:
            open_dt = datetime.fromisoformat(f"{date}T15:00:00").replace(tzinfo=tz)
            close_dt = open_dt + timedelta(hours=14)
        from src.regime.switch_logic import SessionInfo
        return SessionInfo(date, slot, open_dt, close_dt)

    def test_day_and_night_both_fire(self):
        """Same date, different sessions -- both reports emitted."""
        report = {"date": "2026-07-24", "strategy": {"name": "T"}}

        # DAY session
        runner = self._make_runner(("2026-07-24", "DAY"))
        day_session = self._mock_session("2026-07-24", "DAY")
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ), patch(
            "src.regime.switch_logic.current_session",
            return_value=day_session,
        ):
            runner._generate_daily_report()
        assert runner._last_report_key == "2026-07-24_DAY"
        assert "on_daily_report" in runner._events

        # NIGHT session -- same date, different session
        runner._events.clear()
        runner._sk = ("2026-07-24", "NIGHT")
        night_session = self._mock_session("2026-07-24", "NIGHT")
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ), patch(
            "src.regime.switch_logic.current_session",
            return_value=night_session,
        ):
            runner._generate_daily_report()
        assert runner._last_report_key == "2026-07-24_NIGHT"
        assert "on_daily_report" in runner._events

    def test_same_session_deduped(self):
        """Same date + same session -- second call is suppressed."""
        report = {"date": "2026-07-24", "strategy": {"name": "T"}}

        runner = self._make_runner(("2026-07-24", "DAY"))
        day_session = self._mock_session("2026-07-24", "DAY")
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ), patch(
            "src.regime.switch_logic.current_session",
            return_value=day_session,
        ):
            runner._generate_daily_report()
        assert "on_daily_report" in runner._events

        runner._events.clear()
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ), patch(
            "src.regime.switch_logic.current_session",
            return_value=day_session,
        ):
            runner._generate_daily_report()
        assert "on_daily_report" not in runner._events

    def test_gap_stop_uses_session_slot_fallback(self):
        """stop() during a gap (current_session=None) still deduplicates."""
        report = {"date": "2026-07-24", "strategy": {"name": "T"}}

        runner = self._make_runner(("2026-07-24", "NIGHT"))
        # First report during live session
        night_session = self._mock_session("2026-07-24", "NIGHT")
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ), patch(
            "src.regime.switch_logic.current_session",
            return_value=night_session,
        ):
            runner._generate_daily_report()
        assert runner._last_report_key == "2026-07-24_NIGHT"
        assert "on_daily_report" in runner._events

        # Second call during gap (stop at 05:30) — current_session=None,
        # session_slot falls back to ("2026-07-25", "NIGHT").
        # Key differs, so a fresh report is emitted — but the key is never
        # empty, preventing dedup destruction (review issue #95).
        runner._events.clear()
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ), patch(
            "src.regime.switch_logic.current_session",
            return_value=None,
        ), patch(
            "src.regime.switch_logic.session_slot",
            return_value=("2026-07-25", "NIGHT"),
        ):
            runner._generate_daily_report()
        # Key is non-empty even when current_session is None
        assert runner._last_report_key != ""
        assert runner._last_report_key == "2026-07-25_NIGHT"

    def test_gap_stop_dedup_same_slot(self):
        """Two gap-stop calls with the same session_slot are deduped."""
        report = {"date": "2026-07-25", "strategy": {"name": "T"}}

        runner = self._make_runner(("2026-07-25", "NIGHT"))
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ), patch(
            "src.regime.switch_logic.current_session",
            return_value=None,
        ), patch(
            "src.regime.switch_logic.session_slot",
            return_value=("2026-07-25", "NIGHT"),
        ):
            runner._generate_daily_report()
        assert "on_daily_report" in runner._events

        runner._events.clear()
        with patch(
            "src.daily_report.report_generator.generate_session_report",
            return_value=dict(report),
        ), patch(
            "src.regime.switch_logic.current_session",
            return_value=None,
        ), patch(
            "src.regime.switch_logic.session_slot",
            return_value=("2026-07-25", "NIGHT"),
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

    def test_sent_report_skip_is_logged_once(self, caplog):
        """Three repeats of a sent key log one skip line; a new key logs again."""
        _assert_sent_skip_logged_once(
            self._make_runner(("2026-07-24", "DAY")),
            "src.live.live_runner",
            caplog,
        )

    def test_no_trades_is_logged_once(self, caplog):
        """A None report logs once per key and still does not emit."""
        _assert_no_trades_logged_once(
            self._make_runner(("2026-07-24", "DAY")),
            "src.live.live_runner",
            caplog,
        )


def _info_records(caplog, logger_name, text):
    return [
        rec for rec in caplog.records
        if rec.name == logger_name and rec.levelno == logging.INFO
        and text in rec.getMessage()
    ]


def _assert_sent_skip_logged_once(runner, logger_name, caplog):
    report = {"date": "2026-07-24", "strategy": {"name": "T"}}
    day = TestDailyReportSessionDedup._mock_session("2026-07-24", "DAY")
    night = TestDailyReportSessionDedup._mock_session("2026-07-24", "NIGHT")
    gen = "src.daily_report.report_generator.generate_session_report"
    cur = "src.regime.switch_logic.current_session"
    with caplog.at_level(logging.INFO, logger=logger_name):
        with patch(gen, return_value=dict(report)) as generated, patch(
            cur, return_value=day,
        ):
            runner._generate_daily_report()
            for _ in range(3):
                runner._generate_daily_report()
            assert generated.call_count == 1
        assert runner._last_report_key == "2026-07-24_DAY"
        assert len(runner._events["on_daily_report"]) == 1
        skips = _info_records(caplog, logger_name, "already sent")
        assert len(skips) == 1
        assert skips[0].getMessage() == (
            "daily report skipped: key 2026-07-24_DAY already sent "
            "(bot=test_bot)")

        runner._events.clear()
        runner._sk = ("2026-07-24", "NIGHT")
        with patch(gen, return_value=dict(report)) as generated, patch(
            cur, return_value=night,
        ):
            runner._generate_daily_report()
            for _ in range(3):
                runner._generate_daily_report()
            assert generated.call_count == 1
        assert runner._last_report_key == "2026-07-24_NIGHT"
        assert len(runner._events["on_daily_report"]) == 1
        skips = _info_records(caplog, logger_name, "already sent")
        assert len(skips) == 2
        assert "key 2026-07-24_NIGHT already sent" in skips[1].getMessage()


def _assert_no_trades_logged_once(runner, logger_name, caplog):
    day = TestDailyReportSessionDedup._mock_session("2026-07-24", "DAY")
    night = TestDailyReportSessionDedup._mock_session("2026-07-24", "NIGHT")
    gen = "src.daily_report.report_generator.generate_session_report"
    cur = "src.regime.switch_logic.current_session"
    with caplog.at_level(logging.INFO, logger=logger_name):
        with patch(gen, return_value=None) as generated, patch(
            cur, return_value=day,
        ):
            for _ in range(3):
                runner._generate_daily_report()
            assert generated.call_count == 3
        assert runner._last_report_key is None
        assert "on_daily_report" not in runner._events
        notes = _info_records(caplog, logger_name, "no completed trades")
        assert len(notes) == 1
        assert notes[0].getMessage() == (
            "daily report not generated: no completed trades for key "
            "2026-07-24_DAY (bot=test_bot)")

        runner._sk = ("2026-07-24", "NIGHT")
        with patch(gen, return_value=None) as generated, patch(
            cur, return_value=night,
        ):
            runner._generate_daily_report()
            runner._generate_daily_report()
            assert generated.call_count == 2
        assert runner._last_report_key is None
        assert "on_daily_report" not in runner._events
        notes = _info_records(caplog, logger_name, "no completed trades")
        assert len(notes) == 2
        assert "2026-07-24_NIGHT" in notes[1].getMessage()


class TestRegimeDailyReportSkipLog:
    """The regime override is a copy of the same two silent returns."""

    def _make_runner(self, session_key=("2026-07-24", "DAY")):
        from src.live.regime_switching_runner import RegimeSwitchingRunner

        class StubRunner(RegimeSwitchingRunner):
            def __init__(self, sk):
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
                self._active_leg = "long"
                self._long_strategy_name = "LongLeg"
                self._short_strategy_name = "ShortLeg"
                self._manager = SimpleNamespace(
                    _state=SimpleNamespace(effective_regime="trending-up"))

            def _session_key(self):
                return self._sk

            def _auto_save_session(self):
                pass

            def _emit(self, event, *args):
                self._events.setdefault(event, []).append(args)

        return StubRunner(session_key)

    def test_sent_report_skip_is_logged_once(self, caplog):
        _assert_sent_skip_logged_once(
            self._make_runner(("2026-07-24", "DAY")),
            "src.live.regime_switching_runner",
            caplog,
        )

    def test_no_trades_is_logged_once(self, caplog):
        _assert_no_trades_logged_once(
            self._make_runner(("2026-07-24", "DAY")),
            "src.live.regime_switching_runner",
            caplog,
        )
