"""W3 RSS+Gemini news scorer — sentiment-weighted regime votes.

Standalone bridge script (no n8n dependency).  Fetches public RSS feeds,
filters to articles from the last 4 hours, scores each headline+summary
via Gemini (see GEMINI_MODEL), aggregates a net sentiment score into an
EMA (exponential moving average) with a 2-hour half-life, and when the
EMA total exceeds SESSION_VOTE_THRESHOLD writes a ``regime_vote.json``
for the regime state machine.  Discord posts only on state changes
by default (``news.rss_discord``): scoring starts failing / recovers, or
optionally a vote is written.  The per-run embed is ``rss_discord: all``.
Scoring is skipped Sat 05:00 - Mon 05:00 TPE (any vote would target a
night session that never happens).  From Mon 05:00 scoring resumes like
any other weekday morning — the vote targets Monday night (opens 15:00).

Deduplicates via a persistent state file (GUID-based) so articles are
scored exactly once across restarts.

Usage:
    # one-shot check
    python rss_scorer.py --settings settings.yaml --once

    # continuous loop (default 30 min interval from settings)
    python rss_scorer.py --settings settings.yaml --loop

    # force a vote write (pipeline test)
    python rss_scorer.py --settings settings.yaml --force-fire bullish

Gemini API key is read from ``ai.google_api_key``.  Discord webhook is
read from env var ``NEWS_DISCORD_WEBHOOK`` or the ``--discord-webhook``
CLI flag (same as W2).  What gets posted is ``news.rss_discord``
(off | errors | changes | all, default errors — see DISCORD_MODES); the
dashboard's 新聞訊號 view replaces the per-run embed.

Settings block (in settings.yaml under ``news``)::

    news:
      rss_feeds:                 # empty = DEFAULT_FEEDS
      rss_interval_minutes: 30
      rss_state_file: data/rss_scorer_state.json
      rss_news_log: ""   # per-article JSONL history; empty = <state dir>/news/news_log.jsonl
      rss_discord: errors  # off | errors | changes | all

Every article Gemini scores is appended to that history (``rss_news_log.py``)
so the dashboard's 新聞訊號 view can show what drove the vote.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import rss_news_log  # noqa: E402

_TZ_TAIPEI = timezone(timedelta(hours=8))

VOTE_THRESHOLD = 0.8          # per-run scale (kept as net_score_to_vote default)
SESSION_VOTE_THRESHOLD: float = 1.5  # session EMA net required to fire a vote

# EMA half-life for session score — empirically, news attention decays within 1-2h
# 0.84 ≈ exp(-ln(2) * 0.5 / 2.0) = 2-hour half-life at 30-min polling cadence
EMA_HALF_LIFE_HOURS: float = 2.0
EMA_DECAY: float = math.exp(-math.log(2) * 0.5 / EMA_HALF_LIFE_HOURS)

# Each article's signed score is clipped to ±ARTICLE_CLIP before summing.
# The dashboard (scripts/dashboard/news_feed.py, news_pulse.py) imports
# these so its numbers stay identical to the ones that fire the vote.
ARTICLE_CLIP: float = 0.5
EROSION_POINTS: float = 1.5   # session net this far back from its peak = "Eroding"

ARTICLE_MAX_AGE_HOURS = 4
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

LOG_NAME = "rss_scorer.log"
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_LINES = 2000

_UA = {"User-Agent": "Mozilla/5.0 (tai-robot news bridge)", "Connection": "close"}

DEFAULT_FEEDS = [
    # ── Global ──────────────────────────────────────────────────────────
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://feeds.marketwatch.com/marketwatch/topstories",
    "https://www.investing.com/rss/news.rss",
    # ── Taiwan / Asia ───────────────────────────────────────────────────
    # Yahoo US rssindex 404s and TechNews is dead. MoneyDJ stays out.
    "https://tw.stock.yahoo.com/rss?category=tw-market",
    "https://news.cnyes.com/rss/v1/news/category/tw_stock",
    "https://www.taifex.com.tw/cht/11/RSS1",
    "https://news.google.com/rss/topics/CAAqJQgKIh9DQkFTRVFvSUwyMHZNRFptTXpJU0JYcG9MVlJYS0FBUAE?hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    "https://news.google.com/rss/search?q=TSMC+OR+%E5%8F%B0%E7%A9%8D%E9%9B%BB&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    "https://news.google.com/rss/search?q=%E5%8F%B0%E8%82%A1+OR+TAIEX+OR+%E5%8A%A0%E6%AC%8A%E6%8C%87%E6%95%B8&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    "https://news.google.com/rss/search?q=%E5%A4%A9%E4%B8%8B%E9%9B%9C%E8%AA%8C+%E5%8F%B0%E7%81%A3%E7%B6%93%E6%BF%9F&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    "https://news.google.com/rss/search?q=%E5%95%86%E6%A5%AD%E9%80%B1%E5%88%8A+%E5%8F%B0%E8%82%A1&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    "https://www.scmp.com/rss/5/feed",
]

SCORING_PROMPT = """\
You are a financial sentiment analyst.  Given the headline and summary of a
news article, classify its likely SHORT-TERM impact on global equity markets
and Taiwan equity futures (TAIEX / 台股加權指數) in particular (next 1-4 hours).

Articles may be in English or Traditional Chinese (繁體中文) — analyse both equally.

Respond with ONLY a JSON object — no markdown, no explanation:
{{"direction": "bullish"|"bearish"|"neutral", "confidence": <float 0.0-1.0>, "reason": "<one sentence>"}}

Rules:
- "confidence" reflects how strongly the article moves markets, not how sure you are of the classification.
- Routine earnings within expectations → neutral with low confidence.
- Geopolitical escalation, surprise rate moves, major tech earnings beats/misses → high confidence.
- TSMC (台積電) earnings, guidance, or capex changes are high-impact for TAIEX (~30% weight).
- 外資 (foreign institutional) large buy/sell flows, Taiwan central bank policy, and cross-strait tensions are Taiwan-specific high-impact signals.
- If unsure, default to neutral with confidence 0.2.

Headline: {headline}
Summary: {summary}
"""

# Appended only for items tagged as the TAIFEX exchange feed. The base
# prompt above is what every other source sees; do not fold this into it.
TAIFEX_ADMIN_RULE = (
    "\nAdditional rule for this item only (its source is the TAIFEX exchange feed): "
    "exchange administrative notices (settlement prices, calendars, routine circulars) "
    "score neutral at low confidence. Market-wide margin hikes or trading halts are "
    "scored by their market impact.\n"
)
FEED_TIMEOUT_S = 20


def scoring_prompt(headline: str, summary: str, source: str = "") -> str:
    """Prompt for one article. The TAIFEX rule is added only for that feed."""
    prompt = SCORING_PROMPT.format(headline=headline, summary=(summary or "")[:500])
    if str(source or "").strip().lower() == "taifex":
        return prompt + TAIFEX_ADMIN_RULE
    return prompt


class FeedFetchError(Exception):
    """One feed could not be fetched or parsed. ``kind`` is network, timeout, http, or bozo."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}")


# ── Settings loading ─────────────────────────────────────────────────────

def load_settings(path: str) -> dict:
    """Load settings.yaml and return the full dict."""
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_news_config(settings: dict, discord_webhook: str | None = None) -> dict:
    """Extract the news config block with defaults."""
    news = settings.get("news", {}) or {}
    ai = settings.get("ai", {}) or {}
    return {
        "feeds": news.get("rss_feeds") or DEFAULT_FEEDS,
        "interval_minutes": int(news.get("rss_interval_minutes", 30)),
        "state_file": news.get("rss_state_file", "data/rss_scorer_state.json"),
        "news_log_file": news.get("rss_news_log") or "",
        "gemini_api_key": ai.get("google_api_key", ""),
        "discord_webhook_url": discord_webhook or news.get("discord_webhook") or "",
        "discord_mode": discord_mode(news.get("rss_discord")),
    }


# What W3 posts to Discord.  The dashboard's 新聞訊號 view shows every run,
# so the routine per-run embed is opt-in ("all").  The webhook itself is
# shared with W2/W4 and stays configured either way.
#   off      — never post
#   errors   — Gemini scoring starts failing, and once when it recovers
#   changes  — errors + a vote written (first time per session/direction)
#   all      — legacy: an embed for every run that scored articles, plus
#              the weekend-pause notice
DISCORD_MODES = ("off", "errors", "changes", "all")
DEFAULT_DISCORD_MODE = "errors"


def discord_mode(value) -> str:
    """``news.rss_discord`` → one of DISCORD_MODES (unknown → default).

    YAML reads a bare ``off`` as boolean False, so False means "off".
    """
    if value is False:
        return "off"
    text = str(value or "").strip().lower()
    if not text:
        return DEFAULT_DISCORD_MODE
    if text not in DISCORD_MODES:
        print(f"  news.rss_discord={value!r} unknown — using {DEFAULT_DISCORD_MODE!r}")
        return DEFAULT_DISCORD_MODE
    return text


# ── RSS fetching ─────────────────────────────────────────────────────────

def _feed_tag(url: str) -> str:
    """Source marker for prompt rules. Only the TAIFEX feed is tagged."""
    return "taifex" if "taifex.com" in str(url).lower() else ""


def _entry_guid(entry) -> str:
    """GUID for dedup. An entry with no id uses ``link|title``.

    TAIFEX RSS1 has no ``<guid>`` and several notices share a link, so a
    link-only id drops every later settlement or margin notice as already
    seen. Investing.com is also id-less and is re-scored once.
    """
    raw = entry.get("id")
    if isinstance(raw, str):
        raw = raw.strip()
    else:
        raw = ""
    if raw:
        return raw
    link = str(entry.get("link") or "").strip()
    title = str(entry.get("title") or "").strip()
    if link or title:
        return f"{link}|{title}"
    return ""


def _is_timeout(exc: BaseException | None) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return "timed out" in str(exc or "").lower()


def _download_feed(url: str) -> bytes:
    """GET one feed. Raises FeedFetchError on transport errors and HTTP >= 400."""
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=FEED_TIMEOUT_S) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise FeedFetchError("http", f"HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        kind = "timeout" if _is_timeout(reason) else "network"
        text = str(reason if reason is not None else exc)
        raise FeedFetchError(kind, text) from exc
    except TimeoutError as exc:
        raise FeedFetchError("timeout", str(exc)) from exc
    except OSError as exc:
        kind = "timeout" if _is_timeout(exc) else "network"
        raise FeedFetchError(kind, str(exc)) from exc
    if isinstance(status, int) and status >= 400:
        raise FeedFetchError("http", f"HTTP {status}")
    if isinstance(body, str):
        body = body.encode("utf-8", errors="replace")
    return body or b""


def _parse_feed_bytes(url: str, body: bytes) -> list[dict]:
    """Parse feed bytes.

    Unrecognised input is a failure: feedparser left ``version`` empty
    (an HTML page), or set bozo and produced no entries.  A recognised
    feed with zero entries, or entries that are all too old, is quiet.
    """
    import feedparser
    feed = feedparser.parse(body)
    entries = list(getattr(feed, "entries", None) or [])
    version = str(getattr(feed, "version", "") or "")
    if not entries and (not version or getattr(feed, "bozo", 0)):
        exc = getattr(feed, "bozo_exception", None)
        detail = str(exc) if exc else ("unrecognised feed" if not version else "unparseable feed")
        raise FeedFetchError("bozo", detail)
    feed_title = (getattr(feed, "feed", None) or {}).get("title", "")
    tag = _feed_tag(url)
    articles = []
    for entry in entries:
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub is None:
            continue
        import calendar
        pub_dt = datetime.fromtimestamp(calendar.timegm(pub), tz=timezone.utc)
        articles.append({
            "guid": _entry_guid(entry),
            "title": entry.get("title", ""),
            "summary": entry.get("summary", ""),
            "published_dt": pub_dt,
            "link": entry.get("link", ""),
            "source": rss_news_log.source_name(entry.get("source"), feed_title, url),
            "feed_tag": tag,
        })
    return articles


def fetch_feed(url: str) -> list[dict]:
    """Fetch and parse one RSS feed.  Returns list of article dicts.

    Each dict has keys: guid, title, summary, published_dt (datetime, UTC),
    link, source (publisher name for the news history), feed_tag
    (``"taifex"`` only for the TAIFEX feed).

    Raises FeedFetchError on DNS, connection, timeout, HTTP >= 400, a
    bozo parse with zero entries, or a body feedparser does not recognise
    (empty ``version`` and no entries — an HTML page). A recognised feed
    with zero entries is an empty list, not an error.
    """
    return _parse_feed_bytes(url, _download_feed(url))


def _fetch_status_name(ok: int, failed: int) -> str:
    if failed and ok == 0:
        return "failed"
    if failed:
        return "degraded"
    return "ok"


def fetch_all_feeds(feed_urls: list[str], max_age_hours: float = ARTICLE_MAX_AGE_HOURS,
                    stats: dict | None = None) -> list[dict]:
    """Fetch all feeds, filter to recent articles, return combined list.

    Signature and return value are unchanged so callers that patch this
    function keep working. Pass ``stats`` to receive ``feeds_ok``,
    ``feeds_failed``, ``errors`` (at most 5) and ``status``
    (``ok`` / ``degraded`` / ``failed``). A 2xx feed with nothing recent
    counts as ok.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    all_articles = []
    ok = failed = 0
    errors: list[dict] = []
    for url in feed_urls:
        try:
            articles = fetch_feed(url)
        except FeedFetchError as exc:
            failed += 1
            if len(errors) < 5:
                errors.append({"url": url, "kind": exc.kind, "message": str(exc.message)[:200]})
            print(f"  [{url[:60]}...] fetch failed: {exc.kind}: {exc.message}")
            continue
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if len(errors) < 5:
                errors.append({
                    "url": url, "kind": "error",
                    "message": f"{type(exc).__name__}: {exc}"[:200],
                })
            print(f"  [{url[:60]}...] fetch failed: {type(exc).__name__}: {exc}")
            continue
        recent = [a for a in articles if a["published_dt"] >= cutoff]
        ok += 1
        print(f"  [{url[:60]}...] {len(recent)}/{len(articles)} recent")
        all_articles.extend(recent)
    if isinstance(stats, dict):
        stats["feeds_ok"] = ok
        stats["feeds_failed"] = failed
        stats["errors"] = errors
        stats["status"] = _fetch_status_name(ok, failed)
    return all_articles


# ── Deduplication ────────────────────────────────────────────────────────

def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"seen_guids": [], "last_check": None}


def save_state(path: str, state: dict) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(out) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, out)


def dedup_articles(articles: list[dict], state: dict) -> list[dict]:
    """Return only articles whose GUID hasn't been seen before."""
    seen = set(state.get("seen_guids", []))
    new = [a for a in articles if a["guid"] not in seen]
    return new


def mark_seen(state: dict, guids: list[str]) -> dict:
    """Add GUIDs to the seen set.  Keep last 5000 to bound state size."""
    seen = list(state.get("seen_guids", []))
    seen.extend(guids)
    state["seen_guids"] = seen[-5000:]
    return state


# ── Gemini scoring ───────────────────────────────────────────────────────

import re

_DIR_RE = re.compile(r'"direction"\s*:\s*"(bullish|bearish|neutral)"')
_CONF_RE = re.compile(r'"confidence"\s*:\s*([\d.]+)')


def _parse_partial(text: str) -> dict | None:
    """Extract direction+confidence from truncated Gemini JSON."""
    dm = _DIR_RE.search(text)
    cm = _CONF_RE.search(text)
    if dm and cm:
        return {"direction": dm.group(1), "confidence": float(cm.group(1)),
                "reason": "(truncated)"}
    return None


class GeminiAPIError(Exception):
    """Gemini API call failed (HTTP error, connection error, timeout)."""
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"HTTP {status}: {message}" if status else message)


def score_article(headline: str, summary: str, api_key: str,
                  source: str = "") -> dict | None:
    """Call Gemini to score one article.  Returns parsed JSON or None.

    ``source`` is the feed tag (``"taifex"`` or ``""``). Only a TAIFEX item
    gets the administrative-notice rule; every other source uses
    SCORING_PROMPT unchanged.
    """
    prompt = scoring_prompt(headline, summary, source)
    url = GEMINI_API_URL.format(model=GEMINI_MODEL, key=api_key)
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 1024},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=body,
            headers={**_UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        parts = data["candidates"][0]["content"]["parts"]
        text = ""
        for part in parts:
            if "thought" not in part and "text" in part:
                text = part["text"]
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except json.JSONDecodeError:
        return _parse_partial(text)
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status == 429:
            msg = "credits depleted / rate limited"
        elif status >= 500:
            msg = f"server error ({exc.reason})"
        else:
            msg = str(exc.reason)
        raise GeminiAPIError(status, msg) from exc
    except OSError as exc:
        raise GeminiAPIError(0, f"connection error: {exc}") from exc
    except Exception as e:  # noqa: BLE001
        print(f"    gemini scoring failed: {type(e).__name__}: {e}")
        return None


def score_articles(articles: list[dict], api_key: str) -> tuple[list[dict], list[str], str | None]:
    """Score each article via Gemini.

    Returns (scored_results, scored_guids, api_error):
      scored_results: list of {article, score} for successfully scored articles
      scored_guids: GUIDs where the API call succeeded (safe to mark as seen)
      api_error: error description if Gemini API failed, None otherwise
    """
    results = []
    scored_guids = []
    api_error = None
    for art in articles:
        title = art.get("title") or ""
        summary = art.get("summary") or ""
        # TAIFEX repeats the title as the description. An empty summary
        # keeps the model on the headline instead of scoring the echo.
        if summary.strip() == title.strip():
            summary = ""
        try:
            score = score_article(title, summary, api_key,
                                  source=str(art.get("feed_tag") or ""))
        except GeminiAPIError as exc:
            api_error = str(exc)
            print(f"    [API ERROR] {exc} — aborting remaining articles")
            break
        scored_guids.append(art["guid"])
        if score and "direction" in score and "confidence" in score:
            score["direction"] = score["direction"].lower()
            results.append({"article": art, "score": score})
            direction = score["direction"]
            confidence = score["confidence"]
            print(f"    [{direction} {confidence:.2f}] {art['title'][:70]}")
        else:
            print(f"    [SKIP] {art['title'][:70]}")
    return results, scored_guids, api_error


# ── Aggregation ──────────────────────────────────────────────────────────

def aggregate_scores(scored: list[dict]) -> float:
    """Net score: sum of clipped (confidence × direction_sign).

    Each article's signed score is clipped to ±0.5 to prevent a single
    high-confidence article from dominating the EMA.
    bullish = +1, bearish = -1, neutral = 0.
    """
    total = 0.0
    for item in scored:
        s = item["score"]
        direction = s.get("direction", "neutral").lower()
        confidence = float(s.get("confidence", 0))
        if direction == "bullish":
            raw = confidence
        elif direction == "bearish":
            raw = -confidence
        else:
            continue
        total += max(-ARTICLE_CLIP, min(ARTICLE_CLIP, raw))
    return total


def net_score_to_vote(net_score: float, threshold: float = VOTE_THRESHOLD) -> str | None:
    """Map aggregate score to a regime vote direction, or None."""
    if net_score >= threshold:
        return "trending-up"
    if net_score <= -threshold:
        return "trending-down"
    return None


# ── Session key (mirrors crossmarket_monitor.py) ────────────────────────

def night_session_key(now: datetime) -> str:
    """Target the NIGHT session this vote applies to.

    Boundary is 05:00, not 15:00 — votes during 05:00-14:59 target
    tonight (today|NIGHT), not last night (already classified at ~04:58).
    """
    if now.hour >= 5:
        return f"{now.strftime('%Y-%m-%d')}|NIGHT"
    return f"{(now - timedelta(days=1)).strftime('%Y-%m-%d')}|NIGHT"


def in_weekend_pause(now: datetime) -> bool:
    """True from Sat 05:00 to Mon 05:00 TPE.

    In that window ``night_session_key`` targets Sat|NIGHT or Sun|NIGHT —
    sessions that never happen — so any vote written would expire unread.
    From Mon 05:00 the key targets Monday night (opens 15:00), a real
    session classified Tue ~04:58, making Monday morning identical to
    every other weekday morning (which are never paused).  Scoring (and
    its Gemini cost) is skipped during the pause; with
    ``rss_discord: all`` a notice embed still posts once.
    """
    wd = now.weekday()  # Mon=0 .. Sun=6
    if wd == 5:  # Saturday: Friday's night session ends 05:00
        return now.hour >= 5
    if wd == 6:  # Sunday
        return True
    if wd == 0:  # Monday 00:00-04:59 targets Sun|NIGHT (nonexistent)
        return now.hour < 5
    return False


# ── Discord ──────────────────────────────────────────────────────────────

TOP_ARTICLES_SHOWN = 10


def post_discord_embed(webhook: str | None, direction: str | None, net_score: float,
                       scored: list[dict], session_net: float | None = None,
                       session_peak: float = 0.0, recent_runs: list[float] | None = None,
                       note: str | None = None,
                       api_error: str | None = None, total_fetched: int = 0) -> bool:
    """Post one embed. True only when Discord accepted it, so callers can
    re-try a transition notice (error / recovery) on the next run."""
    if not webhook:
        return False
    if api_error:
        color = 0xFFA500
    elif direction == "trending-up":
        color = 0x00AA00
    elif direction == "trending-down":
        color = 0xCC0000
    else:
        color = 0x888888
    by_impact = sorted(scored, key=lambda x: abs(float(x["score"].get("confidence", 0))), reverse=True)
    top_articles = by_impact[:TOP_ARTICLES_SHOWN]
    desc_lines = []
    for item in top_articles:
        s = item["score"]
        desc_lines.append(
            f"**{s['direction']}** ({s['confidence']:.1f}) — {item['article']['title'][:80]}")
    if len(scored) > TOP_ARTICLES_SHOWN:
        desc_lines.append(f"*…and {len(scored) - TOP_ARTICLES_SHOWN} more*")
    if note:
        desc_lines.append(f"*{note}*")

    bull = sum(float(x["score"]["confidence"]) for x in scored if x["score"].get("direction", "").lower() == "bullish")
    bear = sum(float(x["score"]["confidence"]) for x in scored if x["score"].get("direction", "").lower() == "bearish")

    fields = [{"name": "Run Net", "value": f"{net_score:+.2f}", "inline": True}]
    if session_net is not None:
        fields.append({"name": "Session Net",
                       "value": f"{session_net:+.2f} / ±{SESSION_VOTE_THRESHOLD:.0f}",
                       "inline": True})
    fields.append({"name": "Bullish / Bearish", "value": f"+{bull:.1f} / -{bear:.1f}", "inline": True})
    if api_error:
        fields.append({"name": "⚠️ API Error", "value": api_error, "inline": False})
        fields.append({"name": "Articles Fetched", "value": str(total_fetched), "inline": True})
        if scored:
            fields.append({"name": "Scored (partial)", "value": str(len(scored)), "inline": True})
    else:
        fields.append({"name": "Articles Scored", "value": str(len(scored)), "inline": True})

    if session_peak > 0 and session_net is not None and session_net < session_peak - EROSION_POINTS:
        erosion = session_net - session_peak
        fields.append({"name": "Drawdown",
                       "value": f"⚠️ Eroding: peak +{session_peak:.1f} → now +{session_net:.1f} ({erosion:.1f})",
                       "inline": False})
    elif session_peak < 0 and session_net is not None and session_net > session_peak + EROSION_POINTS:
        erosion = session_net - session_peak
        fields.append({"name": "Drawdown",
                       "value": f"⚠️ Eroding: peak {session_peak:.1f} → now {session_net:.1f} (+{erosion:.1f})",
                       "inline": False})

    if recent_runs:
        recent_net = sum(recent_runs)
        if recent_net > 0.5:
            emoji = "\U0001f7e2"
        elif recent_net < -0.5:
            emoji = "\U0001f534"
        else:
            emoji = "➡️"
        fields.append({"name": "Momentum",
                       "value": f"Recent ({len(recent_runs)} runs): {recent_net:+.2f} {emoji}",
                       "inline": True})

    embed_title = ("📰 RSS Sentiment: ⚠️ Scoring Error" if api_error
                   else f"📰 RSS Sentiment Vote: {direction or 'none'}")
    embed_desc = "\n".join(desc_lines) or (
        "Scoring failed — see error below." if api_error else "No articles scored.")
    embed = {
        "title": embed_title,
        "description": embed_desc,
        "color": color,
        "fields": fields,
        "footer": {"text": "W3 RSS Scorer"},
    }
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    try:
        req = urllib.request.Request(
            webhook, data=body,
            headers={**_UA, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        print("  discord embed sent")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  discord post failed: {type(e).__name__}: {e}")
        return False


# ── Liveness logging (matches W2 pattern) ────────────────────────────────

def append_log(path: str | os.PathLike, line: str) -> None:
    try:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if out.stat().st_size > LOG_MAX_BYTES:
            with open(out, encoding="utf-8") as f:
                kept = f.readlines()[-LOG_KEEP_LINES:]
            tmp = str(out) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(kept)
            os.replace(tmp, out)
    except Exception:  # noqa: BLE001
        pass


# ── Isolated import (avoids triggering news/__init__.py dep chain) ───

def _get_write_regime_vote():
    """Import write_regime_vote directly from the module file."""
    import importlib.util
    mod_path = Path(__file__).resolve().parents[2] / "src" / "news" / "regime_vote.py"
    spec = importlib.util.spec_from_file_location("regime_vote", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.write_regime_vote


# ── Main logic ───────────────────────────────────────────────────────────

def _session_net(state: dict, session_key: str) -> float:
    """Current session-cumulative net score (0.0 if the session rolled over)."""
    if state.get("session_key") != session_key:
        state["session_peak"] = 0.0
        state["recent_runs"] = []
        return 0.0
    return float(state.get("session_net", 0.0))


def record_scoring_result(state: dict, now: datetime, api_error: str | None,
                          skipped: int = 0) -> None:
    """Persist the outcome of a scoring attempt in the state file.

    ``last_check`` is stamped on every pass, including ones where Gemini
    failed, so it alone cannot tell "alive and scoring" from "alive but
    every call failing".  These fields can: ``error_streak`` > 0 means
    the most recent scoring attempt failed (read by src.news.vote_status
    for the dashboard, the GUI regime tab and the bridge monitor).
    """
    stamp = now.isoformat(timespec="seconds")
    state["last_skipped"] = int(skipped)
    if api_error:
        state["last_error"] = str(api_error)[:300]
        state["last_error_at"] = stamp
        state["error_streak"] = int(state.get("error_streak") or 0) + 1
    else:
        state["error_streak"] = 0
        state["last_success_at"] = stamp


def unusable_scores_error(scored: list[dict], scored_guids: list[str]) -> str | None:
    """A failure description when Gemini answered every call but no answer
    was usable (all [SKIP]), else None.

    That is not a GeminiAPIError (HTTP 200, the body just lacks a score —
    a changed response shape, a safety block), so score_articles reports
    no api_error; left alone it would reset error_streak and read healthy
    while the vote silently stops.
    """
    if scored_guids and not scored:
        return f"no usable score in {len(scored_guids)} Gemini response(s)"
    return None


def notify_transitions(state: dict, mode: str, post, *, api_error: str | None,
                       vote_key: str = "") -> None:
    """Discord for the non-``all`` modes: state changes only.

    *post(note)* sends this run's embed and returns True when Discord
    accepted it; a flag is only set on success, so a failed send is
    retried on the next scoring run instead of being lost.
    """
    if api_error:
        if not state.get("error_notified"):
            streak = int(state.get("error_streak") or 1)
            if post(f"Gemini scoring failing ({streak} run(s)) — "
                    "one more message when it recovers"):
                state["error_notified"] = True
    elif state.get("error_notified"):
        if post("✅ Gemini scoring recovered"):
            state["error_notified"] = False
    if mode == "changes" and vote_key and state.get("vote_notified") != vote_key:
        if post("Vote written — first crossing this session"):
            state["vote_notified"] = vote_key


def _apply_fetch_stats(state: dict, stats: dict, now: datetime) -> None:
    """Persist the fetch outcome. Does not touch the Gemini error_streak."""
    status = stats.get("status")
    if status not in ("ok", "degraded", "failed"):
        return
    state["fetch_status"] = status
    state["feeds_ok"] = int(stats.get("feeds_ok") or 0)
    state["feeds_failed"] = int(stats.get("feeds_failed") or 0)
    state["last_feed_errors"] = list(stats.get("errors") or [])[:5]
    if status == "failed":
        state["fetch_error_streak"] = int(state.get("fetch_error_streak") or 0) + 1
    else:
        state["fetch_error_streak"] = 0
        state["last_fetch_ok_at"] = now.isoformat(timespec="seconds")


def _feed_error_brief(errors) -> str:
    parts = []
    for item in errors or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind") or "error"
        message = item.get("message") or ""
        parts.append(f"{kind}: {message}" if message else str(kind))
    return "; ".join(parts)


def _notify_fetch_failure(cfg: dict, state: dict, stats: dict) -> None:
    """Discord errors/changes/all: one notice, worded as a fetch failure."""
    mode = cfg.get("discord_mode", DEFAULT_DISCORD_MODE)
    if mode == "off" or state.get("fetch_fail_notified"):
        return
    n_fail = int(stats.get("feeds_failed") or 0)
    note = f"Fetch failed — all {n_fail} feeds failed: {_feed_error_brief(stats.get('errors'))}"
    if post_discord_embed(cfg.get("discord_webhook_url") or "", None, 0.0, [], note=note):
        state["fetch_fail_notified"] = True


def _notify_fetch_recovery(cfg: dict, state: dict) -> None:
    mode = cfg.get("discord_mode", DEFAULT_DISCORD_MODE)
    if mode == "off" or not state.get("fetch_fail_notified"):
        return
    if post_discord_embed(cfg.get("discord_webhook_url") or "", None, 0.0, [],
                          note="✅ RSS fetch recovered"):
        state["fetch_fail_notified"] = False


def check_once(cfg: dict, state: dict, vote_out: str,
               now: datetime | None = None) -> dict:
    """One RSS scoring pass.  Returns updated state.

    The vote fires on the SESSION-CUMULATIVE net score, not the per-run
    net — one noisy 30-min window can't set the standing vote, but a
    genuinely strong news day crosses the threshold within a run or two.
    """
    now = now or datetime.now(_TZ_TAIPEI)
    print(f"[{now:%Y-%m-%d %H:%M:%S}] RSS scorer checking {len(cfg['feeds'])} feeds")

    mode = cfg.get("discord_mode", DEFAULT_DISCORD_MODE)
    if in_weekend_pause(now):
        print("  weekend pause (Sat 05:00 - Mon 05:00 TPE) — scoring skipped")
        if mode == "all" and not state.get("weekend_pause_notified"):
            post_discord_embed(cfg["discord_webhook_url"], None, 0.0, [],
                               note="Weekend — scoring paused (no night session to vote on)")
        state["weekend_pause_notified"] = True
        state["last_check"] = now.isoformat(timespec="seconds")
        # A pause does not fetch. Leaving Friday's all-feeds-failed state
        # in place would keep the P2 up until Monday's first real pass.
        if state.get("fetch_status") in ("failed", "degraded"):
            state["fetch_status"] = "ok"
            state["fetch_error_streak"] = 0
            state["feeds_failed"] = 0
            state["last_feed_errors"] = []
        return state
    state["weekend_pause_notified"] = False

    fetch_stats: dict = {}
    articles = fetch_all_feeds(cfg["feeds"], stats=fetch_stats)
    if fetch_stats.get("status"):
        _apply_fetch_stats(state, fetch_stats, now)
    if fetch_stats.get("status") == "failed":
        # Nothing was fetched. Leave the session EMA, peak, seen list and
        # any standing vote exactly as they were. Gemini's error_streak
        # is not a fetch counter and is left alone.
        state["last_check"] = now.isoformat(timespec="seconds")
        _notify_fetch_failure(cfg, state, fetch_stats)
        print(f"  all {fetch_stats.get('feeds_failed', 0)} feeds failed "
              f"— session and vote left unchanged")
        return state
    if fetch_stats.get("status") in ("ok", "degraded"):
        _notify_fetch_recovery(cfg, state)

    new_articles = dedup_articles(articles, state)
    print(f"  {len(articles)} recent articles, {len(new_articles)} new")
    state["last_fetched"] = len(articles)
    state["last_new"] = len(new_articles)

    session_key = night_session_key(now)

    if not new_articles:
        state["last_check"] = now.isoformat(timespec="seconds")
        return state

    scored, scored_guids, api_error = score_articles(new_articles, cfg["gemini_api_key"])
    state = mark_seen(state, scored_guids)
    api_error = api_error or unusable_scores_error(scored, scored_guids)
    record_scoring_result(state, now, api_error,
                          skipped=len(scored_guids) - len(scored))

    net = aggregate_scores(scored)
    cum = round(_session_net(state, session_key) * EMA_DECAY + net, 4)
    state["session_key"] = session_key
    state["session_net"] = cum

    peak = float(state.get("session_peak", 0.0))
    if cum > 0 and cum > peak:
        peak = cum
    elif cum < 0 and cum < peak:
        peak = cum
    state["session_peak"] = peak

    recent = list(state.get("recent_runs", []))
    recent.append(net)
    state["recent_runs"] = recent[-3:]

    direction = net_score_to_vote(cum, SESSION_VOTE_THRESHOLD)

    log_path = cfg.get("news_log_path")
    if log_path and scored:
        records = [rss_news_log.build_record(
            item["article"], item["score"], scored_at=now, session=session_key,
            run_net=net, session_net=cum) for item in scored]
        if not rss_news_log.append(log_path, records):
            print(f"  news history append failed: {log_path}")

    print(f"  run net: {net:+.2f}, session net: {cum:+.2f} -> vote: {direction or 'none'}")
    if api_error:
        print(f"  ⚠ Gemini API error: {api_error}")

    vote_written = False
    if direction and vote_out:
        try:
            write_regime_vote = _get_write_regime_vote()
            write_regime_vote(vote_out, direction, session_key, source="W3")
            vote_written = True
            print(f"  VOTE WRITTEN: {direction} for {session_key} -> {vote_out}")
        except Exception as e:  # noqa: BLE001
            print(f"  vote write failed: {type(e).__name__}: {e}")

    def post(note=None):
        return post_discord_embed(cfg["discord_webhook_url"], direction, net, scored,
                                  session_net=cum, session_peak=peak,
                                  recent_runs=state["recent_runs"], note=note,
                                  api_error=api_error, total_fetched=len(new_articles))

    if mode == "all":
        post()
    elif mode != "off":
        notify_transitions(state, mode, post, api_error=api_error,
                           vote_key=f"{session_key}:{direction}" if vote_written else "")

    state["last_check"] = now.isoformat(timespec="seconds")
    state["last_net_score"] = net
    state["last_articles_scored"] = len(scored)
    return state


class _NoRunLog:
    """Stand-in when src/news/bridge_runs.py cannot be loaded."""
    result = ""
    exit_code = None

    def fail(self, error):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_recorder(bridge_dir, workflow: str):
    """``bridge_runs.run_recorder`` (runs/<W>.jsonl for the dashboard's
    工作流程 view), loaded by file path like ``_get_write_regime_vote``.
    Bookkeeping only — a load failure degrades to a no-op."""
    try:
        import importlib.util
        mod_path = Path(__file__).resolve().parents[2] / "src" / "news" / "bridge_runs.py"
        spec = importlib.util.spec_from_file_location("bridge_runs", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.run_recorder(bridge_dir, workflow)
    except Exception as e:  # noqa: BLE001
        print(f"  run log unavailable: {type(e).__name__}: {e}")
        return _NoRunLog()


def main() -> int:
    ap = argparse.ArgumentParser(description="W3 RSS+Gemini news scorer")
    ap.add_argument("--settings", required=True, help="path to settings.yaml")
    ap.add_argument("--vote-out", default=None,
                    help="regime_vote.json output path (overrides settings)")
    ap.add_argument("--state", default=None,
                    help="state file path (overrides settings)")
    ap.add_argument("--news-log", default=None,
                    help="per-article JSONL history (default: news.rss_news_log, "
                         "else <state dir>/news/news_log.jsonl)")
    ap.add_argument("--once", action="store_true", help="single check then exit")
    ap.add_argument("--loop", action="store_true",
                    help="poll continuously at rss_interval_minutes")
    ap.add_argument("--force-fire", metavar="DIRECTION",
                    choices=["bullish", "bearish"],
                    help="force a vote write (pipeline test) and exit")
    ap.add_argument("--discord-webhook",
                    default=os.environ.get("NEWS_DISCORD_WEBHOOK") or None)
    args = ap.parse_args()

    settings = load_settings(args.settings)
    cfg = get_news_config(settings, discord_webhook=args.discord_webhook)

    vote_out = args.vote_out or settings.get("news", {}).get("regime_vote_path", "")
    state_path = args.state or cfg["state_file"]
    cfg["news_log_path"] = (args.news_log or cfg["news_log_file"]
                            or rss_news_log.default_path(state_path))

    if not cfg["gemini_api_key"] and not args.force_fire:
        print("ERROR: ai.google_api_key not set in settings.yaml")
        with _run_recorder(Path(state_path).parent, "W3") as run:
            run.exit_code = 1
            run.fail("ai.google_api_key not set in settings.yaml")
        return 1

    if args.force_fire:
        direction = "trending-up" if args.force_fire == "bullish" else "trending-down"
        if vote_out:
            write_regime_vote = _get_write_regime_vote()
            now = datetime.now(_TZ_TAIPEI)
            write_regime_vote(vote_out, direction, night_session_key(now), source="W3-manual")
            print(f"  FORCE VOTE: {direction} -> {vote_out}")
        else:
            print("ERROR: no --vote-out or news.regime_vote_path configured")
            return 1
        if cfg["discord_mode"] in ("changes", "all"):
            post_discord_embed(cfg["discord_webhook_url"], direction, 99.0, [])
        return 0

    state = load_state(state_path)
    log_dir = Path(state_path).parent

    if args.loop:
        interval = cfg["interval_minutes"] * 60
        print(f"looping every {cfg['interval_minutes']}m")
        while True:
            state = _recorded_pass(cfg, state, vote_out, state_path)
            append_log(log_dir / LOG_NAME,
                       f"{datetime.now(_TZ_TAIPEI):%Y-%m-%d %H:%M:%S} TPE | "
                       f"scored {state.get('last_articles_scored', 0)} | "
                       f"net {state.get('last_net_score', 0):+.2f}")
            time.sleep(interval)
    else:
        state = _recorded_pass(cfg, state, vote_out, state_path)

    return 0


def _recorded_pass(cfg: dict, state: dict, vote_out: str, state_path: str) -> dict:
    """One scoring pass + state save, logged to runs/W3.jsonl.  A pass
    whose Gemini scoring failed (error_streak went up) is a failed run
    even though the process itself carries on."""
    with _run_recorder(Path(state_path).parent, "W3") as run:
        before = int(state.get("error_streak") or 0)
        state = check_once(cfg, state, vote_out)
        save_state(state_path, state)
        run.exit_code = 0
        run.result = (f"fetched {state.get('last_fetched', '-')} | "
                      f"new {state.get('last_new', '-')} | "
                      f"scored {state.get('last_articles_scored', 0)} | "
                      f"net {float(state.get('session_net') or 0):+.2f}")
        if state.get("fetch_status") == "failed":
            n_fail = int(state.get("feeds_failed") or 0)
            brief = _feed_error_brief(state.get("last_feed_errors"))
            run.fail(f"all {n_fail} feeds failed: {brief}".rstrip(": "))
        elif int(state.get("error_streak") or 0) > before:
            run.fail(str(state.get("last_error") or "scoring failed"))
    return state


if __name__ == "__main__":
    sys.exit(main())
