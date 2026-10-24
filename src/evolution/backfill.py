"""D7/P3 validation-bar backfill.

Reads broker K-line bars (the same feed live uses), drops any bar that
falls on an exchange closure, and writes 1-minute history plus the
15m/1H/H4/1D aggregates under ``data/backfill/`` only.

Dry-run is the default. ``write=True`` (CLI ``--write``) is required
before any file is created, and every path is refused unless it
resolves inside ``data/backfill/``. The live tree — other ``data/``
children, settings, state, votes, locks — is never written.

Closures come from ``src.market_data.holidays`` (weekends, Taiwan
public holidays, and the operator ``taifex_closures.yaml``) plus the
market-wide announcement list below. A closed session is omitted.
Missing bars are reported as gaps. Nothing is forward-filled.

Nova Q4 (2026-10-24). This module does not call the broker and does
not download a third-party vendor feed.
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.backtest.data_loader import load_bars_from_csv
from src.live.bar_aggregator import aggregate_bars
from src.market_data.holidays import (
    is_taifex_holiday,
    load_closure_override_dates,
)
from src.market_data.kline_config import SYMBOL_CONFIG
from src.market_data.models import Bar
from src.market_data.sessions import (
    DAY_CLOSE,
    DAY_OPEN,
    NIGHT_CLOSE,
    NIGHT_OPEN,
)

# Market span from Nova Q4: first recorded trade, minus warmup of at
# least 150 native bars (roughly mid-April). Not a per-bot constant.
FIRST_TRADE_DATE = date(2026, 5, 7)
WARMUP_NATIVE_BARS = 150
DEFAULT_BACKFILL_START = date(2026, 4, 15)
FOLD_DAYS = 14
MIN_FOLDS = 8
MAX_FOLDS = 10
CROSS_CHECK_TOLERANCE = 0.001  # 0.1% of the official daily close

RES_INTERVAL = {"1m": 60, "15m": 900}
_DERIVED: dict[str, tuple[tuple[str, int], ...]] = {
    "1m": (
        ("bars_1m.csv", 60),
        ("bars_15m.csv", 900),
        ("bars_1H.csv", 3600),
        ("bars_H4.csv", 14400),
        ("bars_1D.csv", 86400),
    ),
    "15m": (
        ("bars_15m.csv", 900),
        ("bars_1H.csv", 3600),
        ("bars_H4.csv", 14400),
        ("bars_1D.csv", 86400),
    ),
}
_REASONS = frozenset({"holiday", "typhoon", "halt"})
_TAIPEI = ZoneInfo("Asia/Taipei")


class BackfillPathError(ValueError):
    """The target resolves outside ``data/backfill/``."""


class BackfillError(ValueError):
    """The request cannot be run (symbol, dates, source, resolution)."""


@dataclass(frozen=True)
class Announcement:
    """One market-wide closure from a TAIFEX announcement.

    ``session`` is ``day``, ``night``, or ``both``. ``reason`` is
    ``holiday``, ``typhoon``, or ``halt``.
    """

    day: date
    session: str
    reason: str
    source: str


@dataclass(frozen=True)
class ClosureRow:
    day: date
    session: str
    reason: str
    source: str


@dataclass(frozen=True)
class Gap:
    day: date
    session: str
    missing: int
    sample: tuple[str, ...] = ()


@dataclass
class BackfillReport:
    symbol: str
    prefix: str
    resolution: str
    start: date
    end: date
    bars_in: int
    bars_kept: int
    bars_skipped_closure: int
    bars_outside_span: int
    bars_off_session: int
    bars_off_grid: int
    bars_duplicate: int
    sessions_expected: int
    sessions_closed: int
    gaps: list[Gap] = field(default_factory=list)
    closures: list[ClosureRow] = field(default_factory=list)
    cross_check: str = "skipped"
    cross_check_mismatches: list[str] = field(default_factory=list)
    fold_count: int = 0
    meets_min_folds: bool = False
    first_bar: str = ""
    last_bar: str = ""
    warnings: list[str] = field(default_factory=list)
    ok: bool = False
    write: bool = False
    wrote: bool = False
    files_written: list[str] = field(default_factory=list)
    message: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=_json_default)

    def format_text(self) -> str:
        gap_n = len(self.gaps)
        lines = [
            f"backfill {self.symbol} ({self.prefix}) res={self.resolution} "
            f"{self.start.isoformat()}..{self.end.isoformat()}",
            f"kept {self.bars_kept} source bars, "
            f"skipped {self.bars_skipped_closure} closure bars, "
            f"outside span {self.bars_outside_span}",
            f"sessions expected {self.sessions_expected}, "
            f"closed {self.sessions_closed}, unexpected gaps {gap_n}",
            f"folds {self.fold_count} ({FOLD_DAYS}d, cap {MAX_FOLDS})"
            + ("" if self.meets_min_folds else f" — under {MIN_FOLDS}"),
            f"cross-check {self.cross_check}",
            "wrote data/backfill/" if self.wrote else "dry-run: wrote nothing",
        ]
        if self.gaps:
            sample = ", ".join(
                f"{g.day.isoformat()} {g.session} missing {g.missing}"
                for g in self.gaps[:8]
            )
            lines.append(f"gaps: {sample}")
        if self.cross_check_mismatches:
            lines.append("mismatches: " + "; ".join(self.cross_check_mismatches[:8]))
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        if self.message:
            lines.append(self.message)
        return "\n".join(lines)


# Nova Q4 known dates. All four are already closed by the weekend or
# public-holiday calendar; the rows exist so the sidecar records the
# announcement reason (typhoon vs holiday). A later weekday halt belongs
# in taifex_closures.yaml or in this list — never in a bot constant.
ANNOUNCED_CLOSURES: tuple[Announcement, ...] = (
    Announcement(date(2026, 9, 26), "both", "typhoon", "taifex_announcement"),
    Announcement(date(2026, 9, 28), "both", "holiday", "taifex_announcement"),
    Announcement(date(2026, 10, 10), "both", "holiday", "taifex_announcement"),
    Announcement(date(2026, 10, 11), "both", "holiday", "taifex_announcement"),
)


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def default_repo_root() -> Path:
    """Checkout root (this file lives at ``src/evolution/backfill.py``)."""
    return Path(__file__).resolve().parents[2]


def backfill_root(repo_root: Path) -> Path:
    return Path(os.path.normpath(str(repo_root.resolve() / "data" / "backfill")))


def _lexical(path: Path) -> Path:
    """Collapse ``.`` and ``..`` without following symlinks."""
    return Path(os.path.normpath(str(path)))


def resolve_output_dir(requested: Path | str | None, repo_root: Path) -> Path:
    """Return ``requested`` if it is ``data/backfill`` or a child of it.

    A lexical ``..`` escape and a symlink that resolves outside
    ``data/backfill`` both raise ``BackfillPathError``. Nothing is created.
    """
    repo = repo_root.resolve()
    root = backfill_root(repo)
    if requested is None or requested == "":
        lexical = root
    else:
        raw = Path(requested)
        lexical = raw if raw.is_absolute() else _lexical(repo / raw)
        lexical = _lexical(lexical)
    if lexical != root and root not in lexical.parents:
        raise BackfillPathError(
            f"refusing path outside data/backfill/: {lexical}"
        )
    resolved = lexical.resolve()
    try:
        rel = resolved.relative_to(repo)
    except ValueError as exc:
        raise BackfillPathError(
            f"refusing path outside data/backfill/: {resolved}"
        ) from exc
    if rel.parts[:2] != ("data", "backfill"):
        raise BackfillPathError(
            f"refusing path outside data/backfill/ "
            f"(resolves to {rel.as_posix()})"
        )
    return resolved


def canonical_symbol(name: str) -> tuple[str, str]:
    """Return ``(quote_symbol, file_prefix)`` from ``SYMBOL_CONFIG``.

    Accepts the quote symbol (``TX00``), the cache prefix (``TXF1``),
    or the TAIFEX commodity id (``TX``). Bot names are not recognized.
    """
    key = name.strip()
    if key in SYMBOL_CONFIG:
        return key, SYMBOL_CONFIG[key]["prefix"]
    for quote, cfg in SYMBOL_CONFIG.items():
        if key in (cfg.get("prefix"), cfg.get("taifex_id")):
            return quote, cfg["prefix"]
    known = ", ".join(sorted(SYMBOL_CONFIG))
    raise BackfillError(
        f"unknown symbol {name!r}. Expected a configured market symbol ({known}) "
        f"or its prefix. Bot names are not accepted."
    )


def taipei_today() -> date:
    return datetime.now(_TAIPEI).date()


def default_end(today: date | None = None) -> date:
    """Yesterday in Taipei, so an in-progress session is not a gap."""
    return (today or taipei_today()) - timedelta(days=1)


def _at(day: date, minutes: int) -> datetime:
    return datetime(day.year, day.month, day.day, minutes // 60, minutes % 60)


def session_of(dt: datetime) -> tuple[date, str] | None:
    """Return ``(session_open_date, 'day'|'night')`` or None if off-session.

    A bar after midnight belongs to the night that opened the previous
    calendar date. That date, not the bar's calendar date, is what a
    closure closes.
    """
    minutes = dt.hour * 60 + dt.minute
    if dt.second or dt.microsecond:
        return None
    if DAY_OPEN <= minutes < DAY_CLOSE:
        return dt.date(), "day"
    if minutes >= NIGHT_OPEN:
        return dt.date(), "night"
    if minutes < NIGHT_CLOSE:
        return dt.date() - timedelta(days=1), "night"
    return None


def session_bar_opens(session_date: date, session: str, interval_seconds: int) -> list[datetime]:
    """Open times of an exact grid covering one session. No extra bars."""
    if interval_seconds <= 0:
        raise BackfillError(f"bad interval {interval_seconds}")
    if session == "day":
        start = _at(session_date, DAY_OPEN)
        stop = _at(session_date, DAY_CLOSE)
    elif session == "night":
        start = _at(session_date, NIGHT_OPEN)
        stop = _at(session_date + timedelta(days=1), NIGHT_CLOSE)
    else:
        raise BackfillError(f"bad session {session!r}")
    step = timedelta(seconds=interval_seconds)
    opens: list[datetime] = []
    cursor = start
    while cursor < stop:
        opens.append(cursor)
        cursor += step
    return opens


def _calendar_reason(day: date) -> tuple[str, str] | None:
    """Reason from the static calendar only (not the operator file)."""
    import holidays as _holidays

    from src.market_data.holidays import OVERRIDE_HOLIDAYS, OVERRIDE_TRADING

    if day in OVERRIDE_TRADING:
        return None
    if day in OVERRIDE_HOLIDAYS:
        return "holiday", "override_holidays"
    if day.weekday() >= 5:
        return "holiday", "calendar"
    try:
        public = _holidays.country_holidays("TW", years=day.year)
    except Exception:
        public = frozenset()
    if day in public:
        return "holiday", "holidays"
    return None


def _announcement_index(
    announcements: tuple[Announcement, ...] | None,
) -> dict[tuple[date, str], tuple[str, str]]:
    source = ANNOUNCED_CLOSURES if announcements is None else announcements
    index: dict[tuple[date, str], tuple[str, str]] = {}
    for ann in source:
        if ann.reason not in _REASONS:
            raise BackfillError(f"bad closure reason {ann.reason!r}")
        if ann.session not in ("day", "night", "both"):
            raise BackfillError(f"bad closure session {ann.session!r}")
        sessions = ("day", "night") if ann.session == "both" else (ann.session,)
        for session in sessions:
            index[(ann.day, session)] = (ann.reason, ann.source)
    return index


def closure_reason(
    day: date,
    session: str,
    announcements: tuple[Announcement, ...] | None = None,
    *,
    _index: dict[tuple[date, str], tuple[str, str]] | None = None,
) -> tuple[str, str] | None:
    """``(reason, source)`` when this session is closed, else None.

    Announcements win the label. An operator ``taifex_closures.yaml``
    date that the static calendar does not already close is a ``halt``.
    Weekends and public holidays are ``holiday``.
    """
    index = _index if _index is not None else _announcement_index(announcements)
    announced = index.get((day, session))
    if announced is not None:
        return announced
    calendar = _calendar_reason(day)
    if calendar is not None:
        return calendar
    if day in load_closure_override_dates() or is_taifex_holiday(day):
        return "halt", "taifex_closures"
    return None


def closure_table(
    start: date,
    end: date,
    announcements: tuple[Announcement, ...] | None = None,
) -> list[ClosureRow]:
    """Closed sessions in ``[start, end]``, day session before night."""
    index = _announcement_index(announcements)
    rows: list[ClosureRow] = []
    day = start
    while day <= end:
        for session in ("day", "night"):
            reason = closure_reason(day, session, _index=index)
            if reason is not None:
                rows.append(ClosureRow(day, session, reason[0], reason[1]))
        day += timedelta(days=1)
    return rows


def detect_resolution(bars: list[Bar]) -> str:
    """``1m`` or ``15m`` from the spacing of in-session bars."""
    stamps = sorted({b.dt for b in bars})
    deltas: list[float] = []
    for prev, nxt in zip(stamps, stamps[1:]):
        delta = (nxt - prev).total_seconds()
        if delta > 0:
            deltas.append(delta)
    if not deltas:
        raise BackfillError("cannot detect resolution from fewer than 2 bars")
    one = sum(1 for d in deltas if 50 <= d <= 70)
    fifteen = sum(1 for d in deltas if 800 <= d <= 1000)
    if one / len(deltas) >= 0.8:
        return "1m"
    if fifteen / len(deltas) >= 0.8:
        return "15m"
    raise BackfillError(
        "cannot detect 1m or 15m resolution; pass --resolution. "
        "Other resolutions are not a backfill source."
    )


def _daily_closes(bars: list[Bar]) -> dict[date, int]:
    """Last day-session close per session date. Night bars are ignored."""
    latest: dict[date, tuple[datetime, int]] = {}
    for bar in bars:
        located = session_of(bar.dt)
        if located is None or located[1] != "day":
            continue
        prev = latest.get(located[0])
        if prev is None or bar.dt >= prev[0]:
            latest[located[0]] = (bar.dt, bar.close)
    return {day: close for day, (_, close) in latest.items()}


def cross_check_closes(
    kept: list[Bar],
    official: list[Bar],
    tolerance: float = CROSS_CHECK_TOLERANCE,
) -> list[str]:
    """Dates whose day-session close differs from the official daily close.

    Comparison only. Official bars are never copied into the output.
    """
    ours = _daily_closes(kept)
    theirs: dict[date, tuple[datetime, int]] = {}
    for bar in official:
        day = bar.dt.date()
        prev = theirs.get(day)
        if prev is None or bar.dt >= prev[0]:
            theirs[day] = (bar.dt, bar.close)
    mismatches: list[str] = []
    for day, official_close in sorted((d, pair[1]) for d, pair in theirs.items()):
        if day not in ours:
            continue
        got = ours[day]
        if official_close == 0 or abs(got - official_close) / abs(official_close) > tolerance:
            mismatches.append(
                f"{day.isoformat()} close {got} vs official {official_close}"
            )
    return mismatches


def multifold_count(first: datetime, last: datetime, *,
                    fold_days: int = FOLD_DAYS,
                    max_folds: int = MAX_FOLDS) -> int:
    """14-day folds the bar span can support, using the live walk.

    ``analyze_folds`` anchors at the next midnight after the last exit
    and walks backward until no evidence remains before the window, or
    ``max_folds`` is hit. Bars from ``first`` through ``last`` are
    evidence on every day in between, so a window counts while ``first``
    still falls before that window's start. Two edge trades are not
    enough: the walker drops leading empty folds, and the fold cap would
    hide the May trade behind empty windows.
    """
    if last < first:
        return 0
    end = datetime(last.year, last.month, last.day) + timedelta(days=1)
    count = 0
    while count < max_folds:
        start = end - timedelta(days=fold_days)
        count += 1
        if first < start:
            end = start
            continue
        break
    return count


def _with_interval(bars: list[Bar], interval: int) -> list[Bar]:
    return [replace(bar, interval=interval) for bar in bars]


def _csv_bytes(bars: list[Bar], resolution: str) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["datetime", "open", "high", "low", "close", "volume", "res"])
    for bar in bars:
        writer.writerow([
            bar.dt.strftime("%Y/%m/%d %H:%M"),
            bar.open, bar.high, bar.low, bar.close, bar.volume,
            resolution,
        ])
    return buf.getvalue().encode("utf-8")


def _closures_csv_bytes(rows: list[ClosureRow]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["date", "session", "reason", "source"])
    for row in rows:
        writer.writerow([row.day.isoformat(), row.session, row.reason, row.source])
    return buf.getvalue().encode("utf-8")


def _derived_files(kept: list[Bar], resolution: str, prefix: str) -> list[tuple[str, list[Bar]]]:
    """``(repo-relative path, bars)`` for the native file and coarser aggregates."""
    native = RES_INTERVAL[resolution]
    files: list[tuple[str, list[Bar]]] = []
    for name, interval in _DERIVED[resolution]:
        derived = kept if interval == native else aggregate_bars(kept, interval)
        files.append((f"data/backfill/{prefix}/{name}", derived))
    return files


def _payloads(
    kept: list[Bar],
    resolution: str,
    rows: list[ClosureRow],
    report_json: str,
    prefix: str,
    derived: list[tuple[str, list[Bar]]] | None = None,
) -> list[tuple[str, bytes]]:
    """Repo-relative paths under ``data/backfill`` and their bytes."""
    files: list[tuple[str, bytes]] = [
        ("data/backfill/closures.csv", _closures_csv_bytes(rows)),
    ]
    for rel, bars in (derived if derived is not None else _derived_files(kept, resolution, prefix)):
        files.append((rel, _csv_bytes(bars, resolution)))
    files.append((f"data/backfill/{prefix}/report.json", report_json.encode("utf-8")))
    return files


def _write_payloads(payloads: list[tuple[str, bytes]], repo_root: Path) -> list[str]:
    """Write payloads. Re-check every path. Create nothing outside backfill."""
    repo = repo_root.resolve()
    checked: list[tuple[Path, bytes, str]] = []
    for rel, data in payloads:
        if not rel.startswith("data/backfill/"):
            raise BackfillPathError(f"refusing path outside data/backfill/: {rel}")
        path = resolve_output_dir(repo / rel, repo)
        checked.append((path, data, rel))
    written: list[str] = []
    for path, data, rel in checked:
        path.parent.mkdir(parents=True, exist_ok=True)
        # mkdir of the parent must still be inside the backfill root
        # (or be the root itself). Re-check after create in case of a race.
        resolve_output_dir(path, repo)
        path.write_bytes(data)
        written.append(rel)
    return written


def run_backfill(
    bars: list[Bar],
    *,
    symbol: str,
    start: date,
    end: date,
    repo_root: Path,
    output_dir: Path | str | None = None,
    write: bool = False,
    resolution: str | None = None,
    cross_check: list[Bar] | None = None,
    announcements: tuple[Announcement, ...] | None = None,
    require_folds: int = 0,
) -> BackfillReport:
    """Validate ``bars`` and, only when ``write`` is set and the report
    passes, write under ``data/backfill/``.

    ``write`` defaults to False. A target outside ``data/backfill/``
    raises ``BackfillPathError`` before any file is created.
    """
    # Refuse before building the report so a bad target never looks successful
    # and a dry-run cannot point at the live tree.
    dest = resolve_output_dir(output_dir, repo_root)
    # ``dest`` is the directory the caller named. The standard layout is
    # always the backfill root, which ``dest`` must be inside. Writing
    # uses repo-relative ``data/backfill/...`` paths, not ``dest``, so a
    # subdirectory request cannot redirect the live tree. Confirm the
    # root itself is acceptable (symlink to ``data/live`` fails here).
    root = resolve_output_dir(None, repo_root)
    if dest != root and root not in dest.parents:
        raise BackfillPathError(f"refusing path outside data/backfill/: {dest}")

    quote, prefix = canonical_symbol(symbol)
    if end < start:
        raise BackfillError(f"end {end} is before start {start}")
    if resolution not in (None, "auto", "1m", "15m"):
        raise BackfillError(f"resolution must be 1m or 15m, got {resolution!r}")

    index = _announcement_index(announcements)
    rows = closure_table(start, end, announcements)
    closed = {(row.day, row.session) for row in rows}

    kept_by_dt: dict[datetime, Bar] = {}
    skipped = outside = off_session = off_grid = duplicate = 0
    for bar in bars:
        located = session_of(bar.dt)
        if located is None:
            off_session += 1
            continue
        session_day, session_name = located
        if session_day < start or session_day > end:
            outside += 1
            continue
        if (session_day, session_name) in closed:
            # Closed session: drop the bar. Do not replace it.
            skipped += 1
            continue
        if bar.dt in kept_by_dt:
            duplicate += 1
            continue
        kept_by_dt[bar.dt] = bar

    # Resolution from the bars that survived the closure filter, so a
    # holiday print cannot decide the spacing. Off-grid check needs it.
    survivors = [kept_by_dt[dt] for dt in sorted(kept_by_dt)]
    chosen = resolution if resolution in RES_INTERVAL else None
    if chosen is None:
        if not survivors:
            chosen = "1m"
        else:
            chosen = detect_resolution(survivors)
    interval = RES_INTERVAL[chosen]

    off_grid_dts: list[datetime] = []
    kept: list[Bar] = []
    for bar in survivors:
        located = session_of(bar.dt)
        assert located is not None
        grid = session_bar_opens(located[0], located[1], interval)
        if bar.dt not in set(grid):  # noqa: performance — sessions are short
            off_grid_dts.append(bar.dt)
            continue
        kept.append(bar)
    off_grid = len(off_grid_dts)

    # Rebuild the expected grid and diff. A missing open is a gap.
    # Nothing is synthesized to fill it.
    present = {bar.dt for bar in kept}
    gaps: list[Gap] = []
    expected_sessions = 0
    day = start
    while day <= end:
        for session in ("day", "night"):
            if (day, session) in closed:
                continue
            expected_sessions += 1
            missing = [dt for dt in session_bar_opens(day, session, interval) if dt not in present]
            if missing:
                gaps.append(Gap(
                    day, session, len(missing),
                    tuple(dt.strftime("%Y-%m-%d %H:%M") for dt in missing[:3]),
                ))
        day += timedelta(days=1)

    kept = _with_interval(kept, interval)
    mismatches: list[str] = []
    if cross_check is None:
        cross_status = "skipped"
    elif not cross_check:
        cross_status = "empty"
        mismatches.append("cross-check file had no bars")
    else:
        mismatches = cross_check_closes(kept, cross_check)
        if not mismatches and not _overlap(kept, cross_check):
            cross_status = "no_overlap"
            mismatches.append("cross-check shares no day-session dates with kept bars")
        elif mismatches:
            cross_status = "mismatch"
        else:
            cross_status = "ok"

    warnings: list[str] = []
    if kept:
        first_dt, last_dt = kept[0].dt, kept[-1].dt
        fold_count = multifold_count(first_dt, last_dt)
        first_s = first_dt.strftime("%Y-%m-%d %H:%M")
        last_s = last_dt.strftime("%Y-%m-%d %H:%M")
        if first_dt.date() > FIRST_TRADE_DATE:
            warnings.append(
                f"first kept bar {first_s} is after first trade "
                f"{FIRST_TRADE_DATE.isoformat()}"
            )
    else:
        fold_count = 0
        first_s = ""
        last_s = ""
        warnings.append("no bars kept")

    if duplicate:
        warnings.append(f"dropped {duplicate} duplicate timestamps")
    if chosen == "15m":
        warnings.append(
            "res=15m: intrabar-sensitive checks must refuse this span"
        )

    problems: list[str] = []
    if off_session:
        problems.append(f"{off_session} off-session bars")
    if off_grid:
        problems.append(f"{off_grid} off-grid bars")
    if gaps:
        problems.append(f"{len(gaps)} unexpected gaps")
    if mismatches:
        problems.append(f"cross-check {cross_status}")
    if not kept:
        problems.append("no bars kept")
    if require_folds and fold_count < require_folds:
        problems.append(f"folds {fold_count} < {require_folds}")

    report = BackfillReport(
        symbol=quote,
        prefix=prefix,
        resolution=chosen,
        start=start,
        end=end,
        bars_in=len(bars),
        bars_kept=len(kept),
        bars_skipped_closure=skipped,
        bars_outside_span=outside,
        bars_off_session=off_session,
        bars_off_grid=off_grid,
        bars_duplicate=duplicate,
        sessions_expected=expected_sessions,
        sessions_closed=len(rows),
        gaps=gaps,
        closures=rows,
        cross_check=cross_status,
        cross_check_mismatches=mismatches,
        fold_count=fold_count,
        meets_min_folds=fold_count >= MIN_FOLDS,
        first_bar=first_s,
        last_bar=last_s,
        warnings=warnings,
        ok=not problems,
        write=write,
        wrote=False,
        message=("ok" if not problems else "validation failed: " + "; ".join(problems)),
    )

    if write and report.ok:
        derived = _derived_files(kept, chosen, prefix)
        report.files_written = (
            ["data/backfill/closures.csv"]
            + [rel for rel, _ in derived]
            + [f"data/backfill/{prefix}/report.json"]
        )
        report.wrote = True
        payloads = _payloads(kept, chosen, rows, report.to_json(), prefix, derived)
        try:
            report.files_written = _write_payloads(payloads, repo_root)
        except Exception:
            report.wrote = False
            raise
    return report


def _overlap(kept: list[Bar], official: list[Bar]) -> bool:
    ours = set(_daily_closes(kept))
    return any(bar.dt.date() in ours for bar in official)


def load_input_csv(path: Path, *, interval: int = 60) -> list[Bar]:
    """Read an OHLCV CSV. The interval is replaced once resolution is known."""
    if not path.is_file():
        raise BackfillError(f"input CSV not found: {path}")
    bars = load_bars_from_csv(path, symbol="BACKFILL", interval=interval)
    if not bars:
        raise BackfillError(f"input CSV has no bars: {path}")
    return bars


def _parse_date(text: str, flag: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise BackfillError(f"{flag} must be YYYY-MM-DD, got {text!r}") from exc


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Backfill validation bars into data/backfill/. "
            "Dry-run unless --write is given. Never writes the live tree "
            "and never fills exchange closures."
        ),
    )
    parser.add_argument(
        "--input", required=True,
        help="Broker K-line OHLCV CSV (same feed as live). Not a third-party vendor.",
    )
    parser.add_argument(
        "--symbol", required=True,
        help="Market symbol: TX00, MTX00, TMF00, or prefix TXF1/TMF1/IMF1. Not a bot name.",
    )
    parser.add_argument(
        "--start", default=DEFAULT_BACKFILL_START.isoformat(),
        help=f"YYYY-MM-DD span start (default {DEFAULT_BACKFILL_START.isoformat()}, "
             f"warmup before first trade {FIRST_TRADE_DATE.isoformat()})",
    )
    parser.add_argument(
        "--end", default=None,
        help="YYYY-MM-DD span end (default yesterday, Taipei). In-progress sessions are excluded.",
    )
    parser.add_argument(
        "--resolution", choices=("auto", "1m", "15m"), default="auto",
        help="Native resolution. auto detects 1m or 15m. 15m is marked res=15m.",
    )
    parser.add_argument(
        "--out", default=None,
        help="Output directory. Must be data/backfill or a directory inside it.",
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Write files under data/backfill/. Omit to dry-run (no files created).",
    )
    parser.add_argument(
        "--cross-check", default=None,
        help="Optional TAIFEX official daily CSV. Closes are compared (0.1%%). "
             "Never used as the bar source.",
    )
    parser.add_argument(
        "--require-folds", type=int, default=0,
        help=f"Fail validation when the span yields fewer than N {FOLD_DAYS}-day folds "
             f"(0 disables; a May–October span supports at least {MIN_FOLDS}).",
    )
    return parser


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    """CLI entry. Returns 0 on a passing report, 1 on validation failure, 2 on refusal."""
    import sys

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2

    root = repo_root if repo_root is not None else default_repo_root()
    try:
        resolve_output_dir(args.out, root)
        start = _parse_date(args.start, "--start")
        end = default_end() if args.end is None else _parse_date(args.end, "--end")
        bars = load_input_csv(Path(args.input))
        official = None
        if args.cross_check:
            official = load_input_csv(Path(args.cross_check), interval=86400)
        resolution = None if args.resolution == "auto" else args.resolution
        report = run_backfill(
            bars,
            symbol=args.symbol,
            start=start,
            end=end,
            repo_root=root,
            output_dir=args.out,
            write=bool(args.write),
            resolution=resolution,
            cross_check=official,
            require_folds=args.require_folds,
        )
    except (BackfillPathError, BackfillError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(report.format_text())
    return 0 if report.ok else 1
