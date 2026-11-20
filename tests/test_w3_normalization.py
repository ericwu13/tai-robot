"""W3 volume normalisation + story dedup, and the history rescale.

2026-11-20 on Steven: the 13 default feeds brought 40-82 scored articles
per run, the plain clipped sum gave run nets of -12..-30 and a session
net of -76.8 against the +/-1.5 vote line, and one story was scored once
per outlet (Yahoo股市 + its Google News copy, UDN + 經濟日報 ...).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "news_bridge"))
import rescale_news_log  # noqa: E402
import rss_news_log  # noqa: E402
import rss_scorer  # noqa: E402

_TPE = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 19, 10, 0, tzinfo=_TPE)          # Thu, scoring window


def _art(guid, title):
    return {"guid": guid, "title": title, "summary": "",
            "published_dt": NOW - timedelta(minutes=20), "source": "x"}


def _scored(n, direction="bearish", confidence=1.0):
    return [{"article": {}, "score": {"direction": direction, "confidence": confidence}}
            for _ in range(n)]


# ── run scale ────────────────────────────────────────────────────────────

def test_quiet_runs_are_summed_exactly_as_before():
    assert rss_scorer.run_scale(1) == 1.0
    assert rss_scorer.run_scale(rss_scorer.REF_STORIES_PER_RUN) == 1.0
    assert rss_scorer.aggregate_scores(_scored(3)) == pytest.approx(-1.5)
    assert rss_scorer.aggregate_scores(_scored(12)) == pytest.approx(-6.0)


def test_busy_runs_are_mean_times_ref():
    # 82 bearish stories at 0.5 clip: plain sum -41, normalised -6.
    assert rss_scorer.aggregate_scores(_scored(82)) == pytest.approx(-6.0)
    # neutral stories count toward the run size: 12 bearish + 12 neutral
    mixed = _scored(12) + _scored(12, "neutral", 0.2)
    assert rss_scorer.aggregate_scores(mixed) == pytest.approx(-3.0)


# ── story key / dedup ────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("台股暴挫2200點 綠燈一片 鴻海跌幅逆勢", "台股暴挫2200點　綠燈一片！鴻海跌幅逆勢 - Yahoo股市"),
    ("台積電陷2000元關卡保衛戰 台股血崩", "台積電陷2000元關卡保衛戰，台股血崩 - 經濟日報"),
    ("Fed holds rates steady, signals two cuts", "Fed Holds Rates Steady: Signals Two Cuts - Reuters"),
])
def test_same_story_same_key(a, b):
    assert rss_scorer.story_key(a) and rss_scorer.story_key(a) == rss_scorer.story_key(b)


def test_numeric_suffix_is_part_of_the_headline():
    # Review P3 on #334: only a publisher-like suffix is stripped.
    up = rss_scorer.story_key("鴻海10月營收創同期新高 - 年增20%")
    down = rss_scorer.story_key("鴻海10月營收創同期新高 - 年減5%")
    assert up and down and up != down
    assert rss_scorer.story_key("鴻海10月營收創同期新高 - 經濟日報") == \
        rss_scorer.story_key("鴻海10月營收創同期新高")
    assert rss_scorer.story_key("Fed cuts rates - 25bp") != rss_scorer.story_key("Fed cuts rates - 50bp")


def test_templated_headlines_stay_separate():
    # Recorded feeds: TAIFEX settlement notices and FactSet updates share a
    # long template but are different items.
    fx = str(Path(__file__).parent / "fixtures" / "news" / "taifex_rss1.xml")
    import feedparser
    titles = [e.title for e in feedparser.parse(open(fx, "rb").read()).entries]
    keys = [rss_scorer.story_key(t) for t in titles]
    assert len(set(keys)) == len(set(titles))
    assert rss_scorer.story_key("鉅亨速報 - Factset 最新調查：上銀(2049-TW)EPS預估上修至10.36元") != \
        rss_scorer.story_key("鉅亨速報 - Factset 最新調查：上銀(2049-TW)EPS預估上修至10.18元")


def test_different_stories_and_short_titles():
    assert rss_scorer.story_key("台股暴挫2200點") != rss_scorer.story_key("台積電迎來新機會")
    assert rss_scorer.story_key("快訊") == ""                # too short to compare


def test_dedup_within_batch_keeps_first_feed_copy():
    arts = [_art("y1", "台股暴挫2200點 綠燈一片 鴻海跌幅逆勢"),
            _art("g1", "台股暴挫2200點 綠燈一片 鴻海跌幅逆勢 - Yahoo股市"),
            _art("o1", "央行理監事會維持利率不變")]
    keep, dups = rss_scorer.dedup_stories(arts, {}, NOW)
    assert [a["guid"] for a in keep] == ["y1", "o1"]
    assert [a["guid"] for a in dups] == ["g1"]


def test_story_remembered_only_once_scored_and_for_ttl():
    state = {}
    arts = [_art("y1", "台股暴挫2200點 綠燈一片 鴻海跌幅逆勢"), _art("o1", "央行理監事會維持利率不變")]
    rss_scorer.remember_stories(state, arts, ["y1"], NOW)         # o1 failed to score
    later = [_art("g1", "台股暴挫2200點，綠燈一片 鴻海跌幅逆勢 - UDN"),
             _art("o2", "央行理監事會維持利率不變 - 中央社")]
    keep, dups = rss_scorer.dedup_stories(later, state, NOW + timedelta(minutes=30))
    assert [a["guid"] for a in dups] == ["g1"] and [a["guid"] for a in keep] == ["o2"]
    expired = NOW + timedelta(hours=rss_scorer.STORY_TTL_HOURS, minutes=1)
    keep, dups = rss_scorer.dedup_stories(later, state, expired)
    assert not dups


# ── check_once end to end ────────────────────────────────────────────────

def _cfg(tmp_path):
    return {"feeds": ["https://example.com/rss"], "interval_minutes": 30,
            "state_file": str(tmp_path / "state.json"), "gemini_api_key": "k",
            "discord_webhook_url": "", "news_log_path": str(tmp_path / "news_log.jsonl")}


@patch("rss_scorer.fetch_all_feeds")
@patch("rss_scorer.score_article")
def test_duplicates_never_reach_gemini_and_busy_run_is_scaled(score, feeds, tmp_path):
    arts = [_art(f"a{i}", f"獨立新聞標題第{i:02d}則 台股盤勢分析報導") for i in range(24)]
    arts += [_art(f"d{i}", f"獨立新聞標題第{i:02d}則 台股盤勢分析報導 - Yahoo股市") for i in range(6)]
    feeds.return_value = arts
    score.return_value = {"direction": "bearish", "confidence": 1.0, "reason": "r"}
    vote = str(tmp_path / "regime_vote.json")
    state = rss_scorer.check_once(_cfg(tmp_path), {"seen_guids": []}, vote, now=NOW)

    assert score.call_count == 24
    assert state["last_dup_stories"] == 6
    assert {f"d{i}" for i in range(6)} <= set(state["seen_guids"])
    assert state["session_net"] == pytest.approx(-6.0)          # was -12 (24 x -0.5)
    # #340 writes the month file (news_log_YYYYMM.jsonl); the base name is
    # the pre-split file and is no longer created.
    files = rss_news_log.log_files(str(tmp_path / "news_log.jsonl"))
    assert files == [rss_news_log.month_path(str(tmp_path / "news_log.jsonl"),
                                             NOW.strftime("%Y%m"))]
    recs = [json.loads(x) for x in open(files[0], encoding="utf-8")]
    assert len(recs) == 24 and all(r["run_scale"] == 0.5 for r in recs)
    assert all(r["run_net"] == pytest.approx(-6.0) for r in recs)


@patch("rss_scorer.fetch_all_feeds")
@patch("rss_scorer.score_article")
def test_skipped_copy_is_not_remembered_so_the_other_outlet_is_scored(score, feeds, tmp_path):
    """A [SKIP] answer is marked seen; its story key is not kept.

    The other outlet's copy on the next pass is still sent to Gemini.
    """
    title = "台股暴挫2200點 綠燈一片 鴻海跌幅逆勢"
    feeds.return_value = [_art("y1", title)]
    score.return_value = None
    vote = str(tmp_path / "regime_vote.json")
    cfg = _cfg(tmp_path)
    state = rss_scorer.check_once(cfg, {"seen_guids": []}, vote, now=NOW)

    assert state["seen_guids"] == ["y1"]
    assert rss_scorer.story_key(title) not in (state.get("recent_stories") or {})

    feeds.return_value = [_art("g1", f"{title} - Yahoo股市")]
    score.return_value = {"direction": "bearish", "confidence": 1.0, "reason": "r"}
    score.reset_mock()
    state = rss_scorer.check_once(cfg, state, vote, now=NOW + timedelta(minutes=30))

    assert score.call_count == 1
    assert state["last_dup_stories"] == 0
    assert "g1" in state["seen_guids"]
    assert rss_scorer.story_key(title) in state["recent_stories"]


@patch("rss_scorer.fetch_all_feeds")
@patch("rss_scorer.score_article")
def test_same_run_skip_leaves_the_other_copy_for_the_next_pass(score, feeds, tmp_path):
    """Both copies arrive together. A [SKIP] on the kept copy must not
    consume the other outlet's GUID.

    Direct feeds are listed before Google News, so the kept copy is the
    one that just failed. The echo stays unseen and is scored next pass.
    """
    title = "台股暴挫2200點 綠燈一片 鴻海跌幅逆勢"
    feeds.return_value = [_art("g1", title), _art("y1", f"{title} - Yahoo股市")]

    def _answer(headline, summary, api_key, source=""):
        if headline == title:
            return None
        return {"direction": "bearish", "confidence": 1.0, "reason": "r"}

    score.side_effect = _answer
    vote = str(tmp_path / "regime_vote.json")
    cfg = _cfg(tmp_path)
    state = rss_scorer.check_once(cfg, {"seen_guids": []}, vote, now=NOW)

    assert score.call_count == 1
    assert score.call_args.args[0] == title
    assert state["seen_guids"] == ["g1"]
    key = rss_scorer.story_key(title)
    assert key not in (state.get("recent_stories") or {})

    score.reset_mock()
    state = rss_scorer.check_once(cfg, state, vote, now=NOW + timedelta(minutes=30))

    assert score.call_count == 1
    assert score.call_args.args[0] == f"{title} - Yahoo股市"
    assert state["session_net"] == pytest.approx(-0.5)
    assert key in state["recent_stories"]
    assert "y1" in state["seen_guids"]


@patch("rss_scorer.fetch_all_feeds")
@patch("rss_scorer.score_article")
def test_scored_kept_copy_marks_the_same_run_duplicate_seen(score, feeds, tmp_path):
    """When the kept copy scores, the other outlet is seen and the next
    pass does not send it to Gemini."""
    title = "台股暴挫2200點 綠燈一片 鴻海跌幅逆勢"
    feeds.return_value = [_art("g1", title), _art("y1", f"{title} - Yahoo股市")]
    score.return_value = {"direction": "bearish", "confidence": 1.0, "reason": "r"}
    vote = str(tmp_path / "regime_vote.json")
    cfg = _cfg(tmp_path)
    state = rss_scorer.check_once(cfg, {"seen_guids": []}, vote, now=NOW)

    assert score.call_count == 1
    assert "y1" in state["seen_guids"]
    assert rss_scorer.story_key(title) in state["recent_stories"]

    score.reset_mock()
    state = rss_scorer.check_once(cfg, state, vote, now=NOW + timedelta(minutes=30))
    assert score.call_count == 0
    assert state["last_dup_stories"] == 0


# ── dashboard numbers follow run_scale ───────────────────────────────────

def test_drivers_weight_by_run_scale():
    from scripts.dashboard import news_pulse
    rows = [{"session": "s", "direction": "bearish", "scored_at": "t1", "score": -1.0,
             "title": "a", "link": "", "source": "", "reason": "", "run_scale": 0.5},
            {"session": "s", "direction": "bearish", "scored_at": "t1", "score": -1.0,
             "title": "b", "link": "", "source": "", "reason": "", "run_scale": 0.0}]
    out = news_pulse.build_drivers(rows, "s", 0.84)
    assert out["total"] == pytest.approx(-0.25)


def test_old_log_lines_read_as_scale_one():
    from scripts.dashboard import news_feed
    row = news_feed.normalize({"title": "t", "scored_at": "2026-11-01T10:00:00+08:00",
                               "score": -0.5, "direction": "bearish"})
    assert row["run_scale"] == 1.0


# ── history rescale ──────────────────────────────────────────────────────

def _line(at, session, title, score, run_net=0.0, session_net=0.0):
    return json.dumps({"v": 1, "scored_at": at, "session": session, "title": title,
                       "direction": "bearish" if score < 0 else "neutral",
                       "score": score, "run_net": run_net, "session_net": session_net},
                      ensure_ascii=False) + "\n"


def _history():
    s1, s2 = "2026-11-19|NIGHT", "2026-11-20|NIGHT"
    lines = [_line("2026-11-19T09:30:00+08:00", s1, f"第一輪新聞標題{i:02d} 台股走勢分析", -1.0,
                   -12.0, -12.0) for i in range(24)]
    lines.append("not json\n")
    lines += [_line("2026-11-19T10:00:00+08:00", s1, "第一輪新聞標題00 台股走勢分析 - UDN", -1.0,
                    -0.5, -10.58),
              _line("2026-11-19T10:00:00+08:00", s1, "央行理監事會決議維持利率不變", -0.4,
                    -0.4, -10.98)]
    lines += [_line("2026-11-20T09:30:00+08:00", s2, "新的一天新的新聞標題 外資動向", -0.2,
                    -0.2, -0.2)]
    return lines


def test_rescale_replays_scale_dedup_and_ema():
    out, summary = rescale_news_log.rescale(_history())
    assert "not json\n" in out
    recs = [json.loads(x) for x in out if x != "not json\n"]
    first = recs[:24]
    assert all(r["run_scale"] == 0.5 and r["run_net"] == pytest.approx(-6.0) for r in first)
    assert first[-1]["session_net"] == pytest.approx(-6.0)
    dup, other = recs[24], recs[25]
    assert dup["story_dup"] is True and dup["run_scale"] == 0.0
    assert other["run_scale"] == 1.0 and other["run_net"] == pytest.approx(-0.4)
    assert other["session_net"] == pytest.approx(round(-6.0 * rss_scorer.EMA_DECAY - 0.4, 4))
    assert recs[26]["session_net"] == pytest.approx(-0.2)        # new session resets
    assert summary["duplicates"] == 1 and summary["runs"] == 3
    assert summary["sessions"]["2026-11-19|NIGHT"]["old_final"] == pytest.approx(-10.98)


def test_rescale_is_idempotent():
    once, _ = rescale_news_log.rescale(_history())
    twice, _ = rescale_news_log.rescale(once)
    assert once == twice


def test_dry_run_writes_nothing_and_apply_keeps_a_backup(tmp_path):
    log = tmp_path / "news_log.jsonl"
    log.write_text("".join(_history()), encoding="utf-8")
    state = tmp_path / "rss_scorer_state.json"
    state.write_text(json.dumps({"session_key": "2026-11-20|NIGHT", "session_net": -76.8,
                                 "session_peak": -77.0, "seen_guids": ["x"]}), encoding="utf-8")
    before = log.read_text(encoding="utf-8")
    assert rescale_news_log.main(["--log", str(log), "--state", str(state)]) == 0
    assert log.read_text(encoding="utf-8") == before
    assert json.loads(state.read_text(encoding="utf-8"))["session_net"] == -76.8

    assert rescale_news_log.main(["--log", str(log), "--state", str(state), "--apply"]) == 0
    assert len(list(tmp_path.glob("news_log.jsonl.bak-*"))) == 1
    assert len(list(tmp_path.glob("rss_scorer_state.json.bak-*"))) == 1
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["session_net"] == pytest.approx(-0.2) and st["seen_guids"] == ["x"]
    assert st["recent_runs"] == [pytest.approx(-0.2)]


def test_rescale_processes_the_base_file_and_the_monthly_file(tmp_path):
    text = "".join(_history())
    base = tmp_path / "news_log.jsonl"
    month = tmp_path / "news_log_202611.jsonl"
    base.write_text(text, encoding="utf-8")
    month.write_text(text, encoding="utf-8")
    state = tmp_path / "rss_scorer_state.json"
    state.write_text(json.dumps({"session_key": "2026-11-20|NIGHT", "session_net": -76.8,
                                 "session_peak": -77.0, "seen_guids": ["x"]}), encoding="utf-8")

    assert rescale_news_log.main(["--log", str(base), "--state", str(state)]) == 0
    assert base.read_text(encoding="utf-8") == text
    assert month.read_text(encoding="utf-8") == text
    assert json.loads(state.read_text(encoding="utf-8"))["session_net"] == -76.8
    assert list(tmp_path.glob("*.bak-*")) == []

    assert rescale_news_log.main(["--log", str(base), "--state", str(state), "--apply"]) == 0
    expected = "".join(rescale_news_log.rescale(_history())[0])
    assert base.read_text(encoding="utf-8") == expected
    assert month.read_text(encoding="utf-8") == expected
    assert expected != text
    assert len(list(tmp_path.glob("news_log.jsonl.bak-*"))) == 1
    assert len(list(tmp_path.glob("news_log_202611.jsonl.bak-*"))) == 1
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["session_net"] == pytest.approx(-0.2) and st["seen_guids"] == ["x"]

    assert rescale_news_log.main(["--log", str(base), "--apply"]) == 0
    assert base.read_text(encoding="utf-8") == expected
    assert month.read_text(encoding="utf-8") == expected


def test_state_for_another_session_is_left_alone(tmp_path):
    _, summary = rescale_news_log.rescale(_history())
    st = {"session_key": "2026-11-21|NIGHT", "session_net": 3.0}
    assert rescale_news_log.update_state(st, summary) is False and st["session_net"] == 3.0
