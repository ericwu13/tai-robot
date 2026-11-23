"""Layer 4 — Signal routing, source tiering, and outlet breadth.

Responsibilities:
- Release gate: not a price recap, not a duplicate, has a new fact, category
  is not DROP, direction is not NEUTRAL, materiality >= min_materiality_pass (4).
  One floor for bull and bear. Tier 3 alone never triggers a vote or risk_off.
- Full weight is sign * (materiality / 5) * tier_weight. The first distinct
  outlet scores first_sighting_weight of that; full weight at full_weight_outlets.
- Opt-in bearish risk_off still requires check_bearish_market_confirm.
- The router only emits file-contract outputs for RegimeSwitchingRunner.
"""
from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .classifier import ClassifiedEvent
from .config import PipelineConfig, RouterConfig, SourceTierConfig

_TZ_TAIPEI = timezone(timedelta(hours=8))
_QUOTES_FILENAME = "crossmarket_quotes.json"

# Routed action constants
ACTION_DROP = "DROP"
ACTION_ADVISORY_LOW_TIER = "ADVISORY_LOW_TIER"
ACTION_VOTE_BEARISH = "VOTE_BEARISH"
ACTION_VOTE_BULLISH = "VOTE_BULLISH"
ACTION_RISK_OFF = "RISK_OFF"


@dataclass(frozen=True)
class MarketSnapshot:
    """Point-in-time market context for Layer 3 priced-in check & Layer 4 cross-validation.

    Combines W2's ``crossmarket_quotes.json`` sidecar with an optional SKCOM
    live quote/bar hook (``skcom_bar_pct`` / ``skcom_volume_ratio``).
    """

    quotes: dict[str, dict[str, Any]] = field(default_factory=dict)
    skcom_bar_pct: float | None = None
    skcom_volume_ratio: float | None = None
    summary_text: str = ""


def load_market_snapshot(
    signal_path: str = "",
    *,
    skcom_hook: Callable[[], dict[str, Any] | None] | None = None,
    now: datetime | None = None,
    max_file_age_s: int = 1800,
    max_lines: int = 12,
) -> MarketSnapshot:
    """Read W2's ``crossmarket_quotes.json`` sidecar and optional SKCOM hook.

    Checks ``data["asof"]`` age against *max_file_age_s* (default 30 min) so a
    stopped W2 process cannot leave stale ``fresh: true`` flags passing market
    confirmation indefinitely. Advisory and fail-safe — never raises.
    """
    quotes: dict[str, dict[str, Any]] = {}
    lines: list[str] = []

    if signal_path:
        qpath = Path(signal_path).parent / _QUOTES_FILENAME
        try:
            with open(qpath, encoding="utf-8") as f:
                data = json.load(f)
            file_stale = False
            asof_raw = data.get("asof") if isinstance(data, dict) else None
            if asof_raw:
                try:
                    asof_dt = datetime.fromisoformat(str(asof_raw))
                    if asof_dt.tzinfo is None:
                        asof_dt = asof_dt.replace(tzinfo=_TZ_TAIPEI)
                    ref_now = now or datetime.now(_TZ_TAIPEI)
                    if ref_now.tzinfo is None:
                        ref_now = ref_now.replace(tzinfo=_TZ_TAIPEI)
                    age_s = (ref_now.astimezone(timezone.utc) - asof_dt.astimezone(timezone.utc)).total_seconds()
                    if age_s > max_file_age_s:
                        file_stale = True
                except (ValueError, TypeError):
                    file_stale = True
            raw_quotes = data.get("quotes") if isinstance(data, dict) else None
            if isinstance(raw_quotes, dict):
                for sym, q in raw_quotes.items():
                    if not isinstance(q, dict) or q.get("pct") is None:
                        continue
                    qcopy = dict(q)
                    if file_stale:
                        qcopy["fresh"] = False
                    quotes[str(sym)] = qcopy
                    when = ""
                    if qcopy.get("quote_time"):
                        try:
                            when = datetime.fromtimestamp(
                                float(qcopy["quote_time"]), _TZ_TAIPEI
                            ).strftime("%m-%d %H:%M")
                        except (OSError, ValueError, TypeError):
                            when = ""
                    state = "" if qcopy.get("fresh") else " (market closed / stale)"
                    lines.append(f"- {sym}: {float(qcopy['pct']):+.2f}% at {when}{state}")
        except (OSError, ValueError, TypeError):
            pass

    skcom_pct: float | None = None
    skcom_vol: float | None = None
    if callable(skcom_hook):
        try:
            live = skcom_hook()
            if isinstance(live, dict):
                if live.get("pct") is not None:
                    skcom_pct = float(live["pct"])
                    lines.insert(0, f"- TXF(SKCOM): {skcom_pct:+.2f}% (live)")
                if live.get("volume_ratio") is not None:
                    skcom_vol = float(live["volume_ratio"])
        except Exception:  # noqa: BLE001 — advisory hook
            pass

    return MarketSnapshot(
        quotes=quotes,
        skcom_bar_pct=skcom_pct,
        skcom_volume_ratio=skcom_vol,
        summary_text="\n".join(lines[:max_lines]),
    )


def check_bearish_market_confirm(
    market: MarketSnapshot | None, cfg: RouterConfig
) -> tuple[bool, str]:
    """Cross-validate an opt-in ``risk_off`` emission against SKCOM / W2 quotes."""
    if not cfg.emit_risk_off_requires_market_confirm:
        return True, "market_confirm_disabled"
    if market is None:
        return False, "no_market_snapshot"

    if market.skcom_bar_pct is not None:
        if market.skcom_bar_pct <= cfg.bearish_confirm_max_pct:
            return True, f"skcom_not_contradicting ({market.skcom_bar_pct:+.2f}%)"
        return False, f"skcom_rallying ({market.skcom_bar_pct:+.2f}%)"

    fresh_moves: list[tuple[str, float]] = []
    for sym in cfg.bullish_confirm_symbols:
        q = market.quotes.get(sym)
        if not isinstance(q, dict) or not q.get("fresh") or q.get("pct") is None:
            continue
        try:
            fresh_moves.append((sym, float(q["pct"])))
        except (TypeError, ValueError):
            continue

    if not fresh_moves:
        return False, "no_fresh_confirm_quotes"

    strong_up = [(s, p) for s, p in fresh_moves if p > cfg.bearish_confirm_max_pct]
    if strong_up and all(p >= 0.0 for _, p in fresh_moves):
        sym, pct = strong_up[0]
        return False, f"market_rallying_{sym} ({pct:+.2f}%)"

    return True, "market_aligned_or_weak"


@dataclass(frozen=True)
class RoutedSignal:
    """Layer 4 routing outcome for one classified event."""

    event: ClassifiedEvent
    passed: bool
    action: str               # ACTION_* constant
    reject_reason: str
    weighted_score: float     # signed impact fed into session EMA
    full_score: float = 0.0   # weight before the outlet-breadth multiplier
    emit_risk_off: bool = False
    discord_alert: bool = False
    market_note: str = ""


@dataclass(frozen=True)
class RouteBatchOutcome:
    """Session-level routing summary for one W3 scoring pass."""

    routed_signals: tuple[RoutedSignal, ...]
    run_net: float
    session_net: float
    vote_direction: str | None          # "trending-up" | "trending-down" | None
    risk_off_event: RoutedSignal | None # highest-severity opt-in risk_off candidate
    discord_alerts: tuple[RoutedSignal, ...]


def ema_decay_for(half_life_hours: float, interval_minutes: float = 30.0) -> float:
    hl = max(0.25, float(half_life_hours))
    step_h = max(0.05, float(interval_minutes) / 60.0)
    return math.exp(-math.log(2) * step_h / hl)


class SignalRouter:
    """Layer 4 router: one materiality floor and outlet-breadth weighting."""

    def __init__(self, config: PipelineConfig):
        self._cfg: PipelineConfig = config
        self._rt: RouterConfig = config.router
        self._tiers: SourceTierConfig = config.source_tiers
        self._min_mat_pass: int = config.classifier.min_materiality_pass

    def breadth_multiplier(self, outlets: int) -> float:
        """cap at 1 outlet, 1.0 once distinct outlets reach full_weight_outlets."""
        cap = float(self._rt.first_sighting_weight)
        breadth = max(1, int(self._rt.full_weight_outlets))
        seen = max(1, int(outlets))
        if seen >= breadth:
            return 1.0
        return min(1.0, cap + (1.0 - cap) * (seen - 1) / (breadth - 1))

    def evaluate_event(
        self,
        event: ClassifiedEvent,
        market: MarketSnapshot | None = None,
        *,
        emit_risk_off_enabled: bool = False,
        outlets: int = 1,
    ) -> RoutedSignal:
        # 1. Strict 5-condition release gate (Part A §3)
        if event.is_price_recap_only:
            return RoutedSignal(event, False, ACTION_DROP, "price_recap_only", 0.0)
        if event.is_duplicate:
            return RoutedSignal(event, False, ACTION_DROP, "duplicate", 0.0)
        if not event.has_new_fact_delta:
            return RoutedSignal(event, False, ACTION_DROP, "no_new_fact_delta", 0.0)
        if event.category == "DROP":
            return RoutedSignal(event, False, ACTION_DROP, "category_drop", 0.0)
        if event.materiality < self._min_mat_pass:
            return RoutedSignal(event, False, ACTION_DROP, "low_materiality", 0.0)
        if event.direction == "NEUTRAL":
            return RoutedSignal(event, False, ACTION_DROP, "neutral_direction", 0.0)

        # 2. Source tier authority gate
        tier_weight = self._tiers.weight_for(event.source_tier)
        if event.source_tier >= 3 and not self._rt.tier3_can_trigger_vote:
            return RoutedSignal(
                event,
                False,
                ACTION_ADVISORY_LOW_TIER,
                "tier3_cannot_trigger_alone",
                0.0,
                discord_alert=False,
            )

        sign = -1.0 if event.direction == "BEARISH" else 1.0
        full = sign * round((event.materiality / 5.0) * tier_weight, 4)
        weighted = round(full * self.breadth_multiplier(outlets), 4)
        if event.direction == "BEARISH":
            wants_risk_off = False
            mkt_note = ""
            if (
                emit_risk_off_enabled
                and event.materiality >= self._rt.emit_risk_off_min_materiality
                and event.source_tier <= self._rt.emit_risk_off_max_tier
            ):
                ok, mkt_note = check_bearish_market_confirm(market, self._rt)
                wants_risk_off = ok
            return RoutedSignal(
                event=event,
                passed=True,
                action=ACTION_RISK_OFF if wants_risk_off else ACTION_VOTE_BEARISH,
                reject_reason="",
                weighted_score=weighted,
                full_score=full,
                emit_risk_off=wants_risk_off,
                discord_alert=event.materiality >= self._rt.discord_alert_min_materiality,
                market_note=mkt_note,
            )
        return RoutedSignal(
            event=event,
            passed=True,
            action=ACTION_VOTE_BULLISH,
            reject_reason="",
            weighted_score=weighted,
            full_score=full,
            discord_alert=event.materiality >= self._rt.discord_alert_min_materiality,
        )

    def resolve_vote_direction(
        self,
        session_net: float,
        active_vote_direction: str = "",
    ) -> str | None:
        """Asymmetric entry thresholds with withdrawal hysteresis.

        A single deduplicated catalyst only scores once per 4-48h, while
        ``RegimeSwitchingRunner`` reads ``regime_vote_w3.json`` once per night
        at ~04:58 TPE. Hysteresis holds a standing vote until ``session_net``
        fades inside ``(-vote_withdraw_threshold, +vote_withdraw_threshold)``
        or crosses into the opposite direction.
        """
        if session_net <= -self._rt.bearish_vote_threshold:
            return "trending-down"
        if session_net >= self._rt.bullish_vote_threshold:
            return "trending-up"
        withdraw = max(0.0, float(self._rt.vote_withdraw_threshold))
        if active_vote_direction == "trending-down" and session_net <= -withdraw:
            return "trending-down"
        if active_vote_direction == "trending-up" and session_net >= withdraw:
            return "trending-up"
        return None

    def route_batch(
        self,
        events: list[ClassifiedEvent],
        *,
        prev_session_net: float,
        active_vote_direction: str = "",
        market: MarketSnapshot | None = None,
        emit_risk_off_enabled: bool = False,
        interval_minutes: float = 30.0,
        outlets_by_event: dict[str, int] | None = None,
        reweight_delta: float = 0.0,
    ) -> RouteBatchOutcome:
        """Route classified events and compute the session EMA and vote."""
        counts = outlets_by_event or {}
        routed = [
            self.evaluate_event(
                ev, market=market, emit_risk_off_enabled=emit_risk_off_enabled,
                outlets=counts.get(ev.event_id, 1),
            )
            for ev in events
        ]
        run_net = round(sum(r.weighted_score for r in routed) + float(reweight_delta), 4)
        decay = ema_decay_for(self._rt.ema_half_life_hours, interval_minutes)
        session_net = round(float(prev_session_net) * decay + run_net, 4)
        vote_dir = self.resolve_vote_direction(session_net, active_vote_direction)

        risk_off_candidates = [r for r in routed if r.emit_risk_off]
        risk_off_event = (
            max(
                risk_off_candidates,
                key=lambda r: (r.event.materiality, -r.event.source_tier),
            )
            if risk_off_candidates
            else None
        )
        alerts = tuple(r for r in routed if r.discord_alert)

        return RouteBatchOutcome(
            routed_signals=tuple(routed),
            run_net=run_net,
            session_net=session_net,
            vote_direction=vote_dir,
            risk_off_event=risk_off_event,
            discord_alerts=alerts,
        )


def write_risk_off_signal(
    signal_path: str | os.PathLike,
    routed: RoutedSignal,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write a schema-v1 ``signal.json`` ``risk_off`` file atomically.

    Called ONLY when the operator explicitly opts into ``news.rss_emit_risk_off``.
    Uses ``severity: "high"`` (not ``"critical"``) so that under
    ``news.suppress_scope: conflicting_leg`` a bearish news shock gates only
    long legs instead of force-closing a winning short leg. Under
    ``suppress_scope: both``, both legs remain gated.
    """
    moment = now or datetime.now(_TZ_TAIPEI)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_TZ_TAIPEI)
    ev = routed.event
    payload = {
        "version": 1,
        "signal_id": str(uuid.uuid4()),
        "issued_at": moment.astimezone(_TZ_TAIPEI).isoformat(timespec="seconds"),
        "action": "risk_off",
        "severity": "high",
        "source": f"W3-v2-{ev.category.lower()}",
        "reason": f"[{ev.category} M{ev.materiality} T{ev.source_tier}] {ev.summary_one_line}"[:220],
        "direction": "bearish",
    }
    out = Path(signal_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(out) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, out)
    return payload
