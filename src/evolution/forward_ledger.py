"""Forward ledger: did evolution's verdicts carry forward information?

Read-only, report-only. Nothing here gates a promotion, writes a bot
directory, or calls a model. It answers the one question the weekly
pipeline never asked itself: after a Saturday verdict, did the
candidate actually beat the incumbent on the bars that came later?

For every past attempt in ``evolution_history.jsonl`` that reached a
verdict (``final_status`` pass or fail):

* resolve the incumbent's source (``source_hash``) and, when the
  attempt built one, the candidate's (``candidate_source_hash``; for
  older knob-mode rows without it, rebuilt from the incumbent source
  plus ``directives.knobs``);
* backtest both over ALL supplied bars, so indicators are warm on the
  proposal date, and keep only trades that ENTER after the proposal
  timestamp — the forward window;
* score ``delta = candidate forward P&L − incumbent forward P&L``.

``summarize`` reports, per verdict, how often the candidate beat the
incumbent forward (the hit rate). If PASS hits no more often than FAIL,
the gate carried no forward information. Rows with no candidate (a
grid that picked nothing, a refused plan) still score the incumbent's
own forward P&L, which shows whether a decaying incumbent was left in
place.

Fill model: ``next_open`` by default, the GUI and live-paper parity
model. The weekly validation itself still runs ``on_close`` (the
engine default); ``fill_mode`` lets a caller reproduce that.

Timestamps: history ``ts`` is the bot machine's local wall clock;
trades are TPE strings ``YYYY-MM-DD HH:MM``. Rows written from this
change on carry ``ts_utc_offset_min`` and are converted with it. Older
rows use ``ts_offset_hours`` (default 0). That default is right for
Steven: its Saturday runs are triggered at >= 05:05 TPE and their
``ts`` reads 05:05:25-05:09 (2026-10-31, 2026-11-07), so its clock is
TPE. This PC's clock is UTC-7: pass ``--ts-offset-hours 15`` for old
local rows.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

SCORED = "scored"
_VERDICTS = ("pass", "fail")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class LedgerEntry:
    """One past attempt worth scoring forward."""
    bot: str
    ts: str
    final_status: str
    reason_kind: str
    incumbent_class: str
    incumbent_hash: str
    candidate_class: str = ""
    candidate_hash: str = ""
    knobs: dict | None = None
    ts_utc_offset_min: int | None = None


@dataclass
class ForwardScore:
    entry: LedgerEntry
    status: str
    detail: str = ""
    start: str = ""
    end: str = ""
    inc_n: int = 0
    inc_pnl: int = 0
    cand_n: int | None = None
    cand_pnl: int | None = None
    candidate_origin: str = ""

    @property
    def delta(self) -> int | None:
        if self.status != SCORED or self.cand_pnl is None:
            return None
        return int(self.cand_pnl) - int(self.inc_pnl)

    def to_dict(self) -> dict:
        out = asdict(self)
        out["delta"] = self.delta
        return out


# ── sources ──────────────────────────────────────────────────────────


class SourceResolver:
    """sha256 → source text, from the bot's store and any extra folders.

    Extra folders are scanned for ``*.py`` (a StrategyStore directory, a
    copy of another bot's ``evolution_sources``). ``extra_sources`` are
    raw texts (e.g. ``source_code`` rows read from a StrategyPool db).
    A class-name index is kept as a last resort for rows that predate
    ``candidate_source_hash`` (changelog PASSes, or a missing hash file).
    It fails closed: once two different bodies define the same class name
    (every weekly incumbent version lands in ``evolution_sources/``),
    ``by_class`` returns None and the row is reported as ambiguous instead
    of being scored against whichever body sorts first.
    """

    def __init__(self, *, bot_dirs=(), extra_dirs=(), extra_sources=()):
        self._by_hash: dict[str, str] = {}
        self._by_class: dict[str, set[str]] = {}
        from .history import SOURCES_DIRNAME
        folders = [os.path.join(os.fspath(d), SOURCES_DIRNAME) for d in bot_dirs]
        folders += [os.fspath(d) for d in extra_dirs]
        for folder in folders:
            if not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                if not name.endswith(".py"):
                    continue
                try:
                    with open(os.path.join(folder, name), "r",
                              encoding="utf-8", newline="") as handle:
                        self.add(handle.read())
                except OSError:
                    continue
        for text in extra_sources:
            self.add(text)

    def add(self, text: str) -> str:
        if not text:
            return ""
        digest = _sha(text)
        self._by_hash.setdefault(digest, text)
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("class ") and "(" in stripped:
                name = stripped[6:stripped.index("(")].strip()
                if name:
                    self._by_class.setdefault(name, set()).add(digest)
        return digest

    def by_hash(self, digest: str) -> str | None:
        return self._by_hash.get(digest or "")

    def class_body_count(self, name: str) -> int:
        """How many distinct bodies define ``class name(``."""
        return len(self._by_class.get(name or "", ()))

    def by_class(self, name: str) -> str | None:
        """The one body defining ``name``; None when absent or ambiguous."""
        digests = self._by_class.get(name or "", set())
        if len(digests) != 1:
            return None
        return self._by_hash.get(next(iter(digests)))


# ── entries ──────────────────────────────────────────────────────────


def entries_from_history(records) -> list[LedgerEntry]:
    """Attempts that reached a verdict, oldest first. Malformed rows drop."""
    found: list[LedgerEntry] = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        status = str(rec.get("final_status") or "")
        if status not in _VERDICTS:
            continue
        ts = str(rec.get("ts") or "")
        if len(ts) < 16:
            continue
        directives = rec.get("directives") if isinstance(rec.get("directives"), dict) else {}
        knobs = directives.get("knobs") if isinstance(directives.get("knobs"), dict) else None
        offset = rec.get("ts_utc_offset_min")
        if isinstance(offset, bool) or not isinstance(offset, int):
            offset = None
        found.append(LedgerEntry(
            bot=str(rec.get("bot") or ""),
            ts=ts,
            final_status=status,
            reason_kind=str(rec.get("reason_kind") or ""),
            incumbent_class=str(rec.get("class") or ""),
            incumbent_hash=str(rec.get("source_hash") or ""),
            candidate_class=str(rec.get("candidate_class") or ""),
            candidate_hash=str(rec.get("candidate_source_hash") or ""),
            knobs=knobs or None,
            ts_utc_offset_min=offset,
        ))
    found.sort(key=lambda e: e.ts)
    return found


def entries_from_changelog(entries) -> list[LedgerEntry]:
    """PASS rows from ``data/changelog.json`` (pre-history installs).

    ``evolution_history.jsonl`` only exists from v2.19.9 on. Earlier
    PASSes (V3Evo1 08-01, V2Evo1 09-26, the local 0422 Evo1 10-10) are
    recorded only as AI changelog rows whose class changed. They carry
    class names, not hashes, so sources resolve by class name (pass the
    StrategyStore folder as an extra source dir). ``date`` is the
    machine's local clock.
    """
    from .paper_gate import RELATIVE_EVAL_MARKER
    found: list[LedgerEntry] = []
    for row in entries or []:
        if not isinstance(row, dict) or row.get("initiated_by") != "ai":
            continue
        before = str(row.get("version_before") or "")
        after = str(row.get("version_after") or "")
        summary = str(row.get("change_summary") or "")
        if not before or not after or before == after:
            continue
        if summary.lstrip().startswith(RELATIVE_EVAL_MARKER):
            continue
        ts = str(row.get("date") or "")
        if len(ts) < 16:
            continue
        knobs = None
        p_before, p_after = row.get("params_before"), row.get("params_after")
        if isinstance(p_before, dict) and isinstance(p_after, dict):
            knobs = {k: v for k, v in p_after.items() if p_before.get(k) != v} or None
        found.append(LedgerEntry(
            bot=str(row.get("strategy") or ""), ts=ts, final_status="pass",
            reason_kind="changelog", incumbent_class=before, incumbent_hash="",
            candidate_class=after, candidate_hash="", knobs=knobs))
    found.sort(key=lambda e: e.ts)
    return found


def merge_entries(history: list[LedgerEntry],
                  changelog: list[LedgerEntry]) -> list[LedgerEntry]:
    """History rows win; a changelog PASS already in history is dropped."""
    seen = {(e.bot, e.candidate_class, e.ts[:10]) for e in history
            if e.candidate_class}
    extra = [e for e in changelog
             if (e.bot, e.candidate_class, e.ts[:10]) not in seen]
    return sorted([*history, *extra], key=lambda e: e.ts)


# ── scoring ──────────────────────────────────────────────────────────


def _parse_ts(ts: str, offset_hours: float) -> datetime | None:
    text = ts.replace("T", " ")
    for width, fmt in ((19, "%Y-%m-%d %H:%M:%S"), (16, "%Y-%m-%d %H:%M")):
        try:
            return datetime.strptime(text[:width], fmt) + timedelta(
                hours=offset_hours)
        except ValueError:
            continue
    return None


def _entry_after(trade, start: datetime) -> bool:
    text = str(getattr(trade, "entry_dt", "") or "")
    try:
        return datetime.strptime(text[:16], "%Y-%m-%d %H:%M") > start
    except ValueError:
        return False


def bars_for_strategy(strategy, bars) -> list:
    """``bars`` at the strategy's own timeframe.

    The engine aggregates a 1-min feed only for strategies that declare
    ``htf_intervals`` (``BacktestEngine._prepare_bars``). A single-TF
    15-min strategy handed bot-dir 1-min CSVs would otherwise run on
    1-min bars. Finer bars are aggregated up to the primary interval;
    MTF strategies and bars already at (or above) it pass through.
    """
    from src.backtest.engine import strategy_primary_interval
    from src.live.bar_aggregator import aggregate_bars
    bars = list(bars)
    if not bars or getattr(strategy, "htf_intervals", None):
        return bars
    primary = strategy_primary_interval(strategy)
    if primary > 0 and 0 < int(bars[0].interval or 0) < primary:
        return aggregate_bars(bars, primary)
    return bars


def backtest_trades(cls, bars, point_value: int, *, fill_mode: str = "next_open",
                    capital_base: int = 100_000) -> list:
    """Default-constructed ``cls`` over ``bars``. Raises on engine errors."""
    from src.backtest.engine import BacktestEngine
    strategy = cls()
    feed = bars_for_strategy(strategy, bars)
    engine = BacktestEngine(
        strategy, point_value=int(point_value),
        max_bars=max(5000, len(feed)), fill_mode=fill_mode,
        initial_balance=int(capital_base))
    return list(engine.run(feed).trades)


def _load(source: str):
    from src.ai.code_sandbox import load_strategy_from_source
    return load_strategy_from_source(source)


def _candidate_source(entry: LedgerEntry, resolver: SourceResolver,
                      incumbent_source: str) -> tuple[str | None, str]:
    if entry.candidate_hash:
        text = resolver.by_hash(entry.candidate_hash)
        if text is not None:
            return text, "stored"
    if entry.candidate_class:
        text = resolver.by_class(entry.candidate_class)
        if text is not None:
            return text, "by_class"
        count = resolver.class_body_count(entry.candidate_class)
        if count > 1:
            return None, f"ambiguous ({count} bodies define {entry.candidate_class})"
    if entry.knobs and incumbent_source and entry.incumbent_class:
        try:
            from .knobs import rewrite_knob_source
            return rewrite_knob_source(
                incumbent_source, entry.incumbent_class,
                entry.incumbent_class + "LedgerKnob", dict(entry.knobs)), "knob_rebuilt"
        except Exception:
            pass
    return None, ""


def score_entry(entry: LedgerEntry, bars, resolver: SourceResolver, *,
                point_value: int, fill_mode: str = "next_open",
                capital_base: int = 100_000, ts_offset_hours: float = 0.0,
                min_forward_days: int = 0, backtest=None, load=None,
                cache: dict | None = None) -> ForwardScore:
    """Forward P&L of the incumbent and (if any) the candidate. Never raises."""
    run = backtest or backtest_trades
    loader = load or _load
    cache = {} if cache is None else cache
    if entry.ts_utc_offset_min is not None:
        ts_offset_hours = (480 - int(entry.ts_utc_offset_min)) / 60.0
    start = _parse_ts(entry.ts, ts_offset_hours)
    if start is None:
        return ForwardScore(entry, "bad_ts", entry.ts)
    if not bars:
        return ForwardScore(entry, "no_bars")
    last = bars[-1].dt
    out = ForwardScore(entry, SCORED, start=start.strftime("%Y-%m-%d %H:%M"),
                       end=last.strftime("%Y-%m-%d %H:%M"))
    if last <= start + timedelta(days=max(0, int(min_forward_days))):
        out.status = "no_forward_bars"
        return out
    inc_src = resolver.by_hash(entry.incumbent_hash)
    if inc_src is None:
        inc_src = resolver.by_class(entry.incumbent_class)
        count = resolver.class_body_count(entry.incumbent_class)
        if inc_src is None and count > 1:
            out.status = "ambiguous_incumbent_source"
            out.detail = (f"{count} bodies define {entry.incumbent_class}; "
                          "no hash to pick the one live at ts")
            return out
    if inc_src is None:
        out.status = "no_incumbent_source"
        return out

    def _trades(source: str) -> list:
        key = _sha(source)
        if key not in cache:
            cache[key] = run(loader(source), bars, point_value,
                             fill_mode=fill_mode, capital_base=capital_base)
        return cache[key]

    try:
        inc = [t for t in _trades(inc_src) if _entry_after(t, start)]
    except Exception as exc:
        out.status = "incumbent_error"
        out.detail = f"[{type(exc).__name__}] {exc}"
        return out
    out.inc_n = len(inc)
    out.inc_pnl = int(sum(int(getattr(t, "pnl", 0) or 0) for t in inc))
    cand_src, origin = _candidate_source(entry, resolver, inc_src)
    if cand_src is None:
        # Incumbent-only row: a grid no-pick, a refused or failed codegen,
        # or a class-name lookup that matched more than one body.
        out.detail = ("candidate source " + origin) if origin else "no candidate source"
        return out
    out.candidate_origin = origin
    try:
        cand = [t for t in _trades(cand_src) if _entry_after(t, start)]
    except Exception as exc:
        out.detail = f"candidate error [{type(exc).__name__}] {exc}"
        return out
    out.cand_n = len(cand)
    out.cand_pnl = int(sum(int(getattr(t, "pnl", 0) or 0) for t in cand))
    return out


# ── summary ──────────────────────────────────────────────────────────


@dataclass
class VerdictStats:
    n: int = 0
    hits: int = 0
    total_delta: int = 0
    deltas: list = field(default_factory=list)

    @property
    def hit_rate(self) -> float | None:
        return (self.hits / self.n) if self.n else None

    @property
    def mean_delta(self) -> float | None:
        return (self.total_delta / self.n) if self.n else None


def summarize(scores) -> dict:
    """Hit rate (candidate beat incumbent forward) per verdict."""
    by: dict[str, VerdictStats] = {v: VerdictStats() for v in _VERDICTS}
    statuses: dict[str, int] = {}
    incumbent_only = 0
    for score in scores:
        statuses[score.status] = statuses.get(score.status, 0) + 1
        if score.status != SCORED:
            continue
        delta = score.delta
        if delta is None:
            incumbent_only += 1
            continue
        stats = by[score.entry.final_status]
        stats.n += 1
        stats.hits += 1 if delta > 0 else 0
        stats.total_delta += delta
        stats.deltas.append(delta)
    p, f = by["pass"].hit_rate, by["fail"].hit_rate
    return {
        "pass": by["pass"],
        "fail": by["fail"],
        "incumbent_only": incumbent_only,
        "statuses": statuses,
        # PASS hit rate minus FAIL hit rate: ≤ 0 means the gate told us
        # nothing about the future. None until both sides are scored.
        "gate_lift": (p - f) if (p is not None and f is not None) else None,
    }


def _pct(value) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _num(value) -> str:
    return "n/a" if value is None else f"{value:+,.0f}"


def format_report(scores, summary: dict, *, point_value: int,
                  fill_mode: str) -> str:
    lines = [
        "EVO FORWARD LEDGER — report only; nothing was gated, applied, or written.",
        f"fill_mode={fill_mode} point_value={point_value} "
        "(forward = trades entering after the proposal timestamp)",
        "",
        f"{'ts':16} {'bot':28} {'verdict':7} {'kind':15} {'inc n/P&L':>14} "
        f"{'cand n/P&L':>14} {'Δ':>9}  origin/status",
    ]
    for s in scores:
        e = s.entry
        inc = f"{s.inc_n}/{s.inc_pnl:+,}" if s.status == SCORED else "-"
        cand = (f"{s.cand_n}/{s.cand_pnl:+,}"
                if s.cand_pnl is not None else "-")
        tail = s.candidate_origin or s.status
        if s.status != SCORED or s.detail:
            tail = (s.status if s.status != SCORED else tail) + (
                f" ({s.detail})" if s.detail else "")
        lines.append(
            f"{e.ts[:16]:16} {e.bot[:28]:28} {e.final_status:7} "
            f"{e.reason_kind[:15]:15} {inc:>14} {cand:>14} "
            f"{_num(s.delta):>9}  {tail}")
    p, f = summary["pass"], summary["fail"]
    lines += [
        "",
        f"PASS vs incumbent forward: hit rate {_pct(p.hit_rate)} "
        f"({p.hits}/{p.n}), mean Δ {_num(p.mean_delta)}, total Δ {_num(p.total_delta)}",
        f"FAIL vs incumbent forward: hit rate {_pct(f.hit_rate)} "
        f"({f.hits}/{f.n}), mean Δ {_num(f.mean_delta)}, total Δ {_num(f.total_delta)}",
        f"gate lift (PASS hit − FAIL hit): {_pct(summary['gate_lift'])}",
        f"incumbent-only rows (no candidate to compare): {summary['incumbent_only']}",
        "not scored: " + (", ".join(
            f"{k}={v}" for k, v in sorted(summary["statuses"].items())
            if k != SCORED) or "none"),
    ]
    return "\n".join(lines)
