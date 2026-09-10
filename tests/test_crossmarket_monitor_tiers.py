"""W2 re-tier: the vote quorum gates ``risk_off``, ASML.AS drops to alert.

Over the window the nightly regime lane actually consumes a W2 vote
(fired during the US session, read at the 04:58 classification the next
morning) W2 was right 3 times in 10.  The fire review traced most of the
misses to a SINGLE signal-tier symbol tripping on its own — ASML.AS
worst of all at 20% win.  So:

- ``risk_off`` now requires the quorum ``_vote_direction`` already
  computes: >= VOTE_MIN_SYMBOLS fresh symbols breaching down and none
  breaching up.  A lone breach is an alert, not a circuit breaker.
- ``ASML.AS`` moves to the alert tier.  It keeps its vote thresholds —
  it still counts toward the quorum, it just cannot fire on its own.

These tests use the same by-path loader / monkeypatched-network harness
as ``test_crossmarket_monitor.py``; nothing here touches the network.
"""

import importlib.util
import json
import time
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parents[1]
           / "scripts" / "news_bridge" / "crossmarket_monitor.py")


def _load_monitor():
    spec = importlib.util.spec_from_file_location("crossmarket_monitor", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = _load_monitor()

TPE = timezone(timedelta(hours=8))
FROZEN_NOW = datetime(2026, 8, 5, 22, 30, tzinfo=TPE)


def quote(pct: float, age_sec: float = 0.0) -> dict:
    prev = 100.0
    return {
        "price": prev * (1 + pct / 100.0),
        "prev_close": prev,
        "pct": pct,
        "quote_time": time.time() - age_sec,
    }


@pytest.fixture
def market(monkeypatch):
    book: dict[str, dict | None] = {}
    monkeypatch.setattr(cm, "fetch_quote", lambda sym: book.get(sym))
    return book


@pytest.fixture
def discord(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(cm, "post_discord", lambda hook, msg: sent.append(msg))
    return sent


@pytest.fixture
def frozen_clock(monkeypatch):
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW.replace(tzinfo=None)

    monkeypatch.setattr(cm, "datetime", _Frozen)
    return FROZEN_NOW


def make_args(tmp_path, **overrides):
    args = Namespace(
        signal_out=str(tmp_path / "signal.json"),
        vote_out=None,
        min_move=None,
        ignore_freshness=False,
        discord_webhook="https://discord.invalid/webhook",
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def signal_of(args):
    p = Path(args.signal_out)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ── (a) a lone signal-tier breach no longer flattens the book ──────────

def test_single_signal_tier_breach_without_quorum_alerts_only(
        tmp_path, market, discord, frozen_clock):
    """FAILS pre-change: SOXX -3% alone used to write risk_off."""
    market["SOXX"] = quote(-3.0)              # breaches signal -2.5 AND vote -2.5
    market["QQQ"] = quote(-0.4)               # fresh, nowhere near either threshold
    market["^N225"] = quote(0.2)
    args = make_args(tmp_path)

    state = cm.check_once(args, {})

    assert signal_of(args) is None, "one symbol is not a quorum"
    assert "down:2026-08-05" not in state, "the real-fire dedup slot stays free"
    assert state["signal-alert-down:2026-08-05"] == "alerted"
    alert = "".join(discord)
    assert "SOXX" in alert
    assert "quorum not met" in alert and "未達票數門檻" in alert


def test_no_quorum_alert_does_not_suppress_a_later_real_fire(
        tmp_path, market, discord, frozen_clock):
    """The alert-only key must never consume the risk_off dedup slot."""
    market["SOXX"] = quote(-3.0)
    args = make_args(tmp_path)
    state = cm.check_once(args, {})
    assert signal_of(args) is None

    market["QQQ"] = quote(-2.6)               # quorum arrives on a later poll
    cm.check_once(args, state)

    sig = signal_of(args)
    assert sig is not None and sig["action"] == "risk_off"


# ── (b) quorum met → risk_off, bearish ─────────────────────────────────

def test_two_symbols_down_writes_risk_off(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-3.0)              # vote down -2.5
    market["QQQ"] = quote(-2.5)               # vote down -2.0
    args = make_args(tmp_path)

    state = cm.check_once(args, {})

    sig = signal_of(args)
    assert sig is not None
    assert sig["action"] == "risk_off"
    assert sig["direction"] == "bearish"
    assert "SOXX" in sig["reason"]
    assert state["down:2026-08-05"] == sig["signal_id"]


def test_quorum_may_come_from_an_alert_tier_symbol(
        tmp_path, market, discord, frozen_clock):
    """Alert-tier symbols cannot fire, but they can supply the second vote."""
    market["SOXX"] = quote(-3.0)              # signal tier — the trigger
    market["ASML.AS"] = quote(-3.2)           # alert tier — the corroboration
    args = make_args(tmp_path)

    cm.check_once(args, {})

    sig = signal_of(args)
    assert sig is not None and sig["action"] == "risk_off"
    assert "SOXX" in sig["reason"]


# ── (c) a fresh contradiction vetoes the fire ──────────────────────────

def test_contradicting_up_breach_blocks_risk_off(
        tmp_path, market, discord, frozen_clock):
    """FAILS pre-change: SOXX -3% fired regardless of what else was doing."""
    market["SOXX"] = quote(-3.0)              # vote down
    market["QQQ"] = quote(-2.6)               # vote down — quorum would be met...
    market["^KS11"] = quote(2.5)              # ...but a fresh symbol breaks UP
    args = make_args(tmp_path)

    state = cm.check_once(args, {})

    assert signal_of(args) is None, "a fresh dissenter vetoes the fire"
    assert "down:2026-08-05" not in state
    assert any("quorum not met" in m for m in discord)


# ── (d) ASML.AS is no longer a signal-tier symbol ──────────────────────

def test_asml_is_alert_tier(tmp_path):
    """FAILS pre-change: ASML.AS used to sit in the signal tier."""
    assert cm.SYMBOLS["ASML.AS"]["tier"] == "alert"
    signal_tier = {s for s, c in cm.SYMBOLS.items() if c["tier"] == "signal"}
    assert signal_tier == {"SOXX", "TSM", "QQQ"}
    # ...but it keeps its vote thresholds, so it still counts to the quorum.
    assert cm.VOTE_THRESHOLDS["ASML.AS"] == (-3.0, 3.0)


def test_asml_alone_writes_no_signal(tmp_path, market, discord, frozen_clock):
    """FAILS pre-change: ASML.AS -3.4% used to auto-write risk_off (20% win)."""
    market["ASML.AS"] = quote(-3.4)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    assert signal_of(args) is None
    assert any("ASML.AS" in m for m in discord), "the breach is still announced"


def test_two_alert_tier_symbols_still_write_nothing(
        tmp_path, market, discord, frozen_clock):
    """Quorum is necessary, not sufficient: the trigger must be signal tier."""
    market["ASML.AS"] = quote(-3.4)
    market["^N225"] = quote(-2.5)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    assert signal_of(args) is None


# ── the quorum is computed once and reused ─────────────────────────────

def test_fire_and_vote_agree_on_the_same_quorum(
        tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-3.0)
    market["QQQ"] = quote(-2.6)
    args = make_args(tmp_path, vote_out=str(tmp_path / "regime_vote.json"))

    cm.check_once(args, {})

    vote = json.loads((tmp_path / "regime_vote_w2.json").read_text(encoding="utf-8"))
    assert vote["direction"] == "trending-down"
    assert signal_of(args)["action"] == "risk_off"


def test_no_quorum_writes_neither_signal_nor_vote(
        tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-3.0)
    args = make_args(tmp_path, vote_out=str(tmp_path / "regime_vote.json"))

    cm.check_once(args, {})

    assert signal_of(args) is None
    assert not (tmp_path / "regime_vote_w2.json").exists()


def test_log_line_marks_the_no_quorum_pass(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-3.0)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    line = (tmp_path / "monitor.log").read_text(encoding="utf-8").splitlines()[0]
    assert "fired: signal-down(no-quorum)" in line
    assert "vote: -" in line
