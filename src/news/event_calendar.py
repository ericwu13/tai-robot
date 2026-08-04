"""排程事件行事曆 Scheduled-event calendar gate (Phase 1 foundations).

Reads a JSON calendar written by an external n8n workflow and answers two
questions:

- :func:`active_event` — is the session we are trading RIGHT NOW covered
  by an event at or above ``min_severity``?
- :func:`upcoming_event` — is the NEXT session covered?  The switching
  runner uses this at apply-time (during a closed gap, while flat) to
  decide a sit-out *before* the session opens.

Both are keyed by the session's OPEN date, matching the repo-wide session
identity convention (see ``src.regime.switch_logic``): the night session
that opens 08-04 15:00 and closes 08-05 05:00 is "2026-08-04|NIGHT" on
both sides of midnight.  An event dated 2026-08-04 with ``["NIGHT"]``
therefore still gates a 02:00 poll on 08-05.

The file is UNTRUSTED input.  Every failure mode — missing file, bad
JSON, wrong version, junk entries — degrades to "no events" instead of
raising, because a broken n8n job must never be able to halt trading.
For the same reason a *stale* calendar FAILS OPEN: see
:func:`calendar_stale`.

File schema::

    {
      "version": 1,
      "updated_at": "2026-08-04T10:00:00+08:00",
      "events": [
        {"date": "2026-08-13", "name": "US CPI",
         "severity": "high", "sessions": ["DAY", "NIGHT"]}
      ]
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.regime.switch_logic import (
    SessionInfo,
    _is_closed_day,
    current_session,
)

_TZ_TAIPEI = timezone(timedelta(hours=8))

# Schema version this module understands. A file stamped with anything
# else is rejected wholesale — a future n8n workflow bumping the version
# must not have its new semantics guessed at by old code.
SCHEMA_VERSION = 1

# high > medium > low. Unknown severity strings are rejected (an entry
# whose severity we cannot rank could otherwise gate every session).
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}

_VALID_SLOTS = ("DAY", "NIGHT")

# Session open times (hour, minute) in chronological order within a day.
_SESSION_OPENS = (("DAY", 8, 45), ("NIGHT", 15, 0))

# How far ahead next_session() looks for the next open trading day.
_MAX_LOOKAHEAD_DAYS = 15


@dataclass
class EventEntry:
    """One scheduled macro event (一則排程事件)."""

    date: str                                    # YYYY-MM-DD, session OPEN date
    name: str
    severity: str                                # "high" | "medium" | "low"
    sessions: list[str] = field(default_factory=list)   # subset of DAY/NIGHT


def _to_taipei(now: datetime | None) -> datetime:
    """Normalize *now* to an aware Taipei datetime (naive → assume TPE)."""
    if now is None:
        return datetime.now(_TZ_TAIPEI)
    if now.tzinfo is None:
        return now.replace(tzinfo=_TZ_TAIPEI)
    return now


def severity_rank(severity: str) -> int:
    """Numeric rank of a severity string (0 = unknown/unrankable)."""
    return _SEVERITY_RANK.get(str(severity).strip().lower(), 0)


def _parse_event(raw: object) -> EventEntry | None:
    """Build an EventEntry from one raw JSON element, or None if unusable.

    Rejects rather than guesses: an entry with no usable severity or no
    usable session list would otherwise gate sessions it was never meant
    to cover.
    """
    if not isinstance(raw, dict):
        return None

    date_str = raw.get("date")
    if not isinstance(date_str, str):
        return None
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    severity = raw.get("severity")
    if not isinstance(severity, str) or severity_rank(severity) == 0:
        return None

    sessions_raw = raw.get("sessions")
    if not isinstance(sessions_raw, list):
        return None
    sessions = [
        s.strip().upper() for s in sessions_raw
        if isinstance(s, str) and s.strip().upper() in _VALID_SLOTS
    ]
    if not sessions:
        return None

    return EventEntry(
        date=date_str,
        name=name.strip(),
        severity=severity.strip().lower(),
        sessions=sessions,
    )


def load_events(path: str | os.PathLike | None) -> tuple[list[EventEntry], str]:
    """Load the calendar. Returns ``(events, error_reason)`` — never raises.

    ``error_reason`` is non-empty whenever ANYTHING was rejected, so the
    caller can log a warning; the two are independent:

    - ``([], "<reason>")`` — the whole file was unusable (fail open, no
      gating at all).
    - ``([good...], "<reason>")`` — partial parse: individual junk
      entries were skipped, the rest survive.
    - ``([...], "")`` — clean file.
    """
    if not path:
        return [], "no events_path configured"
    path = str(path)
    if not os.path.exists(path):
        return [], f"events file not found: {path}"

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return [], f"malformed events file: {e}"

    if not isinstance(data, dict):
        return [], "events file root is not a JSON object"

    version = data.get("version")
    if version != SCHEMA_VERSION:
        return [], f"unsupported events schema version: {version!r}"

    raw_events = data.get("events")
    if not isinstance(raw_events, list):
        return [], "events file has no 'events' list"

    events: list[EventEntry] = []
    skipped = 0
    for raw in raw_events:
        ev = _parse_event(raw)
        if ev is None:
            skipped += 1
            continue
        events.append(ev)

    if skipped:
        return events, f"skipped {skipped} malformed event entry/entries"
    return events, ""


def read_updated_at(path: str | os.PathLike | None) -> str:
    """The calendar's ``updated_at`` stamp, or "" when unreadable.

    Split out from :func:`load_events` so the staleness warning can be
    raised independently of whether any events parsed (an empty but
    fresh calendar is a legitimate "no events this fortnight").
    """
    if not path:
        return ""
    try:
        with open(str(path), encoding="utf-8") as f:
            data = json.load(f)
        value = data.get("updated_at")
        return value if isinstance(value, str) else ""
    except (OSError, ValueError, AttributeError):
        return ""


def calendar_stale(
    updated_at_str: str,
    now: datetime | None = None,
    max_age_days: int = 14,
) -> bool:
    """True when the calendar is too old to be trusted — FAILS OPEN.

    A stale calendar means "stop gating", not "gate everything": a
    forgotten n8n job must not be able to sit the bot out forever.
    Callers log a warning and skip the event gate when this is True.

    An unparsable/naive-safe ``updated_at`` counts as stale (we cannot
    prove freshness).  A file exactly ``max_age_days`` old is already
    stale — the boundary is closed on the stale side.
    """
    if not updated_at_str:
        return True
    try:
        updated = datetime.fromisoformat(updated_at_str)
    except (TypeError, ValueError):
        return True
    updated = _to_taipei(updated)
    now = _to_taipei(now)
    return (now - updated) >= timedelta(days=max_age_days)


def next_session(now: datetime | None = None) -> SessionInfo | None:
    """The next session that OPENS strictly after *now*, or None.

    Well defined both inside a session (returns the session after this
    one) and in a closed gap (returns the session the gap leads into):

    - DAY 08:45-13:45  → tonight's NIGHT (15:00)
    - gap 13:46-14:59  → tonight's NIGHT (15:00)
    - NIGHT 15:00-23:59 → next trading day's DAY (08:45)
    - NIGHT carryover 00:00-05:00 and gap 05:01-08:44 → today's DAY if
      today trades, else the next trading day's DAY

    Holidays and weekends are skipped using the same calendar
    ``switch_logic`` uses, so a Friday-night poll returns Monday's DAY.
    Returns None only if no trading day is found within ~2 weeks.
    """
    now = _to_taipei(now)
    for back in range(_MAX_LOOKAHEAD_DAYS):
        d = (now + timedelta(days=back)).date()
        if _is_closed_day(d):
            continue
        for slot, hour, minute in _SESSION_OPENS:
            open_dt = datetime(d.year, d.month, d.day, hour, minute,
                               tzinfo=_TZ_TAIPEI)
            if open_dt <= now:
                continue
            close_dt = (open_dt + timedelta(hours=14) if slot == "NIGHT"
                        else open_dt.replace(hour=13, minute=45))
            return SessionInfo(d.isoformat(), slot, open_dt, close_dt)
    return None


def _match(
    events: list[EventEntry],
    session: SessionInfo | None,
    min_severity: str,
) -> EventEntry | None:
    """First event covering *session* at or above *min_severity*."""
    if session is None:
        return None
    floor = severity_rank(min_severity)
    if floor == 0:
        floor = _SEVERITY_RANK["high"]
    for ev in events:
        if ev.date != session.open_date:
            continue
        if session.slot not in ev.sessions:
            continue
        if severity_rank(ev.severity) >= floor:
            return ev
    return None


def active_event(
    events: list[EventEntry],
    now: datetime | None = None,
    min_severity: str = "high",
) -> EventEntry | None:
    """The event covering the session in progress at *now*, or None.

    Matches on the session's OPEN date, so the midnight-straddling night
    keeps one identity: at 02:00 on 08-05 the live session is
    "2026-08-04|NIGHT" and an event dated 2026-08-04 matches.

    Returns None when the market is closed (gap/weekend/holiday) — there
    is no session to gate.
    """
    return _match(events, current_session(now), min_severity)


def upcoming_event(
    events: list[EventEntry],
    now: datetime | None = None,
    min_severity: str = "high",
) -> EventEntry | None:
    """The event covering the NEXT session to open after *now*, or None.

    This is the apply-time question: the runner is flat in a closed gap
    and decides whether to sit the coming session out.
    """
    return _match(events, next_session(now), min_severity)
