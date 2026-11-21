"""Layer 2 — 3-stage real-time incremental sliding-window deduplication.

Stages:
1. Pre-dedup fingerprints: canonical URL match, normalized cleaned title key,
   and 64-bit SimHash (Hamming distance <= ``simhash_max_hamming``) for exact
   and near-verbatim reprints.
2. Sliding-window semantic centroid clustering (1-4h active window): embeds
   ``cleaned_title + summary`` via a pluggable :class:`Embedder` (default:
   Gemini ``batchEmbedContents`` REST API with automatic fallback to pure-Python
   signed-hash lexical embeddings), and computes O(k) cosine similarity ONLY
   against active cluster centroids — never re-running global DBSCAN.
3. Single representative + cross-day drift protection (24-48h archive): only
   the first or highest-authority article triggers Layer 3 LLM classification;
   duplicates increment ``mention_count``. New clusters similar to a 4-48h
   archived event carry ``prior_event_context`` into Layer 3 so the classifier
   evaluates ``has_new_fact_delta``.

All state serializes cleanly to JSON for persistence across short-lived
``--once`` scheduled runs.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import DedupConfig, PipelineConfig
from .prefilter import PreFilterResult

_TZ_TAIPEI = timezone(timedelta(hours=8))
_GEMINI_BATCH_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:batchEmbedContents?key={key}"
)
_UA = {"User-Agent": "Mozilla/5.0 (tai-robot news pipeline)", "Connection": "close"}
_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})

_MAX_CLUSTER_URLS = 16
_MAX_CLUSTER_KEYS = 16
_MAX_CLUSTER_SIMHASHES = 12


# ── Pure-Python fingerprinting & vector math ─────────────────────────────

def normalized_title_key(cleaned_title: str) -> str:
    """Alphanumeric casefolded key of a cleaned headline ('' if too short)."""
    text = unicodedata.normalize("NFKC", str(cleaned_title or "")).casefold()
    key = "".join(ch for ch in text if ch.isalnum())
    return key[:160] if len(key) >= 6 else ""


def extract_shingles(text: str) -> list[str]:
    """Extract mixed EN word unigrams/bigrams and CJK character bigrams."""
    norm = unicodedata.normalize("NFKC", str(text or "")).casefold()
    shingles: list[str] = []

    words = [w for w in _WORD_RE.findall(norm) if len(w) >= 2]
    shingles.extend(words)
    for i in range(len(words) - 1):
        shingles.append(f"{words[i]}_{words[i + 1]}")

    cjk_chars = _CJK_RE.findall(norm)
    if len(cjk_chars) == 1:
        shingles.append(cjk_chars[0])
    else:
        for i in range(len(cjk_chars) - 1):
            shingles.append(cjk_chars[i] + cjk_chars[i + 1])
        for i in range(len(cjk_chars) - 2):
            shingles.append("3:" + cjk_chars[i] + cjk_chars[i + 1] + cjk_chars[i + 2])
    return shingles


def simhash64(text: str) -> int:
    """Deterministic 64-bit SimHash over mixed EN/CJK shingles."""
    shingles = extract_shingles(text)
    if not shingles:
        return 0
    weights = [0] * 64
    for tok in shingles:
        digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        h = int.from_bytes(digest, "big")
        for bit in range(64):
            if (h >> bit) & 1:
                weights[bit] += 1
            else:
                weights[bit] -= 1
    out = 0
    for bit, w in enumerate(weights):
        if w > 0:
            out |= 1 << bit
    return out


def hamming_distance(a: int, b: int) -> int:
    return (int(a) ^ int(b)).bit_count()


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0.0:
        return [0.0] * len(vec)
    return [x / norm for x in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def update_centroid(centroid: list[float], count: int, new_vec: list[float]) -> list[float]:
    """Incremental O(d) centroid update + L2 normalization."""
    if not centroid:
        return l2_normalize(new_vec)
    if not new_vec or len(centroid) != len(new_vec):
        return centroid
    n = max(1, int(count))
    blended = [(c * n + v) / (n + 1) for c, v in zip(centroid, new_vec)]
    return l2_normalize(blended)


def _round_vec(vec: list[float], digits: int = 4) -> list[float]:
    return [round(float(x), digits) for x in vec]


# ── Pluggable Embedder interface ─────────────────────────────────────────

class Embedder(ABC):
    """Pluggable text embedder for Layer 2 semantic clustering."""

    name: str = "base"
    is_semantic: bool = False

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return one L2-normalized embedding vector per input string."""


class LexicalFallbackEmbedder(Embedder):
    """Pure-Python signed feature-hashing embedder (zero external dependencies).

    Captures EN word unigrams/bigrams and CJK bigrams/trigrams into a fixed
    compact dense vector. Used for offline testing, replay without API calls,
    and automatic fallback if Gemini Embedding API fails.
    """

    name = "lexical"
    is_semantic = False

    def __init__(self, dim: int = 256):
        self.dim = max(32, int(dim))

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in extract_shingles(text):
                digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
                h = int.from_bytes(digest, "big")
                idx = h % self.dim
                sign = 1.0 if ((h >> 63) & 1) == 0 else -1.0
                # Upweight multi-character / bigram tokens slightly over single chars
                weight = 1.5 if ("_" in tok or tok.startswith("3:")) else 1.0
                vec[idx] += sign * weight
            out.append(l2_normalize(vec))
        return out


class GeminiEmbedder(Embedder):
    """Multilingual Gemini ``batchEmbedContents`` REST embedder (stdlib-only).

    Falls back automatically to :class:`LexicalFallbackEmbedder` if the API key
    is empty or the HTTP request fails, so Layer 2 never blocks a scoring pass.
    """

    name = "gemini"
    is_semantic = True

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gemini-embedding-001",
        output_dim: int = 256,
        batch_size: int = 64,
        timeout_s: int = 20,
        fallback: Embedder | None = None,
    ):
        self.api_key = str(api_key or "").strip()
        self.model = model
        self.output_dim = int(output_dim)
        self.batch_size = max(1, int(batch_size))
        self.timeout_s = int(timeout_s)
        self.fallback = fallback or LexicalFallbackEmbedder(dim=self.output_dim)
        self.last_used_provider: str = self.name
        self.last_fallback_reason: str = ""

    def _call_batch(self, batch: list[str]) -> list[list[float]]:
        url = _GEMINI_BATCH_EMBED_URL.format(model=self.model, key=self.api_key)
        model_ref = self.model if self.model.startswith("models/") else f"models/{self.model}"
        payload = {
            "requests": [
                {
                    "model": model_ref,
                    "content": {"parts": [{"text": t[:1500]}]},
                    "taskType": "CLUSTERING",
                    "outputDimensionality": self.output_dim,
                }
                for t in batch
            ]
        }
        body = json.dumps(payload).encode("utf-8")
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url, data=body, headers={**_UA, "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    data = json.load(resp)
                embeddings = data.get("embeddings") or []
                if len(embeddings) != len(batch):
                    raise ValueError(
                        f"expected {len(batch)} embeddings, got {len(embeddings)}"
                    )
                return [l2_normalize([float(x) for x in e["values"]]) for e in embeddings]
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code in _TRANSIENT_HTTP and attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise
            except (OSError, ValueError, KeyError, TypeError) as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise
        raise RuntimeError(f"Gemini embed failed: {last_exc}")

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key:
            self.last_used_provider = self.fallback.name
            self.last_fallback_reason = "missing_api_key"
            return self.fallback.embed_texts(texts)

        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start : start + self.batch_size]
                vectors.extend(self._call_batch(batch))
            self.last_used_provider = self.name
            self.last_fallback_reason = ""
            return vectors
        except Exception as exc:  # noqa: BLE001 — degrade safely to lexical fallback
            self.last_used_provider = self.fallback.name
            self.last_fallback_reason = f"{type(exc).__name__}: {exc}"[:160]
            return self.fallback.embed_texts(texts)


def build_embedder(
    dedup_cfg: DedupConfig,
    api_key: str = "",
    provider_override: str = "",
) -> Embedder | None:
    """Construct the configured Embedder (``None`` when provider is ``"none"``)."""
    provider = (provider_override or dedup_cfg.embedding_provider or "lexical").strip().lower()
    if provider == "none":
        return None
    if provider == "gemini":
        return GeminiEmbedder(
            api_key=api_key,
            model=dedup_cfg.embedding_model,
            output_dim=dedup_cfg.embedding_output_dim,
            batch_size=dedup_cfg.embedding_batch_size,
            timeout_s=dedup_cfg.embedding_timeout_s,
        )
    return LexicalFallbackEmbedder(dim=dedup_cfg.embedding_output_dim)


# ── Event Cluster & Incremental Sliding-Window Clusterer ─────────────────

@dataclass
class PriorEventContext:
    """24-48h archived cluster context passed to Layer 3 for delta detection."""

    prior_cluster_id: str
    prior_first_seen_at: str
    prior_title: str
    prior_summary: str
    prior_category: str = ""
    prior_direction: str = ""
    similarity: float = 0.0


@dataclass
class EventCluster:
    cluster_id: str
    first_seen_at: str
    last_seen_at: str
    representative_title: str
    representative_summary: str
    representative_source: str
    representative_url: str
    source_tier: int
    mention_count: int = 1
    centroid: list[float] = field(default_factory=list)
    embedder_name: str = ""
    simhashes: list[int] = field(default_factory=list)
    canonical_urls: list[str] = field(default_factory=list)
    norm_keys: list[str] = field(default_factory=list)
    classified_summary: str = ""
    classified_category: str = ""
    classified_direction: str = ""
    classified_materiality: int = 0
    routed: bool = False

    def to_dict(self, *, archived: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cluster_id": self.cluster_id,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "representative_title": self.representative_title,
            "source_tier": int(self.source_tier),
            "mention_count": int(self.mention_count),
            "centroid": _round_vec(self.centroid, 3),
            "embedder_name": self.embedder_name,
            "simhashes": [int(x) for x in self.simhashes[-4 if archived else -_MAX_CLUSTER_SIMHASHES :]],
            "classified_summary": self.classified_summary[:240],
            "classified_category": self.classified_category,
            "classified_direction": self.classified_direction,
            "classified_materiality": int(self.classified_materiality),
            "routed": bool(self.routed),
        }
        if not archived:
            payload["representative_summary"] = self.representative_summary[:300]
            payload["representative_source"] = self.representative_source
            payload["representative_url"] = self.representative_url
            payload["canonical_urls"] = list(self.canonical_urls[-_MAX_CLUSTER_URLS:])
            payload["norm_keys"] = list(self.norm_keys[-_MAX_CLUSTER_KEYS:])
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EventCluster | None":
        if not isinstance(raw, dict) or not raw.get("cluster_id"):
            return None
        return cls(
            cluster_id=str(raw["cluster_id"]),
            first_seen_at=str(raw.get("first_seen_at") or ""),
            last_seen_at=str(raw.get("last_seen_at") or raw.get("first_seen_at") or ""),
            representative_title=str(raw.get("representative_title") or ""),
            representative_summary=str(raw.get("representative_summary") or ""),
            representative_source=str(raw.get("representative_source") or ""),
            representative_url=str(raw.get("representative_url") or ""),
            source_tier=int(raw.get("source_tier", 3)),
            mention_count=max(1, int(raw.get("mention_count", 1))),
            centroid=[float(x) for x in (raw.get("centroid") or [])],
            embedder_name=str(raw.get("embedder_name") or ""),
            simhashes=[int(x) for x in (raw.get("simhashes") or [])],
            canonical_urls=[str(x) for x in (raw.get("canonical_urls") or [])],
            norm_keys=[str(x) for x in (raw.get("norm_keys") or [])],
            classified_summary=str(raw.get("classified_summary") or ""),
            classified_category=str(raw.get("classified_category") or ""),
            classified_direction=str(raw.get("classified_direction") or ""),
            classified_materiality=int(raw.get("classified_materiality", 0)),
            routed=bool(raw.get("routed", False)),
        )


@dataclass
class DedupDecision:
    """Layer 2 result for one Layer-1-passing article."""

    article: dict
    prefilter: PreFilterResult
    cluster_id: str
    is_duplicate: bool
    dup_method: str                   # "" | "url" | "title_key" | "simhash" | "semantic"
    similarity: float
    mention_count: int
    source_tier: int
    tier_upgraded: bool = False
    prior_event: PriorEventContext | None = None

    @property
    def needs_llm(self) -> bool:
        """Call Layer 3 for new event representatives or unrouted tier-upgraded clusters."""
        return (not self.is_duplicate) or self.tier_upgraded


def _parse_iso(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_TAIPEI)
    return dt


def _as_tpe(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(_TZ_TAIPEI)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_TZ_TAIPEI)
    return dt.astimezone(_TZ_TAIPEI)


def _article_time(article: dict, fallback_now: datetime) -> datetime:
    pub = article.get("published_dt")
    if isinstance(pub, datetime):
        return _as_tpe(pub)
    if isinstance(pub, str):
        parsed = _parse_iso(pub)
        if parsed is not None:
            return parsed.astimezone(_TZ_TAIPEI)
    pub_at = article.get("published_at")
    if isinstance(pub_at, str):
        parsed = _parse_iso(pub_at)
        if parsed is not None:
            return parsed.astimezone(_TZ_TAIPEI)
    return fallback_now


def _make_cluster_id(now: datetime, cleaned_title: str) -> str:
    slug = hashlib.blake2b(cleaned_title.encode("utf-8"), digest_size=4).hexdigest()
    return f"cluster_{now.strftime('%Y%m%d')}_{slug}"


class IncrementalEventClusterer:
    """O(k) sliding-window event clusterer with 24-48h cross-day archive."""

    def __init__(
        self,
        config: PipelineConfig,
        embedder: Embedder | None = None,
    ):
        self._cfg: DedupConfig = config.dedup
        self._tier3_can_vote: bool = config.router.tier3_can_trigger_vote
        self.embedder: Embedder | None = embedder
        self.active: list[EventCluster] = []
        self.archived: list[EventCluster] = []

    def _effective_embedder_name(self) -> str:
        if self.embedder is None:
            return "none"
        return getattr(self.embedder, "last_used_provider", self.embedder.name)

    def _is_semantic_active(self) -> bool:
        if self.embedder is None:
            return False
        used = getattr(self.embedder, "last_used_provider", self.embedder.name)
        return bool(self.embedder.is_semantic and used == self.embedder.name)

    def _active_cosine_threshold(self) -> float:
        return (
            self._cfg.cosine_threshold
            if self._is_semantic_active()
            else self._cfg.lexical_cosine_threshold
        )

    def _followup_cosine_threshold(self) -> float:
        return (
            self._cfg.followup_cosine_threshold
            if self._is_semantic_active()
            else self._cfg.lexical_followup_cosine_threshold
        )

    def expire_windows(self, now: datetime) -> None:
        """Move clusters past ``cluster_window_hours`` to archive; drop past ``followup_window_hours``."""
        now_tpe = _as_tpe(now)
        active_cutoff = now_tpe - timedelta(hours=self._cfg.cluster_window_hours)
        archive_cutoff = now_tpe - timedelta(hours=self._cfg.followup_window_hours)

        still_active: list[EventCluster] = []
        newly_archived: list[EventCluster] = []
        for c in self.active:
            seen = _parse_iso(c.last_seen_at) or _parse_iso(c.first_seen_at)
            if seen is None or seen >= active_cutoff:
                still_active.append(c)
            elif seen >= archive_cutoff:
                newly_archived.append(c)

        kept_archived: list[EventCluster] = []
        for c in self.archived + newly_archived:
            seen = _parse_iso(c.last_seen_at) or _parse_iso(c.first_seen_at)
            if seen is not None and seen >= archive_cutoff:
                kept_archived.append(c)

        self.active = still_active[-self._cfg.max_active_clusters :]
        self.archived = kept_archived[-self._cfg.max_archived_clusters :]

    def _match_stage1(
        self, canonical_url: str, norm_key: str, sh: int
    ) -> tuple[EventCluster | None, str]:
        for c in self.active:
            if canonical_url and canonical_url in c.canonical_urls:
                return c, "url"
            if norm_key and norm_key in c.norm_keys:
                return c, "title_key"
            if sh:
                for prev_sh in c.simhashes:
                    if prev_sh and hamming_distance(sh, prev_sh) <= self._cfg.simhash_max_hamming:
                        return c, "simhash"
        return None, ""

    def _match_semantic(
        self, vec: list[float], embedder_name: str, pool: list[EventCluster], threshold: float
    ) -> tuple[EventCluster | None, float]:
        if not vec or not pool:
            return None, 0.0
        best_cluster: EventCluster | None = None
        best_sim = 0.0
        for c in pool:
            if not c.centroid or c.embedder_name != embedder_name:
                continue
            sim = cosine_similarity(vec, c.centroid)
            if sim > best_sim:
                best_sim = sim
                best_cluster = c
        if best_cluster is not None and best_sim >= threshold:
            return best_cluster, best_sim
        return None, best_sim

    def _merge_into_cluster(
        self,
        cluster: EventCluster,
        article: dict,
        pf: PreFilterResult,
        *,
        norm_key: str,
        sh: int,
        vec: list[float],
        embedder_name: str,
        seen_at: str,
    ) -> bool:
        """Merge an article into *cluster*. Returns True if unrouted Tier 3 upgraded to Tier 1/2."""
        prev_tier = cluster.source_tier
        cluster.mention_count += 1
        cluster.last_seen_at = seen_at
        if pf.canonical_url and pf.canonical_url not in cluster.canonical_urls:
            cluster.canonical_urls.append(pf.canonical_url)
            cluster.canonical_urls = cluster.canonical_urls[-_MAX_CLUSTER_URLS:]
        if norm_key and norm_key not in cluster.norm_keys:
            cluster.norm_keys.append(norm_key)
            cluster.norm_keys = cluster.norm_keys[-_MAX_CLUSTER_KEYS:]
        if sh and sh not in cluster.simhashes:
            cluster.simhashes.append(sh)
            cluster.simhashes = cluster.simhashes[-_MAX_CLUSTER_SIMHASHES:]
        if vec and cluster.embedder_name == embedder_name:
            cluster.centroid = update_centroid(cluster.centroid, cluster.mention_count - 1, vec)

        # Promote representative if the newly arrived source has strictly higher authority
        tier_upgraded = False
        if pf.source_tier < cluster.source_tier:
            cluster.source_tier = pf.source_tier
            cluster.representative_title = pf.cleaned_title
            cluster.representative_summary = pf.cleaned_summary
            cluster.representative_source = str(article.get("source") or "")
            cluster.representative_url = pf.canonical_url
            if (
                not cluster.routed
                and prev_tier >= 3
                and pf.source_tier < 3
                and not self._tier3_can_vote
            ):
                tier_upgraded = True
        return tier_upgraded

    def _find_prior_followup(
        self, sh: int, vec: list[float], embedder_name: str
    ) -> PriorEventContext | None:
        """Check 4-48h archived clusters for an ongoing cross-day story."""
        if not self.archived:
            return None
        matched: EventCluster | None = None
        sim = 0.0
        if vec:
            matched, sim = self._match_semantic(
                vec, embedder_name, self.archived, self._followup_cosine_threshold()
            )
        if matched is None and sh:
            for c in reversed(self.archived):
                if any(
                    prev_sh and hamming_distance(sh, prev_sh) <= self._cfg.simhash_max_hamming + 2
                    for prev_sh in c.simhashes
                ):
                    matched = c
                    sim = 1.0
                    break
        if matched is None:
            return None
        return PriorEventContext(
            prior_cluster_id=matched.cluster_id,
            prior_first_seen_at=matched.first_seen_at,
            prior_title=matched.representative_title,
            prior_summary=matched.classified_summary or matched.representative_summary,
            prior_category=matched.classified_category,
            prior_direction=matched.classified_direction,
            similarity=round(sim, 3),
        )

    def process_batch(
        self,
        items: list[tuple[dict, PreFilterResult]],
        now: datetime,
    ) -> list[DedupDecision]:
        """Incrementally cluster a batch of Layer-1-passing articles.

        Evaluates higher-authority (lower tier number) articles first within
        each batch (stable sort), so a Tier 1/2 wire story becomes the single
        representative when arriving alongside Tier 3 echoes. Returns decisions
        aligned with the input order.
        """
        now_tpe = _as_tpe(now)
        self.expire_windows(now_tpe)
        if not items:
            return []

        # Stable order: Tier 1 -> Tier 2 -> Tier 3, preserving input order on ties
        eval_order = sorted(range(len(items)), key=lambda i: items[i][1].source_tier)

        meta: dict[int, dict[str, Any]] = {}
        need_embed_indices: list[int] = []
        for idx in eval_order:
            art, pf = items[idx]
            nkey = normalized_title_key(pf.cleaned_title)
            sh = simhash64(pf.cleaned_title)
            art_dt = _article_time(art, now_tpe)
            embed_text = f"{pf.cleaned_title}. {pf.cleaned_summary[:250]}".strip()
            meta[idx] = {
                "article": art,
                "pf": pf,
                "norm_key": nkey,
                "simhash": sh,
                "seen_at": art_dt.isoformat(timespec="seconds"),
                "embed_text": embed_text,
            }
            hit, _ = self._match_stage1(pf.canonical_url, nkey, sh)
            if hit is None:
                need_embed_indices.append(idx)

        # Single batch embedding call for Stage-1 survivors
        vectors_by_idx: dict[int, list[float]] = {}
        if self.embedder is not None and need_embed_indices:
            texts = [meta[i]["embed_text"] for i in need_embed_indices]
            vecs = self.embedder.embed_texts(texts)
            for i, vec in zip(need_embed_indices, vecs):
                vectors_by_idx[i] = vec
        embedder_name = self._effective_embedder_name()
        active_thresh = self._active_cosine_threshold()

        decisions_by_idx: dict[int, DedupDecision] = {}
        for idx in eval_order:
            m = meta[idx]
            art = m["article"]
            pf: PreFilterResult = m["pf"]
            nkey: str = m["norm_key"]
            sh: int = m["simhash"]
            seen_at: str = m["seen_at"]
            vec: list[float] = vectors_by_idx.get(idx, [])

            # Stage 1: exact URL / cleaned-title key / SimHash against active clusters
            hit, method = self._match_stage1(pf.canonical_url, nkey, sh)
            if hit is not None:
                upgraded = self._merge_into_cluster(
                    hit, art, pf, norm_key=nkey, sh=sh, vec=vec,
                    embedder_name=embedder_name, seen_at=seen_at,
                )
                decisions_by_idx[idx] = DedupDecision(
                    article=art,
                    prefilter=pf,
                    cluster_id=hit.cluster_id,
                    is_duplicate=not upgraded,
                    dup_method=method,
                    similarity=1.0,
                    mention_count=hit.mention_count,
                    source_tier=hit.source_tier,
                    tier_upgraded=upgraded,
                )
                continue

            # Stage 2: O(k) incremental cosine similarity against active centroids
            sem_hit, sim = self._match_semantic(vec, embedder_name, self.active, active_thresh)
            if sem_hit is not None:
                upgraded = self._merge_into_cluster(
                    sem_hit, art, pf, norm_key=nkey, sh=sh, vec=vec,
                    embedder_name=embedder_name, seen_at=seen_at,
                )
                decisions_by_idx[idx] = DedupDecision(
                    article=art,
                    prefilter=pf,
                    cluster_id=sem_hit.cluster_id,
                    is_duplicate=not upgraded,
                    dup_method="semantic",
                    similarity=round(sim, 3),
                    mention_count=sem_hit.mention_count,
                    source_tier=sem_hit.source_tier,
                    tier_upgraded=upgraded,
                )
                continue

            # Stage 3: New active cluster + 24-48h cross-day archive follow-up check
            prior = self._find_prior_followup(sh, vec, embedder_name)
            cid = _make_cluster_id(now_tpe, pf.cleaned_title)
            existing_ids = {c.cluster_id for c in self.active}
            if cid in existing_ids:
                cid = f"{cid}_{idx}"
            cluster = EventCluster(
                cluster_id=cid,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
                representative_title=pf.cleaned_title,
                representative_summary=pf.cleaned_summary,
                representative_source=str(art.get("source") or ""),
                representative_url=pf.canonical_url,
                source_tier=pf.source_tier,
                mention_count=1,
                centroid=vec,
                embedder_name=embedder_name if vec else "",
                simhashes=[sh] if sh else [],
                canonical_urls=[pf.canonical_url] if pf.canonical_url else [],
                norm_keys=[nkey] if nkey else [],
            )
            self.active.append(cluster)
            if len(self.active) > self._cfg.max_active_clusters:
                self.active = self.active[-self._cfg.max_active_clusters :]

            decisions_by_idx[idx] = DedupDecision(
                article=art,
                prefilter=pf,
                cluster_id=cluster.cluster_id,
                is_duplicate=False,
                dup_method="",
                similarity=round(sim, 3),
                mention_count=1,
                source_tier=cluster.source_tier,
                tier_upgraded=False,
                prior_event=prior,
            )

        return [decisions_by_idx[i] for i in range(len(items))]

    def record_classification(
        self,
        cluster_id: str,
        *,
        summary: str = "",
        category: str = "",
        direction: str = "",
        materiality: int = 0,
        routed: bool = False,
    ) -> None:
        """Attach Layer 3 classification and routing status onto a cluster."""
        for c in reversed(self.active):
            if c.cluster_id == cluster_id:
                c.classified_summary = str(summary or "")[:300]
                c.classified_category = str(category or "")
                c.classified_direction = str(direction or "")
                c.classified_materiality = int(materiality or 0)
                if routed:
                    c.routed = True
                return

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "active": [c.to_dict() for c in self.active[-self._cfg.max_active_clusters :]],
            "archived": [
                c.to_dict(archived=True)
                for c in self.archived[-self._cfg.max_archived_clusters :]
            ],
        }

    def load_dict(self, data: Any) -> None:
        self.active = []
        self.archived = []
        if not isinstance(data, dict):
            return
        for raw in data.get("active") or []:
            c = EventCluster.from_dict(raw)
            if c is not None:
                self.active.append(c)
        for raw in data.get("archived") or []:
            c = EventCluster.from_dict(raw)
            if c is not None:
                self.archived.append(c)
        self.active = self.active[-self._cfg.max_active_clusters :]
        self.archived = self.archived[-self._cfg.max_archived_clusters :]
