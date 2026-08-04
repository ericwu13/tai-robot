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

# ── Suppression sources (SUPPRESS/UNSUPPRESS arg) ──
SRC_SIGNAL = "signal"   # Tier 1 risk_off — released only by `clear`
SRC_EVENT = "event"     # scheduled-event calendar — released only by the calendar

# Order tag used for the news flatten, so the decision log / Discord can
# tell a circuit-breaker exit apart from a session-end auto close.
FLATTEN_TAG = "news_risk_off"


@dataclass(frozen=True)
class BreakerAction:
    """One command for the runner to execute."""

    kind: str
    arg: str = ""
    reason: str = ""


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

    @property
    def suppressed(self) -> bool:
        return self.signal_suppressed or self.event_suppressed

    def to_dict(self) -> dict:
        return {
            "signal_suppressed": self.signal_suppressed,
            "event_suppressed": self.event_suppressed,
            "suppressed_reason": self.suppressed_reason,
            "event_name": self.event_name,
            "news_strategy_active": self.news_strategy_active,
            "news_deployed_session_key": self.news_deployed_session_key,
        }

    @classmethod
    def from_dict(cls, data: object) -> "BreakerState":
        """Rebuild from ``session.json``. Junk degrades to a clean state —
        a corrupt session file must not resurrect a phantom suppression."""
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


def plan_signal_actions(
    signal: NewsSignal | None,
    is_consumed: bool,
    cfg_tier2_enabled: bool,
    has_position: bool,
    state: BreakerState,
    in_replay: bool,
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
        if has_position:
            acts.append(BreakerAction(
                FLATTEN, arg=FLATTEN_TAG,
                reason=f"news risk_off ({signal.source or 'n8n'})"))
        acts.append(BreakerAction(
            SUPPRESS, arg=SRC_SIGNAL,
            reason=signal.reason or "news risk_off"))
        acts.append(BreakerAction(
            DISCORD, reason=_msg(
                "🚨 **新聞斷路器 News circuit breaker** — 風險關閉 RISK OFF",
                signal,
                ("已平倉並暫停進場 flattened + entries suppressed"
                 if has_position else
                 "已暫停進場（無持倉）entries suppressed (flat)"))))

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
