"""Layer 1 — Hard-rule + semantic pre-filter (NewsPreFilter).

Runs BEFORE any embedding or LLM call to eliminate:
- Blocked social/forum sources (e.g. Facebook, CMoney 股市爆料同學會)
- Zero-alpha automated spam templates (Investing.com transcripts/Form-4/slides,
  FactSet target-price spam, TAIFEX routine admin notices, gossip/clickbait)
- Pure price-recap headlines ("台股收跌 2,393 點", "U.S. stocks lower at close")
  unless rescued by an explicit causal catalyst keyword
- Off-topic non-heavyweight small-cap noise

Also performs URL canonicalization, iterative leading/trailing media prefix &
suffix stripping, and source-tier resolution. All rules come from
``config/news_filter.yaml``.
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .config import PipelineConfig, PreFilterConfig, SourceTierConfig

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_MAX_STRIP_PASSES = 6


@dataclass(frozen=True)
class PreFilterResult:
    """Audit-ready outcome of running Layer 1 on a single article."""

    passed: bool
    drop_reason: str
    cleaned_title: str
    cleaned_summary: str
    canonical_url: str
    source_tier: int
    matched_rule: str = ""


def strip_html(text: str) -> str:
    """Unescape HTML entities, drop tags, NFKC-normalize, collapse whitespace."""
    if not text:
        return ""
    plain = html.unescape(_HTML_TAG_RE.sub(" ", str(text)))
    plain = unicodedata.normalize("NFKC", plain)
    return _WS_RE.sub(" ", plain).strip()


def normalize_url(url: str, tracking_params: frozenset[str] = frozenset()) -> str:
    """Canonicalize an article URL for cross-feed exact deduplication.

    Lowercases scheme and host, removes ``www.``, strips tracking query
    parameters, sorts remaining query parameters, removes fragments, and
    strips trailing slashes on non-root paths. Returns ``""`` for non-http(s)
    or malformed URLs.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.netloc:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parsed.port
    netloc = f"{host}:{port}" if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)) else host
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    kept_query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in tracking_params
    ]
    kept_query.sort()
    query = urlencode(kept_query)
    return urlunparse((scheme, netloc, path, "", query, ""))


def clean_title(
    title: str,
    prefixes: tuple[re.Pattern[str], ...],
    suffixes: tuple[re.Pattern[str], ...],
    *,
    min_chars: int = 4,
) -> str:
    """Strip media prefixes/suffixes iteratively without destroying short titles."""
    text = strip_html(title)
    if not text:
        return ""

    for _ in range(_MAX_STRIP_PASSES):
        changed = False
        for rx in prefixes:
            cand = rx.sub("", text, count=1).strip()
            if cand != text and len(cand) >= min_chars:
                text = cand
                changed = True
        for rx in suffixes:
            cand = rx.sub("", text, count=1).strip()
            if cand != text and len(cand) >= min_chars:
                text = cand
                changed = True
        if not changed:
            break

    return text.rstrip(".… ").strip()


def _match_tier(text: str, tiers_cfg: SourceTierConfig) -> int | None:
    if not text:
        return None
    for rx in tiers_cfg.tier_1:
        if rx.search(text):
            return 1
    for rx in tiers_cfg.tier_2:
        if rx.search(text):
            return 2
    for rx in tiers_cfg.tier_3:
        if rx.search(text):
            return 3
    return None


def resolve_source_tier(source: str, url: str, tiers_cfg: SourceTierConfig) -> int:
    """Map an article's publisher name (preferred) or URL host to tier (1, 2, or 3)."""
    src = str(source or "").strip()
    matched = _match_tier(src, tiers_cfg)
    if matched is not None:
        return matched
    host = ""
    if url:
        try:
            host = (urlparse(str(url)).hostname or "").lower()
        except ValueError:
            host = ""
    matched = _match_tier(host, tiers_cfg)
    return matched if matched is not None else tiers_cfg.default_tier


class NewsPreFilter:
    """Stateless Layer 1 hard-rule filter backed by ``PipelineConfig``."""

    def __init__(self, config: PipelineConfig):
        self._cfg: PipelineConfig = config
        self._pf: PreFilterConfig = config.prefilter
        self._tiers: SourceTierConfig = config.source_tiers

    def filter_article(self, article: dict) -> PreFilterResult:
        raw_title = strip_html(str(article.get("title") or ""))
        cleaned_summary = strip_html(str(article.get("summary") or ""))
        if cleaned_summary == raw_title:
            cleaned_summary = ""
        raw_url = str(article.get("link") or "")
        source = str(article.get("source") or "")

        canonical_url = normalize_url(raw_url, self._pf.tracking_query_params)
        cleaned = clean_title(
            raw_title,
            self._pf.media_prefixes,
            self._pf.media_suffixes,
            min_chars=self._pf.min_cleaned_title_chars,
        )
        tier = resolve_source_tier(source, raw_url, self._tiers)

        if len(cleaned) < self._pf.min_cleaned_title_chars:
            return PreFilterResult(
                passed=False,
                drop_reason="empty_title",
                cleaned_title=cleaned,
                cleaned_summary=cleaned_summary,
                canonical_url=canonical_url,
                source_tier=tier,
                matched_rule="min_cleaned_title_chars",
            )

        # 1. Blocked sources (forums / social media)
        for rx in self._pf.blocked_sources:
            if rx.search(source) or rx.search(raw_title):
                return PreFilterResult(
                    passed=False,
                    drop_reason="blocked_source",
                    cleaned_title=cleaned,
                    cleaned_summary=cleaned_summary,
                    canonical_url=canonical_url,
                    source_tier=tier,
                    matched_rule=rx.pattern,
                )

        # 2. Named zero-alpha spam/admin templates
        for rule_name, rx in self._pf.noise_blacklist:
            if rx.search(raw_title) or rx.search(cleaned):
                return PreFilterResult(
                    passed=False,
                    drop_reason=f"noise:{rule_name}",
                    cleaned_title=cleaned,
                    cleaned_summary=cleaned_summary,
                    canonical_url=canonical_url,
                    source_tier=tier,
                    matched_rule=rule_name,
                )

        # 3. Pure price-recap blacklist (with strict causal catalyst rescue)
        recap_hit = None
        for rx in self._pf.price_recap_blacklist:
            if rx.search(raw_title) or rx.search(cleaned):
                recap_hit = rx.pattern
                break
        if recap_hit is not None:
            protected = any(
                rx.search(raw_title) or rx.search(cleaned)
                for rx in self._pf.causal_protection_whitelist
            )
            if not protected:
                return PreFilterResult(
                    passed=False,
                    drop_reason="price_recap_only",
                    cleaned_title=cleaned,
                    cleaned_summary=cleaned_summary,
                    canonical_url=canonical_url,
                    source_tier=tier,
                    matched_rule=recap_hit,
                )

        # 4. Core domain / entity relevance gate (drops unrelated small-caps)
        if self._pf.require_core_relevance and self._pf.core_relevance_whitelist:
            relevant = any(
                rx.search(cleaned) or rx.search(raw_title)
                for rx in self._pf.core_relevance_whitelist
            )
            if not relevant:
                return PreFilterResult(
                    passed=False,
                    drop_reason="off_topic",
                    cleaned_title=cleaned,
                    cleaned_summary=cleaned_summary,
                    canonical_url=canonical_url,
                    source_tier=tier,
                    matched_rule="core_relevance_whitelist",
                )

        return PreFilterResult(
            passed=True,
            drop_reason="",
            cleaned_title=cleaned,
            cleaned_summary=cleaned_summary,
            canonical_url=canonical_url,
            source_tier=tier,
            matched_rule="",
        )

    def filter_batch(
        self, articles: list[dict]
    ) -> tuple[list[tuple[dict, PreFilterResult]], list[tuple[dict, PreFilterResult]], dict[str, int]]:
        """Filter a batch of articles.

        Returns ``(kept, dropped, drop_counts)`` where ``kept`` and ``dropped``
        are lists of ``(article, PreFilterResult)`` pairs.
        """
        kept: list[tuple[dict, PreFilterResult]] = []
        dropped: list[tuple[dict, PreFilterResult]] = []
        drop_counts: dict[str, int] = {}
        for art in articles:
            res = self.filter_article(art)
            if res.passed:
                kept.append((art, res))
            else:
                dropped.append((art, res))
                drop_counts[res.drop_reason] = drop_counts.get(res.drop_reason, 0) + 1
        return kept, dropped, drop_counts
