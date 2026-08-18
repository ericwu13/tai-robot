"""Tests for src/regime/episodes.py — switching-log episode grouping.

Fixtures mirror the real regime_history.csv quirks the module must
absorb: duplicate session rows (pre-idempotent-record files), pre-v3
short rows without the votes column, DAY result rows, and unknown
strategies from retired configs.
"""

from src.regime.episodes import (
    format_votes_cell,
    group_episodes,
    parse_history,
    trend_text,
)
from src.regime.store import _V2_HEADER, _V3_HEADER


def _row(date, session, *, raw="", eff="", adx="", decision="",
         pnl="", trades="", active="", votes=None):
    cells = {name: "" for name in _V3_HEADER}
    cells.update({"date": date, "session": session, "raw_regime": raw,
                  "effective_regime": eff, "adx": adx, "decision": decision,
                  "pnl": pnl, "trades": trades, "strategy_active": active})
    if votes is None:               # pre-v3 short row
        return [cells[name] for name in _V2_HEADER]
    cells["votes"] = votes
    return [cells[name] for name in _V3_HEADER]


LEG_OF = {"AI: LongBot": "long", "AI: ShortBot": "short"}.get


# ── parse_history ────────────────────────────────────────────────────────

def test_parse_skips_header_only():
    assert parse_history([list(_V3_HEADER)]) == []


def test_parse_reads_mixed_v2_and_v3_rows():
    rows = [list(_V3_HEADER),
            _row("2026-08-04", "NIGHT", raw="range-bound", adx="15.3"),
            _row("2026-08-05", "NIGHT", raw="trending-up", adx="44.6",
                 votes="W3:trending-up*")]
    sessions = parse_history(rows)
    assert sessions[0].votes == ""          # short row: guarded, not crashed
    assert sessions[1].votes == "W3:trending-up*"


def test_parse_merges_duplicate_session_rows():
    """Pre-fix files hold a bare result row AND a classification row for
    the same session — they must merge into one (later fields win)."""
    rows = [list(_V3_HEADER),
            _row("2026-08-05", "NIGHT", pnl="0.0", trades="0", active="idle"),
            _row("2026-08-05", "NIGHT", raw="range-bound", eff="unknown",
                 adx="15.3", decision="hold", votes="")]
    sessions = parse_history(rows)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.raw_regime == "range-bound"
    assert s.pnl == 0.0 and s.trades == 0
    assert s.strategy_active == "idle"


def test_parse_pnl_and_trades_typing():
    rows = [list(_V3_HEADER),
            _row("2026-08-13", "DAY", pnl="-2470.0", trades="2", active="AI: LongBot"),
            _row("2026-08-14", "NIGHT", raw="trending-down", decision="deploy_short")]
    sessions = parse_history(rows)
    assert sessions[0].pnl == -2470.0 and sessions[0].trades == 2
    assert sessions[1].pnl is None and sessions[1].trades is None


# ── group_episodes ───────────────────────────────────────────────────────

def _history():
    """The user's real August sequence, compressed: idle stretch →
    LONG episode → SHORT episode (open)."""
    return parse_history([
        list(_V3_HEADER),
        _row("2026-08-06", "NIGHT", raw="range-bound", decision="sit_out", pnl="0.0", trades="0", active="idle"),
        _row("2026-08-07", "NIGHT", raw="range-bound", decision="sit_out", pnl="0.0", trades="0", active="idle"),
        _row("2026-08-12", "NIGHT", raw="trending-up", decision="deploy_long", pnl="0.0", trades="0", active="idle", votes="W2:trending-up*"),
        _row("2026-08-13", "DAY", pnl="-2470.0", trades="2", active="AI: LongBot"),
        _row("2026-08-13", "NIGHT", raw="trending-up", decision="deploy_long", pnl="150.0", trades="1", active="AI: LongBot"),
        _row("2026-08-14", "DAY", pnl="-1410.0", trades="1", active="AI: LongBot"),
        _row("2026-08-14", "NIGHT", raw="trending-down", decision="deploy_short", pnl="0.0", trades="0", active="AI: LongBot"),
        _row("2026-08-17", "DAY", pnl="0.0", trades="0", active="AI: ShortBot"),
        _row("2026-08-17", "NIGHT", raw="range-bound", eff="trending-down", decision="hold", pnl="1190.0", trades="1", active="AI: ShortBot"),
        _row("2026-08-18", "DAY", pnl="2150.0", trades="1", active="AI: ShortBot"),
    ])


def test_group_episodes_by_active_leg():
    episodes = group_episodes(_history(), LEG_OF)
    assert [e.leg for e in episodes] == ["idle", "long", "short"]
    idle, long_ep, short_ep = episodes
    assert len(idle.sessions) == 3          # sit_out ×2 + the deploy_long night
    assert long_ep.start == "2026-08-13" and long_ep.end == "2026-08-14"
    assert short_ep.open and not long_ep.open


def test_episode_cumulative_pnl_and_trades():
    episodes = group_episodes(_history(), LEG_OF)
    idle, long_ep, short_ep = episodes
    assert idle.pnl == 0.0
    assert long_ep.pnl == -2470.0 + 150.0 - 1410.0
    assert long_ep.trades == 4
    assert short_ep.pnl == 1190.0 + 2150.0


def test_unknown_strategy_grouped_not_crashed():
    sessions = parse_history([
        list(_V3_HEADER),
        _row("2026-08-13", "DAY", pnl="5.0", trades="1", active="AI: RetiredBot")])
    episodes = group_episodes(sessions, LEG_OF)
    assert episodes[0].leg == "unknown"
    assert episodes[0].strategy == "AI: RetiredBot"


def test_day_row_without_strategy_inherits_running_episode():
    sessions = parse_history([
        list(_V3_HEADER),
        _row("2026-08-13", "NIGHT", raw="trending-up", decision="deploy_long", active="AI: LongBot"),
        _row("2026-08-14", "DAY", pnl="300.0", trades="1")])
    episodes = group_episodes(sessions, LEG_OF)
    assert len(episodes) == 1
    assert episodes[0].pnl == 300.0


def test_no_leg_mapper_defaults_to_unknown():
    sessions = parse_history([
        list(_V3_HEADER),
        _row("2026-08-13", "DAY", active="AI: LongBot")])
    assert group_episodes(sessions, None)[0].leg == "unknown"


# ── display helpers ──────────────────────────────────────────────────────

def test_format_votes_cell():
    assert format_votes_cell("") == ""
    assert format_votes_cell("W3:trending-up") == "W3↑"
    assert format_votes_cell("W3:trending-up+W2:trending-down") == "W3↑+W2↓"
    assert format_votes_cell("W2:trending-up*") == "W2↑ 加速"


def test_trend_text_unconfirmed_readable():
    sessions = parse_history([
        list(_V3_HEADER),
        _row("2026-08-04", "NIGHT", raw="trending-up", eff="unknown"),
        _row("2026-08-17", "NIGHT", raw="range-bound", eff="trending-down"),
        _row("2026-08-12", "NIGHT", raw="trending-up", eff="trending-up"),
        _row("2026-08-13", "DAY")])
    assert trend_text(sessions[0]) == "trending-up (未確認 unconfirmed)"
    assert trend_text(sessions[1]) == "range-bound (有效 trending-down)"
    assert trend_text(sessions[2]) == "trending-up"
    assert trend_text(sessions[3]) == ""
