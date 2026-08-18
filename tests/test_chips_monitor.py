"""Tests for the W4 chips monitor bridge (scripts/news_bridge/).

W4 turns TAIFEX 外資 futures positioning into regime votes.  The vote can
accelerate a regime flip, so the no-vote branches are the safety surface:
the hysteresis band, the futures-vs-equity contradiction check (hedging),
the weak-signal slope confirmation, and the holiday/outage no-op that
also deletes any stale vote file.

The script lives under scripts/ and is not an importable package, so it
is loaded by path.  Both fetchers are monkeypatched in the flow tests —
nothing here touches the network.
"""

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_SCRIPT = (Path(__file__).resolve().parents[1]
           / "scripts" / "news_bridge" / "chips_monitor.py")


def _load_monitor():
    spec = importlib.util.spec_from_file_location("chips_monitor", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chips = _load_monitor()

TPE = ZoneInfo("Asia/Taipei")
# 16:15 TPE on a Friday — the n8n cron slot, inside tonight's night session.
RUN_NOW = datetime(2026, 8, 14, 16, 15, tzinfo=TPE)


def read_json(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ── compute_chips_direction: every branch ───────────────────────────────

def test_strong_long_votes_up_regardless_of_slope():
    assert chips.compute_chips_direction(8_000, 0.0, 9_500) == "trending-up"
    assert chips.compute_chips_direction(8_000, 0.0, None) == "trending-up"


def test_strong_short_votes_down_regardless_of_slope():
    assert chips.compute_chips_direction(-8_000, 0.0, -2_000) == "trending-down"
    assert chips.compute_chips_direction(-8_000, 0.0, None) == "trending-down"


def test_hysteresis_band_no_vote():
    assert chips.compute_chips_direction(999, 0.0, 0.0) is None
    assert chips.compute_chips_direction(-999, 0.0, 0.0) is None
    assert chips.compute_chips_direction(0, 9e9, 0.0) is None


def test_weak_signal_with_agreeing_slope_votes():
    assert chips.compute_chips_direction(2_000, 0.0, 1_000) == "trending-up"
    assert chips.compute_chips_direction(-2_000, 0.0, -1_000) == "trending-down"


def test_weak_signal_with_disagreeing_slope_no_vote():
    # Net long but shrinking — 外資 unwinding, not building.
    assert chips.compute_chips_direction(2_000, 0.0, 3_000) is None


def test_weak_signal_without_previous_oi_no_vote():
    assert chips.compute_chips_direction(2_000, 0.0, None) is None


def test_contradiction_futures_short_equity_buy_no_vote():
    """外資 net buying stocks while net short futures = hedging, not view."""
    assert chips.compute_chips_direction(-8_000, 6e9, None) is None


def test_contradiction_futures_long_equity_sell_no_vote():
    assert chips.compute_chips_direction(8_000, -6e9, None) is None


def test_equity_flow_inside_neutral_band_is_not_a_contradiction():
    assert chips.compute_chips_direction(8_000, -4.9e9, None) == "trending-up"
    assert chips.compute_chips_direction(-8_000, 4.9e9, None) == "trending-down"


def test_equity_flow_agreeing_confirms_vote():
    assert chips.compute_chips_direction(8_000, 6e9, None) == "trending-up"


# ── night_session_key: 05:00 boundary ───────────────────────────────────

def test_night_session_key_open_date():
    # Inside the night that opened 08-14 15:00.
    assert chips.night_session_key(datetime(2026, 8, 14, 22, 30, tzinfo=TPE)) \
        == "2026-08-14|NIGHT"
    # Post-midnight, still the SAME night session.
    assert chips.night_session_key(datetime(2026, 8, 15, 2, 0, tzinfo=TPE)) \
        == "2026-08-14|NIGHT"
    assert chips.night_session_key(datetime(2026, 8, 15, 4, 59, tzinfo=TPE)) \
        == "2026-08-14|NIGHT"
    # From 05:00 the vote targets TONIGHT's classification.
    assert chips.night_session_key(datetime(2026, 8, 15, 5, 0, tzinfo=TPE)) \
        == "2026-08-15|NIGHT"
    assert chips.night_session_key(datetime(2026, 8, 15, 10, 0, tzinfo=TPE)) \
        == "2026-08-15|NIGHT"


# ── fetcher parsing (HTTP mocked at _http_json) ─────────────────────────

def _taifex_row(product, ident, net_oi, date="20260814"):
    """Live API shape (verified 2026-08-15): English keys, Chinese values."""
    return {"Date": date, "ContractCode": product, "Item": ident,
            "OpenInterest(Net)": net_oi}


def test_taifex_parse_filters_product_and_identity(monkeypatch):
    rows = [
        _taifex_row("臺股期貨", "自營商", "1,111"),
        _taifex_row("臺股期貨", "投信", "2,222"),
        _taifex_row("臺股期貨", "外資及陸資", "8,123"),
        _taifex_row("小型臺股期貨", "外資及陸資", "99,999"),  # mini TX must be excluded
        _taifex_row("臺指選擇權", "外資及陸資", "77,777"),
    ]
    monkeypatch.setattr(chips, "_http_json", lambda url: rows)
    assert chips.fetch_taifex_foreign_net_oi("20260814") == 8_123.0


def test_taifex_parse_accepts_chinese_keys_and_slash_dates(monkeypatch):
    """Fallback shape: Chinese keys (swagger docs) and Y/M/D dates."""
    rows = [{"日期": "2026/08/14", "商品名稱": "臺股期貨", "身份別": "外資",
             "多空未平倉口數淨額": "-3,456"}]
    monkeypatch.setattr(chips, "_http_json", lambda url: rows)
    assert chips.fetch_taifex_foreign_net_oi("20260814") == -3_456.0


def test_taifex_date_mismatch_means_no_data(monkeypatch):
    """Weekend/holiday: TAIFEX serves the LAST trading day — must no-op."""
    rows = [_taifex_row("臺股期貨", "外資", "8,123", date="2026/08/14")]
    monkeypatch.setattr(chips, "_http_json", lambda url: rows)
    assert chips.fetch_taifex_foreign_net_oi("20260815") is None


def test_taifex_unreachable_or_empty(monkeypatch):
    monkeypatch.setattr(chips, "_http_json", lambda url: None)
    assert chips.fetch_taifex_foreign_net_oi("20260814") is None
    monkeypatch.setattr(chips, "_http_json", lambda url: [])
    assert chips.fetch_taifex_foreign_net_oi("20260814") is None


def _bfi82u(stat="OK", data=None):
    return {
        "stat": stat,
        "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
        "data": data or [],
    }


def test_twse_parse_sums_foreign_rows(monkeypatch):
    payload = _bfi82u(data=[
        ["自營商(自行買賣)", "1", "2", "-1,000"],
        ["投信", "1", "2", "2,000"],
        ["外資及陸資(不含外資自營商)", "1", "2", "12,345,678,901"],
        ["外資自營商", "1", "2", "-345,678,901"],
    ])
    monkeypatch.setattr(chips, "_http_json", lambda url: payload)
    assert chips.fetch_twse_foreign_net_buy("20260814") == 12_000_000_000.0


def test_twse_holiday_stat_not_ok(monkeypatch):
    monkeypatch.setattr(
        chips, "_http_json",
        lambda url: {"stat": "很抱歉，沒有符合條件的資料!"})
    assert chips.fetch_twse_foreign_net_buy("20260815") is None


# ── run_once flow (both fetchers mocked) ────────────────────────────────

@pytest.fixture
def base(tmp_path):
    return str(tmp_path / "regime_vote.json")


def _patch_fetch(monkeypatch, net_oi, equity):
    monkeypatch.setattr(chips, "fetch_taifex_foreign_net_oi", lambda d: net_oi)
    monkeypatch.setattr(chips, "fetch_twse_foreign_net_buy", lambda d: equity)


def test_run_once_writes_valid_vote_file(monkeypatch, base):
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    assert chips.run_once(base, "20260814", RUN_NOW) == 0

    vote = read_json(chips.w4_vote_path(base))
    assert vote == {
        "version": 1,
        "direction": "trending-up",
        "expires_after_session": "2026-08-14|NIGHT",
        "source": "W4",
    }


def test_run_once_no_data_deletes_stale_vote(monkeypatch, base):
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    assert chips.w4_vote_path(base).exists()

    # Next day is a holiday — TAIFEX has nothing; yesterday's vote must go.
    _patch_fetch(monkeypatch, None, None)
    assert chips.run_once(base, "20260815", RUN_NOW) == 0
    assert not chips.w4_vote_path(base).exists()


def test_run_once_no_vote_deletes_stale_vote(monkeypatch, base):
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    assert chips.w4_vote_path(base).exists()

    # Signal collapsed into the hysteresis band — stale vote must go.
    _patch_fetch(monkeypatch, 500.0, 6e9)
    chips.run_once(base, "20260815", RUN_NOW)
    assert not chips.w4_vote_path(base).exists()


def test_run_once_twse_unavailable_is_conservative_no_vote(monkeypatch, base):
    """Strong futures signal, but the contradiction check can't run."""
    _patch_fetch(monkeypatch, 8_000.0, None)
    chips.run_once(base, "20260814", RUN_NOW)
    assert not chips.w4_vote_path(base).exists()
    # OI is still recorded so tomorrow's slope check works.
    state = chips.load_state(chips.state_path_for(base))
    assert state["history"]["20260814"] == 8_000.0


def test_run_once_slope_uses_previous_day_state(monkeypatch, base):
    # Day 1: weak long, no history yet → no vote, but OI recorded.
    _patch_fetch(monkeypatch, 1_500.0, 0.0)
    chips.run_once(base, "20260813", RUN_NOW)
    assert not chips.w4_vote_path(base).exists()

    # Day 2: still weak, but OI grew vs day 1 → slope agrees → vote.
    _patch_fetch(monkeypatch, 2_500.0, 0.0)
    chips.run_once(base, "20260814", RUN_NOW)
    vote = read_json(chips.w4_vote_path(base))
    assert vote["direction"] == "trending-up"


def test_run_once_same_day_rerun_does_not_use_own_oi_as_prev(monkeypatch, base):
    """Re-running for the same date must compare against the PRIOR day,
    not the value the first run just recorded (slope would always be 0)."""
    _patch_fetch(monkeypatch, 3_000.0, 0.0)
    chips.run_once(base, "20260813", RUN_NOW)

    _patch_fetch(monkeypatch, 2_000.0, 0.0)  # weak, shrinking → no vote
    chips.run_once(base, "20260814", RUN_NOW)
    assert not chips.w4_vote_path(base).exists()

    # Re-run 08-14: prev must still be 08-13's 3,000 (shrinking → no vote),
    # not the 2,000 recorded moments ago (slope 0 → down would "agree").
    chips.run_once(base, "20260814", RUN_NOW)
    assert not chips.w4_vote_path(base).exists()
    state = chips.load_state(chips.state_path_for(base))
    assert state["history"] == {"20260813": 3_000.0, "20260814": 2_000.0}


def test_run_once_dry_run_writes_and_deletes_nothing(monkeypatch, base):
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW, dry_run=True)
    assert not chips.w4_vote_path(base).exists()
    assert not chips.state_path_for(base).exists()

    # A stale vote survives a dry run even when the decision is no-vote.
    chips.run_once(base, "20260814", RUN_NOW)
    assert chips.w4_vote_path(base).exists()
    _patch_fetch(monkeypatch, 0.0, 0.0)
    chips.run_once(base, "20260815", RUN_NOW, dry_run=True)
    assert chips.w4_vote_path(base).exists()


def test_run_once_stamps_liveness_on_data_day(monkeypatch, base):
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    state = chips.load_state(chips.state_path_for(base))
    assert state["last_check"] == RUN_NOW.isoformat(timespec="seconds")
    assert "vote: trending-up" in state["last_result"]


def test_run_once_stamps_liveness_on_no_data_day(monkeypatch, base):
    """A holiday/outage pass must still prove W4 ran — otherwise a dead
    bridge and a TAIFEX holiday are indistinguishable from disk."""
    _patch_fetch(monkeypatch, None, None)
    chips.run_once(base, "20260815", RUN_NOW)
    state = chips.load_state(chips.state_path_for(base))
    assert state["last_check"] == RUN_NOW.isoformat(timespec="seconds")
    assert "no TAIFEX data" in state["last_result"]
    log = Path(base).parent / chips.LOG_NAME
    assert log.exists()
    assert "no TAIFEX data" in log.read_text(encoding="utf-8")


def test_run_once_no_vote_still_stamps_liveness(monkeypatch, base):
    _patch_fetch(monkeypatch, 500.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    state = chips.load_state(chips.state_path_for(base))
    assert "vote: -" in state["last_result"]


def test_run_once_dry_run_never_stamps(monkeypatch, base):
    _patch_fetch(monkeypatch, None, None)
    chips.run_once(base, "20260814", RUN_NOW, dry_run=True)
    assert not chips.state_path_for(base).exists()


def test_state_history_trims_to_keep_days(monkeypatch, base):
    _patch_fetch(monkeypatch, 8_000.0, 0.0)
    for day in range(1, chips.STATE_KEEP_DAYS + 5):
        chips.run_once(base, f"202607{day:02d}", RUN_NOW)
    history = chips.load_state(chips.state_path_for(base))["history"]
    assert len(history) == chips.STATE_KEEP_DAYS
    assert min(history) == f"202607{5:02d}"
