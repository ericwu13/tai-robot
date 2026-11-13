"""W4 籌碼 (institutional money flow) bridge — TAIFEX 三大法人 regime votes.

Standalone bridge script (stdlib only, same family as W2/W3).  Fired
several times per trading day (16:15 → 22:15 evening slots plus an 04:15
next-morning catch-up — the TAIFEX *website* posts the 三大法人 report at
~15:30 but the OpenAPI dataset lags it by hours, sometimes past midnight;
observed 2026-08-24: still serving the previous trading day at 16:15).
Each pass fetches:

- TAIFEX OpenAPI 區分各期貨契約三大法人交易資訊 — 外資 net open interest
  in 臺股期貨 (TX).  NOTE: this dataset aggregates ALL delivery months per
  contract (one row per 商品 × 身份別); there is no near/far-month split.
- TWSE BFI82U 三大法人買賣金額統計表 — 外資 equity net buy/sell in NTD,
  used as a contradiction check (futures short + equity buy = probably
  hedging, not a directional view).  The T86 endpoint is NOT used: T86 is
  a per-stock report with no market-level 外資 row.

and map them to a regime vote (see ``compute_chips_direction``).  The
futures signal is the DAY-OVER-DAY CHANGE in net OI (ΔOI), never the
level: 外資 carry a permanent structural hedge short (~-80,000
contracts), so the level clears every threshold in the short direction
every single day and can never turn positive.

- no OI baseline within ``MAX_PREV_GAP_DAYS`` calendar days
                                  → no vote (ΔOI undefined); today's OI
                                    is still recorded, so the next pass
                                    has a baseline
- ``|ΔOI| < 1,000``               → no vote (hysteresis band)
- futures vs equity contradiction → no vote (institutional hedging:
                                    adding shorts on a day of equity
                                    buying is a hedge adjustment)
- ``|ΔOI| >= 5,000``              → vote on the sign of ΔOI
- ``1,000 <= |ΔOI| < 5,000``      → vote only when the 外資 equity flow
                                    points the SAME way (active
                                    confirmation)

The previous OI comes from the ``.chips_state.json`` sidecar, which
records every fetched dataset — vote or not.  The same file keeps a
display-only ``daily`` map (trade date → net OI, ΔOI, equity flow,
decision, reason; last 60) written on decision passes only, for the
dashboard 籌碼 panel; ``history`` stays the ΔOI baseline.

There is no neutral vote — when uncertain the W4 vote file is DELETED so
a stale vote can never linger (votes are also consumed by the classifier
after every pass, so persisting a vote means re-writing it every run).
An empty TAIFEX result after a vote for the current session was already
decided, and a TWSE body with zero rows, are no-data: the vote file is
left as it is.

Usage:
    # normal run (n8n Execute Command node, several slots per weekday)
    python chips_monitor.py --base-path C:/n8n-bridge/regime_vote.json

    # inspect without writing anything
    python chips_monitor.py --base-path C:/n8n-bridge/regime_vote.json --dry-run

    # re-run for a specific trade date (testing; add --force to redo a
    # date that already reached a decision)
    python chips_monitor.py --base-path C:/n8n-bridge/regime_vote.json --date 20260814

Trade-date targeting: the OpenAPI takes NO date parameter — it serves
exactly ONE dataset, the latest published, whose lag is unbounded (still
the previous trading day at 22:15, sometimes past midnight).  A scheduled
pass therefore reads the trade date OFF the served rows and votes it as
long as it is within ``WALK_BACK_TRADING_DAYS`` trading days and newer
than the last decided one; older datasets are logged and ignored.
Pre-picking today's date instead is what left W4 voteless from 08-19: the
calendar moved on before the API caught up, so the lagging dataset was
never requested again.  ``--date`` keeps the strict exact-match rule for
manual backfills.

Dedup: once a dataset reaches a decision (vote OR deliberate no-vote —
API data for a closed day is final) its trade date is recorded in the
state sidecar, and later passes serving that date or an older one exit
early: one fetch, one Discord post, one decision per dataset.  On a
Saturday/Sunday the decision is DEFERRED, not taken: the night session
key would name a session that never opens, so the vote would expire
unread and its dedup entry would silence Monday's real vote.
"""
from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ_TAIPEI = ZoneInfo("Asia/Taipei")
_LOG = logging.getLogger("chips_monitor")


def _log_info(message: str) -> None:
    """INFO line for a decision. Also printed: this script has no log config."""
    _LOG.info(message)
    print(f"  INFO {message}")

TAIFEX_URL = ("https://openapi.taifex.com.tw/v1/"
              "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
TWSE_BFI82U_URL = ("https://www.twse.com.tw/rwd/zh/fund/BFI82U"
                   "?type=day&dayDate={date}&response=json")

TX_PRODUCT_NAME = "臺股期貨"   # strict match — excludes 小型臺股期貨 / options
FOREIGN_PREFIX = "外資"        # matches 外資 and the older 外資及陸資 label

# Signal thresholds (contracts / NTD) — NTNU study: 外資 is the only
# institution with 5/5 significant predictive metrics; TEJ backtest
# (2016-01 .. 2025-10) picked these bands.
#
# The contract bands are magnitudes of a DAILY CHANGE in net OI, not of
# the level.  外資 hold a permanent hedge short (-83,655 observed on
# 20260828): applied to the level, |OI| >= STRONG_CONTRACTS is true every
# day in the short direction, the sign can structurally never flip
# positive, and the weak band is unreachable dead code.  The numbers
# themselves are inherited from that level-era calibration — a
# re-calibration of both bands as ΔOI magnitudes against the TEJ backtest
# is PENDING.
HYSTERESIS_CONTRACTS = 1_000
STRONG_CONTRACTS = 5_000
EQUITY_FLOW_THRESHOLD_NTD = 5_000_000_000

# ΔOI only means anything against a RECENT baseline.  A Friday → Monday
# gap spans 3 calendar days and passes; anything wider (holiday run,
# bridge downtime) makes the recorded OI stale, so no Δ is computed.
MAX_PREV_GAP_DAYS = 5

# A scheduled pass accepts the served dataset when its own trade date is
# within this many TRADING days of the current one (weekends skipped).
WALK_BACK_TRADING_DAYS = 3

STATE_NAME = ".chips_state.json"
STATE_KEEP_DAYS = 10
# ``daily``: one display row per DECIDED trade date for the dashboard 籌碼
# panel (ΔOI, equity flow, decision, reason).  Separate from ``history``,
# the ΔOI baseline, which it never touches.  Nothing reads it back.
DAILY_KEEP_DAYS = 60
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

class ChipsFetchError(Exception):
    """TAIFEX or TWSE could not be reached (DNS, connection, timeout, HTTP 5xx)."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        self.message = message
        super().__init__(f"{kind}: {message}")


def _is_timeout_error(exc) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    return "timed out" in str(exc or "").lower()


def _http_json(url: str) -> object | None:
    """GET *url*, parse JSON.

    Raises ChipsFetchError on DNS, connection, timeout, any HTTP error
    (4xx and 5xx), a truncated read, a 200 whose body is empty or
    whitespace, or a 200 body that is not JSON (an HTML block page).
    A blank 200 is what a WAF or proxy can return; it is not an empty
    dataset.      Returns a parsed JSON value.  JSON ``null`` is a fetch failure.
    Callers treat TAIFEX ``[]`` and a TWSE object whose ``stat`` is not
    OK as no-data.  Tests that mock this to return None still mean
    "no data".  Any other exception is a fetch failure; the message
    names the exception type.
    """
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ChipsFetchError("http", f"HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        text = str(reason if reason is not None else exc)
        kind = "timeout" if _is_timeout_error(reason) else "network"
        raise ChipsFetchError(kind, text) from exc
    except TimeoutError as exc:
        raise ChipsFetchError("timeout", str(exc)) from exc
    except OSError as exc:
        kind = "timeout" if _is_timeout_error(exc) else "network"
        raise ChipsFetchError(kind, str(exc)) from exc
    except http.client.HTTPException as exc:
        # IncompleteRead / BadStatusLine are not OSError. A cut-off body
        # is a transport failure, not an empty dataset.
        raise ChipsFetchError("network", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # ValueError, UnicodeError, KeyError, and anything else that is
        # not a classified transport error. Name the type in the log.
        raise ChipsFetchError("error", f"{type(exc).__name__}: {exc}") from exc
    if isinstance(status, int) and status >= 400:
        raise ChipsFetchError("http", f"HTTP {status}")
    if raw is None or (isinstance(raw, (bytes, str)) and not raw.strip()):
        raise ChipsFetchError("http", "empty body")
    try:
        # utf-8-sig drops a leading BOM. json.load() on the raw bytes
        # accepted one; a strict utf-8 decode leaves U+FEFF and json.loads
        # then rejects the body.
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        parsed = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ChipsFetchError("http", f"non-JSON body: {type(exc).__name__}: {exc}") from exc
    if parsed is None:
        raise ChipsFetchError("http", "JSON null")
    return parsed


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

def fetch_taifex_tx_foreign_oi() -> tuple[str, float] | None:
    """``(dataset_date, 外資 net OI)`` for whatever the OpenAPI serves NOW.

    The endpoint takes no date parameter (probed) — it serves exactly ONE
    dataset, the latest one published, whose own row date can lag the
    calendar by more than a day.  So the caller reads the date OFF the
    data instead of pre-picking one; see ``run_once``'s walk-back.

    Returns None only for a genuine empty list (``[]``).  A non-list
    (an error object, JSON null) or a non-empty list with no 臺股期貨/外資
    rows is a fetch failure: the published dataset includes that contract,
    so a list that never mentions it is the wrong document, not a holiday.
    A list that does contain those rows, for another trade date, is still
    None from ``fetch_taifex_foreign_net_oi`` (no data for the requested
    date).  The dataset has one row per 商品 × 身份別 covering ALL delivery
    months combined; rows are summed defensively but normally exactly one
    matches.
    """
    data = _http_json(TAIFEX_URL)
    if data is None:
        return None
    if not isinstance(data, list):
        raise ChipsFetchError("http", f"TAIFEX JSON shape {type(data).__name__}")
    if not data:
        print("  TAIFEX response was an empty list")
        return None

    dataset_date, total, matched = "", 0.0, 0
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
        if not dataset_date:
            dataset_date = row_date
        elif row_date != dataset_date:
            continue  # defensive: one dataset, one trade date
        value = _num(_field(row, "OpenInterest(Net)", "多空未平倉口數淨額", "多空淨額"))
        if value is None:
            continue
        total += value
        matched += 1

    if matched == 0 or not dataset_date:
        raise ChipsFetchError(
            "http",
            f"TAIFEX JSON had no {TX_PRODUCT_NAME}/{FOREIGN_PREFIX} rows")
    print(f"  TAIFEX 外資 {TX_PRODUCT_NAME} net OI: {total:+,.0f} contracts "
          f"({matched} row(s), all delivery months combined, data {dataset_date})")
    return dataset_date, total


def fetch_taifex_foreign_net_oi(date_key: str) -> float | None:
    """外資 net OI (contracts) in TX for trade date *date_key*, exact match.

    Manual-backfill semantics (``--date``): a dataset for any other date
    is "no data for this date", never a substitute.
    """
    found = fetch_taifex_tx_foreign_oi()
    if found is None:
        return None
    dataset_date, total = found
    if dataset_date != date_key:
        print(f"  TAIFEX rows are for {dataset_date or '?'} but trade date is "
              f"{date_key} — no data for this date (weekend/holiday?)")
        return None
    return total


class _TwseNoData:
    """Marker: TWSE ``stat`` is OK and the data array has zero rows."""

    def __repr__(self) -> str:
        return "no-data"


_TWSE_NO_DATA = _TwseNoData()


def fetch_twse_foreign_net_buy(date_key: str) -> float | None | _TwseNoData:
    """外資 equity net buy (NTD) for *date_key* from TWSE BFI82U.

    Sums the 買賣差額 of every row whose 單位名稱 starts with 外資
    (外資及陸資(不含外資自營商) + 外資自營商).  None when the date is a
    holiday (a dict whose ``stat`` is not OK).  A non-dict, or a dict
    with no ``stat`` (an error envelope), is a fetch failure.
    ``stat`` OK with no data rows (missing or ``[]``) is no-data
    (``_TWSE_NO_DATA``): the caller logs that and does not delete or
    overwrite the vote.
    """
    data = _http_json(TWSE_BFI82U_URL.format(date=date_key))
    if data is None:
        return None
    if not isinstance(data, dict) or "stat" not in data:
        raise ChipsFetchError(
            "http", f"TWSE JSON shape {type(data).__name__}")
    if data.get("stat") != "OK":
        print(f"  TWSE BFI82U: no data for {date_key} "
              f"(stat={data.get('stat')!r})")
        return None

    rows = data.get("data")
    if rows is None or rows == []:
        # Zero rows is an empty dataset, not "equity flow unavailable".
        # The caller logs no-data and leaves any vote file alone.
        print(f"  no-data — TWSE BFI82U zero rows for {date_key}")
        return _TWSE_NO_DATA

    fields = data.get("fields") or []
    try:
        diff_idx = fields.index("買賣差額")
    except ValueError:
        diff_idx = 3  # documented column order: 單位名稱, 買進, 賣出, 買賣差額

    total, matched = 0.0, 0
    for row in rows:
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
    foreign_futures_oi_delta: float,
    foreign_equity_net_buy_ntd: float,
) -> tuple[str | None, str]:
    """(direction, reason) — direction is 'trending-up'/'trending-down'/None.

    *foreign_futures_oi_delta* is the DAY-OVER-DAY CHANGE in 外資 TX net
    OI (today's level minus the previously recorded one), never the level
    — see the threshold note above.
    """
    if abs(foreign_futures_oi_delta) < HYSTERESIS_CONTRACTS:
        return None, (f"|ΔOI| {abs(foreign_futures_oi_delta):,.0f} < "
                      f"{HYSTERESIS_CONTRACTS:,} hysteresis band — too weak")

    futures_direction = 1 if foreign_futures_oi_delta > 0 else -1

    if foreign_equity_net_buy_ntd > EQUITY_FLOW_THRESHOLD_NTD:
        equity_direction = 1
    elif foreign_equity_net_buy_ntd < -EQUITY_FLOW_THRESHOLD_NTD:
        equity_direction = -1
    else:
        equity_direction = 0
    if equity_direction != 0 and equity_direction != futures_direction:
        # 外資 adding futures shorts on a day they bought equities (or the
        # mirror) is a hedge adjustment, not a directional view.
        return None, ("futures vs equity contradiction "
                      "(likely institutional hedging) — no vote")

    if abs(foreign_futures_oi_delta) >= STRONG_CONTRACTS:
        reason = f"strong: |ΔOI| >= {STRONG_CONTRACTS:,}"
    elif equity_direction == futures_direction:
        # A weak ΔOI needs ACTIVE confirmation from the cash market.  The
        # old day-over-day slope check is circular now that the input IS
        # the slope, so equity flow is the only independent witness left.
        reason = "weak ΔOI but equity flow agrees"
    else:
        return None, "weak ΔOI and no confirming equity flow — no vote"

    direction = "trending-up" if futures_direction > 0 else "trending-down"
    return direction, reason


def compute_chips_direction(
    foreign_futures_oi_delta: float,
    foreign_equity_net_buy_ntd: float,
) -> str | None:
    """Returns 'trending-up', 'trending-down', or None (no vote)."""
    return decide(foreign_futures_oi_delta, foreign_equity_net_buy_ntd)[0]


# ── Session key (mirrors crossmarket_monitor.py / rss_scorer.py) ─────────

def night_session_key(now: datetime) -> str:
    """Target the NIGHT session this vote applies to.

    Boundary is 05:00, not 15:00 — votes during 05:00-14:59 target
    tonight (today|NIGHT), not last night (already classified at ~04:58).
    """
    if now.hour >= 5:
        return f"{now.strftime('%Y-%m-%d')}|NIGHT"
    return f"{(now - timedelta(days=1)).strftime('%Y-%m-%d')}|NIGHT"


def default_trade_date(now: datetime) -> str:
    """Trade date (YYYYMMDD) whose 三大法人 report this pass should fetch.

    Mirrors the night_session_key boundary: the 04:15 catch-up slot runs
    inside the night session that OPENED yesterday, so it wants
    yesterday's report — today's doesn't exist yet.  From 05:00 the
    target flips to today (tonight's session, classified at ~04:58
    tomorrow).
    """
    if now.hour >= 5:
        return now.strftime("%Y%m%d")
    return (now - timedelta(days=1)).strftime("%Y%m%d")


def walk_back_dates(now: datetime) -> list[str]:
    """Trade dates a scheduled pass may act on, newest first.

    ``default_trade_date(now)`` and the ``WALK_BACK_TRADING_DAYS - 1``
    trading days before it (Sat/Sun skipped; TAIFEX holidays are not
    modelled — an unpublished date simply never shows up as the served
    dataset).  The OpenAPI regularly lags a full day and once lagged a
    weekend, so requesting only today made every late dataset invisible.
    """
    day = datetime.strptime(default_trade_date(now), "%Y%m%d")
    dates = []
    while len(dates) < WALK_BACK_TRADING_DAYS:
        if day.weekday() < 5:
            dates.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
    return dates


def is_weekend_session(session_key: str) -> bool:
    """True when *session_key* names a Sat/Sun night — no such session."""
    try:
        day = datetime.strptime(session_key.split("|")[0], "%Y-%m-%d")
    except ValueError:
        return False
    return day.weekday() >= 5


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


def _vote_session(base_path: str) -> str:
    """``expires_after_session`` of the W4 vote on disk, or ``""``."""
    try:
        with open(w4_vote_path(base_path), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("expires_after_session") or "")


def delete_stale_vote(base_path: str) -> None:
    vote_file = w4_vote_path(base_path)
    try:
        os.remove(vote_file)
        print(f"  stale {SOURCE} vote deleted: {vote_file}")
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"  could not delete stale vote: {type(e).__name__}: {e}")


# ── OI state sidecar (ΔOI baseline memory) ───────────────────────────────

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


def previous_net_oi(state: dict, date_key: str) -> tuple[str, float] | None:
    """``(date, OI)`` of the most recent record STRICTLY BEFORE *date_key*.

    Strictly-before matters for re-runs: comparing today against the value
    this same pass just recorded would make every ΔOI zero.  The date is
    returned too — the caller rejects a baseline older than
    ``MAX_PREV_GAP_DAYS``, which a bare value can't express.
    """
    history = state.get("history", {})
    if not isinstance(history, dict):
        return None
    prior = [d for d in history if d < date_key]
    if not prior:
        return None
    prev_date = max(prior)
    value = _num(history[prev_date])
    return None if value is None else (prev_date, value)


def date_gap_days(earlier: str, later: str) -> int | None:
    """Calendar days between two YYYYMMDD keys; None when unparseable."""
    try:
        start = datetime.strptime(earlier, "%Y%m%d")
        end = datetime.strptime(later, "%Y%m%d")
    except ValueError:
        return None
    return (end - start).days


def record_net_oi(state: dict, date_key: str, net_oi: float) -> dict:
    history = dict(state.get("history", {}) or {})
    history[date_key] = net_oi
    state["history"] = dict(sorted(history.items())[-STATE_KEEP_DAYS:])
    return state


def record_daily(state: dict, date_key: str, row: dict) -> dict:
    """Upsert the display row for *date_key*, keeping the newest
    ``DAILY_KEEP_DAYS`` dates.  A malformed ``daily`` is replaced."""
    daily = state.get("daily")
    daily = dict(daily) if isinstance(daily, dict) else {}
    daily[date_key] = row
    state["daily"] = dict(sorted(daily.items())[-DAILY_KEEP_DAYS:])
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

def _is_closed_day(date_key: str | None) -> bool:
    """Weekend, or a weekday ``is_taifex_holiday`` confirms.

    If the holiday calendar cannot be imported, only weekends count.
    """
    try:
        day = datetime.strptime(str(date_key), "%Y%m%d").date()
    except (TypeError, ValueError):
        return False
    if day.weekday() >= 5:
        return True
    # Launched as ``python scripts/news_bridge/chips_monitor.py``, sys.path[0]
    # is that directory, not the repo root. The weekend check above does not
    # need this import; a weekday holiday does.
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from src.market_data.holidays import is_taifex_holiday
    except Exception:  # noqa: BLE001 — weekend-only fallback
        return False
    try:
        return bool(is_taifex_holiday(day))
    except Exception:  # noqa: BLE001
        return False


def no_data_message(date_key: str | None) -> str:
    """Wording for a genuine empty dataset. Keeps the ``no TAIFEX data``
    prefix the voteless-streak check and the existing no-data tests use.
    ``holiday`` appears only when the day is actually closed."""
    if _is_closed_day(date_key):
        return "no TAIFEX data — TAIFEX holiday — no vote"
    return "no TAIFEX data — no data yet (API lag) — no vote"


def _clear_fetch_streak(state: dict) -> None:
    state["fetch_error_streak"] = 0


def _abort_fetch(state: dict, now: datetime, spath, base_path: str,
                 dry_run: bool, source: str, exc: ChipsFetchError) -> int:
    """Transport failure: keep the vote, do not mark the day decided.

    The log line must not contain ``no TAIFEX data`` — that phrase is what
    check_bridge counts as a voteless streak.  Exit 1 so the scheduled
    task shows failed; a later slot retries because last_decided_date is
    left unset.
    """
    msg = (f"{source} fetch failed ({exc.kind}: {exc.message}) "
           "— vote kept, no decision")
    print(f"  {msg}")
    if not dry_run:
        state["fetch_error_streak"] = int(state.get("fetch_error_streak") or 0) + 1
        state["last_fetch_error"] = msg[:300]
        save_state(spath, stamp_liveness(state, now, msg))
        append_log(Path(base_path).parent / LOG_NAME,
                   f"{now:%Y-%m-%d %H:%M:%S} TPE | {msg}")
    return 1


def _no_data_keep_vote(state: dict, now: datetime, spath, base_path: str,
                       dry_run: bool, message: str) -> int:
    """Log *message* and leave the vote file bytes alone. Exit 0.

    A genuine TAIFEX ``[]`` after a vote for this session was already
    decided, and a TWSE zero-row body, both come through here. Neither
    deletes nor rewrites the vote, and neither marks the day decided.
    """
    print(f"  {message}")
    if not dry_run:
        _clear_fetch_streak(state)
        save_state(spath, stamp_liveness(state, now, message))
        append_log(Path(base_path).parent / LOG_NAME,
                   f"{now:%Y-%m-%d %H:%M:%S} TPE | {message}")
    return 0


def run_once(base_path: str, date_key: str | None, now: datetime,
             dry_run: bool = False, discord_webhook: str | None = None,
             force: bool = False) -> int:
    """One W4 pass.  A transport failure (DNS, connection, timeout, HTTP
    error, non-JSON) on the TAIFEX or TWSE path exits 1 so the scheduled
    task shows failed; the vote file is kept and the trade date is not
    marked decided, so the retry slots keep probing.  A genuine no-data
    day or holiday still exits 0 and deletes a stale vote when this
    pass has not already decided one for the current session.  An empty
    TAIFEX result (``[]``) after that — the on-disk vote's session is
    still ``night_session_key(now)``, or an explicit ``--date`` repeats
    ``last_decided_date`` — is no-data and leaves the vote untouched.
    A later day's empty result still deletes.  A TWSE response with
    zero rows is no-data too: the vote is not deleted or overwritten.

    *date_key* explicit (``--date``) = manual backfill: that trade date
    is requested and anything else is "no data".  *date_key* None = the
    scheduled path: the served dataset's OWN trade date decides, as long
    as it is inside the walk-back window and newer than the last decided
    one.  Pre-picking today's date is what left W4 voteless from 08-19 —
    the OpenAPI never caught up before the calendar moved on.
    """
    scheduled = not date_key
    print(f"[{now:%Y-%m-%d %H:%M:%S}] W4 chips monitor — trade date "
          f"{date_key or f'as served (walk-back from {default_trade_date(now)})'}"
          + (" (DRY RUN)" if dry_run else ""))

    spath = state_path_for(base_path)
    state = load_state(spath)
    decided = str(state.get("last_decided_date") or "")
    # None until a genuine empty dataset. Specific "wrong date" messages
    # replace it; a transport error never reaches this string.
    no_data = None
    # True only when the fetch itself returned no rows ([] / None), not
    # when a dated dataset was discarded for being ahead or too old.
    empty_result = False

    # Retry-slot dedup: a decision for a dataset is final (the API data
    # won't change), so later slots for it are no-ops — no re-fetch, no
    # duplicate Discord post.  No-DATA passes never set this, so retries
    # keep probing until the OpenAPI catches up.  A transport error also
    # leaves last_decided_date unset, so those slots keep trying.
    try:
        if date_key:
            if not dry_run and not force and decided == date_key:
                print(f"  already decided for {date_key} — skipping (--force to redo)")
                return 0
            net_oi = fetch_taifex_foreign_net_oi(date_key)
            empty_result = net_oi is None
        else:
            newest = default_trade_date(now)
            window = walk_back_dates(now)
            found = fetch_taifex_tx_foreign_oi()
            empty_result = found is None
            date_key, net_oi = (newest, None) if found is None else found
            if net_oi is not None and date_key > newest:
                no_data = (f"no TAIFEX data — dataset {date_key} is ahead of trade "
                           f"date {newest}, ignoring")
                net_oi = None
                empty_result = False
            elif net_oi is not None and not dry_run and not force \
                    and decided and date_key <= decided:
                # Liveness still gets stamped: a deduped pass is a HEALTHY
                # pass, and without the stamp a long weekend + API lag pushes
                # last_check past the monitor's 72h threshold (false "W4
                # bridge stale").  The log line also keeps every cron slot
                # visible to check_bridge's slot heuristic.
                msg = (f"nothing new — dataset {date_key} already decided "
                       f"(last decided {decided})")
                print(f"  {msg}")
                _clear_fetch_streak(state)
                save_state(spath, stamp_liveness(state, now, msg))
                append_log(Path(base_path).parent / LOG_NAME,
                           f"{now:%Y-%m-%d %H:%M:%S} TPE | {msg}")
                return 0
            elif net_oi is not None and date_key not in window:
                no_data = (f"no TAIFEX data — dataset {date_key} is older than the "
                           f"{WALK_BACK_TRADING_DAYS}-trading-day walk-back window "
                           f"({window[-1]}..{window[0]})")
                net_oi = None
                empty_result = False
    except ChipsFetchError as exc:
        return _abort_fetch(state, now, spath, base_path, dry_run, "TAIFEX", exc)

    if net_oi is None:
        if not no_data:
            no_data = no_data_message(date_key)
        # [] after a decision for this session is no-data, but the
        # vote file and last_decided_date stay. A scheduled pass keeps
        # the vote when it still names tonight — a lagging dataset
        # (Friday's rows voted for Monday night) is "that day" even
        # though last_decided_date is not today's calendar date. An
        # explicit --date keeps it only when that date was the one
        # decided, so the next day's empty result still deletes.
        same_explicit_day = (not scheduled) and bool(decided) and str(date_key or "") == decided
        same_session = scheduled and bool(decided) and _vote_session(base_path) == night_session_key(now)
        if empty_result and (same_explicit_day or same_session):
            return _no_data_keep_vote(
                state, now, spath, base_path, dry_run,
                f"{no_data} — decided vote for {date_key} left unchanged")
        print(f"  {no_data}")
        if not dry_run:
            delete_stale_vote(base_path)
            _clear_fetch_streak(state)
            save_state(spath, stamp_liveness(state, now, no_data))
            append_log(Path(base_path).parent / LOG_NAME,
                       f"{now:%Y-%m-%d %H:%M:%S} TPE | {no_data}")
        return 0

    session_key = night_session_key(now)
    if scheduled and is_weekend_session(session_key):
        # A Sat/Sun key names a night session that never opens: voting it
        # would both expire unread AND dedup-block the Monday pass that
        # must vote this dataset for Monday|NIGHT.  No decision is
        # recorded — but liveness IS stamped, or a long weekend pushes
        # last_check past the monitor's 72h stale threshold.
        msg = (f"weekend — deferring decision on {date_key} to the next "
               f"trading day")
        print(f"  {msg}")
        if not dry_run:
            _clear_fetch_streak(state)
            save_state(spath, stamp_liveness(state, now, msg))
            append_log(Path(base_path).parent / LOG_NAME,
                       f"{now:%Y-%m-%d %H:%M:%S} TPE | {msg}")
        return 0

    # The signal is the CHANGE in net OI, so a recent baseline is
    # mandatory; a stale one (holiday run, bridge downtime) is worse than
    # none because it silently inflates Δ across the gap.
    prev_date, prev_oi = None, None
    prev_entry = previous_net_oi(state, date_key)
    if prev_entry is not None:
        gap = date_gap_days(prev_entry[0], date_key)
        if gap is None or gap > MAX_PREV_GAP_DAYS:
            print(f"  previous net OI {prev_entry[1]:+,.0f} is from "
                  f"{prev_entry[0]} ({gap if gap is not None else '?'} calendar "
                  f"days back, max {MAX_PREV_GAP_DAYS}) — too stale for ΔOI")
        else:
            prev_date, prev_oi = prev_entry
    print(f"  previous net OI: "
          f"{f'{prev_oi:+,.0f} ({prev_date})' if prev_oi is not None else 'n/a'}")

    if not dry_run:
        # Record BEFORE deciding: tomorrow's ΔOI needs today's OI even on
        # a no-vote day (prev was read above, so this can't self-compare).
        save_state(spath, record_net_oi(state, date_key, net_oi))

    if prev_oi is None:
        # First pass after a deploy (or after a long gap) abstains by
        # design — it has just laid down the baseline the next one needs.
        delta, equity_net = None, None
        direction, reason = None, ("no recent OI history for ΔOI (need previous "
                                   "trading day) — recording only")
    else:
        delta = net_oi - prev_oi
        try:
            equity_net = fetch_twse_foreign_net_buy(date_key)
        except ChipsFetchError as exc:
            # Keep the vote on disk and do not mark the day decided.
            # A TWSE transport failure (timeout, HTTP error, non-JSON)
            # is not an equity-flow reading. The old "unavailable →
            # delete" rule is only for a parsed empty flow (None).
            # Deleting here would drop a vote already written for this
            # night from an earlier dataset while today's ΔOI is still
            # unconfirmed, and a later slot must be able to retry.
            _log_info(
                f"TWSE transport failure ({exc.kind}: {exc.message}) for "
                f"{date_key} — keeping the vote on disk and leaving the "
                f"day undecided so a later slot can retry. A transport "
                f"error is not an equity-flow reading."
            )
            return _abort_fetch(state, now, spath, base_path, dry_run, "TWSE", exc)
        if equity_net is _TWSE_NO_DATA:
            return _no_data_keep_vote(
                state, now, spath, base_path, dry_run,
                f"no-data — TWSE BFI82U zero rows for {date_key}; "
                f"vote left unchanged")
        if equity_net is None:
            # Contradiction check can't run — conservative: no vote at all.
            direction, reason = None, "TWSE equity flow unavailable — cannot rule out hedging, no vote"
        else:
            direction, reason = decide(delta, equity_net)

    flow_txt = (f"ΔOI {delta:+,.0f} (level {net_oi:+,.0f}, prev {prev_date})"
                if delta is not None
                else f"ΔOI n/a (level {net_oi:+,.0f}, no prev)")
    print(f"  {flow_txt}")
    print(f"  decision: {direction or 'no vote'} ({reason})")

    if dry_run:
        if direction:
            print(f"  DRY RUN: would write {direction} for {session_key} "
                  f"(data {date_key}) -> {w4_vote_path(base_path)}")
        else:
            print(f"  DRY RUN: would delete {w4_vote_path(base_path)} if present")
        return 0

    equity_txt = f"{equity_net:+,.0f}" if equity_net is not None else "n/a"

    if direction:
        write_regime_vote = _get_write_regime_vote()
        write_regime_vote(base_path, direction, session_key, source=SOURCE,
                          data_date=date_key)
        print(f"  VOTE WRITTEN: {direction} for {session_key} (data {date_key}) "
              f"-> {w4_vote_path(base_path)}")
        post_discord(discord_webhook,
                     f"📊 **W4 籌碼 regime vote: {direction}**\n"
                     f"外資 TX {flow_txt} | equity: {equity_txt} NTD\n"
                     f"{reason} (contradiction check passed)\n"
                     f"資料日 data {date_key} → {session_key}")
    else:
        delete_stale_vote(base_path)
        if "contradiction" in reason or "no confirming equity flow" in reason:
            post_discord(discord_webhook,
                         f"⚠️ **W4 籌碼 no vote**\n"
                         f"外資 TX {flow_txt} | equity: {equity_txt} NTD\n"
                         f"{reason}\n資料日 data {date_key}")
    summary = (f"{flow_txt} | equity {equity_txt} | "
               f"vote: {direction or '-'} | {reason} (data {date_key})")
    if equity_net is not None:
        # Decision is final only when BOTH sources answered.  A TWSE
        # outage no-vote stays retryable — a later slot may still turn
        # today's OI into a vote once the contradiction check can run.
        state["last_decided_date"] = date_key
    record_daily(state, date_key, {
        "net_oi": net_oi, "delta_oi": delta, "prev_date": prev_date,
        "equity_net": equity_net, "decision": direction, "reason": reason,
        "final": equity_net is not None, "session": session_key,
        "at": now.isoformat(timespec="seconds"),
    })
    _clear_fetch_streak(state)
    save_state(spath, stamp_liveness(state, now, summary))
    append_log(Path(base_path).parent / LOG_NAME,
               f"{now:%Y-%m-%d %H:%M:%S} TPE | {summary}")
    return 0


class _NoRunLog:
    """Stand-in when src/news/bridge_runs.py cannot be loaded."""
    result = ""
    exit_code = None

    def fail(self, error):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _run_recorder(bridge_dir, workflow: str):
    """``bridge_runs.run_recorder`` (runs/<W>.jsonl for the dashboard's
    工作流程 view), loaded by file path like ``_get_write_regime_vote``.
    Bookkeeping only — a load failure degrades to a no-op."""
    try:
        import importlib.util
        mod_path = Path(__file__).resolve().parents[2] / "src" / "news" / "bridge_runs.py"
        spec = importlib.util.spec_from_file_location("bridge_runs", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.run_recorder(bridge_dir, workflow)
    except Exception as e:  # noqa: BLE001
        print(f"  run log unavailable: {type(e).__name__}: {e}")
        return _NoRunLog()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="W4 chips monitor -> regime_vote_w4.json")
    ap.add_argument("--base-path", default=None,
                    help="regime_vote.json base path (same as W2/W3 --vote-out)")
    ap.add_argument("--settings", default=None,
                    help="settings.yaml fallback for news.regime_vote_path")
    ap.add_argument("--date", default=None, metavar="YYYYMMDD",
                    help="exact trade date to fetch (default: whatever trade "
                         "date the OpenAPI is serving, accepted while it is "
                         f"within {WALK_BACK_TRADING_DAYS} trading days)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the would-be vote, write/delete nothing")
    ap.add_argument("--force", action="store_true",
                    help="re-run a trade date that already reached a decision")
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
    date_key = args.date
    if date_key and (len(date_key) != 8 or not date_key.isdigit()):
        print(f"ERROR: --date must be YYYYMMDD, got {date_key!r}")
        return 1

    if args.dry_run:
        return run_once(base_path, date_key, now, dry_run=True,
                        discord_webhook=discord_webhook, force=args.force)
    with _run_recorder(Path(base_path).parent, "W4") as run:
        code = run_once(base_path, date_key, now,
                        discord_webhook=discord_webhook, force=args.force)
        run.exit_code = code
        run.result = str(load_state(state_path_for(base_path)).get("last_result") or "")
        if code:
            run.fail(run.result or "fetch failed")
        return code


if __name__ == "__main__":
    sys.exit(main())
