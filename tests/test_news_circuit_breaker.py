"""Tests for the pure news circuit-breaker planner (src.news.circuit_breaker).

Every branch the runner can take is decided here, so this file is where
the behaviour is pinned: what gets flattened, what gets suppressed, which
suppression source is released by what, and the two invariants that keep
the 30s poll from looping — always mark consumed, never trade in replay.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.news.circuit_breaker import (
    CLEAR_EVENT,
    DEPLOY_NEWS,
    DISCORD,
    FLATTEN,
    FLATTEN_TAG,
    MARK_CONSUMED,
    SRC_EVENT,
    SRC_SIGNAL,
    STAMP_EVENT,
    SUPPRESS,
    UNSUPPRESS,
    BreakerAction,
    BreakerState,
    plan_calendar_gate,
    plan_signal_actions,
    should_revert_news,
)
from src.news.event_calendar import EventEntry
from src.news.signal_file import NewsSignal

_TZ = timezone(timedelta(hours=8))


def _sig(action="risk_off", signal_id="sig-1", **kw):
    return NewsSignal(
        signal_id=signal_id,
        action=action,
        issued_at=datetime(2026, 8, 4, 22, 5, tzinfo=_TZ),
        severity=kw.get("severity", "high"),
        source=kw.get("source", "crossmarket-sox"),
        reason=kw.get("reason", "SOX -3.2% intraday"),
    )


def _ev(name="US CPI", severity="high", date="2026-08-13"):
    return EventEntry(date=date, name=name, severity=severity,
                      sessions=["DAY", "NIGHT"])


def _plan(signal=None, consumed=False, tier2=False, has_position=False,
          state=None, in_replay=False):
    return plan_signal_actions(
        signal, consumed, tier2, has_position,
        state if state is not None else BreakerState(), in_replay)


# ── BreakerState ──

class TestBreakerState:
    def test_suppressed_is_or_of_sources(self):
        assert BreakerState().suppressed is False
        assert BreakerState(signal_suppressed=True).suppressed is True
        assert BreakerState(event_suppressed=True).suppressed is True
        assert BreakerState(signal_suppressed=True,
                            event_suppressed=True).suppressed is True

    def test_dict_roundtrip(self):
        st = BreakerState(
            signal_suppressed=True, event_suppressed=True,
            suppressed_reason="boom", event_name="US CPI",
            news_strategy_active=True,
            news_deployed_session_key="2026-08-04|NIGHT")
        assert BreakerState.from_dict(st.to_dict()) == st

    @pytest.mark.parametrize("junk", [None, "", [], 0, {"nope": 1}])
    def test_from_dict_rejects_junk(self, junk):
        """A corrupt session file must not resurrect a phantom gate."""
        assert BreakerState.from_dict(junk) == BreakerState()


# ── Signal planner: no-ops ──

class TestSignalNoOps:
    def test_none_signal_no_actions(self):
        assert _plan(None).actions == []

    def test_consumed_signal_no_actions(self):
        assert _plan(_sig("risk_off"), consumed=True).actions == []

    def test_consumed_risk_off_does_not_reflatten(self):
        """The exact loop the ledger exists to prevent."""
        d = _plan(_sig("risk_off"), consumed=True, has_position=True)
        assert not d.has(FLATTEN)
        assert not d.has(MARK_CONSUMED)


# ── Signal planner: risk_off (Tier 1) ──

class TestRiskOff:
    def test_with_position_flattens(self):
        d = _plan(_sig("risk_off"), has_position=True)
        assert d.kinds == [FLATTEN, SUPPRESS, DISCORD, MARK_CONSUMED]
        assert d.arg_of(FLATTEN) == FLATTEN_TAG

    def test_flat_does_not_flatten(self):
        d = _plan(_sig("risk_off"), has_position=False)
        assert d.kinds == [SUPPRESS, DISCORD, MARK_CONSUMED]

    def test_suppresses_signal_source_only(self):
        d = _plan(_sig("risk_off"))
        srcs = [a.arg for a in d.actions if a.kind == SUPPRESS]
        assert srcs == [SRC_SIGNAL]

    def test_never_touches_event_source(self):
        d = _plan(_sig("risk_off"),
                  state=BreakerState(event_suppressed=True, event_name="FOMC"))
        assert not any(a.arg == SRC_EVENT for a in d.actions)
        assert not d.has(CLEAR_EVENT)

    def test_repeat_risk_off_new_id_flattens_again(self):
        """A second distinct signal_id is a new event, not a duplicate."""
        d = _plan(_sig("risk_off", signal_id="sig-2"),
                  has_position=True, state=BreakerState(signal_suppressed=True))
        assert d.has(FLATTEN)
        assert d.consumed_id == "sig-2"

    def test_discord_message_carries_source_and_reason(self):
        d = _plan(_sig("risk_off", source="crossmarket-sox",
                       reason="SOX -3.2%"))
        msg = next(a.reason for a in d.actions if a.kind == DISCORD)
        assert "crossmarket-sox" in msg and "SOX -3.2%" in msg
        assert "RISK OFF" in msg


# ── Signal planner: clear ──

class TestClear:
    def test_clear_unsuppresses_signal_source(self):
        d = _plan(_sig("clear"), state=BreakerState(signal_suppressed=True))
        assert d.kinds == [UNSUPPRESS, DISCORD, MARK_CONSUMED]
        assert d.arg_of(UNSUPPRESS) == SRC_SIGNAL

    def test_clear_does_not_cancel_event_suppression(self):
        """A Discord tap during an FOMC session must not un-gate the event."""
        state = BreakerState(signal_suppressed=True, event_suppressed=True,
                             event_name="FOMC")
        d = _plan(_sig("clear"), state=state)
        assert not any(a.kind == UNSUPPRESS and a.arg == SRC_EVENT
                       for a in d.actions)
        assert not d.has(CLEAR_EVENT)

    def test_clear_never_flattens(self):
        d = _plan(_sig("clear"), has_position=True)
        assert not d.has(FLATTEN)


# ── Signal planner: Tier 2 deploys ──

class TestTier2Deploys:
    def test_disabled_produces_note_only(self):
        d = _plan(_sig("deploy_short"), tier2=False, has_position=True)
        assert d.kinds == [DISCORD, MARK_CONSUMED]
        assert "tier2_enabled=false" in d.actions[0].reason

    def test_disabled_still_marks_consumed(self):
        """Otherwise the same 'ignored' note posts every 30s for 15 min."""
        d = _plan(_sig("deploy_long", signal_id="s9"), tier2=False)
        assert d.consumed_id == "s9"

    def test_short_with_position_flattens_then_deploys(self):
        d = _plan(_sig("deploy_short"), tier2=True, has_position=True)
        assert d.kinds == [FLATTEN, DEPLOY_NEWS, UNSUPPRESS, DISCORD,
                           MARK_CONSUMED]
        assert d.arg_of(DEPLOY_NEWS) == "short"

    def test_long_flat_skips_flatten(self):
        d = _plan(_sig("deploy_long"), tier2=True, has_position=False)
        assert d.kinds == [DEPLOY_NEWS, UNSUPPRESS, DISCORD, MARK_CONSUMED]
        assert d.arg_of(DEPLOY_NEWS) == "long"

    def test_deploy_releases_signal_suppression(self):
        """A prior risk_off would otherwise swallow the one forced entry."""
        d = _plan(_sig("deploy_short"), tier2=True,
                  state=BreakerState(signal_suppressed=True))
        assert d.arg_of(UNSUPPRESS) == SRC_SIGNAL

    def test_deploy_does_not_release_event_suppression(self):
        d = _plan(_sig("deploy_short"), tier2=True,
                  state=BreakerState(event_suppressed=True, event_name="CPI"))
        assert not any(a.kind == UNSUPPRESS and a.arg == SRC_EVENT
                       for a in d.actions)

    def test_flatten_precedes_deploy(self):
        d = _plan(_sig("deploy_long"), tier2=True, has_position=True)
        assert d.kinds.index(FLATTEN) < d.kinds.index(DEPLOY_NEWS)


# ── Signal planner: replay ──

class TestInReplay:
    @pytest.mark.parametrize("action",
                             ["risk_off", "clear", "deploy_short", "deploy_long"])
    def test_replay_never_trades(self, action):
        d = _plan(_sig(action), tier2=True, has_position=True, in_replay=True)
        assert d.kinds == [DISCORD, MARK_CONSUMED]
        for forbidden in (FLATTEN, SUPPRESS, UNSUPPRESS, DEPLOY_NEWS):
            assert not d.has(forbidden)

    def test_replay_still_consumes(self):
        d = _plan(_sig("risk_off", signal_id="r1"), in_replay=True)
        assert d.consumed_id == "r1"

    def test_replay_note_names_the_action(self):
        d = _plan(_sig("deploy_short"), tier2=True, in_replay=True)
        assert "deploy_short" in d.actions[0].reason


# ── The always-mark-consumed invariant ──

class TestAlwaysMarkConsumed:
    @pytest.mark.parametrize("action",
                             ["risk_off", "clear", "deploy_short", "deploy_long"])
    @pytest.mark.parametrize("tier2", [True, False])
    @pytest.mark.parametrize("has_position", [True, False])
    @pytest.mark.parametrize("in_replay", [True, False])
    def test_every_unconsumed_signal_marked_exactly_once(
            self, action, tier2, has_position, in_replay):
        d = _plan(_sig(action, signal_id="only-one"), tier2=tier2,
                  has_position=has_position, in_replay=in_replay)
        marks = [a for a in d.actions if a.kind == MARK_CONSUMED]
        assert len(marks) == 1
        assert marks[0].arg == "only-one"

    def test_mark_is_last(self):
        """The runner defers it to a `finally`; the list order documents it."""
        d = _plan(_sig("risk_off"), has_position=True)
        assert d.actions[-1].kind == MARK_CONSUMED

    def test_executable_excludes_mark(self):
        d = _plan(_sig("risk_off"), has_position=True)
        assert MARK_CONSUMED not in [a.kind for a in d.executable]
        assert len(d.executable) == len(d.actions) - 1


# ── Calendar gate ──

class TestCalendarGate:
    def test_active_event_stamps_and_suppresses(self):
        d = plan_calendar_gate(_ev("US CPI"), None, False, BreakerState())
        assert d.kinds == [STAMP_EVENT, SUPPRESS, DISCORD]
        assert d.arg_of(STAMP_EVENT) == "US CPI"
        assert d.arg_of(SUPPRESS) == SRC_EVENT

    def test_upcoming_event_gates_next_session(self):
        d = plan_calendar_gate(None, _ev("FOMC"), False, BreakerState())
        assert d.arg_of(STAMP_EVENT) == "FOMC"
        assert "next session" in next(
            a.reason for a in d.actions if a.kind == DISCORD)

    def test_active_wins_over_upcoming(self):
        d = plan_calendar_gate(_ev("US CPI"), _ev("FOMC"), False, BreakerState())
        assert d.arg_of(STAMP_EVENT) == "US CPI"
        assert "current session" in next(
            a.reason for a in d.actions if a.kind == DISCORD)

    def test_same_event_is_idempotent(self):
        """The 30s poll must not re-stamp or re-announce."""
        state = BreakerState(event_suppressed=True, event_name="US CPI")
        assert plan_calendar_gate(_ev("US CPI"), None, False, state).actions == []

    def test_different_event_restamps(self):
        state = BreakerState(event_suppressed=True, event_name="US CPI")
        d = plan_calendar_gate(_ev("FOMC"), None, False, state)
        assert d.arg_of(STAMP_EVENT) == "FOMC"

    def test_window_passed_clears(self):
        state = BreakerState(event_suppressed=True, event_name="US CPI")
        d = plan_calendar_gate(None, None, False, state)
        assert d.kinds == [CLEAR_EVENT, UNSUPPRESS, DISCORD]
        assert d.arg_of(UNSUPPRESS) == SRC_EVENT

    def test_no_event_nothing_stamped_is_silent(self):
        assert plan_calendar_gate(None, None, False, BreakerState()).actions == []

    def test_calendar_release_does_not_cancel_risk_off(self):
        """The mirror of test_clear_does_not_cancel_event_suppression."""
        state = BreakerState(signal_suppressed=True, event_suppressed=True,
                             event_name="US CPI")
        d = plan_calendar_gate(None, None, False, state)
        assert not any(a.kind == UNSUPPRESS and a.arg == SRC_SIGNAL
                       for a in d.actions)

    def test_calendar_never_flattens_or_deploys(self):
        d = plan_calendar_gate(_ev(), None, False, BreakerState())
        assert not d.has(FLATTEN)
        assert not d.has(DEPLOY_NEWS)
        assert not d.has(MARK_CONSUMED)


class TestCalendarGateDeferredWhilePositioned:
    """Suppression halts the bar pipeline before the strategy runs, so it
    is only safe when flat. risk_off keeps that invariant by flattening in
    the same batch; a calendar gate must not flatten (it is pre-emptive,
    not an emergency) — so it defers until the trade exits on its own."""

    def test_new_event_deferred_while_positioned(self):
        d = plan_calendar_gate(_ev("US CPI"), None, False, BreakerState(),
                               has_position=True)
        assert d.actions == []

    def test_upcoming_event_deferred_while_positioned(self):
        d = plan_calendar_gate(None, _ev("FOMC"), False, BreakerState(),
                               has_position=True)
        assert d.actions == []

    def test_same_state_gates_once_flat(self):
        """The deferral is purely about the position — nothing else changed."""
        state = BreakerState()
        assert plan_calendar_gate(_ev("US CPI"), None, False, state,
                                  has_position=True).actions == []
        d = plan_calendar_gate(_ev("US CPI"), None, False, state,
                               has_position=False)
        assert d.kinds == [STAMP_EVENT, SUPPRESS, DISCORD]
        assert d.arg_of(STAMP_EVENT) == "US CPI"
        assert d.arg_of(SUPPRESS) == SRC_EVENT

    def test_deferral_never_flattens_or_deploys(self):
        d = plan_calendar_gate(_ev("US CPI"), None, False, BreakerState(),
                               has_position=True)
        assert not d.has(FLATTEN)
        assert not d.has(DEPLOY_NEWS)

    def test_already_gated_event_with_position_is_still_silent(self):
        """Idempotence unchanged — and it must not UNSUPPRESS either."""
        state = BreakerState(event_suppressed=True, event_name="US CPI")
        d = plan_calendar_gate(_ev("US CPI"), None, False, state,
                               has_position=True)
        assert d.actions == []

    def test_window_passed_still_releases_while_positioned(self):
        """Releasing is always safe with a position open."""
        state = BreakerState(event_suppressed=True, event_name="US CPI")
        d = plan_calendar_gate(None, None, False, state, has_position=True)
        assert d.kinds == [CLEAR_EVENT, UNSUPPRESS, DISCORD]

    def test_stale_release_still_fires_while_positioned(self):
        state = BreakerState(event_suppressed=True, event_name="US CPI")
        d = plan_calendar_gate(None, None, True, state, has_position=True)
        assert d.kinds == [CLEAR_EVENT, UNSUPPRESS, DISCORD]

    def test_default_is_flat(self):
        """Existing 4-arg callers keep the pre-fix behaviour."""
        d = plan_calendar_gate(_ev("US CPI"), None, False, BreakerState())
        assert d.kinds == [STAMP_EVENT, SUPPRESS, DISCORD]


class TestCalendarStaleFailsOpen:
    def test_stale_does_not_gate(self):
        d = plan_calendar_gate(_ev("US CPI"), _ev("FOMC"), True, BreakerState())
        assert d.actions == []

    def test_stale_releases_an_existing_event_gate(self):
        """A forgotten n8n job must not park the bot forever."""
        state = BreakerState(event_suppressed=True, event_name="US CPI")
        d = plan_calendar_gate(None, None, True, state)
        assert d.kinds == [CLEAR_EVENT, UNSUPPRESS, DISCORD]
        assert d.arg_of(UNSUPPRESS) == SRC_EVENT

    def test_stale_leaves_risk_off_suppression_alone(self):
        state = BreakerState(signal_suppressed=True, event_suppressed=True,
                             event_name="X")
        d = plan_calendar_gate(None, None, True, state)
        assert not any(a.kind == UNSUPPRESS and a.arg == SRC_SIGNAL
                       for a in d.actions)


# ── Revert-at-boundary ──

class TestShouldRevertNews:
    def _deployed(self, key="2026-08-04|NIGHT"):
        return BreakerState(news_strategy_active=True,
                            news_deployed_session_key=key)

    def test_no_news_strategy_never_reverts(self):
        assert should_revert_news(BreakerState(), True, "2026-08-05|DAY") is False

    def test_mid_session_never_reverts(self):
        assert should_revert_news(self._deployed(), False, "2026-08-05|DAY") is False

    def test_same_session_pending_does_not_revert(self):
        """Deployed during the gap BEFORE its session — let it run."""
        st = self._deployed("2026-08-04|NIGHT")
        assert should_revert_news(st, True, "2026-08-04|NIGHT") is False

    def test_session_over_reverts(self):
        st = self._deployed("2026-08-04|NIGHT")
        assert should_revert_news(st, True, "2026-08-05|DAY") is True

    def test_unknown_deployed_key_reverts_at_first_gap(self):
        st = self._deployed("")
        assert should_revert_news(st, True, "2026-08-05|DAY") is True

    def test_no_next_session_reverts(self):
        st = self._deployed("2026-08-04|NIGHT")
        assert should_revert_news(st, True, "") is True


class TestBreakerDecisionHelpers:
    def test_bool_is_action_presence(self):
        from src.news.circuit_breaker import BreakerDecision
        assert not BreakerDecision()
        assert BreakerDecision([BreakerAction(DISCORD, reason="hi")])

    def test_consumed_id_empty_without_mark(self):
        assert plan_calendar_gate(_ev(), None, False, BreakerState()).consumed_id == ""

    def test_arg_of_missing_kind(self):
        assert _plan(_sig("clear")).arg_of(DEPLOY_NEWS) == ""
