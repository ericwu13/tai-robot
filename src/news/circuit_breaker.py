"""斷路器決策核心 News circuit-breaker decision core (pure planner).

Every branch the runner needs is decided HERE, over plain data, with no
runner/broker/COM/Tk in sight.  The Phase 2 glue in
``RegimeSwitchingRunner`` is then a dumb executor: read files → call a
planner → run the returned action list.  This is deliberate — the bugs
in this repo live in the glue, so the glue must contain no branching
worth testing.

Three planners:

- :func:`plan_signal_actions` — the circuit-breaker signal file
  (``risk_off`` / ``clear`` / ``deploy_short`` / ``deploy_long``).
- :func:`plan_suppression_maintenance` — expire a signal suppression no
  fresh ``risk_off`` has refreshed past a session boundary (fail open),
  and emit a once-per-session "still suppressed" reminder.
- :func:`plan_calendar_gate` — the scheduled-event calendar: stamp or
  clear the ``_event_risk`` flag the regime selector sits out on.
- :func:`should_revert_news` — when to hand a deployed event strategy
  back to regime control.

Action representation
---------------------
A decision is an ordered list of :class:`BreakerAction`, each a
``(kind, arg, reason)`` triple.  ``kind`` is one of the module-level
constants; ``arg`` is kind-specific; ``reason`` is human-readable
(bilingual for anything that reaches Discord):

===================  =========================  ===========================
kind                 arg                        meaning
===================  =========================  ===========================
``FLATTEN``          ``""``                     force-close the position
``SUPPRESS``         ``SRC_SIGNAL``/``SRC_EVENT``  block entries, per source
``UNSUPPRESS``       ``SRC_SIGNAL``/``SRC_EVENT``  release that source only
``DEPLOY_NEWS``      ``"short"`` / ``"long"``   swap to the event strategy
``STAMP_EVENT``      event name                 set ``_event_risk``
``CLEAR_EVENT``      ``""``                     clear ``_event_risk``
``DISCORD``          ``""``                     ``reason`` is the message
``MARK_CONSUMED``    signal_id                  ledger write (always last)
``STAMP_NOTICE``     session key                still-suppressed reminder sent
===================  =========================  ===========================

Two invariants the tests pin down:

1. **Always mark consumed.**  Any valid, unconsumed signal yields a
   ``MARK_CONSUMED`` action even when it produces no trading action at
   all (tier 2 disabled, history replay in progress).  Without it the
   30s poll re-acts on the same file until it ages out.
2. **Two independent suppression sources.**  A ``clear`` signal releases
   only :data:`SRC_SIGNAL`; the calendar releases only
   :data:`SRC_EVENT`.  Entries stay blocked while either holds, so a
   ``clear`` tapped during an FOMC session cannot un-gate the event.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .event_calendar import EventEntry
from .signal_file import NewsSignal

# ── Action kinds ──
FLATTEN = "flatten"
SUPPRESS = "suppress"
UNSUPPRESS = "unsuppress"
DEPLOY_NEWS = "deploy_news"
STAMP_EVENT = "stamp_event"
CLEAR_EVENT = "clear_event"
DISCORD = "discord"
MARK_CONSUMED = "mark_consumed"
STAMP_NOTICE = "stamp_notice"   # arg = session key the reminder covered

# ── Suppression sources (SUPPRESS/UNSUPPRESS arg) ──
SRC_SIGNAL = "signal"   # Tier 1 risk_off — released only by `clear`
SRC_EVENT = "event"     # scheduled-event calendar — released only by the calendar

# Order tag used for the news flatten, so the decision log / Discord can
# tell a circuit-breaker exit apart from a session-end auto close.
FLATTEN_TAG = "news_risk_off"


@dataclass(frozen=True)
class BreakerAction:
    """One command for the runner to execute.

    ``direction``/``severity``/``session_key`` are carried only on
    ``SUPPRESS(SRC_SIGNAL)`` actions — the runner stamps them onto
    :class:`BreakerState` so later polls can gate per-leg and expire the
    suppression at a session boundary. Blank everywhere else.
    """

    kind: str
    arg: str = ""
    reason: str = ""
    direction: str = ""
    severity: str = ""
    session_key: str = ""


@dataclass
class BreakerState:
    """斷路器狀態 Circuit-breaker state, persisted in ``session.json``.

    ``signal_suppressed`` and ``event_suppressed`` are tracked apart on
    purpose — see the module docstring.  ``suppressed`` is the OR of the
    two and is what the runner mirrors onto ``LiveRunner.news_idle``.
    """

    signal_suppressed: bool = False
    event_suppressed: bool = False
    suppressed_reason: str = ""
    event_name: str = ""                  # currently stamped `_event_risk`
    news_strategy_active: bool = False
    news_deployed_session_key: str = ""   # "" when no event strategy is deployed
    # ── Signal-suppression metadata (blank unless signal_suppressed) ──
    signal_direction: str = ""            # "bearish"/"bullish"/"" from the signal
    signal_severity: str = ""             # "critical" gates both legs regardless of scope
    signal_session_key: str = ""          # session the suppression was (re)stamped in;
                                          # expiry anchor — "" means unknown age (legacy)
    signal_suppressed_at: str = ""        # human-readable TPE stamp, display only
    notice_session_key: str = ""          # last session a still-suppressed reminder went out

    @property
    def suppressed(self) -> bool:
        return self.signal_suppressed or self.event_suppressed

    def gates_leg(self, active_leg: str, scope: str = "both") -> bool:
        """Does the current suppression stop *active_leg* from trading?

        The event source always gates: scheduled macro events (FOMC, CPI)
        cut both ways, so the calendar carries no direction. The signal
        source gates per *scope*:

        - ``"both"`` (default): any signal suppression gates every leg —
          the pre-direction behavior, unchanged.
        - ``"conflicting_leg"``: a directional signal gates only the leg
          it would hurt (bearish → long legs, bullish → short legs).
          No direction, or severity ``"critical"``, still gates
          everything — unknown danger fails closed.

        Leg matching is by suffix so event legs participate too
        (``"news_short"`` counts as short). ``"idle"`` is gated, which is
        harmless — an idle leg does not trade anyway.
        """
        if self.event_suppressed:
            return True
        if not self.signal_suppressed:
            return False
        if scope != "conflicting_leg":
            return True
        if self.signal_severity == "critical":
            return True
        if self.signal_direction == "bearish":
            return not active_leg.endswith("short")
        if self.signal_direction == "bullish":
            return not active_leg.endswith("long")
        return True

    def to_dict(self) -> dict:
        return {
            "signal_suppressed": self.signal_suppressed,
            "event_suppressed": self.event_suppressed,
            "suppressed_reason": self.suppressed_reason,
            "event_name": self.event_name,
            "news_strategy_active": self.news_strategy_active,
            "news_deployed_session_key": self.news_deployed_session_key,
            "signal_direction": self.signal_direction,
            "signal_severity": self.signal_severity,
            "signal_session_key": self.signal_session_key,
            "signal_suppressed_at": self.signal_suppressed_at,
            "notice_session_key": self.notice_session_key,
        }

    @classmethod
    def from_dict(cls, data: object) -> "BreakerState":
        """Rebuild from ``session.json``. Junk degrades to a clean state —
        a corrupt session file must not resurrect a phantom suppression.

        A pre-expiry session file has no ``signal_session_key``; the field
        defaults to "" and :func:`plan_suppression_maintenance` releases
        such a suppression on the first poll (unknown age fails open).
        """
        if not isinstance(data, dict):
            return cls()
        return cls(
            signal_suppressed=bool(data.get("signal_suppressed", False)),
            event_suppressed=bool(data.get("event_suppressed", False)),
            suppressed_reason=str(data.get("suppressed_reason", "") or ""),
            event_name=str(data.get("event_name", "") or ""),
            news_strategy_active=bool(data.get("news_strategy_active", False)),
            news_deployed_session_key=str(
                data.get("news_deployed_session_key", "") or ""),
            signal_direction=str(data.get("signal_direction", "") or ""),
            signal_severity=str(data.get("signal_severity", "") or ""),
            signal_session_key=str(data.get("signal_session_key", "") or ""),
            signal_suppressed_at=str(data.get("signal_suppressed_at", "") or ""),
            notice_session_key=str(data.get("notice_session_key", "") or ""),
        )


@dataclass
class BreakerDecision:
    """An ordered action list. Empty = nothing to do."""

    actions: list[BreakerAction] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.actions)

    @property
    def kinds(self) -> list[str]:
        return [a.kind for a in self.actions]

    @property
    def consumed_id(self) -> str:
        """signal_id to write to the ledger, or "" when there is none.

        The runner marks this in a ``finally`` so a crashing action can
        never leave the signal armed for the next poll.
        """
        for a in self.actions:
            if a.kind == MARK_CONSUMED:
                return a.arg
        return ""

    @property
    def executable(self) -> list[BreakerAction]:
        """Everything except the ledger write (which the runner defers)."""
        return [a for a in self.actions if a.kind != MARK_CONSUMED]

    def has(self, kind: str) -> bool:
        return any(a.kind == kind for a in self.actions)

    def arg_of(self, kind: str) -> str:
        for a in self.actions:
            if a.kind == kind:
                return a.arg
        return ""


def _source_line(signal: NewsSignal) -> str:
    bits = []
    if signal.source:
        bits.append(f"來源 Source: `{signal.source}`")
    if signal.severity:
        bits.append(f"等級 Severity: `{signal.severity}`")
    if signal.reason:
        bits.append(f"理由 Reason: {signal.reason}")
    return "\n".join(bits)


def _msg(title: str, signal: NewsSignal, tail: str = "") -> str:
    parts = [title, _source_line(signal)]
    if tail:
        parts.append(tail)
    return "\n".join(p for p in parts if p)


def _position_conflicts(direction: str, position_side: str) -> bool:
    """Would a *direction* shock hurt a *position_side* holding?

    Unknown direction or side is treated as conflicting — when in doubt,
    the emergency exit stays an emergency exit.
    """
    side = (position_side or "").lower()
    if direction == "bearish":
        return side != "short"
    if direction == "bullish":
        return side != "long"
    return True


def plan_signal_actions(
    signal: NewsSignal | None,
    is_consumed: bool,
    cfg_tier2_enabled: bool,
    has_position: bool,
    state: BreakerState,
    in_replay: bool,
    *,
    suppress_scope: str = "both",
    position_side: str = "",
    session_key: str = "",
) -> BreakerDecision:
    """Plan the response to one circuit-breaker signal.

    Args:
        signal: validated signal, or None when the file is absent/stale/
            invalid (``read_signal`` already rejected it).
        is_consumed: ledger says this ``signal_id`` was already acted on.
        cfg_tier2_enabled: ``news.tier2_enabled`` — forced-entry deploys.
        has_position: the simulated broker holds a position right now.
        state: current breaker state (read-only here).
        in_replay: history replay / reload window is in progress.
        suppress_scope: ``news.suppress_scope`` — ``"both"`` (a risk_off
            gates every leg, pre-direction behavior) or
            ``"conflicting_leg"`` (a directional risk_off gates only the
            leg it would hurt; see :meth:`BreakerState.gates_leg`).
        position_side: ``"long"``/``"short"``/"" — under
            ``conflicting_leg`` a directional risk_off flattens only a
            CONFLICTING position (bearish flattens a long); a
            same-direction position is left to its strategy's own exits,
            whose pipeline keeps running because the leg is not gated.
        session_key: the session (current, else next) the suppression is
            stamped for — the expiry anchor
            :func:`plan_suppression_maintenance` checks. A re-fired
            risk_off refreshes the stamp.

    Returns an empty decision for a consumed or absent signal; otherwise
    a list that ALWAYS ends with ``MARK_CONSUMED``.
    """
    acts: list[BreakerAction] = []
    if signal is None or is_consumed:
        return BreakerDecision(acts)

    sid = signal.signal_id
    action = signal.action

    # Replay must never trade on a live signal: the bars being replayed
    # are minutes-to-hours old, and the broker is mid-reconstruction.
    # Consume it anyway — re-acting when replay ends would fire the
    # breaker on a move that is already history.
    if in_replay:
        acts.append(BreakerAction(
            DISCORD, reason=_msg(
                f"⏸️ **新聞斷路器 News circuit breaker** — 回補中略過 skipped "
                f"during history replay (`{action}`)", signal,
                "回補期間不交易；此訊號已標記為已處理 "
                "no trading during replay; signal marked consumed")))
        acts.append(BreakerAction(MARK_CONSUMED, arg=sid))
        return BreakerDecision(acts)

    if action == "risk_off":
        directional = (suppress_scope == "conflicting_leg"
                       and signal.severity != "critical"
                       and signal.direction in ("bearish", "bullish"))
        keep_position = (has_position and directional
                         and not _position_conflicts(signal.direction,
                                                     position_side))
        if has_position and not keep_position:
            acts.append(BreakerAction(
                FLATTEN, arg=FLATTEN_TAG,
                reason=f"news risk_off ({signal.source or 'n8n'})"))
        acts.append(BreakerAction(
            SUPPRESS, arg=SRC_SIGNAL,
            reason=signal.reason or "news risk_off",
            direction=signal.direction, severity=signal.severity,
            session_key=session_key))
        if keep_position:
            tail = (f"同向持倉保留（{signal.direction}）same-direction "
                    f"position kept; only the conflicting leg is gated")
        elif has_position:
            tail = "已平倉並暫停進場 flattened + entries suppressed"
        else:
            tail = "已暫停進場（無持倉）entries suppressed (flat)"
        if directional and not has_position:
            tail += (f"\n方向 Direction: `{signal.direction}` — "
                     f"只擋衝突方向 gates the conflicting leg only")
        acts.append(BreakerAction(
            DISCORD, reason=_msg(
                "🚨 **新聞斷路器 News circuit breaker** — 風險關閉 RISK OFF",
                signal, tail)))

    elif action == "clear":
        acts.append(BreakerAction(
            UNSUPPRESS, arg=SRC_SIGNAL, reason=signal.reason or "news clear"))
        acts.append(BreakerAction(
            DISCORD, reason=_msg(
                "✅ **新聞斷路器 News circuit breaker** — 解除 CLEAR", signal,
                "訊號暫停已解除；行事曆事件暫停不受影響 "
                "signal suppression released (event-calendar gate unaffected)")))

    elif action in ("deploy_short", "deploy_long"):
        direction = "short" if action == "deploy_short" else "long"
        if not cfg_tier2_enabled:
            acts.append(BreakerAction(
                DISCORD, reason=_msg(
                    f"ℹ️ **新聞斷路器 News circuit breaker** — 忽略 ignored "
                    f"`{action}`", signal,
                    "Tier 2 未啟用 tier2_enabled=false — 未部署事件策略 "
                    "no event strategy deployed")))
        else:
            if has_position:
                acts.append(BreakerAction(
                    FLATTEN, arg=FLATTEN_TAG,
                    reason=f"news {action} ({signal.source or 'n8n'})"))
            acts.append(BreakerAction(
                DEPLOY_NEWS, arg=direction,
                reason=signal.reason or f"news {action}"))
            # The event strategy exists to enter — a suppression left over
            # from an earlier risk_off would silently swallow its one shot.
            acts.append(BreakerAction(
                UNSUPPRESS, arg=SRC_SIGNAL, reason=f"news {action}"))
            acts.append(BreakerAction(
                DISCORD, reason=_msg(
                    f"⚡ **新聞斷路器 News circuit breaker** — 事件策略部署 "
                    f"deploy **{direction.upper()}**", signal,
                    ("已平倉後部署 flattened, then deployed"
                     if has_position else "已部署 deployed"))))

    acts.append(BreakerAction(MARK_CONSUMED, arg=sid))
    return BreakerDecision(acts)


def plan_suppression_maintenance(
    state: BreakerState,
    current_session_key: str,
) -> BreakerDecision:
    """Expire an unrefreshed signal suppression; remind while suppressed.

    Two session-keyed jobs:

    1. **Expiry (fail open).** A ``risk_off`` is information about the
       session it fired in. The monitor re-fires while the breach
       persists (each fire is a fresh ``signal_id``, so the ledger does
       not swallow it), which re-stamps ``signal_session_key``; once the
       session key has moved on without a refresh the suppression is
       stale and is released. A suppression with no session key (carried
       in from a pre-expiry ``session.json``) has unknown age and is
       released immediately. Same rule the calendar gate already
       follows — a forgotten n8n job must not park the bot forever — and
       the mirror of the 900s freshness gate on *acting*: if relevance
       decays for acting on a signal, it decays for staying suppressed
       by one. (The 2026-08-18→20 incident: a latched risk_off idled a
       bot for two days because no ``clear`` ever came.)
    2. **Reminder.** While EITHER source is still suppressed, one
       Discord line per session, so a silently idle bot is noticed in
       hours, not discovered days later by accident.

    The event source is never expired here — the calendar stamps and
    clears it against its own schedule (and already fails open when
    stale).

    A blank *current_session_key* (no session resolvable — deep holiday
    edge) holds everything: no expiry, no reminder. Conservative, and
    the next resolvable poll catches up.
    """
    acts: list[BreakerAction] = []
    signal_still_on = state.signal_suppressed

    if signal_still_on and current_session_key:
        legacy = not state.signal_session_key
        moved = (not legacy
                 and current_session_key != state.signal_session_key)
        if legacy or moved:
            why = ("legacy suppression with no expiry key (unknown age)"
                   if legacy else
                   f"session moved {state.signal_session_key} → "
                   f"{current_session_key} without a fresh risk_off")
            acts.append(BreakerAction(
                UNSUPPRESS, arg=SRC_SIGNAL, reason=f"expired: {why}"))
            acts.append(BreakerAction(
                DISCORD, reason=(
                    "⏲️ **新聞暫停到期 News suppression EXPIRED** — 自動解除 "
                    "released (fail open)\n"
                    f"原因 Was: {state.suppressed_reason or '(no reason)'}\n"
                    f"到期 Expiry: {why}\n"
                    "持續的風險會由監控重新觸發 an ongoing breach re-fires "
                    "the monitor and re-suppresses")))
            signal_still_on = False

    if ((signal_still_on or state.event_suppressed)
            and current_session_key
            and state.notice_session_key != current_session_key):
        srcs = []
        if signal_still_on:
            d = f", {state.signal_direction}" if state.signal_direction else ""
            since = (f" since {state.signal_suppressed_at}"
                     if state.signal_suppressed_at else "")
            srcs.append(f"signal ({state.suppressed_reason or 'risk_off'}"
                        f"{d}){since}")
        if state.event_suppressed:
            srcs.append(f"event ({state.event_name or 'scheduled event'})")
        acts.append(BreakerAction(
            DISCORD, reason=(
                "⏸️ **進場仍暫停中 Entries still SUPPRESSED** — "
                f"{current_session_key}\n"
                f"來源 Source: {'; '.join(srcs)}\n"
                "解除 To clear: `clear` 訊號 signal / 等到期 wait for expiry")))
        acts.append(BreakerAction(STAMP_NOTICE, arg=current_session_key))

    return BreakerDecision(acts)


def plan_calendar_gate(
    active_ev: EventEntry | None,
    upcoming_ev: EventEntry | None,
    calendar_is_stale: bool,
    state: BreakerState,
    has_position: bool = False,
) -> BreakerDecision:
    """Plan the ``_event_risk`` stamp/clear and the event-session gate.

    The gate covers both the session in progress (``active_ev``) and the
    one about to open (``upcoming_ev``), so a bot polling inside a closed
    gap sits the coming session out before it opens.  ``active_ev`` wins
    when both match.

    **Deferred while a position is open.**  Suppression stops the whole
    bar pipeline before the strategy runs, so it is only safe under an
    "suppressed ⇒ flat" invariant.  ``risk_off`` preserves that invariant
    by flattening in the same batch; a calendar gate must NOT — it is
    pre-emptive, not an emergency, and flattening on a scheduled event
    the bot is already positioned through would be a surprise exit.  So
    when an event would newly gate while a position is open, this returns
    NO actions at all: the open trade keeps its normal stop/exit
    management, and the 30s poll re-plans until the position closes on
    its own terms, at which point the gate engages.  (Without this, an
    events.json refreshed mid-session would freeze an open position's
    exit management until the event window ended.)

    A STALE calendar fails OPEN: no new gating, and any gate this module
    previously stamped is released.  A forgotten n8n job must not be able
    to park the bot forever — that is the exact failure mode the
    fail-open rule exists to prevent.

    Releasing is always safe with a position open, so the stale-release
    and window-passed branches ignore ``has_position``.

    Idempotent: re-planning with the same event in place yields no
    actions, so the 30s poll does not re-stamp or re-announce.
    """
    acts: list[BreakerAction] = []

    if calendar_is_stale:
        if state.event_suppressed or state.event_name:
            acts.append(BreakerAction(CLEAR_EVENT))
            acts.append(BreakerAction(
                UNSUPPRESS, arg=SRC_EVENT, reason="calendar stale — fail open"))
            acts.append(BreakerAction(
                DISCORD, reason=(
                    "⚠️ **事件行事曆過期 Event calendar stale** — "
                    f"解除事件暫停 releasing event gate (`{state.event_name}`)\n"
                    "過期行事曆一律不閘控 a stale calendar never gates")))
        return BreakerDecision(acts)

    ev = active_ev or upcoming_ev

    if ev is not None:
        if state.event_name == ev.name and state.event_suppressed:
            return BreakerDecision(acts)   # already gated by this event
        if has_position:
            # Deferral, not a decision: no stamp, no Discord, no release
            # either. The next poll re-plans once the trade is out.
            return BreakerDecision(acts)
        when = "本盤 current session" if active_ev is not None else "下一盤 next session"
        acts.append(BreakerAction(STAMP_EVENT, arg=ev.name))
        acts.append(BreakerAction(
            SUPPRESS, arg=SRC_EVENT, reason=f"scheduled event: {ev.name}"))
        acts.append(BreakerAction(
            DISCORD, reason=(
                f"📅 **排程事件迴避 Scheduled event** — {when}\n"
                f"事件 Event: **{ev.name}** (`{ev.severity}`, {ev.date})\n"
                f"動作 Action: 暫停進場 entries suppressed")))
        return BreakerDecision(acts)

    if state.event_suppressed or state.event_name:
        acts.append(BreakerAction(CLEAR_EVENT))
        acts.append(BreakerAction(
            UNSUPPRESS, arg=SRC_EVENT, reason="event window passed"))
        acts.append(BreakerAction(
            DISCORD, reason=(
                f"📅 **排程事件結束 Scheduled event over** — "
                f"`{state.event_name}`\n"
                f"動作 Action: 恢復進場 entries re-enabled "
                f"(除非其他來源仍暫停 unless another source still suppresses)")))
    return BreakerDecision(acts)


def should_revert_news(
    state: BreakerState,
    is_closed_gap: bool,
    next_session_key: str,
) -> bool:
    """True when a deployed event strategy should be handed back to regime.

    Reverting happens in a closed gap only (the same window regime swaps
    use — flat, no ticks).  ``news_deployed_session_key`` is the session
    the deploy was FOR: the live one when deployed mid-session, otherwise
    the session the gap leads into.  While the next session to open is
    still that one, the deploy has not had its session yet and must
    stand; once ``next_session_key`` has moved on, the session is over.

    An unknown/blank deployed key reverts at the first gap — the event
    strategy never re-enters anyway, so the risk is being parked on a
    dead strategy, and returning to regime control is the safe default.
    """
    if not state.news_strategy_active:
        return False
    if not is_closed_gap:
        return False
    if not state.news_deployed_session_key:
        return True
    return next_session_key != state.news_deployed_session_key
