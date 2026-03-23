"""Tests for Issue #23: Resubscribe doesn't work over the weekends

The issue is that:
1. When reconnecting over weekends, it doesn't resubscribe properly when market opens on Monday
2. When symbol is changed to TMF, it still resubscribes to TX00 instead of the current symbol
"""

from unittest.mock import MagicMock, Mock, patch
import pytest

from src.gateway.quote_feed import QuoteFeed
from src.gateway.event_bus import EventBus


class TestIssue23_ResubscriptionProblems:
    """Test resubscription logic over weekends and symbol changes."""

    @patch('src.gateway.quote_feed._get_sk')
    def test_resubscribe_all_uses_current_symbols(self, mock_get_sk):
        """resubscribe_all() should use symbols currently in _subscribed_symbols set."""
        event_bus = EventBus()
        login_id = "test_user"
        quote_feed = QuoteFeed(event_bus, login_id)

        # Mock the SK module to track calls
        mock_sk = MagicMock()
        mock_get_sk.return_value = mock_sk

        # Subscribe to initial symbol (simulating TX00)
        quote_feed.subscribe("TX00")
        assert "TX00" in quote_feed._subscribed_symbols

        # Clear the mock to track resubscribe calls
        mock_sk.reset_mock()

        # Call resubscribe_all - should resubscribe to TX00
        quote_feed.resubscribe_all()

        # Verify it called the right subscription methods for TX00
        mock_sk.SKQuoteLib_RequestStocks.assert_called_with("TX00")
        mock_sk.SKQuoteLib_RequestTicks.assert_called_with(0, "TX00")
        assert mock_sk.SKQuoteLib_RequestStocks.call_count == 1
        assert mock_sk.SKQuoteLib_RequestTicks.call_count == 1

    @patch('src.gateway.quote_feed._get_sk')
    def test_subscribe_to_new_symbol_updates_subscribed_set(self, mock_get_sk):
        """Subscribing to a new symbol should update _subscribed_symbols set."""
        event_bus = EventBus()
        login_id = "test_user"
        quote_feed = QuoteFeed(event_bus, login_id)

        # Mock the SK module
        mock_sk = MagicMock()
        mock_get_sk.return_value = mock_sk

        # Subscribe to TX00 first
        quote_feed.subscribe("TX00")
        assert quote_feed._subscribed_symbols == {"TX00"}

        # Subscribe to TMF (should add to set, not replace)
        quote_feed.subscribe("TMF")
        assert "TMF" in quote_feed._subscribed_symbols
        assert "TX00" in quote_feed._subscribed_symbols

    @patch('src.gateway.quote_feed._get_sk')
    def test_unsubscribe_removes_from_subscribed_set(self, mock_get_sk):
        """Unsubscribing should remove symbol from _subscribed_symbols set."""
        event_bus = EventBus()
        login_id = "test_user"
        quote_feed = QuoteFeed(event_bus, login_id)

        # Mock the SK module
        mock_sk = MagicMock()
        mock_get_sk.return_value = mock_sk

        # Subscribe to both symbols
        quote_feed.subscribe("TX00")
        quote_feed.subscribe("TMF")
        assert quote_feed._subscribed_symbols == {"TX00", "TMF"}

        # Unsubscribe from TX00
        quote_feed.unsubscribe("TX00")
        assert quote_feed._subscribed_symbols == {"TMF"}

        # Resubscribe should only resubscribe to TMF
        mock_sk.reset_mock()
        quote_feed.resubscribe_all()
        mock_sk.SKQuoteLib_RequestStocks.assert_called_with("TMF")
        mock_sk.SKQuoteLib_RequestTicks.assert_called_with(0, "TMF")
        assert mock_sk.SKQuoteLib_RequestStocks.call_count == 1

    @patch('src.gateway.quote_feed._get_sk')
    def test_change_symbol_method_switches_subscription(self, mock_get_sk):
        """FAILS: Test shows we need a method to switch from one symbol to another.

        This test will FAIL initially because there's no method to change the
        subscribed symbol from TX00 to TMF. This is the core issue.
        """
        event_bus = EventBus()
        login_id = "test_user"
        quote_feed = QuoteFeed(event_bus, login_id)

        # Mock the SK module
        mock_sk = MagicMock()
        mock_get_sk.return_value = mock_sk

        # Subscribe to TX00 initially
        quote_feed.subscribe("TX00")
        assert quote_feed._subscribed_symbols == {"TX00"}

        # This method doesn't exist yet - this test should FAIL
        # This demonstrates the problem: we can't change the symbol cleanly
        try:
            quote_feed.change_symbol("TMF")
            # If this succeeds, TMF should be subscribed and TX00 unsubscribed
            assert quote_feed._subscribed_symbols == {"TMF"}

            # Resubscribe should use TMF, not TX00
            mock_sk.reset_mock()
            quote_feed.resubscribe_all()
            mock_sk.SKQuoteLib_RequestStocks.assert_called_with("TMF")
            mock_sk.SKQuoteLib_RequestTicks.assert_called_with(0, "TMF")
        except AttributeError:
            # This is expected to fail initially - the method doesn't exist
            pytest.fail("change_symbol method doesn't exist - this is the bug we need to fix")

    @patch('src.gateway.quote_feed._get_sk')
    def test_weekend_reconnection_scenario(self, mock_get_sk):
        """FAILS: Test shows weekend reconnection issue.

        Over the weekend, when market reopens, resubscription should work properly.
        """
        event_bus = EventBus()
        login_id = "test_user"
        quote_feed = QuoteFeed(event_bus, login_id)

        # Mock the SK module
        mock_sk = MagicMock()
        mock_get_sk.return_value = mock_sk

        # Subscribe to TMF before weekend
        quote_feed.subscribe("TMF")
        assert "TMF" in quote_feed._subscribed_symbols

        # Simulate weekend disconnection (connection lost but symbols remain)
        # The _subscribed_symbols should persist
        assert quote_feed._subscribed_symbols == {"TMF"}

        # Monday morning: reconnect and resubscribe
        mock_sk.reset_mock()
        quote_feed.resubscribe_all()

        # Should resubscribe to TMF, not TX00
        mock_sk.SKQuoteLib_RequestStocks.assert_called_with("TMF")
        mock_sk.SKQuoteLib_RequestTicks.assert_called_with(0, "TMF")

        # Verify it didn't try to subscribe to TX00
        for call in mock_sk.SKQuoteLib_RequestStocks.call_args_list:
            assert call[0][0] != "TX00", "Should not resubscribe to TX00 when TMF was selected"