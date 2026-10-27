"""Regime store writes survive one monitor read of the target.

On Windows ``os.replace`` fails against ANY open handle on the target,
even the FILE_SHARE_DELETE handle ``fileio.open_shared`` gives the ops
monitor and dashboard. The store's writers must retry like the heartbeat
writer does, not raise on the first sharing violation.
"""

import csv
import json
import os

import pytest

from src.live import main_heartbeat
from src.regime.selector import Recommendation
from src.regime.state_machine import RegimeState
from src.regime.store import append_history, record_session_result, save_state
from src.utils import fileio

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")


def _state():
    s = RegimeState()
    s.raw_regime = "trending-up"
    s.effective_regime = "trending-up"
    s.last_features = {"adx": 30.1, "plus_di": 25.0, "minus_di": 10.0,
                       "atr_ratio": 1.2, "ema_slope": 5.0, "last_close": 22000}
    return s


def _rec():
    return Recommendation(action="deploy_long", strategy_name="LongA",
                          qty_scale=1.0, reason="test")


def _read_once_then_release(monkeypatch, target):
    """Hold a monitor read on ``target``; the first backoff sleep closes it."""
    reader = fileio.open_shared(str(target))
    sleeps = []

    def _sleep(s):
        sleeps.append(s)
        reader.close()

    monkeypatch.setattr(main_heartbeat, "_sleep", _sleep)
    return reader, sleeps


@windows_only
def test_save_state_outlasts_one_read(tmp_path, monkeypatch):
    target = tmp_path / "regime_state.json"
    target.write_text('{"regime": "unknown"}', encoding="utf-8")
    reader, sleeps = _read_once_then_release(monkeypatch, target)
    try:
        save_state(str(target), _state(), _rec(), "2026-10-26")
    finally:
        reader.close()
    assert sleeps
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["next_session"]["strategy"] == "LongA"
    assert not os.path.exists(str(target) + ".tmp")


@windows_only
def test_append_history_outlasts_one_read(tmp_path, monkeypatch):
    csv_path = tmp_path / "regime_history.csv"
    append_history(str(csv_path), "2026-10-23", _state(), _rec())
    reader, sleeps = _read_once_then_release(monkeypatch, csv_path)
    try:
        append_history(str(csv_path), "2026-10-26", _state(), _rec())
    finally:
        reader.close()
    assert sleeps
    with open(csv_path, newline="", encoding="utf-8") as f:
        dates = [r[0] for r in list(csv.reader(f))[1:]]
    assert dates == ["2026-10-23", "2026-10-26"]


@windows_only
def test_record_session_result_outlasts_one_read(tmp_path, monkeypatch):
    csv_path = tmp_path / "regime_history.csv"
    append_history(str(csv_path), "2026-10-26", _state(), _rec())
    reader, sleeps = _read_once_then_release(monkeypatch, csv_path)
    try:
        record_session_result(str(csv_path), "2026-10-26", "NIGHT", 120.0, 2)
    finally:
        reader.close()
    assert sleeps
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[-1]["pnl"] == "120.0"
    assert rows[-1]["trades"] == "2"


@windows_only
def test_save_state_still_raises_when_read_never_ends(tmp_path, monkeypatch):
    """The retry is bounded: a handle held past the budget still raises."""
    target = tmp_path / "regime_state.json"
    target.write_text('{"regime": "unknown"}', encoding="utf-8")
    sleeps = []
    monkeypatch.setattr(main_heartbeat, "_sleep", sleeps.append)
    with fileio.open_shared(str(target)):
        with pytest.raises(PermissionError):
            save_state(str(target), _state(), _rec(), "2026-10-26")
    assert len(sleeps) == main_heartbeat._REPLACE_ATTEMPTS - 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"regime": "unknown"}
