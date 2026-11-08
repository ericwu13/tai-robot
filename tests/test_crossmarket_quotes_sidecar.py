"""W2 ``crossmarket_quotes.json`` sidecar — the numbers behind the dashboard
ticker strip.

Before this file existed W2 persisted no numbers at all: per-symbol % only
lived as free text in ``monitor_state.json`` ``last_result``.  The sidecar
is display-only and deliberately SEPARATE from ``monitor_state.json``: a
failed save there loses the dedup keys and re-fires ``risk_off``, so the
sidecar write must never raise and never touch the dedup state.
"""

import json
from pathlib import Path

import pytest

from tests.test_crossmarket_monitor import (  # noqa: F401 — fixtures
    cm, discord, frozen_clock, make_args, market, quote)


def sidecar(args):
    p = Path(args.signal_out).parent / "crossmarket_quotes.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def test_every_pass_writes_every_symbol(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-1.2)
    market["TSM"] = quote(0.8)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    data = sidecar(args)
    assert data is not None, "W2 must write crossmarket_quotes.json every pass"
    assert data["version"] == 1
    assert data["asof"].startswith("2026-08-05T22:30:00")
    # Every configured symbol is present — fetch failures included — so the
    # strip can show a row for each and grey the dead ones.
    assert set(data["quotes"]) == set(cm.SYMBOLS)
    soxx = data["quotes"]["SOXX"]
    assert soxx["tier"] == "signal"
    assert soxx["pct"] == pytest.approx(-1.2)
    assert soxx["prev_close"] == pytest.approx(100.0)
    assert soxx["price"] == pytest.approx(98.8)
    assert soxx["fresh"] is True and soxx["stale_reason"] is None
    assert (soxx["down"], soxx["up"]) == (-2.5, 2.5)
    assert (soxx["vote_down"], soxx["vote_up"]) == (-2.5, 2.5)
    assert 0 <= soxx["age_s"] < 60
    assert isinstance(soxx["quote_time"], int)
    assert data["quotes"]["TSM"]["vote_down"] == -2.0
    # Unlisted symbols fetched as None.
    assert sorted(data["fetch_failed"]) == sorted(set(cm.SYMBOLS) - {"SOXX", "TSM"})
    qqq = data["quotes"]["QQQ"]
    assert qqq["fresh"] is False and qqq["price"] is None
    assert qqq["stale_reason"] == "fetch_failed"


def test_stale_quote_is_kept_and_flagged(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-4.0, age_sec=cm.SYMBOLS["SOXX"]["max_age"] + 120)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    soxx = sidecar(args)["quotes"]["SOXX"]
    assert soxx["fresh"] is False
    assert soxx["pct"] == pytest.approx(-4.0)      # still shown, greyed
    assert "allowance" in soxx["stale_reason"]


def test_quorum_and_fired_are_recorded(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-3.1)
    market["QQQ"] = quote(-2.4)
    args = make_args(tmp_path)

    cm.check_once(args, {})

    data = sidecar(args)
    assert data["quorum"]["direction"] == "trending-down"
    assert sorted(data["quorum"]["down"]) == ["QQQ", "SOXX"]
    assert data["quorum"]["up"] == []
    assert data["quorum"]["min_symbols"] == cm.VOTE_MIN_SYMBOLS
    assert "signal-down" in data["fired"]
    assert data["last_fired"]["what"] == ["signal-down"]
    assert data["last_fired"]["at"].startswith("2026-08-05T22:30:00")


def test_last_fired_survives_deduped_and_quiet_passes(tmp_path, market, discord,
                                                       frozen_clock):
    market["SOXX"] = quote(-3.1)
    market["QQQ"] = quote(-2.4)
    args = make_args(tmp_path)
    state = cm.check_once(args, {})
    first = sidecar(args)["last_fired"]

    state = cm.check_once(args, state)            # same breach → deduped
    assert sidecar(args)["fired"] == ["signal-down(deduped)"]
    assert sidecar(args)["last_fired"] == first

    market.clear()                                 # quiet pass
    cm.check_once(args, state)
    data = sidecar(args)
    assert data["fired"] == []
    assert data["last_fired"] == first


def test_min_move_override_shows_effective_thresholds(tmp_path, market, discord,
                                                      frozen_clock):
    market["SOXX"] = quote(-0.6)
    args = make_args(tmp_path, min_move=0.5)
    cm.check_once(args, {})
    soxx = sidecar(args)["quotes"]["SOXX"]
    assert (soxx["down"], soxx["up"]) == (-0.5, 0.5)
    assert (soxx["vote_down"], soxx["vote_up"]) == (-0.5, 0.5)


def test_replace_retries_a_transient_permission_error(tmp_path, market, discord,
                                                      frozen_clock, monkeypatch):
    """A reader holding the file open makes os.replace raise PermissionError
    on Windows for a moment; one short retry must land the write."""
    market["SOXX"] = quote(-1.0)
    args = make_args(tmp_path)
    real_replace = cm.os.replace
    calls = []

    def flaky(src, dst):
        if str(dst).endswith("crossmarket_quotes.json") and not calls:
            calls.append(dst)
            raise PermissionError(13, "in use")
        return real_replace(src, dst)

    monkeypatch.setattr(cm.os, "replace", flaky)
    monkeypatch.setattr(cm.time, "sleep", lambda s: None)
    cm.check_once(args, {})
    assert calls, "the first replace must have been attempted"
    assert sidecar(args)["quotes"]["SOXX"]["pct"] == pytest.approx(-1.0)


def test_sidecar_failure_never_breaks_the_pass(tmp_path, market, discord,
                                               frozen_clock, monkeypatch):
    """The sidecar is display-only: a write that keeps failing must not raise,
    must not cost the dedup state, and must not block risk_off."""
    market["SOXX"] = quote(-3.1)
    market["QQQ"] = quote(-2.4)
    args = make_args(tmp_path)
    real_replace = cm.os.replace

    def broken(src, dst):
        if str(dst).endswith("crossmarket_quotes.json"):
            raise PermissionError(13, "in use")
        return real_replace(src, dst)

    monkeypatch.setattr(cm.os, "replace", broken)
    monkeypatch.setattr(cm.time, "sleep", lambda s: None)
    state = cm.check_once(args, {})

    assert sidecar(args) is None
    assert state["down:2026-08-05"], "risk_off dedup key must still be set"
    assert json.loads(Path(args.signal_out).read_text(encoding="utf-8"))["action"] == "risk_off"
    assert "crossmarket_quotes" not in json.dumps(state)


def test_sidecar_is_not_inside_monitor_state(tmp_path, market, discord, frozen_clock):
    market["SOXX"] = quote(-1.0)
    args = make_args(tmp_path)
    state = cm.check_once(args, {})
    assert "quotes" not in state and "SOXX" not in json.dumps(
        {k: v for k, v in state.items() if k != "last_result"})


def test_unwritable_directory_does_not_raise(tmp_path, market, discord, frozen_clock,
                                             monkeypatch):
    market["SOXX"] = quote(-1.0)
    args = make_args(tmp_path)

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(cm, "_dump_json_atomic", boom)
    cm.check_once(args, {})                         # must not raise
    assert sidecar(args) is None
