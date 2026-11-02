"""The ops monitor's reads must not break a live bot's own writes.

On Windows a plain ``open()`` handle does not share DELETE access, so
while the monitor holds one the bot's ``save_session`` (``os.replace``,
then its remove-then-rename fallback) raises and a clean stop's
``os.remove(.lock)`` fails. The monitor reads through
``src.utils.fileio`` (FILE_SHARE_DELETE) instead.

``_bot_write_during_read`` runs the bot's real write at the moment the
monitor has its handle open, whichever opener the monitor used, and
records whether the write raised.
"""

import builtins
import json
import os

import pytest

import scripts.monitor.check_bots as cb
import scripts.monitor.common as common
from scripts.monitor.check_bots import check_bots
from scripts.monitor.check_evolution import check_evolution
from scripts.monitor.check_regime import check_regime
from src.live.session_store import load_session, save_session
from src.utils import fileio
from tests.test_monitor_checks import (
    HEALTHY_STATE, NOW, NOW_IN_SESSION, make_live_bot, make_regime_bot,
    write_heartbeat,
)
from tests.test_monitor_evolution import (
    NOW as EVO_NOW, make_bot, start_line, write_debug_log, write_usage,
)

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")

OLD = {"strategy": "S", "broker": {"position_size": 0}, "saved_at": "old"}
NEW = {"strategy": "S", "broker": {"position_size": 1}, "saved_at": "new"}


def _bot_write_during_read(monkeypatch, target, write):
    """Fire ``write`` once, while the first read handle on ``target`` is open.

    Returns the outcome list: ``[None]`` when the write landed, ``[exc]``
    when it raised. Empty means the reader never opened ``target``.
    """
    target = os.path.abspath(str(target))
    outcome = []
    real_open = builtins.open
    real_shared = fileio.open_shared

    def fire(path):
        try:
            hit = os.path.abspath(os.fspath(path)) == target
        except TypeError:
            return
        if not hit or outcome:
            return
        try:
            write()
        except OSError as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    def plain(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        if isinstance(mode, str) and not any(ch in mode for ch in "wax+"):
            fire(file)
        return handle

    def shared(path):
        handle = real_shared(path)
        fire(path)
        return handle

    monkeypatch.setattr(builtins, "open", plain)
    monkeypatch.setattr(fileio, "open_shared", shared)
    return outcome


@windows_only
def test_plain_open_reader_makes_save_session_raise(tmp_path, monkeypatch):
    """The hazard, through the same hook (fail-first baseline)."""
    target = tmp_path / "session.json"
    save_session(str(target), OLD)
    outcome = _bot_write_during_read(
        monkeypatch, target, lambda: save_session(str(target), NEW))
    with open(target, encoding="utf-8") as f:
        assert json.load(f) == OLD
    assert len(outcome) == 1 and isinstance(outcome[0], PermissionError)


@windows_only
def test_save_session_succeeds_while_monitor_loads_session(tmp_path, monkeypatch):
    """check_bots' session source (HUNG grading, frozen-log grade)."""
    target = tmp_path / "session.json"
    save_session(str(target), OLD)
    outcome = _bot_write_during_read(
        monkeypatch, target, lambda: save_session(str(target), NEW))
    assert cb._load_session_data(str(target)) == OLD   # reader keeps old data
    assert outcome == [None]
    assert load_session(str(target)) == NEW


@windows_only
def test_save_session_succeeds_while_monitor_reads_json(tmp_path, monkeypatch):
    """common.read_json: session.json for check_regime/check_evolution/daily_review."""
    target = tmp_path / "session.json"
    save_session(str(target), OLD)
    outcome = _bot_write_during_read(
        monkeypatch, target, lambda: save_session(str(target), NEW))
    assert common.read_json(str(target)) == OLD
    assert outcome == [None]
    assert load_session(str(target)) == NEW


@windows_only
def test_lock_removal_succeeds_while_monitor_reads_lock(tmp_path, monkeypatch):
    lock = tmp_path / ".lock"
    lock.write_text("4242", encoding="utf-8")
    outcome = _bot_write_during_read(monkeypatch, lock, lambda: os.remove(lock))
    assert common.read_lock_pid(str(tmp_path)) == (True, 4242)
    assert outcome == [None]
    assert not lock.exists()


@windows_only
def test_heartbeat_removal_succeeds_while_monitor_reads_heartbeat(tmp_path, monkeypatch):
    """The bot deletes heartbeat.json on a clean stop."""
    target = tmp_path / "heartbeat.json"
    target.write_text('{"ts": 1, "pid": 4242}', encoding="utf-8")
    outcome = _bot_write_during_read(monkeypatch, target, lambda: os.remove(target))
    assert cb._read_heartbeat_json(str(target)) == ({"ts": 1, "pid": 4242}, None)
    assert outcome == [None]
    assert not target.exists()


# ── routing: no monitor check opens a bot file with plain open() ───────

def _forbid_plain_open_under(monkeypatch, root):
    """Plain open() of anything under ``root`` fails the test; shared
    opens are recorded by basename so the check is not vacuous."""
    root = os.path.abspath(str(root))
    real_open = builtins.open
    real_shared = fileio.open_shared
    opened = set()

    def plain(file, mode="r", *args, **kwargs):
        try:
            path = os.path.abspath(os.fspath(file))
        except TypeError:
            path = ""
        if path.startswith(root):
            raise AssertionError(f"plain open() of a bot file: {path}")
        return real_open(file, mode, *args, **kwargs)

    def shared(path):
        opened.add(os.path.basename(os.fspath(path)))
        # The non-Windows fallback inside open_shared is itself a builtin
        # open(); let it through so the guard only trips on direct opens.
        builtins.open = real_open
        try:
            return real_shared(path)
        finally:
            builtins.open = plain

    monkeypatch.setattr(builtins, "open", plain)
    monkeypatch.setattr(fileio, "open_shared", shared)
    return opened


def test_check_bots_reads_bot_files_only_through_shared_opener(tmp_path, monkeypatch):
    bot = make_live_bot(tmp_path, pid=4242, log_time="15:55:00")
    write_heartbeat(bot, NOW_IN_SESSION, age_s=400)
    (bot / "decisions.csv").write_text(
        "2026-08-25 15:00:00,2026-08-25 15:00,x,REAL_ORDER_TIMEOUT,\n",
        encoding="utf-8")
    opened = _forbid_plain_open_under(monkeypatch, tmp_path)
    check_bots(NOW_IN_SESSION, str(tmp_path / "live"),
               pid_alive_fn=lambda pid: True)
    assert {".lock", "session.json", "heartbeat.json", "debug_20260825.log",
            "decisions.csv"} <= opened, opened


def test_check_regime_reads_bot_files_only_through_shared_opener(tmp_path, monkeypatch):
    bot = make_regime_bot(tmp_path, HEALTHY_STATE)
    (bot / "regime_history.csv").write_text(
        "date,session,pnl\n2026-08-24,NIGHT,1250.0\n", encoding="utf-8")
    (bot / "decisions.csv").write_text(
        "2026-08-25 05:00:00,2026-08-25 05:00,x,NEWS_SUPPRESSED,\n",
        encoding="utf-8")
    opened = _forbid_plain_open_under(monkeypatch, tmp_path)
    check_regime(NOW, str(tmp_path / "live"))
    assert {"regime_state.json", "session.json", "regime_history.csv",
            "decisions.csv"} <= opened, opened


def test_check_evolution_reads_usage_and_debug_log_only_through_shared_opener(
        tmp_path, monkeypatch):
    """ai_usage.csv and the debug-log scan go through the shared opener.

    Plain ``open()`` is forbidden anywhere under the temp tree, which
    holds the bot directory and the usage ledger. The opened-set check
    is what keeps the guard from passing without reading those files.
    """
    bot = make_bot(tmp_path, name="TMF00_bot")
    write_debug_log(bot, "20260815", [start_line("2026-08-29 05:05:25")])
    write_usage(tmp_path, [("2026-08-28T21:05:49+00:00", "bot_evolution")])
    opened = _forbid_plain_open_under(monkeypatch, tmp_path)
    check_evolution(EVO_NOW, str(tmp_path / "live"), str(tmp_path),
                    pid_alive_fn=lambda pid: True)
    assert {"ai_usage.csv", "debug_20260815.log"} <= opened, opened


def test_read_json_invalid_utf8_matches_master(tmp_path):
    """Master opens with strict UTF-8 and returns None.

    ``UnicodeDecodeError`` is a ``ValueError``, so the failure is not
    raised. The bytes are valid JSON once a lenient decode replaces or
    drops the bad byte, which is what a ``errors="replace"`` mutant
    would return instead of None.
    """
    path = tmp_path / "session.json"
    path.write_bytes(b'{"k":"\xff"}')
    assert common.read_json(str(path)) is None


def test_read_lock_pid_unreadable_lock_is_present_without_a_pid(
        tmp_path, monkeypatch):
    """An unreadable lock still exists: ``(True, None)``, never ``(False, None)``.

    Invalid bytes decode leniently and then fail ``int()``. An open that
    raises is swallowed by ``read_text`` and takes the same branch.
    """
    bot = tmp_path / "bot"
    bot.mkdir()
    lock = bot / ".lock"
    lock.write_bytes(b"\xff\xfe")
    assert common.read_lock_pid(str(bot)) == (True, None)

    lock.write_text("4242", encoding="utf-8")

    def raising(path):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(fileio, "open_shared", raising)
    assert common.read_lock_pid(str(bot)) == (True, None)
