"""Direction-aware suppression + session-boundary expiry (issue: the
2026-08-18→20 incident — a bearish risk_off latched for two days and
silenced a SHORT strategy through three profitable signals).

Pins four behaviors:

1. ``BreakerState.gates_leg`` — per-leg gating under ``conflicting_leg``
   scope; ``both`` scope and the event source are direction-blind.
2. ``plan_signal_actions`` — a directional risk_off keeps a
   same-direction position, and the SUPPRESS action carries
   direction/severity/session_key for the runner to stamp.
3. ``plan_suppression_maintenance`` — expiry when the session key moved
   without a fresh risk_off, immediate release of legacy (key-less)
   suppressions, and the once-per-session still-suppressed reminder.
4. ``read_signal`` — the optional ``direction`` field parses and junk
   degrades to "" (gates both, the safe default).
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.news.circuit_breaker import (
    DISCORD,
    FLATTEN,
    MARK_CONSUMED,
    SRC_SIGNAL,
    STAMP_NOTICE,
    SUPPRESS,
    UNSUPPRESS,
    BreakerState,
    plan_signal_actions,
    plan_suppression_maintenance,
)
from src.news.signal_file import NewsSignal, read_signal

_TZ = timezone(timedelta(hours=8))


def _sig(action="risk_off", signal_id="sig-1", **kw):
    return NewsSignal(
        signal_id=signal_id,
        action=action,
        issued_at=datetime(2026, 8, 19, 22, 5, tzinfo=_TZ),
        severity=kw.get("severity", "high"),
        source=kw.get("source", "crossmarket-monitor"),
        reason=kw.get("reason", "downside breach: SOXX -2.52%"),
        direction=kw.get("direction", ""),
    )


def _plan(signal, **kw):
    return plan_signal_actions(
        signal,
        kw.get("consumed", False),
        kw.get("tier2", False),
        kw.get("has_position", False),
        kw.get("state", BreakerState()),
        kw.get("in_replay", False),
        suppress_scope=kw.get("suppress_scope", "both"),
        position_side=kw.get("position_side", ""),
        session_key=kw.get("session_key", ""),
    )


# ── gates_leg ──

class TestGatesLeg:
    def test_not_suppressed_never_gates(self):
        st = BreakerState()
        assert st.gates_leg("short", "conflicting_leg") is False
        assert st.gates_leg("long", "both") is False

    def test_scope_both_gates_every_leg(self):
        st = BreakerState(signal_suppressed=True, signal_direction="bearish")
        for leg in ("long", "short", "idle", "news_short"):
            assert st.gates_leg(leg, "both") is True

    def test_bearish_gates_long_not_short(self):
        st = BreakerState(signal_suppressed=True, signal_direction="bearish")
        assert st.gates_leg("long", "conflicting_leg") is True
        assert st.gates_leg("short", "conflicting_leg") is False
        assert st.gates_leg("news_short", "conflicting_leg") is False

    def test_bullish_gates_short_not_long(self):
        st = BreakerState(signal_suppressed=True, signal_direction="bullish")
        assert st.gates_leg("short", "conflicting_leg") is True
        assert st.gates_leg("long", "conflicting_leg") is False
        assert st.gates_leg("news_long", "conflicting_leg") is False

    def test_unknown_direction_fails_closed(self):
        st = BreakerState(signal_suppressed=True, signal_direction="")
        assert st.gates_leg("short", "conflicting_leg") is True
        assert st.gates_leg("long", "conflicting_leg") is True

    def test_critical_severity_gates_both(self):
        st = BreakerState(signal_suppressed=True, signal_direction="bearish",
                          signal_severity="critical")
        assert st.gates_leg("short", "conflicting_leg") is True

    def test_idle_leg_is_gated_harmlessly(self):
        st = BreakerState(signal_suppressed=True, signal_direction="bearish")
        assert st.gates_leg("idle", "conflicting_leg") is True

    def test_event_source_is_direction_blind(self):
        st = BreakerState(event_suppressed=True, event_name="FOMC")
        assert st.gates_leg("short", "conflicting_leg") is True
        assert st.gates_leg("long", "conflicting_leg") is True

    def test_dict_roundtrip_keeps_direction_fields(self):
        st = BreakerState(
            signal_suppressed=True, signal_direction="bearish",
            signal_severity="high", signal_session_key="2026-08-19|NIGHT",
            signal_suppressed_at="2026-08-19 22:10",
            notice_session_key="2026-08-19|NIGHT")
        assert BreakerState.from_dict(st.to_dict()) == st

    def test_from_dict_legacy_file_defaults_blank_key(self):
        # a pre-expiry session.json: suppression carried, no session key —
        # plan_suppression_maintenance releases it on the first poll
        st = BreakerState.from_dict({"signal_suppressed": True,
                                     "suppressed_reason": "old risk_off"})
        assert st.signal_suppressed is True
        assert st.signal_session_key == ""


# ── plan_signal_actions: direction ──

class TestDirectionalRiskOff:
    def test_suppress_action_carries_metadata(self):
        d = _plan(_sig(direction="bearish"),
                  session_key="2026-08-19|NIGHT")
        sup = next(a for a in d.actions if a.kind == SUPPRESS)
        assert sup.arg == SRC_SIGNAL
        assert sup.direction == "bearish"
        assert sup.severity == "high"
        assert sup.session_key == "2026-08-19|NIGHT"

    def test_conflicting_position_still_flattened(self):
        d = _plan(_sig(direction="bearish"), has_position=True,
                  suppress_scope="conflicting_leg", position_side="long")
        assert d.has(FLATTEN)

    def test_same_direction_position_kept(self):
        d = _plan(_sig(direction="bearish"), has_position=True,
                  suppress_scope="conflicting_leg", position_side="short")
        assert not d.has(FLATTEN)
        assert d.has(SUPPRESS)          # the long leg still gets gated
        assert d.has(MARK_CONSUMED)

    def test_scope_both_always_flattens(self):
        d = _plan(_sig(direction="bearish"), has_position=True,
                  suppress_scope="both", position_side="short")
        assert d.has(FLATTEN)

    def test_critical_severity_always_flattens(self):
        d = _plan(_sig(direction="bearish", severity="critical"),
                  has_position=True,
                  suppress_scope="conflicting_leg", position_side="short")
        assert d.has(FLATTEN)

    def test_directionless_signal_always_flattens(self):
        d = _plan(_sig(direction=""), has_position=True,
                  suppress_scope="conflicting_leg", position_side="short")
        assert d.has(FLATTEN)

    def test_unknown_position_side_flattens(self):
        d = _plan(_sig(direction="bearish"), has_position=True,
                  suppress_scope="conflicting_leg", position_side="")
        assert d.has(FLATTEN)


# ── plan_suppression_maintenance ──

def _suppressed(key="2026-08-19|NIGHT", **kw):
    return BreakerState(
        signal_suppressed=True,
        suppressed_reason=kw.get("reason", "downside breach: SOXX -2.52%"),
        signal_direction=kw.get("direction", "bearish"),
        signal_session_key=key,
        signal_suppressed_at=kw.get("at", "2026-08-19 22:10"),
        notice_session_key=kw.get("notice_key", ""),
        event_suppressed=kw.get("event", False),
        event_name=kw.get("event_name", ""),
    )


class TestSuppressionExpiry:
    def test_same_session_holds(self):
        d = plan_suppression_maintenance(
            _suppressed("2026-08-19|NIGHT", notice_key="2026-08-19|NIGHT"),
            "2026-08-19|NIGHT")
        assert not d.has(UNSUPPRESS)

    def test_session_moved_releases(self):
        d = plan_suppression_maintenance(
            _suppressed("2026-08-19|NIGHT"), "2026-08-20|DAY")
        rel = next(a for a in d.actions if a.kind == UNSUPPRESS)
        assert rel.arg == SRC_SIGNAL
        assert d.has(DISCORD)

    def test_legacy_keyless_suppression_releases_immediately(self):
        # the exact 08-18→20 failure: suppression restored from an old
        # session.json with unknown age must fail open, not latch
        d = plan_suppression_maintenance(
            _suppressed(key=""), "2026-08-20|DAY")
        assert d.has(UNSUPPRESS)

    def test_blank_current_session_holds_everything(self):
        d = plan_suppression_maintenance(_suppressed("2026-08-19|NIGHT"), "")
        assert d.actions == []

    def test_not_suppressed_is_silent(self):
        d = plan_suppression_maintenance(BreakerState(), "2026-08-20|DAY")
        assert d.actions == []

    def test_event_source_never_expired_here(self):
        st = BreakerState(event_suppressed=True, event_name="FOMC",
                          notice_session_key="2026-08-20|DAY")
        d = plan_suppression_maintenance(st, "2026-08-20|DAY")
        assert not d.has(UNSUPPRESS)

    def test_release_does_not_touch_event_gate(self):
        st = _suppressed("2026-08-19|NIGHT", event=True, event_name="FOMC",
                         notice_key="2026-08-20|DAY")
        d = plan_suppression_maintenance(st, "2026-08-20|DAY")
        rel = [a for a in d.actions if a.kind == UNSUPPRESS]
        assert [a.arg for a in rel] == [SRC_SIGNAL]


class TestSuppressionReminder:
    def test_reminder_once_per_session(self):
        st = _suppressed("2026-08-19|NIGHT", notice_key="")
        d = plan_suppression_maintenance(st, "2026-08-19|NIGHT")
        assert d.has(DISCORD)
        stamp = next(a for a in d.actions if a.kind == STAMP_NOTICE)
        assert stamp.arg == "2026-08-19|NIGHT"
        # runner stamps notice_session_key; a re-poll is then silent
        st.notice_session_key = "2026-08-19|NIGHT"
        again = plan_suppression_maintenance(st, "2026-08-19|NIGHT")
        assert again.actions == []

    def test_reminder_covers_event_suppression(self):
        st = BreakerState(event_suppressed=True, event_name="FOMC")
        d = plan_suppression_maintenance(st, "2026-08-20|DAY")
        assert d.has(DISCORD)
        assert d.has(STAMP_NOTICE)

    def test_no_reminder_after_expiry_released_everything(self):
        # signal expired this poll and no event gate: released, done —
        # a "still suppressed" reminder now would be a lie
        d = plan_suppression_maintenance(
            _suppressed("2026-08-19|NIGHT"), "2026-08-20|DAY")
        assert d.has(UNSUPPRESS)
        assert not d.has(STAMP_NOTICE)

    def test_reminder_names_reason_and_direction(self):
        d = plan_suppression_maintenance(
            _suppressed("2026-08-19|NIGHT", notice_key=""),
            "2026-08-19|NIGHT")
        msg = next(a for a in d.actions if a.kind == DISCORD).reason
        assert "SOXX -2.52%" in msg
        assert "bearish" in msg


# ── read_signal: direction field ──

class TestSignalDirection:
    def _write(self, tmp_path, extra):
        payload = {
            "version": 1, "signal_id": "abc", "action": "risk_off",
            "issued_at": "2026-08-19T22:10:01+08:00",
            "severity": "high", "source": "t", "reason": "r",
        }
        payload.update(extra)
        p = tmp_path / "signal.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return str(p)

    _NOW = datetime(2026, 8, 19, 22, 11, tzinfo=_TZ)

    def test_direction_parses(self, tmp_path):
        sig, _ = read_signal(self._write(tmp_path, {"direction": "bearish"}),
                             now=self._NOW)
        assert sig.direction == "bearish"

    def test_direction_case_insensitive(self, tmp_path):
        sig, _ = read_signal(self._write(tmp_path, {"direction": " Bullish "}),
                             now=self._NOW)
        assert sig.direction == "bullish"

    @pytest.mark.parametrize("junk", ["sideways", 123, None, {}])
    def test_junk_direction_degrades_to_blank(self, tmp_path, junk):
        sig, _ = read_signal(self._write(tmp_path, {"direction": junk}),
                             now=self._NOW)
        assert sig is not None
        assert sig.direction == ""

    def test_absent_direction_is_blank(self, tmp_path):
        sig, _ = read_signal(self._write(tmp_path, {}), now=self._NOW)
        assert sig.direction == ""
