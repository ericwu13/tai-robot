"""Cluster window, frozen anchor, and one JSON retry. Stub embedders only."""
import io
import json
import math
from datetime import timedelta

import pytest

from src.news.pipeline import (
    ClassifierConfig, DedupDecision, Embedder, EventCluster, GeminiEventClassifier,
    IncrementalEventClusterer, NewsSignalPipeline, PreFilterResult, cosine_similarity,
    load_pipeline_config,
)
from src.news.pipeline.config import _build_config
from tests import test_news_pipeline as ntp

import backfill_news_v2  # noqa: E402

_SEC = "Exclusive-Nvidia's accounting is a focus of US SEC investigation"
_NOW = ntp._FIXED_NOW


def _at(cos: float) -> list[float]:
    return [cos, math.sqrt(1.0 - cos * cos)]

class _Vec(Embedder):
    name, is_semantic = "vec", True

    def __init__(self, vecs):
        self.v, self.i = [list(x) for x in vecs], 0

    def embed_texts(self, texts):
        out = [list(self.v[min(self.i + n, len(self.v) - 1)]) for n in range(len(texts))]
        self.i += len(texts)
        return out

def _pair(title, when, n, source="Reuters", tier=1):
    url = f"https://news.example.com/i/{n}"
    art = {"guid": f"g{n}", "title": title, "summary": "TSMC NVDA", "source": source,
           "link": url, "published_dt": when}
    return art, PreFilterResult(True, "", title, "TSMC NVDA", url, tier)

def _cl(vecs):
    return IncrementalEventClusterer(load_pipeline_config(), embedder=_Vec(vecs))

def _pipe(transport, vec=None):
    cfg = load_pipeline_config()
    return NewsSignalPipeline(
        cfg, embedder=_Vec([vec or [1.0, 0.0]]),
        classifier=GeminiEventClassifier(cfg, api_key="k", transport=transport),
    )


def _art(title, guid, link, source="Reuters"):
    return ntp._art(title, source=source, guid=guid, link=link)


def test_semantic_chain_expires_from_first_seen():
    clusterer, spans = _cl([[1.0, 0.0]]), {}
    for i in range(25):
        when = _NOW + timedelta(minutes=30 * i)
        dec = clusterer.process_batch([_pair(f"Wire {i:02d} nvidia sec qx{i}", when, i)], now=when)[0]
        spans.setdefault(dec.cluster_id, [when, when])[1] = when
    widths = [end - begin for begin, end in spans.values()]
    assert len(spans) == 3 and max(widths) >= timedelta(hours=3, minutes=30)
    assert all(w <= timedelta(hours=4) for w in widths)


def test_anchor_rejects_drifted_topic_after_merges():
    origin, step, far = [1.0, 0.0], _at(0.82), _at(0.50)
    assert cosine_similarity(step, origin) == pytest.approx(0.82)
    clusterer = _cl([origin, *([step] * 5), far])
    decisions = [
        clusterer.process_batch([_pair(f"Catalyst {i} alpha{i} beta{i}", _NOW + timedelta(minutes=i), i)],
                                now=_NOW + timedelta(minutes=i))[0]
        for i in range(7)
    ]
    assert all(d.is_duplicate for d in decisions[1:6])
    original = next(c for c in clusterer.active if c.mention_count == 6)
    assert cosine_similarity(far, original.anchor) == pytest.approx(0.50)
    assert cosine_similarity(far, original.centroid) >= 0.82
    assert decisions[-1].needs_llm and not decisions[-1].is_duplicate
    assert decisions[-1].cluster_id != original.cluster_id


def test_anchor_round_trip_and_old_sidecar_uses_centroid():
    clusterer = _cl([[1.0, 0.0]])
    clusterer.process_batch([_pair("First nvidia sec wire", _NOW, 1)], now=_NOW)
    saved = clusterer.to_dict()["active"][0]
    assert saved["anchor"]
    again = _cl([[1.0, 0.0]])
    again.load_dict({"version": 1, "active": [saved], "archived": []})
    assert again.active[0].anchor == saved["anchor"]
    far = _at(0.50)
    old = dict(saved, centroid=list(far))
    old.pop("anchor")
    assert EventCluster.from_dict(old).anchor is None
    follower = _cl([far])
    follower.load_dict({"version": 1, "active": [old], "archived": []})
    dec = follower.process_batch([_pair("Later wording zeta", _NOW, 2)], now=_NOW)[0]
    assert dec.is_duplicate and dec.dup_method == "semantic" and dec.cluster_id == old["cluster_id"]


def test_unparsable_first_seen_expires():
    clusterer = _cl([[1.0, 0.0]])
    clusterer.process_batch([_pair("Nvidia sec wire", _NOW, 1)], now=_NOW)
    cluster = clusterer.active[0]
    cluster.first_seen_at, cluster.last_seen_at = "not-a-timestamp", _NOW.isoformat()
    clusterer.expire_windows(_NOW + timedelta(minutes=5))
    assert cluster.cluster_id not in {c.cluster_id for c in clusterer.active + clusterer.archived}


def test_unterminated_json_retries_once_then_classifies():
    calls = []

    def transport(_p):
        calls.append(1)
        return '{"category":' if len(calls) == 1 else ntp._valid_event_json()

    report = _pipe(transport).run([_art(_SEC, "r1", "https://reuters.com/sec-1")], now=_NOW)
    assert len(calls) == 2 and report.l3_succeeded == 1 and report.passed_count == 1


def test_always_bad_json_evicts_cluster_so_later_article_needs_llm():
    calls = []
    pipe = _pipe(lambda _p: calls.append(1) or '{"category":')
    report = pipe.run([_art(_SEC, "b1", "https://reuters.com/sec-1")], now=_NOW)
    assert len(calls) == 2 and report.l3_succeeded == 0 and report.passed_count == 0 and pipe.clusterer.active == []
    pipe.classifier._transport = lambda _p: ntp._valid_event_json()
    later = pipe.run(
        [_art("Fed Chair Warsh hikes rates 25 bps as 10-year Treasury yield tops 5.0%",
              "b2", "https://bloomberg.com/fed-1", source="Bloomberg Markets")],
        now=_NOW + timedelta(minutes=30),
    )
    assert later.l2_duplicate_count == 0 and later.l3_attempted == 1 and later.l3_succeeded == 1


def test_bad_enum_is_not_retried_and_unclassified_cluster_is_evicted():
    calls = []
    pipe = _pipe(lambda _p: calls.append(1) or ntp._valid_event_json(category="NOT_A_CATEGORY"))
    report = pipe.run([_art(_SEC, "e1", "https://reuters.com/sec-1")], now=_NOW)
    assert len(calls) == 1 and report.l3_succeeded == 0 and report.passed_count == 0
    assert pipe.clusterer.active == []


def test_classified_cluster_is_not_evicted_when_reclassification_fails():
    state = {"bad": False, "n": 0}

    def transport(_p):
        state["n"] += 1
        return "{" if state["bad"] else ntp._valid_event_json()

    pipe = _pipe(transport)
    assert pipe.run([_art("美國SEC傳啟動輝達會計審查", "t3", "https://etoday.com/1", source="ETtoday財經雲")], now=_NOW).l3_succeeded == 1
    assert pipe.clusterer.active[0].classified_category == "GOVERNANCE_REGULATORY"
    state.update(bad=True, n=0)
    second = pipe.run([_art(_SEC, "t1", "https://reuters.com/sec-2")], now=_NOW + timedelta(minutes=15))
    assert second.l2_tier_upgraded_count == 1 and state["n"] == 2 and second.l3_succeeded == 0
    assert second.passed_count == 0 and len(pipe.clusterer.active) == 1
    assert pipe.clusterer.active[0].classified_category == "GOVERNANCE_REGULATORY"


def _urlopen(monkeypatch, bodies, sink=None):
    calls = {"n": 0}

    class _Body(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False

    def fake(req, timeout=None):
        calls["n"] += 1
        if sink is not None:
            sink["body"] = json.loads(req.data.decode())
        return _Body(json.dumps(bodies[calls["n"] - 1]).encode())
    monkeypatch.setattr("src.news.pipeline.classifier.urllib.request.urlopen", fake)
    return calls


def _cand(text, finish):
    return {"candidates": [{"finishReason": finish, "content": {"parts": [{"text": text}]}}]}


def _dec():
    return DedupDecision(
        {"guid": "g", "title": _SEC, "source": "Reuters"},
        PreFilterResult(True, "", _SEC, "SEC probe.", "https://news.example.com/sec", 1),
        "cluster_x", False, "", 0.0, 1, 1,
    )


def test_generation_config_uses_temperature_zero_and_minimal_thinking(monkeypatch):
    valid, sink = ntp._valid_event_json(), {}
    _urlopen(monkeypatch, [_cand(valid, "STOP")], sink)
    text, finish = GeminiEventClassifier(load_pipeline_config(), api_key="k")._call_gemini("classify")
    gen = sink["body"]["generationConfig"]
    assert (text, finish) == (valid, "STOP")
    assert gen["temperature"] == 0 and gen["responseMimeType"] == "application/json"
    assert gen["maxOutputTokens"] == 8192 and gen["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert ClassifierConfig().max_output_tokens == _build_config({}, "mem").classifier.max_output_tokens == 8192


def test_max_tokens_finish_retries_once(monkeypatch):
    valid = ntp._valid_event_json()
    calls = _urlopen(monkeypatch, [_cand("{", "MAX_TOKENS"), _cand(valid, "STOP")])
    ev, err = GeminiEventClassifier(load_pipeline_config(), api_key="k").classify(_dec())
    assert calls["n"] == 2 and err == "" and ev is not None


def test_max_tokens_with_bad_enum_is_not_retried():
    calls, bad = [], ntp._valid_event_json(category="NOT_A_CATEGORY")
    clf = GeminiEventClassifier(load_pipeline_config(), api_key="k", transport=lambda _p: calls.append(1) or (bad, "MAX_TOKENS"))
    ev, err = clf.classify(_dec())
    assert len(calls) == 1 and ev is None and err.startswith("invalid category")


def test_dry_run_embedder_override_matches_live_and_summary_names_it():
    resolve = backfill_news_v2.resolve_embedder_override
    assert resolve("", "") == "" and resolve("lexical", "gemini") == "lexical" and resolve("", "gemini") == "gemini"
    text = backfill_news_v2.format_summary(backfill_news_v2.BackfillResult(embedder_used="gemini"), dry_run=True)
    assert "Embedder used" in text and "gemini" in text
