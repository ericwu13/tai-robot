"""W2 reference-close cross-check (``verify_prev_close``).

Replays 2026-11-17 13:40 TPE: W2 logged ``^TWII -8.39%`` while TAIEX was
~-2.5% on the day.  -8.39% is the live price against FRIDAY 11/13's
close (43,675.12); Monday 11/16 (41,040.08, -6.03%) was skipped by the
reference Yahoo's 1-min chart carried.  No network: urlopen is faked.
"""

import importlib.util
import io
import json
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
FRI_CLOSE = 43675.12        # 2026-11-13
MON_CLOSE = 41040.08        # 2026-11-16 (-6.03%)
TUE_PRICE = 40010.78        # 2026-11-17 intraday; -8.39% vs FRI, -2.51% vs MON
TUE_STAMP = int(datetime(2026, 11, 17, 13, 25, tzinfo=TPE).timestamp())


def _ts(y, m, d, hh=9):
    return int(datetime(y, m, d, hh, 0, tzinfo=TPE).timestamp())


def _minute_body(prev=FRI_CLOSE):
    return {"chart": {"result": [{
        "meta": {"regularMarketPrice": TUE_PRICE, "chartPreviousClose": prev,
                 "regularMarketTime": TUE_STAMP, "gmtoffset": 28800,
                 "currentTradingPeriod": {"regular": {
                     "start": _ts(2026, 11, 17, 9),
                     "end": _ts(2026, 11, 17, 13) + 1800,
                     "gmtoffset": 28800}}},
        "timestamp": [_ts(2026, 11, 17, 9), TUE_STAMP - 60],
    }]}}


def _daily_body(rows):
    return {"chart": {"result": [{
        "meta": {"gmtoffset": 28800},
        "timestamp": [_ts(*d) for d, _ in rows],
        "indicators": {"quote": [{"close": [c for _, c in rows]}]},
    }]}}


DAILY_MISSING_MONDAY = [((2026, 11, 12), 44100.0), ((2026, 11, 13), FRI_CLOSE),
                        ((2026, 11, 17), TUE_PRICE)]
DAILY_WITH_MONDAY = [((2026, 11, 13), FRI_CLOSE), ((2026, 11, 16), MON_CLOSE),
                     ((2026, 11, 17), TUE_PRICE)]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def yahoo(monkeypatch):
    """Route fake Yahoo bodies by interval; record the URLs requested."""
    book = {"1m": _minute_body(), "1d": _daily_body(DAILY_MISSING_MONDAY)}
    calls: list[str] = []

    def _open(req, timeout=None):
        url = req.full_url
        calls.append(url)
        body = book["1d"] if "interval=1d" in url else book["1m"]
        if isinstance(body, Exception):
            raise body
        return _Resp(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(cm.urllib.request, "urlopen", _open)
    book["calls"] = calls
    return book


def test_unchecked_parse_reproduces_the_live_reading():
    q = cm.parse_chart_quote(_minute_body()["chart"]["result"][0])
    assert q["pct"] == pytest.approx(-8.39, abs=0.01)


def test_missing_weekday_in_daily_series_rejects(yahoo):
    q = cm.fetch_quote("^TWII")
    assert "SUSPECT prev close" in q["prev_reject"]
    assert "2026-11-13" in q["prev_reject"]


def test_daily_close_corrects_stale_chart_reference(yahoo):
    yahoo["1d"] = _daily_body(DAILY_WITH_MONDAY)
    q = cm.fetch_quote("^TWII")
    assert "prev_reject" not in q
    assert q["prev_close"] == pytest.approx(MON_CLOSE)
    assert q["pct"] == pytest.approx(-2.51, abs=0.01)


def test_agreeing_closes_leave_quote_untouched(yahoo):
    yahoo["1m"] = _minute_body(prev=MON_CLOSE)
    yahoo["1d"] = _daily_body(DAILY_WITH_MONDAY)
    q = cm.fetch_quote("^TWII")
    assert q["prev_close"] == MON_CLOSE and "prev_note" not in q
    assert "prev_reject" not in q


def test_monday_after_weekend_is_not_a_gap():
    mon = int(datetime(2026, 11, 16, 13, 0, tzinfo=TPE).timestamp())
    q = {"price": 41040.08, "prev_close": FRI_CLOSE, "pct": -6.03,
         "quote_time": mon, "gmtoffset": 28800}
    daily = cm.parse_daily_closes(_daily_body(
        [((2026, 11, 13), FRI_CLOSE), ((2026, 11, 16), 41040.08)])["chart"]["result"][0])
    out = cm.verify_prev_close(q, daily, "^TWII")
    assert "prev_reject" not in out
    assert out["pct"] == pytest.approx(-6.03)


ET = timezone(timedelta(hours=-5))


def _us_case(prev_day):
    """QQQ on Fri 2026-11-27, the day after Thanksgiving, down 2.2%."""
    fri = int(datetime(2026, 11, 27, 11, 0, tzinfo=ET).timestamp())
    q = {"price": 500.0, "prev_close": 511.25, "pct": -2.2, "quote_time": fri,
         "gmtoffset": -18000}
    rows = [(prev_day, 511.25), ((2026, 11, 27), 500.0)]
    body = {"meta": {"gmtoffset": -18000},
            "timestamp": [int(datetime(*d, 10, 0, tzinfo=ET).timestamp()) for d, _ in rows],
            "indicators": {"quote": [{"close": [c for _, c in rows]}]}}
    return q, cm.parse_daily_closes(body)


def test_exchange_holiday_is_not_a_missing_session():
    # Thanksgiving: NYSE closed Thu 11/26, so Wed 11/25 is the right reference.
    q, daily = _us_case((2026, 11, 25))
    out = cm.verify_prev_close(q, daily, "QQQ")
    assert "prev_reject" not in out and out["pct"] == pytest.approx(-2.2)


def test_missing_non_holiday_still_rejects_with_a_calendar():
    q, daily = _us_case((2026, 11, 24))          # Wed 11/25 missing, not a holiday
    out = cm.verify_prev_close(q, daily, "QQQ")
    assert "2026-11-25" in out["prev_reject"] and "2026-11-26" not in out["prev_reject"]


def test_symbol_without_calendar_never_rejects_for_a_gap():
    q, daily = _us_case((2026, 11, 24))
    out = cm.verify_prev_close(q, daily, "NQ=F")
    assert "prev_reject" not in out


def test_missing_holidays_package_fails_open(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_holidays(name, *a, **k):
        if name == "holidays":
            raise ImportError("no holidays")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_holidays)
    q, daily = _us_case((2026, 11, 24))
    assert "prev_reject" not in cm.verify_prev_close(q, daily, "QQQ")


def test_daily_fetch_failure_fails_open(yahoo):
    yahoo["1d"] = OSError("boom")
    q = cm.fetch_quote("^TWII")
    assert "prev_reject" not in q
    assert q["pct"] == pytest.approx(-8.39, abs=0.01)
    assert q["prev_note"] == "prev close unverified"


def test_small_move_makes_no_extra_request(yahoo):
    yahoo["1m"] = _minute_body(prev=TUE_PRICE * 1.005)
    cm.fetch_quote("^TWII")
    assert len(yahoo["calls"]) == 1


def test_rejected_reference_drops_symbol_from_decisions(monkeypatch):
    q = cm.verify_prev_close(
        cm.parse_chart_quote(_minute_body()["chart"]["result"][0]),
        cm.parse_daily_closes(_daily_body(DAILY_MISSING_MONDAY)["chart"]["result"][0]),
        "^TWII")
    monkeypatch.setattr(cm, "fetch_quote",
                        lambda sym: dict(q) if sym == "^TWII" else None)
    args = Namespace(min_move=None, ignore_freshness=True)
    fresh, breaches, details, _fail, rejects = cm._collect_quotes(args)
    assert "^TWII" not in fresh
    assert rejects == ["^TWII"]
    assert breaches["alert_down"] == []
