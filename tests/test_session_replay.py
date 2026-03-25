"""Session replay tests — audit recorded decisions from real bot sessions.

Reads decisions.csv from session folders to catch framework bugs:
1. Float exit prices (ATR-computed stops that don't match real ticks)
2. Duplicate TRADE_CLOSE (off-by-one _bar_index causes ghost on next bar)
3. Float limit/stop in EXIT_ORDER (should be rounded before use)

These tests require NO strategy code — they audit the recorded CSV directly.
Drop a session folder (session.json + decisions.csv + bars_1m_*.csv) into
tests/fixtures/sessions/ and it becomes a test case automatically.
"""

import csv
import json
import os
from collections import defaultdict
from pathlib import Path

import pytest


# ── Session discovery ──

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "sessions"

# Sessions recorded BEFORE the fix — contain known bugs.
# These are tested with inverted assertions (expect failures) to prove
# the audit catches real production bugs.
KNOWN_BUGGY = {"TX00_andy"}


def _discover_sessions():
    """Find all session folders with decisions.csv."""
    if not FIXTURES_DIR.exists():
        return []
    sessions = []
    for d in sorted(FIXTURES_DIR.iterdir()):
        if d.is_dir() and (d / "decisions.csv").exists():
            sessions.append(d)
    return sessions


def _is_known_buggy(session_dir: Path) -> bool:
    return session_dir.name in KNOWN_BUGGY


def _load_decisions(session_dir: Path) -> list[dict]:
    """Parse decisions.csv into a list of dicts."""
    decisions = []
    csv_path = session_dir / "decisions.csv"
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Parse price as float to detect non-integer values
            try:
                row["_price_float"] = float(row.get("price", "0"))
            except (ValueError, TypeError):
                row["_price_float"] = 0.0
            decisions.append(row)
    return decisions


def _load_session_config(session_dir: Path) -> dict:
    """Load session.json config."""
    with open(session_dir / "session.json", encoding="utf-8") as f:
        return json.load(f)


# ── Parametrize over all discovered sessions ──

_sessions = _discover_sessions()
_session_ids = [s.name for s in _sessions]


@pytest.fixture(params=_sessions, ids=_session_ids)
def session(request):
    """Provides (session_dir, decisions, config) for each test session."""
    d = request.param
    return d, _load_decisions(d), _load_session_config(d)


# ── Level 1: Decision Audit (pure CSV analysis) ──

class TestDecisionAudit:
    """Audit recorded decisions for framework bugs.

    These tests catch bugs that already happened in production —
    if they fail, the recorded session contains a known bug pattern.
    """

    def test_no_float_exit_prices(self, session):
        """TRADE_CLOSE prices must be valid integers (no ATR floats)."""
        session_dir, decisions, config = session
        float_exits = []
        for d in decisions:
            if d["action"] == "TRADE_CLOSE":
                price = d["_price_float"]
                if price != 0 and price != int(price):
                    float_exits.append(
                        f"{d['datetime']} {d['tag']} price={d['price']}")

        if _is_known_buggy(session_dir):
            assert float_exits, "Known-buggy session should have float exits"
        else:
            assert not float_exits, (
                f"Float exit prices in {session_dir.name}:\n"
                + "\n".join(f"  {e}" for e in float_exits)
            )

    def test_no_duplicate_trade_close(self, session):
        """Each trade should trigger exactly one TRADE_CLOSE."""
        session_dir, decisions, config = session
        trade_closes = [d for d in decisions if d["action"] == "TRADE_CLOSE"]

        # Group by (tag, pnl_from_reason) — duplicates have same tag and PnL
        seen = defaultdict(list)
        for d in trade_closes:
            # Extract PnL from reason field (e.g. "PnL=-36400" or "tick exit PnL=-36400")
            reason = d.get("reason", "")
            pnl = ""
            if "PnL=" in reason:
                pnl = reason.split("PnL=")[-1].strip()
            key = (d["tag"], pnl)
            seen[key].append(d["datetime"])

        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        if _is_known_buggy(session_dir):
            assert duplicates, "Known-buggy session should have duplicates"
        else:
            assert not duplicates, (
                f"Duplicate TRADE_CLOSE in {session_dir.name}:\n"
                + "\n".join(
                    f"  tag={k[0]} PnL={k[1]} fired {len(v)} times: {v}"
                    for k, v in duplicates.items()
                )
            )

    def test_no_float_stop_limit_in_exit_order(self, session):
        """EXIT_ORDER limit/stop should be rounded to valid tick prices."""
        session_dir, decisions, config = session
        float_orders = []
        for d in decisions:
            if d["action"] == "EXIT_ORDER":
                reason = d.get("reason", "")
                # Parse limit=... stop=... from reason
                for part in reason.split():
                    if "=" in part:
                        key, val = part.split("=", 1)
                        try:
                            fval = float(val)
                            if fval != 0 and fval != int(fval):
                                float_orders.append(
                                    f"{d['datetime']} {key}={val}")
                        except ValueError:
                            pass

        if _is_known_buggy(session_dir):
            assert float_orders, "Known-buggy session should have float orders"
        else:
            assert not float_orders, (
                f"Float stop/limit in EXIT_ORDER in {session_dir.name}:\n"
                + "\n".join(f"  {o}" for o in float_orders)
            )

    def test_entry_prices_are_integers(self, session):
        """ENTRY_FILL prices must be integers (bar close prices)."""
        session_dir, decisions, config = session
        float_entries = []
        for d in decisions:
            if d["action"] == "ENTRY_FILL":
                price = d["_price_float"]
                if price != 0 and price != int(price):
                    float_entries.append(
                        f"{d['datetime']} {d['tag']} price={d['price']}")

        assert not float_entries, (
            f"Float entry prices in {session_dir.name}:\n"
            + "\n".join(f"  {e}" for e in float_entries)
        )

    def test_force_close_price_is_integer(self, session):
        """FORCE_CLOSE prices must be integers."""
        session_dir, decisions, config = session
        float_closes = []
        for d in decisions:
            if d["action"] == "FORCE_CLOSE":
                price = d["_price_float"]
                if price != 0 and price != int(price):
                    float_closes.append(
                        f"{d['datetime']} price={d['price']}")

        assert not float_closes, (
            f"Float FORCE_CLOSE prices in {session_dir.name}:\n"
            + "\n".join(f"  {c}" for c in float_closes)
        )


# ── Level 2: Session metadata validation ──

class TestSessionMetadata:
    """Validate session.json consistency."""

    def test_trades_have_integer_prices(self, session):
        """All trade entry/exit prices in session.json must be integers."""
        session_dir, decisions, config = session
        broker = config.get("broker", {})
        float_trades = []
        for i, t in enumerate(broker.get("trades", [])):
            ep = t.get("entry_price", 0)
            xp = t.get("exit_price", 0)
            if ep != int(ep):
                float_trades.append(f"trade[{i}] entry_price={ep}")
            if xp != int(xp):
                float_trades.append(f"trade[{i}] exit_price={xp}")

        assert not float_trades, (
            f"Float prices in session.json trades:\n"
            + "\n".join(f"  {t}" for t in float_trades)
        )

    def test_trade_count_matches_decisions(self, session):
        """Number of trades in session.json should match TRADE_CLOSE count
        in decisions.csv (excluding duplicates)."""
        session_dir, decisions, config = session
        broker = config.get("broker", {})
        session_trades = len(broker.get("trades", []))

        # Count unique TRADE_CLOSE (by PnL to dedup)
        seen_pnls = set()
        unique_closes = 0
        for d in decisions:
            if d["action"] == "TRADE_CLOSE":
                reason = d.get("reason", "")
                pnl = reason.split("PnL=")[-1].strip() if "PnL=" in reason else ""
                key = (d["tag"], pnl)
                if key not in seen_pnls:
                    seen_pnls.add(key)
                    unique_closes += 1

        # Include FORCE_CLOSE as trades too
        force_closes = sum(1 for d in decisions if d["action"] == "FORCE_CLOSE")
        decision_trades = unique_closes + force_closes

        assert session_trades == decision_trades, (
            f"Trade count mismatch: session.json has {session_trades}, "
            f"decisions.csv has {decision_trades} "
            f"({unique_closes} TRADE_CLOSE + {force_closes} FORCE_CLOSE)"
        )
