"""P5 fire scoring: joining a W2 fire to the night it named and the leg it moved.

The whole point of the join is the LAG.  A fire writes a vote; the vote
is read at the ~04:58 classification of the night it names — the END of
that session — and the leg that classification picks only trades from
the next session onwards.  These tests pin each hop of that chain with
synthetic rows, including the two cases that are easy to get wrong: a
fire before 05:00 (which belongs to YESTERDAY's night) and a holiday gap
(where "the next night" is not "tomorrow").
"""

from datetime import datetime, timedelta

from src.news.fire_scoring import (
    OUT_HEADER,
    PNL_COLUMNS,
    Fire,
    HistoryRow,
    day_open_price,
    find_classification,
    leg_window_move,
    next_night_after,
    night_close_price,
    night_key_for,
    parse_tpe_time,
    score_fires,
    session_pnl,
)


# ── harness ────────────────────────────────────────────────────────────

def fire(time_tpe, direction="trending-down", tier="signal", **pnl):
    cells = {col: "" for col in PNL_COLUMNS}
    cells.update(pnl)
    return Fire(time_tpe=time_tpe, tier=tier, direction=direction, pnl=cells)


def night(date, decision="deploy_short", strategy="ShortBot",
          votes="W2:trending-down", pnl="", raw="trending-down"):
    return HistoryRow(date=date, session="NIGHT", decision=decision,
                      strategy_active=strategy, votes=votes, pnl=pnl,
                      raw_regime=raw)


def day(date, pnl="", strategy="ShortBot"):
    return HistoryRow(date=date, session="DAY", strategy_active=strategy,
                      pnl=pnl)


def bars_for(date, day_open=22000.0, night_close=22150.0):
    """A minimal day session (08:45-13:45) and night session (15:00-05:00)."""
    base = datetime.strptime(date, "%Y-%m-%d")
    out = []
    for i in range(3):                       # 08:45, 08:46, 08:47
        dt = base.replace(hour=8, minute=45) + timedelta(minutes=i)
        px = day_open + i
        out.append((dt, px, px + 5, px - 5, px + 1, 100))
    for i in range(3):                       # 15:00, 15:01, 15:02
        dt = base.replace(hour=15, minute=0) + timedelta(minutes=i)
        out.append((dt, night_close, night_close, night_close, night_close, 50))
    # the night's LAST bar lands after midnight — the straddle is the
    # reason a night is identified by its open date.
    last = base.replace(hour=4, minute=59) + timedelta(days=1)
    out.append((last, night_close, night_close, night_close, night_close, 50))
    return out


# ── night_key: the 05:00 boundary ──────────────────────────────────────

class TestNightKey:
    def test_evening_fire_targets_tonight(self):
        assert night_key_for(datetime(2026, 9, 1, 22, 31)) == "2026-09-01|NIGHT"

    def test_pre_0500_fire_targets_yesterdays_night(self):
        """A 02:30 fire is INSIDE the night that opened yesterday 15:00 —
        the one being classified in two and a half hours, not tonight's."""
        assert night_key_for(datetime(2026, 9, 2, 2, 30)) == "2026-09-01|NIGHT"

    def test_0459_is_still_last_night(self):
        assert night_key_for(datetime(2026, 9, 2, 4, 59)) == "2026-09-01|NIGHT"

    def test_0500_flips_to_tonight(self):
        assert night_key_for(datetime(2026, 9, 2, 5, 0)) == "2026-09-02|NIGHT"

    def test_daytime_fire_targets_tonight(self):
        assert night_key_for(datetime(2026, 9, 2, 10, 15)) == "2026-09-02|NIGHT"


class TestParseTime:
    def test_accepts_both_separators(self):
        assert parse_tpe_time("2026-09-01 22:31") == datetime(2026, 9, 1, 22, 31)
        assert parse_tpe_time("2026/09/01 22:31") == datetime(2026, 9, 1, 22, 31)

    def test_accepts_seconds_and_iso_t(self):
        assert parse_tpe_time("2026-09-01T22:31:07") == datetime(2026, 9, 1, 22, 31, 7)

    def test_strips_a_trailing_offset(self):
        assert parse_tpe_time("2026-09-01 22:31:07+08:00") == datetime(
            2026, 9, 1, 22, 31, 7)

    def test_junk_is_none(self):
        assert parse_tpe_time("") is None
        assert parse_tpe_time("last tuesday") is None


# ── history lookups ────────────────────────────────────────────────────

class TestHistoryLookups:
    def test_finds_the_classification_row(self):
        history = [night("2026-09-01", decision="deploy_short")]
        assert find_classification(history, "2026-09-01").decision == "deploy_short"

    def test_result_only_row_is_not_a_classification(self):
        """A row with P&L but no raw_regime means the bot recorded the
        session without classifying it — no vote was consumed."""
        history = [night("2026-09-01", pnl="1200", raw="")]
        assert find_classification(history, "2026-09-01") is None

    def test_missing_night_is_none(self):
        assert find_classification([night("2026-09-01")], "2026-08-31") is None

    def test_next_night_skips_a_holiday_gap(self):
        """The bot writes no rows on a closed day, so "the next night" is
        simply the next one that exists — 09-04 here, not 09-02."""
        history = [night("2026-09-01"), night("2026-09-04")]
        assert next_night_after(history, "2026-09-01") == "2026-09-04"

    def test_next_night_is_strictly_after(self):
        history = [night("2026-09-01"), night("2026-09-02")]
        assert next_night_after(history, "2026-09-01") == "2026-09-02"

    def test_next_night_unordered_history(self):
        history = [night("2026-09-04"), night("2026-09-01"), night("2026-09-02")]
        assert next_night_after(history, "2026-09-01") == "2026-09-02"

    def test_no_later_night_is_blank(self):
        assert next_night_after([night("2026-09-01")], "2026-09-01") == ""

    def test_session_pnl_by_slot(self):
        history = [day("2026-09-02", pnl="-400"), night("2026-09-02", pnl="1650")]
        assert session_pnl(history, "2026-09-02", "DAY") == "-400"
        assert session_pnl(history, "2026-09-02", "NIGHT") == "1650"
        assert session_pnl(history, "2026-09-03", "DAY") == ""


# ── price windows ──────────────────────────────────────────────────────

class TestPriceWindows:
    def test_day_open_is_the_first_day_session_bar(self):
        assert day_open_price(bars_for("2026-09-02", day_open=22000.0),
                              "2026-09-02") == 22000.0

    def test_night_close_reaches_past_midnight(self):
        """The night's last bar is stamped 04:59 the NEXT calendar day."""
        assert night_close_price(bars_for("2026-09-02", night_close=22150.0),
                                 "2026-09-02") == 22150.0

    def test_missing_day_is_none(self):
        bars = bars_for("2026-09-02")
        assert day_open_price(bars, "2026-09-03") is None
        assert night_close_price(bars, "2026-09-03") is None

    def test_up_fire_keeps_the_sign(self):
        bars = bars_for("2026-09-02", day_open=22000.0, night_close=22150.0)
        assert leg_window_move(bars, "2026-09-02", "trending-up") == "+150"

    def test_down_fire_flips_the_sign(self):
        """POSITIVE always means the fire was right: a market that rose
        150 points after a DOWN fire scores -150."""
        bars = bars_for("2026-09-02", day_open=22000.0, night_close=22150.0)
        assert leg_window_move(bars, "2026-09-02", "trending-down") == "-150"

    def test_down_fire_that_was_right_scores_positive(self):
        bars = bars_for("2026-09-02", day_open=22000.0, night_close=21800.0)
        assert leg_window_move(bars, "2026-09-02", "trending-down") == "+200"

    def test_bilingual_direction_words(self):
        bars = bars_for("2026-09-02", day_open=22000.0, night_close=22150.0)
        assert leg_window_move(bars, "2026-09-02", "bearish") == "-150"
        assert leg_window_move(bars, "2026-09-02", "看多") == "+150"

    def test_unreadable_direction_is_blank(self):
        bars = bars_for("2026-09-02")
        assert leg_window_move(bars, "2026-09-02", "???") == ""

    def test_missing_bars_are_blank(self):
        assert leg_window_move(bars_for("2026-09-02"), "2026-09-03",
                               "trending-down") == ""
        assert leg_window_move([], "2026-09-02", "trending-down") == ""


# ── the whole join ─────────────────────────────────────────────────────

class TestScoreFires:
    def test_evening_fire_full_chain(self):
        history = [
            night("2026-09-01", decision="deploy_short", strategy="ShortBot",
                  votes="W2:trending-down*"),
            day("2026-09-02", pnl="-400"),
            night("2026-09-02", pnl="1650", votes=""),
        ]
        bars = bars_for("2026-09-02", day_open=22000.0, night_close=21800.0)

        [scored] = score_fires(
            [fire("2026-09-01 22:31", **{"4h淨損益": "+1650"})], history, bars)

        assert scored.night_key == "2026-09-01|NIGHT"
        assert scored.consumed_row == "2026-09-01"
        assert scored.decision == "deploy_short"
        assert scored.strategy_active == "ShortBot"
        assert scored.votes == "W2:trending-down*"
        assert scored.leg_date == "2026-09-02"
        assert scored.leg_day_pnl == "-400"
        assert scored.leg_night_pnl == "1650"
        assert scored.leg_window_move == "+200"
        # the sheet's own P&L columns pass through untouched
        assert scored.fire.pnl["4h淨損益"] == "+1650"

    def test_pre_0500_fire_lands_on_yesterdays_night(self):
        history = [night("2026-09-01"), night("2026-09-02", pnl="900")]
        [scored] = score_fires([fire("2026-09-02 02:30")], history, [])

        assert scored.night_key == "2026-09-01|NIGHT"
        assert scored.consumed_row == "2026-09-01"
        assert scored.leg_date == "2026-09-02"
        assert scored.leg_night_pnl == "900"

    def test_holiday_gap_between_vote_and_leg(self):
        """Fire on Friday night; the market is shut until Monday, so the
        leg's first session is 09-04 — three calendar days later."""
        history = [
            night("2026-09-01"),
            day("2026-09-04", pnl="120"),
            night("2026-09-04", pnl="-330"),
        ]
        bars = bars_for("2026-09-04", day_open=22000.0, night_close=21900.0)

        [scored] = score_fires([fire("2026-09-01 21:00")], history, bars)

        assert scored.leg_date == "2026-09-04"
        assert scored.leg_day_pnl == "120"
        assert scored.leg_night_pnl == "-330"
        assert scored.leg_window_move == "+100"     # down fire, market fell

    def test_vote_that_was_never_classified(self):
        """A fire whose night has no classification row: the vote expired
        unread, or the bot was down. It still reports its night and leg."""
        history = [night("2026-09-05", pnl="300", raw="")]
        [scored] = score_fires([fire("2026-09-01 22:00")], history, [])

        assert scored.night_key == "2026-09-01|NIGHT"
        assert scored.consumed_row == ""
        assert scored.decision == ""
        assert scored.leg_date == "2026-09-05"

    def test_fire_with_no_later_night_has_blank_leg(self):
        history = [night("2026-09-01")]
        [scored] = score_fires([fire("2026-09-01 22:00")], history, [])
        assert scored.leg_date == ""
        assert scored.leg_day_pnl == "" and scored.leg_night_pnl == ""
        assert scored.leg_window_move == ""

    def test_unparseable_fire_is_kept_not_dropped(self):
        [scored] = score_fires([fire("last tuesday")], [night("2026-09-01")], [])
        assert scored.night_key == ""
        assert scored.leg_date == ""

    def test_row_matches_the_header(self):
        history = [night("2026-09-01"), night("2026-09-02", pnl="1")]
        [scored] = score_fires([fire("2026-09-01 22:00")], history, [])
        assert len(scored.to_row()) == len(OUT_HEADER)

    def test_multiple_fires_same_night(self):
        history = [night("2026-09-01"), night("2026-09-02", pnl="500")]
        rows = score_fires(
            [fire("2026-09-01 21:00"), fire("2026-09-02 03:10")], history, [])
        assert [r.night_key for r in rows] == ["2026-09-01|NIGHT"] * 2
        assert [r.leg_date for r in rows] == ["2026-09-02"] * 2

    def test_empty_inputs(self):
        assert score_fires([], [], []) == []


# ── the script's readers ───────────────────────────────────────────────
# The join is pure, but the readers are where this kind of tool actually
# breaks: a BOM on a sheet export, a history header that grew a column,
# a bar file that rotated mid-session.

def _script():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "score_fires.py"
    spec = importlib.util.spec_from_file_location("score_fires", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestReaders:
    def test_fires_export_with_a_bom(self, tmp_path):
        path = tmp_path / "fires.csv"
        path.write_text(
            "\ufeff時間TPE,tier,方向,1h淨損益,2h淨損益,4h淨損益,8h淨損益,收盤淨損益\n"
            "2026-09-01 22:31,signal,trending-down,+1650,-690,,,+120\n",
            encoding="utf-8")

        [f] = _script().load_fires(str(path))

        assert f.time_tpe == "2026-09-01 22:31"
        assert f.tier == "signal" and f.direction == "trending-down"
        assert f.pnl["1h淨損益"] == "+1650"
        assert f.pnl["4h淨損益"] == ""

    def test_fires_rows_without_a_timestamp_are_skipped(self, tmp_path):
        path = tmp_path / "fires.csv"
        path.write_text("時間TPE,tier,方向\n,signal,trending-down\n"
                        "2026-09-01 22:31,signal,trending-down\n",
                        encoding="utf-8")
        assert len(_script().load_fires(str(path))) == 1

    def test_history_is_read_by_header_name(self, tmp_path):
        """A pre-v4 file (one column short) must still parse — the header
        grew v2 -> v3 -> v4 and old rows were never padded."""
        from src.regime.store import _V3_HEADER, _V4_HEADER
        path = tmp_path / "regime_history.csv"
        v3_row = {n: "" for n in _V3_HEADER}
        v3_row.update(date="2026-09-01", session="NIGHT",
                      raw_regime="trending-down", decision="deploy_short",
                      strategy_active="ShortBot", votes="W2:trending-down*")
        lines = [",".join(_V4_HEADER),
                 ",".join(v3_row[n] for n in _V3_HEADER)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        [row] = _script().load_history(str(tmp_path))

        assert row.date == "2026-09-01" and row.session == "NIGHT"
        assert row.decision == "deploy_short"
        assert row.votes == "W2:trending-down*"
        assert row.pnl == ""

    def test_bars_merge_across_daily_files(self, tmp_path):
        """A night session straddles midnight, so its bars land in TWO
        daily files — the reader has to merge them into one series."""
        (tmp_path / "bars_1m_20260902.csv").write_text(
            "datetime,open,high,low,close,volume\n"
            "2026/09/02 08:45,22000,22010,21990,22005,100\n"
            "2026/09/02 15:00,21900,21910,21890,21905,60\n"
            "garbage row\n",
            encoding="utf-8")
        (tmp_path / "bars_1m_20260903.csv").write_text(
            "datetime,open,high,low,close,volume\n"
            "2026/09/03 04:59,21800,21810,21790,21800,60\n",
            encoding="utf-8")

        bars = _script().load_bars(str(tmp_path))

        assert len(bars) == 3
        assert bars == sorted(bars, key=lambda b: b[0])
        assert day_open_price(bars, "2026-09-02") == 22000.0
        assert night_close_price(bars, "2026-09-02") == 21800.0

    def test_missing_history_is_a_clean_exit(self, tmp_path):
        import pytest
        with pytest.raises(SystemExit):
            _script().load_history(str(tmp_path))
