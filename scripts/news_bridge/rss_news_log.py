"""Per-article history of the W3 RSS scorer (one JSON object per line).

``rss_scorer.py`` keeps only GUIDs and session aggregates in its state
file, and ``rss_scorer.log`` only one line per run — neither says WHICH
articles moved the vote. This file is that record: every article Gemini
scored is appended once (the scorer's GUID dedup guarantees once), with
the run/session context the vote was computed from.

Stdlib only, no side effects on import: the dashboard imports it too.

Record (all strings TPE ISO-8601 unless noted)::

    {"v": 1, "scored_at": "...", "published_at": "...",
     "session": "2026-11-05|NIGHT",           # night session the vote targets
     "source": "Bloomberg", "title": "...", "link": "https://...",
     "summary": "plain text, <= 300 chars",
     "direction": "bullish|bearish|neutral", "confidence": 0.7,
     "score": 0.7,                             # signed: +bull / -bear / 0
     "reason": "...", "run_net": 1.2, "session_net": 1.6}
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
SUMMARY_MAX_CHARS = 300
# ~600 B/record, a few hundred records a day: ~20 MB is weeks of history.
# Past it, the oldest lines are dropped (same self-trimming as the run log).
MAX_BYTES = 20_000_000
KEEP_LINES = 30_000

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


def build_record(article: dict, score: dict, *, scored_at: datetime,
                 session: str, run_net: float, session_net: float) -> dict:
    direction = str(score.get("direction", "neutral")).lower()
    try:
        confidence = round(max(0.0, min(1.0, float(score.get("confidence", 0)))), 3)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
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
    }


def append(path: str, records: list[dict]) -> bool:
    """Append records; trim to the newest KEEP_LINES past MAX_BYTES.

    Never raises — losing a history line must not cost the vote.
    """
    if not records:
        return True
    try:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8", newline="\n") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        if out.stat().st_size > MAX_BYTES:
            with open(out, encoding="utf-8") as f:
                kept = f.readlines()[-KEEP_LINES:]
            tmp = str(out) + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.writelines(kept)
            os.replace(tmp, out)
        return True
    except Exception:  # noqa: BLE001
        return False
