"""Four-layer news signal funnel (Filter -> Deduplicate -> Classify -> Route).

Public entry point :class:`NewsSignalPipeline` coordinates:
- Layer 1: :class:`NewsPreFilter` (hard regex rules, URL/title cleanup, source tiers)
- Layer 2: :class:`IncrementalEventClusterer` (SimHash + O(k) sliding-window centroid
  clustering + 24-48h cross-day archive)
- Layer 3: :class:`GeminiEventClassifier` (strict JSON event taxonomy + delta check)
- Layer 4: :class:`SignalRouter` (source tiering, asymmetric gates, market confirmation)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .classifier import (
    ClassifiedEvent,
    ClassifierAPIError,
    GeminiEventClassifier,
    build_classification_prompt,
    parse_classified_event,
)
from .config import (
    DEFAULT_CONFIG_PATH,
    ClassifierConfig,
    DedupConfig,
    PipelineConfig,
    PreFilterConfig,
    RouterConfig,
    SourceTierConfig,
    load_pipeline_config,
)
from .dedup import (
    DedupDecision,
    Embedder,
    EventCluster,
    GeminiEmbedder,
    IncrementalEventClusterer,
    LexicalFallbackEmbedder,
    PriorEventContext,
    build_embedder,
    cosine_similarity,
    hamming_distance,
    normalized_title_key,
    simhash64,
)
from .prefilter import (
    NewsPreFilter,
    PreFilterResult,
    clean_title,
    normalize_url,
    resolve_source_tier,
)
from .router import (
    ACTION_ADVISORY_LOW_TIER,
    ACTION_DROP,
    ACTION_PENDING_MARKET_CONFIRM,
    ACTION_RISK_OFF,
    ACTION_VOTE_BEARISH,
    ACTION_VOTE_BULLISH,
    MarketSnapshot,
    RouteBatchOutcome,
    RoutedSignal,
    SignalRouter,
    check_bearish_market_confirm,
    check_bullish_market_confirm,
    ema_decay_for,
    load_market_snapshot,
    write_risk_off_signal,
)


@dataclass
class PipelineRunReport:
    """Complete audit + routing result for one pass of the 4-layer funnel."""

    input_count: int
    l1_kept_count: int
    l1_dropped_count: int
    l1_drop_reasons: dict[str, int]
    l2_unique_count: int
    l2_duplicate_count: int
    l2_tier_upgraded_count: int
    l3_attempted: int
    l3_succeeded: int
    l3_schema_failures: int
    api_error: str | None
    classified_items: list[tuple[DedupDecision, ClassifiedEvent]]
    routing: RouteBatchOutcome
    elapsed_ms: float
    embedder_used: str = ""
    embedder_fallback_reason: str = ""
    l1_dropped_samples: list[dict[str, str]] = field(default_factory=list)
    l2_duplicate_samples: list[dict[str, Any]] = field(default_factory=list)
    l3_failure_reasons: list[str] = field(default_factory=list)
    attempted_guids: list[str] = field(default_factory=list)
    safe_seen_guids: list[str] = field(default_factory=list)
    bearish_vote_threshold: float = 0.85
    bullish_vote_threshold: float = 1.0
    vote_withdraw_threshold: float = 0.45

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.routing.routed_signals if r.passed)

    @property
    def llm_calls_saved(self) -> int:
        return max(0, self.input_count - self.l3_attempted)

    def stats_dict(self) -> dict[str, Any]:
        return {
            "input": self.input_count,
            "l1_kept": self.l1_kept_count,
            "l1_dropped": self.l1_dropped_count,
            "l1_drop_reasons": dict(self.l1_drop_reasons),
            "l2_unique": self.l2_unique_count,
            "l2_duplicates": self.l2_duplicate_count,
            "l2_tier_upgraded": self.l2_tier_upgraded_count,
            "l3_attempted": self.l3_attempted,
            "l3_succeeded": self.l3_succeeded,
            "l3_schema_failures": self.l3_schema_failures,
            "l4_passed": self.passed_count,
            "llm_calls_saved": self.llm_calls_saved,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "embedder_used": self.embedder_used,
            "embedder_fallback_reason": self.embedder_fallback_reason,
            "run_net": self.routing.run_net,
            "session_net": self.routing.session_net,
            "vote_direction": self.routing.vote_direction or "",
            "bearish_vote_threshold": self.bearish_vote_threshold,
            "bullish_vote_threshold": self.bullish_vote_threshold,
            "vote_withdraw_threshold": self.vote_withdraw_threshold,
        }


class NewsSignalPipeline:
    """Stateful 4-layer news signal orchestrator (persists Layer-2 cluster window)."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        api_key: str = "",
        embedder: Embedder | None = None,
        embedder_provider_override: str = "",
        classifier: GeminiEventClassifier | None = None,
    ):
        self.config = config
        self.prefilter = NewsPreFilter(config)
        resolved_embedder = (
            embedder
            if embedder is not None
            else build_embedder(
                config.dedup,
                api_key=api_key,
                provider_override=embedder_provider_override,
            )
        )
        self.clusterer = IncrementalEventClusterer(config, embedder=resolved_embedder)
        self.classifier = classifier or GeminiEventClassifier(config, api_key=api_key)
        self.router = SignalRouter(config)

    def load_state(self, state_dict: Any) -> None:
        self.clusterer.load_dict(state_dict)

    def dump_state(self) -> dict[str, Any]:
        return self.clusterer.to_dict()

    def run(
        self,
        articles: list[dict],
        *,
        now: datetime,
        prev_session_net: float = 0.0,
        active_vote_direction: str = "",
        market: MarketSnapshot | None = None,
        emit_risk_off_enabled: bool = False,
        interval_minutes: float = 30.0,
    ) -> PipelineRunReport:
        """Execute Layers 1 -> 2 -> 3 -> 4 over *articles*."""
        t0 = time.perf_counter()

        # Layer 1: Hard-rule pre-filter
        l1_kept, l1_dropped, l1_drop_reasons = self.prefilter.filter_batch(articles)
        l1_dropped_samples = [
            {
                "title": str(art.get("title") or "")[:120],
                "source": str(art.get("source") or ""),
                "reason": pf.drop_reason,
            }
            for art, pf in l1_dropped[:20]
        ]

        # Layer 2: 3-stage incremental sliding-window deduplication
        dedup_decisions = self.clusterer.process_batch(l1_kept, now=now)
        l2_dups = [d for d in dedup_decisions if d.is_duplicate]
        l2_tier_upgraded = [d for d in dedup_decisions if d.tier_upgraded]
        to_classify = [d for d in dedup_decisions if d.needs_llm]
        l2_dup_samples = [
            {
                "title": d.prefilter.cleaned_title[:120],
                "cluster_id": d.cluster_id,
                "method": d.dup_method,
                "similarity": d.similarity,
                "mention_count": d.mention_count,
            }
            for d in l2_dups[:20]
        ]

        # Layer 3: Strict JSON classification (representatives only)
        classified_items: list[tuple[DedupDecision, ClassifiedEvent]] = []
        l3_attempted = 0
        l3_schema_failures = 0
        l3_failure_reasons: list[str] = []
        attempted_guids: list[str] = []
        unreached_cluster_ids: set[str] = set()
        api_error: str | None = None
        market_text = market.summary_text if market is not None else ""

        for idx, dec in enumerate(to_classify):
            l3_attempted += 1
            try:
                ev, err = self.classifier.classify(dec, market_context_text=market_text)
            except ClassifierAPIError as exc:
                api_error = str(exc)
                unreached_cluster_ids = {d.cluster_id for d in to_classify[idx:]}
                break
            guid = str(dec.article.get("guid") or "")
            if guid:
                attempted_guids.append(guid)
            if ev is None:
                l3_schema_failures += 1
                if len(l3_failure_reasons) < 10:
                    l3_failure_reasons.append(f"{dec.cluster_id}: {err}")
                continue
            classified_items.append((dec, ev))

        if unreached_cluster_ids:
            # Drop newly created unclassified clusters so retried articles are not
            # mis-flagged as duplicates on the next pass after API recovery.
            self.clusterer.active = [
                c for c in self.clusterer.active
                if not (c.cluster_id in unreached_cluster_ids and not c.classified_category)
            ]

        # Layer 4: Signal routing + asymmetric thresholds + market cross-validation
        events = [ev for _, ev in classified_items]
        routing = self.router.route_batch(
            events,
            prev_session_net=prev_session_net,
            active_vote_direction=active_vote_direction,
            market=market,
            emit_risk_off_enabled=emit_risk_off_enabled,
            interval_minutes=interval_minutes,
        )

        # Persist classification summary + routed latch back onto active clusters
        for (dec, ev), rsig in zip(classified_items, routing.routed_signals):
            self.clusterer.record_classification(
                dec.cluster_id,
                summary=ev.summary_one_line,
                category=ev.category,
                direction=ev.direction,
                materiality=ev.materiality,
                routed=rsig.passed,
            )

        # GUIDs safe to mark seen: Layer 1 drops + Layer 2 non-LLM dups (excluding
        # any cluster whose representative was aborted by an API error) + responded items.
        safe_seen_guids: list[str] = []
        for art, _ in l1_dropped:
            g = str(art.get("guid") or "")
            if g:
                safe_seen_guids.append(g)
        for d in l2_dups:
            if not d.needs_llm and d.cluster_id not in unreached_cluster_ids:
                g = str(d.article.get("guid") or "")
                if g:
                    safe_seen_guids.append(g)
        safe_seen_guids.extend(attempted_guids)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        emb = self.clusterer.embedder
        emb_used = getattr(emb, "last_used_provider", emb.name) if emb is not None else "none"
        emb_fb = getattr(emb, "last_fallback_reason", "") if emb is not None else ""

        return PipelineRunReport(
            input_count=len(articles),
            l1_kept_count=len(l1_kept),
            l1_dropped_count=len(l1_dropped),
            l1_drop_reasons=l1_drop_reasons,
            l2_unique_count=len(to_classify),
            l2_duplicate_count=len(l2_dups),
            l2_tier_upgraded_count=len(l2_tier_upgraded),
            l3_attempted=l3_attempted,
            l3_succeeded=len(classified_items),
            l3_schema_failures=l3_schema_failures,
            api_error=api_error,
            classified_items=classified_items,
            routing=routing,
            elapsed_ms=elapsed_ms,
            embedder_used=emb_used,
            embedder_fallback_reason=emb_fb,
            l1_dropped_samples=l1_dropped_samples,
            l2_duplicate_samples=l2_dup_samples,
            l3_failure_reasons=l3_failure_reasons,
            attempted_guids=attempted_guids,
            safe_seen_guids=safe_seen_guids,
            bearish_vote_threshold=self.config.router.bearish_vote_threshold,
            bullish_vote_threshold=self.config.router.bullish_vote_threshold,
            vote_withdraw_threshold=self.config.router.vote_withdraw_threshold,
        )


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "PipelineConfig",
    "PreFilterConfig",
    "SourceTierConfig",
    "DedupConfig",
    "ClassifierConfig",
    "RouterConfig",
    "load_pipeline_config",
    "PreFilterResult",
    "NewsPreFilter",
    "clean_title",
    "normalize_url",
    "resolve_source_tier",
    "Embedder",
    "GeminiEmbedder",
    "LexicalFallbackEmbedder",
    "build_embedder",
    "EventCluster",
    "PriorEventContext",
    "DedupDecision",
    "IncrementalEventClusterer",
    "normalized_title_key",
    "simhash64",
    "hamming_distance",
    "cosine_similarity",
    "ClassifiedEvent",
    "ClassifierAPIError",
    "GeminiEventClassifier",
    "build_classification_prompt",
    "parse_classified_event",
    "ACTION_DROP",
    "ACTION_ADVISORY_LOW_TIER",
    "ACTION_PENDING_MARKET_CONFIRM",
    "ACTION_RISK_OFF",
    "ACTION_VOTE_BEARISH",
    "ACTION_VOTE_BULLISH",
    "MarketSnapshot",
    "RoutedSignal",
    "RouteBatchOutcome",
    "SignalRouter",
    "load_market_snapshot",
    "check_bullish_market_confirm",
    "check_bearish_market_confirm",
    "ema_decay_for",
    "write_risk_off_signal",
    "PipelineRunReport",
    "NewsSignalPipeline",
]
