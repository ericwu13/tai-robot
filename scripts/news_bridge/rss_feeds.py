"""W3's default RSS feed list, shared by the scorer and the monitor.

``news.rss_feeds`` in settings.yaml REPLACES this list when it is
non-empty (``rss_scorer.get_news_config``).  An install that copied the
list into its settings therefore never sees a default change: Steven ran
the 5 pre-#266 feeds (one of them dead) for days after #266 shipped 13.
``override_gap`` names what such an override leaves out, so the scorer
log and ``check_bridge`` can say so.  Stdlib only: the scorer imports it
as a sibling module, the monitor as ``scripts.news_bridge.rss_feeds``.
"""
from __future__ import annotations

from urllib.parse import urlparse

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


def _norm(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower()


def missing_defaults(configured) -> list[str]:
    """Default feeds absent from a non-empty ``news.rss_feeds`` override.

    Empty / missing / not-a-list means the defaults are used: nothing is
    missing.
    """
    if not isinstance(configured, list) or not configured:
        return []
    have = {_norm(u) for u in configured if isinstance(u, str)}
    return [u for u in DEFAULT_FEEDS if _norm(u) not in have]


def override_gap(configured) -> str:
    """One-line warning for a stale override, or ``""``."""
    missing = missing_defaults(configured)
    if not missing:
        return ""
    hosts = []
    for url in missing:
        host = (urlparse(url).hostname or url).removeprefix("www.")
        if host not in hosts:
            hosts.append(host)
    return (f"settings news.rss_feeds overrides the defaults and leaves out "
            f"{len(missing)} of {len(DEFAULT_FEEDS)} default feed(s) "
            f"({', '.join(hosts)}); empty the list to use the defaults")
