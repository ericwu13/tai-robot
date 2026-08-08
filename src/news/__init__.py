"""新聞/事件交易框架 News & event trading framework (Phase 1 foundations).

Three independent, file-based inputs written by an external n8n workflow:

- ``event_calendar`` — SCHEDULED risk (CPI, FOMC, ...): sit the session
  out before it opens.
- ``signal_file`` — UNSCHEDULED risk: a circuit-breaker signal that
  flattens the book (Tier 1) or deploys a forced-entry event strategy
  (Tier 2), acted on exactly once via ``SignalLedger``.
- ``regime_vote`` — cross-market confirmation acceleration: when the
  semiconductor complex moves sharply in one direction, counts as one
  regime confirmation vote (accelerates hysteresis from 2→1 night).

All treat their files as untrusted and fail open on any parse problem —
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
from .regime_vote import (
    SCHEMA_VERSION as VOTE_SCHEMA_VERSION,
    VALID_DIRECTIONS,
    RegimeVote,
    consume_all_regime_votes,
    consume_regime_vote,
    read_all_regime_votes,
    read_regime_vote,
    write_regime_vote,
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
    "VOTE_SCHEMA_VERSION",
    "VALID_DIRECTIONS",
    "RegimeVote",
    "consume_all_regime_votes",
    "consume_regime_vote",
    "read_all_regime_votes",
    "read_regime_vote",
    "write_regime_vote",
    "NewsSignal",
    "SignalLedger",
    "read_signal",
]
