"""Phase 5 — Offline replay and quantitative benchmark for the 4-layer news pipeline.

Evaluates Layer 1 (NewsPreFilter), Layer 2 (IncrementalEventClusterer), and
optional Layer 3 + Layer 4 over historical ``news_log*.jsonl`` files (via
``rss_news_log.log_files``) and/or RSS XML snapshots (``tests/fixtures/news/*.xml``).

Metrics reported:
- Total input articles vs. Layer 1 kept vs. Layer 2 unique representatives
- Total pre-LLM noise reduction rate (%) & LLM API call savings (%)
- Layer 1 drop breakdown (pure price recaps, spam templates, off-topic, blocked sources)
- Layer 2 dedup rate (%) by stage (url / title_key / simhash / semantic) & top clusters
- Cross-day 24-48h follow-up detections
- Average per-article pipeline latency (ms)

By default runs 100% offline using the local lexical embedder and zero Gemini
calls (safe, free, fast). Pass ``--embedder gemini`` + ``--settings`` to test
with live Gemini embeddings, or ``--simulate-l4`` to also project Layer 4
routing over logged v1 scores.

Usage::

    python scripts/news_bridge/replay_news_pipeline.py --log /path/to/news_log.jsonl
    python scripts/news_bridge/replay_news_pipeline.py --xml tests/fixtures/news/*.xml
    python scripts/news_bridge/replay_news_pipeline.py --log ... --json-out report.json
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent
for p in (str(_REPO_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import rss_news_log  # noqa: E402
import rss_scorer  # noqa: E402
from src.news.pipeline import (  # noqa: E402
    IncrementalEventClusterer,
    NewsPreFilter,
    build_embedder,
    load_pipeline_config,
)

_TZ_TAIPEI = timezone(timedelta(hours=8))


def _parse_iso(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_TAIPEI)
    return dt.astimezone(_TZ_TAIPEI)


def load_jsonl_batches(log_path: str) -> list[tuple[datetime, list[dict]]]:
    """Load one or more monthly JSONL logs grouped into chronological scorer runs."""
    files = rss_news_log.log_files(log_path)
    if not files:
        p = Path(log_path)
        if p.is_file():
            files = [str(p)]
        else:
            raise FileNotFoundError(f"No news log files found for {log_path}")

    runs_by_ts: dict[str, list[dict]] = collections.defaultdict(list)
    for fpath in files:
        with open(fpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or not rec.get("title"):
                    continue
                scored_at = str(rec.get("scored_at") or rec.get("published_at") or "")
                pub_dt = _parse_iso(str(rec.get("published_at") or scored_at))
                article = {
                    "guid": rec.get("link") or f"{scored_at}|{rec.get('title', '')}",
                    "title": rec.get("title", ""),
                    "summary": rec.get("summary", ""),
                    "link": rec.get("link", ""),
                    "source": rec.get("source", ""),
                    "published_dt": pub_dt,
                    "session": rec.get("session", ""),
                    "_v1_direction": rec.get("direction", "neutral"),
                    "_v1_score": float(rec.get("score") or 0.0),
                }
                runs_by_ts[scored_at].append(article)

    ordered: list[tuple[datetime, list[dict]]] = []
    for stamp in sorted(runs_by_ts):
        dt = _parse_iso(stamp) or datetime(2026, 11, 20, 12, 0, tzinfo=_TZ_TAIPEI)
        ordered.append((dt, runs_by_ts[stamp]))
    return ordered


def load_xml_batches(xml_paths: list[str]) -> list[tuple[datetime, list[dict]]]:
    """Load RSS XML snapshots as a single chronological replay batch."""
    articles: list[dict] = []
    latest_dt: datetime | None = None
    for xp in xml_paths:
        body = Path(xp).read_bytes()
        # Use filename as synthetic feed URL so TAIFEX tag & host resolution work
        stem = Path(xp).stem.lower()
        fake_url = (
            "https://www.taifex.com.tw/cht/11/RSS1"
            if "taifex" in stem
            else "https://news.cnyes.com/rss/v1/news/category/tw_stock"
            if "cnyes" in stem
            else "https://tw.stock.yahoo.com/rss?category=tw-market"
            if "yahoo" in stem
            else f"https://fixture.local/{stem}"
        )
        parsed = rss_scorer._parse_feed_bytes(fake_url, body)
        for art in parsed:
            pub = art.get("published_dt")
            if isinstance(pub, datetime) and (latest_dt is None or pub > latest_dt):
                latest_dt = pub
        articles.extend(parsed)
    now = (latest_dt or datetime(2026, 11, 20, 12, 0, tzinfo=_TZ_TAIPEI)).astimezone(_TZ_TAIPEI)
    return [(now, articles)]


def replay_batches(
    batches: list[tuple[datetime, list[dict]]],
    *,
    config_path: str | None = None,
    embedder_provider: str = "lexical",
    api_key: str = "",
) -> dict[str, Any]:
    """Replay chronological batches through Layer 1 + Layer 2 and collect metrics."""
    cfg = load_pipeline_config(config_path)
    prefilter = NewsPreFilter(cfg)
    embedder = build_embedder(cfg.dedup, api_key=api_key, provider_override=embedder_provider)
    clusterer = IncrementalEventClusterer(cfg, embedder=embedder)

    total_in = 0
    l1_kept_total = 0
    l1_dropped_total = 0
    l1_reasons: collections.Counter[str] = collections.Counter()

    l2_unique_total = 0
    l2_dup_total = 0
    l2_tier_upgraded = 0
    l2_methods: collections.Counter[str] = collections.Counter()
    l2_followups = 0
    tier_counts: collections.Counter[int] = collections.Counter()
    cluster_mentions: dict[str, dict[str, Any]] = {}

    t0 = time.perf_counter()
    for run_dt, batch in batches:
        total_in += len(batch)
        kept, _dropped, drop_counts = prefilter.filter_batch(batch)
        l1_kept_total += len(kept)
        l1_dropped_total += len(batch) - len(kept)
        l1_reasons.update(drop_counts)

        decisions = clusterer.process_batch(kept, now=run_dt)
        for dec in decisions:
            if dec.is_duplicate:
                l2_dup_total += 1
                l2_methods[dec.dup_method or "unknown"] += 1
            else:
                l2_unique_total += 1
                tier_counts[dec.source_tier] += 1
                if dec.tier_upgraded:
                    l2_tier_upgraded += 1
                if dec.prior_event is not None:
                    l2_followups += 1
            entry = cluster_mentions.setdefault(
                dec.cluster_id,
                {
                    "cluster_id": dec.cluster_id,
                    "title": dec.prefilter.cleaned_title,
                    "source": dec.article.get("source", ""),
                    "source_tier": dec.source_tier,
                    "mentions": 0,
                },
            )
            entry["mentions"] = max(entry["mentions"], dec.mention_count)
            if dec.source_tier < entry["source_tier"]:
                entry["source_tier"] = dec.source_tier
                entry["title"] = dec.prefilter.cleaned_title
                entry["source"] = dec.article.get("source", "")

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    avg_ms = elapsed_ms / total_in if total_in else 0.0
    l1_drop_rate = (l1_dropped_total / total_in * 100.0) if total_in else 0.0
    price_recap_rate = (l1_reasons.get("price_recap_only", 0) / total_in * 100.0) if total_in else 0.0
    l2_dedup_rate = (l2_dup_total / l1_kept_total * 100.0) if l1_kept_total else 0.0
    llm_calls_needed = l2_unique_total
    llm_savings_rate = ((total_in - llm_calls_needed) / total_in * 100.0) if total_in else 0.0
    # Tier-3-only clusters do not fire votes under default config; show actionable Tier 1/2 count too.
    routable_tier12 = tier_counts.get(1, 0) + tier_counts.get(2, 0)

    top_clusters = sorted(
        (c for c in cluster_mentions.values() if c["mentions"] >= 2),
        key=lambda c: (-c["mentions"], c["source_tier"]),
    )[:15]

    return {
        "batches": len(batches),
        "total_input": total_in,
        "l1_kept": l1_kept_total,
        "l1_dropped": l1_dropped_total,
        "l1_drop_rate_pct": round(l1_drop_rate, 2),
        "l1_price_recap_dropped": l1_reasons.get("price_recap_only", 0),
        "l1_price_recap_rate_pct": round(price_recap_rate, 2),
        "l1_drop_reasons": dict(l1_reasons.most_common()),
        "l2_unique_representatives": l2_unique_total,
        "l2_duplicates_collapsed": l2_dup_total,
        "l2_dedup_rate_pct": round(l2_dedup_rate, 2),
        "l2_dedup_methods": dict(l2_methods.most_common()),
        "l2_tier_upgraded": l2_tier_upgraded,
        "l2_crossday_followups": l2_followups,
        "unique_by_source_tier": {str(k): v for k, v in sorted(tier_counts.items())},
        "routable_tier12_representatives": routable_tier12,
        "llm_calls_needed": llm_calls_needed,
        "llm_calls_saved": total_in - llm_calls_needed,
        "llm_savings_rate_pct": round(llm_savings_rate, 2),
        "elapsed_ms_total": round(elapsed_ms, 2),
        "avg_latency_ms_per_article": round(avg_ms, 4),
        "embedder_provider": embedder_provider,
        "top_merged_clusters": top_clusters,
    }


def format_report(summary: dict[str, Any]) -> str:
    lines = [
        "=== News Signal Pipeline v2 — Offline Replay Report ===",
        f"Batches replayed              : {summary['batches']}",
        f"Total input articles          : {summary['total_input']:,}",
        f"Layer 1 kept / dropped        : {summary['l1_kept']:,} / {summary['l1_dropped']:,} "
        f"(L1 drop rate: {summary['l1_drop_rate_pct']:.1f}%)",
        f"  - Pure price recap dropped  : {summary['l1_price_recap_dropped']:,} "
        f"({summary['l1_price_recap_rate_pct']:.1f}% of input)",
        f"Layer 2 unique / duplicates   : {summary['l2_unique_representatives']:,} / "
        f"{summary['l2_duplicates_collapsed']:,} (L2 dedup rate: {summary['l2_dedup_rate_pct']:.1f}%)",
        f"  - Dedup methods             : {summary['l2_dedup_methods']}",
        f"  - Cross-day followups (48h) : {summary['l2_crossday_followups']:,}",
        f"Unique by source tier         : {summary['unique_by_source_tier']} "
        f"(Tier 1+2 routable: {summary['routable_tier12_representatives']:,})",
        f"LLM calls needed / saved      : {summary['llm_calls_needed']:,} / "
        f"{summary['llm_calls_saved']:,} (LLM savings: {summary['llm_savings_rate_pct']:.1f}%)",
        f"Latency (L1+L2 {summary['embedder_provider']:7s})       : "
        f"{summary['elapsed_ms_total']:.1f} ms total "
        f"({summary['avg_latency_ms_per_article']:.3f} ms/article)",
        "",
        "Layer 1 drop breakdown:",
    ]
    for reason, cnt in summary["l1_drop_reasons"].items():
        lines.append(f"  - {reason:32s}: {cnt:,}")
    if summary["top_merged_clusters"]:
        lines.append("")
        lines.append("Top merged multi-outlet event clusters:")
        for c in summary["top_merged_clusters"][:10]:
            lines.append(
                f"  - [x{c['mentions']:2d} T{c['source_tier']}] ({c['source'][:16]}) "
                f"{c['title'][:76]}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline replay benchmark for news pipeline v2")
    ap.add_argument("--log", default="", help="Path to news_log.jsonl (auto-loads monthly files)")
    ap.add_argument("--xml", nargs="*", default=[], help="RSS XML fixture files to replay")
    ap.add_argument("--config", default="", help="Override path to config/news_filter.yaml")
    ap.add_argument(
        "--embedder",
        default="lexical",
        choices=["lexical", "gemini", "none"],
        help="Embedding provider for Layer 2 replay (default: lexical offline)",
    )
    ap.add_argument("--settings", default="", help="settings.yaml (only needed for --embedder gemini)")
    ap.add_argument("--json-out", default="", help="Optional JSON output file path")
    args = ap.parse_args(argv)

    if not args.log and not args.xml:
        ap.error("Provide --log <news_log.jsonl> and/or --xml <fixture.xml ...>")

    batches: list[tuple[datetime, list[dict]]] = []
    if args.log:
        batches.extend(load_jsonl_batches(args.log))
    if args.xml:
        batches.extend(load_xml_batches(args.xml))

    api_key = ""
    if args.embedder == "gemini" and args.settings:
        settings = rss_scorer.load_settings(args.settings)
        api_key = (settings.get("ai") or {}).get("google_api_key", "")

    summary = replay_batches(
        batches,
        config_path=args.config or None,
        embedder_provider=args.embedder,
        api_key=api_key,
    )
    print(format_report(summary))

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSaved JSON report -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
