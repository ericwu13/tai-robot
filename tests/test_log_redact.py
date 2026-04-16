"""Tests for src.utils.log_redact — mask account/client IDs in logs.

Covers real payload formats seen in production debug logs (issues from
users reporting leaked IDs even after v2.7.1).
"""

from __future__ import annotations

import pytest

from src.utils.log_redact import (
    redact_acct,
    scrub_ids,
    redact_open_interest,
    redact_future_rights,
)


class TestRedactAcct:
    def test_long_id_keeps_last_4(self):
        assert redact_acct("F1111111112222") == "***9366"

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
        assert scrub_ids("acct=F1111111112222 open") == "acct=F***9366 open"

    def test_masks_l_prefix(self):
        assert scrub_ids("client=L3333344444 sent") == "client=L***3388 sent"

    def test_masks_multiple_ids_in_same_string(self):
        text = "client=L3333344444, acct=F1111111112222"
        out = scrub_ids(text)
        assert "L3333344444" not in out
        assert "F1111111112222" not in out
        assert "L***3388" in out
        assert "F***9366" in out

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
        raw = "TF,F1111111112222,TM04,B,1,0,37220.00,,,L3333344444"
        out = redact_open_interest(raw)
        # Both IDs should be masked somewhere in the output
        assert "F1111111112222" not in out
        assert "L3333344444" not in out
        # Last 4 digits of each should still be visible
        assert "9366" in out
        assert "3388" in out
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
        # Real payload from v2.7.1 debug log — IDs at fields [-2] and [-1]
        raw = (
            "29951,130,105,49,0,0,30081,6231,0,0,0,-1490,130,23850,18300,"
            "23850,18300,0,6231,30081,0,0,31595,,126,NTD,23850,18300,6101,"
            "0,0,6101,6101,0,125,0,0,0,0,L3333344444,F1111111112222"
        )
        out = redact_future_rights(raw)
        assert "L3333344444" not in out
        assert "F1111111112222" not in out
        assert "L***3388" in out
        assert "F***9366" in out

    def test_empty_sentinel_passthrough(self):
        assert redact_future_rights("##,,,,,,,,,,,") == "##,,,,,,,,,,,"

    def test_none_input(self):
        assert redact_future_rights(None) == ""


class TestBugReportPayloads:
    """Regression test using the exact payloads from the user's
    complaint after v2.7.1."""

    def test_open_interest_from_real_log(self):
        raw = "TF,***9366,TM05,B,1,0,37220.00,,,L3333344444"
        # The first field is already redacted (from an earlier run),
        # but L3333344444 at the end must also be masked.
        out = redact_open_interest(raw)
        assert "L3333344444" not in out
        assert "L***3388" in out

    def test_future_rights_from_real_log(self):
        raw = (
            "29951,130,105,49,0,0,30081,6231,0,0,0,-1490,130,23850,18300,"
            "23850,18300,0,6231,30081,0,0,31595,,126,NTD,23850,18300,6101,"
            "0,0,6101,6101,0,125,0,0,0,0,L3333344444,F1111111112222"
        )
        out = redact_future_rights(raw)
        # Both identifiable sequences must be gone
        for leak in ("L3333344444", "F1111111112222"):
            assert leak not in out, f"{leak} leaked in output: {out}"
