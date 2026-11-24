"""Configuration loader for the 4-layer news signal pipeline.

All thresholds, regex patterns, whitelists, and source weights are loaded
from ``config/news_filter.yaml`` (or a caller-supplied YAML override) so
business logic never hardcodes rules or thresholds.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "news_filter.yaml"


def _compile_list(patterns: list[str] | None) -> tuple[re.Pattern[str], ...]:
    out: list[re.Pattern[str]] = []
    for pat in patterns or []:
        if isinstance(pat, str) and pat.strip():
            out.append(re.compile(pat, re.IGNORECASE))
    return tuple(out)


def _compile_map(mapping: dict[str, str] | None) -> tuple[tuple[str, re.Pattern[str]], ...]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for name, pat in (mapping or {}).items():
        if isinstance(name, str) and isinstance(pat, str) and pat.strip():
            out.append((name, re.compile(pat, re.IGNORECASE)))
    return tuple(out)


@dataclass(frozen=True)
class PreFilterConfig:
    require_core_relevance: bool = True
    min_cleaned_title_chars: int = 4
    tracking_query_params: frozenset[str] = field(default_factory=frozenset)
    media_prefixes: tuple[re.Pattern[str], ...] = ()
    media_suffixes: tuple[re.Pattern[str], ...] = ()
    blocked_sources: tuple[re.Pattern[str], ...] = ()
    noise_blacklist: tuple[tuple[str, re.Pattern[str]], ...] = ()
    price_recap_blacklist: tuple[re.Pattern[str], ...] = ()
    causal_protection_whitelist: tuple[re.Pattern[str], ...] = ()
    core_relevance_whitelist: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class SourceTierConfig:
    default_tier: int = 3
    tier_weights: dict[int, float] = field(default_factory=lambda: {1: 1.0, 2: 0.85, 3: 0.35})
    tier_1: tuple[re.Pattern[str], ...] = ()
    tier_2: tuple[re.Pattern[str], ...] = ()
    tier_3: tuple[re.Pattern[str], ...] = ()

    def weight_for(self, tier: int) -> float:
        return float(self.tier_weights.get(int(tier), self.tier_weights.get(self.default_tier, 0.35)))


@dataclass(frozen=True)
class DedupConfig:
    simhash_bits: int = 64
    simhash_max_hamming: int = 6
    cluster_window_hours: float = 4.0
    cosine_threshold: float = 0.82
    lexical_cosine_threshold: float = 0.58
    followup_window_hours: float = 48.0
    followup_cosine_threshold: float = 0.72
    lexical_followup_cosine_threshold: float = 0.45
    max_active_clusters: int = 400
    max_archived_clusters: int = 600
    embedding_provider: str = "gemini"
    embedding_model: str = "gemini-embedding-001"
    embedding_output_dim: int = 256
    embedding_batch_size: int = 64
    embedding_timeout_s: int = 20


@dataclass(frozen=True)
class ClassifierConfig:
    model: str = "gemini-3.6-flash"
    max_output_tokens: int = 8192
    timeout_s: int = 30
    retry_attempts: int = 3
    retry_base_delay_s: float = 1.0
    summary_max_chars: int = 500
    min_materiality_pass: int = 4
    allowed_categories: tuple[str, ...] = (
        "MACRO_RATES",
        "AI_SUPPLY_CHAIN",
        "GOVERNANCE_REGULATORY",
        "CREDIT_FLOWS",
        "DROP",
    )
    allowed_directions: tuple[str, ...] = ("BULLISH", "BEARISH", "NEUTRAL")
    allowed_horizons: tuple[str, ...] = ("INTRADAY", "MULTI_DAY", "STRUCTURAL")


@dataclass(frozen=True)
class RouterConfig:
    first_sighting_weight: float = 0.5
    full_weight_outlets: int = 4
    bullish_confirm_symbols: tuple[str, ...] = ("TSM", "SOXX", "QQQ", "NQ=F", "^TWII")
    market_snapshot_max_age_s: int = 1800
    tier3_can_trigger_vote: bool = False
    bearish_vote_threshold: float = 0.85
    bullish_vote_threshold: float = 0.85
    vote_withdraw_threshold: float = 0.45
    ema_half_life_hours: float = 10.0
    emit_risk_off_min_materiality: int = 4
    emit_risk_off_max_tier: int = 2
    emit_risk_off_requires_market_confirm: bool = True
    bearish_confirm_max_pct: float = 0.5
    discord_alert_min_materiality: int = 4


@dataclass(frozen=True)
class PipelineConfig:
    version: int
    source_path: str
    prefilter: PreFilterConfig
    source_tiers: SourceTierConfig
    dedup: DedupConfig
    classifier: ClassifierConfig
    router: RouterConfig


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def _build_config(raw: dict[str, Any], source_path: str) -> PipelineConfig:
    pf = raw.get("prefilter") or {}
    st = raw.get("source_tiers") or {}
    dd = raw.get("dedup") or {}
    cl = raw.get("classifier") or {}
    rt = raw.get("router") or {}

    raw_weights = st.get("tier_weights") or {1: 1.0, 2: 0.85, 3: 0.35}
    tier_weights = {int(k): float(v) for k, v in raw_weights.items()}

    return PipelineConfig(
        version=int(raw.get("version", 1)),
        source_path=source_path,
        prefilter=PreFilterConfig(
            require_core_relevance=bool(pf.get("require_core_relevance", True)),
            min_cleaned_title_chars=int(pf.get("min_cleaned_title_chars", 4)),
            tracking_query_params=frozenset(
                str(p).strip().lower() for p in (pf.get("tracking_query_params") or []) if p
            ),
            media_prefixes=_compile_list(pf.get("media_prefixes")),
            media_suffixes=_compile_list(pf.get("media_suffixes")),
            blocked_sources=_compile_list(pf.get("blocked_sources")),
            noise_blacklist=_compile_map(pf.get("noise_blacklist")),
            price_recap_blacklist=_compile_list(pf.get("price_recap_blacklist")),
            causal_protection_whitelist=_compile_list(pf.get("causal_protection_whitelist")),
            core_relevance_whitelist=_compile_list(pf.get("core_relevance_whitelist")),
        ),
        source_tiers=SourceTierConfig(
            default_tier=int(st.get("default_tier", 3)),
            tier_weights=tier_weights,
            tier_1=_compile_list(st.get("tier_1")),
            tier_2=_compile_list(st.get("tier_2")),
            tier_3=_compile_list(st.get("tier_3")),
        ),
        dedup=DedupConfig(
            simhash_bits=int(dd.get("simhash_bits", 64)),
            simhash_max_hamming=int(dd.get("simhash_max_hamming", 6)),
            cluster_window_hours=float(dd.get("cluster_window_hours", 4.0)),
            cosine_threshold=float(dd.get("cosine_threshold", 0.82)),
            lexical_cosine_threshold=float(dd.get("lexical_cosine_threshold", 0.58)),
            followup_window_hours=float(dd.get("followup_window_hours", 48.0)),
            followup_cosine_threshold=float(dd.get("followup_cosine_threshold", 0.72)),
            lexical_followup_cosine_threshold=float(dd.get("lexical_followup_cosine_threshold", 0.45)),
            max_active_clusters=int(dd.get("max_active_clusters", 400)),
            max_archived_clusters=int(dd.get("max_archived_clusters", 600)),
            embedding_provider=str(dd.get("embedding_provider", "gemini")).strip().lower(),
            embedding_model=str(dd.get("embedding_model", "gemini-embedding-001")).strip(),
            embedding_output_dim=int(dd.get("embedding_output_dim", 256)),
            embedding_batch_size=int(dd.get("embedding_batch_size", 64)),
            embedding_timeout_s=int(dd.get("embedding_timeout_s", 20)),
        ),
        classifier=ClassifierConfig(
            model=str(cl.get("model", "gemini-3.6-flash")).strip(),
            max_output_tokens=int(cl.get("max_output_tokens", 8192)),
            timeout_s=int(cl.get("timeout_s", 30)),
            retry_attempts=max(1, int(cl.get("retry_attempts", 3))),
            retry_base_delay_s=float(cl.get("retry_base_delay_s", 1.0)),
            summary_max_chars=int(cl.get("summary_max_chars", 500)),
            min_materiality_pass=int(cl.get("min_materiality_pass", 4)),
            allowed_categories=tuple(str(x).upper() for x in (cl.get("allowed_categories") or (
                "MACRO_RATES", "AI_SUPPLY_CHAIN", "GOVERNANCE_REGULATORY", "CREDIT_FLOWS", "DROP"
            ))),
            allowed_directions=tuple(str(x).upper() for x in (cl.get("allowed_directions") or (
                "BULLISH", "BEARISH", "NEUTRAL"
            ))),
            allowed_horizons=tuple(str(x).upper() for x in (cl.get("allowed_horizons") or (
                "INTRADAY", "MULTI_DAY", "STRUCTURAL"
            ))),
        ),
        router=RouterConfig(
            first_sighting_weight=float(rt.get("first_sighting_weight", 0.5)),
            full_weight_outlets=int(rt.get("full_weight_outlets", 4)),
            bullish_confirm_symbols=tuple(str(s) for s in (rt.get("bullish_confirm_symbols") or (
                "TSM", "SOXX", "QQQ", "NQ=F", "^TWII"
            ))),
            market_snapshot_max_age_s=int(rt.get("market_snapshot_max_age_s", 1800)),
            tier3_can_trigger_vote=bool(rt.get("tier3_can_trigger_vote", False)),
            bearish_vote_threshold=float(rt.get("bearish_vote_threshold", 0.85)),
            bullish_vote_threshold=float(rt.get("bullish_vote_threshold", 0.85)),
            vote_withdraw_threshold=float(rt.get("vote_withdraw_threshold", 0.45)),
            ema_half_life_hours=float(rt.get("ema_half_life_hours", 10.0)),
            emit_risk_off_min_materiality=int(rt.get("emit_risk_off_min_materiality", 4)),
            emit_risk_off_max_tier=int(rt.get("emit_risk_off_max_tier", 2)),
            emit_risk_off_requires_market_confirm=bool(rt.get("emit_risk_off_requires_market_confirm", True)),
            bearish_confirm_max_pct=float(rt.get("bearish_confirm_max_pct", 0.5)),
            discord_alert_min_materiality=int(rt.get("discord_alert_min_materiality", 4)),
        ),
    )


def load_pipeline_config(
    path: str | os.PathLike | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> PipelineConfig:
    """Load pipeline config from YAML, optionally applying dict overrides.

    When *path* is empty/None, loads ``config/news_filter.yaml`` from the repo root.
    """
    import yaml  # local import to keep module import cheap

    target = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(target, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Expected YAML mapping in {target}, got {type(raw).__name__}")
    if overrides:
        raw = _deep_merge(raw, overrides)
    return _build_config(raw, str(target))
