"""Monthly W3 history files read by the dashboard (news_feed table window,
news_trend runs)."""
from __future__ import annotations

import json

from scripts.dashboard import news_feed, news_trend
from tests.test_dashboard_news_trend import _ct, _row


# ── monthly history files ────────────────────────────────────────────────

def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_runs_and_table_span_the_legacy_file_and_monthly_files(tmp_path):
    base = tmp_path / "news" / "news_log.jsonl"
    _write(base, [_row("2026-10-30T20:00:00+08:00", 0.5)])                      # pre-split
    _write(tmp_path / "news" / "news_log_202611.jsonl", [_row("2026-11-02T20:00:00+08:00", -0.5)])
    runs = news_trend.read_runs(str(base))
    assert [r["t"] for r in runs] == [_ct(2026, 10, 30, 20, 0), _ct(2026, 11, 2, 20, 0)]
    titles = [r["title"] for r in news_feed.read_items(str(base))]
    assert len(titles) == 2 and titles[0].startswith("t2026-11-02")             # newest first
    # A new month appears later: both readers pick it up.
    _write(tmp_path / "news" / "news_log_202612.jsonl", [_row("2026-12-01T09:00:00+08:00", 0.2)])
    assert len(news_trend.read_runs(str(base))) == 3
    assert len(news_feed.read_items(str(base))) == 3


def test_table_window_fills_from_older_months_only_as_needed(tmp_path, monkeypatch):
    monkeypatch.setattr(news_feed, "WINDOW_LINES", 5)
    d = tmp_path / "news"
    _write(d / "news_log_202610.jsonl", [_row(f"2026-10-01T{h:02d}:00:00+08:00", 0.1) for h in range(10, 14)])
    _write(d / "news_log_202611.jsonl", [_row(f"2026-11-01T{h:02d}:00:00+08:00", 0.1) for h in range(10, 13)])
    items = news_feed.read_items(str(d / "news_log.jsonl"))
    assert len(items) == 5                                      # 3 from Nov + newest 2 from Oct
    assert sorted(r["scored_at"][:13] for r in items)[:2] == ["2026-10-01T12", "2026-10-01T13"]
