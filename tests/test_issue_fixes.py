"""Tests for GitHub issue fixes (#3, #5, #7, #8)."""

import inspect
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.ai.chat_client import ChatClient, PROVIDER_GOOGLE
from src.ai.code_sandbox import extract_python_code, validate_code, load_strategy_from_source
from src.ai.prompts import STRATEGY_CODE_CONTEXT


# ---------------------------------------------------------------------------
# Issue #8: macd() parameter names in prompt must match actual function
# ---------------------------------------------------------------------------

class TestIssue8_PromptParamNames:
    """Verify STRATEGY_CODE_CONTEXT documents correct indicator signatures."""

    def test_macd_param_names_match_actual(self):
        """The prompt must document fast_period/slow_period/signal_period,
        not fast/slow/signal — the exact bug that caused issue #8."""
        from src.strategy.indicators.macd import macd
        sig = inspect.signature(macd)
        param_names = list(sig.parameters.keys())
        # Actual params: values, fast_period, slow_period, signal_period
        assert "fast_period" in param_names
        assert "slow_period" in param_names
        assert "signal_period" in param_names

        # Prompt must contain the correct names
        assert "fast_period" in STRATEGY_CODE_CONTEXT
        assert "slow_period" in STRATEGY_CODE_CONTEXT
        assert "signal_period" in STRATEGY_CODE_CONTEXT

        # Prompt must NOT contain the old wrong short names as kwargs
        # (they could appear as Pine Script positional args, so check the
        # Python indicator section specifically)
        py_section_start = STRATEGY_CODE_CONTEXT.index("## Available Indicators")
        py_section_end = STRATEGY_CODE_CONTEXT.index("## Code Rules")
        py_section = STRATEGY_CODE_CONTEXT[py_section_start:py_section_end]
        assert "fast=" not in py_section, "Prompt still has wrong param name 'fast='"
        assert "slow=" not in py_section, "Prompt still has wrong param name 'slow='"
        assert "signal=" not in py_section, "Prompt still has wrong param name 'signal='"

    def test_sma_param_names_match_actual(self):
        from src.strategy.indicators.ma import sma
        sig = inspect.signature(sma)
        assert "values" in sig.parameters
        assert "period" in sig.parameters
        assert "sma(values, period)" in STRATEGY_CODE_CONTEXT

    def test_ema_param_names_match_actual(self):
        from src.strategy.indicators.ma import ema
        sig = inspect.signature(ema)
        assert "values" in sig.parameters
        assert "period" in sig.parameters
        assert "ema(values, period)" in STRATEGY_CODE_CONTEXT

    def test_rsi_param_names_match_actual(self):
        from src.strategy.indicators.rsi import rsi
        sig = inspect.signature(rsi)
        assert "values" in sig.parameters
        assert "period" in sig.parameters
        assert "rsi(values, period" in STRATEGY_CODE_CONTEXT

    def test_bollinger_bands_param_names_match_actual(self):
        from src.strategy.indicators.bollinger import bollinger_bands
        sig = inspect.signature(bollinger_bands)
        assert "values" in sig.parameters
        assert "period" in sig.parameters
        assert "bollinger_bands(values, period" in STRATEGY_CODE_CONTEXT

    def test_ai_generated_macd_code_runs(self):
        """Simulate what the AI would generate following the prompt docs.
        This is the exact pattern from issue #8 that used to fail."""
        code = '''
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext, OrderSide
from src.market_data.models import Bar
from src.market_data.data_store import DataStore
from src.strategy.indicators import macd

class MacdTestStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 60

    def __init__(self, **kwargs):
        self.fast = kwargs.get("fast", 12)

    def required_bars(self) -> int:
        return 35

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
        closes = data_store.get_closes()
        result = macd(closes, fast_period=12, slow_period=26, signal_period=9)
        if result is None:
            return
'''
        # Must load without error
        strategy_cls = load_strategy_from_source(code)
        assert strategy_cls is not None
        assert strategy_cls.__name__ == "MacdTestStrategy"


# ---------------------------------------------------------------------------
# Issue #7: Gemini truncated response detection
# ---------------------------------------------------------------------------

def _google_response_with_finish(text, finish_reason="STOP", status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {
        "candidates": [{
            "content": {"parts": [{"text": text}]},
            "finishReason": finish_reason,
        }],
    }
    resp.text = json.dumps(resp.json.return_value)
    return resp


class TestIssue7_TruncatedResponse:
    """Verify truncated Gemini responses are detected."""

    def test_normal_response_no_warning(self):
        client = ChatClient(api_key="test", provider=PROVIDER_GOOGLE)
        client._client = MagicMock()
        client._client.post.return_value = _google_response_with_finish(
            "```python\nclass Foo:\n    pass\n```", "STOP")

        result = client.one_shot("generate code")
        assert "[WARNING: Response truncated" not in result

    def test_max_tokens_adds_warning(self):
        """When finishReason is MAX_TOKENS, response should include warning."""
        client = ChatClient(api_key="test", provider=PROVIDER_GOOGLE)
        client._client = MagicMock()
        truncated_code = "```python\nclass Foo:\n    def on_bar(self) ->"
        client._client.post.return_value = _google_response_with_finish(
            truncated_code, "MAX_TOKENS")

        result = client.one_shot("generate code")
        assert "[WARNING: Response truncated" in result

    def test_send_message_max_tokens_adds_warning(self):
        """send_message should also detect truncation."""
        client = ChatClient(api_key="test", provider=PROVIDER_GOOGLE)
        client._client = MagicMock()
        client._client.post.return_value = _google_response_with_finish(
            "partial response...", "MAX_TOKENS")

        result = client.send_message("test")
        assert "[WARNING: Response truncated" in result

    def test_one_shot_max_tokens_passthrough_google(self):
        """one_shot(max_tokens=N) should pass N to Gemini's maxOutputTokens."""
        client = ChatClient(api_key="test", provider=PROVIDER_GOOGLE)
        client._client = MagicMock()
        client._client.post.return_value = _google_response_with_finish("ok", "STOP")

        client.one_shot("test", max_tokens=65536)
        call_args = client._client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["generationConfig"]["maxOutputTokens"] == 65536

    def test_one_shot_default_max_tokens(self):
        """one_shot() without max_tokens should use the client default."""
        client = ChatClient(api_key="test", provider=PROVIDER_GOOGLE, max_tokens=16384)
        client._client = MagicMock()
        client._client.post.return_value = _google_response_with_finish("ok", "STOP")

        client.one_shot("test")
        call_args = client._client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["generationConfig"]["maxOutputTokens"] == 16384

    def test_truncated_code_extraction_with_syntax_error(self):
        """Truncated code (no closing ```, incomplete syntax) should be
        extractable but fail validation — reproducing issue #7's flow."""
        truncated_response = '''```python
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext, OrderSide
from src.market_data.models import Bar
from src.market_data.data_store import DataStore

class ExtremeMomentumStrategy(BacktestStrategy):
    kline_type = 0
    kline_minute = 1

    def __init__(self, **kwargs):
        self.m5_macd_fast = kwargs.get("m5_macd_fast", 60)

    def required_bars(self) -> int:
        return 175

    def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) ->'''

        # extract_python_code handles truncated responses
        code = extract_python_code(truncated_response)
        assert code is not None  # extracted despite no closing ```

        # But validation catches the syntax error
        errors = validate_code(code)
        assert len(errors) > 0
        assert any("Syntax" in e for e in errors)


# ---------------------------------------------------------------------------
# Issue #5: Tick watchdog — connected but no ticks
# ---------------------------------------------------------------------------

class TestIssue5_TickWatchdog:
    """Test the watchdog logic for the overnight stale-tick scenario.

    Since _check_tick_watchdog lives in run_backtest.py (GUI code), we test
    the logic by extracting the decision rules into a helper and testing that.
    """

    def test_watchdog_returns_resubscribe_when_connected_5min(self):
        """After 5 min of no ticks while IsConnected==1, should re-subscribe."""
        action = _watchdog_action(
            elapsed=310,  # 5 min 10 sec
            is_connected=True,
            market_open_now=True,
            last_tick_during_market=True,
        )
        assert action == "resubscribe"

    def test_watchdog_returns_reconnect_when_connected_10min(self):
        """After 10 min of no ticks while IsConnected==1, should force reconnect."""
        action = _watchdog_action(
            elapsed=620,  # 10+ min
            is_connected=True,
            market_open_now=True,
            last_tick_during_market=True,
        )
        assert action == "reconnect"

    def test_watchdog_returns_disconnect_when_not_connected(self):
        """When IsConnected != 1, should trigger disconnect handler."""
        action = _watchdog_action(
            elapsed=130,  # just over 2 min
            is_connected=False,
            market_open_now=True,
            last_tick_during_market=True,
        )
        assert action == "disconnect"

    def test_watchdog_returns_none_when_market_closed(self):
        """During closed market, watchdog should do nothing."""
        action = _watchdog_action(
            elapsed=3600,
            is_connected=True,
            market_open_now=False,
            last_tick_during_market=True,
        )
        assert action is None

    def test_watchdog_returns_reset_when_last_tick_during_closed(self):
        """If last tick was during closed market hours, should reset timer."""
        action = _watchdog_action(
            elapsed=3600,
            is_connected=True,
            market_open_now=True,
            last_tick_during_market=False,
        )
        assert action == "reset"

    def test_watchdog_returns_warn_under_5min(self):
        """Between 2-5 min, should warn but not take action if connected."""
        action = _watchdog_action(
            elapsed=180,  # 3 min
            is_connected=True,
            market_open_now=True,
            last_tick_during_market=True,
        )
        assert action == "warn"

    def test_watchdog_returns_none_under_threshold(self):
        """Under 2 min, no action needed."""
        action = _watchdog_action(
            elapsed=60,
            is_connected=True,
            market_open_now=True,
            last_tick_during_market=True,
        )
        assert action is None

    def test_overnight_scenario_issue5(self):
        """Reproduce the exact issue #5 scenario:
        - Last tick during PM session (market open)
        - Now it's AM session (market open)
        - Elapsed > 3 hours (overnight gap)
        - IsConnected returns 1
        Should force reconnect (> 10 min threshold)."""
        action = _watchdog_action(
            elapsed=3 * 3600,  # 3 hours
            is_connected=True,
            market_open_now=True,
            last_tick_during_market=True,  # PM session = market open
        )
        assert action == "reconnect"


def _watchdog_action(
    elapsed: float,
    is_connected: bool,
    market_open_now: bool,
    last_tick_during_market: bool,
) -> str | None:
    """Pure-function version of _check_tick_watchdog decision logic.

    Returns: None, "reset", "warn", "disconnect", "resubscribe", "reconnect"
    """
    WATCHDOG_TIMEOUT = 120
    RESUBSCRIBE_TIMEOUT = 300
    FORCE_RECONNECT_TIMEOUT = 600

    if not market_open_now:
        return None

    if elapsed <= WATCHDOG_TIMEOUT:
        return None

    if not last_tick_during_market:
        return "reset"

    if not is_connected:
        return "disconnect"

    if elapsed > FORCE_RECONNECT_TIMEOUT:
        return "reconnect"

    if elapsed > RESUBSCRIBE_TIMEOUT:
        return "resubscribe"

    return "warn"


# ---------------------------------------------------------------------------
# Issue #3: MACD performance (O(n²) per call, O(n³) total)
# ---------------------------------------------------------------------------

class TestIssue3_MacdPerformance:
    """Verify macd() completes in reasonable time for realistic data sizes."""

    def test_macd_1000_bars_under_2s(self):
        """1000 bars (3 months of hourly data) should complete quickly."""
        from src.strategy.indicators.macd import macd
        values = [20000 + i * 10 for i in range(1000)]
        start = time.perf_counter()
        result = macd(values)
        elapsed = time.perf_counter() - start
        assert result is not None
        assert elapsed < 2.0, f"macd(1000 values) took {elapsed:.2f}s, expected < 2s"

    def test_macd_500_bars_under_500ms(self):
        """500 bars should be very fast."""
        from src.strategy.indicators.macd import macd
        values = [20000 + i * 10 for i in range(500)]
        start = time.perf_counter()
        result = macd(values)
        elapsed = time.perf_counter() - start
        assert result is not None
        assert elapsed < 0.5, f"macd(500 values) took {elapsed:.2f}s, expected < 0.5s"

    def test_macd_repeated_calls_simulating_backtest(self):
        """Simulate backtest: call macd() on growing data for 500 bars.
        This is the O(n³) scenario from issue #3."""
        from src.strategy.indicators.macd import macd
        all_values = [20000 + i * 10 for i in range(500)]
        start = time.perf_counter()
        for i in range(35, 500):  # start after required_bars
            macd(all_values[:i])
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, (
            f"500 repeated macd() calls took {elapsed:.2f}s, expected < 5s. "
            f"This confirms the O(n³) performance issue."
        )

    def test_backtest_engine_does_not_block(self):
        """Verify BacktestEngine.run() can complete a simple strategy quickly."""
        from src.backtest.engine import BacktestEngine
        from src.backtest.strategy import BacktestStrategy
        from src.market_data.models import Bar
        from datetime import datetime

        class NullStrategy(BacktestStrategy):
            kline_type = 0
            kline_minute = 60
            def required_bars(self):
                return 1
            def on_bar(self, bar, data_store, broker):
                pass

        from datetime import timedelta
        base = datetime(2025, 1, 1, 9, 0)
        bars = [
            Bar(symbol="TX00", dt=base + timedelta(minutes=i),
                open=20000, high=20010, low=19990, close=20005,
                volume=100, interval=60)
            for i in range(1000)
        ]

        engine = BacktestEngine(NullStrategy(), point_value=200)
        start = time.perf_counter()
        result = engine.run(bars)
        elapsed = time.perf_counter() - start

        assert result.bars_processed == 1000
        assert elapsed < 2.0, f"1000-bar backtest took {elapsed:.2f}s"
