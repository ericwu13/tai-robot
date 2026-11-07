"""新聞訊號 view: the W3 scorer's per-article history, read-only.

Reads the JSONL written by ``scripts/news_bridge/rss_news_log.py`` (newest
lines only, through ``fileio`` so the scorer's append/trim is never
blocked) and answers ``GET /api/news``. Filtering happens here so the page
and the tests share one implementation; the facets (sessions, sources)
come from the unfiltered window so the filter menus never empty out.
"""
from __future__ import annotations

import json
import math

from scripts.dashboard import fileio
from scripts.news_bridge import rss_news_log

WINDOW_LINES = 5000           # newest records considered
WINDOW_BYTES = 4_000_000
DEFAULT_LIMIT = 300
MAX_LIMIT = 1000
_DIRECTIONS = ("bullish", "bearish", "neutral")


class BadQuery(ValueError):
    """A filter parameter the page would never send."""


def _num(value, default=0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _str(value, limit: int = 400) -> str:
    return value[:limit] if isinstance(value, str) else ""


def normalize(raw) -> dict | None:
    """One stored line → the row the page renders, or None if unusable.

    Defence in depth: the file is plain text on disk, so the link is
    re-checked here and nothing but known fields reaches the client.
    """
    if not isinstance(raw, dict):
        return None
    title = _str(raw.get("title"), 300)
    published = _str(raw.get("published_at"), 40)
    scored = _str(raw.get("scored_at"), 40)
    if not title or not (published or scored):
        return None
    direction = _str(raw.get("direction"), 16).lower()
    if direction not in _DIRECTIONS:
        direction = "neutral"
    return {
        "published_at": published or scored,
        "scored_at": scored,
        "session": _str(raw.get("session"), 32),
        "source": _str(raw.get("source"), 80) or "—",
        "title": title,
        "link": rss_news_log.safe_link(raw.get("link")),
        "summary": _str(raw.get("summary"), 400),
        "direction": direction,
        "confidence": round(_num(raw.get("confidence")), 3),
        "score": round(_num(raw.get("score")), 3),
        "reason": _str(raw.get("reason"), 400),
        "run_net": round(_num(raw.get("run_net")), 3),
        "session_net": round(_num(raw.get("session_net")), 3),
    }


def parse_query(query: dict) -> dict:
    """``parse_qs`` dict → validated filters. Raises BadQuery."""
    def one(key):
        return (query.get(key) or [""])[0].strip()

    out = {"session": one("session"), "source": one("source"),
           "min": None, "max": None, "limit": DEFAULT_LIMIT}
    for key in ("min", "max"):
        text = one(key)
        if text:
            try:
                value = float(text)
            except ValueError:
                raise BadQuery(f"{key} must be a number") from None
            if not math.isfinite(value):
                raise BadQuery(f"{key} must be finite")
            out[key] = value
    text = one("limit")
    if text:
        if not text.isdigit():
            raise BadQuery("limit must be a positive integer")
        out["limit"] = max(1, min(MAX_LIMIT, int(text)))
    if len(out["session"]) > 32 or len(out["source"]) > 80:
        raise BadQuery("filter too long")
    return out


def read_items(path: str) -> list[dict]:
    """Newest-first rows from the newest WINDOW_LINES of the log."""
    rows = []
    for line in fileio.tail_lines(path, WINDOW_LINES, WINDOW_BYTES):
        line = line.strip()
        if not line:
            continue
        try:
            row = normalize(json.loads(line))
        except ValueError:
            continue
        if row:
            rows.append(row)
    rows.sort(key=lambda r: (r["published_at"], r["scored_at"]), reverse=True)
    return rows


def news_feed(path: str | None, filters: dict | None = None) -> dict:
    """The ``/api/news`` payload. ``path`` None = no settings to find it."""
    filters = filters or parse_query({})
    if not path:
        return {"available": False, "reason": "no_settings", "items": [],
                "sessions": [], "sources": [], "total": 0, "matched": 0,
                "summary": {"bullish": 0, "bearish": 0, "neutral": 0, "net": 0.0,
                            "net_clipped": 0.0},
                "latest": None}
    items = read_items(path)
    latest = None
    if items:
        newest = max(items, key=lambda r: r["scored_at"])
        latest = {"scored_at": newest["scored_at"], "session": newest["session"],
                  "session_net": newest["session_net"]}
    sessions = sorted({r["session"] for r in items if r["session"]}, reverse=True)
    counts: dict[str, int] = {}
    for r in items:
        counts[r["source"]] = counts.get(r["source"], 0) + 1
    sources = [{"name": k, "count": v}
               for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    def keep(r):
        if filters["session"] and r["session"] != filters["session"]:
            return False
        if filters["source"] and r["source"] != filters["source"]:
            return False
        if filters["min"] is not None and r["score"] < filters["min"]:
            return False
        if filters["max"] is not None and r["score"] > filters["max"]:
            return False
        return True

    matched = [r for r in items if keep(r)]
    return {
        "available": True,
        "reason": "" if items else "empty",
        "items": matched[:filters["limit"]],
        "sessions": sessions,
        "sources": sources,
        "total": len(items),
        "matched": len(matched),
        "summary": {
            "bullish": sum(1 for r in matched if r["direction"] == "bullish"),
            "bearish": sum(1 for r in matched if r["direction"] == "bearish"),
            "neutral": sum(1 for r in matched if r["direction"] == "neutral"),
            "net": round(sum(r["score"] for r in matched), 3),
            # What the vote actually sums: each article clipped to ±0.5
            # (rss_scorer.aggregate_scores), before the per-run EMA decay.
            "net_clipped": round(sum(max(-0.5, min(0.5, r["score"])) for r in matched), 3),
        },
        "latest": latest,
    }
