"""W3 RSS+Gemini news scorer — sentiment-weighted regime votes.

Standalone bridge script (no n8n dependency).  Fetches public RSS feeds,
filters to articles from the last 2 hours, scores each headline+summary
via Gemini 1.5 Flash, aggregates a net sentiment score, and when the
aggregate exceeds the threshold writes a ``regime_vote.json`` for the
regime state machine and posts a Discord embed.

Deduplicates via a persistent state file (GUID-based) so articles are
scored exactly once across restarts.

Usage:
    # one-shot check
    python rss_scorer.py --settings settings.yaml --once

    # continuous loop (default 30 min interval from settings)
    python rss_scorer.py --settings settings.yaml --loop

    # force a vote write (pipeline test)
    python rss_scorer.py --settings settings.yaml --force-fire bullish

Settings block (in settings.yaml under ``news``)::

    news:
      rss_feeds:
        - https://feeds.reuters.com/reuters/businessNews
        - ...
      rss_interval_minutes: 30
      rss_state_file: data/rss_scorer_state.json
      gemini_api_key: ""
      discord_webhook_url: ""
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TZ_TAIPEI = timezone(timedelta(hours=8))

VOTE_THRESHOLD = 0.8
ARTICLE_MAX_AGE_HOURS = 2
GEMINI_MODEL = "gemini-1.5-flash"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

LOG_NAME = "rss_scorer.log"
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_LINES = 2000

_UA = {"User-Agent": "Mozilla/5.0 (tai-robot news bridge)"}

DEFAULT_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "https://finance.yahoo.com/news/rssindex",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
]

SCORING_PROMPT = """\
You are a financial sentiment analyst.  Given the headline and summary of a
news article, classify its likely SHORT-TERM impact on global equity markets
(next 1-4 hours).

Respond with ONLY a JSON object — no markdown, no explanation:
{"direction": "bullish"|"bearish"|"neutral", "confidence": <float 0.0-1.0>, "reason": "<one sentence>"}

Rules:
- "confidence" reflects how strongly the article moves markets, not how sure you are of the classification.
- Routine earnings within expectations → neutral with low confidence.
- Geopolitical escalation, surprise rate moves, major tech earnings beats/misses → high confidence.
- If unsure, default to neutral with confidence 0.2.

Headline: {headline}
Summary: {summary}
"""


# ── Settings loading ─────────────────────────────────────────────────────

def load_settings(path: str) -> dict:
    """Load settings.yaml and return the full dict."""
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_news_config(settings: dict) -> dict:
    """Extract the news config block with defaults."""
    news = settings.get("news", {}) or {}
    return {
        "feeds": news.get("rss_feeds") or DEFAULT_FEEDS,
        "interval_minutes": int(news.get("rss_interval_minutes", 30)),
        "state_file": news.get("rss_state_file", "data/rss_scorer_state.json"),
        "gemini_api_key": news.get("gemini_api_key", ""),
        "discord_webhook_url": news.get("discord_webhook_url", ""),
    }


# ── RSS fetching ─────────────────────────────────────────────────────────

def fetch_feed(url: str) -> list[dict]:
    """Fetch and parse one RSS feed.  Returns list of article dicts.

    Each dict has keys: guid, title, summary, published_dt (datetime, UTC).
    """
    import feedparser
    feed = feedparser.parse(url)
    articles = []
    for entry in feed.entries:
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        if pub is None:
            continue
        import calendar
        pub_dt = datetime.fromtimestamp(calendar.timegm(pub), tz=timezone.utc)
        guid = entry.get("id") or entry.get("link") or entry.get("title", "")
        articles.append({
            "guid": guid,
            "title": entry.get("title", ""),
            "summary": entry.get("summary", ""),
            "published_dt": pub_dt,
        })
    return articles


def fetch_all_feeds(feed_urls: list[str], max_age_hours: float = ARTICLE_MAX_AGE_HOURS) -> list[dict]:
    """Fetch all feeds, filter to recent articles, return combined list."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    all_articles = []
    for url in feed_urls:
        try:
            articles = fetch_feed(url)
            recent = [a for a in articles if a["published_dt"] >= cutoff]
            print(f"  [{url[:60]}...] {len(recent)}/{len(articles)} recent")
            all_articles.extend(recent)
        except Exception as e:  # noqa: BLE001
            print(f"  [{url[:60]}...] fetch failed: {type(e).__name__}: {e}")
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

def score_article(headline: str, summary: str, api_key: str) -> dict | None:
    """Call Gemini to score one article.  Returns parsed JSON or None."""
    prompt = SCORING_PROMPT.format(headline=headline, summary=summary[:500])
    url = GEMINI_API_URL.format(model=GEMINI_MODEL, key=api_key)
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 256},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            url, data=body,
            headers={**_UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception as e:  # noqa: BLE001
        print(f"    gemini scoring failed: {type(e).__name__}: {e}")
        return None


def score_articles(articles: list[dict], api_key: str) -> list[dict]:
    """Score each article, return list of {article, score} dicts."""
    results = []
    for art in articles:
        score = score_article(art["title"], art["summary"], api_key)
        if score and "direction" in score and "confidence" in score:
            results.append({"article": art, "score": score})
            direction = score["direction"]
            confidence = score["confidence"]
            print(f"    [{direction} {confidence:.2f}] {art['title'][:70]}")
        else:
            print(f"    [SKIP] {art['title'][:70]}")
    return results


# ── Aggregation ──────────────────────────────────────────────────────────

def aggregate_scores(scored: list[dict]) -> float:
    """Net score: sum of (confidence × direction_sign).

    bullish = +1, bearish = -1, neutral = 0.
    """
    total = 0.0
    for item in scored:
        s = item["score"]
        direction = s.get("direction", "neutral")
        confidence = float(s.get("confidence", 0))
        if direction == "bullish":
            total += confidence
        elif direction == "bearish":
            total -= confidence
    return total


def net_score_to_vote(net_score: float) -> str | None:
    """Map aggregate score to a regime vote direction, or None."""
    if net_score >= VOTE_THRESHOLD:
        return "trending-up"
    if net_score <= -VOTE_THRESHOLD:
        return "trending-down"
    return None


# ── Session key (mirrors crossmarket_monitor.py) ────────────────────────

def night_session_key(now: datetime) -> str:
    if now.hour >= 15:
        return f"{now.strftime('%Y-%m-%d')}|NIGHT"
    return f"{(now - timedelta(days=1)).strftime('%Y-%m-%d')}|NIGHT"


# ── Discord ──────────────────────────────────────────────────────────────

def post_discord_embed(webhook: str | None, direction: str, net_score: float,
                       scored: list[dict]) -> None:
    if not webhook:
        return
    color = 0x00AA00 if direction == "trending-up" else 0xCC0000
    top_articles = scored[:5]
    desc_lines = []
    for item in top_articles:
        s = item["score"]
        desc_lines.append(
            f"**{s['direction']}** ({s['confidence']:.1f}) — {item['article']['title'][:80]}")
    embed = {
        "title": f"📰 RSS Sentiment Vote: {direction}",
        "description": "\n".join(desc_lines) or "No articles scored.",
        "color": color,
        "fields": [
            {"name": "Net Score", "value": f"{net_score:+.2f}", "inline": True},
            {"name": "Articles Scored", "value": str(len(scored)), "inline": True},
        ],
        "footer": {"text": "W3 RSS Scorer"},
    }
    body = json.dumps({"embeds": [embed]}).encode("utf-8")
    try:
        req = urllib.request.Request(
            webhook, data=body,
            headers={**_UA, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        print("  discord embed sent")
    except Exception as e:  # noqa: BLE001
        print(f"  discord post failed: {type(e).__name__}: {e}")


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


# ── Main logic ───────────────────────────────────────────────────────────

def check_once(cfg: dict, state: dict, vote_out: str) -> dict:
    """One RSS scoring pass.  Returns updated state."""
    now = datetime.now(_TZ_TAIPEI)
    print(f"[{now:%Y-%m-%d %H:%M:%S}] RSS scorer checking {len(cfg['feeds'])} feeds")

    articles = fetch_all_feeds(cfg["feeds"])
    new_articles = dedup_articles(articles, state)
    print(f"  {len(articles)} recent articles, {len(new_articles)} new")

    if not new_articles:
        state["last_check"] = now.isoformat(timespec="seconds")
        return state

    scored = score_articles(new_articles, cfg["gemini_api_key"])
    state = mark_seen(state, [a["guid"] for a in new_articles])

    net = aggregate_scores(scored)
    direction = net_score_to_vote(net)

    print(f"  net score: {net:+.2f} -> vote: {direction or 'none'}")

    if direction and vote_out:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        from news.regime_vote import write_regime_vote
        session_key = night_session_key(now)
        write_regime_vote(vote_out, direction, session_key, source="W3")
        print(f"  VOTE WRITTEN: {direction} for {session_key} -> {vote_out}")
        post_discord_embed(cfg["discord_webhook_url"], direction, net, scored)

    state["last_check"] = now.isoformat(timespec="seconds")
    state["last_net_score"] = net
    state["last_articles_scored"] = len(scored)
    return state


def main() -> int:
    ap = argparse.ArgumentParser(description="W3 RSS+Gemini news scorer")
    ap.add_argument("--settings", required=True, help="path to settings.yaml")
    ap.add_argument("--vote-out", default=None,
                    help="regime_vote.json output path (overrides settings)")
    ap.add_argument("--state", default=None,
                    help="state file path (overrides settings)")
    ap.add_argument("--once", action="store_true", help="single check then exit")
    ap.add_argument("--loop", action="store_true",
                    help="poll continuously at rss_interval_minutes")
    ap.add_argument("--force-fire", metavar="DIRECTION",
                    choices=["bullish", "bearish"],
                    help="force a vote write (pipeline test) and exit")
    args = ap.parse_args()

    settings = load_settings(args.settings)
    cfg = get_news_config(settings)

    vote_out = args.vote_out or settings.get("news", {}).get("regime_vote_path", "")
    state_path = args.state or cfg["state_file"]

    if not cfg["gemini_api_key"] and not args.force_fire:
        print("ERROR: news.gemini_api_key not set in settings.yaml")
        return 1

    if args.force_fire:
        direction = "trending-up" if args.force_fire == "bullish" else "trending-down"
        if vote_out:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
            from news.regime_vote import write_regime_vote
            now = datetime.now(_TZ_TAIPEI)
            write_regime_vote(vote_out, direction, night_session_key(now), source="W3-manual")
            print(f"  FORCE VOTE: {direction} -> {vote_out}")
        else:
            print("ERROR: no --vote-out or news.regime_vote_path configured")
            return 1
        post_discord_embed(cfg["discord_webhook_url"], direction, 99.0, [])
        return 0

    state = load_state(state_path)
    log_dir = Path(state_path).parent

    if args.loop:
        interval = cfg["interval_minutes"] * 60
        print(f"looping every {cfg['interval_minutes']}m")
        while True:
            state = check_once(cfg, state, vote_out)
            save_state(state_path, state)
            append_log(log_dir / LOG_NAME,
                       f"{datetime.now(_TZ_TAIPEI):%Y-%m-%d %H:%M:%S} TPE | "
                       f"scored {state.get('last_articles_scored', 0)} | "
                       f"net {state.get('last_net_score', 0):+.2f}")
            time.sleep(interval)
    else:
        state = check_once(cfg, state, vote_out)
        save_state(state_path, state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
