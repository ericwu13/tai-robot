"""Tests for regime store v2: record_session_result, migrate_legacy_history."""

import csv
import os

from src.regime.state_machine import RegimeState
from src.regime.selector import Recommendation
import json

from src.regime.store import (
    append_history,
    record_session_result,
    migrate_legacy_history,
    write_placeholder_state,
    load_state,
    save_state,
    _V2_HEADER,
)


def _make_state(**overrides):
    s = RegimeState()
    s.raw_regime = "trending-up"
    s.effective_regime = "trending-up"
    s.last_features = {"adx": 30.1, "plus_di": 25.0, "minus_di": 10.0,
                       "atr_ratio": 1.2, "ema_slope": 5.0, "last_close": 22000}
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_rec(**overrides):
    defaults = dict(action="deploy_long", strategy_name="LongA",
                    qty_scale=1.0, reason="test")
    defaults.update(overrides)
    return Recommendation(**defaults)


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


class TestWritePlaceholderState:
    def test_writes_when_absent(self, tmp_path):
        path = str(tmp_path / "regime_state.json")
        write_placeholder_state(path)
        assert os.path.exists(path)
        d = json.loads(open(path, encoding="utf-8").read())
        assert d == {"regime": "unknown", "classified_at": None}

    def test_no_clobber_when_present(self, tmp_path):
        path = str(tmp_path / "regime_state.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"last_assessed": "2026-07-09|NIGHT"}, f)
        write_placeholder_state(path)
        d = json.loads(open(path, encoding="utf-8").read())
        assert d == {"last_assessed": "2026-07-09|NIGHT"}


class TestAppendHistoryV2:
    def test_creates_v2_header(self, tmp_path):
        path = str(tmp_path / "history.csv")
        append_history(path, "2026-07-09", _make_state(), _make_rec())
        rows = _read_csv(path)
        assert rows[0] == _V2_HEADER
        assert len(rows) == 2

    def test_v2_extra_columns(self, tmp_path):
        path = str(tmp_path / "history.csv")
        append_history(path, "2026-07-09", _make_state(), _make_rec(),
                       strategy_active="LongA", applied=True,
                       applied_at="2026-07-09 06:00", trading_mode="paper")
        rows = _read_csv(path)
        row = rows[1]
        assert row[_V2_HEADER.index("strategy_active")] == "LongA"
        assert row[_V2_HEADER.index("applied")] == "true"
        assert row[_V2_HEADER.index("applied_at")] == "2026-07-09 06:00"
        assert row[_V2_HEADER.index("trading_mode")] == "paper"

    def test_pnl_and_trades_left_blank_for_classification(self, tmp_path):
        path = str(tmp_path / "history.csv")
        append_history(path, "2026-07-09", _make_state(), _make_rec())
        rows = _read_csv(path)
        assert rows[1][_V2_HEADER.index("pnl")] == ""
        assert rows[1][_V2_HEADER.index("trades")] == ""


class TestRecordSessionResult:
    def test_backfills_existing_classification_row(self, tmp_path):
        path = str(tmp_path / "history.csv")
        append_history(path, "2026-07-09", _make_state(), _make_rec())
        record_session_result(path, "2026-07-09", "NIGHT", 5000.0, 3,
                              strategy_active="LongA", trading_mode="paper")
        rows = _read_csv(path)
        assert len(rows) == 2  # header + 1 row, no new row
        assert rows[1][_V2_HEADER.index("pnl")] == "5000.0"
        assert rows[1][_V2_HEADER.index("trades")] == "3"

    def test_standalone_day_result(self, tmp_path):
        path = str(tmp_path / "history.csv")
        append_history(path, "2026-07-09", _make_state(), _make_rec())
        record_session_result(path, "2026-07-09", "DAY", 1200.0, 1,
                              strategy_active="LongA")
        rows = _read_csv(path)
        assert len(rows) == 3  # header + classification + result
        assert rows[2][0] == "2026-07-09"
        assert rows[2][1] == "DAY"
        assert rows[2][_V2_HEADER.index("pnl")] == "1200.0"

    def test_sit_out_zero_pnl(self, tmp_path):
        path = str(tmp_path / "history.csv")
        append_history(path, "2026-07-09", _make_state(),
                       _make_rec(action="sit_out", strategy_name=""))
        record_session_result(path, "2026-07-09", "NIGHT", 0, 0,
                              strategy_active="idle")
        rows = _read_csv(path)
        assert rows[1][_V2_HEADER.index("pnl")] == "0"
        assert rows[1][_V2_HEADER.index("trades")] == "0"

    def test_creates_file_if_missing(self, tmp_path):
        path = str(tmp_path / "history.csv")
        record_session_result(path, "2026-07-09", "DAY", 800.0, 2)
        rows = _read_csv(path)
        assert rows[0] == _V2_HEADER
        assert len(rows) == 2

    def test_re_record_updates_in_place(self, tmp_path):
        path = str(tmp_path / "history.csv")
        append_history(path, "2026-07-09", _make_state(), _make_rec())
        record_session_result(path, "2026-07-09", "NIGHT", 5000.0, 3)
        record_session_result(path, "2026-07-09", "NIGHT", 9999.0, 9)
        rows = _read_csv(path)
        # Re-recording the same session UPDATES the row (the caller
        # recomputes the full-session total each time, so latest wins) —
        # appending would double-count the session on stop()/restart.
        assert rows[1][_V2_HEADER.index("pnl")] == "9999.0"
        assert rows[1][_V2_HEADER.index("trades")] == "9"
        assert len(rows) == 2  # header + the single session row


class TestKeyFormatMigration:
    """Pre-v2.16 state files stamped last_assessed with the night's CLOSE
    date; load_state must translate it once to the OPEN-date key so the
    upgrade neither skips the next night (key collision) nor re-assesses
    an already-assessed one (cleared key double-stepping hysteresis)."""

    def _write(self, path, d):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)

    def test_old_close_date_key_translated(self, tmp_path):
        # 2026-07-16 is a Thursday; the night that closed 07-16 05:00
        # opened Wednesday 07-15.
        path = str(tmp_path / "regime_state.json")
        self._write(path, {"last_assessed": "2026-07-16|NIGHT"})
        s = load_state(path)
        assert s.last_assessed == "2026-07-15|NIGHT"

    def test_old_weekend_phantom_key_translated(self, tmp_path):
        # An old-code Sunday phantom stamp resolves to the Friday-open
        # night — the same night Saturday's legitimate stamp covered.
        path = str(tmp_path / "regime_state.json")
        self._write(path, {"last_assessed": "2026-07-12|NIGHT"})
        s = load_state(path)
        assert s.last_assessed == "2026-07-10|NIGHT"

    def test_new_format_untouched(self, tmp_path):
        path = str(tmp_path / "regime_state.json")
        self._write(path, {"last_assessed": "2026-07-15|NIGHT",
                           "key_format": "open-date"})
        s = load_state(path)
        assert s.last_assessed == "2026-07-15|NIGHT"

    def test_empty_key_untouched(self, tmp_path):
        path = str(tmp_path / "regime_state.json")
        self._write(path, {"last_assessed": ""})
        s = load_state(path)
        assert s.last_assessed == ""

    def test_garbage_key_cleared(self, tmp_path):
        path = str(tmp_path / "regime_state.json")
        self._write(path, {"last_assessed": "not-a-date|NIGHT"})
        s = load_state(path)
        assert s.last_assessed == ""

    def test_save_stamps_marker_and_round_trips(self, tmp_path):
        path = str(tmp_path / "regime_state.json")
        state = _make_state(last_assessed="2026-07-15|NIGHT")
        save_state(path, state, _make_rec(), "2026-07-15")
        d = json.loads(open(path, encoding="utf-8").read())
        assert d["key_format"] == "open-date"
        # A reload must NOT re-translate a key saved by current code.
        s = load_state(path)
        assert s.last_assessed == "2026-07-15|NIGHT"


class TestMigrateLegacyHistory:
    def test_renames_v1_file(self, tmp_path):
        path = str(tmp_path / "regime_history.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "session", "adx", "plus_di", "minus_di", "atr_ratio",
                         "ema_slope", "close", "raw_regime", "effective_regime",
                         "confirm_count", "decision", "strategy_deployed",
                         "dry_run", "override", "pnl", "trades"])
            w.writerow(["2026-07-08", "NIGHT", "28.5", "25", "12", "1.1",
                         "3.0", "22000", "trending-up", "trending-up",
                         "2", "deploy_long", "LongA", "True", "auto", "", ""])
        migrate_legacy_history(path)
        assert not os.path.exists(path)
        legacy = str(tmp_path / "regime_history_legacy.csv")
        assert os.path.exists(legacy)

    def test_leaves_v2_in_place(self, tmp_path):
        path = str(tmp_path / "regime_history.csv")
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(_V2_HEADER)
        migrate_legacy_history(path)
        assert os.path.exists(path)  # not renamed

    def test_noop_if_missing(self, tmp_path):
        path = str(tmp_path / "regime_history.csv")
        migrate_legacy_history(path)  # no crash


class TestBackfillPnlRemoved:
    def test_backfill_pnl_not_importable(self):
        """backfill_pnl was retired — ensure it's not exported."""
        import src.regime.store as store_mod
        assert not hasattr(store_mod, "backfill_pnl")
