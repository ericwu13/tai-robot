"""Zero-API-key, pure-stdlib fetchers & TAIFEX session mapper for W1 (events.json).

Official public sources (no scraping of paywalled/WAF sites, no paid keys):
- **Federal Reserve** (``fomccalendars.htm``): FOMC rate decisions & SEP/dot-plot (`*`)
- **US BLS** (``bls.ics`` + ``cpi.htm`` / ``empsit.htm`` lookahead): US CPI, NFP, PPI
- **US BEA** (``release_dates.json``): US PCE (Personal Income and Outlays) & GDP
- **Nasdaq Analyst API** (``api.nasdaq.com/api/analyst/{SYM}/earnings-date``):
  Tier-1 AI/semi & hyperscaler earnings dates (confirmed vs estimated, BMO vs AMC)
- **TAIFEX Settlement** (``src.market_data.holidays.settlement_day``): monthly expiry

All parsers are pure functions so tests run 100% offline with fixed ``now``.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

DEFAULT_CALENDAR_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "calendar_sources.yaml"

_TZ_TAIPEI = timezone(timedelta(hours=8))
_NY_TZ = ZoneInfo("America/New_York")

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BLS_CPI_HTML_URL = "https://www.bls.gov/schedule/news_release/cpi.htm"
BLS_NFP_HTML_URL = "https://www.bls.gov/schedule/news_release/empsit.htm"
BEA_JSON_URL = "https://apps.bea.gov/API/signup/release_dates.json"
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/analyst/{symbol}/earnings-date"

_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (tai-robot calendar)",
    "Connection": "close",
}
_NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
    "Connection": "close",
}

_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}

_MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

HttpGet = Callable[[str, dict[str, str], int], str]


def default_http_get(url: str, headers: dict[str, str], timeout_s: int) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── Configuration ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TickerRule:
    symbol: str
    label: str
    severity: str
    default_timing: str = "amc"  # "amc" | "bmo"


@dataclass(frozen=True)
class CalendarConfig:
    lookahead_days: int = 45
    lookback_days: int = 2
    timeout_s: int = 15
    fomc_enabled: bool = True
    fomc_severity: str = "high"
    bls_enabled: bool = True
    cpi_severity: str = "high"
    nfp_severity: str = "high"
    ppi_severity: str = "medium"
    bea_enabled: bool = True
    pce_severity: str = "medium"
    gdp_severity: str = "medium"
    nasdaq_enabled: bool = True
    include_estimated_earnings: bool = True
    amc_include_edt_night: bool = False
    tickers: tuple[TickerRule, ...] = ()
    txf_settlement_enabled: bool = True
    txf_settlement_severity: str = "low"


def _sev(raw: Any, default: str) -> str:
    s = str(raw or default).strip().lower()
    return s if s in _SEVERITY_RANK else default


def load_calendar_config(path: str | Path | None = None) -> CalendarConfig:
    """Load ``config/calendar_sources.yaml`` with safe defaults."""
    cfg_path = Path(path) if path else DEFAULT_CALENDAR_CONFIG_PATH
    data: dict[str, Any] = {}
    if cfg_path.is_file():
        with open(cfg_path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                data = loaded

    win = data.get("window") or {}
    http = data.get("http") or {}
    srcs = data.get("sources") or {}
    fomc = srcs.get("fomc") or {}
    bls = srcs.get("bls") or {}
    bea = srcs.get("bea") or {}
    nq = srcs.get("nasdaq_earnings") or {}
    txf = srcs.get("txf_settlement") or {}

    tickers: list[TickerRule] = []
    raw_tickers = nq.get("tickers")
    if isinstance(raw_tickers, dict):
        for sym, spec in raw_tickers.items():
            if not isinstance(spec, dict):
                continue
            s = str(sym).strip().upper()
            if not s:
                continue
            timing = str(spec.get("default_timing") or "amc").strip().lower()
            if timing not in ("amc", "bmo"):
                timing = "amc"
            tickers.append(
                TickerRule(
                    symbol=s,
                    label=str(spec.get("label") or s).strip(),
                    severity=_sev(spec.get("severity"), "medium"),
                    default_timing=timing,
                )
            )

    return CalendarConfig(
        lookahead_days=max(1, int(win.get("lookahead_days", 45))),
        lookback_days=max(0, int(win.get("lookback_days", 2))),
        timeout_s=max(3, int(http.get("timeout_s", 15))),
        fomc_enabled=bool(fomc.get("enabled", True)),
        fomc_severity=_sev(fomc.get("severity"), "high"),
        bls_enabled=bool(bls.get("enabled", True)),
        cpi_severity=_sev(bls.get("cpi_severity"), "high"),
        nfp_severity=_sev(bls.get("nfp_severity"), "high"),
        ppi_severity=_sev(bls.get("ppi_severity"), "medium"),
        bea_enabled=bool(bea.get("enabled", True)),
        pce_severity=_sev(bea.get("pce_severity"), "medium"),
        gdp_severity=_sev(bea.get("gdp_severity"), "medium"),
        nasdaq_enabled=bool(nq.get("enabled", True)),
        include_estimated_earnings=bool(nq.get("include_estimated", True)),
        amc_include_edt_night=bool(nq.get("amc_include_edt_night", False)),
        tickers=tuple(tickers),
        txf_settlement_enabled=bool(txf.get("enabled", True)),
        txf_settlement_severity=_sev(txf.get("severity"), "low"),
    )


# ── TAIFEX trading-day & session mapping (EDT vs EST aware) ──────────────

def _next_tw_trading_day(d: date) -> date:
    """First TAIFEX trading day >= *d* (falls back to Mon-Fri if holidays pkg missing)."""
    try:
        from src.market_data.holidays import next_trading_day  # noqa: PLC0415

        return next_trading_day(d)
    except Exception:  # noqa: BLE001 — fallback for minimal environments
        cur = d
        while cur.weekday() >= 5:
            cur += timedelta(days=1)
        return cur


def _is_us_edt(d: date) -> bool:
    """True if *d* at 16:00 America/New_York is in Daylight Saving Time (EDT, UTC-4)."""
    dt = datetime(d.year, d.month, d.day, 16, 0, tzinfo=_NY_TZ)
    dst = dt.dst()
    return bool(dst and dst > timedelta(0))


def map_us_release_to_taifex_sessions(
    us_date: date,
    timing: str,
    *,
    amc_include_edt_night: bool = False,
) -> list[tuple[date, list[str]]]:
    """Map a US release (*us_date*, *timing*) to TAIFEX ``[(open_date, sessions)]``.

    - ``"macro_am"`` (08:30 ET = 20:30/21:30 TPE) & ``"fomc"`` (14:00 ET =
      02:00/03:00 TPE next day) & ``"bmo"`` (before US open, e.g. TSMC 14:00 TPE):
      occur directly inside or right before ``us_date|NIGHT`` -> ``[(us_date, ["NIGHT"])]``.
    - ``"amc"`` (US after-market-close ~16:15 ET):
      * EST (Nov–Mar): 16:15 EST = 05:15 TPE next day — ``us_date|NIGHT`` ALREADY
        CLOSED at 05:00 TPE! Maps strictly to ``next_tw_trading_day(us_date + 1) ["DAY"]``.
      * EDT (Mar–Nov): 16:15 EDT = 04:15 TPE next day (last 45m of NIGHT + full
        Taiwan cash open at 08:45 TPE). Maps to ``next_tw_trading_day(us_date + 1) ["DAY"]``
        (and optionally ``us_date ["NIGHT"]`` when *amc_include_edt_night* is True).
    """
    t = timing.strip().lower()
    if t in ("macro_am", "fomc", "bmo"):
        return [(us_date, ["NIGHT"])]
    if t == "amc":
        next_day = _next_tw_trading_day(us_date + timedelta(days=1))
        out: list[tuple[date, list[str]]] = []
        if amc_include_edt_night and _is_us_edt(us_date):
            out.append((us_date, ["NIGHT"]))
        out.append((next_day, ["DAY"]))
        return out
    return [(us_date, ["NIGHT"])]


# ── Pure parsers (100% offline unit-testable) ────────────────────────────

def _parse_month_token(token: str) -> int | None:
    cleaned = token.strip().rstrip(".").lower()
    return _MONTH_MAP.get(cleaned)


def parse_fomc_html(html: str, *, severity: str = "high") -> list[dict[str, Any]]:
    """Parse official Federal Reserve ``fomccalendars.htm`` into event dicts.

    FOMC announces at 14:00 ET on Day 2 of each meeting. Handles single-month
    (``January`` + ``27-28``), cross-month (``April/May`` + ``30-1``), and
    SEP/dot-plot markers (``8-9*``).
    """
    events: list[dict[str, Any]] = []
    year_blocks = list(re.finditer(r"(\d{4})\s+FOMC\s+Meetings", html))
    for i, ym in enumerate(year_blocks):
        year = int(ym.group(1))
        start = ym.end()
        end = year_blocks[i + 1].start() if i + 1 < len(year_blocks) else len(html)
        block = html[start:end]
        pairs = re.findall(
            r"fomc-meeting__month[^>]*>\s*(?:<strong>)?([^<]+?)(?:</strong>)?\s*</div>"
            r".*?fomc-meeting__date[^>]*>\s*([^<]+?)\s*</div>",
            block,
            re.S,
        )
        for raw_month, raw_days in pairs:
            month_parts = [p.strip() for p in re.split(r"[/\-]", raw_month) if p.strip()]
            if not month_parts:
                continue
            has_sep = "*" in raw_days
            day_nums = re.findall(r"\d{1,2}", raw_days)
            if not day_nums:
                continue
            # Day 2 is the decision date; on cross-month ("April/May" + "30-1") use second month
            day = int(day_nums[-1])
            month_tok = month_parts[-1] if len(month_parts) > 1 and len(day_nums) > 1 else month_parts[0]
            month = _parse_month_token(month_tok)
            if not month:
                continue
            try:
                dec_date = date(year, month, day)
            except ValueError:
                continue
            label = "FOMC Rate Decision (SEP/Dot Plot)" if has_sep else "FOMC Rate Decision"
            for open_date, sessions in map_us_release_to_taifex_sessions(dec_date, "fomc"):
                events.append(
                    {
                        "date": open_date.isoformat(),
                        "name": label,
                        "severity": severity,
                        "sessions": sessions,
                        "source": "fed_fomc",
                        "category": "fomc",
                    }
                )
    return events


def _unfold_ics(text: str) -> str:
    """RFC-5545 line unfolding (CRLF + space/tab continuation)."""
    return re.sub(r"\r?\n[ \t]", "", text)


def parse_bls_ics(
    ics_text: str,
    *,
    cpi_severity: str = "high",
    nfp_severity: str = "high",
    ppi_severity: str = "medium",
) -> list[dict[str, Any]]:
    """Parse official BLS ``bls.ics``.

    Uses STRICT exact ``SUMMARY`` matching so ``"Employment Situation"`` never
    accidentally matches ``"Employment Situation of Veterans"``.
    """
    exact_map = {
        "Consumer Price Index": ("US CPI", "us_cpi", cpi_severity),
        "Employment Situation": ("US Nonfarm Payrolls (NFP)", "us_nfp", nfp_severity),
        "Producer Price Index": ("US PPI", "us_ppi", ppi_severity),
    }
    unfolded = _unfold_ics(ics_text)
    events: list[dict[str, Any]] = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, re.S):
        sm = re.search(r"^SUMMARY:(.+)$", block, re.M)
        dm = re.search(r"^DTSTART(?:;[^:]*)?:(\d{8})", block, re.M)
        if not sm or not dm:
            continue
        summary = sm.group(1).strip()
        spec = exact_map.get(summary)
        if spec is None:
            continue
        raw_d = dm.group(1)
        try:
            rel_date = date(int(raw_d[:4]), int(raw_d[4:6]), int(raw_d[6:8]))
        except ValueError:
            continue
        label, cat, sev = spec
        for open_date, sessions in map_us_release_to_taifex_sessions(rel_date, "macro_am"):
            events.append(
                {
                    "date": open_date.isoformat(),
                    "name": label,
                    "severity": sev,
                    "sessions": sessions,
                    "source": "bls_ics",
                    "category": cat,
                }
            )
    return events


def parse_bls_release_html(
    html: str,
    *,
    label: str,
    category: str,
    severity: str,
) -> list[dict[str, Any]]:
    """Parse official BLS HTML tables (``cpi.htm``, ``empsit.htm``) for next-year lookahead."""
    events: list[dict[str, Any]] = []
    rows = re.findall(
        r"<tr[^>]*>\s*<td[^>]*>([^<]+)</td>\s*<td[^>]*>([^<]+)</td>\s*<td[^>]*>([^<]+)</td>",
        html,
        re.S,
    )
    for ref_period, rel_date_raw, _rel_time in rows:
        m = re.match(r"^\s*([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})\s*$", rel_date_raw)
        if not m:
            continue
        month = _parse_month_token(m.group(1))
        if not month:
            continue
        try:
            rel_date = date(int(m.group(3)), month, int(m.group(2)))
        except ValueError:
            continue
        ref_clean = re.sub(r"\s+", " ", ref_period).strip()
        name = f"{label} ({ref_clean})" if ref_clean else label
        for open_date, sessions in map_us_release_to_taifex_sessions(rel_date, "macro_am"):
            events.append(
                {
                    "date": open_date.isoformat(),
                    "name": name,
                    "severity": severity,
                    "sessions": sessions,
                    "source": "bls_html",
                    "category": category,
                }
            )
    return events


def parse_bea_json(
    data: Any,
    *,
    pce_severity: str = "medium",
    gdp_severity: str = "medium",
) -> list[dict[str, Any]]:
    """Parse official US BEA ``release_dates.json`` (exact top-level keys only)."""
    if not isinstance(data, dict):
        return []
    targets = {
        "Personal Income and Outlays": ("US PCE (Personal Income & Outlays)", "us_pce", pce_severity),
        "Gross Domestic Product": ("US GDP", "us_gdp", gdp_severity),
    }
    events: list[dict[str, Any]] = []
    for key, (label, cat, sev) in targets.items():
        entry = data.get(key)
        if not isinstance(entry, dict):
            continue
        for stamp in entry.get("release_dates") or []:
            try:
                dt = datetime.fromisoformat(str(stamp))
            except ValueError:
                continue
            rel_date = dt.astimezone(_NY_TZ).date() if dt.tzinfo else dt.date()
            for open_date, sessions in map_us_release_to_taifex_sessions(rel_date, "macro_am"):
                events.append(
                    {
                        "date": open_date.isoformat(),
                        "name": label,
                        "severity": sev,
                        "sessions": sessions,
                        "source": "bea_json",
                        "category": cat,
                    }
                )
    return events


def parse_nasdaq_earnings(
    rule: TickerRule,
    payload: Any,
    *,
    include_estimated: bool = True,
    amc_include_edt_night: bool = False,
) -> list[dict[str, Any]]:
    """Parse Nasdaq ``/api/analyst/{SYM}/earnings-date`` response for one ticker."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    ann = str(data.get("announcement") or "")
    report = str(data.get("reportText") or "")

    # Extract date from announcement ("Dec 09, 2026") or reportText ("12/09/2026")
    rel_date: date | None = None
    m_ann = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})", ann)
    if m_ann:
        mo = _parse_month_token(m_ann.group(1))
        if mo:
            try:
                rel_date = date(int(m_ann.group(3)), mo, int(m_ann.group(2)))
            except ValueError:
                rel_date = None
    if rel_date is None:
        m_num = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", report)
        if m_num:
            try:
                rel_date = date(int(m_num.group(3)), int(m_num.group(1)), int(m_num.group(2)))
            except ValueError:
                rel_date = None
    if rel_date is None:
        return []

    low = report.lower()
    is_estimated = ("estimated to report" in low) and ("expected*" not in low)
    if is_estimated and not include_estimated:
        return []

    if "before market open" in low:
        timing = "bmo"
    elif "after market close" in low:
        timing = "amc"
    else:
        timing = rule.default_timing

    timing_tag = "US pre-open" if timing == "bmo" else "US after-close"
    est_tag = ", est" if is_estimated else ""
    name = f"{rule.label} earnings ({timing_tag}{est_tag})"
    category = f"earnings_{rule.symbol.lower()}"

    events: list[dict[str, Any]] = []
    for open_date, sessions in map_us_release_to_taifex_sessions(
        rel_date, timing, amc_include_edt_night=amc_include_edt_night
    ):
        events.append(
            {
                "date": open_date.isoformat(),
                "name": name,
                "severity": rule.severity,
                "sessions": sessions,
                "source": "nasdaq_earnings",
                "category": category,
            }
        )
    return events


def generate_txf_settlement_events(
    start_date: date,
    end_date: date,
    *,
    severity: str = "low",
) -> list[dict[str, Any]]:
    """Generate TAIFEX monthly settlement days in ``[start_date, end_date]``."""
    try:
        from src.market_data.holidays import settlement_day  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return []
    events: list[dict[str, Any]] = []
    y, m = start_date.year, start_date.month
    while (y, m) <= (end_date.year, end_date.month):
        sd = settlement_day(y, m)
        if start_date <= sd <= end_date:
            events.append(
                {
                    "date": sd.isoformat(),
                    "name": f"TXF Monthly Settlement ({y}-{m:02d})",
                    "severity": severity,
                    "sessions": ["DAY"],
                    "source": "taifex_settlement",
                    "category": "txf_settlement",
                }
            )
        m += 1
        if m > 12:
            m = 1
            y += 1
    return events


# ── Per-source network fetchers ──────────────────────────────────────────

def fetch_fomc_events(cfg: CalendarConfig, http_get: HttpGet = default_http_get) -> list[dict[str, Any]]:
    html = http_get(FOMC_URL, _DEFAULT_HEADERS, cfg.timeout_s)
    events = parse_fomc_html(html, severity=cfg.fomc_severity)
    if not events:
        raise ValueError("no FOMC meetings parsed from federalreserve.gov")
    return events


def fetch_bls_events(cfg: CalendarConfig, http_get: HttpGet = default_http_get) -> list[dict[str, Any]]:
    ics = http_get(BLS_ICS_URL, _DEFAULT_HEADERS, cfg.timeout_s)
    events = parse_bls_ics(
        ics,
        cpi_severity=cfg.cpi_severity,
        nfp_severity=cfg.nfp_severity,
        ppi_severity=cfg.ppi_severity,
    )
    # Supplement with HTML release tables so Dec->Jan cross-year lookahead always has CPI & NFP
    for url, label, cat, sev in (
        (BLS_CPI_HTML_URL, "US CPI", "us_cpi", cfg.cpi_severity),
        (BLS_NFP_HTML_URL, "US Nonfarm Payrolls (NFP)", "us_nfp", cfg.nfp_severity),
    ):
        try:
            html = http_get(url, _DEFAULT_HEADERS, cfg.timeout_s)
            events.extend(parse_bls_release_html(html, label=label, category=cat, severity=sev))
        except Exception:  # noqa: BLE001 — ICS already succeeded; HTML is lookahead supplement
            pass
    if not events:
        raise ValueError("no CPI/NFP/PPI releases parsed from bls.gov")
    return events


def fetch_bea_events(cfg: CalendarConfig, http_get: HttpGet = default_http_get) -> list[dict[str, Any]]:
    raw = http_get(BEA_JSON_URL, _DEFAULT_HEADERS, cfg.timeout_s)
    events = parse_bea_json(
        json.loads(raw),
        pce_severity=cfg.pce_severity,
        gdp_severity=cfg.gdp_severity,
    )
    if not events:
        raise ValueError("no PCE/GDP releases parsed from apps.bea.gov")
    return events


def fetch_nasdaq_earnings(cfg: CalendarConfig, http_get: HttpGet = default_http_get) -> list[dict[str, Any]]:
    if not cfg.tickers:
        return []
    events: list[dict[str, Any]] = []
    failures: list[str] = []
    for rule in cfg.tickers:
        url = NASDAQ_EARNINGS_URL.format(symbol=rule.symbol)
        try:
            raw = http_get(url, _NASDAQ_HEADERS, cfg.timeout_s)
            parsed = parse_nasdaq_earnings(
                rule,
                json.loads(raw),
                include_estimated=cfg.include_estimated_earnings,
                amc_include_edt_night=cfg.amc_include_edt_night,
            )
            events.extend(parsed)
        except Exception as exc:  # noqa: BLE001 — one ticker failure shouldn't drop all others
            failures.append(f"{rule.symbol}: {type(exc).__name__}")
    if len(failures) == len(cfg.tickers):
        raise RuntimeError(f"all {len(failures)} Nasdaq earnings queries failed ({'; '.join(failures[:3])})")
    return events


# ── Orchestrator: merge, deduplicate, cache fallback, window filter ──────

@dataclass
class CalendarFetchResult:
    events: list[dict[str, Any]]
    status: str                           # "ok" | "degraded" | "failed"
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    cached_by_source: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _merge_and_filter_events(
    auto_events: list[dict[str, Any]],
    manual_events: list[dict[str, Any]],
    *,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    """Deduplicate auto events and let manual overrides take precedence."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}

    def _key(ev: dict[str, Any]) -> tuple[str, str]:
        d = str(ev.get("date") or "").strip()
        cat = str(ev.get("category") or "").strip().lower()
        if cat:
            return (d, f"cat:{cat}")
        name = re.sub(r"\s+", " ", str(ev.get("name") or "")).strip().lower()
        return (d, f"name:{name}")

    for ev in auto_events:
        if not isinstance(ev, dict) or not ev.get("date") or not ev.get("name"):
            continue
        k = _key(ev)
        existing = merged.get(k)
        if existing is None:
            merged[k] = dict(ev)
        else:
            # Prefer richer name (e.g. BLS HTML with reference month over bare ICS)
            # and union sessions.
            sess = list(dict.fromkeys(list(existing.get("sessions") or []) + list(ev.get("sessions") or [])))
            existing["sessions"] = sess
            if len(str(ev.get("name") or "")) > len(str(existing.get("name") or "")):
                existing["name"] = ev["name"]

    # Manual overrides win on exact (date, category) or (date, name)
    for ev in manual_events:
        if not isinstance(ev, dict) or not ev.get("date") or not ev.get("name"):
            continue
        item = dict(ev)
        item.setdefault("severity", "high")
        item.setdefault("sessions", ["DAY", "NIGHT"])
        item.setdefault("source", "manual")
        d = str(item["date"]).strip()
        cat = str(item.get("category") or "").strip().lower()
        name_low = re.sub(r"\s+", " ", str(item["name"])).strip().lower()
        # Drop any auto event with same date & matching category/name before inserting manual
        for ek in list(merged):
            if ek[0] != d:
                continue
            ev_name_low = re.sub(r"\s+", " ", str(merged[ek].get("name") or "")).strip().lower()
            if (cat and ek[1] == f"cat:{cat}") or ev_name_low == name_low:
                del merged[ek]
        merged[_key(item)] = item

    lo, hi = start_date.isoformat(), end_date.isoformat()
    kept = [ev for ev in merged.values() if lo <= str(ev["date"]) <= hi]
    kept.sort(
        key=lambda e: (
            str(e["date"]),
            -_SEVERITY_RANK.get(str(e.get("severity") or "").lower(), 0),
            str(e.get("name") or ""),
        )
    )
    return kept


def build_calendar_events(
    cfg: CalendarConfig,
    *,
    now: datetime,
    manual_events: list[dict[str, Any]] | None = None,
    prev_cached_by_source: dict[str, list[dict[str, Any]]] | None = None,
    http_get: HttpGet = default_http_get,
) -> CalendarFetchResult:
    """Fetch all enabled official sources, retain per-source cache on partial
    outages, merge manual overrides, and prune to the configured date window.
    """
    today = now.astimezone(_TZ_TAIPEI).date()
    start_date = today - timedelta(days=cfg.lookback_days)
    end_date = today + timedelta(days=cfg.lookahead_days)

    prev_cache = prev_cached_by_source if isinstance(prev_cached_by_source, dict) else {}
    new_cache: dict[str, list[dict[str, Any]]] = {}
    sources_ok: list[str] = []
    sources_failed: list[str] = []
    errors: dict[str, str] = {}
    auto_events: list[dict[str, Any]] = []

    remote_fetchers: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = []
    if cfg.fomc_enabled:
        remote_fetchers.append(("fed_fomc", lambda: fetch_fomc_events(cfg, http_get)))
    if cfg.bls_enabled:
        remote_fetchers.append(("bls", lambda: fetch_bls_events(cfg, http_get)))
    if cfg.bea_enabled:
        remote_fetchers.append(("bea", lambda: fetch_bea_events(cfg, http_get)))
    if cfg.nasdaq_enabled and cfg.tickers:
        remote_fetchers.append(("nasdaq_earnings", lambda: fetch_nasdaq_earnings(cfg, http_get)))

    for name, fn in remote_fetchers:
        try:
            fetched = fn()
            sources_ok.append(name)
            # Keep a bounded window in cache so calendar_state.json stays small
            windowed = [
                e for e in fetched
                if start_date.isoformat() <= str(e.get("date") or "") <= (end_date + timedelta(days=30)).isoformat()
            ]
            new_cache[name] = windowed
            auto_events.extend(fetched)
        except Exception as exc:  # noqa: BLE001 — isolate source failure
            sources_failed.append(name)
            errors[name] = f"{type(exc).__name__}: {exc}"[:200]
            cached = prev_cache.get(name)
            if isinstance(cached, list):
                new_cache[name] = cached
                auto_events.extend(cached)

    if cfg.txf_settlement_enabled:
        auto_events.extend(
            generate_txf_settlement_events(start_date, end_date, severity=cfg.txf_settlement_severity)
        )

    if remote_fetchers and not sources_ok:
        status = "failed"
    elif sources_failed:
        status = "degraded"
    else:
        status = "ok"

    events = _merge_and_filter_events(
        auto_events,
        list(manual_events or []),
        start_date=start_date,
        end_date=end_date,
    )
    return CalendarFetchResult(
        events=events,
        status=status,
        sources_ok=sources_ok,
        sources_failed=sources_failed,
        errors=errors,
        cached_by_source=new_cache,
    )
