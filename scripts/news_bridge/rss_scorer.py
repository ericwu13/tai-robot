"""W3 RSS+Gemini news scorer — sentiment-weighted regime votes.

Standalone bridge script (no n8n dependency).  Fetches public RSS feeds,
filters to articles from the last 2 hours, scores each headline+summary
via Gemini (see GEMINI_MODEL), aggregates a net sentiment score into an
EMA (exponential moving average) with a 2-hour half-life, and when the
EMA total exceeds SESSION_VOTE_THRESHOLD writes a ``regime_vote.json``
for the regime state machine.  A Discord embed posts only when something
changed: a run that scored new articles, or once on entering the weekend
pause.
Scoring is skipped Sat 05:00 - Mon 15:00 TPE (no night session to vote
on).

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
CLI flag (same as W2).

Settings block (in settings.yaml under ``news``)::

    news:
      rss_feeds:
        - https://technews.tw/feed/
        - ...
      rss_interval_minutes: 30
      rss_state_file: data/rss_scorer_state.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TZ_TAIPEI = timezone(timedelta(hours=8))

VOTE_THRESHOLD = 0.8          # per-run scale (kept as net_score_to_vote default)
SESSION_VOTE_THRESHOLD: float = 1.5  # session EMA net required to fire a vote

# EMA half-life for session score — empirically, news attention decays within 1-2h
# 0.84 ≈ exp(-ln(2) * 0.5 / 2.0) = 2-hour half-life at 30-min polling cadence
EMA_HALF_LIFE_HOURS: float = 2.0
EMA_DECAY: float = math.exp(-math.log(2) * 0.5 / EMA_HALF_LIFE_HOURS)

ARTICLE_MAX_AGE_HOURS = 4
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

LOG_NAME = "rss_scorer.log"
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_LINES = 2000

_UA = {"User-Agent": "Mozilla/5.0 (tai-robot news bridge)"}

DEFAULT_FEEDS = [
    # ── Global ──────────────────────────────────────────────────────────
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://feeds.marketwatch.com/marketwatch/topstories",
    "https://finance.yahoo.com/news/rssindex",
    "https://www.investing.com/rss/news.rss",
    # ── Taiwan / Asia ───────────────────────────────────────────────────
    "https://technews.tw/feed/",
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
        "gemini_api_key": ai.get("google_api_key", ""),
        "discord_webhook_url": discord_webhook or news.get("discord_webhook") or "",
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


def score_article(headline: str, summary: str, api_key: str) -> dict | None:
    """Call Gemini to score one article.  Returns parsed JSON or None."""
    prompt = SCORING_PROMPT.format(headline=headline, summary=summary[:500])
    url = GEMINI_API_URL.format(model=GEMINI_MODEL, key=api_key)
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024},
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
    except Exception as e:  # noqa: BLE001
        print(f"    gemini scoring failed: {type(e).__name__}: {e}")
        return None


def score_articles(articles: list[dict], api_key: str) -> list[dict]:
    """Score each article, return list of {article, score} dicts."""
    results = []
    for art in articles:
        score = score_article(art["title"], art["summary"], api_key)
        if score and "direction" in score and "confidence" in score:
            score["direction"] = score["direction"].lower()
            results.append({"article": art, "score": score})
            direction = score["direction"]
            confidence = score["confidence"]
            print(f"    [{direction} {confidence:.2f}] {art['title'][:70]}")
        else:
            print(f"    [SKIP] {art['title'][:70]}")
    return results


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
        total += max(-0.5, min(0.5, raw))
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
    if now.hour >= 15:
        return f"{now.strftime('%Y-%m-%d')}|NIGHT"
    return f"{(now - timedelta(days=1)).strftime('%Y-%m-%d')}|NIGHT"


def in_weekend_pause(now: datetime) -> bool:
    """True from Sat 05:00 to Mon 15:00 TPE.

    No TAIFEX night session exists in that window, so any vote written
    would expire against a session that never happens.  Scoring (and its
    Gemini cost) is skipped; the heartbeat embed still posts.
    """
    wd = now.weekday()  # Mon=0 .. Sun=6
    if wd == 5:  # Saturday: Friday's night session ends 05:00
        return now.hour >= 5
    if wd == 6:  # Sunday
        return True
    if wd == 0:  # Monday: night session opens 15:00
        return now.hour < 15
    return False


# ── Discord ──────────────────────────────────────────────────────────────

TOP_ARTICLES_SHOWN = 10


def post_discord_embed(webhook: str | None, direction: str | None, net_score: float,
                       scored: list[dict], session_net: float | None = None,
                       session_peak: float = 0.0, recent_runs: list[float] | None = None,
                       note: str | None = None) -> None:
    if not webhook:
        return
    if direction == "trending-up":
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
    fields.append({"name": "Articles Scored", "value": str(len(scored)), "inline": True})

    if session_peak > 0 and session_net is not None and session_net < session_peak - 1.5:
        erosion = session_net - session_peak
        fields.append({"name": "Drawdown",
                       "value": f"⚠️ Eroding: peak +{session_peak:.1f} → now +{session_net:.1f} ({erosion:.1f})",
                       "inline": False})
    elif session_peak < 0 and session_net is not None and session_net > session_peak + 1.5:
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

    embed = {
        "title": f"📰 RSS Sentiment Vote: {direction or 'none'}",
        "description": "\n".join(desc_lines) or "No articles scored.",
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


def check_once(cfg: dict, state: dict, vote_out: str,
               now: datetime | None = None) -> dict:
    """One RSS scoring pass.  Returns updated state.

    The vote fires on the SESSION-CUMULATIVE net score, not the per-run
    net — one noisy 30-min window can't set the standing vote, but a
    genuinely strong news day crosses the threshold within a run or two.
    """
    now = now or datetime.now(_TZ_TAIPEI)
    print(f"[{now:%Y-%m-%d %H:%M:%S}] RSS scorer checking {len(cfg['feeds'])} feeds")

    if in_weekend_pause(now):
        print("  weekend pause (Sat 05:00 - Mon 15:00 TPE) — scoring skipped")
        if not state.get("weekend_pause_notified"):
            post_discord_embed(cfg["discord_webhook_url"], None, 0.0, [],
                               note="Weekend — scoring paused (no night session to vote on)")
            state["weekend_pause_notified"] = True
        state["last_check"] = now.isoformat(timespec="seconds")
        return state
    state["weekend_pause_notified"] = False

    articles = fetch_all_feeds(cfg["feeds"])
    new_articles = dedup_articles(articles, state)
    print(f"  {len(articles)} recent articles, {len(new_articles)} new")

    session_key = night_session_key(now)

    if not new_articles:
        state["last_check"] = now.isoformat(timespec="seconds")
        return state

    scored = score_articles(new_articles, cfg["gemini_api_key"])
    state = mark_seen(state, [a["guid"] for a in new_articles])

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

    print(f"  run net: {net:+.2f}, session net: {cum:+.2f} -> vote: {direction or 'none'}")

    if direction and vote_out:
        try:
            write_regime_vote = _get_write_regime_vote()
            write_regime_vote(vote_out, direction, session_key, source="W3")
            print(f"  VOTE WRITTEN: {direction} for {session_key} -> {vote_out}")
        except Exception as e:  # noqa: BLE001
            print(f"  vote write failed: {type(e).__name__}: {e}")
    post_discord_embed(cfg["discord_webhook_url"], direction, net, scored,
                       session_net=cum, session_peak=peak,
                       recent_runs=state["recent_runs"])

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
    ap.add_argument("--discord-webhook",
                    default=os.environ.get("NEWS_DISCORD_WEBHOOK") or None)
    args = ap.parse_args()

    settings = load_settings(args.settings)
    cfg = get_news_config(settings, discord_webhook=args.discord_webhook)

    vote_out = args.vote_out or settings.get("news", {}).get("regime_vote_path", "")
    state_path = args.state or cfg["state_file"]

    if not cfg["gemini_api_key"] and not args.force_fire:
        print("ERROR: ai.google_api_key not set in settings.yaml")
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
