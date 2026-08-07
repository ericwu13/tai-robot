"""Tests for the W2 cross-market monitor bridge (scripts/news_bridge/).

The monitor is the only component in the news framework that can write a
`risk_off` signal with no human in the loop, so the tier boundary is a
safety boundary: "signal"-tier symbols may auto-flatten the book on a
downside breach, "alert"-tier symbols must never write signal.json at all.
These tests pin that asymmetry, the freshness guard that handles session
hand-offs between Tokyo/Frankfurt/New York, the per-direction dedup, and
the 2-of-N regime vote.

The script lives under scripts/ and is not an importable package, so it is
loaded by path.  fetch_quote and post_discord are monkeypatched in every
test — nothing here touches the network.
"""

import importlib.util
import json
import time
import uuid
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parents[1]
           / "scripts" / "news_bridge" / "crossmarket_monitor.py")


def _load_monitor():
    spec = importlib.util.spec_from_file_location("crossmarket_monitor", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = _load_monitor()

TPE = timezone(timedelta(hours=8))
# 22:30 TPE — inside the night session that OPENED on 2026-08-05.
FROZEN_NOW = datetime(2026, 8, 5, 22, 30, tzinfo=TPE)


# ── harness ────────────────────────────────────────────────────────────

def quote(pct: float, age_sec: float = 0.0) -> dict:
    """A Yahoo-shaped quote dict *age_sec* seconds old."""
    prev = 100.0
    return {
        "price": prev * (1 + pct / 100.0),
        "prev_close": prev,
        "pct": pct,
        "quote_time": time.time() - age_sec,
    }


@pytest.fixture
def market(monkeypatch):
    """Control what every symbol quotes.  Unlisted symbols fetch as None."""
    book: dict[str, dict | None] = {}
    monkeypatch.setattr(cm, "fetch_quote", lambda sym: book.get(sym))
    return book


@pytest.fixture
def discord(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(cm, "post_discord", lambda hook, msg: sent.append(msg))
    return sent


@pytest.fixture
def frozen_clock(monkeypatch):
    """Freeze the monitor's wall clock (dedup buckets + session keys)."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW.replace(tzinfo=None)

    monkeypatch.setattr(cm, "datetime", _Frozen)
    return FROZEN_NOW


def make_args(tmp_path, **overrides):
    args = Namespace(
        signal_out=str(tmp_path / "signal.json"),
        vote_out=None,
        min_move=None,
        ignore_freshness=False,
        discord_webhook="https://discord.invalid/webhook",
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def allowance(symbol: str) -> float:
    """The symbol's own freshness allowance in seconds."""
    return cm.SYMBOLS[symbol].get("max_age", cm.QUOTE_MAX_AGE_SEC)


def read_json(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def signal_of(args):
    return read_json(args.signal_out)


# ── the tier table itself ──────────────────────────────────────────────

def test_symbol_table_tiers_and_thresholds():
    signal_tier = {s for s, c in cm.SYMBOLS.items() if c["tier"] == "signal"}
    alert_tier = {s for s, c in cm.SYMBOLS.items() if c["tier"] == "alert"}
    assert signal_tier == {"SOXX", "TSM", "QQQ", "ASML.AS"}
    assert alert_tier == {"^STOXX50E", "NQ=F", "^N225", "^KS11"}

    for sym, cfg in cm.SYMBOLS.items():
        assert cfg["down"] < 0 < cfg["up"], sym
        vd, vu = cfg["vote"]
        assert vd < 0 < vu, sym
        assert cm.VOTE_THRESHOLDS[sym] == cfg["vote"], sym

    assert cm.SYMBOLS["ASML.AS"]["down"] == -3.0
    assert cm.SYMBOLS["ASML.AS"]["up"] == 3.0
    assert cm.SYMBOLS["TSM"]["vote"] == (-2.0, 2.0)


def test_freshness_allowance_matches_feed_latency():
    """Yahoo is real-time only for US cash: CME futures run ~10 min behind,
    Asian/European cash ~15-20 min."""
    for sym in ("SOXX", "TSM", "QQQ"):
        assert allowance(sym) == 600, sym
    assert allowance("NQ=F") == 900
    for sym in ("ASML.AS", "^STOXX50E", "^N225", "^KS11"):
        assert allowance(sym) == 1500, sym


# ── 1. signal tier: downside auto-fires ────────────────────────────────

def test_signal_tier_downside_writes_risk_off(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-3.1)
    market["QQQ"] = quote(-0.4)
    args = make_args(tmp_path)

    state = cm.check_once(args, {})

    sig = signal_of(args)
    assert sig is not None, "signal-tier downside breach must write signal.json"
    assert sig["action"] == "risk_off"
    assert sig["version"] == 1
    assert "SOXX" in sig["reason"]
    assert "-3.1" in sig["reason"]
    assert uuid.UUID(sig["signal_id"])
    assert state["down:2026-08-05"] == sig["signal_id"]
    assert any("DOWNSIDE" in m for m in discord)


def test_new_signal_tier_symbol_asml_can_fire(tmp_path, market, discord, frozen_clock):
    """ASML.AS is a signal-tier addition — it must be able to auto-protect."""
    market["ASML.AS"] = quote(-3.4)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    sig = signal_of(args)
    assert sig is not None and sig["action"] == "risk_off"
    assert "ASML.AS" in sig["reason"]


def test_below_threshold_does_not_fire(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-2.4)      # threshold is -2.5
    market["TSM"] = quote(-3.4)       # threshold is -3.5
    args = make_args(tmp_path)

    cm.check_once(args, {})

    assert signal_of(args) is None


# ── 2. alert tier never writes a signal ────────────────────────────────

def test_alert_tier_downside_never_writes_signal(tmp_path, market, discord, frozen_clock):
    """NQ=F -2% breaches its own -1.5 threshold, but alert tier = Discord only."""
    market["NQ=F"] = quote(-2.0)
    market["SOXX"] = quote(-0.2)      # signal tier quiet
    market["QQQ"] = quote(-0.1)
    args = make_args(tmp_path)

    state = cm.check_once(args, {})

    assert signal_of(args) is None, "alert-tier symbols must never write signal.json"
    assert "down:2026-08-05" not in state
    assert any("NQ=F" in m for m in discord), "the breach should still be announced"


@pytest.mark.parametrize("symbol,pct", [
    ("^STOXX50E", -2.5), ("^N225", -2.6), ("^KS11", -3.0), ("NQ=F", -4.0)])
def test_every_alert_tier_symbol_is_signal_free(
        tmp_path, market, discord, frozen_clock, symbol, pct):
    market[symbol] = quote(pct)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    assert signal_of(args) is None


def test_alert_tier_dedup_does_not_suppress_later_signal_fire(
        tmp_path, market, discord, frozen_clock):
    """An alert-tier alert must never consume the signal-tier dedup slot."""
    market["NQ=F"] = quote(-2.0)
    args = make_args(tmp_path)
    state = cm.check_once(args, {})
    assert signal_of(args) is None

    market["SOXX"] = quote(-3.0)      # same bucket, later poll
    cm.check_once(args, state)

    sig = signal_of(args)
    assert sig is not None and "SOXX" in sig["reason"]


# ── 3. upside is an alert, never an auto-entry ─────────────────────────

def test_signal_tier_upside_alerts_only(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(3.2)
    args = make_args(tmp_path)

    state = cm.check_once(args, {})

    assert signal_of(args) is None, "entries always need the human tap"
    assert state["up:2026-08-05"] == "alerted"
    assert any("UPSIDE" in m for m in discord)


# ── 4. staleness = session hand-off ────────────────────────────────────

def test_stale_breach_is_ignored(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-5.0, age_sec=allowance("SOXX") + 60)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    assert signal_of(args) is None
    assert discord == []


def test_fresh_side_of_the_boundary_still_fires(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-5.0, age_sec=allowance("SOXX") - 60)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    assert signal_of(args) is not None


def test_delayed_asian_feed_counts_under_its_wider_allowance(
        tmp_path, market, discord, frozen_clock):
    """The 13:48 TPE smoke test: KOSPI was open and -4.07%, but Yahoo's
    quote was 1208 s old.  Under one global 600 s guard the whole move was
    thrown away — these symbols get their own 1500 s allowance."""
    market["^KS11"] = quote(-4.07, age_sec=1208)
    market["^N225"] = quote(-2.4, age_sec=903)
    vote_path = tmp_path / "regime_vote.json"
    args = make_args(tmp_path, vote_out=str(vote_path))

    state = cm.check_once(args, {})

    assert any("^KS11" in m for m in discord), "a delayed-but-live breach must alert"
    assert state.get("alert-down:2026-08-05") == "alerted"
    assert read_json(vote_path)["direction"] == "trending-down", \
        "delayed symbols must still be able to vote"
    assert signal_of(args) is None                   # alert tier stays signal-free


def test_delayed_futures_feed_counts_just_past_ten_minutes(
        tmp_path, market, discord, frozen_clock):
    """Yahoo serves CME futures 10-min delayed, so a LIVE NQ=F quote lands
    just over 600 s old (observed 603 s and 604 s while Globex traded).  A
    600 s allowance rejects the symbol on literally every poll."""
    market["NQ=F"] = quote(-2.0, age_sec=610)
    vote_path = tmp_path / "regime_vote.json"
    args = make_args(tmp_path, vote_out=str(vote_path))

    state = cm.check_once(args, {})

    assert any("NQ=F" in m for m in discord), "a live futures breach must count"
    assert state.get("alert-down:2026-08-05") == "alerted"
    assert signal_of(args) is None                   # alert tier stays signal-free


def test_futures_quote_past_its_allowance_is_stale(tmp_path, market, discord, frozen_clock):
    """900 s covers the 10-min delay plus the 2-min cron cadence — beyond
    that the Globex feed has actually stopped."""
    market["NQ=F"] = quote(-2.0, age_sec=910)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    assert discord == []


def test_us_symbol_keeps_the_tight_allowance(tmp_path, market, discord, frozen_clock):
    """Widening the guard for the delayed feeds must not widen it for the
    real-time US names — a 20-min-old QQQ print is a closed market."""
    market["QQQ"] = quote(-3.0, age_sec=1200)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    assert signal_of(args) is None
    assert discord == []


def test_closed_delayed_market_still_goes_stale(tmp_path, market, discord, frozen_clock):
    """1500 s is an allowance, not an amnesty: once the feed stops, the
    last print ages out and the symbol drops from every decision."""
    market["^KS11"] = quote(-4.07, age_sec=allowance("^KS11") + 60)
    args = make_args(tmp_path, vote_out=str(tmp_path / "regime_vote.json"))

    cm.check_once(args, {})

    assert discord == []
    assert not (tmp_path / "regime_vote.json").exists()


def test_ignore_freshness_accepts_stale_quote(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-5.0, age_sec=6000)
    args = make_args(tmp_path, ignore_freshness=True)

    cm.check_once(args, {})

    assert signal_of(args) is not None


# ── 5. dedup ───────────────────────────────────────────────────────────

def test_second_downside_breach_in_same_bucket_is_deduped(
        tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-3.0)
    args = make_args(tmp_path)

    state = cm.check_once(args, {})
    first_id = signal_of(args)["signal_id"]

    market["SOXX"] = quote(-4.5)      # keeps falling on the next poll
    cm.check_once(args, state)

    assert signal_of(args)["signal_id"] == first_id, "one fire per direction per bucket"


def test_dedup_state_survives_reload_from_disk(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-3.0)
    args = make_args(tmp_path)
    state_path = tmp_path / "monitor_state.json"

    state = cm.check_once(args, {})
    cm.save_state(state_path, state)
    first_id = signal_of(args)["signal_id"]

    cm.check_once(args, cm.load_state(state_path))       # process restart

    assert signal_of(args)["signal_id"] == first_id


def test_upside_dedup_does_not_block_downside(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(3.0)
    args = make_args(tmp_path)
    state = cm.check_once(args, {})
    assert signal_of(args) is None

    market["SOXX"] = quote(-3.0)
    cm.check_once(args, state)

    assert signal_of(args) is not None


# ── 6-8. regime vote ───────────────────────────────────────────────────

def test_vote_fires_on_two_fresh_symbols_agreeing(
        tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(2.8)                      # vote up 2.5
    market["QQQ"] = quote(2.4)                       # vote up 2.0
    market["TSM"] = quote(-6.0, age_sec=9000)        # stale — drops out
    market["^N225"] = quote(0.3)                     # fresh but quiet
    vote_path = tmp_path / "regime_vote.json"
    args = make_args(tmp_path, vote_out=str(vote_path))

    state = cm.check_once(args, {})

    vote = read_json(vote_path)
    assert vote is not None, "2 agreeing fresh symbols must produce a vote"
    assert vote["direction"] == "trending-up"
    assert vote["expires_after_session"] == "2026-08-05|NIGHT"
    assert vote["version"] == 1 and vote["source"] == "W2"
    assert state["vote:2026-08-05"] == "trending-up"


def test_vote_fires_downward_and_mixes_tiers(tmp_path, market, discord, frozen_clock):
    """Alert-tier symbols carry votes even though they never write signals."""
    market["NQ=F"] = quote(-1.8)                     # vote down -1.5
    market["^KS11"] = quote(-2.4)                    # vote down -2.0
    vote_path = tmp_path / "regime_vote.json"
    args = make_args(tmp_path, vote_out=str(vote_path))

    cm.check_once(args, {})

    assert read_json(vote_path)["direction"] == "trending-down"
    assert signal_of(args) is None                   # still no auto-signal


def test_vote_blocked_by_a_fresh_dissenter(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(2.8)
    market["QQQ"] = quote(2.4)
    market["NQ=F"] = quote(-1.9)                     # fresh, breaching the other way
    vote_path = tmp_path / "regime_vote.json"
    args = make_args(tmp_path, vote_out=str(vote_path))

    state = cm.check_once(args, {})

    assert read_json(vote_path) is None, "a fresh dissenter must veto the vote"
    assert "vote:2026-08-05" not in state


def test_vote_needs_two_symbols(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(2.8)                      # the only breach
    market["QQQ"] = quote(0.9)                       # fresh, under threshold
    market["TSM"] = quote(5.0, age_sec=9000)         # stale
    vote_path = tmp_path / "regime_vote.json"
    args = make_args(tmp_path, vote_out=str(vote_path))

    cm.check_once(args, {})

    assert read_json(vote_path) is None


def test_vote_deduped_per_bucket(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(2.8)
    market["QQQ"] = quote(2.4)
    vote_path = tmp_path / "regime_vote.json"
    args = make_args(tmp_path, vote_out=str(vote_path))

    state = cm.check_once(args, {})
    first = read_json(vote_path)

    market["SOXX"] = quote(-3.0)                     # reversal, same bucket
    market["QQQ"] = quote(-2.6)
    cm.check_once(args, state)

    assert read_json(vote_path) == first, "one vote per bucket"


def test_no_vote_file_written_without_vote_out(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(2.8)
    market["QQQ"] = quote(2.4)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    assert not (tmp_path / "regime_vote.json").exists()


def test_night_session_key_uses_open_date():
    assert cm.night_session_key(datetime(2026, 8, 5, 22, 30, tzinfo=TPE)) == "2026-08-05|NIGHT"
    assert cm.night_session_key(datetime(2026, 8, 6, 2, 15, tzinfo=TPE)) == "2026-08-05|NIGHT"


# ── 9. manual tap / CLI ────────────────────────────────────────────────

def test_force_fire_writes_fresh_signal(tmp_path, monkeypatch, discord):
    from src.news.signal_file import read_signal

    out = tmp_path / "signal.json"
    monkeypatch.setattr("sys.argv", [
        "crossmarket_monitor.py", "--signal-out", str(out), "--force-fire", "risk_off"])
    monkeypatch.setattr(cm, "fetch_quote",
                        lambda sym: pytest.fail("--force-fire must not poll quotes"))

    assert cm.main() == 0

    payload = read_json(out)
    assert payload["action"] == "risk_off"
    assert uuid.UUID(payload["signal_id"])
    first_id = payload["signal_id"]

    # the bot must accept what we wrote
    sig, reason = read_signal(str(out), datetime.now(TPE))
    assert sig is not None, reason
    assert sig.action == "risk_off"

    assert cm.main() == 0                            # a second tap = a new decision
    assert read_json(out)["signal_id"] != first_id


def test_min_move_override_lowers_all_tier_thresholds(
        tmp_path, market, discord, frozen_clock):
    market["QQQ"] = quote(-0.6)                      # far from its -2.0 threshold
    args = make_args(tmp_path, min_move=0.5)

    cm.check_once(args, {})

    assert signal_of(args)["action"] == "risk_off"


def test_active_window_covers_day_and_night_sessions():
    def at(hour, minute=0, day=5):
        return cm.in_active_window(datetime(2026, 8, day, hour, minute, tzinfo=TPE))

    assert at(9), "Taiwan day session"
    assert at(14, 30), "Korea/Japan closing prints — must reach the 15:00 night open"
    assert at(14, 58), "last day-cron tick"
    assert at(21), "night session"
    assert at(3, 0, day=6), "night session after midnight"
    assert at(4, 59), "last minute before the idle gap"

    assert not at(5, 30), "idle gap"
    assert not at(6, 30), "idle gap"
    assert not at(7, 59), "last minute before the day window"
    assert at(8, 0), "day window opens"


def test_fetch_failure_is_survivable(tmp_path, market, discord, frozen_clock):
    """Every symbol failing to fetch must not raise or write anything."""
    args = make_args(tmp_path, vote_out=str(tmp_path / "regime_vote.json"))

    state = cm.check_once(args, {})

    assert signal_of(args) is None
    assert not (tmp_path / "regime_vote.json").exists()
    assert [k for k in state if not k.startswith("last_")] == []


# ── liveness log ───────────────────────────────────────────────────────

def log_lines(tmp_path, name="monitor.log"):
    p = Path(tmp_path) / name
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def test_log_line_appended_per_pass(tmp_path, market, discord, frozen_clock):
    market["^N225"] = quote(-0.99)
    market["^KS11"] = quote(-4.81, age_sec=1208)
    args = make_args(tmp_path)

    cm.check_once(args, {})
    lines = log_lines(tmp_path)

    assert len(lines) == 1, "exactly one line per pass"
    line = lines[0]
    assert line.startswith("2026-08-05 22:30:00 TPE | ")
    assert "fresh 2/8" in line
    assert "^N225 -0.99%" in line and "^KS11 -4.81%" in line
    assert "fired: alert-down" in line
    assert "vote: -" in line
    assert "fetch-fail 6" in line              # the other six never answered

    cm.check_once(args, {})
    assert len(log_lines(tmp_path)) == 2, "lines accumulate, they don't overwrite"


def test_log_records_quiet_passes(tmp_path, market, discord, frozen_clock):
    """The whole point: a pass where nothing happened still proves life."""
    market["QQQ"] = quote(-0.2)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    line = log_lines(tmp_path)[0]
    assert "fresh 1/8 (QQQ -0.20%)" in line
    assert "fired: none" in line


def test_log_records_dedup_and_vote(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-3.0)
    market["QQQ"] = quote(-2.6)
    args = make_args(tmp_path, vote_out=str(tmp_path / "regime_vote.json"))

    state = cm.check_once(args, {})
    assert "fired: signal-down |" in log_lines(tmp_path)[0]
    assert "vote: trending-down" in log_lines(tmp_path)[0]

    cm.check_once(args, state)                 # same bucket → both deduped

    second = log_lines(tmp_path)[1]
    assert "fired: signal-down(deduped)" in second
    assert "vote: trending-down(deduped)" in second


def test_log_path_defaults_next_to_signal_file(tmp_path, market, discord, frozen_clock):
    """n8n's deployed command has no --log-file; logging must start anyway."""
    nested = tmp_path / "bridge"
    args = make_args(nested)                   # dir does not exist yet

    cm.check_once(args, {})

    assert (nested / "monitor.log").exists()
    assert cm.log_path_for(args) == nested / "monitor.log"


def test_explicit_log_file_overrides_default(tmp_path, market, discord, frozen_clock):
    custom = tmp_path / "logs" / "w2.log"
    args = make_args(tmp_path, log_file=str(custom))

    cm.check_once(args, {})

    assert len(log_lines(custom.parent, custom.name)) == 1
    assert not (tmp_path / "monitor.log").exists()


def test_log_self_trims_to_the_tail(tmp_path, market, discord, frozen_clock):
    log = tmp_path / "monitor.log"
    log.write_text("".join(f"old line {i} {'x' * 200}\n" for i in range(6000)),
                   encoding="utf-8")
    assert log.stat().st_size > cm.LOG_MAX_BYTES
    args = make_args(tmp_path)

    cm.check_once(args, {})
    lines = log_lines(tmp_path)

    assert len(lines) == cm.LOG_KEEP_LINES, "trim keeps a bounded tail"
    assert lines[-1].endswith("| vote: - | fetch-fail 8"), "newest line survives"
    assert lines[0].startswith("old line 4001"), "oldest lines are the ones dropped"
    assert not (tmp_path / "monitor.log.tmp").exists()


def test_state_carries_last_check_and_last_result(tmp_path, market, discord, frozen_clock):
    market["^KS11"] = quote(-4.81, age_sec=1208)
    args = make_args(tmp_path)
    state_path = tmp_path / "monitor_state.json"

    state = cm.check_once(args, {})
    cm.save_state(state_path, state)

    assert state["last_check"] == "2026-08-05T22:30:00+08:00"
    assert state["last_result"].startswith("fresh 1/8 (^KS11 -4.81%)")
    assert "fired: alert-down" in state["last_result"]
    # the log line is the timestamped form of the very same summary
    assert log_lines(tmp_path)[0].endswith(state["last_result"])
    # and it survives the round-trip the monitor actually does
    assert cm.load_state(state_path)["last_result"] == state["last_result"]


def test_unwritable_log_does_not_break_the_pass(tmp_path, market, discord, frozen_clock):
    """A monitoring pass that completed must never be lost to a log error."""
    market["SOXX"] = quote(-3.0)
    blocked = tmp_path / "monitor.log"
    blocked.mkdir()                            # a directory where a file goes
    args = make_args(tmp_path)

    state = cm.check_once(args, {})

    assert signal_of(args)["action"] == "risk_off", "the signal still got written"
    assert state["last_result"], "state liveness still recorded"


def test_force_fire_logs_the_manual_tap(tmp_path, monkeypatch, discord):
    out = tmp_path / "signal.json"
    monkeypatch.setattr("sys.argv", [
        "crossmarket_monitor.py", "--signal-out", str(out), "--force-fire", "deploy_long"])

    assert cm.main() == 0

    line = log_lines(tmp_path)[0]
    sid = read_json(out)["signal_id"]
    assert "force-fire deploy_long -> " + sid[:8] in line
