"""Tests for the news circuit-breaker signal reader (src/news/signal_file.py).

The signal file is untrusted input and acting on a STALE one is the
expensive failure — a 40-minute-old "risk_off" describes a move that has
already happened. These tests pin the rejection boundaries and the
exactly-once ledger.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from src.news.signal_file import (
    CLOCK_SKEW_SEC,
    SignalLedger,
    read_signal,
)

TPE = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 4, 22, 5, tzinfo=TPE)


def write_signal(tmp_path, name="news_signal.json", **overrides):
    payload = {
        "version": 1,
        "signal_id": "sig-0001",
        "issued_at": NOW.isoformat(),
        "action": "risk_off",
        "severity": "high",
        "source": "crossmarket-sox",
        "reason": "SOX -3.2% intraday",
    }
    payload.update(overrides)
    for k in [k for k, v in payload.items() if v is None]:
        del payload[k]
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


# ── read_signal: happy path ────────────────────────────────────────────

def test_valid_signal_parses(tmp_path):
    sig, reason = read_signal(write_signal(tmp_path), NOW)
    assert reason == ""
    assert sig.signal_id == "sig-0001"
    assert sig.action == "risk_off"
    assert sig.severity == "high"
    assert sig.source == "crossmarket-sox"
    assert sig.reason == "SOX -3.2% intraday"
    assert sig.issued_at.tzinfo is not None


def test_all_tier_actions_accepted(tmp_path):
    for action in ("risk_off", "deploy_short", "deploy_long", "clear"):
        path = write_signal(tmp_path, action=action)
        sig, reason = read_signal(path, NOW)
        assert sig is not None and sig.action == action, reason


def test_optional_fields_default_to_empty(tmp_path):
    path = write_signal(tmp_path, severity=None, source=None, reason=None)
    sig, _ = read_signal(path, NOW)
    assert (sig.severity, sig.source, sig.reason) == ("", "", "")


# ── read_signal: rejections ────────────────────────────────────────────

def test_stale_signal_rejected_at_boundary(tmp_path):
    issued = NOW - timedelta(seconds=900)
    path = write_signal(tmp_path, issued_at=issued.isoformat())
    sig, reason = read_signal(path, NOW, max_age_sec=900)
    assert sig is None and "stale" in reason
    # one second inside the window is still actionable
    fresh = write_signal(tmp_path, issued_at=(NOW - timedelta(seconds=899)).isoformat())
    assert read_signal(fresh, NOW, max_age_sec=900)[0] is not None


def test_future_dated_beyond_skew_rejected(tmp_path):
    beyond = write_signal(
        tmp_path, issued_at=(NOW + timedelta(seconds=CLOCK_SKEW_SEC + 1)).isoformat())
    sig, reason = read_signal(beyond, NOW)
    assert sig is None and "future" in reason
    # small host-clock drift is tolerated
    within = write_signal(
        tmp_path, issued_at=(NOW + timedelta(seconds=CLOCK_SKEW_SEC - 1)).isoformat())
    assert read_signal(within, NOW)[0] is not None


def test_naive_issued_at_rejected(tmp_path):
    path = write_signal(tmp_path, issued_at="2026-08-04T22:05:00")
    sig, reason = read_signal(path, NOW)
    assert sig is None
    assert "timezone" in reason


def test_other_offsets_compared_in_utc(tmp_path):
    """A UTC stamp of the same instant must not read as 8 hours stale."""
    issued_utc = (NOW - timedelta(seconds=60)).astimezone(timezone.utc)
    path = write_signal(tmp_path, issued_at=issued_utc.isoformat())
    sig, reason = read_signal(path, NOW, max_age_sec=900)
    assert sig is not None, reason


def test_unknown_action_rejected(tmp_path):
    path = write_signal(tmp_path, action="liquidate_everything")
    sig, reason = read_signal(path, NOW)
    assert sig is None and "unknown action" in reason


def test_missing_signal_id_rejected(tmp_path):
    for bad in (None, "", "   ", 12345):
        path = write_signal(tmp_path, signal_id=bad)
        sig, reason = read_signal(path, NOW)
        assert sig is None and "signal_id" in reason


def test_wrong_version_rejected(tmp_path):
    path = write_signal(tmp_path, version=2)
    sig, reason = read_signal(path, NOW)
    assert sig is None and "version" in reason


def test_malformed_json_rejected_without_raising(tmp_path):
    path = tmp_path / "news_signal.json"
    path.write_text("{not json at all", encoding="utf-8")
    sig, reason = read_signal(str(path), NOW)
    assert sig is None and reason


def test_missing_file_and_empty_path(tmp_path):
    assert read_signal(str(tmp_path / "nope.json"), NOW)[0] is None
    assert read_signal("", NOW) == (None, "no signal_path configured")
    assert read_signal(None, NOW)[0] is None


def test_naive_now_is_treated_as_taipei(tmp_path):
    path = write_signal(tmp_path)
    sig, reason = read_signal(path, NOW.replace(tzinfo=None))
    assert sig is not None, reason


# ── SignalLedger: exactly-once ─────────────────────────────────────────

def test_same_signal_acted_on_once(tmp_path):
    path = write_signal(tmp_path)
    ledger = SignalLedger(str(tmp_path / "ledger.json"))
    acted = 0
    for _ in range(2):       # the 30s poll re-reads the same file
        sig, _reason = read_signal(path, NOW)
        assert sig is not None
        if not ledger.is_consumed(sig.signal_id):
            ledger.mark_consumed(sig.signal_id)
            acted += 1
    assert acted == 1


def test_ledger_survives_reload_from_disk(tmp_path):
    ledger_path = str(tmp_path / "ledger.json")
    SignalLedger(ledger_path).mark_consumed("sig-0001")

    reloaded = SignalLedger(ledger_path)     # process restart
    assert reloaded.is_consumed("sig-0001")
    assert not reloaded.is_consumed("sig-0002")


def test_mark_consumed_is_idempotent(tmp_path):
    ledger_path = str(tmp_path / "ledger.json")
    ledger = SignalLedger(ledger_path)
    ledger.mark_consumed("sig-0001")
    ledger.mark_consumed("sig-0001")
    stored = json.loads(open(ledger_path, encoding="utf-8").read())
    assert stored["consumed"] == ["sig-0001"]


def test_ledger_caps_at_200_fifo(tmp_path):
    ledger_path = str(tmp_path / "ledger.json")
    ledger = SignalLedger(ledger_path)
    for i in range(250):
        ledger.mark_consumed(f"sig-{i:04d}")

    stored = json.loads(open(ledger_path, encoding="utf-8").read())
    assert len(stored["consumed"]) == 200
    assert stored["consumed"][0] == "sig-0050"    # oldest 50 evicted
    assert stored["consumed"][-1] == "sig-0249"

    reloaded = SignalLedger(ledger_path)
    assert not reloaded.is_consumed("sig-0049")   # evicted from memory too
    assert reloaded.is_consumed("sig-0249")


def test_atomic_write_leaves_valid_json_and_no_tmp(tmp_path):
    ledger_path = str(tmp_path / "ledger.json")
    ledger = SignalLedger(ledger_path)
    ledger.mark_consumed("sig-0001")
    ledger.mark_consumed("sig-0002")

    with open(ledger_path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["version"] == 1
    assert data["consumed"] == ["sig-0001", "sig-0002"]
    assert not os.path.exists(ledger_path + ".tmp")


def test_corrupted_ledger_starts_empty(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    ledger_path.write_text("{truncated", encoding="utf-8")
    ledger = SignalLedger(str(ledger_path))
    assert not ledger.is_consumed("sig-0001")
    ledger.mark_consumed("sig-0001")     # and recovers on the next write
    assert SignalLedger(str(ledger_path)).is_consumed("sig-0001")
