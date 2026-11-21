"""Offline unit & integration tests for W1 calendar auto-fetchers & calendar_bridge.py.

All tests run 100% offline with fixed ``now`` (no network calls, no wall-clock flake).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.news_bridge import calendar_bridge
from src.news.calendar_fetchers import (
    BLS_CPI_HTML_URL,
    BLS_ICS_URL,
    BLS_NFP_HTML_URL,
    BEA_JSON_URL,
    FOMC_URL,
    TickerRule,
    build_calendar_events,
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
