"""W2 session-stamp freshness — Yahoo can re-stamp a closed market (#137).

On Sun 2026-09-20 Yahoo served ``^TWII`` with ``regularMarketTime`` ~20 min
old (inside the 1500 s Asian allowance) while ``currentTradingPeriod.regular``
and every 1-min bar still belonged to Friday 09-18.  ``max_age`` alone
accepted the quote; Friday's +2.90% re-alerted.

These tests are built to FAIL against the pre-fix collector, which trusts
``regularMarketTime`` only.  Nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import io
import json
import time
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parents[1]
           / "scripts" / "news_bridge" / "crossmarket_monitor.py")


def _load_monitor():
    spec = importlib.util.spec_from_file_location(
        "crossmarket_monitor_session", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = _load_monitor()

TPE = timezone(timedelta(hours=8))
FROZEN_NOW = datetime(2026, 9, 20, 10, 26, tzinfo=TPE)

# Sunday phantom: stamp looks 20 min old; the session it claims ended
# two days earlier.  Matches the 09-20 Yahoo v8 payload shape.
TWO_DAYS = 2 * 86400
PHANTOM_STAMP_AGE = 1200
PHANTOM_PERIOD_END_AGE = TWO_DAYS          # Fri 13:30 vs Sun ~13:30-ish
PHANTOM_PERIOD_START_AGE = TWO_DAYS + 4 * 3600 + 1800  # Fri 09:00
PHANTOM_LAST_BAR_AGE = TWO_DAYS + 60       # Fri 13:29


def quote(pct: float, age_sec: float = 0.0) -> dict:
    prev = 100.0
    return {
        "price": prev * (1 + pct / 100.0),
        "prev_close": prev,
        "pct": pct,
        "quote_time": time.time() - age_sec,
    }


def session_quote(
    pct: float,
    age_sec: float,
    *,
    period_start_age: float,
    period_end_age: float,
    last_bar_age: float,
    now: float | None = None,
) -> dict:
    """Yahoo-shaped quote with session bounds + last 1-min bar."""
    now = time.time() if now is None else now
    q = quote(pct, age_sec)
    q["quote_time"] = now - age_sec
    q["period_start"] = now - period_start_age
    q["period_end"] = now - period_end_age
    q["last_bar_time"] = now - last_bar_age
    return q


def phantom_twii(pct: float = 2.90) -> dict:
    """The #137 Sunday ^TWII payload: live-looking stamp, Friday tape."""
    return session_quote(
        pct, PHANTOM_STAMP_AGE,
        period_start_age=PHANTOM_PERIOD_START_AGE,
        period_end_age=PHANTOM_PERIOD_END_AGE,
        last_bar_age=PHANTOM_LAST_BAR_AGE,
    )


def live_us_cash(pct: float = 0.4) -> dict:
    """Open US cash: stamp, last bar and regular period all ~now."""
    return session_quote(
        pct, 30,
        period_start_age=3600,
        period_end_age=-2 * 3600,
        last_bar_age=30,
    )


def live_asian_cash(pct: float = 0.8) -> dict:
    """Open Asian cash on the delayed feed (~20 min old, period still open)."""
    return session_quote(
        pct, 1200,
        period_start_age=4 * 3600,
        period_end_age=-1800,
        last_bar_age=1260,
    )


def live_nq_globex(pct: float = -0.4) -> dict:
    """NQ=F during Globex: ~10 min delayed, near-24h regular period."""
    return session_quote(
        pct, 610,
        period_start_age=12 * 3600,
        period_end_age=-10 * 3600,
        last_bar_age=670,
    )


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


def read_json(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ── 1. the Sunday payload is not fresh (#137) ──────────────────────────

def test_phantom_twii_sunday_stamp_is_excluded_from_fresh_breaches_and_vote(
        tmp_path, market, capsys):
    """FAILS pre-fix: age 1200 s < ^TWII max_age 1500 s, so +2.90% is fresh.

    Payload shape from the 09-20 Yahoo v8 chart: regularMarketTime ≈ now
    − 1200 s, currentTradingPeriod.regular + every bar two days earlier.
    """
    market["^TWII"] = phantom_twii(+2.90)
    args = make_args(tmp_path)

    fresh, breaches, details, _fail = cm._collect_quotes(args)
    direction, up_syms, down_syms = cm._vote_direction(fresh)

    assert "^TWII" not in fresh, "phantom-fresh ^TWII must not join the book"
    assert "^TWII" not in details
    assert breaches["alert_up"] == []
    assert all("^TWII" not in line for hits in breaches.values() for line in hits)
    assert direction is None
    assert "^TWII" not in up_syms and "^TWII" not in down_syms
    captured = capsys.readouterr().out
    assert "STALE — stamp outside trading period" in captured


# ── 2. quorum: phantom + one real breach must not vote (#137) ──────────

def test_phantom_twii_plus_fresh_n225_does_not_write_a_vote(
        tmp_path, market, discord, frozen_clock):
    """FAILS pre-fix: both symbols look fresh and breach up → trending-up.

    Latent #137 impact: a phantom ^TWII plus one genuinely fresh
    breaching symbol meets VOTE_MIN_SYMBOLS=2.
    """
    market["^TWII"] = phantom_twii(+2.90)
    market["^N225"] = live_asian_cash(+2.10)
    vote_base = tmp_path / "regime_vote.json"
    vote_path = tmp_path / "regime_vote_w2.json"
    args = make_args(tmp_path, vote_out=str(vote_base))

    state = cm.check_once(args, {})

    assert read_json(vote_path) is None, "phantom member must not make the quorum"
    assert "vote:2026-09-19" not in state
    assert not any("Regime vote" in m for m in discord)
    # The live Nikkei still announces on its own (alert tier, one symbol).
    assert any("^N225" in m for m in discord)


# ── 3. live markets still pass (#137 regressions) ──────────────────────

@pytest.mark.parametrize("symbol,builder,expect_fresh", [
    ("SOXX", live_us_cash, True),
    ("^N225", live_asian_cash, True),
    ("NQ=F", live_nq_globex, True),
])
def test_live_us_asian_and_nq_still_pass_freshness(
        tmp_path, market, symbol, builder, expect_fresh):
    market[symbol] = builder()
    args = make_args(tmp_path)

    fresh, breaches, details, _fail = cm._collect_quotes(args)

    assert (symbol in fresh) is expect_fresh
    assert any(symbol in line for line in details) is expect_fresh


def test_nq_globex_survives_a_cash_like_period_when_the_tape_is_live(
        tmp_path, market):
    """Yahoo may report a short regular period for NQ=F while Globex prints.

    Period ended hours ago; last bar and stamp are ~10 min old.  Trust the
    tape, not the period (issue #137 — do not false-reject futures).
    """
    q = live_nq_globex(-0.4)
    q["period_start"] = time.time() - 20 * 3600
    q["period_end"] = time.time() - 8 * 3600          # closed 8 h ago
    market["NQ=F"] = q
    args = make_args(tmp_path)

    fresh, _breaches, details, _fail = cm._collect_quotes(args)

    assert "NQ=F" in fresh
    assert any("NQ=F" in line for line in details)


def test_closing_auction_print_just_after_period_end_is_still_fresh(
        tmp_path, market):
    """^N225 stamps ~15 min after regular end (14:45 vs 14:30).

    Additional reject, not a replacement for max_age — a just-closed
    market whose stamp is inside end+grace and still under 1500 s stays.
    """
    q = session_quote(
        -2.4, 900,
        period_start_age=5 * 3600 + 900,
        period_end_age=900 + 15 * 60,   # period ended 15 min before the stamp
        last_bar_age=960,
    )
    market["^N225"] = q
    args = make_args(tmp_path)

    fresh, _breaches, details, _fail = cm._collect_quotes(args)

    assert "^N225" in fresh


def test_ks11_official_close_hours_after_last_bar_uses_max_age_not_period(
        tmp_path, market):
    """^KS11 has been seen at 19:05 vs period end 14:00 (~5 h).

    6 h grace keeps the official-close stamp; max_age on that stamp
    still decides freshness.  A 2-minute-old 19:05 print must count.
    """
    five_h = 5 * 3600
    q = session_quote(
        -2.4, 120,
        period_start_age=five_h + 5 * 3600,
        period_end_age=five_h,
        last_bar_age=five_h + 60,
    )
    market["^KS11"] = q
    args = make_args(tmp_path)

    fresh, _breaches, details, _fail = cm._collect_quotes(args)

    assert "^KS11" in fresh


def test_missing_period_and_bars_falls_back_to_max_age(tmp_path, market, capsys):
    """No session metadata → today's max_age behaviour (logged)."""
    market["QQQ"] = quote(-0.2, age_sec=30)          # no period / bars
    args = make_args(tmp_path)

    fresh, _breaches, details, _fail = cm._collect_quotes(args)

    assert "QQQ" in fresh
    captured = capsys.readouterr().out
    assert "session metadata missing — freshness by max_age only" in captured


def test_missing_metadata_still_ages_out_via_max_age(tmp_path, market):
    market["QQQ"] = quote(-3.0, age_sec=1200)        # 600 s US allowance
    args = make_args(tmp_path)

    fresh, _breaches, details, _fail = cm._collect_quotes(args)

    assert "QQQ" not in fresh


# ── parser: Yahoo v8 body yields the session fields ────────────────────

def _yahoo_body(now: float) -> dict:
    price, prev = 47180.75, 45848.90
    stamp = int(now - PHANTOM_STAMP_AGE)
    start = int(now - PHANTOM_PERIOD_START_AGE)
    end = int(now - PHANTOM_PERIOD_END_AGE)
    last = int(now - PHANTOM_LAST_BAR_AGE)
    return {
        "chart": {
            "result": [{
                "meta": {
                    "regularMarketPrice": price,
                    "chartPreviousClose": prev,
                    "regularMarketTime": stamp,
                    "currentTradingPeriod": {
                        "regular": {"start": start, "end": end,
                                    "timezone": "CST", "gmtoffset": 28800},
                    },
                },
                "timestamp": [start, start + 60, last],
            }],
        },
    }


def test_parse_chart_quote_exposes_session_fields():
    now = time.time()
    result = _yahoo_body(now)["chart"]["result"][0]
    q = cm.parse_chart_quote(result)

    assert q["pct"] == pytest.approx(2.90, abs=0.005)
    assert q["quote_time"] == int(now - PHANTOM_STAMP_AGE)
    assert q["period_start"] == int(now - PHANTOM_PERIOD_START_AGE)
    assert q["period_end"] == int(now - PHANTOM_PERIOD_END_AGE)
    assert q["last_bar_time"] == int(now - PHANTOM_LAST_BAR_AGE)


def test_fetch_quote_reads_session_fields_from_yahoo_payload(monkeypatch):
    now = time.time()
    body = _yahoo_body(now)

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        cm.urllib.request, "urlopen",
        lambda *a, **k: _Resp(json.dumps(body).encode("utf-8")))

    q = cm.fetch_quote("^TWII")
    assert q is not None
    assert q["period_end"] == int(now - PHANTOM_PERIOD_END_AGE)
    assert q["last_bar_time"] == int(now - PHANTOM_LAST_BAR_AGE)
    assert q["pct"] == pytest.approx(2.90, abs=0.005)


def test_session_stale_reason_names_the_period_reject():
    reason = cm.session_stale_reason(phantom_twii(), max_age=1500)
    assert reason == "STALE — stamp outside trading period"


def test_session_stale_reason_none_for_live_us():
    assert cm.session_stale_reason(live_us_cash(), max_age=600) is None


def test_ignore_freshness_still_accepts_the_phantom(tmp_path, market):
    market["^TWII"] = phantom_twii(+2.90)
    args = make_args(tmp_path, ignore_freshness=True)

    fresh, breaches, _details, _fail = cm._collect_quotes(args)

    assert "^TWII" in fresh
    assert any("2.90" in line for line in breaches["alert_up"])
