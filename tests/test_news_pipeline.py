"""Comprehensive tests for the 4-layer news signal funnel (Phases 1-5).

Covers:
- Phase 1: config/news_filter.yaml loading, URL normalization, iterative media
  prefix/suffix stripping, blocked forum sources, pure price-recap blocking,
  causal catalyst rescue, off-topic filtering, source tiering.
- Phase 2: Stage-1 URL/title/SimHash dedup, O(k) sliding-window centroid
  clustering, cross-lingual EN<->ZH clustering with semantic embedder, single
  representative + mention_count increment + Tier-3->Tier-1/2 upgrade, 1-4h
  expiration -> 24-48h cross-day archive prior context, JSON state persistence,
  GeminiEmbedder batch payload + automatic fallback.
- Phase 3: Strict JSON schema validation, fail-closed degradation on malformed
  or invalid LLM responses, authoritative cluster metadata enforcement,
  cross-day delta prompt injection, transient 503 retry.
- Phase 4: Strict 5-condition pass gate, Tier-3 cannot trigger alone,
  asymmetric bullish/bearish thresholds, SKCOM/W2 price-volume confirmation,
  opt-in signal.json risk_off writer validated by src.news.signal_file.read_signal.
- Phase 4/5 integration: rss_scorer.py Feature Flag (off | shadow | live),
  partial API-error rollback, Dashboard compatibility, offline replay script.

All wall-clock tests pass a fixed ``now`` (TPE) so tests never flake on weekends.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

_BRIDGE_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "news_bridge")
if _BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _BRIDGE_DIR)

import backfill_news_v2  # noqa: E402
import replay_news_pipeline  # noqa: E402
import rss_news_log  # noqa: E402
import rss_scorer  # noqa: E402
from scripts.dashboard import news_feed, news_pulse, news_trend  # noqa: E402
from src.news.pipeline import (  # noqa: E402
    ACTION_ADVISORY_LOW_TIER,
    ACTION_DROP,
    ACTION_RISK_OFF,
    ACTION_VOTE_BEARISH,
    ACTION_VOTE_BULLISH,
    ClassifiedEvent,
    ClassifierAPIError,
    DedupDecision,
    ema_decay_for,
    Embedder,
    GeminiEmbedder,
    GeminiEventClassifier,
    IncrementalEventClusterer,
    LexicalFallbackEmbedder,
    MarketSnapshot,
    NewsPreFilter,
    NewsSignalPipeline,
    PreFilterResult,
    PriorEventContext,
    SignalRouter,
    build_classification_prompt,
    clean_title,
    load_market_snapshot,
    load_pipeline_config,
    normalize_url,
    parse_classified_event,
    resolve_source_tier,
    simhash64,
    write_risk_off_signal,
)
from src.news.circuit_breaker import BreakerState  # noqa: E402
from src.news.regime_vote import read_regime_vote  # noqa: E402
from src.news.signal_file import read_signal  # noqa: E402

_TZ_TPE = timezone(timedelta(hours=8))
_FIXED_NOW = datetime(2026, 11, 20, 21, 30, 0, tzinfo=_TZ_TPE)


def _art(
    title: str,
    *,
    summary: str = "",
    source: str = "Bloomberg Markets",
    link: str = "",
    guid: str = "",
    published_dt: datetime | None = None,
) -> dict:
    return {
        "guid": guid or f"guid:{hash((title, source, link))}",
        "title": title,
        "summary": summary,
        "source": source,
        "link": link or "https://news.example.com/articles/sample",
        "published_dt": published_dt or _FIXED_NOW,
    }


def _valid_event_json(**overrides) -> str:
    payload = {
        "event_id": "cluster_20261120_test",
        "is_duplicate": False,
        "is_price_recap_only": False,
        "has_new_fact_delta": True,
        "category": "GOVERNANCE_REGULATORY",
        "entities": ["NVDA", "TSM", "TXF"],
        "direction": "BEARISH",
        "materiality": 5,
        "horizon": "MULTI_DAY",
        "source_tier": 1,
        "mention_count": 1,
        "summary_one_line": "SEC launches accounting probe into Nvidia.",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# ── Phase 1: Config & Layer 1 NewsPreFilter ──────────────────────────────

class TestLayer1PreFilter:
    def setup_method(self):
        self.cfg = load_pipeline_config()
        self.pf = NewsPreFilter(self.cfg)

    def test_url_canonicalization_strips_tracking_and_www(self):
        u1 = normalize_url(
            "HTTPS://www.Bloomberg.com/news/nvda-sec/?utm_source=gn&b=2&fbclid=xyz&a=1#top",
            self.cfg.prefilter.tracking_query_params,
        )
        u2 = normalize_url(
            "https://bloomberg.com/news/nvda-sec?a=1&b=2",
            self.cfg.prefilter.tracking_query_params,
        )
        assert u1 == u2 == "https://bloomberg.com/news/nvda-sec?a=1&b=2"
        assert normalize_url("javascript:alert(1)", self.cfg.prefilter.tracking_query_params) == ""

    def test_media_prefix_and_multi_pipe_suffix_stripping(self):
        t1 = clean_title(
            "快訊／〈美股盤後〉只靠台積電硬撐不夠？巨人傑揭台股「致命傷」 | 蕭宥宸 | 新聞 - 風傳媒",
            self.cfg.prefilter.media_prefixes,
            self.cfg.prefilter.media_suffixes,
        )
        assert t1 == "只靠台積電硬撐不夠?巨人傑揭台股「致命傷」"

        t2 = clean_title(
            "Exclusive-Nvidia's accounting is a focus of US SEC investigation",
            self.cfg.prefilter.media_prefixes,
            self.cfg.prefilter.media_suffixes,
        )
        assert t2 == "Nvidia's accounting is a focus of US SEC investigation"

    @pytest.mark.parametrize(
        "title,source",
        [
            ("【台股開盤】跌76.69點", "Yahoo股市 - 最新新聞"),
            ("台股早盤震盪逾500點 台積電跌40元", "Yahoo股市 - 最新新聞"),
            ("今日台股》綠光照頂！台積電守2000元關卡、台股收跌2393點", "Yahoo股市 - 最新新聞"),
            ("U.S. stocks lower at close of trade; Dow Jones Industrial Average down 3.45%", "All News"),
            ("Asia stocks to drop after AI rout rocks US markets", "All News"),
            ("【Follow法人】外資重砍台積電破萬張...失血237億元！", "Yahoo股市 - 最新新聞"),
            ("盤中速報 - 櫃買市場加權指數上漲6.88點至350.99點，漲幅2%", "Anue鉅亨"),
            ("Stocks rise after Anthropic IPO debut", "All News"),
            ("台積電ADR19日下跌2.4% 較台北交易溢價18%", "Yahoo股市 - 最新新聞"),
        ],
    )
    def test_pure_price_recaps_and_institutional_flows_dropped(self, title, source):
        res = self.pf.filter_article(_art(title, source=source))
        assert not res.passed
        assert res.drop_reason in ("price_recap_only", "noise:zh_routine_list")

    @pytest.mark.parametrize(
        "title,source",
        [
            ("Nvidia shares fall 10% as report of SEC accounting probe adds to pressure", "All News"),
            ("台股遭狙擊重挫1768點 時隔4年再祭禁空令", "Yahoo股市 - 最新新聞"),
            ("美新規封中國AI！台積電傳停16奈米以下訂單、一度失守2000元", "ETtoday財經雲"),
            ("Broadcom plunges 12% as Google reportedly cuts 2027 TPU orders", "CNBC"),
            ("South Korea's KOSPI halts trade after 8% wipeout on AI losses", "All News"),
            ("Fed Chair Warsh hikes rates 25 bps as 10-year Treasury yield tops 5.0%", "Bloomberg Markets"),
            ("CoreWeave unsecured bond yield jumps to 15% as 5Y CDS widens", "Bloomberg Markets"),
            ("U.S. finalizing rule to block China's remote AI chip access", "Reuters"),
            ("Strong October jobs report dims Fed rate cut hopes", "Bloomberg Markets"),
            ("China Extends Pause on 24% US Tariffs", "Bloomberg Markets"),
            ("Taiwan's Currency Weakens Past 33 Against Dollar on Capital Outflows", "Bloomberg Markets"),
        ],
    )
    def test_causal_event_headlines_protected_even_with_price_words(self, title, source):
        res = self.pf.filter_article(_art(title, source=source))
        assert res.passed, f"Expected causal headline to pass, got {res.drop_reason}: {title}"

    @pytest.mark.parametrize(
        "title,source,expected_prefix",
        [
            ("Earnings call transcript: Acme Corp Q3 2026", "All News", "noise:investing_transcript"),
            ("CoreWeave COO Sachin Jain sells $629,236 in company shares", "All News", "noise:investing_insider_form4"),
            ("Why is CoreWeave stock surging today?", "All News", "noise:investing_why_moving"),
            ("Stifel cuts Nvidia stock price target to $150", "All News", "noise:analyst_pt_rating"),
            ("鉅亨速報 - Factset 最新調查：廣達(2382-TW)目標價調降至410元", "Anue鉅亨", "noise:analyst_pt_rating"),
            ("命理師預言2027年「台積電有望創新高」4類人要注意", "Yahoo股市 - 最新新聞", "noise:clickbait_gossip_promo"),
            ("2330 台積電 - 歷史禁空令台股短線100%破底！ - 股市爆料同學會 - CMoney", "CMoney", "blocked_source"),
            ("台積電法說前瞻", "Facebook", "blocked_source"),
            ("公告調整本公司全新期貨(GUF)保證金適用比例，請查照。", "TAIFEX", "noise:taifex_admin"),
            ("Lyft shares slide 7% on surprise Q3 loss", "All News", "off_topic"),
        ],
    )
    def test_spam_forums_admin_and_offtopic_dropped(self, title, source, expected_prefix):
        res = self.pf.filter_article(_art(title, source=source))
        assert not res.passed
        assert res.drop_reason == expected_prefix

    def test_taifex_txf_margin_hike_preserved(self):
        res = self.pf.filter_article(
            _art("公告調整本公司臺股期貨(TX)保證金適用比例，自次一營業日起實施", source="TAIFEX")
        )
        assert res.passed
        assert res.source_tier == 1

    def test_source_tier_resolution(self):
        assert resolve_source_tier("Bloomberg Markets", "", self.cfg.source_tiers) == 1
        assert resolve_source_tier("Reuters", "", self.cfg.source_tiers) == 1
        assert resolve_source_tier("經濟日報", "", self.cfg.source_tiers) == 2
        assert resolve_source_tier("All News", "https://www.investing.com/news/1", self.cfg.source_tiers) == 2
        assert resolve_source_tier("ETtoday財經雲", "", self.cfg.source_tiers) == 3
        assert resolve_source_tier("Unknown Blog", "", self.cfg.source_tiers) == 3


# ── Phase 2: Layer 2 IncrementalEventClusterer & Embedders ───────────────

class _FakeCrossLingualEmbedder(Embedder):
    """Deterministic semantic embedder simulating EN<->ZH cross-lingual alignment."""

    name = "fake_semantic"
    is_semantic = True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vecs = []
        for t in texts:
            low = t.lower()
            if ("sec" in low and ("nvidia" in low or "輝達" in low)) or ("會計審查" in t and "輝達" in t):
                vecs.append([1.0, 0.0, 0.0, 0.0])
            elif "broadcom" in low or "博通" in t:
                vecs.append([0.0, 1.0, 0.0, 0.0])
            elif "fomc" in low or "聯準會" in t:
                vecs.append([0.0, 0.0, 1.0, 0.0])
            else:
                vecs.append([0.0, 0.0, 0.0, 1.0])
        return vecs


class TestLayer2Dedup:
    def setup_method(self):
        self.cfg = load_pipeline_config()
        self.pf = NewsPreFilter(self.cfg)

    def test_cross_lingual_en_zh_same_event_deduplicated_and_mention_counted(self):
        clusterer = IncrementalEventClusterer(self.cfg, embedder=_FakeCrossLingualEmbedder())
        a_en = _art(
            "Exclusive-Nvidia's accounting is a focus of US SEC investigation",
            source="Reuters",
            link="https://www.reuters.com/tech/nvda-sec-1",
            guid="g1",
        )
        a_zh = _art(
            "快訊／路透獨家：美國SEC啟動輝達會計審查，聚焦營收認列",
            source="經濟日報",
            link="https://money.udn.com/money/story/nvda-sec-2",
            guid="g2",
        )
        a_other = _art(
            "Fed FOMC raises rates 25bps, signals higher for longer",
            source="Bloomberg Markets",
            link="https://www.bloomberg.com/news/fed-1",
            guid="g3",
        )
        kept, _, _ = self.pf.filter_batch([a_en, a_zh, a_other])
        decisions = clusterer.process_batch(kept, now=_FIXED_NOW)

        assert len(decisions) == 3
        assert decisions[0].is_duplicate is False
        assert decisions[0].source_tier == 1
        assert decisions[1].is_duplicate is True
        assert decisions[1].dup_method == "semantic"
        assert decisions[1].cluster_id == decisions[0].cluster_id
        assert decisions[1].mention_count == 2
        assert decisions[2].is_duplicate is False
        assert decisions[2].cluster_id != decisions[0].cluster_id

    def test_tier3_arriving_first_then_tier1_upgrades_cluster_once(self):
        clusterer = IncrementalEventClusterer(self.cfg, embedder=_FakeCrossLingualEmbedder())
        # Pass 1: Tier 3 aggregator reports first
        t3 = _art("美國SEC傳啟動輝達會計審查", source="ETtoday財經雲", guid="t3")
        kept1, _, _ = self.pf.filter_batch([t3])
        dec1 = clusterer.process_batch(kept1, now=_FIXED_NOW)
        assert dec1[0].is_duplicate is False
        assert dec1[0].source_tier == 3
        # Tier 3 is NOT marked routed (advisory only)
        clusterer.record_classification(dec1[0].cluster_id, summary="SEC probe", routed=False)

        # Pass 2 (15 min later): Tier 1 Reuters confirms the same cluster -> upgrades!
        t1 = _art("Exclusive-Nvidia's accounting is a focus of US SEC investigation", source="Reuters", guid="t1")
        kept2, _, _ = self.pf.filter_batch([t1])
        dec2 = clusterer.process_batch(kept2, now=_FIXED_NOW + timedelta(minutes=15))
        assert dec2[0].cluster_id == dec1[0].cluster_id
        assert dec2[0].tier_upgraded is True
        assert dec2[0].is_duplicate is False
        assert dec2[0].needs_llm is True
        assert dec2[0].source_tier == 1
        assert dec2[0].mention_count == 2
        clusterer.record_classification(dec2[0].cluster_id, summary="SEC probe confirmed", routed=True)

        # Pass 3 (30 min later): another Tier 1 copy arrives -> already routed, strict duplicate!
        t1_dup = _art("Wall Street Regulator Probing Nvidia's Accounting, Reuters Says", source="Bloomberg Markets", guid="t1b")
        kept3, _, _ = self.pf.filter_batch([t1_dup])
        dec3 = clusterer.process_batch(kept3, now=_FIXED_NOW + timedelta(minutes=30))
        assert dec3[0].is_duplicate is True
        assert dec3[0].needs_llm is False
        assert dec3[0].mention_count == 3

    def test_sliding_window_expiry_to_archive_and_crossday_prior_context(self):
        clusterer = IncrementalEventClusterer(self.cfg, embedder=_FakeCrossLingualEmbedder())
        day1 = _FIXED_NOW
        a1 = _art("Nvidia's accounting is a focus of US SEC investigation", source="Reuters", guid="d1", published_dt=day1)
        kept1, _, _ = self.pf.filter_batch([a1])
        dec1 = clusterer.process_batch(kept1, now=day1)
        clusterer.record_classification(
            dec1[0].cluster_id,
            summary="SEC opens accounting review of Nvidia.",
            category="GOVERNANCE_REGULATORY",
            direction="BEARISH",
            materiality=5,
            routed=True,
        )

        # Persist & reload state (simulating --once CLI restart 18h later)
        saved = clusterer.to_dict()
        reloaded = IncrementalEventClusterer(self.cfg, embedder=_FakeCrossLingualEmbedder())
        reloaded.load_dict(saved)

        # 18 hours later (>4h active window, <48h archive window): follow-up gets prior_event!
        day2 = day1 + timedelta(hours=18)
        a2 = _art("Nvidia responds to SEC accounting review, promises 10-Q next week", source="Bloomberg Markets", guid="d2", published_dt=day2)
        kept2, _, _ = self.pf.filter_batch([a2])
        dec2 = reloaded.process_batch(kept2, now=day2)

        assert len(reloaded.archived) == 1
        assert dec2[0].is_duplicate is False
        assert dec2[0].prior_event is not None
        assert dec2[0].prior_event.prior_cluster_id == dec1[0].cluster_id
        assert "SEC opens accounting review" in dec2[0].prior_event.prior_summary

        # 72 hours later (>48h archive window): expired from archive completely
        day4 = day1 + timedelta(hours=72)
        reloaded.expire_windows(day4)
        assert len(reloaded.archived) == 0

    def test_simhash_collapses_near_verbatim_reprints_without_semantic_embedder(self):
        clusterer = IncrementalEventClusterer(self.cfg, embedder=None)
        a1 = _art("美國商務部禁令生效 台積電暫停中國16奈米以下先進製程訂單", source="經濟日報", guid="s1", link="https://a.com/1")
        a2 = _art("美國商務部禁令生效！台積電暫停中國16奈米以下先進製程訂單", source="工商時報", guid="s2", link="https://b.com/2")
        assert simhash64(a1["title"]) == simhash64(a2["title"])
        kept, _, _ = self.pf.filter_batch([a1, a2])
        decs = clusterer.process_batch(kept, now=_FIXED_NOW)
        assert decs[0].is_duplicate is False
        assert decs[1].is_duplicate is True
        assert decs[1].dup_method in ("title_key", "simhash")

    def test_gemini_embedder_falls_back_gracefully_on_missing_key_or_error(self):
        emb = GeminiEmbedder(api_key="", output_dim=64)
        vecs = emb.embed_texts(["TSMC 2nm ramp accelerates"])
        assert len(vecs) == 1
        assert len(vecs[0]) == 64
        assert emb.last_used_provider == "lexical"
        assert emb.last_fallback_reason == "missing_api_key"


# ── Phase 3: Layer 3 Strict JSON Classifier ──────────────────────────────

class TestLayer3Classifier:
    def setup_method(self):
        self.cfg = load_pipeline_config()
        self.pf = NewsPreFilter(self.cfg)

    def test_parse_valid_schema_and_anchor_authoritative_tier(self):
        # LLM attempts to spoof source_tier=1 on a Tier 3 cluster -> must stay 3!
        raw = _valid_event_json(source_tier=1, mention_count=99, event_id="spoofed")
        ev, err = parse_classified_event(
            raw,
            event_id="cluster_20261120_real",
            source_tier=3,
            mention_count=2,
            layer2_is_duplicate=False,
            cfg=self.cfg.classifier,
        )
        assert err == ""
        assert ev is not None
        assert ev.event_id == "cluster_20261120_real"
        assert ev.source_tier == 3
        assert ev.mention_count == 2
        assert ev.category == "GOVERNANCE_REGULATORY"
        assert ev.materiality == 5

    @pytest.mark.parametrize(
        "bad_payload",
        [
            "not json at all",
            json.dumps({"category": "UNKNOWN_CAT", "direction": "BEARISH", "materiality": 5, "horizon": "INTRADAY", "is_duplicate": False, "is_price_recap_only": False, "has_new_fact_delta": True, "entities": [], "summary_one_line": "x"}),
            json.dumps({"category": "MACRO_RATES", "direction": "BEARISH", "materiality": 6, "horizon": "INTRADAY", "is_duplicate": False, "is_price_recap_only": False, "has_new_fact_delta": True, "entities": [], "summary_one_line": "x"}),
            json.dumps({"category": "MACRO_RATES", "direction": "BEARISH", "materiality": True, "horizon": "INTRADAY", "is_duplicate": False, "is_price_recap_only": False, "has_new_fact_delta": True, "entities": [], "summary_one_line": "x"}),
            json.dumps({"category": "MACRO_RATES", "direction": "BEARISH", "materiality": 4, "horizon": "INTRADAY", "is_duplicate": "false", "is_price_recap_only": False, "has_new_fact_delta": True, "entities": [], "summary_one_line": "x"}),
        ],
    )
    def test_parse_fail_closed_on_malformed_or_invalid_schema(self, bad_payload):
        ev, err = parse_classified_event(
            bad_payload,
            event_id="c1",
            source_tier=1,
            mention_count=1,
            layer2_is_duplicate=False,
            cfg=self.cfg.classifier,
        )
        assert ev is None
        assert err != ""

    def test_price_recap_true_forces_drop_neutral_even_if_llm_contradicts(self):
        raw = _valid_event_json(
            is_price_recap_only=True,
            category="AI_SUPPLY_CHAIN",
            direction="BEARISH",
            materiality=5,
        )
        ev, err = parse_classified_event(
            raw,
            event_id="c1",
            source_tier=1,
            mention_count=1,
            layer2_is_duplicate=False,
            cfg=self.cfg.classifier,
        )
        assert err == ""
        assert ev is not None
        assert ev.is_price_recap_only is True
        assert ev.category == "DROP"
        assert ev.direction == "NEUTRAL"
        assert ev.materiality <= 2

    def test_prompt_injects_crossday_prior_event_context(self):
        clusterer = IncrementalEventClusterer(self.cfg, embedder=_FakeCrossLingualEmbedder())
        a1 = _art("Nvidia's accounting is a focus of US SEC investigation", source="Reuters", published_dt=_FIXED_NOW)
        kept1, _, _ = self.pf.filter_batch([a1])
        d1 = clusterer.process_batch(kept1, now=_FIXED_NOW)
        clusterer.record_classification(d1[0].cluster_id, summary="Initial SEC accounting probe", category="GOVERNANCE_REGULATORY", direction="BEARISH")

        a2 = _art("Nvidia SEC accounting probe follow-up", source="Reuters", published_dt=_FIXED_NOW + timedelta(hours=12))
        kept2, _, _ = self.pf.filter_batch([a2])
        d2 = clusterer.process_batch(kept2, now=_FIXED_NOW + timedelta(hours=12))

        prompt = build_classification_prompt(d2[0], cfg=self.cfg.classifier, market_context_text="- SOXX: -2.10%")
        assert "Prior related event (last 24-48h" in prompt
        assert "Initial SEC accounting probe" in prompt
        assert "SOXX: -2.10%" in prompt


# ── Phase 4: Layer 4 SignalRouter & Asymmetric Gates ─────────────────────

class TestLayer4Router:
    def setup_method(self):
        self.cfg = load_pipeline_config()
        self.router = SignalRouter(self.cfg)

    def _ev(self, **kwargs) -> ClassifiedEvent:
        base = dict(
            event_id="cluster_20261120_x",
            is_duplicate=False,
            is_price_recap_only=False,
            has_new_fact_delta=True,
            category="GOVERNANCE_REGULATORY",
            entities=("NVDA", "TSM", "TXF"),
            direction="BEARISH",
            materiality=4,
            horizon="MULTI_DAY",
            source_tier=1,
            mention_count=1,
            summary_one_line="SEC accounting probe into Nvidia.",
        )
        base.update(kwargs)
        return ClassifiedEvent(**base)

    def test_strict_release_gate_rejects_recap_dup_nodelta_drop_lowmat(self):
        assert self.router.evaluate_event(self._ev(is_price_recap_only=True)).action == ACTION_DROP
        assert self.router.evaluate_event(self._ev(is_duplicate=True)).action == ACTION_DROP
        assert self.router.evaluate_event(self._ev(has_new_fact_delta=False)).action == ACTION_DROP
        assert self.router.evaluate_event(self._ev(category="DROP")).action == ACTION_DROP
        assert self.router.evaluate_event(self._ev(materiality=3)).action == ACTION_DROP

    def test_tier3_cannot_trigger_vote_alone(self):
        res = self.router.evaluate_event(self._ev(source_tier=3, materiality=5))
        assert not res.passed
        assert res.action == ACTION_ADVISORY_LOW_TIER
        assert res.weighted_score == 0.0

    def test_m4_tier2_bull_and_bear_share_one_floor(self):
        bear = self.router.evaluate_event(self._ev(direction="BEARISH", materiality=4, source_tier=2))
        bull = self.router.evaluate_event(self._ev(direction="BULLISH", materiality=4, source_tier=2))
        assert bear.passed and bear.action == ACTION_VOTE_BEARISH
        assert bull.passed and bull.action == ACTION_VOTE_BULLISH
        assert bear.weighted_score == pytest.approx(-0.34)
        assert bull.weighted_score == pytest.approx(0.34)
        assert bear.full_score == pytest.approx(-0.68)
        assert bull.full_score == pytest.approx(0.68)

    def test_bullish_m5_routes_without_a_price_gate(self):
        ev = self._ev(
            direction="BULLISH", materiality=5, source_tier=1, category="MACRO_RATES",
            summary_one_line="Fed signals pause; 10Y yield drops below 4.8%.",
        )
        adverse = MarketSnapshot(quotes={
            "^KS11": {"pct": -3.51, "fresh": True}, "NQ=F": {"pct": 2.77, "fresh": True},
            "^TWII": {"pct": 0.80, "fresh": True},
        })
        for market in (None, adverse, MarketSnapshot(skcom_bar_pct=-1.2)):
            res = self.router.evaluate_event(ev, market=market)
            assert res.passed and res.action == ACTION_VOTE_BULLISH
            assert res.weighted_score == pytest.approx(0.5)
            assert res.full_score == pytest.approx(1.0)

    def test_opt_in_risk_off_writes_valid_signal_file_readable_by_bot(self, tmp_path):
        weak_mkt = MarketSnapshot(quotes={"SOXX": {"pct": -1.8, "fresh": True}})
        # Default emit_risk_off_enabled=False never emits risk_off
        r_off = self.router.evaluate_event(self._ev(materiality=5), market=weak_mkt, emit_risk_off_enabled=False)
        assert r_off.emit_risk_off is False
        assert r_off.action == ACTION_VOTE_BEARISH

        # Explicit opt-in emit_risk_off_enabled=True emits valid schema-v1 signal.json
        r_on = self.router.evaluate_event(self._ev(materiality=5), market=weak_mkt, emit_risk_off_enabled=True)
        assert r_on.emit_risk_off is True
        assert r_on.action == ACTION_RISK_OFF

        sig_path = tmp_path / "signal.json"
        write_risk_off_signal(sig_path, r_on, now=_FIXED_NOW)
        sig, err = read_signal(sig_path, now=_FIXED_NOW + timedelta(seconds=10), max_age_sec=900)
        assert err == ""
        assert sig is not None
        assert sig.action == "risk_off"
        assert sig.direction == "bearish"
        # "high" (not "critical") so conflicting_leg gates longs without flattening winning shorts
        assert sig.severity == "high"
        breaker = BreakerState(
            signal_suppressed=True,
            signal_direction=sig.direction,
            signal_severity=sig.severity,
        )
        assert breaker.gates_leg("trend_long", scope="conflicting_leg") is True
        assert breaker.gates_leg("trend_short", scope="conflicting_leg") is False
        assert breaker.gates_leg("trend_short", scope="both") is True

    def test_asof_staleness_marks_w2_quotes_stale(self, tmp_path):
        sig_path = tmp_path / "signal.json"
        quotes_path = tmp_path / "crossmarket_quotes.json"
        stale_asof = (_FIXED_NOW - timedelta(minutes=45)).isoformat(timespec="seconds")
        quotes_path.write_text(
            json.dumps(
                {
                    "asof": stale_asof,
                    "quotes": {"SOXX": {"pct": 2.2, "fresh": True}, "NQ=F": {"pct": 1.5, "fresh": True}},
                }
            ),
            encoding="utf-8",
        )
        snap = load_market_snapshot(str(sig_path), now=_FIXED_NOW, max_file_age_s=1800)
        assert snap.quotes["SOXX"]["fresh"] is False
        # The file-age flag is unchanged. A bullish M5 no longer waits on that tape.
        ev = self._ev(direction="BULLISH", materiality=5, source_tier=1)
        res = self.router.evaluate_event(ev, market=snap)
        assert res.passed and res.action == ACTION_VOTE_BULLISH

    def test_vote_hysteresis_holds_single_shock_and_withdraws_below_floor(self):
        shock = self._ev(direction="BEARISH", materiality=4, source_tier=2)
        out = self.router.route_batch([shock], prev_session_net=0.0, active_vote_direction="")
        assert out.session_net == pytest.approx(-0.34)
        assert out.vote_direction is None  # one T2 M4 story is under the 0.85 line

        net, vote = -0.85, "trending-down"
        for _ in range(16):
            step = self.router.route_batch([], prev_session_net=net, active_vote_direction=vote)
            net = step.session_net
            vote = step.vote_direction or ""
        assert net < -0.45
        assert vote == "trending-down"

        # After 4 more quiet passes (~10 hours total) -> decays below 0.45 and withdraws cleanly
        for _ in range(4):
            step = self.router.route_batch([], prev_session_net=net, active_vote_direction=vote)
            net = step.session_net
            vote = step.vote_direction or ""
        assert abs(net) < 0.45
        assert vote == ""

    def test_breadth_multiplier_reaches_full_at_four_outlets(self):
        got = [self.router.breadth_multiplier(b) for b in range(1, 6)]
        assert got[0] == pytest.approx(0.5)
        assert got[1] == pytest.approx(2 / 3)
        assert got[2] == pytest.approx(0.5 + 0.5 * 2 / 3)
        assert got[3] == got[4] == 1.0

    def test_nov23_stories_route_through_an_adverse_tape(self):
        adverse = MarketSnapshot(quotes={
            "^TWII": {"pct": 0.80, "fresh": True},
            "NQ=F": {"pct": 2.77, "fresh": True},
            "^KS11": {"pct": -3.51, "fresh": True},
        })
        ban = self.router.evaluate_event(
            self._ev(direction="BULLISH", materiality=4, source_tier=2), market=adverse)
        assert ban.action == ACTION_VOTE_BULLISH
        assert ban.weighted_score == pytest.approx(0.34)
        assert ban.full_score == pytest.approx(0.68)
        one = self.router.evaluate_event(
            self._ev(direction="BULLISH", materiality=5, source_tier=2), market=adverse, outlets=1)
        two = self.router.evaluate_event(
            self._ev(direction="BULLISH", materiality=5, source_tier=2), market=adverse, outlets=2)
        four = self.router.evaluate_event(
            self._ev(direction="BULLISH", materiality=5, source_tier=2), market=adverse, outlets=4)
        assert one.weighted_score == pytest.approx(0.425)
        assert one.full_score == pytest.approx(0.85)
        assert two.weighted_score == pytest.approx(0.5667)
        assert four.weighted_score == pytest.approx(0.85)


class TestOutletBreadth:
    def test_outlet_key_rules_and_legacy_round_trip(self):
        from src.news.pipeline.dedup import EventCluster, outlet_key
        assert outlet_key({"title": "Ban lifted - Reuters"}) == "reuters"
        assert outlet_key({"title": "No separator here", "source": "Bloomberg Markets"}) == "bloomberg markets"
        # The replay regex needs a hyphen-free tail. "Yahoo股市- 最新新聞" has a
        # hyphen, so the title does not match and the source is used.
        assert outlet_key({"title": "Story - Yahoo股市- 最新新聞", "source": "Yahoo股市 - 最新新聞"}) == "yahoo股市"
        # Anue flash prefixes: ` - ([^-]+)$` captures the headline, not 鉅亨速報.
        # The same headline from 鉅亨速報 and 盤中速報 is one outlet. A different
        # headline is a different key (the headline itself).
        same = "台積電法說會"
        assert outlet_key({"title": f"鉅亨速報 - {same}", "source": "Anue鉅亨"}) == same
        assert outlet_key({"title": f"盤中速報 - {same}", "source": "Anue鉅亨"}) == same
        other = outlet_key({"title": "鉅亨速報 - 聯發科上修展望", "source": "Anue鉅亨"})
        assert other == "聯發科上修展望"
        assert other != same
        legacy = EventCluster.from_dict({
            "cluster_id": "legacy", "representative_source": "CNBC", "source_tier": 2,
        })
        assert legacy.outlets == ["cnbc"]
        again = EventCluster.from_dict(legacy.to_dict())
        assert again.outlets == ["cnbc"] and again.full_weight == 0.0

    def test_reweight_sums_to_full_minus_first_without_another_llm_call(self):
        p_cfg = load_pipeline_config()
        calls = {"n": 0}

        def transport(_prompt: str) -> str:
            calls["n"] += 1
            return _valid_event_json(direction="BULLISH", materiality=5, category="AI_SUPPLY_CHAIN",
                                     summary_one_line="Nvidia capacity guidance rises.")

        pipe = NewsSignalPipeline(
            p_cfg, embedder=LexicalFallbackEmbedder(64),
            classifier=GeminiEventClassifier(p_cfg, api_key="fake", transport=transport),
        )
        base = "Exclusive-Nvidia's accounting is a focus of US SEC investigation"

        def art(outlet, guid):
            return _art(f"{base} - {outlet}", source=outlet, guid=guid, link=f"https://news.example.com/{guid}")

        now = _FIXED_NOW
        first = pipe.run([art("Reuters", "a")], now=now)
        assert calls["n"] == 1
        assert first.routing.run_net == pytest.approx(0.5)
        assert first.reweight_delta == 0
        cluster = pipe.clusterer.active[0]
        assert cluster.full_weight == pytest.approx(1.0)
        assert cluster.applied_weight == pytest.approx(0.5)

        second = pipe.run([art("CNBC", "b")], now=now + timedelta(minutes=30))
        third = pipe.run(
            [art("MarketWatch", "c"), art("Financial Times", "d")],
            now=now + timedelta(minutes=60),
        )
        assert calls["n"] == 1
        assert len(cluster.outlets) == 4
        assert cluster.applied_weight == pytest.approx(1.0)
        assert first.reweight_delta + second.reweight_delta + third.reweight_delta == pytest.approx(0.5)

        repeat = pipe.run([art("Reuters", "e")], now=now + timedelta(minutes=90))
        assert calls["n"] == 1 and repeat.reweight_delta == 0 and len(cluster.outlets) == 4

        archived = pipe.run([], now=now + timedelta(hours=5))
        assert archived.reweight_delta == 0
        assert cluster not in pipe.clusterer.active

    def test_tier_upgrade_then_four_outlets_reaches_full_t2_weight(self):
        p_cfg = load_pipeline_config()
        calls = {"n": 0}

        def transport(_prompt: str) -> str:
            calls["n"] += 1
            return _valid_event_json(
                direction="BULLISH", materiality=5, category="GOVERNANCE_REGULATORY",
                summary_one_line="Short-sale ban plus the stabilization fund.",
            )

        pipe = NewsSignalPipeline(
            p_cfg, embedder=LexicalFallbackEmbedder(64),
            classifier=GeminiEventClassifier(p_cfg, api_key="fake", transport=transport),
        )
        adverse = MarketSnapshot(quotes={"^KS11": {"pct": -3.51, "fresh": True}})
        stem = "Exclusive-Nvidia's accounting is a focus of US SEC investigation"
        now = datetime(2026, 11, 23, 10, 0, tzinfo=_TZ_TPE)

        def one(outlet, source, guid, minutes):
            moment = now + timedelta(minutes=minutes)
            return pipe.run(
                [_art(f"{stem} - {outlet}", source=source, guid=guid,
                      link=f"https://news.example.com/{guid}", published_dt=moment)],
                now=moment, market=adverse,
            )

        low = one("經濟日報", "ETtoday", "t3", 0)
        assert low.passed_count == 0
        upgraded = one("經濟日報", "經濟日報", "t2", 30)
        assert upgraded.routing.run_net == pytest.approx(0.425)
        assert pipe.clusterer.active[0].full_weight == pytest.approx(0.85)
        assert pipe.clusterer.active[0].outlets == ["經濟日報"]
        for i, outlet in enumerate(("CNBC", "MarketWatch", "WSJ")):
            one(outlet, outlet, f"o{i}", 60 + 30 * i)
        assert calls["n"] == 2
        assert len(pipe.clusterer.active[0].outlets) == 4
        assert pipe.clusterer.active[0].applied_weight == pytest.approx(0.85)


# ── Phase 4/5 Integration: rss_scorer Feature Flag & Dashboard Parity ────

# 1.2 sits under v1's ±1.5 vote line and over v2's +1.0 line. Carrying that
# v1 EMA into the first live pass emits trending-up; 0.45 hysteresis can
# then hold the unconfirmed vote through the 04:58 classify.
_V1_CARRY_NET = 1.2


def _v2_live_cfg(tmp_path) -> dict:
    return {
        "feeds": ["https://example.com/rss"],
        "interval_minutes": 30,
        "state_file": str(tmp_path / "state.json"),
        "news_log_path": "",
        "gemini_api_key": "fake-key",
        "discord_webhook_url": "",
        "discord_mode": "off",
        "shadow_score": False,
        "signal_path": "",
        "pipeline_v2": "live",
        "filter_config": "",
        "emit_risk_off": False,
        "embedding_provider": "lexical",
        "rss_model": "",
    }


def _v1_carry_state(session_key: str, prior_mode: str | None) -> dict:
    """Same-session v1 EMA left in the state file before the first v2 live pass."""
    state = {
        "seen_guids": [],
        "session_key": session_key,
        "session_net": _V1_CARRY_NET,
        "session_peak": 1.5,
        "v2_active_vote": "trending-up",
    }
    if prior_mode is not None:
        state["pipeline_v2_mode"] = prior_mode
    return state


class TestRssScorerV2Integration:
    def test_pipeline_v2_mode_parser_defaults_live(self):
        assert rss_scorer.DEFAULT_PIPELINE_V2_MODE == "live"
        assert rss_scorer.pipeline_v2_mode(None) == "live"
        assert rss_scorer.pipeline_v2_mode("") == "live"
        assert rss_scorer.pipeline_v2_mode("bogus") == "live"
        assert rss_scorer.pipeline_v2_mode(True) == "live"
        assert rss_scorer.pipeline_v2_mode("shadow") == "shadow"
        assert rss_scorer.pipeline_v2_mode(False) == "off"
        assert rss_scorer.pipeline_v2_mode("off") == "off"
        assert rss_scorer.get_news_config({})["pipeline_v2"] == "live"

    def test_live_mode_end_to_end_vote_log_and_dashboard_compat(self, tmp_path):
        vote_base = tmp_path / "regime_vote.json"
        log_path = tmp_path / "news" / "news_log.jsonl"
        cfg = {
            "feeds": ["https://example.com/rss"],
            "interval_minutes": 30,
            "state_file": str(tmp_path / "state.json"),
            "news_log_path": str(log_path),
            "gemini_api_key": "fake-key",
            "discord_webhook_url": "",
            "discord_mode": "off",
            "shadow_score": False,
            "signal_path": "",
            "pipeline_v2": "live",
            "filter_config": "",
            "emit_risk_off": False,
            "embedding_provider": "lexical",
            "rss_model": "",
        }
        articles = [
            _art("【台股開盤】跌76.69點", source="Yahoo股市 - 最新新聞", guid="g_recap"),
            _art("Exclusive-Nvidia's accounting is a focus of US SEC investigation", source="Reuters", guid="g_sec_1"),
            _art("Exclusive-Nvidia's accounting is a focus of US SEC investigation", source="Bloomberg Markets", guid="g_sec_2"),
        ]
        p_cfg = load_pipeline_config()
        fake_clf = GeminiEventClassifier(p_cfg, api_key="fake", transport=lambda _p: _valid_event_json())
        fake_pipe = NewsSignalPipeline(p_cfg, embedder=LexicalFallbackEmbedder(64), classifier=fake_clf)

        state = {"seen_guids": [], "last_check": None}
        with (
            mock.patch.object(rss_scorer, "fetch_all_feeds", return_value=articles),
            mock.patch.object(rss_scorer, "_build_v2_pipeline", return_value=fake_pipe),
        ):
            state = rss_scorer.check_once(cfg, state, str(vote_base), now=_FIXED_NOW)

        # L1 dropped recap, L2 merged duplicate, L3 called only ONCE!
        stats = state["pipeline_v2_last_stats"]
        assert stats["input"] == 3
        assert stats["l1_dropped"] == 1
        assert stats["l2_duplicates"] == 1
        assert stats["l3_attempted"] == 1
        assert stats["l4_passed"] == 1
        assert stats["llm_calls_saved"] == 2
        assert state["pipeline_v2_mode"] == "live"
        assert state["v2_active_vote"] == ""
        assert state["session_net"] > -0.85

        # Clusters live in sibling sidecar, NOT bloated into main state file
        assert "pipeline_v2_clusters" not in state
        sidecar = tmp_path / rss_scorer.PIPELINE_V2_STATE_FILENAME
        assert sidecar.exists()
        sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert len(sidecar_data["active"]) == 1

        # Standard schema-v1 W3 vote file written & readable by RegimeSwitchingRunner
        session_key = rss_scorer.night_session_key(_FIXED_NOW)
        assert read_regime_vote(tmp_path / "regime_vote_w3.json", session_key) is None

        # No signal.json written because emit_risk_off is False
        assert not (tmp_path / "signal.json").exists()

        # news_log_YYYYMM.jsonl is 100% readable by Dashboard news_feed / news_pulse / news_trend
        feed_out = news_feed.news_feed(str(log_path))
        assert feed_out["available"] is True
        assert feed_out["total"] == 1
        assert feed_out["items"][0]["direction"] == "bearish"
        runs = news_trend.read_runs(str(log_path))
        assert len(runs) == 1
        drivers = news_pulse.build_drivers(feed_out["items"], session_key, rss_scorer.EMA_DECAY)
        assert len(drivers["down"]) == 1

        # Raw RSS archive preserves ALL 3 fetched articles (L1 drop + L2 dup + L3 classified)
        raw_path = Path(rss_news_log.default_raw_path(str(log_path)))
        raw_files = rss_news_log.log_files(str(raw_path))
        assert len(raw_files) == 1
        raw_rows = [json.loads(line) for line in Path(raw_files[0]).read_text(encoding="utf-8").splitlines()]
        assert len(raw_rows) == 3
        assert {r["pipeline_stage"] for r in raw_rows} == {"l1_dropped", "l2_duplicate", "l3_classified"}
        # Directly replayable by replay_news_pipeline / backfill_news_v2
        replayed = replay_news_pipeline.load_jsonl_batches(str(raw_path))
        assert sum(len(a) for _, a in replayed) == 3

    def test_v2_live_logs_one_line_per_classified_item(self, tmp_path, capsys):
        cfg = _v2_live_cfg(tmp_path)
        art = _art(
            "Exclusive-Nvidia's accounting is a focus of US SEC investigation",
            source="Reuters", guid="g_sec_log",
        )
        p_cfg = load_pipeline_config()
        clf = GeminiEventClassifier(p_cfg, api_key="fake", transport=lambda _p: _valid_event_json())
        pipe = NewsSignalPipeline(p_cfg, embedder=LexicalFallbackEmbedder(64), classifier=clf)
        with (
            mock.patch.object(rss_scorer, "fetch_all_feeds", return_value=[art]),
            mock.patch.object(rss_scorer, "_build_v2_pipeline", return_value=pipe),
        ):
            rss_scorer.check_once(cfg, {"seen_guids": []}, str(tmp_path / "vote.json"), now=_FIXED_NOW)
        out = capsys.readouterr().out
        assert out.index("[v2 live]") < out.index("    L4 ")
        lines = [ln for ln in out.splitlines() if ln.startswith("    L4 ")]
        assert len(lines) == 1
        assert "VOTE_BEARISH" in lines[0] and "M5" in lines[0] and "T1" in lines[0]
        assert "BEARISH" in lines[0] and "Nvidia" in lines[0]

    def test_l2_duplicate_raw_record_keeps_similarity_and_match(self, tmp_path):
        log_path = tmp_path / "news" / "news_log.jsonl"
        cfg = _v2_live_cfg(tmp_path)
        cfg["news_log_path"] = str(log_path)
        title = "Exclusive-Nvidia's accounting is a focus of US SEC investigation"
        articles = [
            _art(title, source="Reuters", guid="g_sec_1", link="https://www.reuters.com/nvda-1"),
            _art(title, source="Bloomberg", guid="g_sec_2", link="https://www.bloomberg.com/nvda-2"),
        ]
        p_cfg = load_pipeline_config()
        clf = GeminiEventClassifier(p_cfg, api_key="fake", transport=lambda _p: _valid_event_json())
        pipe = NewsSignalPipeline(p_cfg, embedder=LexicalFallbackEmbedder(64), classifier=clf)
        with (
            mock.patch.object(rss_scorer, "fetch_all_feeds", return_value=articles),
            mock.patch.object(rss_scorer, "_build_v2_pipeline", return_value=pipe),
        ):
            state = rss_scorer.check_once(cfg, {"seen_guids": []}, str(tmp_path / "vote.json"), now=_FIXED_NOW)
        assert "l2_duplicate_samples" not in state["pipeline_v2_last_stats"]
        raw_path = Path(rss_news_log.default_raw_path(str(log_path)))
        rows = [json.loads(line) for line in Path(rss_news_log.log_files(str(raw_path))[0]).read_text(encoding="utf-8").splitlines()]
        dup = next(r for r in rows if r["pipeline_stage"] == "l2_duplicate")
        assert dup["similarity"] == 1.0 and dup["cluster_id"]
        assert dup["matched_title"].startswith("Nvidia's accounting")
        assert dup["dup_method"]

    def test_v2_live_zero_new_articles_still_decays_and_withdraws_vote(self, tmp_path):
        vote_base = tmp_path / "regime_vote.json"
        session_key = rss_scorer.night_session_key(_FIXED_NOW)
        write_regime_vote = rss_scorer._get_write_regime_vote()
        write_regime_vote(str(vote_base), "trending-down", session_key, source="W3")

        cfg = {
            "feeds": ["https://example.com/rss"],
            "interval_minutes": 30,
            "state_file": str(tmp_path / "state.json"),
            "news_log_path": "",
            "gemini_api_key": "fake-key",
            "discord_webhook_url": "",
            "discord_mode": "off",
            "shadow_score": False,
            "signal_path": "",
            "pipeline_v2": "live",
            "filter_config": "",
            "emit_risk_off": False,
            "embedding_provider": "lexical",
            "rss_model": "",
        }
        # Standing vote at -0.50 -> 1 quiet run keeps it (-0.483 <= -0.45)
        state = {
            "seen_guids": [],
            "session_key": session_key,
            "session_net": -0.50,
            "v2_active_vote": "trending-down",
            "pipeline_v2_mode": "live",
        }
        with mock.patch.object(rss_scorer, "fetch_all_feeds", return_value=[]):
            state = rss_scorer.check_once(cfg, state, str(vote_base), now=_FIXED_NOW)
        assert state["session_net"] < -0.45
        assert state["v2_active_vote"] == "trending-down"
        assert (tmp_path / "regime_vote_w3.json").exists()

        # Three more quiet runs push net above -0.45 -> vote withdrawn even with 0 new articles!
        with mock.patch.object(rss_scorer, "fetch_all_feeds", return_value=[]):
            for step in range(1, 4):
                state = rss_scorer.check_once(
                    cfg, state, str(vote_base), now=_FIXED_NOW + timedelta(minutes=30 * step)
                )
        assert abs(state["session_net"]) < 0.45
        assert state["v2_active_vote"] == ""
        assert not (tmp_path / "regime_vote_w3.json").exists()

    @pytest.mark.parametrize(
        "prior_mode",
        ["off", None, "shadow"],
        ids=["off", "absent", "shadow"],
    )
    def test_first_v2_live_pass_ignores_v1_session_net_when_quiet(self, tmp_path, prior_mode):
        vote_base = tmp_path / "regime_vote.json"
        session_key = rss_scorer.night_session_key(_FIXED_NOW)
        state = _v1_carry_state(session_key, prior_mode)
        with mock.patch.object(rss_scorer, "fetch_all_feeds", return_value=[]):
            state = rss_scorer.check_once(
                _v2_live_cfg(tmp_path), state, str(vote_base), now=_FIXED_NOW,
            )
        assert state["pipeline_v2_mode"] == "live"
        assert state["session_net"] == pytest.approx(0.0)
        assert state["session_peak"] == pytest.approx(0.0)
        assert state["v2_active_vote"] == ""
        assert not (tmp_path / "regime_vote_w3.json").exists()

    @pytest.mark.parametrize(
        "prior_mode",
        ["off", None],
        ids=["off", "absent"],
    )
    def test_first_v2_live_pass_starts_ema_from_zero(self, tmp_path, prior_mode):
        vote_base = tmp_path / "regime_vote.json"
        session_key = rss_scorer.night_session_key(_FIXED_NOW)
        art = _art(
            "Exclusive-Nvidia's accounting is a focus of US SEC investigation",
            source="Reuters",
            guid="g_sec_handoff",
        )
        p_cfg = load_pipeline_config()
        fake_clf = GeminiEventClassifier(
            p_cfg, api_key="fake", transport=lambda _p: _valid_event_json(),
        )
        fake_pipe = NewsSignalPipeline(
            p_cfg, embedder=LexicalFallbackEmbedder(64), classifier=fake_clf,
        )
        state = _v1_carry_state(session_key, prior_mode)
        cfg = _v2_live_cfg(tmp_path)
        with (
            mock.patch.object(rss_scorer, "fetch_all_feeds", return_value=[art]),
            mock.patch.object(rss_scorer, "_build_v2_pipeline", return_value=fake_pipe),
        ):
            state = rss_scorer.check_once(cfg, state, str(vote_base), now=_FIXED_NOW)

        decay = ema_decay_for(p_cfg.router.ema_half_life_hours, cfg["interval_minutes"])
        carried = round(_V1_CARRY_NET * decay + state["last_net_score"], 4)
        assert state["last_net_score"] < 0
        assert state["session_net"] == pytest.approx(state["last_net_score"])
        assert state["session_net"] != pytest.approx(carried)
        assert state["last_net_score"] == pytest.approx(-0.5)
        assert state["v2_active_vote"] == ""
        assert read_regime_vote(tmp_path / "regime_vote_w3.json", session_key) is None

    def test_v2_live_inherits_session_net_when_already_live(self, tmp_path):
        vote_base = tmp_path / "regime_vote.json"
        session_key = rss_scorer.night_session_key(_FIXED_NOW)
        cfg = _v2_live_cfg(tmp_path)
        state = {
            "seen_guids": [],
            "session_key": session_key,
            "session_net": _V1_CARRY_NET,
            "session_peak": 1.5,
            "pipeline_v2_mode": "live",
        }
        with mock.patch.object(rss_scorer, "fetch_all_feeds", return_value=[]):
            state = rss_scorer.check_once(cfg, state, str(vote_base), now=_FIXED_NOW)

        router_cfg = load_pipeline_config().router
        decay = ema_decay_for(router_cfg.ema_half_life_hours, cfg["interval_minutes"])
        expected = round(_V1_CARRY_NET * decay, 4)
        assert expected >= router_cfg.bullish_vote_threshold
        assert state["session_net"] == pytest.approx(expected)
        assert state["session_peak"] == pytest.approx(1.5)
        assert state["v2_active_vote"] == "trending-up"
        # A quiet pass updates the held direction; it does not write the vote file.
        assert not (tmp_path / "regime_vote_w3.json").exists()

    def test_dashboard_news_pulse_uses_v2_live_thresholds_and_hysteresis(self):
        session_key = rss_scorer.night_session_key(_FIXED_NOW)
        v2_live_state = {
            "session_key": session_key,
            "session_net": -0.90,
            "session_peak": -1.25,
            "pipeline_v2_mode": "live",
            "v2_active_vote": "trending-down",
            "pipeline_v2_last_stats": {
                "bearish_vote_threshold": 0.85,
                "bullish_vote_threshold": 1.0,
                "vote_withdraw_threshold": 0.45,
            },
        }
        st_v2 = news_pulse.w3_state(v2_live_state, _FIXED_NOW, session_key, threshold=1.5)
        assert st_v2["direction"] == "trending-down"
        assert st_v2["to_threshold"] < 0.0  # already past -0.85 threshold

        # Hysteresis hold: session_net = -0.60 with standing vote still shows trending-down
        v2_hold = dict(v2_live_state, session_net=-0.60)
        st_hold = news_pulse.w3_state(v2_hold, _FIXED_NOW, session_key, threshold=1.5)
        assert st_hold["direction"] == "trending-down"

        # Legacy v1 mode unchanged: -0.90 is still neutral vs +-1.5
        v1_state = {"session_key": session_key, "session_net": -0.90, "session_peak": -0.90}
        st_v1 = news_pulse.w3_state(v1_state, _FIXED_NOW, session_key, threshold=1.5)
        assert st_v1["direction"] == ""
        assert st_v1["to_threshold"] == 0.6

    def test_api_error_rolls_back_unclassified_cluster_for_retry(self, tmp_path):
        p_cfg = load_pipeline_config()
        calls = {"n": 0}

        def flaky_transport(_prompt: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ClassifierAPIError(503, "Service Unavailable")
            return _valid_event_json()

        # Override retry_attempts=1 so first call fails immediately
        cfg_no_retry = load_pipeline_config(overrides={"classifier": {"retry_attempts": 1}})
        clf = GeminiEventClassifier(cfg_no_retry, api_key="fake", transport=flaky_transport)
        pipe = NewsSignalPipeline(cfg_no_retry, embedder=LexicalFallbackEmbedder(64), classifier=clf)

        art = _art("Exclusive-Nvidia's accounting is a focus of US SEC investigation", source="Reuters", guid="retry_g1")
        rep1 = pipe.run([art], now=_FIXED_NOW)
        assert rep1.api_error is not None
        assert "retry_g1" not in rep1.safe_seen_guids
        assert len(pipe.clusterer.active) == 0  # rolled back!

        # Next pass after API recovers -> article is NOT treated as duplicate and classifies cleanly!
        rep2 = pipe.run([art], now=_FIXED_NOW + timedelta(minutes=30))
        assert rep2.api_error is None
        assert rep2.l3_succeeded == 1
        assert rep2.passed_count == 1
        assert "retry_g1" in rep2.safe_seen_guids

    def test_shadow_mode_never_alters_v1_vote(self, tmp_path):
        vote_base = tmp_path / "regime_vote.json"
        cfg = {
            "feeds": ["https://example.com/rss"],
            "interval_minutes": 30,
            "state_file": str(tmp_path / "state.json"),
            "news_log_path": str(tmp_path / "news" / "news_log.jsonl"),
            "gemini_api_key": "fake-key",
            "discord_webhook_url": "",
            "discord_mode": "off",
            "shadow_score": False,
            "signal_path": "",
            "pipeline_v2": "shadow",
            "filter_config": "",
            "emit_risk_off": False,
            "embedding_provider": "lexical",
            "rss_model": "",
        }
        articles = [
            _art("Exclusive-Nvidia's accounting is a focus of US SEC investigation", source="Reuters", guid="sh1")
        ]
        p_cfg = load_pipeline_config()
        fake_clf = GeminiEventClassifier(p_cfg, api_key="fake", transport=lambda _p: _valid_event_json())
        fake_pipe = NewsSignalPipeline(p_cfg, embedder=LexicalFallbackEmbedder(64), classifier=fake_clf)

        # v1 returns neutral -> v1 must NOT write a vote even though v2 shadow says bearish!
        v1_scored = [{"article": articles[0], "score": {"direction": "neutral", "confidence": 0.1, "reason": "v1"}}]
        # Seed a legacy inline pipeline_v2_clusters key to verify migration out of main state
        state = {"seen_guids": [], "last_check": None, "pipeline_v2_clusters": {"version": 1, "active": []}}
        with (
            mock.patch.object(rss_scorer, "fetch_all_feeds", return_value=articles),
            mock.patch.object(rss_scorer, "score_articles", return_value=(v1_scored, ["sh1"], None)),
            mock.patch.object(rss_scorer, "_build_v2_pipeline", return_value=fake_pipe),
        ):
            state = rss_scorer.check_once(cfg, state, str(vote_base), now=_FIXED_NOW)

        assert not (tmp_path / "regime_vote_w3.json").exists()
        assert state["pipeline_v2_mode"] == "shadow"
        assert state["v2_session_net"] < 0
        assert state["pipeline_v2_last_stats"]["vote_direction"] == ""
        assert state["pipeline_v2_shadow_error"] == ""
        assert "pipeline_v2_clusters" not in state
        assert (tmp_path / rss_scorer.PIPELINE_V2_STATE_FILENAME).exists()

    def test_offline_replay_script_on_repo_xml_fixtures(self):
        fixtures = [
            str(Path(__file__).resolve().parent / "fixtures" / "news" / name)
            for name in ("cnyes_tw_stock.xml", "yahoo_tw_market.xml", "taifex_rss1.xml")
        ]
        batches = replay_news_pipeline.load_xml_batches(fixtures)
        summary = replay_news_pipeline.replay_batches(batches, embedder_provider="lexical")
        assert summary["total_input"] == 170
        assert summary["l1_drop_rate_pct"] >= 60.0
        assert summary["llm_savings_rate_pct"] >= 65.0
        text = replay_news_pipeline.format_report(summary)
        assert "Offline Replay Report" in text

    def test_backfill_end_to_end_in_place_backup_cache_state_and_dashboard_parity(self, tmp_path):
        bridge_dir = tmp_path / "bridge"
        news_dir = bridge_dir / "news"
        news_dir.mkdir(parents=True)
        log_base = news_dir / "news_log.jsonl"
        old_month = news_dir / "news_log_202611.jsonl"
        state_file = bridge_dir / "rss_scorer_state.json"
        state_file.write_text(
            json.dumps({"seen_guids": ["existing_guid"], "fetch_status": "ok"}),
            encoding="utf-8",
        )

        t1 = datetime(2026, 11, 20, 18, 0, 0, tzinfo=_TZ_TPE)
        t2 = datetime(2026, 11, 20, 19, 30, 0, tzinfo=_TZ_TPE)  # +90 min same session
        session_key = rss_scorer.night_session_key(t1)
        old_rows = [
            rss_news_log.build_record(
                _art("【台股開盤】跌76.69點", source="Yahoo股市 - 最新新聞", link="https://ex.com/recap", published_dt=t1),
                {"direction": "bearish", "confidence": 0.9, "reason": "v1 noise"},
                scored_at=t1, session=session_key, run_net=-0.5, session_net=-0.5,
            ),
            rss_news_log.build_record(
                _art("Exclusive-Nvidia's accounting is a focus of US SEC investigation", source="Reuters", link="https://reuters.com/nvda-sec", published_dt=t1),
                {"direction": "bearish", "confidence": 0.9, "reason": "v1"},
                scored_at=t1, session=session_key, run_net=-1.0, session_net=-1.0,
            ),
            rss_news_log.build_record(
                _art("Exclusive-Nvidia's accounting is a focus of US SEC investigation", source="Bloomberg Markets", link="https://bloomberg.com/nvda-sec", published_dt=t1),
                {"direction": "bearish", "confidence": 0.9, "reason": "v1 dup"},
                scored_at=t1, session=session_key, run_net=-1.5, session_net=-1.5,
            ),
            rss_news_log.build_record(
                _art("TSMC raises 2nm CoWoS capacity guidance for 2027", source="Bloomberg Markets", link="https://bloomberg.com/tsmc-2nm", published_dt=t2),
                {"direction": "bullish", "confidence": 0.8, "reason": "v1"},
                scored_at=t2, session=session_key, run_net=0.4, session_net=-0.8,
            ),
        ]
        old_month.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in old_rows) + "\n",
            encoding="utf-8",
        )

        p_cfg = load_pipeline_config()
        calls = {"n": 0}

        def fake_transport(prompt: str) -> str:
            calls["n"] += 1
            if "2nm CoWoS" in prompt:
                return _valid_event_json(
                    category="AI_SUPPLY_CHAIN",
                    direction="BULLISH",
                    materiality=4,
                    summary_one_line="TSMC raises 2nm CoWoS capacity guidance.",
                )
            return _valid_event_json()

        cache_path = tmp_path / "backfill_cache.jsonl"
        batches = replay_news_pipeline.load_jsonl_batches(str(log_base))
        assert len(batches) == 2

        clf1 = backfill_news_v2.CachedClassifier(
            GeminiEventClassifier(p_cfg, api_key="fake", transport=fake_transport),
            backfill_news_v2.ClassificationCache(cache_path),
            pace_s=0.0,
        )
        pipe1 = NewsSignalPipeline(p_cfg, embedder=LexicalFallbackEmbedder(64), classifier=clf1)
        res1 = backfill_news_v2.backfill_batches(batches, pipeline=pipe1, progress_every=0)

        # L1 drops recap, L2 dedups duplicate SEC headline -> only 2 LLM calls for 4 rows!
        assert res1.input_articles == 4
        assert res1.l1_dropped == 1
        assert res1.l2_duplicates == 1
        assert res1.api_calls == 2
        assert calls["n"] == 2
        assert res1.l4_passed == 2  # M4 bull now passes; one story is still under the vote line
        assert res1.votes_down == 0

        # Re-run with same cache -> 0 new Gemini calls!
        clf2 = backfill_news_v2.CachedClassifier(
            GeminiEventClassifier(p_cfg, api_key="fake", transport=fake_transport),
            backfill_news_v2.ClassificationCache(cache_path),
            pace_s=0.0,
        )
        pipe2 = NewsSignalPipeline(p_cfg, embedder=LexicalFallbackEmbedder(64), classifier=clf2)
        res2 = backfill_news_v2.backfill_batches(batches, pipeline=pipe2, progress_every=0)
        assert res2.api_calls == 0
        assert res2.cache_hits == 2

        # Write in-place with v1 backup + state/sidecar sync
        out = backfill_news_v2.write_backfill_outputs(
            res1,
            target_log_base=str(log_base),
            in_place=True,
            state_file=str(state_file),
            update_state=True,
            now=t2,
        )
        assert out["backup_dir"] and (Path(out["backup_dir"]) / "news_log_202611.jsonl").exists()
        assert len(out["raw_files"]) == 1
        raw_backfill_rows = [
            json.loads(line)
            for line in Path(out["raw_files"][0]).read_text(encoding="utf-8").splitlines()
        ]
        assert len(raw_backfill_rows) == 4  # ALL 4 raw articles preserved with L1/L2/L3 fate + v1 scores
        assert {r["pipeline_stage"] for r in raw_backfill_rows} == {"l1_dropped", "l2_duplicate", "l3_classified"}
        assert all("v1_direction" in r and "v1_score" in r for r in raw_backfill_rows)
        backfill_dups = [r for r in raw_backfill_rows if r["pipeline_stage"] == "l2_duplicate"]
        assert backfill_dups and backfill_dups[0]["similarity"] == 1.0
        assert backfill_dups[0]["matched_title"]
        assert out["state_updated"] is True
        assert Path(out["sidecar_path"]).exists()

        saved_state = json.loads(state_file.read_text(encoding="utf-8"))
        assert "existing_guid" in saved_state["seen_guids"]  # preserved!
        assert saved_state["pipeline_v2_mode"] == "live"
        assert saved_state["v2_active_vote"] == ""
        assert "pipeline_v2_clusters" not in saved_state

        # Dashboard pulse + feed + trend all read ONLY the clean backfilled v2 data (not raw/)
        feed_after = news_feed.news_feed(str(log_base))
        assert feed_after["total"] == 2
        pulse = news_pulse.pulse(
            t2,
            str(log_base),
            {"rss_state_file": str(state_file)},
            lambda p: json.loads(Path(p).read_text(encoding="utf-8")) if Path(p).is_file() else None,
        )
        assert pulse["threshold"] == pytest.approx(0.85)
        assert pulse["w3"]["direction"] == ""  # one bearish story no longer clears 0.85
        assert len(pulse["drivers"]["down"]) == 1
        # The log still stores the legacy signed materiality, not the breadth weight.
        assert pulse["drivers"]["down"][0]["contribution"] < -0.90
        runs = news_trend.read_runs(str(log_base))
        assert len(runs) == 2

    def test_backfill_historical_market_provider_zero_lookahead(self):
        # Two 30-min candles: 18:00-18:30 (+1.0%) and 18:30-19:00 (-5.0% crash)
        t_c1 = int(datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc).timestamp())
        t_c2 = int(datetime(2026, 11, 20, 18, 30, tzinfo=timezone.utc).timestamp())
        provider = backfill_news_v2.HistoricalMarketProvider(
            candles=[
                {"time": t_c1, "open": 20000.0, "high": 20250.0, "low": 20000.0, "close": 20200.0},
                {"time": t_c2, "open": 20200.0, "high": 20200.0, "low": 19000.0, "close": 19000.0},
            ],
            symbol="TX00",
            interval_s=1800,
        )
        # At 18:45 TPE, candle 2 (ending 19:00) is NOT completed yet -> must use candle 1 only!
        snap = provider.snapshot_at(datetime(2026, 11, 20, 18, 45, tzinfo=_TZ_TPE))
        assert snap is not None
        assert snap.skcom_bar_pct == pytest.approx(1.0)
        # At 19:00 TPE, candle 2 IS completed -> sees the -5.0% drop
        snap2 = provider.snapshot_at(datetime(2026, 11, 20, 19, 0, tzinfo=_TZ_TPE))
        assert snap2 is not None
        assert snap2.skcom_bar_pct == pytest.approx(-5.0)


def _backfill_decision(prior_cluster_id: str | None) -> DedupDecision:
    """DedupDecision whose optional prior is a real PriorEventContext."""
    prior = None
    if prior_cluster_id is not None:
        prior = PriorEventContext(
            prior_cluster_id=prior_cluster_id,
            prior_first_seen_at="2026-11-20T03:30:00+08:00",
            prior_title="Nvidia SEC accounting probe",
            prior_summary="SEC opens accounting review of Nvidia.",
            prior_category="GOVERNANCE_REGULATORY",
            prior_direction="BEARISH",
            similarity=0.91,
        )
    return DedupDecision(
        article={"guid": "followup", "title": "Nvidia responds to SEC accounting review", "source": "Bloomberg Markets"},
        prefilter=PreFilterResult(
            passed=True,
            drop_reason="",
            cleaned_title="Nvidia responds to SEC accounting review",
            cleaned_summary="Nvidia said it will file its 10-Q next week.",
            canonical_url="https://news.example.com/nvda-sec-followup",
            source_tier=1,
        ),
        cluster_id="cluster_followup",
        is_duplicate=False,
        dup_method="",
        similarity=0.91,
        mention_count=1,
        source_tier=1,
        prior_event=prior,
    )


class TestBackfillPriorEventCache:
    """CachedClassifier cache keys read PriorEventContext.prior_cluster_id."""

    def test_cache_key_differs_by_prior_cluster_id(self):
        first = _backfill_decision("cluster_aaa")
        same = _backfill_decision("cluster_aaa")
        other = _backfill_decision("cluster_bbb")

        key_first = backfill_news_v2._cache_key(first)
        key_same = backfill_news_v2._cache_key(same)
        key_other = backfill_news_v2._cache_key(other)

        assert key_first == key_same
        assert key_first != key_other
        assert len(key_first) == 24

        # No prior_event still hashes (empty prior id). A real prior changes the key.
        bare = _backfill_decision(None)
        assert backfill_news_v2._cache_key(bare) == backfill_news_v2._cache_key(_backfill_decision(None))
        assert backfill_news_v2._cache_key(bare) != key_first

    def test_non_dry_run_batch_classifies_prior_event_and_writes_cache(self, tmp_path):
        cfg = load_pipeline_config()
        pf = NewsPreFilter(cfg)
        day1 = _FIXED_NOW
        seed = IncrementalEventClusterer(cfg, embedder=_FakeCrossLingualEmbedder())
        kept1, _, _ = pf.filter_batch([
            _art(
                "Nvidia's accounting is a focus of US SEC investigation",
                source="Reuters",
                guid="d1",
                published_dt=day1,
            )
        ])
        dec1 = seed.process_batch(kept1, now=day1)
        assert dec1 and dec1[0].prior_event is None
        seed.record_classification(
            dec1[0].cluster_id,
            summary="SEC opens accounting review of Nvidia.",
            category="GOVERNANCE_REGULATORY",
            direction="BEARISH",
            materiality=5,
            routed=True,
        )
        saved = seed.to_dict()

        day2 = day1 + timedelta(hours=18)
        follow = _art(
            "Nvidia responds to SEC accounting review, promises 10-Q next week",
            source="Bloomberg Markets",
            guid="d2",
            published_dt=day2,
        )
        # Same archived state the backfill batch will load: the follow-up carries a prior.
        preview = IncrementalEventClusterer(cfg, embedder=_FakeCrossLingualEmbedder())
        preview.load_dict(saved)
        kept2, dropped2, _ = pf.filter_batch([follow])
        assert dropped2 == []
        preview_dec = preview.process_batch(kept2, now=day2)
        assert len(preview_dec) == 1
        assert preview_dec[0].needs_llm is True
        assert preview_dec[0].prior_event is not None
        assert preview_dec[0].prior_event.prior_cluster_id == dec1[0].cluster_id

        seen: list[DedupDecision] = []

        class _StubClassifier:
            def classify(self, decision, *, market_context_text=""):
                assert market_context_text == ""
                seen.append(decision)
                return ClassifiedEvent(
                    event_id=decision.cluster_id,
                    is_duplicate=False,
                    is_price_recap_only=False,
                    has_new_fact_delta=True,
                    category="GOVERNANCE_REGULATORY",
                    entities=("NVDA", "TSM", "TXF"),
                    direction="BEARISH",
                    materiality=5,
                    horizon="MULTI_DAY",
                    source_tier=decision.source_tier,
                    mention_count=decision.mention_count,
                    summary_one_line="Nvidia follow-up on the SEC accounting review.",
                ), ""

        cache_path = tmp_path / "backfill_cache.jsonl"
        clf = backfill_news_v2.CachedClassifier(
            _StubClassifier(),
            backfill_news_v2.ClassificationCache(cache_path),
            pace_s=0.0,
        )
        pipe = NewsSignalPipeline(cfg, embedder=_FakeCrossLingualEmbedder(), classifier=clf)
        pipe.load_state(saved)

        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            res = backfill_news_v2.backfill_batches(
                [(day2, [follow])],
                pipeline=pipe,
                dry_run=False,
                progress_every=0,
            )

        assert len(seen) == 1
        assert seen[0].prior_event is not None
        assert seen[0].prior_event.prior_cluster_id == dec1[0].cluster_id
        assert res.batches_processed == 1
        assert res.l3_succeeded == 1
        assert res.api_calls == 1
        assert cache_path.is_file()
        rows = [
            json.loads(line)
            for line in cache_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["k"] == backfill_news_v2._cache_key(seen[0])
        assert rows[0]["event"]["summary_one_line"] == "Nvidia follow-up on the SEC accounting review."
