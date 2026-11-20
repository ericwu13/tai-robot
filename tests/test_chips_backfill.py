"""chips_backfill.py: fill chips_history.jsonl before W4 started keeping it.

The TAIFEX fixture is a real 三大法人 by-contract download (TX, 2026-11-12
to 11-18, cp950), fetched through the owner's browser on 2026-11-19. Its
11/16 foreign net OI (-96,787) and ΔOI vs 11/13 (+4,083) are the numbers
W4 itself recorded that day.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[1] / "scripts" / "news_bridge"
sys.path.insert(0, str(_DIR))
import chips_backfill as bf  # noqa: E402

cm = bf.cm
FIXTURE = Path(__file__).parent / "fixtures" / "news" / "taifex_fut_contracts_txf.csv"

LOG = "\n".join([
    # pre-08-31 level-only format: skipped
    "2026-08-20 16:15:02 TPE | level -80,000 | vote: - | hysteresis",
    "2026-11-13 16:15:01 TPE | ΔOI -1,297 (level -100,870, prev 20261112) | equity +1,000,000,000 | "
    "vote: - | weak ΔOI and no confirming equity flow — no vote (data 20261113)",
    "2026-11-13 20:15:01 TPE | nothing new — dataset 20261113 already decided",
    "2026-11-16 16:15:01 TPE | ΔOI +4,083 (level -96,787, prev 20261113) | equity n/a | vote: - | "
    "TWSE equity flow unavailable — cannot rule out hedging, no vote (data 20261116)",
    "2026-11-16 18:15:01 TPE | ΔOI +4,083 (level -96,787, prev 20261113) | equity -70,189,212,163 | "
    "vote: - | futures vs equity contradiction (likely institutional hedging) — no vote (data 20261116)",
    "2026-11-17 16:15:02 TPE | ΔOI +6,000 (level -90,787, prev 20261116) | equity +6,000,000,000 | "
    "vote: trending-up | strong: |ΔOI| >= 5,000 (data 20261117)",
    "2026-11-18 04:15:02 TPE | ΔOI n/a (level -90,000, no prev) | equity n/a | vote: - | "
    "no recent OI history (data 20261118)",
])


def test_parse_taifex_real_download():
    levels = bf.parse_taifex_csv(FIXTURE.read_bytes().decode("cp950"))
    assert levels == {"20261112": -99573.0, "20261113": -100870.0, "20261116": -96787.0,
                      "20261117": -97896.0, "20261118": -99438.0}


def test_parse_taifex_rejects_an_unexpected_header():
    with pytest.raises(ValueError):
        bf.parse_taifex_csv("<html>error</html>\n")


def test_parse_log_reads_current_format_last_line_wins():
    rows = bf.parse_log(LOG)
    assert sorted(rows) == ["20261113", "20261116", "20261117", "20261118"]
    r16 = rows["20261116"]
    assert r16["equity_net"] == -70189212163.0 and r16["final"] is True
    assert r16["reason"].startswith("futures vs equity contradiction")
    assert r16["session"] == "2026-11-16|NIGHT" and r16["at"] == "2026-11-16T18:15:01+08:00"
    r17 = rows["20261117"]
    assert r17["decision"] == "trending-up" and r17["reason"] == "strong: |ΔOI| >= 5,000"
    assert r17["delta_oi"] == 6000.0 and r17["prev_date"] == "20261116"
    r18 = rows["20261118"]
    assert r18["delta_oi"] is None and r18["prev_date"] is None
    assert r18["session"] == "2026-11-17|NIGHT"              # 04:15 belongs to last night


def test_build_backfill_matches_w4_and_keeps_hindcast_separate():
    levels = bf.parse_taifex_csv(FIXTURE.read_bytes().decode("cp950"))
    rows = bf.build_backfill(levels, {"20261116": -70189212163.0, "20261117": 6e9,
                                      "20261118": 0.0},
                             ["20261112", "20261116", "20261117", "20261118"])
    assert rows["20261112"]["delta_oi"] is None                # first in the series
    r16 = rows["20261116"]
    assert r16["delta_oi"] == 4083.0 and r16["prev_date"] == "20261113"
    assert r16["decision"] is None and r16["reason"] == bf.BACKFILL_REASON
    assert r16["hindcast_decision"] is None and "contradiction" in r16["hindcast_reason"]
    r17 = rows["20261117"]
    assert r17["delta_oi"] == -1109.0 and "contradiction" in r17["hindcast_reason"]
    r18 = rows["20261118"]                                      # -1,542, flat equity
    assert r18["delta_oi"] == -1542.0 and r18["hindcast_reason"].startswith("weak")


def test_gap_longer_than_w4_allows_has_no_delta():
    rows = bf.build_backfill({"20261101": 1.0, "20261110": 5.0}, {}, ["20261110"])
    assert rows["20261110"]["delta_oi"] is None and rows["20261110"]["prev_date"] is None


def _bridge(tmp_path, history=(), daily=None):
    d = tmp_path / "bridge"
    d.mkdir()
    (d / cm.LOG_NAME).write_text(LOG + "\n", encoding="utf-8")
    if history:
        (d / cm.HISTORY_NAME).write_text(
            "".join(json.dumps(x) + "\n" for x in history), encoding="utf-8")
    cm.save_state(d / cm.STATE_NAME, {"daily": daily or {}, "history": {"x": 1}})
    return d


def _lines(d):
    return [json.loads(x) for x in (d / cm.HISTORY_NAME).read_text(encoding="utf-8").splitlines()]


def test_dry_run_writes_nothing_and_calls_no_network(tmp_path, monkeypatch):
    d = _bridge(tmp_path)
    monkeypatch.setattr(bf, "fetch_taifex_levels",
                        lambda a, b: bf.parse_taifex_csv(FIXTURE.read_bytes().decode("cp950")))
    monkeypatch.setattr(bf, "_twse_equity", lambda k: pytest.fail("TWSE called on dry run"))
    assert bf.main(["--base-path", str(d / "regime_vote.json"), "--remote"]) == 0
    assert not (d / cm.HISTORY_NAME).exists()


def test_apply_fills_only_uncovered_dates_and_never_touches_state(tmp_path, monkeypatch):
    d = _bridge(tmp_path, history=[{"v": 1, "date": "20261118", "src": "live", "net_oi": -99438}],
                daily={"20261117": {"net_oi": -97896}})
    state_before = (d / cm.STATE_NAME).read_bytes()
    monkeypatch.setattr(bf, "fetch_taifex_levels",
                        lambda a, b: bf.parse_taifex_csv(FIXTURE.read_bytes().decode("cp950")))
    calls = []
    monkeypatch.setattr(bf, "_twse_equity", lambda k: calls.append(k) or 2e9)
    assert bf.main(["--base-path", str(d / "regime_vote.json"), "--remote",
                    "--since", "20261112", "--until", "20261118", "--sleep", "0", "--apply"]) == 0
    by_date = {}
    for x in _lines(d):
        by_date.setdefault(x["date"], []).append(x["src"])
    # 11/17 is in daily and 11/18 in history: neither written again.
    assert "20261117" not in by_date
    assert by_date["20261118"] == ["live"]
    # 11/13 and 11/16 come from the real log; only 11/12 needs TAIFEX + TWSE.
    assert by_date["20261113"] == ["log"] and by_date["20261116"] == ["log"]
    assert by_date["20261112"] == ["backfill"] and calls == ["20261112"]
    assert (d / cm.STATE_NAME).read_bytes() == state_before
    assert not cm.w4_vote_path(str(d / "regime_vote.json")).exists()


def test_rerun_is_a_no_op(tmp_path, monkeypatch):
    d = _bridge(tmp_path)
    args = ["--base-path", str(d / "regime_vote.json"), "--apply"]
    bf.main(args)
    first = (d / cm.HISTORY_NAME).read_text(encoding="utf-8")
    bf.main(args)
    assert (d / cm.HISTORY_NAME).read_text(encoding="utf-8") == first


def test_trend_reads_backfilled_rows_without_markers(tmp_path, monkeypatch):
    from scripts.dashboard import bridge_panels
    assert bridge_panels.reason_zh(bf.BACKFILL_REASON).startswith("回補資料")
