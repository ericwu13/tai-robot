"""新聞/事件交易框架 News & event trading framework (Phase 1 foundations).

Two independent, file-based inputs written by an external n8n workflow:

- ``event_calendar`` — SCHEDULED risk (CPI, FOMC, ...): sit the session
  out before it opens.
- ``signal_file`` — UNSCHEDULED risk: a circuit-breaker signal that
  flattens the book (Tier 1) or deploys a forced-entry event strategy
  (Tier 2), acted on exactly once via ``SignalLedger``.

Both treat their files as untrusted and fail open on any parse problem —
a broken automation must never be able to halt trading indefinitely.
Runner wiring lands in Phase 2.
"""

from .circuit_breaker import (
    BreakerAction,
    BreakerDecision,
    BreakerState,
    plan_calendar_gate,
    plan_signal_actions,
    should_revert_news,
)
from .event_calendar import (
    SCHEMA_VERSION as CALENDAR_SCHEMA_VERSION,
    EventEntry,
    active_event,
    calendar_stale,
    load_events,
    next_session,
    read_updated_at,
    severity_rank,
    upcoming_event,
)
from .signal_file import (
    CLOCK_SKEW_SEC,
    LEDGER_MAX_IDS,
    SCHEMA_VERSION as SIGNAL_SCHEMA_VERSION,
    VALID_ACTIONS,
    NewsSignal,
    SignalLedger,
    read_signal,
)

__all__ = [
    "BreakerAction",
    "BreakerDecision",
    "BreakerState",
    "plan_calendar_gate",
    "plan_signal_actions",
    "should_revert_news",
    "CALENDAR_SCHEMA_VERSION",
    "EventEntry",
    "active_event",
    "calendar_stale",
    "load_events",
    "next_session",
    "read_updated_at",
    "severity_rank",
    "upcoming_event",
    "CLOCK_SKEW_SEC",
    "LEDGER_MAX_IDS",
    "SIGNAL_SCHEMA_VERSION",
    "VALID_ACTIONS",
    "NewsSignal",
    "SignalLedger",
    "read_signal",
]
