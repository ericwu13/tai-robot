"""Per-article history of the W3 RSS scorer (one JSON object per line).

``rss_scorer.py`` keeps only GUIDs and session aggregates in its state
file, and ``rss_scorer.log`` only one line per run — neither says WHICH
articles moved the vote. This file is that record: every article Gemini
scored is appended once (the scorer's GUID dedup guarantees once), with
the run/session context the vote was computed from.

Files: one per calendar month (TPE) of ``scored_at``, beside the configured
path — ``news_log.jsonl`` -> ``news_log_202611.jsonl``. Append-only, never
trimmed or rewritten, so history keeps growing (~5-20 MB a month) and a
finished month never changes again (readers cache it). A ``news_log.jsonl``
left from before the split is still read as the oldest history, never
written. ``log_files()`` is the one place that knows the layout.

Stdlib only, no side effects on import: the dashboard imports it too.

Record (all strings TPE ISO-8601 unless noted)::

    {"v": 1, "scored_at": "...", "published_at": "...",
     "session": "2026-11-05|NIGHT",           # night session the vote targets
     "source": "Bloomberg", "title": "...", "link": "https://...",
     "summary": "plain text, <= 300 chars",
     "direction": "bullish|bearish|neutral", "confidence": 0.7,
     "score": 0.7,                             # signed: +bull / -bear / 0
     "reason": "...", "run_net": 1.2, "session_net": 1.6,
     "run_scale": 1.0}                         # this article's weight in run_net
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

SCHEMA_VERSION = 1
LOG_DIRNAME = "news"
LOG_FILENAME = "news_log.jsonl"
RAW_DIRNAME = "raw"
RAW_FILENAME = "raw_news.jsonl"
SUMMARY_MAX_CHARS = 300
_MONTH_RE = re.compile(r"_(\d{6})$")

_TZ_TAIPEI = timezone(timedelta(hours=8))
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def default_path(state_file: str) -> str:
    """``<folder of the scorer state file>/news/news_log.jsonl``.

    Derived from the state file so the scorer and the dashboard agree
    without another setting: the default ``data/rss_scorer_state.json``
    gives ``data/news/news_log.jsonl``.
    """
    return os.path.join(os.path.dirname(os.path.abspath(state_file)),
                        LOG_DIRNAME, LOG_FILENAME)


def default_raw_path(news_log_path: str) -> str:
    """``<folder of news_log.jsonl>/raw/raw_news.jsonl``.

    Keeps the complete append-only raw RSS archive (including Layer-1 dropped
    and Layer-2 deduplicated articles) in a separate ``raw/`` subdirectory so
    future re-backfills/audits never lose raw data while the Dashboard's
    ``log_files(news_log_path)`` reads only clean v2 output.
    """
    return os.path.join(os.path.dirname(os.path.abspath(news_log_path)),
                        RAW_DIRNAME, RAW_FILENAME)


def clean_text(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """RSS summaries are HTML fragments: strip tags, unescape, one line."""
    plain = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text or ""))).strip()
    return plain if len(plain) <= limit else plain[:limit - 1].rstrip() + "…"


def safe_link(url) -> str:
    """Only http(s) links survive — anything else (``javascript:``, ``data:``)
    would become a clickable script on the dashboard."""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return ""
    return url if scheme in ("http", "https") else ""


def source_name(entry_source, feed_title, feed_url: str) -> str:
    """Publisher shown on the dashboard. Google News items carry the real
    publisher in ``<source>``; otherwise the feed title, else its host."""
    if isinstance(entry_source, dict) and entry_source.get("title"):
        return clean_text(str(entry_source["title"]), 80)
    if feed_title:
        return clean_text(str(feed_title), 80)
    try:
        return urlparse(feed_url).hostname or ""
    except ValueError:
        return ""


def signed_score(direction: str, confidence) -> float:
    """+confidence bullish, -confidence bearish, 0 neutral (unclipped —
    the per-article ±0.5 clip is an aggregation rule, not the article's score)."""
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        return 0.0
    direction = str(direction or "").lower()
    if direction == "bullish":
        return round(conf, 3)
    if direction == "bearish":
        return round(-conf, 3)
    return 0.0


def _iso(dt) -> str:
    if not isinstance(dt, datetime):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_TZ_TAIPEI).isoformat(timespec="seconds")


NO_SHADOW = object()   # build_record(shadow=...) default: shadow scoring is off


def build_record(article: dict, score: dict, *, scored_at: datetime,
                 session: str, run_net: float, session_net: float,
                 run_scale: float = 1.0,
                 shadow=NO_SHADOW, shadow_run_net: float | None = None,
                 shadow_session_net: float | None = None,
                 v2: dict | None = None) -> dict:
    direction = str(score.get("direction", "neutral")).lower()
    try:
        confidence = round(max(0.0, min(1.0, float(score.get("confidence", 0)))), 3)
    except (TypeError, ValueError):
        confidence = 0.0
    record = {
        "v": SCHEMA_VERSION,
        "scored_at": _iso(scored_at),
        "published_at": _iso(article.get("published_dt")),
        "session": session,
        "source": article.get("source") or "",
        "title": clean_text(article.get("title", ""), 300),
        "link": safe_link(article.get("link", "")),
        "summary": clean_text(article.get("summary", "")),
        "direction": direction,
        "confidence": confidence,
        "score": signed_score(direction, confidence),
        "reason": clean_text(str(score.get("reason", "")), 300),
        "run_net": round(float(run_net), 4),
        "session_net": round(float(session_net), 4),
        # Weight of this article in run_net (rss_scorer.run_scale).
        "run_scale": round(float(run_scale), 4),
    }
    if shadow is not NO_SHADOW:
        # The shadow ("not yet priced in") score; None = that call failed.
        rec = {}
        if isinstance(shadow, dict):
            sd = str(shadow.get("direction", "neutral")).lower()
            try:
                sc = round(max(0.0, min(1.0, float(shadow.get("confidence", 0)))), 3)
            except (TypeError, ValueError):
                sc = 0.0
            rec = {"shadow_score": signed_score(sd, sc),
                   "shadow_reason": clean_text(str(shadow.get("reason", "")), 300)}
        else:
            rec = {"shadow_score": None, "shadow_reason": ""}
        rec["shadow_run_net"] = None if shadow_run_net is None else round(float(shadow_run_net), 4)
        rec["shadow_session_net"] = (None if shadow_session_net is None
                                     else round(float(shadow_session_net), 4))
        record.update(rec)
    if isinstance(v2, dict) and v2:
        record["v2"] = v2
    return record


def build_raw_record(
    article: dict,
    *,
    scored_at: datetime,
    session: str,
    pipeline_stage: str,
    source_tier: int = 3,
    drop_reason: str = "",
    cluster_id: str = "",
    dup_method: str = "",
    similarity: float | None = None,
    representative_title: str = "",
    v2_action: str = "",
    v2_category: str = "",
    v2_direction: str = "",
    v2_materiality: int = 0,
) -> dict:
    """Build one append-only raw RSS archive line (replay/backfill compatible).

    Carries the standard ``scored_at`` / ``published_at`` / ``title`` / ``link``
    / ``source`` / ``summary`` fields so ``replay_news_pipeline.py`` and
    ``backfill_news_v2.py`` can ingest ``raw/raw_news.jsonl`` directly without
    any format conversion.
    """
    rec: dict = {
        "v": SCHEMA_VERSION,
        "scored_at": _iso(scored_at),
        "published_at": _iso(article.get("published_dt")),
        "session": session,
        "guid": str(article.get("guid") or "")[:240],
        "source": article.get("source") or "",
        "title": clean_text(article.get("title", ""), 300),
        "link": safe_link(article.get("link", "")),
        "summary": clean_text(article.get("summary", "")),
        "source_tier": int(source_tier),
        "pipeline_stage": str(pipeline_stage),
    }
    if drop_reason:
        rec["drop_reason"] = str(drop_reason)[:120]
    if cluster_id:
        rec["cluster_id"] = str(cluster_id)[:80]
    if dup_method:
        rec["dup_method"] = str(dup_method)[:32]
    if isinstance(similarity, (int, float)) and not isinstance(similarity, bool):
        rec["similarity"] = round(float(similarity), 4)
    if representative_title:
        rec["representative_title"] = clean_text(str(representative_title), 300)
    if v2_action:
        rec["v2_action"] = str(v2_action)
    if v2_category:
        rec["v2_category"] = str(v2_category)
    if v2_direction:
        rec["v2_direction"] = str(v2_direction)
    if v2_materiality:
        rec["v2_materiality"] = int(v2_materiality)
    if "_v1_direction" in article:
        rec["v1_direction"] = str(article.get("_v1_direction") or "neutral")
        rec["v1_score"] = round(float(article.get("_v1_score") or 0.0), 3)
    return rec


def month_path(path: str, month: str) -> str:
    """``.../news_log.jsonl`` + ``202611`` -> ``.../news_log_202611.jsonl``."""
    p = Path(path)
    return str(p.with_name(f"{p.stem}_{month}{p.suffix}"))


def log_files(path: str) -> list[str]:
    """Existing history files for ``path``, oldest first: the pre-split
    single file (if any), then the monthly files in month order."""
    p = Path(path)
    out = [str(p)] if p.is_file() else []
    try:
        months = []
        for f in p.parent.iterdir():
            m = _MONTH_RE.search(f.stem) if f.suffix == p.suffix else None
            if m and f.stem == f"{p.stem}_{m.group(1)}" and f.is_file():
                months.append((m.group(1), str(f)))
    except OSError:
        months = []
    return out + [f for _, f in sorted(months)]


def _month(record: dict) -> str:
    stamp = str(record.get("scored_at") or "")
    if len(stamp) >= 7 and stamp[4] == "-":
        return stamp[:4] + stamp[5:7]
    return datetime.now(_TZ_TAIPEI).strftime("%Y%m")


def append(path: str, records: list[dict]) -> bool:
    """Append records to their month's file (no trimming, no rewrite).

    Never raises — losing a history line must not cost the vote.
    """
    if not records:
        return True
    try:
        by_month: dict[str, list[dict]] = {}
        for rec in records:
            by_month.setdefault(_month(rec), []).append(rec)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        for month, recs in by_month.items():
            with open(month_path(path, month), "a", encoding="utf-8", newline="\n") as f:
                for rec in recs:
                    f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False
