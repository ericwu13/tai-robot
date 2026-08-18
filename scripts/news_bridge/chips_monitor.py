"""W4 籌碼 (institutional money flow) bridge — TAIFEX 三大法人 regime votes.

Standalone bridge script (stdlib only, same family as W2/W3).  Once per
trading day (~16:15 TPE, after TAIFEX posts the daily 三大法人 report at
~15:30) it fetches:

- TAIFEX OpenAPI 區分各期貨契約三大法人交易資訊 — 外資 net open interest
  in 臺股期貨 (TX).  NOTE: this dataset aggregates ALL delivery months per
  contract (one row per 商品 × 身份別); there is no near/far-month split.
- TWSE BFI82U 三大法人買賣金額統計表 — 外資 equity net buy/sell in NTD,
  used as a contradiction check (futures short + equity buy = probably
  hedging, not a directional view).  The T86 endpoint is NOT used: T86 is
  a per-stock report with no market-level 外資 row.

and maps them to a regime vote (see ``compute_chips_direction``):

- ``|net OI| < 1,000``            → no vote (hysteresis band)
- futures vs equity contradiction → no vote (institutional hedging)
- ``|net OI| >= 5,000``           → vote regardless of slope
- ``1,000 <= |net OI| < 5,000``   → vote only if the day-over-day OI
                                    slope agrees (needs yesterday's OI
                                    from the ``.chips_state.json`` sidecar)

There is no neutral vote — when uncertain the W4 vote file is DELETED so
a stale vote can never linger (votes are also consumed by the classifier
after every pass, so persisting a vote means re-writing it every run).

Usage:
    # normal run (n8n Execute Command node, weekdays 16:15 TPE)
    python chips_monitor.py --base-path C:/n8n-bridge/regime_vote.json

    # inspect without writing anything
    python chips_monitor.py --base-path C:/n8n-bridge/regime_vote.json --dry-run

    # re-run for a specific trade date (testing)
    python chips_monitor.py --base-path C:/n8n-bridge/regime_vote.json --date 20260814

Weekends/holidays: TAIFEX keeps serving the LAST trading day's rows, so
the row date is compared against the requested trade date — a mismatch is
treated as "no data" and the run is a no-op (stale vote still deleted).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import ssl
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ_TAIPEI = ZoneInfo("Asia/Taipei")

TAIFEX_URL = ("https://openapi.taifex.com.tw/v1/"
              "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
TWSE_BFI82U_URL = ("https://www.twse.com.tw/rwd/zh/fund/BFI82U"
                   "?type=day&dayDate={date}&response=json")

TX_PRODUCT_NAME = "臺股期貨"   # strict match — excludes 小型臺股期貨 / options
FOREIGN_PREFIX = "外資"        # matches 外資 and the older 外資及陸資 label

# Signal thresholds (contracts / NTD) — NTNU study: 外資 is the only
# institution with 5/5 significant predictive metrics; TEJ backtest
# (2016-01 .. 2025-10) picked these bands.
HYSTERESIS_CONTRACTS = 1_000
STRONG_CONTRACTS = 5_000
EQUITY_FLOW_THRESHOLD_NTD = 5_000_000_000

STATE_NAME = ".chips_state.json"
STATE_KEEP_DAYS = 10
LOG_NAME = "chips_monitor.log"
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_LINES = 2000

_UA = {"User-Agent": "Mozilla/5.0 (tai-robot news bridge)", "Connection": "close"}


def post_discord(webhook: str | None, msg: str) -> None:
    if not webhook:
        return
    try:
        body = json.dumps({"content": msg}).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=body,
            headers={**_UA, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        print("  discord alert sent")
    except Exception as e:  # noqa: BLE001
        print(f"  discord post failed: {type(e).__name__}: {e}")

# TWSE's intermediate cert lacks a Subject Key Identifier, which trips
# Python 3.13's default VERIFY_X509_STRICT.  Keep chain + hostname
# verification, drop only the strict RFC 5280 extension checks.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.verify_flags &= ~ssl.VERIFY_X509_STRICT


# ── HTTP / parsing helpers ───────────────────────────────────────────────

def _http_json(url: str) -> object | None:
    """GET *url*, parse JSON.  None on any failure (network, HTTP, parse)."""
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            return json.load(resp)
    except Exception as e:  # noqa: BLE001
        print(f"  fetch failed [{url[:70]}...]: {type(e).__name__}: {e}")
        return None


def _num(value) -> float | None:
    """Parse a TAIFEX/TWSE numeric cell ("8,123", -42, "") to float."""
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _field(row: dict, *names):
    """First present key from *names* (API key spellings vary)."""
    for name in names:
        if name in row:
            return row[name]
    return None


def _norm_date(value) -> str:
    """"2026/08/14" / "2026-08-14" / "20260814" → "20260814"."""
    return "".join(ch for ch in str(value) if ch.isdigit())


# ── Data sources ─────────────────────────────────────────────────────────

def fetch_taifex_foreign_net_oi(date_key: str) -> float | None:
    """外資 net open interest (contracts) in TX for trade date *date_key*.

    Returns None when the API is unreachable, returns no TX/外資 rows, or
    its rows are for a different date (weekend/holiday — TAIFEX serves
    the last trading day).  The dataset has one row per 商品 × 身份別
    covering ALL delivery months combined; rows are summed defensively
    but normally exactly one matches.
    """
    data = _http_json(TAIFEX_URL)
    if not isinstance(data, list):
        return None

    total, matched = 0.0, 0
    for row in data:
        if not isinstance(row, dict):
            continue
        # Live API (checked 2026-08-15): English keys, Chinese values —
        # ContractCode="臺股期貨", Item="外資及陸資", OpenInterest(Net).
        # Chinese key spellings kept as fallbacks (other TAIFEX datasets
        # and the swagger docs use them).
        if _field(row, "ContractCode", "商品名稱", "ContractName") != TX_PRODUCT_NAME:
            continue
        ident = str(_field(row, "Item", "身份別", "身分別") or "")
        if not ident.startswith(FOREIGN_PREFIX):
            continue
        row_date = _norm_date(_field(row, "Date", "日期") or "")
        if row_date != date_key:
            print(f"  TAIFEX rows are for {row_date or '?'} but trade date is "
                  f"{date_key} — no data for this date (weekend/holiday?)")
            return None
        value = _num(_field(row, "OpenInterest(Net)", "多空未平倉口數淨額", "多空淨額"))
        if value is None:
            continue
        total += value
        matched += 1

    if matched == 0:
        print(f"  TAIFEX response had no {TX_PRODUCT_NAME}/{FOREIGN_PREFIX} rows")
        return None
    print(f"  TAIFEX 外資 {TX_PRODUCT_NAME} net OI: {total:+,.0f} contracts "
          f"({matched} row(s), all delivery months combined)")
    return total


def fetch_twse_foreign_net_buy(date_key: str) -> float | None:
    """外資 equity net buy (NTD) for *date_key* from TWSE BFI82U.

    Sums the 買賣差額 of every row whose 單位名稱 starts with 外資
    (外資及陸資(不含外資自營商) + 外資自營商).  None when the date is a
    holiday (stat != OK) or the response is malformed.
    """
    data = _http_json(TWSE_BFI82U_URL.format(date=date_key))
    if not isinstance(data, dict) or data.get("stat") != "OK":
        print(f"  TWSE BFI82U: no data for {date_key} "
              f"(stat={data.get('stat') if isinstance(data, dict) else 'n/a'!r})")
        return None

    fields = data.get("fields") or []
    try:
        diff_idx = fields.index("買賣差額")
    except ValueError:
        diff_idx = 3  # documented column order: 單位名稱, 買進, 賣出, 買賣差額

    total, matched = 0.0, 0
    for row in data.get("data") or []:
        if not row or not str(row[0]).startswith(FOREIGN_PREFIX):
            continue
        value = _num(row[diff_idx]) if diff_idx < len(row) else None
        if value is None:
            continue
        total += value
        matched += 1

    if matched == 0:
        print("  TWSE BFI82U response had no 外資 rows")
        return None
    print(f"  TWSE 外資 equity net buy: {total:+,.0f} NTD ({matched} row(s))")
    return total


# ── Signal logic ─────────────────────────────────────────────────────────

def decide(
    foreign_futures_net_oi: float,
    foreign_equity_net_buy_ntd: float,
    prev_foreign_futures_net_oi: float | None,
) -> tuple[str | None, str]:
    """(direction, reason) — direction is 'trending-up'/'trending-down'/None."""
    if abs(foreign_futures_net_oi) < HYSTERESIS_CONTRACTS:
        return None, (f"|net OI| {abs(foreign_futures_net_oi):,.0f} < "
                      f"{HYSTERESIS_CONTRACTS:,} hysteresis band — too weak")

    futures_direction = 1 if foreign_futures_net_oi > 0 else -1

    if foreign_equity_net_buy_ntd > EQUITY_FLOW_THRESHOLD_NTD:
        equity_direction = 1
    elif foreign_equity_net_buy_ntd < -EQUITY_FLOW_THRESHOLD_NTD:
        equity_direction = -1
    else:
        equity_direction = 0
    if equity_direction != 0 and equity_direction != futures_direction:
        return None, ("futures vs equity contradiction "
                      "(likely institutional hedging) — no vote")

    if abs(foreign_futures_net_oi) >= STRONG_CONTRACTS:
        reason = f"strong: |net OI| >= {STRONG_CONTRACTS:,}"
    else:
        if prev_foreign_futures_net_oi is None:
            return None, "weak signal and no previous-day OI for slope — no vote"
        slope = foreign_futures_net_oi - prev_foreign_futures_net_oi
        slope_agrees = (slope > 0) == (futures_direction > 0)
        if not slope_agrees:
            return None, f"weak signal and slope {slope:+,.0f} disagrees — no vote"
        reason = f"weak signal but slope {slope:+,.0f} agrees"

    direction = "trending-up" if futures_direction > 0 else "trending-down"
    return direction, reason


def compute_chips_direction(
    foreign_futures_net_oi: float,
    foreign_equity_net_buy_ntd: float,
    prev_foreign_futures_net_oi: float | None,
) -> str | None:
    """Returns 'trending-up', 'trending-down', or None (no vote)."""
    return decide(foreign_futures_net_oi, foreign_equity_net_buy_ntd,
                  prev_foreign_futures_net_oi)[0]


# ── Session key (mirrors crossmarket_monitor.py / rss_scorer.py) ─────────

def night_session_key(now: datetime) -> str:
    """Target the NIGHT session this vote applies to.

    Boundary is 05:00, not 15:00 — votes during 05:00-14:59 target
    tonight (today|NIGHT), not last night (already classified at ~04:58).
    """
    if now.hour >= 5:
        return f"{now.strftime('%Y-%m-%d')}|NIGHT"
    return f"{(now - timedelta(days=1)).strftime('%Y-%m-%d')}|NIGHT"


# ── Vote file plumbing ───────────────────────────────────────────────────

SOURCE = "W4"


def w4_vote_path(base_path: str) -> Path:
    """regime_vote.json → regime_vote_w4.json (same rule as _source_path)."""
    stem, ext = os.path.splitext(str(base_path))
    return Path(f"{stem}_{SOURCE.lower()}{ext}")


def _get_write_regime_vote():
    """Import write_regime_vote straight from the module file (scripts/ is
    not a package and src/news/__init__ pulls a wider dep chain)."""
    import importlib.util
    mod_path = Path(__file__).resolve().parents[2] / "src" / "news" / "regime_vote.py"
    spec = importlib.util.spec_from_file_location("regime_vote", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.write_regime_vote


def delete_stale_vote(base_path: str) -> None:
    vote_file = w4_vote_path(base_path)
    try:
        os.remove(vote_file)
        print(f"  stale {SOURCE} vote deleted: {vote_file}")
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"  could not delete stale vote: {type(e).__name__}: {e}")


# ── OI state sidecar (slope memory) ──────────────────────────────────────

def state_path_for(base_path: str) -> Path:
    return Path(base_path).parent / STATE_NAME


def load_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def previous_net_oi(state: dict, date_key: str) -> float | None:
    """Most recent recorded OI STRICTLY BEFORE *date_key* (re-runs on the
    same date must not compare today against today)."""
    history = state.get("history", {})
    if not isinstance(history, dict):
        return None
    prior = [d for d in history if d < date_key]
    if not prior:
        return None
    return _num(history[max(prior)])


def record_net_oi(state: dict, date_key: str, net_oi: float) -> dict:
    history = dict(state.get("history", {}) or {})
    history[date_key] = net_oi
    state["history"] = dict(sorted(history.items())[-STATE_KEEP_DAYS:])
    return state


def stamp_liveness(state: dict, now: datetime, summary: str) -> dict:
    """Stamp last_check/last_result (the W2/W3 liveness contract).

    Stamped on EVERY non-dry pass — including no-data days, which
    otherwise leave no trace and make a dead W4 indistinguishable from
    a TAIFEX holiday when reading the sidecar from disk."""
    state["last_check"] = now.isoformat(timespec="seconds")
    state["last_result"] = summary
    return state


# ── Liveness log (matches W2/W3 pattern) ─────────────────────────────────

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

def run_once(base_path: str, date_key: str, now: datetime,
             dry_run: bool = False, discord_webhook: str | None = None) -> int:
    """One W4 pass for trade date *date_key*.  Always exits 0 — a data
    outage is a no-op, not a failure (n8n retries daily anyway)."""
    print(f"[{now:%Y-%m-%d %H:%M:%S}] W4 chips monitor — trade date {date_key}"
          + (" (DRY RUN)" if dry_run else ""))

    spath = state_path_for(base_path)
    state = load_state(spath)

    net_oi = fetch_taifex_foreign_net_oi(date_key)
    if net_oi is None:
        no_data = "no TAIFEX data — no vote (weekend/holiday/outage)"
        print(f"  {no_data}")
        if not dry_run:
            delete_stale_vote(base_path)
            save_state(spath, stamp_liveness(state, now, no_data))
            append_log(Path(base_path).parent / LOG_NAME,
                       f"{now:%Y-%m-%d %H:%M:%S} TPE | {no_data}")
        return 0

    prev = previous_net_oi(state, date_key)
    print(f"  previous-day net OI: "
          f"{f'{prev:+,.0f}' if prev is not None else 'n/a (first run)'}")

    if not dry_run:
        # Record BEFORE deciding: tomorrow's slope needs today's OI even
        # on a no-vote day.
        save_state(spath, record_net_oi(state, date_key, net_oi))

    equity_net = fetch_twse_foreign_net_buy(date_key)
    if equity_net is None:
        # Contradiction check can't run — conservative: no vote at all.
        direction, reason = None, "TWSE equity flow unavailable — cannot rule out hedging, no vote"
    else:
        direction, reason = decide(net_oi, equity_net, prev)

    print(f"  decision: {direction or 'no vote'} ({reason})")

    session_key = night_session_key(now)
    if dry_run:
        if direction:
            print(f"  DRY RUN: would write {direction} for {session_key} "
                  f"-> {w4_vote_path(base_path)}")
        else:
            print(f"  DRY RUN: would delete {w4_vote_path(base_path)} if present")
        return 0

    equity_txt = f"{equity_net:+,.0f}" if equity_net is not None else "n/a"

    if direction:
        write_regime_vote = _get_write_regime_vote()
        write_regime_vote(base_path, direction, session_key, source=SOURCE)
        print(f"  VOTE WRITTEN: {direction} for {session_key} "
              f"-> {w4_vote_path(base_path)}")
        post_discord(discord_webhook,
                     f"📊 **W4 籌碼 regime vote: {direction}**\n"
                     f"外資 TX net OI: {net_oi:+,.0f} | equity: {equity_txt} NTD\n"
                     f"{reason} (contradiction check passed)")
    else:
        delete_stale_vote(base_path)
        if "contradiction" in reason or "disagrees" in reason:
            post_discord(discord_webhook,
                         f"⚠️ **W4 籌碼 no vote**\n"
                         f"外資 TX net OI: {net_oi:+,.0f} | equity: {equity_txt} NTD\n"
                         f"{reason}")
    summary = (f"oi {net_oi:+,.0f} | equity {equity_txt} | "
               f"vote: {direction or '-'} | {reason}")
    save_state(spath, stamp_liveness(state, now, summary))
    append_log(Path(base_path).parent / LOG_NAME,
               f"{now:%Y-%m-%d %H:%M:%S} TPE | {summary}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="W4 chips monitor -> regime_vote_w4.json")
    ap.add_argument("--base-path", default=None,
                    help="regime_vote.json base path (same as W2/W3 --vote-out)")
    ap.add_argument("--settings", default=None,
                    help="settings.yaml fallback for news.regime_vote_path")
    ap.add_argument("--date", default=None, metavar="YYYYMMDD",
                    help="trade date to fetch (default: today in TPE)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the would-be vote, write/delete nothing")
    ap.add_argument("--discord-webhook",
                    default=os.environ.get("NEWS_DISCORD_WEBHOOK") or None)
    args = ap.parse_args()

    base_path = args.base_path
    discord_webhook = args.discord_webhook
    if args.settings:
        try:
            import yaml
            with open(args.settings, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if not base_path:
                base_path = (cfg.get("news", {}) or {}).get("regime_vote_path", "")
            if not discord_webhook:
                discord_webhook = (cfg.get("news", {}) or {}).get("discord_webhook") or None
        except Exception as e:  # noqa: BLE001
            print(f"could not read settings: {type(e).__name__}: {e}")
    if not base_path:
        print("regime_vote_path not configured (empty --base-path and no "
              "news.regime_vote_path) — nothing to do")
        return 0

    now = datetime.now(TZ_TAIPEI)
    date_key = args.date or now.strftime("%Y%m%d")
    if len(date_key) != 8 or not date_key.isdigit():
        print(f"ERROR: --date must be YYYYMMDD, got {date_key!r}")
        return 1

    return run_once(base_path, date_key, now, dry_run=args.dry_run,
                    discord_webhook=discord_webhook)


if __name__ == "__main__":
    sys.exit(main())
