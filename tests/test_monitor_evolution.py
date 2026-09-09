"""Tests for the evolution-pipeline check (scripts/monitor/check_evolution.py).

Every case injects ``now`` (and ``pid_alive_fn`` where liveness matters)
so nothing depends on the wall clock, on live processes, or on the real
repo tree.

Anchor time: Wednesday 2026-09-02 12:00 TPE, which makes the last due
slot Saturday 2026-08-29 05:05 TPE.  The fixtures are written against
the known-bad shapes — a UTC row that would land on the wrong TPE date,
a watermark frozen two Saturdays back, a tree whose only running bot is
a regime bot — so they fail if the check stops noticing them.
"""

import json
import os
from datetime import datetime, timedelta

import pytest

from scripts.monitor.check_evolution import check_evolution, last_due
from scripts.monitor.common import TZ_TPE

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=TZ_TPE)      # Wednesday
DUE = datetime(2026, 8, 29, 5, 5, tzinfo=TZ_TPE)      # Saturday 05:05 TPE

# 2026-08-28T21:05:49Z == 2026-08-29 05:05:49 TPE — the due slot itself.
DUE_ROW_UTC = "2026-08-28T21:05:49+00:00"

USAGE_HEADER = ("timestamp,call_site,provider,model,input_tokens,"
                "output_tokens,reasoning_tokens,total_tokens")


def levels(findings):
    return [f.level for f in findings]


def messages(findings, level=None):
    return [f.message for f in findings if level is None or f.level == level]


def has_level(findings, level):
    return any(f.level == level for f in findings)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def set_mtime(bot, when):
    """Pin every file in *bot* to ``when``.

    ``last_activity`` reads real mtimes, so a fixture that relied on "the
    file was just written" would drift the moment the suite is run on a
    day other than the anchor.
    """
    stamp = when.timestamp()
    for path in bot.iterdir():
        if path.is_file():
            os.utime(path, (stamp, stamp))


def write_usage(repo, rows):
    """``rows`` = ``[(utc_iso, call_site), ...]``."""
    (repo / "data").mkdir(parents=True, exist_ok=True)
    body = [USAGE_HEADER]
    for stamp, site in rows:
        body.append(f"{stamp},{site},google,gemini-2.5-pro,100,20,10,130")
    (repo / "data" / "ai_usage.csv").write_text("\n".join(body) + "\n",
                                                encoding="utf-8")


def make_bot(tmp_path, name="TMF00_0422", watermark=None, baseline=None,
             pid=None, regime_mode=False, session=True):
    """A bot directory with the evolution artefacts a real deploy leaves."""
    bot = tmp_path / "live" / name
    bot.mkdir(parents=True, exist_ok=True)
    if watermark is not None:
        write_json(bot / "evolution_watermark.json", watermark)
    if baseline is not None:
        write_json(bot / "evolution.json", baseline)
    if session:
        payload = {"strategy": "AI: BbandSmaShortV3", "trading_mode": "semi_auto",
                   "saved_at": "2026-09-02T11:55:00"}
        if regime_mode:
            payload["regime_mode"] = True
        write_json(bot / "session.json", payload)
    if pid is not None:
        (bot / ".lock").write_text(str(pid), encoding="utf-8")
    return bot


@pytest.fixture
def repo(tmp_path):
    """A repo root with no changelog.json and an Evo-free index.json."""
    (tmp_path / "strategies").mkdir(parents=True, exist_ok=True)
    write_json(tmp_path / "strategies" / "index.json",
               [{"class_name": "BbandSmaShortV3",
                 "filename": "bband_sma_short_v3.py"}])
    return tmp_path


def run(tmp_path, repo, alive=True):
    return check_evolution(NOW, str(tmp_path / "live"), str(repo),
                           pid_alive_fn=lambda pid: alive)


# ── due-slot arithmetic ─────────────────────────────────────────────────

def test_last_due_from_midweek_is_the_previous_saturday():
    assert last_due(NOW) == DUE


def test_last_due_on_saturday_before_0505_uses_the_previous_saturday():
    """The week's run has not had its chance yet — grading it as missed
    would fire a false P2 every Saturday morning."""
    sat_early = datetime(2026, 8, 29, 4, 30, tzinfo=TZ_TPE)
    assert last_due(sat_early) == datetime(2026, 8, 22, 5, 5, tzinfo=TZ_TPE)


def test_last_due_on_saturday_after_0505_is_today():
    sat_late = datetime(2026, 8, 29, 6, 0, tzinfo=TZ_TPE)
    assert last_due(sat_late) == DUE


# ── check 1: usage evidence ─────────────────────────────────────────────

def test_healthy_run_with_codegen_has_no_p2(tmp_path, repo):
    make_bot(tmp_path, watermark={"trade_count": 127, "at": "2026-08-29 05:05:01"},
             pid=4242)
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution"),
                       ("2026-08-28T21:06:06+00:00", "evolution_codegen_1")])
    findings, lines = run(tmp_path, repo)
    assert not has_level(findings, "P2"), messages(findings, "P2")
    assert any("codegen reached" in ln for ln in lines), lines
    assert any("last evolution run: 2026-08-29 TPE" in ln for ln in lines), lines


def test_missed_saturday_is_p2(tmp_path, repo):
    """A watermark exists (the feature IS in use) but nothing reached the
    AI on the due date — the GUI was closed or no bot was eligible."""
    make_bot(tmp_path, watermark={"trade_count": 127, "at": "2026-08-01 05:05:01"},
             pid=4242)
    write_usage(repo, [("2026-07-31T21:05:49+00:00", "bot_evolution"),
                       ("2026-07-31T21:06:35+00:00", "evolution_codegen_1")])
    findings, _ = run(tmp_path, repo)
    assert any("did not start on 2026-08-29" in m
               for m in messages(findings, "P2")), messages(findings)


def test_plan_phase_only_is_p3_not_p2(tmp_path, repo):
    """Reaching the plan phase and stopping is a verdict, not an outage."""
    make_bot(tmp_path, watermark={"trade_count": 127, "at": "2026-08-29 05:05:01"},
             pid=4242)
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution"),
                       ("2026-08-28T21:06:06+00:00", "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert not any("did not start" in m for m in messages(findings, "P2")), \
        messages(findings, "P2")
    assert any("stopped at plan phase" in m
               for m in messages(findings, "P3")), messages(findings, "P3")
    assert any("codegen NOT reached" in ln for ln in lines), lines


def test_utc_row_before_midnight_counts_for_the_tpe_saturday(tmp_path, repo):
    """2026-08-28T20:00Z is Sat 2026-08-29 04:00 TPE — the SAME TPE date
    as the due slot.  Taking the date off the raw UTC stamp would file it
    under Friday 08-28 and report a missed Saturday."""
    make_bot(tmp_path, watermark={"trade_count": 127, "at": "2026-08-29 05:05:01"},
             pid=4242)
    write_usage(repo, [("2026-08-28T20:00:00+00:00", "bot_evolution"),
                       ("2026-08-28T20:01:00+00:00", "evolution_codegen_1")])
    findings, lines = run(tmp_path, repo)
    assert not has_level(findings, "P2"), messages(findings, "P2")
    assert any("last evolution run: 2026-08-29 TPE" in ln for ln in lines), lines


def test_missing_ai_usage_is_only_p3(tmp_path, repo):
    make_bot(tmp_path, watermark={"trade_count": 127, "at": "2026-08-29 05:05:01"},
             pid=4242)
    findings, _ = run(tmp_path, repo)
    assert not any("did not start" in m for m in messages(findings, "P2")), \
        messages(findings, "P2")
    assert any("ai_usage.csv missing" in m for m in messages(findings, "P3"))


def test_never_used_tree_has_no_p2_at_all(tmp_path, repo):
    """No watermark anywhere = the feature was never used here.  A silent
    Saturday in that tree is expected, not an outage.

    The ledger exists and is full of NON-evolution call sites (chat, trade
    reviews) — matching the real repo, where ai_usage.csv is shared by
    every AI feature."""
    make_bot(tmp_path, name="TMF00_plain", pid=4242)
    write_usage(repo, [("2026-08-29T02:00:00+00:00", "chat"),
                       ("2026-08-29T02:01:00+00:00", "trade_review")])
    findings, lines = run(tmp_path, repo)
    assert not has_level(findings, "P1"), messages(findings, "P1")
    assert not has_level(findings, "P2"), messages(findings, "P2")
    assert any("never been used" in ln for ln in lines), lines


def test_never_used_tree_without_any_ledger_has_no_p2(tmp_path, repo):
    make_bot(tmp_path, name="TMF00_plain", pid=4242)
    findings, _ = run(tmp_path, repo)
    assert not has_level(findings, "P1"), messages(findings, "P1")
    assert not has_level(findings, "P2"), messages(findings, "P2")


# ── check 1b: did it START? (debug-log evidence) ────────────────────────
#
# ai_usage.csv cannot see a run that died before its first AI call, so
# "never started" and "started, then stalled" produce an identical empty
# ledger.  Only the debug log separates them.

def start_line(stamp):
    """The line run_backtest.py logs the moment the slot fires."""
    return (f"[LIVE] [{stamp} TPE / 2026-01-01 00:00:00 local] "
            f"🧬 週末自動演化 Weekly auto-evolution (post-close) starting...")


def noise_line(stamp):
    return (f"[{stamp} TPE / 2026-01-01 00:00:00 local] "
            f"帳戶資料 Account: equity=80683 available=80683 float_pnl=0")


def write_debug_log(bot, tag, rows, mtime=None):
    """``debug_<tag>.log`` — named for the day the deploy OPENED it.

    The mtime is pinned, never left to the wall clock: the scan only
    considers logs still being written at or after the slot, so a
    fixture that inherited "now" would change meaning the day the suite
    is run, and `set_mtime` (added for `last_activity`) would silently
    push a log out of range.
    """
    path = bot / f"debug_{tag}.log"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    stamp = (mtime or (DUE + timedelta(hours=1))).timestamp()
    os.utime(path, (stamp, stamp))
    return path


# The previous Saturday: a watermark this fresh is not stale, so these
# cases assert on the start/no-start finding without a second P2 in the way.
PREV_SLOT = {"trade_count": 127, "at": "2026-08-22 05:05:01"}
PREV_ROW_UTC = "2026-08-21T21:05:49+00:00"


def test_start_line_without_a_plan_call_raises_nothing(tmp_path, repo):
    """The real 2026-09-05 shape: the slot fired and returned before the
    AI was called.

    Every early return in _bot_evolution — including the holdout skip,
    which fires whenever the week's trades all sit inside the withheld
    window — happens before the first AI call.  So a run that completed
    exactly as designed leaves the ledger empty and the watermark
    frozen.  Reporting that as an outage sends the operator hunting a
    failure that did not happen.  The log is also named for a day two
    weeks earlier, because a deploy keeps writing to the file it opened.
    """
    bot = make_bot(tmp_path, watermark=PREV_SLOT, pid=4242)
    write_debug_log(bot, "20260815", [noise_line("2026-08-29 05:04:53"),
                                      start_line("2026-08-29 05:05:25"),
                                      noise_line("2026-08-29 05:06:00")])
    write_usage(repo, [(PREV_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert not any("did not start" in m for m in messages(findings)), \
        messages(findings)
    assert not has_level(findings, "P2"), messages(findings, "P2")
    assert any("start line in debug logs: TMF00_0422 05:05:25" in ln
               for ln in lines), lines
    assert any("by-design holdout skip" in ln for ln in lines), lines


def test_first_ever_slot_that_self_limits_is_not_called_unused(tmp_path, repo):
    """No watermark anywhere yet — it is only written once an attempt
    gets past the early returns.  A start line still proves the feature
    is live, so the tree must not be written off as "never used"."""
    bot = make_bot(tmp_path, pid=4242)
    write_debug_log(bot, "20260815", [start_line("2026-08-29 05:05:25")])
    write_usage(repo, [("2026-08-29T02:00:00+00:00", "chat")])
    findings, lines = run(tmp_path, repo)
    assert not has_level(findings, "P2"), messages(findings, "P2")
    assert not any("never been used" in ln for ln in lines), lines
    assert any("start line in debug logs: TMF00_0422 05:05:25" in ln
               for ln in lines), lines


def test_no_start_line_in_a_live_log_still_reports_a_non_start(tmp_path, repo):
    """The bot was logging through the slot and never started — the
    original reading, which stays correct when the evidence supports it."""
    bot = make_bot(tmp_path, watermark=PREV_SLOT, pid=4242)
    write_debug_log(bot, "20260815", [noise_line("2026-08-29 05:04:53"),
                                      noise_line("2026-08-29 05:06:00")])
    write_usage(repo, [(PREV_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert any("did not start on 2026-08-29" in m
               for m in messages(findings, "P2")), messages(findings, "P2")
    assert any("start line in debug logs: none for this slot" in ln
               for ln in lines), lines


def test_start_line_from_another_saturday_is_not_credited(tmp_path, repo):
    """One log spans many weeks, so it holds every past slot's start
    line.  Crediting the wrong one would excuse a Saturday that never
    fired."""
    bot = make_bot(tmp_path, watermark=PREV_SLOT, pid=4242)
    write_debug_log(bot, "20260815", [start_line("2026-08-22 05:05:11"),
                                      noise_line("2026-08-29 05:06:00")])
    write_usage(repo, [(PREV_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert any("did not start on 2026-08-29" in m
               for m in messages(findings, "P2")), messages(findings, "P2")
    assert any("none for this slot" in ln for ln in lines), lines


def test_every_started_bot_is_named_in_report_order(tmp_path, repo):
    """The slot fires per bot, so the evidence line names each one — in
    the order they fired."""
    first = make_bot(tmp_path, name="TMF00_0422", watermark=PREV_SLOT, pid=4242)
    second = make_bot(tmp_path, name="TMF00_short", watermark=PREV_SLOT, pid=4243)
    write_debug_log(first, "20260815", [start_line("2026-08-29 05:05:25")])
    write_debug_log(second, "20260815", [start_line("2026-08-29 05:05:28")])
    write_usage(repo, [(PREV_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert any("TMF00_0422 05:05:25, TMF00_short 05:05:28" in ln
               for ln in lines), lines
    assert not has_level(findings, "P2"), messages(findings, "P2")


# ── which logs the scan is allowed to look at ───────────────────────────

def test_slot_is_found_behind_many_newer_logs(tmp_path, repo):
    """A bot redeployed daily leaves a log per deploy — one here has 33.

    Capping the scan at the newest few would drop the file the slot is
    actually in and report a non-start, which is the original bug with
    the sign flipped.
    """
    bot = make_bot(tmp_path, watermark=PREV_SLOT, pid=4242)
    write_debug_log(bot, "20260815", [start_line("2026-08-29 05:05:25")])
    for tag in ("20260830", "20260831", "20260901", "20260902",
                "20260903", "20260904", "20260905"):
        write_debug_log(bot, tag, [noise_line("2026-09-01 09:00:00")],
                        mtime=NOW)
    write_usage(repo, [(PREV_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert any("start line in debug logs: TMF00_0422 05:05:25" in ln
               for ln in lines), lines
    assert not any("did not start" in m for m in messages(findings)), \
        messages(findings)


def test_log_closed_before_the_slot_is_not_scanned(tmp_path, repo):
    """A deploy that stopped before 05:05 cannot hold the slot.  Its
    stale start line from an earlier Saturday must not be credited."""
    bot = make_bot(tmp_path, watermark=PREV_SLOT, pid=4242)
    write_debug_log(bot, "20260815", [start_line("2026-08-29 05:05:25")],
                    mtime=DUE - timedelta(hours=2))
    write_usage(repo, [(PREV_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert any("none for this slot" in ln for ln in lines), lines
    assert any("did not start on 2026-08-29" in m
               for m in messages(findings, "P2")), messages(findings, "P2")


def test_log_opened_after_the_slot_is_not_scanned(tmp_path, repo):
    """Opened the day AFTER the due Saturday, so it cannot contain that
    slot however new it is."""
    bot = make_bot(tmp_path, watermark=PREV_SLOT, pid=4242)
    write_debug_log(bot, "20260830", [start_line("2026-08-29 05:05:25")],
                    mtime=NOW)
    write_usage(repo, [(PREV_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert any("none for this slot" in ln for ln in lines), lines
    assert any("did not start on 2026-08-29" in m
               for m in messages(findings, "P2")), messages(findings, "P2")


def test_log_opened_on_the_due_day_is_scanned(tmp_path, repo):
    """A deploy started early on the due Saturday still catches the slot
    on its first post-deploy poll."""
    bot = make_bot(tmp_path, watermark=PREV_SLOT, pid=4242)
    write_debug_log(bot, "20260829", [start_line("2026-08-29 05:05:25")])
    write_usage(repo, [(PREV_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert any("start line in debug logs: TMF00_0422 05:05:25" in ln
               for ln in lines), lines
    assert not any("did not start" in m for m in messages(findings)), \
        messages(findings)


# ── check 2: watermarks / baselines ─────────────────────────────────────

def test_stale_watermark_with_recent_activity_is_p2(tmp_path, repo):
    """08-15 is two due-Saturdays back; session.json is fresh, so the bot
    was demonstrably alive and still skipped a whole cycle."""
    bot = make_bot(tmp_path, watermark={"trade_count": 4, "at": "2026-08-15 05:05:00"},
                   pid=4242)
    set_mtime(bot, NOW - timedelta(days=1))
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution")])
    findings, _ = run(tmp_path, repo)
    assert any("not attempted for 2+ weeks" in m
               for m in messages(findings, "P2")), messages(findings)


def test_stale_watermark_on_a_dormant_bot_is_archaeology(tmp_path, repo):
    """Same stale watermark, but nothing in the directory has moved in
    months — data/live is full of retired test bots."""
    bot = make_bot(tmp_path, name="TMF00_test",
                   watermark={"trade_count": 4, "at": "2026-05-01 05:05:00"},
                   pid=4242)
    set_mtime(bot, NOW - timedelta(days=90))
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert not any("not attempted for 2+ weeks" in m
                   for m in messages(findings, "P2")), messages(findings, "P2")
    assert any("archaeology" in ln for ln in lines), lines


def test_fresh_watermark_is_reported_but_clean(tmp_path, repo):
    make_bot(tmp_path, watermark={"trade_count": 127, "at": "2026-08-29 05:05:01"},
             baseline={"best_composite": 0.3627, "best_recorded_at":
                       "2026-07-04 04:58:10", "n_updates": 5},
             pid=4242)
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution"),
                       ("2026-08-28T21:06:06+00:00", "evolution_codegen_1")])
    findings, lines = run(tmp_path, repo)
    assert not has_level(findings, "P2"), messages(findings, "P2")
    assert any("trade_count=127" in ln and "n_updates=5" in ln
               for ln in lines), lines


def test_bot_without_any_evolution_artefact_is_skipped_silently(tmp_path, repo):
    """A regime bot leaves no watermark and no baseline BY DESIGN."""
    make_bot(tmp_path, name="TMF00_regime", pid=4242, regime_mode=True)
    make_bot(tmp_path, name="TMF00_plain",
             watermark={"trade_count": 9, "at": "2026-08-29 05:05:01"}, pid=4242)
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert not any("TMF00_regime" in ln and "watermark" in ln for ln in lines), lines
    assert not any("TMF00_regime" in m for m in messages(findings)), messages(findings)


# ── check 3: eligibility now ────────────────────────────────────────────

def test_running_plain_bot_is_eligible(tmp_path, repo):
    make_bot(tmp_path, name="TMF00_plain",
             watermark={"trade_count": 9, "at": "2026-08-29 05:05:01"}, pid=4242)
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo, alive=True)
    assert not any("cannot fire" in m for m in messages(findings, "P2")), \
        messages(findings, "P2")
    assert any("eligible (running, non-regime): TMF00_plain" in ln
               for ln in lines), lines


def test_no_running_bot_is_p2(tmp_path, repo):
    make_bot(tmp_path, name="TMF00_plain",
             watermark={"trade_count": 9, "at": "2026-08-29 05:05:01"}, pid=4242)
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution")])
    findings, _ = run(tmp_path, repo, alive=False)
    assert any("cannot fire" in m for m in messages(findings, "P2")), \
        messages(findings)


def test_only_regime_bots_running_is_p2(tmp_path, repo):
    """Regime bots are excluded from evolution by design, so a tree whose
    only live deploy is a regime bot can never evolve."""
    make_bot(tmp_path, name="TMF00_regime", pid=4242, regime_mode=True)
    make_bot(tmp_path, name="TMF00_plain",
             watermark={"trade_count": 9, "at": "2026-08-29 05:05:01"})
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo, alive=True)
    assert any("cannot fire" in m for m in messages(findings, "P2")), \
        messages(findings)
    assert any("eligible (running, non-regime): none" in ln for ln in lines)


# ── check 4: zero-PASS structural note ──────────────────────────────────

def test_zero_pass_note_fires_without_changelog_or_evo_class(tmp_path, repo):
    make_bot(tmp_path, watermark={"trade_count": 127, "at": "2026-08-29 05:05:01"},
             pid=4242)
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution")])
    findings, _ = run(tmp_path, repo)
    assert any("never PASSed a candidate" in m
               for m in messages(findings, "P3")), messages(findings, "P3")


def test_zero_pass_note_silent_once_an_evo_class_exists(tmp_path, repo):
    write_json(repo / "strategies" / "index.json",
               [{"class_name": "BbandSmaShortV3Evo1",
                 "filename": "bband_sma_short_v3_evo1.py"}])
    make_bot(tmp_path, watermark={"trade_count": 127, "at": "2026-08-29 05:05:01"},
             pid=4242)
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution")])
    findings, lines = run(tmp_path, repo)
    assert not any("never PASSed" in m for m in messages(findings)), \
        messages(findings)
    assert any("BbandSmaShortV3Evo1" in ln for ln in lines), lines


def test_zero_pass_note_silent_once_changelog_exists(tmp_path, repo):
    write_json(repo / "data" / "changelog.json", {"entries": []})
    make_bot(tmp_path, watermark={"trade_count": 127, "at": "2026-08-29 05:05:01"},
             pid=4242)
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution")])
    findings, _ = run(tmp_path, repo)
    assert not any("never PASSed" in m for m in messages(findings)), \
        messages(findings)


# ── read-only contract ──────────────────────────────────────────────────

def test_check_never_touches_bot_state(tmp_path, repo):
    bot = make_bot(tmp_path, watermark={"trade_count": 127, "at": "2026-08-01 05:05:01"},
                   baseline={"best_composite": 0.1, "n_updates": 1}, pid=99999)
    write_usage(repo, [(DUE_ROW_UTC, "bot_evolution")])
    before = {p.name: p.read_bytes()
              for p in bot.iterdir() if p.is_file()}
    run(tmp_path, repo, alive=False)
    after = {p.name: p.read_bytes() for p in bot.iterdir() if p.is_file()}
    assert before == after, "monitoring must never mutate bot state"
