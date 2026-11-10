"""Every bridge entry point records its pass in ``<bridge dir>/runs/<W>.jsonl``.

W1 calendar, W2 cross-market, W3 RSS, W4 chips, and the W5 manual tap
(``crossmarket_monitor --force-fire``).  Network calls are monkeypatched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.test_crossmarket_monitor import cm, quote  # noqa: F401
from tests.test_chips_monitor import chips

from scripts.news_bridge import calendar_bridge, rss_scorer


def runs(dirpath, w):
    p = Path(dirpath) / "runs" / f"{w}.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def argv(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["prog", *map(str, args)])


# ── W1 ──────────────────────────────────────────────────────────────────

def test_w1_records_a_successful_publish(tmp_path, monkeypatch):
    src = tmp_path / "src.json"
    src.write_text(json.dumps([{"date": "2026-11-12", "name": "CPI"}]), encoding="utf-8")
    out = tmp_path / "bridge" / "events.json"
    argv(monkeypatch, "--out", out, "--source", src)
    assert calendar_bridge.main() == 0
    [row] = runs(out.parent, "W1")
    assert row["ok"] is True and row["exit_code"] == 0
    assert "1 event" in row["result"]


def test_w1_records_a_bad_source_as_failed(tmp_path, monkeypatch):
    src = tmp_path / "src.json"
    src.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    out = tmp_path / "bridge" / "events.json"
    argv(monkeypatch, "--out", out, "--source", src)
    assert calendar_bridge.main() == 1
    [row] = runs(out.parent, "W1")
    assert row["ok"] is False and row["exit_code"] == 1


# ── W2 / W5 ─────────────────────────────────────────────────────────────

def test_w2_once_records_the_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "fetch_quote", lambda s: quote(-0.5) if s == "SOXX" else None)
    monkeypatch.setattr(cm, "post_discord", lambda *a: None)
    argv(monkeypatch, "--signal-out", tmp_path / "signal.json", "--once")
    assert cm.main() == 0
    [row] = runs(tmp_path, "W2")
    assert row["ok"] is True
    assert row["result"].startswith("fresh 1/")


def test_w2_crash_is_recorded_and_still_raises(tmp_path, monkeypatch):
    def boom(args, state):
        raise RuntimeError("yahoo schema changed")
    monkeypatch.setattr(cm, "check_once", boom)
    argv(monkeypatch, "--signal-out", tmp_path / "signal.json", "--once")
    with pytest.raises(RuntimeError):
        cm.main()
    [row] = runs(tmp_path, "W2")
    assert row["ok"] is False and "yahoo schema changed" in row["error"]


def test_force_fire_is_recorded_as_w5_manual_tap(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "post_discord", lambda *a: None)
    argv(monkeypatch, "--signal-out", tmp_path / "signal.json", "--force-fire", "clear")
    assert cm.main() == 0
    assert runs(tmp_path, "W2") == []
    [row] = runs(tmp_path, "W5")
    assert row["ok"] is True and "clear" in row["result"]


# ── W3 ──────────────────────────────────────────────────────────────────

@pytest.fixture
def w3(tmp_path, monkeypatch):
    state = tmp_path / "rss_scorer_state.json"
    monkeypatch.setattr(rss_scorer, "load_settings", lambda p: {"news": {}})
    monkeypatch.setattr(rss_scorer, "get_news_config", lambda s, discord_webhook=None: {
        "gemini_api_key": "k", "state_file": str(state), "news_log_file": "",
        "interval_minutes": 30, "discord_mode": "changes"})
    argv(monkeypatch, "--settings", tmp_path / "settings.yaml", "--once")
    return tmp_path


def test_w3_records_a_scoring_pass(w3, monkeypatch):
    def ok(cfg, state, vote_out):
        state.update(last_check="2026-11-07T16:00:00+08:00", error_streak=0,
                     last_fetched=40, last_new=3, last_articles_scored=3,
                     session_net=0.7)
        return state
    monkeypatch.setattr(rss_scorer, "check_once", ok)
    assert rss_scorer.main() == 0
    [row] = runs(w3, "W3")
    assert row["ok"] is True and "scored 3" in row["result"]


def test_w3_new_scoring_error_marks_the_run_failed(w3, monkeypatch):
    def failing(cfg, state, vote_out):
        state.update(error_streak=int(state.get("error_streak") or 0) + 1,
                     last_error="HTTP 429 quota")
        return state
    monkeypatch.setattr(rss_scorer, "check_once", failing)
    assert rss_scorer.main() == 0
    [row] = runs(w3, "W3")
    assert row["ok"] is False and row["error"] == "HTTP 429 quota"


def test_w3_missing_key_is_a_failed_run(tmp_path, monkeypatch):
    state = tmp_path / "rss_scorer_state.json"
    monkeypatch.setattr(rss_scorer, "load_settings", lambda p: {"news": {}})
    monkeypatch.setattr(rss_scorer, "get_news_config", lambda s, discord_webhook=None: {
        "gemini_api_key": "", "state_file": str(state), "news_log_file": ""})
    argv(monkeypatch, "--settings", tmp_path / "settings.yaml", "--once")
    assert rss_scorer.main() == 1
    [row] = runs(tmp_path, "W3")
    assert row["ok"] is False and row["exit_code"] == 1


# ── W4 ──────────────────────────────────────────────────────────────────

def test_w4_records_the_pass(tmp_path, monkeypatch):
    base = tmp_path / "regime_vote.json"
    monkeypatch.setattr(chips, "fetch_taifex_foreign_net_oi", lambda d: None)
    argv(monkeypatch, "--base-path", base, "--date", "20261106")
    assert chips.main() == 0
    [row] = runs(tmp_path, "W4")
    assert row["ok"] is True and row["result"].startswith("no TAIFEX data")


def test_w4_dry_run_records_nothing(tmp_path, monkeypatch):
    base = tmp_path / "regime_vote.json"
    monkeypatch.setattr(chips, "fetch_taifex_foreign_net_oi", lambda d: None)
    argv(monkeypatch, "--base-path", base, "--date", "20261106", "--dry-run")
    assert chips.main() == 0
    assert runs(tmp_path, "W4") == []
