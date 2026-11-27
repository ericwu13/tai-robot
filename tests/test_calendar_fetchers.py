"""Offline unit & integration tests for W1 calendar auto-fetchers & calendar_bridge.py.

All tests run 100% offline with fixed ``now`` (no network calls, no wall-clock flake).
"""
from __future__ import annotations

import json
import logging
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts.news_bridge import calendar_bridge
from src.news.calendar_fetchers import (
    BLS_CPI_HTML_URL,
    BLS_ICS_URL,
    BLS_NFP_HTML_URL,
    BEA_JSON_URL,
    FOMC_URL,
    CalendarConfig,
    TickerRule,
    build_calendar_events,
    fetch_nasdaq_earnings,
    generate_txf_settlement_events,
    load_calendar_config,
    map_us_release_to_taifex_sessions,
    parse_bea_json,
    parse_bls_ics,
    parse_bls_release_html,
    parse_fomc_html,
    parse_nasdaq_earnings,
)

_TPE = timezone(timedelta(hours=8))
_FIXED_NOW = datetime(2026, 11, 21, 10, 0, 0, tzinfo=_TPE)


_SAMPLE_FOMC_HTML = """
<div><h4><a>2026 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting">
  <div class="fomc-meeting__month"><strong>April/May</strong></div>
  <div class="fomc-meeting__date">30-1</div>
</div>
<div class="row fomc-meeting">
  <div class="fomc-meeting__month"><strong>December</strong></div>
  <div class="fomc-meeting__date">8-9*</div>
</div>
<div><h4><a>2027 FOMC Meetings</a></h4></div>
<div class="row fomc-meeting">
  <div class="fomc-meeting__month"><strong>January</strong></div>
  <div class="fomc-meeting__date">26-27</div>
</div>
"""

_SAMPLE_BLS_ICS = """\
BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=US-Eastern:20261204T083000
SUMMARY:Employment Situation
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=US-Eastern:20261205T083000
SUMMARY:Employment Situation of Veterans
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=US-Eastern:20261210T083000
SUMMARY:Consumer Price Index
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=US-Eastern:20261215T083000
SUMMARY:Producer Price Index
END:VEVENT
END:VCALENDAR
"""

_SAMPLE_BLS_CPI_HTML = """
<table>
<tr><td>November 2026</td><td>Dec. 10, 2026</td><td>08:30 AM</td></tr>
<tr><td>December 2026</td><td>Jan. 13, 2027</td><td>08:30 AM</td></tr>
</table>
"""

_SAMPLE_BEA_JSON = {
    "Personal Income and Outlays": {
        "release_dates": ["2026-11-25T13:30:00+00:00", "2026-12-23T13:30:00+00:00"],
    },
    "Gross Domestic Product": {
        "release_dates": ["2026-12-23T13:30:00+00:00"],
    },
    "Personal Income by State": {
        "release_dates": ["2026-12-10T13:30:00+00:00"],
    },
}

_SAMPLE_NASDAQ_CONFIRMED = {
    "data": {
        "announcement": "Earnings announcement* for AVGO: Dec 09, 2026",
        "reportText": "Broadcom Inc. is expected* to report earnings on 12/09/2026 after market close.",
    }
}

_SAMPLE_NASDAQ_ESTIMATED = {
    "data": {
        "announcement": "Earnings announcement* for MU: Dec 17, 2026",
        "reportText": "Micron is estimated to report earnings on 12/17/2026 after market close.",
    }
}

_SAMPLE_NASDAQ_BMO = {
    "data": {
        "announcement": "Earnings announcement* for TSM: Dec 14, 2026",
        "reportText": "TSMC is expected* to report earnings on 12/14/2026 before market open.",
    }
}


class TestParsersAndSessionMapping:
    def test_parse_fomc_html_handles_single_cross_month_and_sep_star(self):
        evs = parse_fomc_html(_SAMPLE_FOMC_HTML, severity="high")
        by_date = {e["date"]: e for e in evs}
        assert "2026-05-01" in by_date  # cross-month April/May 30-1 -> May 1
        assert by_date["2026-12-09"]["name"] == "FOMC Rate Decision (SEP/Dot Plot)"
        assert by_date["2026-12-09"]["sessions"] == ["NIGHT"]
        assert by_date["2027-01-27"]["name"] == "FOMC Rate Decision"

    def test_parse_bls_ics_exact_match_excludes_veterans(self):
        evs = parse_bls_ics(_SAMPLE_BLS_ICS)
        names = [e["name"] for e in evs]
        assert "US Nonfarm Payrolls (NFP)" in names
        assert "US CPI" in names
        assert "US PPI" in names
        assert all("Veterans" not in n for n in names)
        assert len(evs) == 3

    def test_parse_bls_html_covers_cross_year_lookahead(self):
        evs = parse_bls_release_html(
            _SAMPLE_BLS_CPI_HTML, label="US CPI", category="us_cpi", severity="high"
        )
        assert [e["date"] for e in evs] == ["2026-12-10", "2027-01-13"]
        assert "December 2026" in evs[1]["name"]

    def test_parse_bea_json_ignores_state_level_keys(self):
        evs = parse_bea_json(_SAMPLE_BEA_JSON)
        assert len(evs) == 3
        assert all(e["category"] in ("us_pce", "us_gdp") for e in evs)
        assert all(e["date"] != "2026-12-10" for e in evs)

    def test_nasdaq_earnings_est_vs_edt_and_bmo_vs_amc_mapping(self):
        # Winter EST: Dec 09 AMC (16:15 EST = Dec 10 05:15 TPE, after NIGHT closes) -> Dec 10 DAY only
        avgo = TickerRule("AVGO", "AVGO", "high", "amc")
        evs = parse_nasdaq_earnings(avgo, _SAMPLE_NASDAQ_CONFIRMED, amc_include_edt_night=True)
        assert [(e["date"], e["sessions"]) for e in evs] == [("2026-12-10", ["DAY"])]
        assert "est" not in evs[0]["name"]

        # Summer EDT: Aug 27 AMC -> next trading day DAY (+ optional us_date NIGHT if enabled)
        edt_only_day = map_us_release_to_taifex_sessions(date(2026, 8, 27), "amc", amc_include_edt_night=False)
        assert edt_only_day == [(date(2026, 8, 28), ["DAY"])]
        edt_with_night = map_us_release_to_taifex_sessions(date(2026, 8, 27), "amc", amc_include_edt_night=True)
        assert edt_with_night == [(date(2026, 8, 27), ["NIGHT"]), (date(2026, 8, 28), ["DAY"])]

        # BMO (e.g. TSMC): lands on us_date NIGHT
        tsm = TickerRule("TSM", "TSMC", "high", "bmo")
        tsm_evs = parse_nasdaq_earnings(tsm, _SAMPLE_NASDAQ_BMO)
        assert [(e["date"], e["sessions"]) for e in tsm_evs] == [("2026-12-14", ["NIGHT"])]

        # Estimated flag respected
        mu = TickerRule("MU", "MU", "medium", "amc")
        assert parse_nasdaq_earnings(mu, _SAMPLE_NASDAQ_ESTIMATED, include_estimated=False) == []
        mu_evs = parse_nasdaq_earnings(mu, _SAMPLE_NASDAQ_ESTIMATED, include_estimated=True)
        assert len(mu_evs) == 1 and "est" in mu_evs[0]["name"]

    def test_txf_settlement_generation(self):
        evs = generate_txf_settlement_events(date(2026, 12, 1), date(2026, 12, 31), severity="low")
        assert len(evs) == 1
        assert evs[0]["date"] == "2026-12-16"
        assert evs[0]["severity"] == "low"
        assert evs[0]["sessions"] == ["DAY"]


class TestBuildAndPublishBridge:
    def _fake_http(self, fail_hosts: set[str] | None = None):
        fail_hosts = fail_hosts or set()

        def _get(url: str, _headers: dict, _timeout: int) -> str:
            for h in fail_hosts:
                if h in url:
                    raise OSError(f"simulated outage for {h}")
            if url == FOMC_URL:
                return _SAMPLE_FOMC_HTML
            if url == BLS_ICS_URL:
                return _SAMPLE_BLS_ICS
            if url == BLS_CPI_HTML_URL:
                return _SAMPLE_BLS_CPI_HTML
            if url == BLS_NFP_HTML_URL:
                return "<table></table>"
            if url == BEA_JSON_URL:
                return json.dumps(_SAMPLE_BEA_JSON)
            if "/AVGO/" in url:
                return json.dumps(_SAMPLE_NASDAQ_CONFIRMED)
            if "/MU/" in url:
                return json.dumps(_SAMPLE_NASDAQ_ESTIMATED)
            return json.dumps({"data": {}})

        return _get

    def test_build_merges_sources_and_manual_override_wins(self):
        cfg = load_calendar_config()
        manual = [
            # Override auto FOMC with custom name & severity
            {"date": "2026-12-09", "name": "FOMC (Manual Override)", "severity": "medium", "sessions": ["NIGHT"], "category": "fomc"},
            {"date": "2026-12-17", "name": "Taiwan CBC Q4", "severity": "medium", "sessions": ["NIGHT"], "category": "tw_cbc"},
        ]
        res = build_calendar_events(
            cfg,
            now=_FIXED_NOW,
            manual_events=manual,
            http_get=self._fake_http(),
        )
        assert res.status == "ok"
        assert set(res.sources_ok) == {"fed_fomc", "bls", "bea", "nasdaq_earnings"}
        fomc_on_dec9 = [e for e in res.events if e["date"] == "2026-12-09" and e.get("category") == "fomc"]
        assert len(fomc_on_dec9) == 1
        assert fomc_on_dec9[0]["name"] == "FOMC (Manual Override)"
        assert fomc_on_dec9[0]["severity"] == "medium"
        assert any(e["name"] == "Taiwan CBC Q4" for e in res.events)
        # BLS HTML enriched CPI name merged cleanly without duplicating Dec 10 CPI
        cpi_dec10 = [e for e in res.events if e["date"] == "2026-12-10" and e.get("category") == "us_cpi"]
        assert len(cpi_dec10) == 1 and "November 2026" in cpi_dec10[0]["name"]

    def test_partial_outage_retains_cached_source_and_marks_degraded(self):
        cfg = load_calendar_config()
        first = build_calendar_events(cfg, now=_FIXED_NOW, http_get=self._fake_http())
        assert first.status == "ok"

        # Now federalreserve.gov is down -> FOMC survives via prev_cached_by_source!
        second = build_calendar_events(
            cfg,
            now=_FIXED_NOW + timedelta(days=1),
            prev_cached_by_source=first.cached_by_source,
            http_get=self._fake_http(fail_hosts={"federalreserve.gov"}),
        )
        assert second.status == "degraded"
        assert second.sources_failed == ["fed_fomc"]
        assert any(e["date"] == "2026-12-09" and "FOMC" in e["name"] for e in second.events)

    def test_total_fetch_outage_exits_1_and_never_stamps_fresh_updated_at(self, tmp_path):
        out = tmp_path / "events.json"
        old_stamp = (_FIXED_NOW - timedelta(days=5)).isoformat(timespec="seconds")
        out.write_text(
            json.dumps({"version": 1, "updated_at": old_stamp, "events": []}),
            encoding="utf-8",
        )
        args = SimpleNamespace(
            out=str(out), source=None, config=None, fetch=False, offline=False, dry_run=False
        )
        code, msg = calendar_bridge.publish(
            args,
            now=_FIXED_NOW,
            http_get=self._fake_http(fail_hosts={"federalreserve.gov", "bls.gov", "bea.gov", "nasdaq.com"}),
        )
        assert code == 1
        assert "failed" in msg
        # events.json updated_at MUST remain untouched!
        assert json.loads(out.read_text(encoding="utf-8"))["updated_at"] == old_stamp
        # Sidecar calendar_state.json records failure streak
        state = json.loads((tmp_path / calendar_bridge.STATE_FILENAME).read_text(encoding="utf-8"))
        assert state["fetch_status"] == "failed"
        assert state["error_streak"] == 1

    def test_publish_live_writes_valid_events_and_sidecar_state(self, tmp_path):
        out = tmp_path / "events.json"
        args = SimpleNamespace(
            out=str(out), source=None, config=None, fetch=False, offline=False, dry_run=False
        )
        code, result = calendar_bridge.publish(args, now=_FIXED_NOW, http_get=self._fake_http())
        assert code == 0
        assert "upcoming" in result
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["updated_at"] == _FIXED_NOW.isoformat(timespec="seconds")
        assert any(e["date"] == "2026-12-09" and "FOMC" in e["name"] for e in data["events"])
        state = json.loads((tmp_path / calendar_bridge.STATE_FILENAME).read_text(encoding="utf-8"))
        assert state["fetch_status"] == "ok"
        assert state["error_streak"] == 0
        assert state["upcoming_count"] >= 5


class _CurlResp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _mock_curl(monkeypatch, responses: dict[str, _CurlResp]):
    """Install a fake ``curl_cffi.requests.get``. No real package or network."""
    calls: list[dict] = []

    def _get(url, headers=None, timeout=None, impersonate=None, **kwargs):
        calls.append({
            "url": url,
            "headers": dict(headers or {}),
            "timeout": timeout,
            "impersonate": impersonate,
        })
        for sym, resp in responses.items():
            if f"/{sym}/" in url:
                return resp
        return _CurlResp(404, "missing")

    package = ModuleType("curl_cffi")
    requests_mod = ModuleType("curl_cffi.requests")
    requests_mod.get = _get
    package.requests = requests_mod
    monkeypatch.setitem(sys.modules, "curl_cffi", package)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", requests_mod)
    return calls


def _block_urllib(monkeypatch):
    def _no(*_a, **_k):
        raise AssertionError("urllib must not be used for Nasdaq")

    monkeypatch.setattr(urllib.request, "urlopen", _no)


def _nasdaq_only(*symbols: str) -> CalendarConfig:
    return CalendarConfig(
        fomc_enabled=False,
        bls_enabled=False,
        bea_enabled=False,
        nasdaq_enabled=True,
        include_estimated_earnings=True,
        txf_settlement_enabled=False,
        tickers=tuple(TickerRule(sym, sym, "high", "amc") for sym in symbols),
    )


_PARTIAL_YAML = """\
version: 1
window:
  lookahead_days: 45
  lookback_days: 2
sources:
  fomc:
    enabled: false
  bls:
    enabled: false
  bea:
    enabled: false
  nasdaq_earnings:
    enabled: true
    include_estimated: true
    tickers:
      AVGO:
        label: AVGO
        severity: high
        default_timing: amc
      MU:
        label: MU
        severity: medium
        default_timing: amc
  txf_settlement:
    enabled: false
"""


class TestNasdaqCurlFetch:
    def test_chrome_impersonation_200_parses_events(self, monkeypatch):
        calls = _mock_curl(monkeypatch, {
            "AVGO": _CurlResp(200, json.dumps(_SAMPLE_NASDAQ_CONFIRMED)),
        })
        _block_urllib(monkeypatch)
        events = fetch_nasdaq_earnings(_nasdaq_only("AVGO"))
        assert len(events) == 1
        assert events[0]["source"] == "nasdaq_earnings"
        assert events[0]["category"] == "earnings_avgo"
        assert calls[0]["impersonate"] == "chrome"
        assert "User-Agent" not in calls[0]["headers"]
        assert calls[0]["headers"]["Accept"].startswith("application/json")
        assert calls[0]["headers"]["Origin"] == "https://www.nasdaq.com"
        assert calls[0]["headers"]["Referer"] == "https://www.nasdaq.com/"

    def test_chrome_impersonation_403_is_recorded(self, monkeypatch, caplog, tmp_path):
        _mock_curl(monkeypatch, {"AVGO": _CurlResp(403, "Access Denied")})
        _block_urllib(monkeypatch)
        caplog.set_level(logging.WARNING, logger="src.news.calendar_fetchers")
        res = build_calendar_events(_nasdaq_only("AVGO"), now=_FIXED_NOW)
        assert res.status == "failed"
        assert res.sources_failed == ["nasdaq_earnings"]
        assert "403" in res.errors["nasdaq_earnings"]
        assert any("403" in rec.message and "AVGO" in rec.message for rec in caplog.records)

        cfg_path = tmp_path / "calendar_sources.yaml"
        only_avgo = _PARTIAL_YAML.replace(
            "      MU:\n        label: MU\n        severity: medium\n        default_timing: amc\n",
            "",
        )
        cfg_path.write_text(only_avgo, encoding="utf-8")
        source = tmp_path / "manual.json"
        source.write_text("[]", encoding="utf-8")
        out = tmp_path / "events.json"
        old_stamp = (_FIXED_NOW - timedelta(days=5)).isoformat(timespec="seconds")
        out.write_text(
            json.dumps({"version": 1, "updated_at": old_stamp, "events": []}),
            encoding="utf-8",
        )
        args = SimpleNamespace(
            out=str(out), source=str(source), config=str(cfg_path),
            fetch=True, offline=False, dry_run=False,
        )
        code, msg = calendar_bridge.publish(args, now=_FIXED_NOW)
        assert code == 1
        assert "403" in msg
        assert json.loads(out.read_text(encoding="utf-8"))["updated_at"] == old_stamp

    def test_partial_403_degrades_and_bridge_carries_detail(self, monkeypatch, caplog, capsys, tmp_path):
        _mock_curl(monkeypatch, {
            "AVGO": _CurlResp(200, json.dumps(_SAMPLE_NASDAQ_CONFIRMED)),
            "MU": _CurlResp(403, "Access Denied"),
        })
        _block_urllib(monkeypatch)
        caplog.set_level(logging.WARNING, logger="src.news.calendar_fetchers")
        res = build_calendar_events(_nasdaq_only("AVGO", "MU"), now=_FIXED_NOW)
        assert res.status == "degraded"
        assert res.sources_failed == ["nasdaq_earnings"]
        assert res.sources_ok == []
        assert "403" in res.errors["nasdaq_earnings"]
        assert "MU" in res.errors["nasdaq_earnings"]
        assert any(e.get("category") == "earnings_avgo" for e in res.events)
        assert any("403" in rec.message and "MU" in rec.message for rec in caplog.records)

        cfg_path = tmp_path / "calendar_sources.yaml"
        cfg_path.write_text(_PARTIAL_YAML, encoding="utf-8")
        source = tmp_path / "manual.json"
        source.write_text("[]", encoding="utf-8")
        out = tmp_path / "events.json"
        args = SimpleNamespace(
            out=str(out), source=str(source), config=str(cfg_path),
            fetch=True, offline=False, dry_run=False,
        )
        code, result = calendar_bridge.publish(args, now=_FIXED_NOW)
        printed = capsys.readouterr().out
        assert code == 0
        assert "degraded" in result
        assert "403" in result
        assert "nasdaq_earnings" in result
        assert "degraded" in printed and "403" in printed
        saved = json.loads(out.read_text(encoding="utf-8"))
        assert any(e.get("category") == "earnings_avgo" for e in saved["events"])

    def test_partial_403_retains_cached_events_for_failed_tickers_only(self, monkeypatch):
        prev_cache = {
            "nasdaq_earnings": [
                {
                    "date": "2026-12-03",
                    "name": "AVGO earnings (US after-close, est)",
                    "severity": "high",
                    "sessions": ["DAY"],
                    "source": "nasdaq_earnings",
                    "category": "earnings_avgo",
                },
                {
                    "date": "2026-12-18",
                    "name": "MU earnings (US after-close)",
                    "severity": "medium",
                    "sessions": ["DAY"],
                    "source": "nasdaq_earnings",
                    "category": "earnings_mu",
                },
            ]
        }
        _mock_curl(monkeypatch, {
            "AVGO": _CurlResp(200, json.dumps(_SAMPLE_NASDAQ_CONFIRMED)),
            "MU": _CurlResp(403, "Access Denied"),
        })
        _block_urllib(monkeypatch)
        res = build_calendar_events(
            _nasdaq_only("AVGO", "MU"),
            now=_FIXED_NOW,
            prev_cached_by_source=prev_cache,
        )
        assert res.status == "degraded"
        by_cat = {e["category"]: e["date"] for e in res.events}
        # AVGO updated to fresh confirmed date (2026-12-10), MU retained from cache
        assert by_cat == {"earnings_avgo": "2026-12-10", "earnings_mu": "2026-12-18"}
        cached_cats = {e["category"] for e in res.cached_by_source["nasdaq_earnings"]}
        assert cached_cats == {"earnings_avgo", "earnings_mu"}

        # A subsequent full outage still recovers both tickers from the preserved cache
        _mock_curl(monkeypatch, {
            "AVGO": _CurlResp(403, "Access Denied"),
            "MU": _CurlResp(403, "Access Denied"),
        })
        res2 = build_calendar_events(
            _nasdaq_only("AVGO", "MU"),
            now=_FIXED_NOW + timedelta(days=1),
            prev_cached_by_source=res.cached_by_source,
        )
        assert res2.status == "failed"
        assert {e["category"] for e in res2.events} == {"earnings_avgo", "earnings_mu"}

    def test_missing_curl_cffi_fails_nasdaq_only(self, monkeypatch):
        from src.news import calendar_fetchers as cf

        monkeypatch.setitem(sys.modules, "curl_cffi", None)
        _block_urllib(monkeypatch)

        def std(url, _headers, _timeout):
            if url == FOMC_URL:
                return _SAMPLE_FOMC_HTML
            raise OSError(url)

        monkeypatch.setattr(cf, "default_http_get", std)
        cfg = CalendarConfig(
            fomc_enabled=True,
            bls_enabled=False,
            bea_enabled=False,
            nasdaq_enabled=True,
            txf_settlement_enabled=False,
            tickers=(TickerRule("AVGO", "AVGO", "high", "amc"),),
        )
        res = build_calendar_events(cfg, now=_FIXED_NOW)
        assert res.status == "degraded"
        assert res.sources_ok == ["fed_fomc"]
        assert res.sources_failed == ["nasdaq_earnings"]
        assert "curl_cffi" in res.errors["nasdaq_earnings"]
        assert any("FOMC" in e["name"] for e in res.events)

    def test_default_path_keeps_fomc_on_urllib_and_nasdaq_on_curl(self, monkeypatch):
        from src.news import calendar_fetchers as cf

        seen = {"urllib": [], "nasdaq": []}

        def std(url, _headers, _timeout):
            seen["urllib"].append(url)
            if url == FOMC_URL:
                return _SAMPLE_FOMC_HTML
            raise OSError(url)

        def nq(url, headers, _timeout):
            seen["nasdaq"].append(url)
            assert "User-Agent" not in headers
            return json.dumps(_SAMPLE_NASDAQ_CONFIRMED)

        monkeypatch.setattr(cf, "default_http_get", std)
        monkeypatch.setattr(cf, "default_nasdaq_http_get", nq)
        _block_urllib(monkeypatch)
        cfg = CalendarConfig(
            fomc_enabled=True,
            bls_enabled=False,
            bea_enabled=False,
            nasdaq_enabled=True,
            txf_settlement_enabled=False,
            tickers=(TickerRule("AVGO", "AVGO", "high", "amc"),),
        )
        res = build_calendar_events(cfg, now=_FIXED_NOW)
        assert res.status == "ok"
        assert seen["urllib"] == [FOMC_URL]
        assert seen["nasdaq"] and "/AVGO/" in seen["nasdaq"][0]
        assert "fed_fomc" in res.sources_ok and "nasdaq_earnings" in res.sources_ok
