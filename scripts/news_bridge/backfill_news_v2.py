"""Backfill historical W3 news logs through Pipeline v2 so the Dashboard
(新聞訊號: pulse gauge, drivers, trend chart, article feed) displays the exact
signals, scores, and session EMA curves that v2 would have produced.

Features:
- Replays all historical ``news_log*.jsonl`` files (pre-split + monthly) in
  exact chronological ``scored_at`` order through the real 4-layer funnel
  (Filter -> Deduplicate -> Classify -> Route).
- Resumable JSONL classification cache (enabled by default) so interrupted or
  rate-limited runs never pay twice for already-classified event clusters.
- Optional ``--bots-dir data/live`` loads historical TXF/MTX/TMF 30-min candles
  (zero look-ahead: last completed candle prior to ``scored_at``) for genuine
  historical Layer-4 bullish price confirmation.
- Safe ``--in-place`` mode moves existing v1 ``news_log*.jsonl`` files into a
  timestamped ``backup_v1_YYYYMMDD_HHMMSS/`` subfolder before atomically
  installing the new ``news_log_YYYYMM.jsonl`` files.
- Optional ``--update-state`` updates ``rss_scorer_state.json`` (preserving
  ``seen_guids`` / fetch telemetry) and writes the warm Layer-2 cluster window
  into sibling ``rss_pipeline_v2_state.json`` so the live Dashboard pulse gauge
  and the next scheduled W3 pass seamlessly continue from the backfilled state.

Usage::

    # 1. Free dry-run preview (Layer 1 + Layer 2 only, zero LLM calls):
    python scripts/news_bridge/backfill_news_v2.py --settings settings.yaml --dry-run

    # 2. Stage backfilled logs into a separate folder:
    python scripts/news_bridge/backfill_news_v2.py --settings settings.yaml --out-dir data/news_v2_preview

    # 3. Full in-place backfill + update W3/Dashboard state (backs up v1 logs):
    python scripts/news_bridge/backfill_news_v2.py --settings settings.yaml --bots-dir data/live --in-place --update-state
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent
for _p in (str(_REPO_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import replay_news_pipeline  # noqa: E402
import rss_news_log  # noqa: E402
import rss_scorer  # noqa: E402
from src.news.pipeline import (  # noqa: E402
    ClassifiedEvent,
    ClassifierAPIError,
    DedupDecision,
    GeminiEventClassifier,
    MarketSnapshot,
    NewsSignalPipeline,
    load_pipeline_config,
    parse_classified_event,
)

_TZ_TAIPEI = timezone(timedelta(hours=8))
_DEFAULT_CACHE_NAME = ".backfill_v2_cache.jsonl"


# ── Optional historical TXF/MTX/TMF price context (zero look-ahead) ──────

class HistoricalMarketProvider:
    """Derive point-in-time TXF/MTX/TMF % change from bot 1-min bar files.

    Uses the exact same candle builder as ``scripts/dashboard/news_trend.py``
    and only reads candles whose end time <= ``batch_dt`` (no look-ahead).
    """

    def __init__(self, candles: list[dict], symbol: str = "", interval_s: int = 1800):
        self.symbol = symbol
        self.interval_s = interval_s
        self.candles = sorted(candles, key=lambda c: c["time"])
        self._ends = [c["time"] + interval_s for c in self.candles]

    @classmethod
    def from_bots_dir(cls, bots_dir: str, since_day: str = "") -> "HistoricalMarketProvider | None":
        if not bots_dir or not os.path.isdir(bots_dir):
            return None
        try:
            from scripts.dashboard import news_trend  # noqa: PLC0415

            files = news_trend.symbol_files(bots_dir)
            sym = news_trend.pick_symbol(files)
            if not sym:
                return None
            candles = news_trend.build_candles(files.get(sym, []), since_day=since_day)
            if not candles:
                return None
            return cls(candles, symbol=sym, interval_s=news_trend.TREND_INTERVAL_S)
        except Exception as exc:  # noqa: BLE001 — optional helper
            print(f"  [backfill] could not load historical candles from {bots_dir}: {exc}")
            return None

    def snapshot_at(self, dt: datetime) -> MarketSnapshot | None:
        if not self.candles:
            return None
        tpe_naive = dt.astimezone(_TZ_TAIPEI).replace(tzinfo=None)
        chart_s = int(tpe_naive.replace(tzinfo=timezone.utc).timestamp())

        # Binary search for latest completed candle (end <= chart_s)
        lo, hi, idx = 0, len(self._ends) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._ends[mid] <= chart_s:
                idx = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if idx < 0:
            return None

        cur = self.candles[idx]
        # Require a reasonably fresh bar (within 4h of batch_dt)
        if chart_s - self._ends[idx] > 4 * 3600:
            return None

        # Compare latest completed close against lookback bar (~4h earlier or earliest available)
        ref_idx = max(0, idx - 8)
        ref_px = float(self.candles[ref_idx]["open"])
        cur_px = float(cur["close"])
        if ref_px <= 0:
            return None
        pct = round((cur_px - ref_px) / ref_px * 100.0, 3)
        return MarketSnapshot(
            skcom_bar_pct=pct,
            skcom_volume_ratio=None,
            summary_text=f"- {self.symbol or 'TXF'}(historical): {pct:+.2f}%",
        )


# ── Resumable JSONL classification cache ─────────────────────────────────

def _cache_key(decision: DedupDecision) -> str:
    prior_id = decision.prior_event.prior_cluster_id if decision.prior_event else ""
    raw = "|".join(
        [
            decision.prefilter.cleaned_title,
            decision.prefilter.cleaned_summary[:300],
            str(decision.source_tier),
            prior_id,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class ClassificationCache:
    """Append-only JSONL cache storing raw Layer-3 JSON payloads by content hash."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = str(path) if path else ""
        self._entries: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        if self.path and os.path.isfile(self.path):
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("k") and isinstance(row.get("event"), dict):
                        self._entries[str(row["k"])] = row["event"]
        except OSError as exc:
            print(f"  [backfill] cache load warning ({self.path}): {exc}")

    def get(self, key: str) -> dict[str, Any] | None:
        hit = self._entries.get(key)
        if hit is not None:
            self.hits += 1
        else:
            self.misses += 1
        return hit

    def put(self, key: str, event_dict: dict[str, Any]) -> None:
        self._entries[key] = event_dict
        if not self.path:
            return
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(
                    json.dumps({"k": key, "event": event_dict}, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
        except OSError:
            pass


class CachedClassifier:
    """Wraps ``GeminiEventClassifier`` with resumable caching and pacing."""

    def __init__(
        self,
        inner: GeminiEventClassifier,
        cache: ClassificationCache,
        *,
        pace_s: float = 0.15,
    ):
        self._inner = inner
        self._cache = cache
        self._pace_s = max(0.0, float(pace_s))
        self.api_calls = 0

    def classify(
        self,
        decision: DedupDecision,
        *,
        market_context_text: str = "",
    ) -> tuple[ClassifiedEvent | None, str]:
        key = _cache_key(decision)
        cached = self._cache.get(key)
        if cached is not None:
            ev, err = parse_classified_event(
                cached,
                event_id=decision.cluster_id,
                source_tier=decision.source_tier,
                mention_count=decision.mention_count,
                layer2_is_duplicate=bool(decision.is_duplicate and not decision.tier_upgraded),
                cfg=self._inner._cfg,  # noqa: SLF001
            )
            if ev is not None:
                return ev, ""

        if self.api_calls > 0 and self._pace_s > 0:
            time.sleep(self._pace_s)
        self.api_calls += 1
        ev, err = self._inner.classify(decision, market_context_text=market_context_text)
        if ev is not None:
            self._cache.put(key, ev.to_dict())
        return ev, err


# ── Core backfill engine ─────────────────────────────────────────────────

@dataclass
class BackfillResult:
    batches_total: int = 0
    batches_processed: int = 0
    input_articles: int = 0
    l1_kept: int = 0
    l1_dropped: int = 0
    l2_unique: int = 0
    l2_duplicates: int = 0
    l3_attempted: int = 0
    l3_succeeded: int = 0
    l3_schema_failures: int = 0
    l4_passed: int = 0
    votes_up: int = 0
    votes_down: int = 0
    bullish_unconfirmed: int = 0
    cache_hits: int = 0
    api_calls: int = 0
    api_errors: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    records_by_month: dict[str, list[dict]] = field(default_factory=dict)
    raw_records_by_month: dict[str, list[dict]] = field(default_factory=dict)
    final_session_state: dict[str, Any] = field(default_factory=dict)
    final_cluster_state: dict[str, Any] = field(default_factory=dict)
    sample_representatives: list[str] = field(default_factory=list)


def _filter_batches(
    batches: list[tuple[datetime, list[dict]]],
    *,
    since: str = "",
    max_batches: int = 0,
) -> list[tuple[datetime, list[dict]]]:
    out = batches
    if since:
        cutoff = datetime.fromisoformat(since)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=_TZ_TAIPEI)
        out = [(dt, arts) for dt, arts in out if dt >= cutoff]
    if max_batches > 0:
        out = out[:max_batches]
    return out


def backfill_batches(
    batches: list[tuple[datetime, list[dict]]],
    *,
    pipeline: NewsSignalPipeline,
    market_provider: HistoricalMarketProvider | None = None,
    dry_run: bool = False,
    progress_every: int = 25,
) -> BackfillResult:
    """Replay chronological batches through *pipeline* and build v2 history logs."""
    t0 = time.perf_counter()
    res = BackfillResult(batches_total=len(batches))

    session_key = ""
    session_net = 0.0
    session_peak = 0.0
    recent_runs: list[float] = []
    active_vote = ""
    prev_batch_dt: datetime | None = None
    last_stats: dict[str, Any] = {}
    last_batch_dt: datetime | None = None
    last_run_net = 0.0
    last_scored_count = 0

    for idx, (batch_dt, articles) in enumerate(batches, start=1):
        res.batches_processed += 1
        res.input_articles += len(articles)

        if dry_run:
            l1_kept, l1_dropped, _ = pipeline.prefilter.filter_batch(articles)
            decisions = pipeline.clusterer.process_batch(l1_kept, now=batch_dt)
            to_cls = [d for d in decisions if d.needs_llm]
            dups = [d for d in decisions if d.is_duplicate]
            res.l1_kept += len(l1_kept)
            res.l1_dropped += len(l1_dropped)
            res.l2_unique += len(to_cls)
            res.l2_duplicates += len(dups)
            for d in to_cls:
                if len(res.sample_representatives) < 15:
                    res.sample_representatives.append(
                        f"[T{d.source_tier}] ({d.article.get('source', '')}) {d.prefilter.cleaned_title[:90]}"
                    )
            continue

        cur_session = rss_scorer.night_session_key(batch_dt)
        if cur_session != session_key:
            session_key = cur_session
            session_net = 0.0
            session_peak = 0.0
            recent_runs = []
            active_vote = ""
            interval_min = 30.0
        elif prev_batch_dt is not None:
            gap_min = (batch_dt - prev_batch_dt).total_seconds() / 60.0
            interval_min = max(5.0, min(240.0, gap_min))
        else:
            interval_min = 30.0

        market = market_provider.snapshot_at(batch_dt) if market_provider else None
        report = pipeline.run(
            articles,
            now=batch_dt,
            prev_session_net=session_net,
            active_vote_direction=active_vote,
            market=market,
            emit_risk_off_enabled=False,
            interval_minutes=interval_min,
        )

        res.l1_kept += report.l1_kept_count
        res.l1_dropped += report.l1_dropped_count
        res.l2_unique += report.l2_unique_count
        res.l2_duplicates += report.l2_duplicate_count
        res.l3_attempted += report.l3_attempted
        res.l3_succeeded += report.l3_succeeded
        res.l3_schema_failures += report.l3_schema_failures
        res.l4_passed += report.passed_count

        if report.api_error:
            res.api_errors.append(f"{batch_dt.isoformat(timespec='seconds')}: {report.api_error}")

        for rsig in report.routing.routed_signals:
            if rsig.reject_reason.startswith("bullish_unconfirmed:"):
                res.bullish_unconfirmed += 1

        run_net = report.routing.run_net
        session_net = report.routing.session_net
        if session_net > 0 and session_net > session_peak:
            session_peak = session_net
        elif session_net < 0 and session_net < session_peak:
            session_peak = session_net
        recent_runs = (recent_runs + [run_net])[-3:]

        direction = report.routing.vote_direction
        if not report.api_error or direction:
            active_vote = direction or ""
        if direction == "trending-up":
            res.votes_up += 1
        elif direction == "trending-down":
            res.votes_down += 1

        prev_batch_dt = batch_dt
        last_batch_dt = batch_dt
        last_stats = report.stats_dict()
        last_run_net = run_net
        last_scored_count = len(report.classified_items)

        month_key = batch_dt.astimezone(_TZ_TAIPEI).strftime("%Y%m")
        if report.classified_items:
            bucket = res.records_by_month.setdefault(month_key, [])
            for (dec, ev), rsig in zip(report.classified_items, report.routing.routed_signals):
                bucket.append(
                    rss_news_log.build_record(
                        dec.article,
                        rss_scorer._v2_legacy_score_dict(ev, rsig),  # noqa: SLF001
                        scored_at=batch_dt,
                        session=session_key,
                        run_net=run_net,
                        session_net=session_net,
                        run_scale=1.0,
                        v2=rss_scorer._v2_record_payload(ev, rsig),  # noqa: SLF001
                    )
                )
        if report.raw_outcomes:
            raw_bucket = res.raw_records_by_month.setdefault(month_key, [])
            for item in report.raw_outcomes:
                raw_bucket.append(
                    rss_news_log.build_raw_record(
                        item["article"],
                        scored_at=batch_dt,
                        session=session_key,
                        pipeline_stage=item["pipeline_stage"],
                        source_tier=item.get("source_tier", 3),
                        drop_reason=item.get("drop_reason", ""),
                        cluster_id=item.get("cluster_id", ""),
                        dup_method=item.get("dup_method", ""),
                        v2_action=item.get("v2_action", ""),
                        v2_category=item.get("v2_category", ""),
                        v2_direction=item.get("v2_direction", ""),
                        v2_materiality=item.get("v2_materiality", 0),
                    )
                )

        if progress_every > 0 and (idx % progress_every == 0 or idx == len(batches)):
            print(
                f"  [{idx}/{len(batches)}] {batch_dt:%Y-%m-%d %H:%M} | "
                f"L1={res.l1_kept}/{res.input_articles} L2_uniq={res.l2_unique} "
                f"L3_ok={res.l3_succeeded} L4_pass={res.l4_passed} "
                f"| net={session_net:+.2f} vote={active_vote or 'none'}"
            )

    res.elapsed_s = time.perf_counter() - t0
    if isinstance(pipeline.classifier, CachedClassifier):
        res.cache_hits = pipeline.classifier._cache.hits  # noqa: SLF001
        res.api_calls = pipeline.classifier.api_calls

    if not dry_run and last_batch_dt is not None:
        res.final_session_state = {
            "pipeline_v2_mode": "live",
            "session_key": session_key,
            "session_net": session_net,
            "session_peak": session_peak,
            "recent_runs": recent_runs,
            "v2_active_vote": active_vote,
            "pipeline_v2_last_stats": last_stats,
            "last_net_score": last_run_net,
            "last_articles_scored": last_scored_count,
            "last_check": last_batch_dt.isoformat(timespec="seconds"),
        }
        res.final_cluster_state = pipeline.dump_state()

    return res


def write_backfill_outputs(
    result: BackfillResult,
    *,
    target_log_base: str,
    in_place: bool = False,
    out_dir: str = "",
    state_file: str = "",
    update_state: bool = False,
    write_raw_archive: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write backfilled monthly JSONL logs (with automatic v1 backup on in-place),
    seed the unified raw archive ``raw/raw_news_YYYYMM.jsonl`` (never overwriting
    an existing raw archive), and optionally update ``rss_scorer_state.json`` +
    ``rss_pipeline_v2_state.json``.
    """
    base_path = Path(target_log_base)
    dest_dir = base_path.parent if in_place else Path(out_dir or (str(base_path.parent) + "_v2_backfill"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = base_path.stem or "news_log"
    if stem == Path(rss_news_log.RAW_FILENAME).stem:
        stem = Path(rss_news_log.LOG_FILENAME).stem
    suffix = base_path.suffix or ".jsonl"

    # 1. Write to staging files first so an error never leaves partial logs
    staged: list[tuple[Path, Path]] = []
    written_files: list[str] = []
    for month in sorted(result.records_by_month):
        final_p = dest_dir / f"{stem}_{month}{suffix}"
        tmp_p = dest_dir / f".{stem}_{month}{suffix}.backfill.tmp"
        with open(tmp_p, "w", encoding="utf-8", newline="\n") as f:
            for rec in result.records_by_month[month]:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        staged.append((tmp_p, final_p))

    backup_dir = ""
    if in_place:
        existing = rss_news_log.log_files(str(base_path))
        if existing:
            stamp = (now or datetime.now(_TZ_TAIPEI)).strftime("%Y%m%d_%H%M%S")
            bdir = dest_dir / f"backup_v1_{stamp}"
            bdir.mkdir(parents=True, exist_ok=True)
            for old_file in existing:
                shutil.move(old_file, str(bdir / Path(old_file).name))
            backup_dir = str(bdir)

    for tmp_p, final_p in staged:
        os.replace(tmp_p, final_p)
        written_files.append(str(final_p))

    # 2. Seed unified raw archive (<dest_dir>/raw/raw_news_YYYYMM.jsonl) if not already
    # reading directly from raw/ and not overwriting an existing raw file.
    raw_files: list[str] = []
    raw_dir = dest_dir / rss_news_log.RAW_DIRNAME
    reading_from_raw = base_path.resolve().parent == raw_dir.resolve()
    if write_raw_archive and result.raw_records_by_month and not reading_from_raw:
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_stem = Path(rss_news_log.RAW_FILENAME).stem
        for month in sorted(result.raw_records_by_month):
            raw_final = raw_dir / f"{raw_stem}_{month}.jsonl"
            if raw_final.exists():
                continue  # never overwrite an existing raw archive
            raw_tmp = raw_dir / f".{raw_stem}_{month}.jsonl.backfill.tmp"
            with open(raw_tmp, "w", encoding="utf-8", newline="\n") as f:
                for rec in result.raw_records_by_month[month]:
                    f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(raw_tmp, raw_final)
            raw_files.append(str(raw_final))

    state_updated = False
    sidecar_path = ""
    if update_state and state_file and result.final_session_state:
        state = rss_scorer.load_state(state_file)
        state.update(result.final_session_state)
        state.pop("pipeline_v2_clusters", None)
        rss_scorer.save_state(state_file, state)
        state_updated = True
        sidecar_path = str(Path(state_file).parent / rss_scorer.PIPELINE_V2_STATE_FILENAME)
        rss_scorer._save_v2_clusters(sidecar_path, result.final_cluster_state)  # noqa: SLF001

    return {
        "written_files": written_files,
        "raw_files": raw_files,
        "backup_dir": backup_dir,
        "state_updated": state_updated,
        "sidecar_path": sidecar_path,
    }


def format_summary(res: BackfillResult, *, dry_run: bool) -> str:
    n_in = max(1, res.input_articles)
    saved = max(0, res.input_articles - res.l2_unique)
    lines = [
        f"=== News Pipeline v2 Backfill {'(DRY-RUN)' if dry_run else 'Complete'} ===",
        f"Batches processed   : {res.batches_processed}/{res.batches_total}",
        f"Input v1 rows       : {res.input_articles:,}",
        f"Layer 1 kept/dropped: {res.l1_kept:,} / {res.l1_dropped:,} ({res.l1_dropped / n_in * 100:.1f}% dropped)",
        f"Layer 2 unique/dups : {res.l2_unique:,} / {res.l2_duplicates:,} (LLM savings: {saved / n_in * 100:.1f}%)",
    ]
    if dry_run:
        est_min = res.l2_unique * 1.5 / 60.0
        lines.append(f"Estimated Gemini LLM calls : {res.l2_unique:,} (~{est_min:.1f} min at ~1.5s/call)")
        if res.sample_representatives:
            lines.append("\nSample event representatives that would be classified:")
            for s in res.sample_representatives:
                lines.append(f"  - {s}")
    else:
        total_rows = sum(len(v) for v in res.records_by_month.values())
        lines.extend(
            [
                f"Layer 3 succeeded   : {res.l3_succeeded:,}/{res.l3_attempted:,} (schema failures: {res.l3_schema_failures})",
                f"Layer 4 passed      : {res.l4_passed:,} (bullish unconfirmed held: {res.bullish_unconfirmed})",
                f"Batch votes         : up={res.votes_up}, down={res.votes_down}",
                f"Gemini API calls    : {res.api_calls:,} (cache hits: {res.cache_hits:,})",
                f"Output JSONL rows   : {total_rows:,} across {len(res.records_by_month)} month(s)",
                f"Elapsed             : {res.elapsed_s:.1f}s",
            ]
        )
        if res.api_errors:
            lines.append(f"API errors ({len(res.api_errors)}): {res.api_errors[:3]}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill historical W3 news logs with Pipeline v2 for Dashboard parity"
    )
    ap.add_argument("--settings", default="", help="Path to settings.yaml (resolves API key & paths)")
    ap.add_argument("--input-log", default="", help="Historical news_log.jsonl base path")
    ap.add_argument("--out-dir", default="", help="Output directory when not running --in-place")
    ap.add_argument(
        "--in-place",
        action="store_true",
        help="Back up existing v1 news_log*.jsonl into backup_v1_<ts>/ and replace with v2 logs",
    )
    ap.add_argument(
        "--update-state",
        action="store_true",
        help="Also update rss_scorer_state.json + rss_pipeline_v2_state.json from final batch",
    )
    ap.add_argument("--state-file", default="", help="Override rss_scorer_state.json path")
    ap.add_argument("--bots-dir", default="", help="Optional data/live dir for historical TXF/TMF candles")
    ap.add_argument("--config", default="", help="Override config/news_filter.yaml path")
    ap.add_argument("--embedder", choices=["gemini", "lexical", "none"], default="", help="Override Layer-2 embedder")
    ap.add_argument("--since", default="", help="Only backfill batches on/after YYYY-MM-DD (TPE)")
    ap.add_argument("--max-batches", type=int, default=0, help="Limit batches processed (smoke test)")
    ap.add_argument("--cache", default="", help="Resumable JSONL classification cache path")
    ap.add_argument("--no-cache", action="store_true", help="Disable classification cache")
    ap.add_argument("--sleep", type=float, default=0.15, help="Seconds to pause between Gemini calls")
    ap.add_argument("--dry-run", action="store_true", help="Run L1+L2 only (zero Gemini calls, no file writes)")
    args = ap.parse_args()

    cfg: dict[str, Any] = {}
    if args.settings:
        settings = rss_scorer.load_settings(args.settings)
        cfg = rss_scorer.get_news_config(settings)

    state_file = args.state_file or cfg.get("state_file") or ""
    input_log = (
        args.input_log
        or cfg.get("news_log_file")
        or (rss_news_log.default_path(state_file) if state_file else "")
    )
    if not input_log:
        ap.error("Provide --input-log or --settings with news.rss_state_file / news.rss_news_log")

    api_key = cfg.get("gemini_api_key") or os.environ.get("GOOGLE_API_KEY") or ""
    if not args.dry_run and not api_key:
        print("ERROR: Gemini API key required (set ai.google_api_key in --settings or GOOGLE_API_KEY env)")
        return 1

    all_batches = replay_news_pipeline.load_jsonl_batches(input_log)
    batches = _filter_batches(all_batches, since=args.since, max_batches=args.max_batches)
    if not batches:
        print("No batches matched the input/date filter.")
        return 0

    p_cfg = load_pipeline_config(args.config or cfg.get("filter_config") or None)
    emb_override = args.embedder or cfg.get("embedding_provider") or ("lexical" if args.dry_run else "")
    raw_clf = GeminiEventClassifier(
        p_cfg,
        api_key=api_key,
        model_override=cfg.get("rss_model") or "",
    )

    dest_dir = Path(input_log).parent if args.in_place else Path(args.out_dir or (str(Path(input_log).parent) + "_v2_backfill"))
    cache_path = "" if (args.no_cache or args.dry_run) else (args.cache or str(dest_dir / _DEFAULT_CACHE_NAME))
    clf = raw_clf if args.dry_run else CachedClassifier(raw_clf, ClassificationCache(cache_path), pace_s=args.sleep)

    pipe = NewsSignalPipeline(
        p_cfg,
        api_key="" if args.dry_run and emb_override != "gemini" else api_key,
        embedder_provider_override=emb_override,
        classifier=clf,
    )

    since_day = (batches[0][0] - timedelta(days=1)).strftime("%Y%m%d") if batches else ""
    market_provider = HistoricalMarketProvider.from_bots_dir(args.bots_dir, since_day=since_day)
    if market_provider:
        print(f"Loaded {len(market_provider.candles)} historical 30m candles for {market_provider.symbol}")

    result = backfill_batches(
        batches,
        pipeline=pipe,
        market_provider=market_provider,
        dry_run=args.dry_run,
    )
    print(format_summary(result, dry_run=args.dry_run))

    if not args.dry_run:
        out_info = write_backfill_outputs(
            result,
            target_log_base=input_log,
            in_place=args.in_place,
            out_dir=str(dest_dir),
            state_file=state_file,
            update_state=args.update_state,
        )
        print(f"Written files ({len(out_info['written_files'])}): {', '.join(out_info['written_files'])}")
        if out_info.get("raw_files"):
            print(f"Preserved raw archive ({len(out_info['raw_files'])}): {', '.join(out_info['raw_files'])}")
        if out_info["backup_dir"]:
            print(f"Backed up v1 logs to: {out_info['backup_dir']}")
        if out_info["state_updated"]:
            print(f"Updated W3 state: {state_file} (+ sidecar {out_info['sidecar_path']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
