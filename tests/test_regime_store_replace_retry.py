"""Regime store writes survive one monitor read of the target.

On Windows ``os.replace`` fails against ANY open handle on the target,
even the FILE_SHARE_DELETE handle ``fileio.open_shared`` gives the ops
monitor and dashboard. The store's writers, and the two ``RegimeManager``
writers of ``regime_state.json`` that go through ``store._replace``, must
retry like the heartbeat writer does, not raise on the first sharing
violation. Any other error still raises on the first attempt.
"""

import csv
import json
import os

import pytest

from src.live import main_heartbeat
from src.regime.manager import RegimeManager
from src.regime.selector import Recommendation
from src.regime.state_machine import RegimeConfig, RegimeState
from src.regime.store import (
    append_history, record_session_result, save_state, write_placeholder_state,
)
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


def _permission_error_once(monkeypatch):
    """First ``os.replace`` raises ``PermissionError``; the next one is real.

    Patches the heartbeat helper's ``os.replace`` (the same object
    ``store._replace`` calls) and its backoff sleep, so a writer that
    still uses a bare ``os.replace`` records one failed call and no sleep.
    """
    calls = []
    sleeps = []
    real = main_heartbeat.os.replace

    def wrapped(src, dst):
        calls.append((src, dst))
        if len(calls) == 1:
            raise PermissionError("busy")
        return real(src, dst)

    monkeypatch.setattr(main_heartbeat.os, "replace", wrapped)
    monkeypatch.setattr(main_heartbeat, "_sleep", sleeps.append)
    return calls, sleeps


def _pending_state():
    return {
        "effective_regime": "trending-up",
        "last_features": {"adx": 30.1},
        "next_session": {
            "date": "2026-10-26",
            "action": "deploy_long",
            "strategy": "LongA",
            "qty_scale": 1.0,
            "reason": "test",
            "executed": False,
            "executed_at": None,
        },
    }


def _manager_with_pending(tmp_path):
    bot_dir = tmp_path / "TX00_retry"
    bot_dir.mkdir()
    path = bot_dir / "regime_state.json"
    path.write_text(json.dumps(_pending_state()), encoding="utf-8")
    mgr = RegimeManager(str(bot_dir), RegimeConfig())
    return mgr, path


def test_write_placeholder_state_retries_permission_error(tmp_path, monkeypatch):
    """Deploy-time placeholder goes through the same retrying replace."""
    path = tmp_path / "regime_state.json"
    calls, sleeps = _permission_error_once(monkeypatch)
    write_placeholder_state(str(path))
    assert len(calls) == 2
    assert sleeps == [main_heartbeat._REPLACE_BACKOFF_S[0]]
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "regime": "unknown", "classified_at": None,
    }
    assert not os.path.exists(str(path) + ".tmp")


def test_non_permission_error_raises_immediately(tmp_path, monkeypatch):
    """Anything other than PermissionError fails on the first attempt."""
    target = tmp_path / "regime_state.json"
    target.write_text('{"regime": "unknown"}', encoding="utf-8")
    calls = []
    sleeps = []

    def boom(src, dst):
        calls.append((src, dst))
        raise OSError("disk full")

    monkeypatch.setattr(main_heartbeat.os, "replace", boom)
    monkeypatch.setattr(main_heartbeat, "_sleep", sleeps.append)
    with pytest.raises(OSError, match="disk full"):
        save_state(str(target), _state(), _rec(), "2026-10-26")
    assert len(calls) == 1
    assert sleeps == []
    assert json.loads(target.read_text(encoding="utf-8")) == {"regime": "unknown"}


def test_mark_recommendation_executed_retries_permission_error(tmp_path, monkeypatch):
    """A transient sharing violation must not leave executed=False."""
    mgr, path = _manager_with_pending(tmp_path)
    calls, sleeps = _permission_error_once(monkeypatch)
    mgr.mark_recommendation_executed("2026-10-27 05:00")
    assert len(calls) == 2
    assert sleeps == [main_heartbeat._REPLACE_BACKOFF_S[0]]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["next_session"]["executed"] is True
    assert data["next_session"]["executed_at"] == "2026-10-27 05:00"
    assert data["next_session"]["strategy"] == "LongA"
    assert not os.path.exists(str(path) + ".tmp")


def test_patch_state_features_retries_permission_error(tmp_path, monkeypatch):
    """last_features is patched through the same retrying replace."""
    mgr, path = _manager_with_pending(tmp_path)
    mgr._state.last_features = {"adx": 30.1, "_event_risk": "CPI"}
    calls, sleeps = _permission_error_once(monkeypatch)
    mgr._patch_state_features()
    assert len(calls) == 2
    assert sleeps == [main_heartbeat._REPLACE_BACKOFF_S[0]]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["last_features"]["_event_risk"] == "CPI"
    assert data["next_session"]["executed"] is False
    assert data["next_session"]["strategy"] == "LongA"
    assert not os.path.exists(str(path) + ".tmp")


@windows_only
def test_mark_recommendation_executed_outlasts_one_read(tmp_path, monkeypatch):
    mgr, path = _manager_with_pending(tmp_path)
    reader, sleeps = _read_once_then_release(monkeypatch, path)
    try:
        mgr.mark_recommendation_executed("2026-10-27 05:00")
    finally:
        reader.close()
    assert sleeps == [main_heartbeat._REPLACE_BACKOFF_S[0]]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["next_session"]["executed"] is True
    assert data["next_session"]["executed_at"] == "2026-10-27 05:00"


@windows_only
def test_patch_state_features_outlasts_one_read(tmp_path, monkeypatch):
    mgr, path = _manager_with_pending(tmp_path)
    mgr._state.last_features = {"adx": 30.1, "_event_risk": "CPI"}
    reader, sleeps = _read_once_then_release(monkeypatch, path)
    try:
        mgr._patch_state_features()
    finally:
        reader.close()
    assert sleeps == [main_heartbeat._REPLACE_BACKOFF_S[0]]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["last_features"]["_event_risk"] == "CPI"
    assert data["next_session"]["executed"] is False
