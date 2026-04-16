"""Tests for src.utils.log_redact — mask account/client IDs in logs.

All account/client IDs used in these tests are synthetic placeholders
(all-9 / repeating patterns). Never paste real broker-issued IDs into
source code.
"""

from __future__ import annotations

import pytest

from src.utils.log_redact import (
    redact_acct,
    scrub_ids,
    redact_open_interest,
    redact_future_rights,
)


# Synthetic fixtures. Keep last-4-digits predictable so assertions are clear.
FAKE_ACCT = "F1111111112222"   # ends in 2222
FAKE_CLIENT = "L3333344444"    # ends in 4444


class TestRedactAcct:
    def test_long_id_keeps_last_4(self):
        assert redact_acct(FAKE_ACCT) == "***2222"

    def test_short_id_fully_masked(self):
        assert redact_acct("abc") == "***"

    def test_exactly_5_chars_keeps_4(self):
        assert redact_acct("12345") == "***2345"

    def test_empty_returns_empty(self):
        assert redact_acct("") == ""

    def test_none_returns_empty(self):
        assert redact_acct(None) == ""


class TestScrubIds:
    def test_masks_f_prefix(self):
        assert scrub_ids(f"acct={FAKE_ACCT} open") == "acct=F***2222 open"

    def test_masks_l_prefix(self):
        assert scrub_ids(f"client={FAKE_CLIENT} sent") == "client=L***4444 sent"

    def test_masks_multiple_ids_in_same_string(self):
        text = f"client={FAKE_CLIENT}, acct={FAKE_ACCT}"
        out = scrub_ids(text)
        assert FAKE_CLIENT not in out
        assert FAKE_ACCT not in out
        assert "L***4444" in out
        assert "F***2222" in out

    def test_does_not_mask_short_numeric_tokens(self):
        """5 digits minimum — words like LIVE, F4, L12 should not match."""
        assert scrub_ids("LIVE mode F4 L12") == "LIVE mode F4 L12"

    def test_does_not_mask_plain_words(self):
        assert scrub_ids("LONG position at 37200") == "LONG position at 37200"

    def test_preserves_prefix_letter(self):
        """Prefix L vs F must be kept so user can distinguish types."""
        assert scrub_ids("L12345678").startswith("L***")
        assert scrub_ids("F12345678").startswith("F***")

    def test_empty_returns_empty(self):
        assert scrub_ids("") == ""

    def test_none_returns_empty(self):
        assert scrub_ids(None) == ""


class TestRedactOpenInterest:
    def test_masks_account_field(self):
        raw = f"TF,{FAKE_ACCT},TM04,B,1,0,37220.00,,,{FAKE_CLIENT}"
        out = redact_open_interest(raw)
        # Both IDs should be masked somewhere in the output
        assert FAKE_ACCT not in out
        assert FAKE_CLIENT not in out
        # Last 4 digits of each should still be visible
        assert "2222" in out
        assert "4444" in out
        # Structure preserved — same number of commas
        assert out.count(",") == raw.count(",")

    def test_empty_sentinel_passthrough(self):
        """Empty `##,,,,,...` rows shouldn't crash."""
        raw = "##,,,,,,,,,,,,,,,,,,,,"
        out = redact_open_interest(raw)
        assert out == raw  # nothing to redact

    def test_error_code_passthrough(self):
        raw = "001,查無資料"
        out = redact_open_interest(raw)
        assert out == raw  # no IDs present

    def test_none_input(self):
        assert redact_open_interest(None) == ""


class TestRedactFutureRights:
    def test_masks_tail_ids(self):
        # Payload shape matching OnFutureRights format (synthetic IDs).
        raw = (
            "29951,130,105,49,0,0,30081,6231,0,0,0,-1490,130,23850,18300,"
            "23850,18300,0,6231,30081,0,0,31595,,126,NTD,23850,18300,6101,"
            f"0,0,6101,6101,0,125,0,0,0,0,{FAKE_CLIENT},{FAKE_ACCT}"
        )
        out = redact_future_rights(raw)
        assert FAKE_CLIENT not in out
        assert FAKE_ACCT not in out
        assert "L***4444" in out
        assert "F***2222" in out

    def test_empty_sentinel_passthrough(self):
        assert redact_future_rights("##,,,,,,,,,,,") == "##,,,,,,,,,,,"

    def test_none_input(self):
        assert redact_future_rights(None) == ""


class TestPartiallyRedactedPayloads:
    """When an earlier redaction pass already masked field 1, any
    L-prefix client ID in a later field must still get scrubbed."""

    def test_open_interest_with_partial_redaction(self):
        raw = f"TF,***2222,TM05,B,1,0,37220.00,,,{FAKE_CLIENT}"
        out = redact_open_interest(raw)
        assert FAKE_CLIENT not in out
        assert "L***4444" in out

    def test_future_rights_leaves_no_known_id_patterns(self):
        raw = (
            "29951,130,105,49,0,0,30081,6231,0,0,0,-1490,130,23850,18300,"
            "23850,18300,0,6231,30081,0,0,31595,,126,NTD,23850,18300,6101,"
            f"0,0,6101,6101,0,125,0,0,0,0,{FAKE_CLIENT},{FAKE_ACCT}"
        )
        out = redact_future_rights(raw)
        for leak in (FAKE_CLIENT, FAKE_ACCT):
            assert leak not in out, f"{leak} leaked in output: {out}"
