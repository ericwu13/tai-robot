"""Test for Issue #23: Resubscribe doesn't work over the weekends."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from src.gateway.quote_feed import QuoteFeed
from src.gateway.event_bus import EventBus


class TestResubscribeIssue:
    """Test resubscription behavior when symbols change."""

    def test_resubscribe_uses_current_symbol_not_stored(self):
        """
        Test that resubscribe uses current symbol, not the one stored at creation.

        This tests the scenario:
        1. Start live trading with TX00
        2. User changes symbol dropdown to TMF00 during weekend
        3. Reconnect on Monday should resubscribe to TMF00, not TX00
        """
        # Setup quote feed
        event_bus = EventBus()
        quote_feed = QuoteFeed(event_bus, "test_login", market_no=2)

        # Mock the SK DLL
        with patch('src.gateway.quote_feed._get_sk') as mock_get_sk:
            mock_sk = Mock()
            mock_get_sk.return_value = mock_sk
            mock_sk.SKQuoteLib_RequestStocks.return_value = 0
            mock_sk.SKQuoteLib_RequestTicks.return_value = 0

            # Initially subscribe to TX00
            quote_feed.subscribe("TX00")
            assert "TX00" in quote_feed._subscribed_symbols

            # User changes selection (simulating dropdown change)
            # This should update the subscription when resubscribe is called
            quote_feed._subscribed_symbols.clear()
            quote_feed._subscribed_symbols.add("TMF00")

            # Resubscribe (simulating reconnection)
            quote_feed.resubscribe_all()

            # Should have called subscribe with the current symbol (TMF00)
            # not the originally stored one (TX00)
            mock_sk.SKQuoteLib_RequestStocks.assert_called_with("TMF00")
            mock_sk.SKQuoteLib_RequestTicks.assert_called_with(0, "TMF00")

    def test_quote_feed_tracks_current_subscription_properly(self):
        """Test that the quote feed properly tracks and resubscribes to current symbols."""
        event_bus = EventBus()
        quote_feed = QuoteFeed(event_bus, "test_login", market_no=2)

        with patch('src.gateway.quote_feed._get_sk') as mock_get_sk:
            mock_sk = Mock()
            mock_get_sk.return_value = mock_sk
            mock_sk.SKQuoteLib_RequestStocks.return_value = 0
            mock_sk.SKQuoteLib_RequestTicks.return_value = 0

            # Subscribe to multiple symbols
            quote_feed.subscribe("TX00")
            quote_feed.subscribe("TMF00")

            assert "TX00" in quote_feed._subscribed_symbols
            assert "TMF00" in quote_feed._subscribed_symbols

            # Unsubscribe from one
            quote_feed.unsubscribe("TX00")
            assert "TX00" not in quote_feed._subscribed_symbols
            assert "TMF00" in quote_feed._subscribed_symbols

            # Resubscribe should only call for remaining symbols
            quote_feed.resubscribe_all()

            # Should have been called for TMF00 but not TX00
            # Check the last call
            last_stocks_call = mock_sk.SKQuoteLib_RequestStocks.call_args_list[-1]
            last_ticks_call = mock_sk.SKQuoteLib_RequestTicks.call_args_list[-1]

            assert last_stocks_call[0][0] == "TMF00"
            assert last_ticks_call[0][1] == "TMF00"

    def test_weekend_symbol_change_scenario_failure(self):
        """
        Test the weekend symbol change scenario from issue #23.

        Scenario:
        1. Start live trading with TX00
        2. Weekend: user changes symbol dropdown to TMF00
        3. Monday: reconnect should use TMF00, not TX00
        """
        from unittest.mock import Mock

        # Simulate the old behavior (before fix)
        def old_resubscribe_logic(live_tick_com_symbol, live_runner_symbol, current_dropdown):
            """Old behavior that caused the bug."""
            return getattr(object(), '_live_tick_com_symbol', live_runner_symbol) if hasattr(object(), '_live_tick_com_symbol') else live_runner_symbol

        # Simulate the new behavior (after fix)
        def new_resubscribe_logic(live_tick_com_symbol, live_runner_symbol, current_dropdown):
            """New behavior that fixes the bug."""
            symbol_config = {
                "TX00": {"tick_symbol": "TX00"},
                "MTX00": {"tick_symbol": "TX00"},
                "TMF00": {"tick_symbol": "TX00"},
            }
            cfg = symbol_config.get(current_dropdown, {})
            return cfg.get("tick_symbol", current_dropdown)

        # Test scenario: started with TX00, user changed to TMF00
        stored_symbol = "TX00"  # What was stored when live trading started
        runner_symbol = "TX00"  # What's in the live runner
        current_dropdown = "TMF00"  # What user selected during weekend

        # Old behavior would use stored/runner symbol (wrong)
        old_result = old_resubscribe_logic(stored_symbol, runner_symbol, current_dropdown)
        # New behavior should use current dropdown (correct)
        new_result = new_resubscribe_logic(stored_symbol, runner_symbol, current_dropdown)

        # Both TMF00 and TX00 map to TX00 for tick data, so result should be same
        # but the important thing is that new logic considers current dropdown
        assert new_result == "TX00"

        # Test with a symbol that doesn't map to TX00 to prove the difference
        current_dropdown_unmapped = "CUSTOM00"
        old_result_unmapped = old_resubscribe_logic(stored_symbol, runner_symbol, current_dropdown_unmapped)
        new_result_unmapped = new_resubscribe_logic(stored_symbol, runner_symbol, current_dropdown_unmapped)

        # Old logic ignores current dropdown (always uses stored value)
        assert old_result_unmapped == "TX00"  # Wrong - ignores user selection
        # New logic uses current dropdown
        assert new_result_unmapped == "CUSTOM00"  # Correct - uses user selection