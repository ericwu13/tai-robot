"""Tests for the scheduled-event calendar gate (src/news/event_calendar.py).

The calendar file is untrusted input written by an external n8n workflow,
so these tests care as much about what is REJECTED as about what matches.

Date anchors (verified TAIFEX trading days):
    2026-08-03 Mon .. 2026-08-07 Fri = trading
    2026-08-08 Sat / 2026-08-09 Sun  = closed
"""

import json
from datetime import datetime, timedelta, timezone

from src.news.event_calendar import (
    EventEntry,
    active_event,
    calendar_stale,
    load_events,
    next_session,
    read_updated_at,
    upcoming_event,
)

TPE = timezone(timedelta(hours=8))


def tpe(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TPE)


def write_calendar(tmp_path, events, version=1, updated_at="2026-08-04T10:00:00+08:00"):
    path = tmp_path / "events.json"
    path.write_text(
        json.dumps({"version": version, "updated_at": updated_at,
                    "events": events}),
        encoding="utf-8",
    )
    return str(path)


def ev(date, name="US CPI", severity="high", sessions=("DAY", "NIGHT")):
    return EventEntry(date=date, name=name, severity=severity,
                      sessions=list(sessions))


# ── load_events: tolerant parsing ──────────────────────────────────────

def test_missing_file_returns_empty_with_reason(tmp_path):
    events, reason = load_events(str(tmp_path / "nope.json"))
    assert events == []
    assert "not found" in reason


def test_no_path_configured_returns_empty(tmp_path):
    assert load_events("") == ([], "no events_path configured")
    assert load_events(None)[0] == []


def test_malformed_json_returns_empty_with_reason(tmp_path):
    path = tmp_path / "events.json"
    path.write_text('{"version": 1, "events": [ {{{ ', encoding="utf-8")
    events, reason = load_events(str(path))
    assert events == []
    assert reason  # caller logs this


def test_wrong_version_rejects_whole_file(tmp_path):
    path = write_calendar(tmp_path, [{"date": "2026-08-04", "name": "US CPI",
                                      "severity": "high", "sessions": ["DAY"]}],
                          version=2)
    events, reason = load_events(path)
    assert events == []
    assert "version" in reason


def test_root_not_object_rejected(tmp_path):
    path = tmp_path / "events.json"
    path.write_text('["not", "an", "object"]', encoding="utf-8")
    events, reason = load_events(str(path))
    assert events == []
    assert reason


def test_bad_entry_skipped_good_ones_survive(tmp_path):
    path = write_calendar(tmp_path, [
        {"date": "not-a-date", "name": "Junk", "severity": "high",
         "sessions": ["DAY"]},                                   # bad date
        {"date": "2026-08-04", "name": "US CPI", "severity": "high",
         "sessions": ["DAY", "NIGHT"]},                          # good
        {"date": "2026-08-05", "severity": "high", "sessions": ["DAY"]},  # no name
        {"date": "2026-08-06", "name": "FOMC", "severity": "critical",
         "sessions": ["NIGHT"]},                                 # unrankable severity
        {"date": "2026-08-07", "name": "NFP", "severity": "high"},        # no sessions
        {"date": "2026-08-07", "name": "PPI", "severity": "medium",
         "sessions": ["NIGHT"]},                                 # good
        "totally bogus",                                         # not a dict
    ])
    events, reason = load_events(path)
    assert [e.name for e in events] == ["US CPI", "PPI"]
    assert "skipped 5" in reason


def test_clean_file_has_empty_reason(tmp_path):
    path = write_calendar(tmp_path, [
        {"date": "2026-08-04", "name": "US CPI", "severity": "HIGH",
         "sessions": ["night"]},
    ])
    events, reason = load_events(path)
    assert reason == ""
    # severity/sessions are normalized, not passed through raw
    assert events[0].severity == "high"
    assert events[0].sessions == ["NIGHT"]


def test_unknown_sessions_filtered_out(tmp_path):
    path = write_calendar(tmp_path, [
        {"date": "2026-08-04", "name": "US CPI", "severity": "high",
         "sessions": ["DAY", "AFTERNOON"]},
    ])
    events, _ = load_events(path)
    assert events[0].sessions == ["DAY"]


def test_read_updated_at(tmp_path):
    path = write_calendar(tmp_path, [], updated_at="2026-08-04T10:00:00+08:00")
    assert read_updated_at(path) == "2026-08-04T10:00:00+08:00"
    assert read_updated_at(str(tmp_path / "nope.json")) == ""


# ── calendar_stale: fails OPEN ─────────────────────────────────────────

def test_stale_calendar_at_exact_boundary():
    updated = "2026-08-01T10:00:00+08:00"
    exactly_14d = tpe(2026, 8, 15, 10, 0)
    assert calendar_stale(updated, exactly_14d, max_age_days=14) is True
    assert calendar_stale(updated, exactly_14d - timedelta(seconds=1),
                          max_age_days=14) is False


def test_missing_or_unparsable_updated_at_is_stale():
    now = tpe(2026, 8, 4, 10)
    assert calendar_stale("", now) is True
    assert calendar_stale("last tuesday", now) is True


def test_future_updated_at_is_not_stale():
    assert calendar_stale("2026-08-10T10:00:00+08:00", tpe(2026, 8, 4, 10)) is False


def test_stale_compares_across_offsets():
    # Same instant expressed in UTC — must not be read as 8 hours older.
    assert calendar_stale("2026-08-01T02:00:00+00:00",
                          tpe(2026, 8, 15, 9, 59), max_age_days=14) is False


# ── active_event: session identity is the OPEN date ────────────────────

def test_active_event_matches_night_by_open_date():
    """A 02:00 poll on Aug 5 is still the night that OPENED on Aug 4."""
    events = [ev("2026-08-04", sessions=["NIGHT"])]
    hit = active_event(events, tpe(2026, 8, 5, 2, 0))
    assert hit is not None and hit.name == "US CPI"


def test_active_event_does_not_match_calendar_date_after_midnight():
    """Fails against a calendar-date implementation.

    At 02:00 on Aug 5 the live session is 2026-08-04|NIGHT. An event
    dated Aug 5 belongs to Aug 5's DAY / Aug 5's NIGHT — matching it now
    would gate the wrong session.
    """
    events = [ev("2026-08-05", sessions=["NIGHT"])]
    assert active_event(events, tpe(2026, 8, 5, 2, 0)) is None


def test_active_event_matches_night_before_midnight():
    events = [ev("2026-08-04", sessions=["NIGHT"])]
    assert active_event(events, tpe(2026, 8, 4, 22, 0)) is not None


def test_active_event_matches_day_session():
    events = [ev("2026-08-04", sessions=["DAY"])]
    assert active_event(events, tpe(2026, 8, 4, 10, 0)) is not None
    # ...but not that day's night session
    assert active_event(events, tpe(2026, 8, 4, 22, 0)) is None


def test_severity_below_minimum_does_not_gate():
    events = [ev("2026-08-04", severity="medium", sessions=["DAY"])]
    now = tpe(2026, 8, 4, 10, 0)
    assert active_event(events, now, min_severity="high") is None
    assert active_event(events, now, min_severity="medium") is not None
    assert active_event(events, now, min_severity="low") is not None


def test_higher_severity_still_matches_lower_floor():
    events = [ev("2026-08-04", severity="high", sessions=["DAY"])]
    assert active_event(events, tpe(2026, 8, 4, 10), min_severity="low") is not None


def test_closed_market_returns_none():
    events = [ev("2026-08-08"), ev("2026-08-07"), ev("2026-08-04")]
    assert active_event(events, tpe(2026, 8, 8, 13, 43)) is None   # Saturday
    assert active_event(events, tpe(2026, 8, 9, 20, 0)) is None    # Sunday
    assert active_event(events, tpe(2026, 8, 4, 14, 0)) is None    # 13:46-15:00 gap
    assert active_event(events, tpe(2026, 8, 4, 7, 0)) is None     # 05:00-08:45 gap


def test_empty_calendar_never_gates():
    assert active_event([], tpe(2026, 8, 4, 10)) is None


# ── next_session / upcoming_event ──────────────────────────────────────

def test_next_session_inside_day_is_tonight():
    s = next_session(tpe(2026, 8, 4, 10, 0))
    assert (s.open_date, s.slot) == ("2026-08-04", "NIGHT")
    assert s.open_dt == tpe(2026, 8, 4, 15, 0)


def test_next_session_in_afternoon_gap_is_tonight():
    s = next_session(tpe(2026, 8, 4, 14, 0))
    assert (s.open_date, s.slot) == ("2026-08-04", "NIGHT")


def test_next_session_inside_night_is_tomorrow_day():
    s = next_session(tpe(2026, 8, 4, 22, 0))
    assert (s.open_date, s.slot) == ("2026-08-05", "DAY")


def test_next_session_after_midnight_is_today_day():
    s = next_session(tpe(2026, 8, 5, 2, 0))
    assert (s.open_date, s.slot) == ("2026-08-05", "DAY")


def test_next_session_in_morning_gap_is_today_day():
    s = next_session(tpe(2026, 8, 5, 7, 0))
    assert (s.open_date, s.slot) == ("2026-08-05", "DAY")


def test_next_session_skips_weekend():
    # Friday night → Monday day (Sat/Sun have no sessions).
    s = next_session(tpe(2026, 8, 7, 22, 0))
    assert (s.open_date, s.slot) == ("2026-08-10", "DAY")


def test_next_session_at_exact_open_returns_the_following_one():
    s = next_session(tpe(2026, 8, 4, 8, 45))
    assert (s.open_date, s.slot) == ("2026-08-04", "NIGHT")


def test_upcoming_event_in_afternoon_gap():
    """13:46-15:00 gap: the runner is flat and decides on tonight."""
    now = tpe(2026, 8, 4, 14, 30)
    assert upcoming_event([ev("2026-08-04", sessions=["NIGHT"])], now) is not None
    # tonight is NIGHT — a DAY-only event for the same date must not gate it
    assert upcoming_event([ev("2026-08-04", sessions=["DAY"])], now) is None


def test_upcoming_event_in_morning_gap():
    """05:00-08:45 gap: the decision is about today's DAY session."""
    now = tpe(2026, 8, 5, 7, 0)
    assert upcoming_event([ev("2026-08-05", sessions=["DAY"])], now) is not None
    assert upcoming_event([ev("2026-08-04", sessions=["DAY"])], now) is None


def test_upcoming_event_over_the_weekend():
    now = tpe(2026, 8, 8, 12, 0)   # Saturday — market closed
    assert active_event([ev("2026-08-10", sessions=["DAY"])], now) is None
    assert upcoming_event([ev("2026-08-10", sessions=["DAY"])], now) is not None


def test_upcoming_event_respects_severity():
    now = tpe(2026, 8, 4, 14, 30)
    events = [ev("2026-08-04", severity="low", sessions=["NIGHT"])]
    assert upcoming_event(events, now, min_severity="high") is None
    assert upcoming_event(events, now, min_severity="low") is not None
