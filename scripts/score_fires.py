"""P5 — score W2 fires against what the nightly lane actually did (offline).

Reads the W2 alerts sheet export and a bot's own artifacts, and writes
one row per fire joining the two:

    python scripts/score_fires.py \
        --fires "exports/w2_alerts.csv" \
        --bot-dir "data/live/TMF00_mybot" \
        --out "exports/w2_fires_scored.csv"

The fires CSV is the alerts-sheet export: `時間TPE`, `tier`, `方向` and
the P&L columns `1h淨損益` / `2h淨損益` / `4h淨損益` / `8h淨損益` /
`收盤淨損益`.  Those columns measure the fire where its edge lives (the
hours right after it).  The columns this script ADDS measure the other
afterlife — the vote the fire wrote, read at the ~04:58 classification
of the night it named, whose leg only trades from the next session on:

    night_key         the "YYYY-MM-DD|NIGHT" the vote targeted
    consumed_row      date of the classification row for that night ("" = none)
    decision          what that classification recommended
    strategy_active   the leg that was actually running
    votes             the consumed-votes audit cell
    leg_date          the next NIGHT row — the first session the leg traded
    leg_day_pnl       recorded P&L of the DAY session on leg_date
    leg_night_pnl     recorded P&L of the NIGHT session on leg_date
    leg_window_move   points from leg_date's 08:45 open to that night's
                      close, signed so POSITIVE means the fire was right

Read-only apart from --out.  The join itself lives in
src/news/fire_scoring.py and is covered by tests/test_fire_scoring.py;
this file is only the I/O around it.
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.news.fire_scoring import (                       # noqa: E402
    OUT_HEADER,
    PNL_COLUMNS,
    Fire,
    HistoryRow,
    score_fires,
)

_BAR_DT_FORMATS = ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S")


def _first_key(row: dict, *names: str) -> str:
    """Value of the first present column, tolerating stray whitespace."""
    lookup = {str(k).strip(): v for k, v in row.items() if k is not None}
    for name in names:
        if name in lookup:
            return str(lookup[name] or "").strip()
    return ""


def load_fires(path: str) -> list:
    """Read the alerts-sheet export.  utf-8-sig: sheet exports carry a BOM."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    fires = []
    for row in rows:
        time_tpe = _first_key(row, "時間TPE", "time_tpe", "datetime")
        if not time_tpe:
            continue
        fires.append(Fire(
            time_tpe=time_tpe,
            tier=_first_key(row, "tier", "層級"),
            direction=_first_key(row, "方向", "direction"),
            pnl={col: _first_key(row, col) for col in PNL_COLUMNS},
        ))
    return fires


def load_history(bot_dir: str) -> list:
    """Read regime_history.csv by HEADER NAME.

    Never by position: the file's header grows (v2 -> v3 votes -> v4
    vote_rule) and older data rows stay short, so an index that is right
    today is wrong against last month's rows.
    """
    path = os.path.join(bot_dir, "regime_history.csv")
    if not os.path.isfile(path):
        raise SystemExit(f"no regime_history.csv in {bot_dir}")
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.reader(f))
    if len(rows) < 2:
        return []
    idx = {name.strip(): i for i, name in enumerate(rows[0])}

    def cell(row, key):
        i = idx.get(key)
        if i is None or i >= len(row):
            return ""
        return (row[i] or "").strip()

    out = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        out.append(HistoryRow(
            date=cell(row, "date"),
            session=cell(row, "session"),
            decision=cell(row, "decision"),
            strategy_active=cell(row, "strategy_active"),
            votes=cell(row, "votes"),
            pnl=cell(row, "pnl"),
            raw_regime=cell(row, "raw_regime"),
        ))
    return out


def load_bars(bot_dir: str) -> list:
    """Load every ``bars_1m_*.csv`` as (dt, o, h, l, c, v) tuples.

    Duplicate timestamps resolve last-file-wins, matching
    ``live_runner.load_1m_bars_from_csvs``.  Garbage rows are skipped
    rather than aborting a scoring run over one bad line.
    """
    merged: dict = {}
    for path in sorted(glob.glob(os.path.join(bot_dir, "bars_1m_*.csv"))):
        try:
            with open(path, newline="", encoding="utf-8", errors="replace") as f:
                reader = csv.reader(f)
                next(reader, None)      # header
                for row in reader:
                    if len(row) < 6:
                        continue
                    dt = None
                    for fmt in _BAR_DT_FORMATS:
                        try:
                            dt = datetime.strptime(row[0].strip(), fmt)
                            break
                        except ValueError:
                            continue
                    if dt is None:
                        continue
                    try:
                        merged[dt] = (dt, float(row[1]), float(row[2]),
                                      float(row[3]), float(row[4]), float(row[5]))
                    except ValueError:
                        continue
        except OSError:
            continue
    return [merged[k] for k in sorted(merged)]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Score W2 fires against the nightly lane (offline)")
    ap.add_argument("--fires", required=True,
                    help="CSV export of the W2 alerts sheet")
    ap.add_argument("--bot-dir", required=True,
                    help="bot data dir with regime_history.csv + bars_1m_*.csv")
    ap.add_argument("--out", required=True, help="output CSV path")
    args = ap.parse_args()

    fires = load_fires(args.fires)
    history = load_history(args.bot_dir)
    bars = load_bars(args.bot_dir)
    print(f"fires: {len(fires)} | history rows: {len(history)} | 1m bars: {len(bars)}")

    scored = score_fires(fires, history, bars)

    out = Path(args.out)
    if out.parent != Path(""):
        out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(OUT_HEADER)
        for row in scored:
            writer.writerow(row.to_row())

    consumed = sum(1 for s in scored if s.consumed_row)
    moved = sum(1 for s in scored if s.leg_window_move)
    right = sum(1 for s in scored
                if s.leg_window_move and s.leg_window_move.startswith("+"))
    print(f"wrote {len(scored)} rows -> {out}")
    print(f"  classification row found: {consumed}/{len(scored)}")
    if moved:
        print(f"  leg window measurable: {moved}; direction right: {right}/{moved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
