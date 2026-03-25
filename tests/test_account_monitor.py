"""Tests for AccountMonitor — account tracking, P&L computation, fill parsing.

AccountMonitor is GUI-independent. These tests verify position aggregation,
signed position computation, display formatting, fill dedup, and parsing.
"""

from __future__ import annotations

import pytest

from src.live.account_monitor import (
    AccountMonitor,
    AccountDisplay,
    FillsResult,
    parse_open_interest,
    parse_future_rights,
    fmt_money,
)


# ── parse_open_interest ──

class TestParseOpenInterest:

    def test_valid(self):
        raw = "F,ACCOUNT,TXF4,B,2,0,22500.0,120,0.0002,USER"
        p = parse_open_interest(raw)
        assert p is not None
        assert p["product"] == "TXF4"
        assert p["side"] == "B"
        assert p["qty"] == 2
        assert p["avg_cost"] == "22500.0"

    def test_short_position(self):
        raw = "F,ACCOUNT,TM04,S,1,0,22300.0,60,0.0002,USER"
        p = parse_open_interest(raw)
        assert p["side"] == "S"
        assert p["qty"] == 1

    def test_error_code_001(self):
        assert parse_open_interest("001,no data") is None

    def test_error_code_970(self):
        assert parse_open_interest("970,error,,,,,,,,,") is None

    def test_error_code_980(self):
        assert parse_open_interest("980,error,,,,,,,,,") is None

    def test_too_few_fields(self):
        assert parse_open_interest("a,b,c") is None

    def test_invalid_qty(self):
        raw = "F,ACCOUNT,TXF4,B,notanum,0,22500.0,120,0.0002,USER"
        assert parse_open_interest(raw) is None


# ── parse_future_rights ──

class TestParseFutureRights:

    def _make_rights_str(self, **overrides):
        """Build a 35-field comma-separated rights string."""
        fields = ["0"] * 35
        mapping = {
            "balance": 0, "float_pnl": 1, "realized_cost": 2, "tax": 3,
            "equity": 6, "excess_margin": 7, "realized_pnl": 11,
            "unrealized": 12, "orig_margin": 13, "maint_margin": 14,
            "maint_rate": 24, "currency": 25, "available": 31, "risk": 34,
        }
        for k, v in overrides.items():
            fields[mapping[k]] = str(v)
        return ",".join(fields)

    def test_valid(self):
        raw = self._make_rights_str(
            equity="500000", available="300000", float_pnl="-2000",
            realized_pnl="5000", realized_cost="100", tax="50",
            maint_rate="150.5",
        )
        p = parse_future_rights(raw)
        assert p is not None
        assert p["equity"] == "500000"
        assert p["available"] == "300000"
        assert p["float_pnl"] == "-2000"
        assert p["maint_rate"] == "150.5"

    def test_sentinel_hash(self):
        assert parse_future_rights("##,end") is None

    def test_error_970(self):
        raw = "970" + ",0" * 34
        assert parse_future_rights(raw) is None

    def test_too_few_fields(self):
        assert parse_future_rights(",".join(["0"] * 10)) is None


# ── fmt_money ──

class TestFmtMoney:

    def test_integer(self):
        assert fmt_money("50000") == "50,000"

    def test_float(self):
        assert fmt_money("1234.56") == "1,234.56"

    def test_negative(self):
        assert fmt_money("-3000") == "-3,000"

    def test_invalid(self):
        assert fmt_money("--") == "--"

    def test_zero(self):
        assert fmt_money("0") == "0"


# ── AccountMonitor position tracking ──

class TestPositionTracking:

    def test_add_and_get_signed_long(self):
        m = AccountMonitor()
        m.add_position({"product": "TXF4", "side": "B", "qty": 2, "avg_cost": "22500"})
        assert m.get_signed_position("TX") == 2

    def test_add_and_get_signed_short(self):
        m = AccountMonitor()
        m.add_position({"product": "TM04", "side": "S", "qty": 1, "avg_cost": "22300"})
        assert m.get_signed_position("TM") == -1

    def test_flat_no_positions(self):
        m = AccountMonitor()
        assert m.get_signed_position("TX") == 0

    def test_empty_prefix(self):
        m = AccountMonitor()
        m.add_position({"product": "TXF4", "side": "B", "qty": 1, "avg_cost": "22500"})
        assert m.get_signed_position("") == 0

    def test_no_match(self):
        m = AccountMonitor()
        m.add_position({"product": "TXF4", "side": "B", "qty": 1, "avg_cost": "22500"})
        assert m.get_signed_position("TM") == 0

    def test_clear_positions(self):
        m = AccountMonitor()
        m.add_position({"product": "TXF4", "side": "B", "qty": 1, "avg_cost": "22500"})
        m.clear_positions()
        assert m.get_signed_position("TX") == 0
        assert m.positions == []

    def test_set_flat(self):
        m = AccountMonitor()
        m.add_position({"product": "TXF4", "side": "B", "qty": 1, "avg_cost": "22500"})
        m.set_flat()
        assert m.positions == []

    def test_reset(self):
        m = AccountMonitor()
        m.add_position({"product": "TXF4", "side": "B", "qty": 1, "avg_cost": "22500"})
        m.update_rights({"equity": "500000"})
        m.reset()
        assert m.positions == []
        assert m.rights == {}

    def test_multiple_positions_first_match(self):
        """get_signed_position returns the first matching position."""
        m = AccountMonitor()
        m.add_position({"product": "TXF4", "side": "B", "qty": 2, "avg_cost": "22500"})
        m.add_position({"product": "TXG4", "side": "B", "qty": 1, "avg_cost": "22600"})
        assert m.get_signed_position("TX") == 2


# ── AccountMonitor.compute_display ──

class TestComputeDisplay:

    def _make_monitor(self, positions=None, rights=None):
        m = AccountMonitor()
        for p in (positions or []):
            m.add_position(p)
        if rights:
            m.update_rights(rights)
        return m

    def test_no_data(self):
        d = AccountMonitor().compute_display()
        assert d.position_text == ""
        assert d.equity == "--"
        assert not d.valid

    def test_positions_only(self):
        m = self._make_monitor(
            positions=[{"product": "TXF4", "side": "B", "qty": 1, "avg_cost": "22500"}])
        d = m.compute_display()
        assert "LONG x1 TXF4 @22500" in d.position_text
        assert not d.valid  # no rights

    def test_full_display(self):
        m = self._make_monitor(
            positions=[{"product": "TXF4", "side": "S", "qty": 1, "avg_cost": "22500"}],
            rights={
                "equity": "500000", "available": "300000",
                "float_pnl": "-2000", "realized_pnl": "5000",
                "realized_cost": "100", "tax": "50",
                "maint_rate": "150.5",
            },
        )
        d = m.compute_display()
        assert "SHORT x1" in d.position_text
        assert d.equity == "500,000"
        assert d.available == "300,000"
        assert d.valid
        # net = 5000 - 100 - 50 + (-2000) = 2850
        assert d.net_pnl_int == 2850
        assert d.net_pnl == "+2,850"
        assert "150" in d.fees  # 100+50=150

    def test_malformed_rights(self):
        """Non-numeric rights values produce invalid display gracefully."""
        m = self._make_monitor(rights={
            "equity": "500000", "available": "300000",
            "float_pnl": "bad", "realized_pnl": "5000",
            "realized_cost": "100", "tax": "50",
            "maint_rate": "150",
        })
        d = m.compute_display()
        assert not d.valid
        assert d.net_pnl == "--"

    def test_maint_rate(self):
        m = self._make_monitor(rights={"maint_rate": "200.3"})
        d = m.compute_display()
        assert d.maint_rate == "200.3%"


# ── AccountMonitor.parse_fills ──

class TestParseFills:

    def _make_fill_line(self, side="B", price="22500.0", qty="1",
                        new_close="N", date="20260324"):
        """Build a 24-field comma-separated fill line."""
        fields = [""] * 24
        fields[15] = side
        fields[19] = price
        fields[20] = qty
        fields[21] = new_close
        fields[23] = date
        return ",".join(fields)

    def test_single_fill(self):
        m = AccountMonitor()
        raw = self._make_fill_line()
        r = m.parse_fills(raw, "成交")
        assert r.count == 1
        assert "BUY" in r.entries[0]
        assert "22,500.0" in r.entries[0]

    def test_sell_close_fill(self):
        m = AccountMonitor()
        raw = self._make_fill_line(side="S", new_close="O")
        r = m.parse_fills(raw, "成交")
        assert "SELL" in r.entries[0]
        assert "\u5e73" in r.entries[0]  # 平

    def test_multiple_fills(self):
        m = AccountMonitor()
        line1 = self._make_fill_line(price="22500.0")
        line2 = self._make_fill_line(side="S", price="22600.0")
        raw = line1 + "\n" + line2
        r = m.parse_fills(raw, "成交")
        assert r.count == 2

    def test_new_fill_detection(self):
        """Second parse call only reports new fills."""
        m = AccountMonitor()
        line1 = self._make_fill_line(price="22500.0")
        r1 = m.parse_fills(line1, "成交")
        assert len(r1.new_entries) == 1

        # Second call with 2 fills — only the new one is reported
        line2 = self._make_fill_line(price="22600.0")
        raw = line1 + "\n" + line2
        r2 = m.parse_fills(raw, "成交")
        assert r2.count == 2
        assert len(r2.new_entries) == 1
        assert "22,600.0" in r2.new_entries[0]

    def test_no_new_fills(self):
        """Same data twice — no new fills."""
        m = AccountMonitor()
        raw = self._make_fill_line()
        m.parse_fills(raw, "成交")
        r2 = m.parse_fills(raw, "成交")
        assert r2.new_entries == []

    def test_empty_raw(self):
        m = AccountMonitor()
        r = m.parse_fills("", "成交")
        assert r.count == 0

    def test_tuple_input(self):
        """COM may return (string, code) tuple."""
        m = AccountMonitor()
        raw = self._make_fill_line()
        r = m.parse_fills((raw, 0), "成交")
        assert r.count == 1

    def test_error_code_001(self):
        m = AccountMonitor()
        r = m.parse_fills("001,no data", "成交")
        assert r.count == 0

    def test_sentinel_hash(self):
        m = AccountMonitor()
        r = m.parse_fills("##,end", "成交")
        assert r.count == 0

    def test_separate_labels_track_independently(self):
        """Different labels have independent fill counters."""
        m = AccountMonitor()
        raw = self._make_fill_line()
        m.parse_fills(raw, "委託(已成)")
        r = m.parse_fills(raw, "成交(同商品)")
        assert len(r.new_entries) == 1  # first time for this label


# ── AccountMonitor.update_fill_poll_position ──

class TestFillPollPosition:

    def test_matching_position(self):
        m = AccountMonitor()
        parsed = {"product": "TXF4", "side": "B", "qty": 1}
        result = m.update_fill_poll_position("raw", parsed, "TX")
        assert result == 1

    def test_matching_short(self):
        m = AccountMonitor()
        parsed = {"product": "TM04", "side": "S", "qty": 2}
        result = m.update_fill_poll_position("raw", parsed, "TM")
        assert result == -2

    def test_no_match(self):
        m = AccountMonitor()
        parsed = {"product": "TXF4", "side": "B", "qty": 1}
        result = m.update_fill_poll_position("raw", parsed, "TM")
        assert result is None

    def test_flat_001(self):
        m = AccountMonitor()
        result = m.update_fill_poll_position("001,no data", None, "TX")
        assert result == 0

    def test_none_parsed_non_001(self):
        m = AccountMonitor()
        result = m.update_fill_poll_position("##,sentinel", None, "TX")
        assert result is None
