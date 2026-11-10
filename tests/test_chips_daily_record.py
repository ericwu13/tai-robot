"""W4 ``.chips_state.json`` ``daily`` key — the numbers behind the dashboard
籌碼 panel.

Before this, ΔOI / equity flow / decision / reason lived only in free-text
strings, and ``last_result`` is overwritten by every dedup pass (four of
five slots a day), so by the evening nothing on disk said what W4 decided.
``daily`` is written only on DECISION passes, keyed by trade date, and
never touches ``history`` (the ΔOI baseline the vote depends on).
"""

from tests.test_chips_monitor import (  # noqa: F401 — fixture + helpers
    RUN_NOW, _patch_fetch, _seed_history, base, chips)


def state_of(base):
    return chips.load_state(chips.state_path_for(base))


def test_decision_pass_records_daily_row(monkeypatch, base):
    _seed_history(base, {"20260813": -80_000.0})
    _patch_fetch(monkeypatch, -72_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)

    row = state_of(base)["daily"]["20260814"]
    assert row["net_oi"] == -72_000.0
    assert row["delta_oi"] == 8_000.0
    assert row["prev_date"] == "20260813"
    assert row["equity_net"] == 6e9
    assert row["decision"] == "trending-up"
    assert row["reason"].startswith("strong")
    assert row["final"] is True
    assert row["session"] == "2026-08-14|NIGHT"
    assert row["at"].startswith("2026-08-14T16:15")


def test_no_vote_decision_is_recorded_too(monkeypatch, base):
    _seed_history(base, {"20260813": -80_000.0})
    _patch_fetch(monkeypatch, -79_500.0, 0.0)
    chips.run_once(base, "20260814", RUN_NOW)
    row = state_of(base)["daily"]["20260814"]
    assert row["decision"] is None
    assert "hysteresis" in row["reason"]
    assert row["delta_oi"] == 500.0


def test_first_pass_without_baseline_records_level_only(monkeypatch, base):
    _patch_fetch(monkeypatch, -80_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    row = state_of(base)["daily"]["20260814"]
    assert row["net_oi"] == -80_000.0
    assert row["delta_oi"] is None and row["prev_date"] is None
    assert row["equity_net"] is None
    assert row["decision"] is None


def test_twse_outage_row_is_not_final_and_is_overwritten(monkeypatch, base):
    _seed_history(base, {"20260813": 0.0})
    _patch_fetch(monkeypatch, 8_000.0, None)
    chips.run_once(base, "20260814", RUN_NOW)
    assert state_of(base)["daily"]["20260814"]["final"] is False

    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    row = state_of(base)["daily"]["20260814"]
    assert row["final"] is True and row["decision"] == "trending-up"


def test_dedup_pass_leaves_daily_alone(monkeypatch, base):
    """The later slots overwrite last_result with 'nothing new' — the
    decision itself must survive them."""
    _seed_history(base, {"20260813": 0.0})
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    before = state_of(base)["daily"]

    monkeypatch.setattr(chips, "fetch_taifex_tx_foreign_oi",
                        lambda: ("20260814", 8_000.0))
    chips.run_once(base, None, RUN_NOW)          # scheduled, same dataset
    st = state_of(base)
    assert st["last_result"].startswith("nothing new")
    assert st["daily"] == before


def test_no_data_and_dry_run_write_no_daily(monkeypatch, base):
    _seed_history(base, {"20260813": 0.0})
    _patch_fetch(monkeypatch, None, None)
    chips.run_once(base, "20260814", RUN_NOW)
    assert "daily" not in state_of(base)

    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW, dry_run=True)
    assert "daily" not in state_of(base)


def test_history_is_untouched_by_daily(monkeypatch, base):
    _seed_history(base, {"20260813": 0.0})
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    assert state_of(base)["history"] == {"20260813": 0.0, "20260814": 8_000.0}


def test_daily_keeps_sixty_dates(monkeypatch, base):
    state = {"history": {"20260813": 0.0},
             "daily": {f"2026{m:02d}{d:02d}": {"net_oi": 0} for m in (5, 6, 7)
                       for d in range(1, 29)}}
    chips.save_state(chips.state_path_for(base), state)
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    chips.run_once(base, "20260814", RUN_NOW)
    daily = state_of(base)["daily"]
    assert len(daily) == chips.DAILY_KEEP_DAYS == 60
    assert max(daily) == "20260814"
    assert "20260501" not in daily


def test_corrupt_daily_is_replaced_not_fatal(monkeypatch, base):
    chips.save_state(chips.state_path_for(base),
                     {"history": {"20260813": 0.0}, "daily": ["junk"]})
    _patch_fetch(monkeypatch, 8_000.0, 6e9)
    assert chips.run_once(base, "20260814", RUN_NOW) == 0
    assert list(state_of(base)["daily"]) == ["20260814"]
