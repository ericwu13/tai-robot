"""Tests for the W4 chips monitor bridge (scripts/news_bridge/).

W4 turns TAIFEX 外資 futures positioning into regime votes.  The signal
is the DAY-OVER-DAY CHANGE in net OI (ΔOI), not the level — 外資 carry a
permanent structural hedge short (~-80,000 contracts), so the level is
past every threshold in the short direction every single day.  The vote
can accelerate a regime flip, so the no-vote branches are the safety
surface: the missing/stale ΔOI baseline, the hysteresis band, the
futures-vs-equity contradiction check (hedging), the weak-ΔOI equity
confirmation, and the holiday/outage no-op that also deletes any stale
vote file.

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


# ── compute_chips_direction: every branch (the input is ΔOI) ────────────

def test_strong_delta_up_votes_up_from_a_structurally_short_book():
    """The point of the Δ rewrite: 外資's net OI LEVEL is ~-80,000 every
    day, so a level-based rule can only ever vote down.  A one-day build
    of +8,000 contracts is bullish no matter where the level sits."""
    assert chips.compute_chips_direction(8_000, 0.0) == "trending-up"
    assert chips.compute_chips_direction(8_000, 4.9e9) == "trending-up"


def test_strong_delta_down_votes_down():
    assert chips.compute_chips_direction(-8_000, 0.0) == "trending-down"
    assert chips.compute_chips_direction(-8_000, -4.9e9) == "trending-down"


def test_hysteresis_band_no_vote():
    assert chips.compute_chips_direction(999, 0.0) is None
    assert chips.compute_chips_direction(-999, 0.0) is None
    assert chips.compute_chips_direction(0, 9e9) is None


def test_weak_delta_with_agreeing_equity_flow_votes():
    assert chips.compute_chips_direction(2_000, 6e9) == "trending-up"
    assert chips.compute_chips_direction(-2_000, -6e9) == "trending-down"


def test_weak_delta_without_equity_confirmation_no_vote():
    """Equity flow inside the neutral band is not a witness — a weak ΔOI
    on its own is noise.  (The old slope check is gone: it compared the
    change against itself once the input became the change.)"""
    assert chips.compute_chips_direction(2_000, 0.0) is None
    assert chips.compute_chips_direction(2_000, 4.9e9) is None
    assert chips.compute_chips_direction(-2_000, -4.9e9) is None


def test_weak_delta_with_opposing_equity_flow_is_a_contradiction():
    assert chips.compute_chips_direction(2_000, -6e9) is None
    assert chips.compute_chips_direction(-2_000, 6e9) is None


def test_contradiction_futures_short_equity_buy_no_vote():
    """Today's live shape: 外資 add TX shorts on a day they net-bought
    equities — a hedge adjustment, not a directional view."""
    assert chips.compute_chips_direction(-8_000, 6e9) is None


def test_contradiction_futures_long_equity_sell_no_vote():
    assert chips.compute_chips_direction(8_000, -6e9) is None


def test_equity_flow_inside_neutral_band_is_not_a_contradiction():
    assert chips.compute_chips_direction(8_000, -4.9e9) == "trending-up"
    assert chips.compute_chips_direction(-8_000, 4.9e9) == "trending-down"


def test_equity_flow_agreeing_confirms_vote():
    assert chips.compute_chips_direction(8_000, 6e9) == "trending-up"


def test_decide_reasons_name_the_delta():
    assert chips.decide(8_000, 0.0)[1] == "strong: |ΔOI| >= 5,000"
    assert chips.decide(2_000, 6e9)[1] == "weak ΔOI but equity flow agrees"
    assert "hysteresis band" in chips.decide(500, 0.0)[1]
    assert "no confirming equity flow" in chips.decide(2_000, 0.0)[1]
    assert "contradiction" in chips.decide(-8_000, 6e9)[1]


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


def _seed_history(base, history):
    """Pre-write the OI sidecar.  ΔOI needs a baseline — without one every
    pass abstains, so a flow test that asserts a VOTE must seed one."""
    chips.save_state(chips.state_path_for(base), {"history": dict(history)})


def test_run_once_writes_valid_vote_file(monkeypatch, base):
    _seed_history(base, {"20260813": 0.0})
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    assert chips.run_once(base, "20260814", RUN_NOW) == 0

    vote = read_json(chips.w4_vote_path(base))
    # write_regime_vote also stamps fired_at (nightly-lane age gate) — a
    # wall-clock value, so it is checked for presence, not equality.
    assert vote.pop("fired_at", "").startswith("20")
    assert vote == {
        "version": 1,
        "direction": "trending-up",
        "expires_after_session": "2026-08-14|NIGHT",
        "source": "W4",
        "data_date": "20260814",
    }


def test_run_once_no_data_deletes_stale_vote(monkeypatch, base):
    _seed_history(base, {"20260813": 0.0})
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    assert chips.w4_vote_path(base).exists()

    # Next day is a holiday — TAIFEX has nothing; yesterday's vote must go.
    _patch_fetch(monkeypatch, None, None)
    assert chips.run_once(base, "20260815", RUN_NOW) == 0
    assert not chips.w4_vote_path(base).exists()


def test_run_once_no_vote_deletes_stale_vote(monkeypatch, base):
    _seed_history(base, {"20260813": 0.0})
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    assert chips.w4_vote_path(base).exists()

    # Next day barely moves (ΔOI +500) — hysteresis band, stale vote goes.
    _patch_fetch(monkeypatch, 8_500.0, 6e9)
    chips.run_once(base, "20260815", RUN_NOW)
    assert not chips.w4_vote_path(base).exists()


def test_run_once_twse_unavailable_is_conservative_no_vote(monkeypatch, base):
    """Strong ΔOI, but the contradiction check can't run."""
    _seed_history(base, {"20260813": 0.0})
    _patch_fetch(monkeypatch, 8_000.0, None)
    chips.run_once(base, "20260814", RUN_NOW)
    assert not chips.w4_vote_path(base).exists()
    # OI is still recorded so tomorrow's ΔOI works.
    state = chips.load_state(chips.state_path_for(base))
    assert state["history"]["20260814"] == 8_000.0


def test_run_once_delta_uses_previous_day_state(monkeypatch, base):
    # Day 1: no baseline yet → abstain by design, but OI recorded.
    _patch_fetch(monkeypatch, -83_655.0, 6e9)
    chips.run_once(base, "20260813", RUN_NOW)
    assert not chips.w4_vote_path(base).exists()

    # Day 2: still deeply net short in LEVEL terms, but the book moved
    # +8,000 contracts toward long — that is the signal.
    _patch_fetch(monkeypatch, -75_655.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    vote = read_json(chips.w4_vote_path(base))
    assert vote["direction"] == "trending-up"


def test_run_once_same_day_rerun_does_not_use_own_oi_as_prev(monkeypatch, base):
    """Re-running for the same date must compare against the PRIOR day,
    not the value the first run just recorded (ΔOI would always be 0)."""
    _seed_history(base, {"20260813": 0.0})
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    assert read_json(chips.w4_vote_path(base))["direction"] == "trending-up"
    chips.w4_vote_path(base).unlink()

    # Re-run 08-14 (force — dedup would skip it): prev must still be
    # 08-13's 0, so ΔOI is +8,000 again.  Reading the 8,000 recorded
    # moments ago would make ΔOI 0 and silence the vote.
    chips.run_once(base, "20260814", RUN_NOW, force=True)
    assert read_json(chips.w4_vote_path(base))["direction"] == "trending-up"
    state = chips.load_state(chips.state_path_for(base))
    assert state["history"] == {"20260813": 0.0, "20260814": 8_000.0}


def test_run_once_dry_run_writes_and_deletes_nothing(monkeypatch, base):
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW, dry_run=True)
    assert not chips.w4_vote_path(base).exists()
    assert not chips.state_path_for(base).exists()

    # A stale vote survives a dry run even when the decision is no-vote.
    _seed_history(base, {"20260813": 0.0})
    chips.run_once(base, "20260814", RUN_NOW)
    assert chips.w4_vote_path(base).exists()
    _patch_fetch(monkeypatch, 8_000.0, 0.0)   # next day: ΔOI 0 → hysteresis
    chips.run_once(base, "20260815", RUN_NOW, dry_run=True)
    assert chips.w4_vote_path(base).exists()


def test_run_once_stamps_liveness_on_data_day(monkeypatch, base):
    _seed_history(base, {"20260813": 0.0})
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
    _seed_history(base, {"20260813": 0.0})
    _patch_fetch(monkeypatch, 500.0, 6e9)       # ΔOI +500 → hysteresis
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


# ── default trade date: the 04:15 catch-up slot ─────────────────────────

def test_default_trade_date_daytime_is_today():
    assert chips.default_trade_date(datetime(2026, 8, 24, 16, 15, tzinfo=TPE)) \
        == "20260824"
    assert chips.default_trade_date(datetime(2026, 8, 24, 22, 15, tzinfo=TPE)) \
        == "20260824"
    assert chips.default_trade_date(datetime(2026, 8, 24, 5, 0, tzinfo=TPE)) \
        == "20260824"


def test_default_trade_date_pre_dawn_is_previous_day():
    """The 04:15 catch-up runs inside the night session that opened
    YESTERDAY — it wants yesterday's report (today's doesn't exist yet).
    Old behavior (always today) made the catch-up slot request a date the
    API can never have, so it would log 'no data' forever."""
    assert chips.default_trade_date(datetime(2026, 8, 25, 4, 15, tzinfo=TPE)) \
        == "20260824"
    assert chips.default_trade_date(datetime(2026, 8, 25, 0, 30, tzinfo=TPE)) \
        == "20260824"
    # Consistency: the trade date and the vote's session key must name the
    # same day, or the vote written at 04:15 would expire-mismatch.
    at = datetime(2026, 8, 25, 4, 15, tzinfo=TPE)
    assert chips.default_trade_date(at)[:4] + "-" + \
        chips.default_trade_date(at)[4:6] + "-" + chips.default_trade_date(at)[6:] \
        == chips.night_session_key(at).split("|")[0]


# ── retry-slot dedup: one decision per trade date ───────────────────────

def test_retry_after_decision_is_a_noop(monkeypatch, base):
    """The 18:15/20:15/22:15/04:15 slots re-fire after a successful
    16:15 decision — they must not re-fetch or re-post."""
    calls = []
    _seed_history(base, {"20260821": 0.0})      # Friday baseline
    monkeypatch.setattr(chips, "fetch_taifex_foreign_net_oi",
                        lambda d: calls.append(d) or 8_000.0)
    monkeypatch.setattr(chips, "fetch_twse_foreign_net_buy", lambda d: 6e9)
    chips.run_once(base, "20260824", RUN_NOW)
    assert len(calls) == 1
    assert read_json(chips.w4_vote_path(base))["direction"] == "trending-up"

    # Same date fires again (later slot) → no fetch, vote untouched.
    chips.run_once(base, "20260824", RUN_NOW)
    assert len(calls) == 1
    assert read_json(chips.w4_vote_path(base))["direction"] == "trending-up"


def test_no_vote_decision_also_dedups(monkeypatch, base):
    """A deliberate no-vote (data fetched, hysteresis band) is final for
    the date — later slots must not re-fetch either."""
    calls = []
    _seed_history(base, {"20260821": 0.0})
    monkeypatch.setattr(chips, "fetch_taifex_foreign_net_oi",
                        lambda d: calls.append(d) or 500.0)
    monkeypatch.setattr(chips, "fetch_twse_foreign_net_buy", lambda d: 0.0)
    chips.run_once(base, "20260824", RUN_NOW)
    chips.run_once(base, "20260824", RUN_NOW)
    assert len(calls) == 1


def test_no_data_pass_does_not_dedup(monkeypatch, base):
    """The whole point of the retry slots: 16:15 finds the OpenAPI still
    serving the previous day (= no data), a later slot must retry and
    win.  Old behavior had no retries; with dedup-on-no-data the retries
    would be useless."""
    _seed_history(base, {"20260821": 0.0})
    _patch_fetch(monkeypatch, None, None)
    chips.run_once(base, "20260824", RUN_NOW)           # 16:15 — API lagging

    _patch_fetch(monkeypatch, 8_000.0, 6e9)             # 20:15 — data landed
    chips.run_once(base, "20260824", RUN_NOW)
    assert read_json(chips.w4_vote_path(base))["direction"] == "trending-up"


def test_new_trade_date_is_not_deduped(monkeypatch, base):
    _seed_history(base, {"20260821": 0.0})
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260824", RUN_NOW)
    _patch_fetch(monkeypatch, -8_000.0, -6e9)           # ΔOI -16,000
    chips.run_once(base, "20260825", RUN_NOW)
    assert read_json(chips.w4_vote_path(base))["direction"] == "trending-down"


def test_twse_outage_no_vote_stays_retryable(monkeypatch, base):
    """A transient TWSE outage must not burn the whole day's vote — the
    next slot retries and completes the contradiction check."""
    _seed_history(base, {"20260821": 0.0})
    _patch_fetch(monkeypatch, 8_000.0, None)            # TWSE down this slot
    chips.run_once(base, "20260824", RUN_NOW)
    assert not chips.w4_vote_path(base).exists()

    _patch_fetch(monkeypatch, 8_000.0, 6e9)             # next slot: TWSE back
    chips.run_once(base, "20260824", RUN_NOW)
    assert read_json(chips.w4_vote_path(base))["direction"] == "trending-up"


def test_force_redoes_a_decided_date(monkeypatch, base):
    calls = []
    _seed_history(base, {"20260821": 0.0})
    monkeypatch.setattr(chips, "fetch_taifex_foreign_net_oi",
                        lambda d: calls.append(d) or 8_000.0)
    monkeypatch.setattr(chips, "fetch_twse_foreign_net_buy", lambda d: 6e9)
    chips.run_once(base, "20260824", RUN_NOW)
    chips.run_once(base, "20260824", RUN_NOW, force=True)
    assert len(calls) == 2


def test_dry_run_neither_dedups_nor_marks_decided(monkeypatch, base):
    _seed_history(base, {"20260821": 0.0})
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260824", RUN_NOW)           # decided
    # Dry-run inspection of a decided date must still show the data...
    chips.run_once(base, "20260824", RUN_NOW, dry_run=True)
    # ...and a dry run must never mark a date decided.
    state = chips.load_state(chips.state_path_for(base))
    assert state["last_decided_date"] == "20260824"


# ── scheduled path: the dataset's own date decides ──────────────────────
#
# The OpenAPI has no date parameter — it serves ONE dataset whose lag is
# unbounded.  Pre-picking today's date meant a dataset that landed after
# the calendar moved on was never requested again (W4 voteless 08-19..).

MON = datetime(2026, 8, 31, 16, 15, tzinfo=TPE)   # Monday 16:15 slot
SAT = datetime(2026, 8, 29, 16, 15, tzinfo=TPE)   # Saturday retry slot
FRI_DATA = "20260828"                             # dataset TAIFEX is serving
PREV_DATA = "20260827"                            # Thursday — ΔOI baseline
PREV_OI = -83_655.0                               # 外資's structural short
DATA_OI = PREV_OI + 8_000                         # ΔOI +8,000 → strong up


def _patch_dataset(monkeypatch, dataset_date, net_oi=DATA_OI, equity=6e9):
    """Mock the served TAIFEX dataset (date + OI) and the TWSE fetch."""
    monkeypatch.setattr(
        chips, "fetch_taifex_tx_foreign_oi",
        lambda: None if dataset_date is None else (dataset_date, net_oi))
    monkeypatch.setattr(chips, "fetch_twse_foreign_net_buy", lambda d: equity)


def test_scheduled_run_votes_a_lagging_dataset_for_tonight(monkeypatch, base):
    """Monday daytime, API still on Friday's report: vote it for TONIGHT."""
    asked = []
    _seed_history(base, {PREV_DATA: PREV_OI})
    _patch_dataset(monkeypatch, FRI_DATA)
    monkeypatch.setattr(chips, "fetch_twse_foreign_net_buy",
                        lambda d: asked.append(d) or 6e9)
    assert chips.run_once(base, None, MON) == 0

    vote = read_json(chips.w4_vote_path(base))
    assert vote["direction"] == "trending-up"
    assert vote["expires_after_session"] == "2026-08-31|NIGHT"
    assert vote["data_date"] == FRI_DATA
    # The equity contradiction check must use the DATA date, not today.
    assert asked == [FRI_DATA]
    state = chips.load_state(chips.state_path_for(base))
    assert state["last_decided_date"] == FRI_DATA
    assert f"(data {FRI_DATA})" in state["last_result"]


def test_scheduled_run_does_not_revote_a_decided_dataset(monkeypatch, base):
    calls = []
    _seed_history(base, {PREV_DATA: PREV_OI})
    monkeypatch.setattr(chips, "fetch_taifex_tx_foreign_oi",
                        lambda: calls.append(1) or (FRI_DATA, DATA_OI))
    monkeypatch.setattr(chips, "fetch_twse_foreign_net_buy", lambda d: 6e9)
    chips.run_once(base, None, MON)
    before = chips.load_state(chips.state_path_for(base))

    # 20:15 slot — same dataset, nothing new: no re-decision, no re-vote,
    # but liveness IS restamped (a deduped pass is a healthy pass; without
    # the stamp a long weekend + API lag false-flags "W4 bridge stale").
    assert chips.run_once(base, None,
                          datetime(2026, 8, 31, 20, 15, tzinfo=TPE)) == 0
    after = chips.load_state(chips.state_path_for(base))
    assert after["last_decided_date"] == before["last_decided_date"]
    assert "nothing new" in after["last_result"]
    assert after["last_check"] != before["last_check"]
    assert len(calls) == 2          # fetched, then rejected as already decided


def test_weekend_run_defers_and_monday_still_votes(monkeypatch, base, capsys):
    """A Saturday pass targets a night session that never opens: voting it
    would expire unread AND dedup-block Monday's real vote."""
    _seed_history(base, {PREV_DATA: PREV_OI})
    _patch_dataset(monkeypatch, FRI_DATA)
    assert chips.run_once(base, None, SAT) == 0
    assert not chips.w4_vote_path(base).exists()
    assert "last_decided_date" not in chips.load_state(chips.state_path_for(base))
    assert "deferring" in capsys.readouterr().out

    assert chips.run_once(base, None, MON) == 0
    vote = read_json(chips.w4_vote_path(base))
    assert vote["expires_after_session"] == "2026-08-31|NIGHT"
    assert chips.load_state(chips.state_path_for(base))["last_decided_date"] \
        == FRI_DATA


def test_scheduled_run_ignores_dataset_older_than_walk_back(monkeypatch, base):
    _patch_dataset(monkeypatch, "20260820")     # 7 trading days behind
    assert chips.run_once(base, None, MON) == 0
    assert not chips.w4_vote_path(base).exists()
    state = chips.load_state(chips.state_path_for(base))
    assert "walk-back window" in state["last_result"]
    assert "last_decided_date" not in state     # still retryable


def test_scheduled_run_ignores_a_future_dataset(monkeypatch, base):
    _patch_dataset(monkeypatch, "20260902")     # absurd — defensive
    assert chips.run_once(base, None, MON) == 0
    assert not chips.w4_vote_path(base).exists()
    state = chips.load_state(chips.state_path_for(base))
    assert "ahead of trade date" in state["last_result"]
    assert "last_decided_date" not in state


def test_explicit_date_keeps_exact_match_behaviour(monkeypatch, base):
    """--date is a manual backfill: a dataset for another date is no data."""
    _seed_history(base, {PREV_DATA: 0.0})
    monkeypatch.setattr(
        chips, "_http_json",
        lambda url: [_taifex_row("臺股期貨", "外資", "8,123", date=FRI_DATA)])
    monkeypatch.setattr(chips, "fetch_twse_foreign_net_buy", lambda d: 6e9)

    assert chips.run_once(base, "20260831", MON) == 0    # asked for Monday
    assert not chips.w4_vote_path(base).exists()

    assert chips.run_once(base, FRI_DATA, MON) == 0
    assert read_json(chips.w4_vote_path(base))["direction"] == "trending-up"


# ── ΔOI baseline: missing, stale, and weekend-gap ───────────────────────
#
# The futures input is the day-over-day CHANGE, so a pass without a
# recent baseline has no signal at all — it records and abstains.

def test_first_pass_without_history_abstains_but_records(monkeypatch, base):
    """ΔOI is undefined on the first pass after a deploy: no vote, but the
    baseline the next pass needs is laid down, and liveness is stamped."""
    _patch_fetch(monkeypatch, -83_655.0, 6e9)
    assert chips.run_once(base, "20260814", RUN_NOW) == 0
    assert not chips.w4_vote_path(base).exists()

    state = chips.load_state(chips.state_path_for(base))
    assert state["history"]["20260814"] == -83_655.0
    assert state["last_check"] == RUN_NOW.isoformat(timespec="seconds")
    assert "no recent OI history" in state["last_result"]
    assert "level -83,655" in state["last_result"]
    # Not a decision — a later slot may still find a baseline.
    assert "last_decided_date" not in state
    log = Path(base).parent / chips.LOG_NAME
    assert "no recent OI history" in log.read_text(encoding="utf-8")


def test_history_older_than_max_gap_is_treated_as_no_history(monkeypatch, base):
    """A stale baseline is worse than none: it would book a whole holiday
    run's drift as one day's ΔOI (here +6,345 → a bogus strong vote)."""
    _seed_history(base, {"20260806": -90_000.0})        # 8 calendar days back
    _patch_fetch(monkeypatch, -83_655.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    assert not chips.w4_vote_path(base).exists()

    state = chips.load_state(chips.state_path_for(base))
    assert "no recent OI history" in state["last_result"]
    assert state["history"]["20260814"] == -83_655.0    # re-seeded anyway


def test_history_exactly_at_max_gap_is_still_usable(monkeypatch, base):
    _seed_history(base, {"20260809": -90_000.0})        # 5 calendar days back
    _patch_fetch(monkeypatch, -83_655.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    assert read_json(chips.w4_vote_path(base))["direction"] == "trending-up"


def test_friday_baseline_for_a_monday_dataset_still_computes_delta(
        monkeypatch, base):
    """The weekend gap is 3 calendar days — inside MAX_PREV_GAP_DAYS — so
    Monday's dataset still gets a ΔOI, reported next to the level."""
    _seed_history(base, {"20260828": -83_655.0})        # Friday
    _patch_fetch(monkeypatch, -77_455.0, 6e9)           # Monday: ΔOI +6,200
    chips.run_once(base, "20260831", MON)
    assert read_json(chips.w4_vote_path(base))["direction"] == "trending-up"

    result = chips.load_state(chips.state_path_for(base))["last_result"]
    assert "ΔOI +6,200" in result
    assert "level -77,455" in result
    assert "prev 20260828" in result
    assert "(data 20260831)" in result
