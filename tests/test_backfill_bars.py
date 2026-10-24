"""D7/P3 backfill: dry-run, path guard, closures, and fold span."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.evolution.backfill import (
    FIRST_TRADE_DATE,
    MIN_FOLDS,
    BackfillPathError,
    build_parser,
    closure_table,
    main,
    multifold_count,
    resolve_output_dir,
    run_backfill,
    session_bar_opens,
)
from src.market_data.models import Bar


def _snapshot(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    if not root.exists():
        return found
    for path in root.rglob("*"):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def _plant_live_tree(root: Path) -> None:
    """Sentinels the backfill must not create or modify."""
    files = {
        "data/live/bot/bars_1m_20260507.csv": b"live-bars",
        "data/live/bot/run.lock": b"lock",
        "data/state/session.json": b"state",
        "data/votes/vote.json": b"vote",
        "data/taifex/TX_daily.csv": b"taifex-cache",
        "settings.yaml": b"secret: no\n",
        "taifex_closures.yaml": b"# operator file\n",
        "votes/root.json": b"vote-root",
        "state/root.json": b"state-root",
        "bot.lock": b"lock-root",
    }
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def _bar(symbol: str, dt: datetime, close: int = 100) -> Bar:
    return Bar(symbol, dt, close - 1, close + 1, close - 2, close, 1, 900)


def _complete(symbol: str, start: date, end: date, interval: int = 900) -> list[Bar]:
    """One on-grid bar for every session the calendar leaves open."""
    closed = {(row.day, row.session) for row in closure_table(start, end)}
    bars: list[Bar] = []
    day = start
    while day <= end:
        for session in ("day", "night"):
            if (day, session) in closed:
                continue
            for dt in session_bar_opens(day, session, interval):
                bars.append(_bar(symbol, dt, close=100 + dt.hour))
        day += timedelta(days=1)
    return bars


def _closed_samples(symbol: str, start: date, end: date) -> list[Bar]:
    """One bar inside each closed session, on the 15m grid."""
    extras: list[Bar] = []
    for row in closure_table(start, end):
        if row.session == "day":
            dt = datetime(row.day.year, row.day.month, row.day.day, 9, 0)
        else:
            dt = datetime(row.day.year, row.day.month, row.day.day, 16, 0)
        extras.append(_bar(symbol, dt, close=1))
    return extras


def _read_dts(path: Path) -> set[datetime]:
    stamps: set[datetime] = set()
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            stamps.add(datetime.strptime(row["datetime"], "%Y/%m/%d %H:%M"))
    return stamps


def _outside(snap: dict[str, bytes]) -> dict[str, bytes]:
    return {
        key: value
        for key, value in snap.items()
        if not key.startswith("data/backfill/")
    }


# 2026-09-24 Thursday is a trading day. 09-25 is a public holiday,
# 09-26 the announced typhoon (also a weekend), 09-28 a public holiday.
_START = date(2026, 9, 24)
_END = date(2026, 9, 29)


def test_dry_run_writes_nothing(tmp_path):
    _plant_live_tree(tmp_path)
    before = _snapshot(tmp_path)
    bars = _complete("TX00", _START, _END) + _closed_samples("TX00", _START, _END)
    report = run_backfill(
        bars, symbol="TX00", start=_START, end=_END,
        repo_root=tmp_path, write=False,
    )
    assert report.ok
    assert report.write is False
    assert report.wrote is False
    assert report.files_written == []
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "data" / "backfill").exists()

    csv_path = _write_csv(tmp_path / "in.csv", bars)
    before = _snapshot(tmp_path)
    code = main(
        ["--input", str(csv_path),
         "--symbol", "TX00", "--start", _START.isoformat(),
         "--end", _END.isoformat()],
        repo_root=tmp_path,
    )
    assert code == 0
    assert _snapshot(tmp_path) == before
    args = build_parser().parse_args(
        ["--input", "x.csv", "--symbol", "TX00"])
    assert args.write is False


def test_write_mode_only_touches_backfill(tmp_path):
    _plant_live_tree(tmp_path)
    live_csv = tmp_path / "data" / "live" / "bot" / "bars_1m_20260507.csv"
    bars = _complete("TX00", _START, _END)
    before = _snapshot(tmp_path)
    report = run_backfill(
        bars, symbol="TX00", start=_START, end=_END,
        repo_root=tmp_path, write=True, resolution="15m",
    )
    assert report.ok
    assert report.wrote is True
    assert report.files_written
    assert all(rel.startswith("data/backfill/") for rel in report.files_written)
    after = _snapshot(tmp_path)
    assert _outside(after) == _outside(before)
    assert live_csv.read_bytes() == b"live-bars"
    written_root = tmp_path / "data" / "backfill"
    for path in written_root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(tmp_path).as_posix()
            assert rel.startswith("data/backfill/")
            assert "live" not in Path(rel).parts
            assert not path.name.endswith(".lock")
    native = written_root / "TXF1" / "bars_15m.csv"
    assert native.is_file()
    assert not (written_root / "TXF1" / "bars_1m.csv").exists()
    with native.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert {row["res"] for row in rows} == {"15m"}
    assert (written_root / "closures.csv").is_file()
    assert (written_root / "TXF1" / "bars_1H.csv").is_file()
    assert (written_root / "TXF1" / "bars_H4.csv").is_file()
    assert (written_root / "TXF1" / "bars_1D.csv").is_file()


def test_closure_dates_are_skipped(tmp_path):
    bars = _complete("TX00", _START, _END)
    extras = _closed_samples("TX00", _START, _END)
    assert extras, "the span must contain at least one closure"
    report = run_backfill(
        bars + extras, symbol="TMF1", start=_START, end=_END,
        repo_root=tmp_path, write=True, resolution="15m",
    )
    assert report.ok, report.message
    assert report.bars_skipped_closure == len(extras)
    assert report.prefix == "TMF1"
    native = tmp_path / "data" / "backfill" / "TMF1" / "bars_15m.csv"
    stamps = _read_dts(native)
    for extra in extras:
        assert extra.dt not in stamps
    # Thursday night runs through Friday 05:00 even though Friday is a holiday.
    assert datetime(2026, 9, 25, 2, 0) in stamps
    # The holiday's own sessions are not filled.
    assert datetime(2026, 9, 25, 9, 0) not in stamps
    assert datetime(2026, 9, 26, 16, 0) not in stamps
    assert datetime(2026, 9, 28, 9, 0) not in stamps

    listed = {(row.day, row.session): row for row in report.closures}
    assert listed[(date(2026, 9, 26), "day")].reason == "typhoon"
    assert listed[(date(2026, 9, 26), "night")].reason == "typhoon"
    assert listed[(date(2026, 9, 28), "day")].reason == "holiday"
    # 10/10 is outside this span; the default announcement list still closes it.
    october = {(row.day, row.session): row
               for row in closure_table(date(2026, 10, 10), date(2026, 10, 11))}
    assert october[(date(2026, 10, 10), "day")].reason == "holiday"
    assert october[(date(2026, 10, 11), "night")].reason == "holiday"

    with (tmp_path / "data" / "backfill" / "closures.csv").open(encoding="utf-8") as handle:
        body = handle.read()
    assert "2026-09-26,day,typhoon,taifex_announcement" in body
    assert "2026-09-25" in body  # public holiday, not synthesized into bars


def test_operator_halt_is_skipped_and_not_a_gap(tmp_path, monkeypatch):
    import src.market_data.holidays as hol

    path = tmp_path / "taifex_closures.yaml"
    path.write_text("2026-04-16\n", encoding="utf-8")
    monkeypatch.setattr(hol, "closures_override_path", lambda: path)
    hol._closures_cache = None
    try:
        start, end = date(2026, 4, 15), date(2026, 4, 17)
        bars = _complete("TX00", start, end)
        halt = _bar("TX00", datetime(2026, 4, 16, 9, 0), close=1)
        report = run_backfill(
            bars + [halt], symbol="TX00", start=start, end=end,
            repo_root=tmp_path, write=True, resolution="15m",
            announcements=(),
        )
        assert report.ok, report.message
        assert report.bars_skipped_closure >= 1
        stamps = _read_dts(tmp_path / "data" / "backfill" / "TXF1" / "bars_15m.csv")
        assert datetime(2026, 4, 16, 9, 0) not in stamps
        assert datetime(2026, 4, 16, 16, 0) not in stamps
        reasons = {(row.day, row.session): row.reason for row in report.closures}
        assert reasons[(date(2026, 4, 16), "day")] == "halt"
        assert reasons[(date(2026, 4, 16), "night")] == "halt"
    finally:
        hol._closures_cache = None


def test_refuses_target_outside_backfill(tmp_path):
    _plant_live_tree(tmp_path)
    before = _snapshot(tmp_path)
    bars = _complete("TX00", _START, _END)
    outside = [
        tmp_path / "data" / "live",
        tmp_path / "data" / "backfill" / ".." / "live",
        tmp_path / "data" / "state",
        tmp_path / "data" / "votes",
        tmp_path / "settings.yaml",
        tmp_path / "votes",
        tmp_path / "state",
        tmp_path / "bot.lock",
        Path("/tmp/backfill-not-here"),
        tmp_path / "data" / "backfill" / ".." / ".." / "settings.yaml",
    ]
    for target in outside:
        with pytest.raises(BackfillPathError):
            resolve_output_dir(target, tmp_path)
        with pytest.raises(BackfillPathError):
            run_backfill(
                bars, symbol="TX00", start=_START, end=_END,
                repo_root=tmp_path, output_dir=target, write=True,
            )
    assert _snapshot(tmp_path) == before

    link_parent = tmp_path / "linked"
    link_parent.mkdir()
    (link_parent / "sentinel").write_bytes(b"sentinel")
    backfill = tmp_path / "data" / "backfill"
    backfill.symlink_to(link_parent, target_is_directory=True)
    before_link = _snapshot(tmp_path)
    with pytest.raises(BackfillPathError):
        run_backfill(
            bars, symbol="TX00", start=_START, end=_END,
            repo_root=tmp_path, write=True,
        )
    assert (link_parent / "sentinel").read_bytes() == b"sentinel"
    assert _snapshot(link_parent) == {"sentinel": b"sentinel"}
    assert _snapshot(tmp_path) == before_link

    code = main(
        ["--input", "missing.csv", "--symbol", "TX00",
         "--out", str(tmp_path / "data" / "live"), "--write"],
        repo_root=tmp_path,
    )
    assert code == 2
    assert _snapshot(tmp_path) == before_link


def test_failed_validation_writes_nothing(tmp_path):
    _plant_live_tree(tmp_path)
    before = _snapshot(tmp_path)
    # One trading day's day-session only: the night session is an unexpected gap.
    day = date(2026, 4, 15)
    bars = [_bar("TX00", dt) for dt in session_bar_opens(day, "day", 900)]
    report = run_backfill(
        bars, symbol="TX00", start=day, end=day,
        repo_root=tmp_path, write=True, resolution="15m",
        announcements=(),
    )
    assert not report.ok
    assert report.wrote is False
    assert any(gap.session == "night" for gap in report.gaps)
    assert _snapshot(tmp_path) == before


def test_may_span_supports_at_least_8_folds():
    first = datetime(2026, 5, 7, 8, 45)
    last = datetime(2026, 10, 23, 13, 30)
    assert multifold_count(first, last) >= MIN_FOLDS
    assert FIRST_TRADE_DATE == date(2026, 5, 7)

    # The live walker, fed a trade every week across the same span.
    from src.evolution.pipeline import analyze_folds

    class _Trade:
        def __init__(self, dt: datetime):
            stamp = dt.strftime("%Y-%m-%d %H:%M")
            self.exit_dt = stamp
            self.entry_dt = stamp
            self.pnl = 1

    trades = []
    cursor = first
    while cursor <= last:
        trades.append(_Trade(cursor))
        cursor += timedelta(days=7)
    assert len(analyze_folds(trades, trades, fold_days=14, max_folds=10)) >= MIN_FOLDS


def test_short_span_fold_count_and_symbol_guard(tmp_path):
    report = run_backfill(
        _complete("TX00", _START, _END),
        symbol="MTX00", start=_START, end=_END,
        repo_root=tmp_path, write=False,
    )
    assert report.symbol == "MTX00"
    assert report.prefix == "TMF1"
    assert report.fold_count < MIN_FOLDS
    assert report.meets_min_folds is False
    with pytest.raises(Exception):
        run_backfill(
            _complete("TX00", _START, _END),
            symbol="not-a-symbol", start=_START, end=_END,
            repo_root=tmp_path, write=True,
        )
    assert not (tmp_path / "data" / "backfill").exists()


def test_one_minute_source_is_stored_and_aggregated(tmp_path):
    day = date(2026, 4, 15)
    bars = [
        Bar("TX00", dt, 10, 12, 9, 11, 1, 60)
        for session in ("day", "night")
        for dt in session_bar_opens(day, session, 60)
    ]
    report = run_backfill(
        bars, symbol="TX00", start=day, end=day,
        repo_root=tmp_path, write=True, resolution="1m",
        announcements=(),
    )
    assert report.ok, report.message
    folder = tmp_path / "data" / "backfill" / "TXF1"
    with (folder / "bars_1m.csv").open(encoding="utf-8") as handle:
        rows_1m = list(csv.DictReader(handle))
    with (folder / "bars_15m.csv").open(encoding="utf-8") as handle:
        rows_15 = list(csv.DictReader(handle))
    assert len(rows_1m) == len(bars)
    assert len(rows_15) == 76  # 20 day + 56 night
    assert {row["res"] for row in rows_1m} == {"1m"}
    assert {row["res"] for row in rows_15} == {"1m"}
    assert not any(path.name.endswith(".lock") for path in folder.rglob("*"))


def test_cross_check_mismatch_blocks_write(tmp_path):
    bars = _complete("TX00", _START, _END)
    # Official close far from the day-session close (100 + 13 = 113 at 13:30).
    official = [Bar("TXF1", datetime(2026, 9, 24), 20000, 20000, 20000, 20000, 1, 86400)]
    before = _snapshot(tmp_path)
    report = run_backfill(
        bars, symbol="TX00", start=_START, end=_END,
        repo_root=tmp_path, write=True, resolution="15m",
        cross_check=official,
    )
    assert not report.ok
    assert report.cross_check == "mismatch"
    assert report.wrote is False
    assert _snapshot(tmp_path) == before


def _write_csv(path: Path, bars: list[Bar]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["datetime", "open", "high", "low", "close", "volume"])
        for bar in bars:
            writer.writerow([
                bar.dt.strftime("%Y/%m/%d %H:%M"),
                bar.open, bar.high, bar.low, bar.close, bar.volume,
            ])
    return path
