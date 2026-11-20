"""One-shot backfill of W4's ``chips_history.jsonl`` (籌碼走勢 trend).

W4 keeps every row it records from now on (``chips_monitor.append_history``).
This fills the dates before that, from two sources, without ever touching
the vote files or ``.chips_state.json``:

* ``--from-log`` (default on): ``chips_monitor.log`` summary lines, i.e.
  what W4 really computed and decided on those days (``src: "log"``).
  Only the line format in use since 2026-08-31 (ΔOI + level + data date)
  is read; older level-only lines are skipped.
* ``--remote``: the TAIFEX 三大法人 by-contract download (TX, one request
  for the whole range) for the foreign net OI level of every trading day,
  plus TWSE BFI82U for the foreign cash-equity net buy of each day not
  already covered, one request per day, ``--sleep`` seconds apart
  (``src: "backfill"``). ΔOI uses W4's rule: the previous trading day's
  level, refused past MAX_PREV_GAP_DAYS. W4 was not running on these
  days, so ``decision`` stays empty; what W4's rules would have said is
  kept as ``hindcast_decision`` / ``hindcast_reason`` for analysis.

A date already in the history file or in ``daily`` is never written again,
except a backfilled day whose TWSE fetch failed (``final: false``, no
equity): a re-run retries it and appends a newer line, which the trend
reader prefers (live > log > backfill, later line within a source).

Run it on the bridge host, between W4 slots (not at :15), dry run first::

    python chips_backfill.py --base-path C:/Users/Traderbot/n8n-bridge/regime_vote.json
    python chips_backfill.py --base-path ... --remote --apply      # + one year from TAIFEX/TWSE
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import chips_monitor as cm  # noqa: E402

TAIFEX_RANGE_URL = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
TX_PRODUCT = "臺股期貨"
BACKFILL_REASON = "backfill: W4 was not recording this day — no live decision"
DEFAULT_DAYS = 365

LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) TPE \| "
    r"ΔOI (?P<delta>[+-]?[\d,]+|n/a) \(level (?P<level>[+-]?[\d,]+), "
    r"(?:prev (?P<prev>\d{8})|no prev)\) \| "
    r"equity (?P<eq>[+-]?[\d,]+|n/a) \| vote: (?P<vote>trending-up|trending-down|-) \| "
    r"(?P<reason>.*) \(data (?P<date>\d{8})\)$")


def _int(text: str | None) -> float | None:
    if text is None or text == "n/a":
        return None
    return float(text.replace(",", ""))


# ── chips_monitor.log ────────────────────────────────────────────────────

def parse_log(text: str) -> dict[str, dict]:
    """``{YYYYMMDD: row}`` from W4 summary lines; the last line per date wins."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        m = LOG_RE.match(line.strip())
        if not m:
            continue
        ts = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S")
        equity = _int(m["eq"])
        out[m["date"]] = {
            "net_oi": _int(m["level"]), "delta_oi": _int(m["delta"]),
            "prev_date": m["prev"], "equity_net": equity,
            "decision": None if m["vote"] == "-" else m["vote"],
            "reason": m["reason"], "final": equity is not None,
            "session": cm.night_session_key(ts),
            "at": ts.isoformat(timespec="seconds") + "+08:00",
        }
    return out


# ── TAIFEX by-contract download ──────────────────────────────────────────

def parse_taifex_csv(text: str) -> dict[str, float]:
    """``{YYYYMMDD: foreign TX net OI}`` from the 三大法人 by-contract CSV."""
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    header = next(reader, None) or []
    try:
        i_date, i_prod = header.index("日期"), header.index("商品名稱")
        i_who = header.index("身份別")
        i_oi = header.index("多空未平倉口數淨額")
    except ValueError as exc:
        raise ValueError(f"unexpected TAIFEX CSV header: {header[:6]}") from exc
    out: dict[str, float] = {}
    for row in reader:
        if len(row) <= max(i_date, i_prod, i_who, i_oi):
            continue
        if row[i_prod].strip() != TX_PRODUCT or not row[i_who].strip().startswith(cm.FOREIGN_PREFIX):
            continue
        key = row[i_date].strip().replace("/", "")
        value = _int(row[i_oi].strip())
        if len(key) == 8 and value is not None:
            out[key] = value
    return out


def fetch_taifex_levels(start: date, end: date) -> dict[str, float]:
    body = urllib.parse.urlencode({"queryStartDate": f"{start:%Y/%m/%d}",
                                   "queryEndDate": f"{end:%Y/%m/%d}",
                                   "commodityId": "TXF"}).encode()
    req = urllib.request.Request(TAIFEX_RANGE_URL, data=body, headers={
        **cm._UA, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60, context=cm._SSL_CTX) as resp:
        raw = resp.read()
    return parse_taifex_csv(raw.decode("cp950", errors="replace"))


def build_backfill(levels: dict[str, float], equity: dict[str, float | None],
                   dates: list[str]) -> dict[str, dict]:
    """Rows for *dates* from a continuous level series (W4's ΔOI rule)."""
    ordered = sorted(levels)
    out: dict[str, dict] = {}
    for key in dates:
        i = ordered.index(key)
        prev = ordered[i - 1] if i else None
        delta = None
        if prev is not None:
            gap = cm.date_gap_days(prev, key)
            if gap is not None and gap <= cm.MAX_PREV_GAP_DAYS:
                delta = levels[key] - levels[prev]
            else:
                prev = None
        eq = equity.get(key)
        hindcast, why = None, ""
        if delta is not None and eq is not None:
            hindcast, why = cm.decide(delta, eq)
        out[key] = {
            "net_oi": levels[key], "delta_oi": delta, "prev_date": prev,
            "equity_net": eq, "decision": None, "reason": BACKFILL_REASON,
            "final": eq is not None,                 # W4's rule: retry without equity "session": f"{key[:4]}-{key[4:6]}-{key[6:]}|NIGHT", "at": "",
            "hindcast_decision": hindcast, "hindcast_reason": why,
        }
    return out


# ── existing coverage / writing ──────────────────────────────────────────

def covered_dates(bridge: Path) -> set[str]:
    """Dates not to write again: every history date (last line per date)
    except a backfilled day still missing its TWSE equity, plus ``daily``."""
    last: dict[str, dict] = {}
    path = bridge / cm.HISTORY_NAME
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and len(str(row.get("date", ""))) == 8:
                last[str(row["date"])] = row
    have = {d for d, row in last.items()
            if not (row.get("src") == "backfill" and row.get("final") is False)}
    daily = cm.load_state(bridge / cm.STATE_NAME).get("daily")
    if isinstance(daily, dict):
        have.update(str(d) for d in daily)
    return have


def append_rows(bridge: Path, rows: dict[str, dict], src: str) -> None:
    with open(bridge / cm.HISTORY_NAME, "a", encoding="utf-8", newline="\n") as f:
        for key in sorted(rows):
            line = {"v": cm.HISTORY_VERSION, "date": key, "src": src, **rows[key]}
            f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")


def _twse_equity(key: str) -> float | None:
    try:
        value = cm.fetch_twse_foreign_net_buy(key)
    except cm.ChipsFetchError as exc:
        print(f"    TWSE {key}: {exc}")
        return None
    return value if isinstance(value, float) else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-path", required=True,
                    help="the W4 vote path (regime_vote.json); files are beside it")
    ap.add_argument("--no-log", action="store_true", help="skip chips_monitor.log")
    ap.add_argument("--remote", action="store_true", help="also fetch TAIFEX + TWSE")
    ap.add_argument("--since", help="YYYYMMDD, default one year ago")
    ap.add_argument("--until", help="YYYYMMDD, default yesterday")
    ap.add_argument("--sleep", type=float, default=3.0, help="seconds between TWSE calls")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args(argv)

    bridge = Path(args.base_path).parent
    have = covered_dates(bridge)
    print(f"history + daily already cover {len(have)} date(s)")

    log_rows: dict[str, dict] = {}
    log_path = bridge / cm.LOG_NAME
    if not args.no_log and log_path.exists():
        parsed = parse_log(log_path.read_text(encoding="utf-8", errors="replace"))
        log_rows = {k: v for k, v in parsed.items() if k not in have}
        print(f"chips_monitor.log: {len(parsed)} dated decision line(s), "
              f"{len(log_rows)} new date(s)")

    remote_rows: dict[str, dict] = {}
    if args.remote:
        today = datetime.now(cm.TZ_TAIPEI).date()
        until = datetime.strptime(args.until, "%Y%m%d").date() if args.until \
            else today - timedelta(days=1)
        since = datetime.strptime(args.since, "%Y%m%d").date() if args.since \
            else until - timedelta(days=DEFAULT_DAYS)
        levels = fetch_taifex_levels(since, until)
        todo = sorted(k for k in levels if k not in have and k not in log_rows)
        print(f"TAIFEX {since}..{until}: {len(levels)} trading day(s), {len(todo)} to backfill; "
              f"TWSE needs {len(todo)} call(s) (~{len(todo) * args.sleep / 60:.0f} min)")
        equity: dict[str, float | None] = {}
        if args.apply:
            for n, key in enumerate(todo):
                if n:
                    time.sleep(args.sleep)
                equity[key] = _twse_equity(key)
        remote_rows = build_backfill(levels, equity, todo)

    for name, rows in (("log", log_rows), ("backfill", remote_rows)):
        if rows:
            first, last = min(rows), max(rows)
            print(f"  {name}: {len(rows)} row(s) {first}..{last}")
    if not args.apply:
        print("DRY RUN - nothing written (TWSE not called). Re-run with --apply.")
        return 0
    if log_rows:
        append_rows(bridge, log_rows, "log")
    if remote_rows:
        append_rows(bridge, remote_rows, "backfill")
    print(f"appended to {bridge / cm.HISTORY_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
