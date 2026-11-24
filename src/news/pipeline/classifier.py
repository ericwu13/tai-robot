"""Layer 3 — Whitelist event classification with strict JSON schema validation.

Only called for Layer-2 new event representatives (or unrouted Tier-3 → Tier-1/2
upgrades), dramatically reducing Gemini calls compared to v1's per-article scoring.

Forces the model to evaluate:
1. ``is_price_recap_only`` (retrospective price/point summary without new catalyst)
2. ``has_new_fact_delta`` (against 24-48h archived prior event context when present)
3. Strict 4-category whitelist (``MACRO_RATES``, ``AI_SUPPLY_CHAIN``,
   ``GOVERNANCE_REGULATORY``, ``CREDIT_FLOWS``) or ``DROP``
4. TXF-centric ``direction`` (``BULLISH`` | ``BEARISH`` | ``NEUTRAL``) and
   ``materiality`` (integer 1-5)

Any malformed JSON or schema violation degrades safely (fail-closed -> ``None``)
and never triggers a vote or order.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import ClassifierConfig, PipelineConfig
from .dedup import DedupDecision, PriorEventContext

_GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
_UA = {"User-Agent": "Mozilla/5.0 (tai-robot news pipeline)", "Connection": "close"}
_TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})


CLASSIFICATION_PROMPT = """\
You are a strict institutional catalyst classifier for Taiwan Equity Index Futures (TXF / 台指期),
which is heavily driven by TSMC (2330 / TSM ~30% weight), MediaTek (2454), Taiwan AI server ODMs,
US AI semis (NVDA, AVGO, MU, SK Hynix, SOX), US hyperscalers (MSFT, GOOGL, AMZN, META, ORCL),
and US macro rates / credit conditions.

Analyze the article (English or Traditional Chinese) and respond with ONLY one valid JSON object
matching this exact schema — no markdown fences, no commentary:
{{
  "event_id": "{event_id}",
  "is_duplicate": false,
  "is_price_recap_only": true|false,
  "has_new_fact_delta": true|false,
  "category": "MACRO_RATES"|"AI_SUPPLY_CHAIN"|"GOVERNANCE_REGULATORY"|"CREDIT_FLOWS"|"DROP",
  "entities": ["TICKER_OR_ENTITY"],
  "direction": "BULLISH"|"BEARISH"|"NEUTRAL",
  "materiality": 1|2|3|4|5,
  "horizon": "INTRADAY"|"MULTI_DAY"|"STRUCTURAL",
  "source_tier": {source_tier},
  "mention_count": {mention_count},
  "summary_one_line": "<concise objective factual summary in Traditional Chinese or English>"
}}

STRICT EVALUATION ORDER (follow steps 1-4 in order):

Step 1 — Price Recap Gate (is_price_recap_only):
- Set is_price_recap_only = true if the article mainly reports price/index/point moves that ALREADY happened
  (closing wraps, intraday plunges/rallies, "台股重挫/大漲 N 點", "美股收黑/收紅", "stocks fell/rallied",
  routine 三大法人買賣超, retail fractional-share volume trivia) WITHOUT revealing a concrete NEW
  fundamental, policy, or credit catalyst that markets have not yet priced in.
- If is_price_recap_only is true, you MUST set category = "DROP", direction = "NEUTRAL", materiality = 1.

Step 2 — Cross-Day New Fact Delta Gate (has_new_fact_delta):
- If "Prior related event (last 24-48h)" is provided below, compare carefully:
  set has_new_fact_delta = false if this article only rehashes, comments on, or recaps the prior event
  with no concrete NEW numbers, filings (e.g. 10-Q), rulings, official confirmations, or policy shifts.
- If no prior event is given and the article reports a concrete new fact, set has_new_fact_delta = true.

Step 3 — Whitelist Event Taxonomy (ONLY 4 categories pass; everything else is DROP):
- MACRO_RATES: Fed FOMC decisions/dot plot, Fed Chair (Warsh) guidance, US CPI/PCE/NFP surprises,
  US 10Y Treasury yield crossing key levels (e.g. 5.0%), oil/energy shocks (Iran/Brent/WTI),
  Taiwan Central Bank policy.
- AI_SUPPLY_CHAIN: TSMC monthly revenue/earnings/gross margin/2nm/CoWoS capacity; NVDA/AVGO/MU/SK Hynix/Dell
  earnings, guidance, gross margins, Vera Rubin/HBM4 ramp; Hyperscaler (MSFT/GOOGL/AMZN/META/ORCL) CapEx
  revisions, FCF inflection, RPO/ARR; physical power/cooling bottlenecks (GE Vernova/Vertiv/Eaton);
  model monetization shifts (Anthropic/OpenAI).
  CRITICAL TXF NUANCE: distinguish supplier mix shift from demand destruction! E.g., Google shifting TPU
  orders from Broadcom (AVGO) to MediaTek (2454) hurts AVGO but still fabricates at TSMC and benefits
  MediaTek — evaluate NET impact on TXF / Taiwan core supply chain, not a single US loser.
- GOVERNANCE_REGULATORY: Core heavyweight executive upheaval, SEC accounting probes, 10-Q filing delays or
  clean 10-Q/CEO resolution, AG subpoenas; US Commerce BIS chip/remote-compute export controls; federal or
  Texas (ERCOT/TCEQ) data-center moratoriums or power permit freezes/approvals; Taiwan FSC short-selling
  bans (限空令/禁空令), National Stabilization Fund (國安基金), exchange circuit breakers / trading halts
  (TAIFEX/TWSE/KOSPI), major settlement default cascades.
- CREDIT_FLOWS: Big Tech halting buybacks, mega debt or ATM equity issuance (GOOGL/META/ORCL/MSFT);
  AI credit stress indicators (CoreWeave unsecured bond yield/price, GPU-backed DDTL, Oracle/hyperscaler
  5Y CDS spread jumps or tightening); extreme institutional positioning shifts (BofA FMS survey, massive
  SOXX/SMH/SOXL/DRAM/QQQ ETF flows).
- DROP: Pure price recaps, post-hoc technical commentary, routine institutional buy/sell recaps,
  non-heavyweight small-cap stocks, analyst opinion/price targets without new facts, gossip/clickbait.

Step 4 — Materiality (1-5) & Direction (TXF perspective):
- 5 = Immediate systemic / Tier-1 pricing shock OR major risk-clearing resolution (e.g. surprise Fed shift
  or 10Y < 5% dovish pivot, NVDA SEC probe / clean 10-Q + permanent CEO resolution, BIS 16nm export halt,
  KOSPI/TXF trading halt, CoreWeave/Oracle CDS blowout or sharp tightening, major TSMC guidance surprise).
- 4 = Concrete high-impact catalyst for TXF over intraday to multi-day horizon (e.g. FSC 限空令, hyperscaler
  CapEx/ATM equity shock, major Broadcom/Micron guidance surprise, BofA FMS extreme positioning shift).
- 3 = Moderate/incremental industry news (not actionable alone).
- 1-2 = Noise, commentary, priced-in recap, or irrelevant small-cap.
- Be equally strict with BULLISH and BEARISH: reserve materiality 5 for undeniable quantitative beats or
  misses, or major risk-on or risk-off catalysts.

{prior_context_block}{market_context_block}Source: {source} (tier {source_tier}, mentions={mention_count})
Headline: {headline}
Summary: {summary}
"""


class ClassifierAPIError(Exception):
    """Gemini classification HTTP / network error (propagated to W3 error_streak)."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"HTTP {status}: {message}" if status else message)


@dataclass(frozen=True)
class ClassifiedEvent:
    """Strictly validated Layer 3 structured classification output."""

    event_id: str
    is_duplicate: bool
    is_price_recap_only: bool
    has_new_fact_delta: bool
    category: str
    entities: tuple[str, ...]
    direction: str
    materiality: int
    horizon: str
    source_tier: int
    mention_count: int
    summary_one_line: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "is_duplicate": self.is_duplicate,
            "is_price_recap_only": self.is_price_recap_only,
            "has_new_fact_delta": self.has_new_fact_delta,
            "category": self.category,
            "entities": list(self.entities),
            "direction": self.direction,
            "materiality": self.materiality,
            "horizon": self.horizon,
            "source_tier": self.source_tier,
            "mention_count": self.mention_count,
            "summary_one_line": self.summary_one_line,
        }


def _strip_code_fences(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return cleaned


def _should_retry_classification(text: str, err: str, finish_reason: str) -> bool:
    """Retry truncated JSON once. A valid JSON schema or enum miss is final."""
    if not err:
        return False
    try:
        json.loads(_strip_code_fences(text))
    except ValueError:
        return err.startswith("json_decode_error") or finish_reason.upper() == "MAX_TOKENS"
    return False


def _require_bool(raw: dict[str, Any], key: str) -> tuple[bool | None, str]:
    val = raw.get(key)
    if isinstance(val, bool):
        return val, ""
    return None, f"{key} must be boolean, got {type(val).__name__}"


def parse_classified_event(
    raw_or_text: str | dict[str, Any],
    *,
    event_id: str,
    source_tier: int,
    mention_count: int,
    layer2_is_duplicate: bool,
    cfg: ClassifierConfig,
) -> tuple[ClassifiedEvent | None, str]:
    """Validate LLM output strictly against the schema.

    Returns ``(ClassifiedEvent, "")`` on success or ``(None, error_reason)`` on
    any parse or schema error (safe fail-closed degradation).

    Authoritative cluster fields (``event_id``, ``source_tier``,
    ``mention_count``) are anchored from Layer 2 so an LLM cannot spoof source
    authority. ``is_duplicate`` is OR'd with Layer 2.
    """
    if isinstance(raw_or_text, str):
        try:
            data = json.loads(_strip_code_fences(raw_or_text))
        except ValueError as exc:
            return None, f"json_decode_error: {exc}"
    elif isinstance(raw_or_text, dict):
        data = raw_or_text
    else:
        return None, f"expected JSON object, got {type(raw_or_text).__name__}"

    if not isinstance(data, dict):
        return None, "root JSON value is not an object"

    is_dup_llm, err = _require_bool(data, "is_duplicate")
    if err:
        return None, err
    is_recap, err = _require_bool(data, "is_price_recap_only")
    if err:
        return None, err
    has_delta, err = _require_bool(data, "has_new_fact_delta")
    if err:
        return None, err

    category = str(data.get("category") or "").strip().upper()
    if category not in cfg.allowed_categories:
        return None, f"invalid category: {category!r}"

    direction = str(data.get("direction") or "").strip().upper()
    if direction not in cfg.allowed_directions:
        return None, f"invalid direction: {direction!r}"

    horizon = str(data.get("horizon") or "").strip().upper()
    if horizon not in cfg.allowed_horizons:
        return None, f"invalid horizon: {horizon!r}"

    mat_raw = data.get("materiality")
    if isinstance(mat_raw, bool) or not isinstance(mat_raw, int) or not (1 <= mat_raw <= 5):
        return None, f"invalid materiality: {mat_raw!r}"

    raw_entities = data.get("entities")
    if not isinstance(raw_entities, list):
        return None, f"entities must be list, got {type(raw_entities).__name__}"
    entities: list[str] = []
    for item in raw_entities[:10]:
        tok = str(item or "").strip()
        if tok and tok not in entities:
            entities.append(tok[:32])

    summary = str(data.get("summary_one_line") or "").strip()
    if not summary:
        return None, "missing summary_one_line"

    # Enforce invariant: price-recap-only can never carry a live category/direction
    if is_recap:
        category = "DROP"
        direction = "NEUTRAL"
        mat_raw = min(mat_raw, 2)

    return ClassifiedEvent(
        event_id=str(event_id),
        is_duplicate=bool(layer2_is_duplicate or is_dup_llm),
        is_price_recap_only=bool(is_recap),
        has_new_fact_delta=bool(has_delta),
        category=category,
        entities=tuple(entities),
        direction=direction,
        materiality=int(mat_raw),
        horizon=horizon,
        source_tier=int(source_tier),
        mention_count=max(1, int(mention_count)),
        summary_one_line=summary[:240],
    ), ""


def build_classification_prompt(
    decision: DedupDecision,
    *,
    cfg: ClassifierConfig,
    market_context_text: str = "",
) -> str:
    """Build the Layer 3 structured classification prompt for one representative."""
    prior: PriorEventContext | None = decision.prior_event
    if prior is not None:
        prior_block = (
            "Prior related event (last 24-48h — check carefully for has_new_fact_delta):\n"
            f"- Prior Event ID: {prior.prior_cluster_id} (first seen {prior.prior_first_seen_at})\n"
            f"- Prior Category/Direction: {prior.prior_category or 'unknown'} / {prior.prior_direction or 'unknown'}\n"
            f"- Prior Headline: {prior.prior_title}\n"
            f"- Prior Summary: {prior.prior_summary[:240]}\n\n"
        )
    else:
        prior_block = ""

    if market_context_text:
        market_block = (
            "Current cross-market moves (use to judge whether price move is already priced in):\n"
            f"{market_context_text}\n\n"
        )
    else:
        market_block = ""

    return CLASSIFICATION_PROMPT.format(
        event_id=decision.cluster_id,
        source_tier=decision.source_tier,
        mention_count=decision.mention_count,
        prior_context_block=prior_block,
        market_context_block=market_block,
        source=str(decision.article.get("source") or "unknown"),
        headline=decision.prefilter.cleaned_title,
        summary=decision.prefilter.cleaned_summary[: cfg.summary_max_chars],
    )


class GeminiEventClassifier:
    """Layer 3 Gemini REST client with strict schema validation and transient retry."""

    def __init__(
        self,
        config: PipelineConfig,
        api_key: str,
        *,
        model_override: str = "",
        transport=None,
    ):
        self._cfg: ClassifierConfig = config.classifier
        self.api_key: str = str(api_key or "").strip()
        self.model: str = (model_override or self._cfg.model).strip()
        # Injectable transport(prompt: str) -> str for deterministic unit tests
        self._transport = transport

    def _call_gemini(self, prompt: str) -> tuple[str, str]:
        if self._transport is not None:
            raw = self._transport(prompt)
            if isinstance(raw, tuple):
                return str(raw[0]), str(raw[1] if len(raw) > 1 else "")
            return str(raw), ""
        if not self.api_key:
            raise ClassifierAPIError(0, "missing Gemini API key")

        url = _GEMINI_API_URL.format(model=self.model, key=self.api_key)
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": self._cfg.max_output_tokens,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingLevel": "minimal"},
            },
        }).encode("utf-8")

        for attempt in range(self._cfg.retry_attempts):
            try:
                req = urllib.request.Request(
                    url, data=payload, headers={**_UA, "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=self._cfg.timeout_s) as resp:
                    data = json.load(resp)
                candidate = data["candidates"][0]
                finish = str(candidate.get("finishReason") or "")
                text = ""
                for part in candidate["content"]["parts"]:
                    if "thought" not in part and "text" in part:
                        text = part["text"]
                return text, finish
            except urllib.error.HTTPError as exc:
                status = exc.code
                if status in _TRANSIENT_HTTP and attempt + 1 < self._cfg.retry_attempts:
                    time.sleep(self._cfg.retry_base_delay_s * (2 ** attempt))
                    continue
                if status == 429:
                    msg = "credits depleted / rate limited"
                elif status >= 500:
                    msg = f"server error ({exc.reason})"
                else:
                    msg = str(exc.reason)
                raise ClassifierAPIError(status, msg) from exc
            except OSError as exc:
                if attempt + 1 < self._cfg.retry_attempts:
                    time.sleep(self._cfg.retry_base_delay_s * (2 ** attempt))
                    continue
                raise ClassifierAPIError(0, f"connection error: {exc}") from exc
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                # Response body shape problem — fail-closed (not a transport outage)
                return f"__invalid_response__:{type(exc).__name__}:{exc}", ""

        raise ClassifierAPIError(0, "retry limit exhausted")

    def classify(
        self,
        decision: DedupDecision,
        *,
        market_context_text: str = "",
    ) -> tuple[ClassifiedEvent | None, str]:
        """Classify one Layer-2 representative.

        Raises :class:`ClassifierAPIError` on HTTP/network failure; returns
        ``(None, error_reason)`` on parse or schema failure (fail-closed).
        """
        prompt = build_classification_prompt(
            decision, cfg=self._cfg, market_context_text=market_context_text
        )
        event, err = None, ""
        for attempt in range(2):
            raw_text, finish = self._call_gemini(prompt)
            event, err = parse_classified_event(
                raw_text,
                event_id=decision.cluster_id,
                source_tier=decision.source_tier,
                mention_count=decision.mention_count,
                layer2_is_duplicate=decision.is_duplicate,
                cfg=self._cfg,
            )
            if event is not None or attempt or not _should_retry_classification(raw_text, err, finish):
                break
        return event, err
