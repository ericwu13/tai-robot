"""Issue #233: RECOVERED hang alert, host-stall rollup, holiday cache.

A heartbeat that stalls past 90 s and then advances sends one HUNG and
one RECOVERED, with stall_s measured from the first sight of the frozen
timestamp. Three bots HUNG inside 120 s send one rollup; two do not.
The rollup is additive: a real bot's own P1 is always sent. The TW
holiday set is built once per year, and the closures file is stat'd at
most once per 60 s.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import src.live.main_heartbeat as hb
import src.live.host_stall as host_stall
from src.live.main_heartbeat import HangWatchdog, HeartbeatWriter


def _p1(alerts: list[str]) -> list[str]:
    return [item for item in alerts if "P1 HUNG" in item]


def _recovered(alerts: list[str]) -> list[str]:
    return [item for item in alerts if "RECOVERED after" in item]


def _stall_rollups(alerts: list[str]) -> list[str]:
    return [item for item in alerts if "bots stalled" in item]


def _recovery_rollups(alerts: list[str]) -> list[str]:
    return [item for item in alerts if "recovered, max stall" in item]


def _latest_claim(root: Path) -> dict:
    numbered = []
    for path in root.glob("host_stall.claim.*"):
        suffix = path.name.rsplit(".", 1)[-1]
        if suffix.isdigit():
            numbered.append((int(suffix), path))
    assert numbered, "no claim generation was published"
    path = max(numbered)[1]
    return json.loads(path.read_text(encoding="utf-8"))


def _boot(root: Path, name: str, mode: str, clock: dict, hung_at: float,
          alerts: list[str]) -> tuple[HangWatchdog, HeartbeatWriter]:
    bot = root / name
    bot.mkdir(parents=True)
    clock["t"] = hung_at
    writer = HeartbeatWriter(str(bot))
    writer.link_state = {
        "mode": mode,
        "entries_blocked": False,
        "position_open": False,
    }
    writer.write()
    watchdog = HangWatchdog(str(bot), hung_s=90.0, alert_fn=alerts.append)
    watchdog._check_once(0.0)
    watchdog._check_once(100.0)
    return watchdog, writer


def _recover(writer: HeartbeatWriter, watchdog: HangWatchdog, clock: dict,
             when: float, mono: float) -> None:
    clock["t"] = when
    writer.write()
    watchdog._check_once(mono)


def test_stall_then_advance_sends_one_hung_and_one_recovered(
        tmp_path, monkeypatch, caplog):
    """Past 90 s, then a new timestamp: one HUNG, one RECOVERED, exact stall_s.

    A second poll after recovery does not alert again. A bot that never
    crosses the threshold sends nothing. A later episode gets its own pair.
    """
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(hb.time, "time", lambda: clock["t"])
    alerts: list[str] = []
    root = tmp_path / "live"
    bot = root / "TXF_One"
    bot.mkdir(parents=True)
    writer = HeartbeatWriter(str(bot))
    writer.write()
    watchdog = HangWatchdog(str(bot), hung_s=90.0, alert_fn=alerts.append)

    quiet: list[str] = []
    quiet_bot = root / "TXF_Quiet"
    quiet_bot.mkdir()
    quiet_writer = HeartbeatWriter(str(quiet_bot))
    quiet_writer.write()
    quiet_wd = HangWatchdog(str(quiet_bot), hung_s=90.0, alert_fn=quiet.append)
    quiet_wd._check_once(0.0)
    quiet_wd._check_once(10.0)
    clock["t"] = 1_700_000_001.0
    quiet_writer.write()
    quiet_wd._check_once(11.0)
    assert quiet == []
    assert not (quiet_bot / "hang.json").exists()

    clock["t"] = 1_700_000_000.0
    writer.write()
    watchdog._check_once(0.0)
    watchdog._check_once(90.0)
    assert alerts == []
    with caplog.at_level(logging.WARNING, logger="src.live.main_heartbeat"):
        watchdog._check_once(90.01)
        assert len(_p1(alerts)) == 1
        assert _recovered(alerts) == []
        clock["t"] = 1_700_000_130.0
        writer.write()
        watchdog._check_once(130.0)
    assert len(_recovered(alerts)) == 1
    assert _recovered(alerts)[0].startswith(
        "[HANG] TXF_One RECOVERED after 130 s")
    assert "reason=heartbeat" in _recovered(alerts)[0]
    assert "top frame=" in _recovered(alerts)[0]
    assert "test_stall_then_advance_sends_one_hung_and_one_recovered" in (
        _recovered(alerts)[0])
    assert any(
        "RECOVERED after 130 s" in rec.message for rec in caplog.records)
    watchdog._check_once(131.0)
    assert len(_recovered(alerts)) == 1
    assert len(_p1(alerts)) == 1
    hang = json.loads((bot / "hang.json").read_text(encoding="utf-8"))
    assert hang["stall_s"] == 130.0
    assert hang["recovered_at"] == 1_700_000_130.0
    assert hang["reason"] == "heartbeat"

    watchdog._check_once(220.01)
    assert len(_p1(alerts)) == 2
    clock["t"] = 1_700_000_250.0
    writer.write()
    watchdog._check_once(250.0)
    assert len(_recovered(alerts)) == 2
    hang = json.loads((bot / "hang.json").read_text(encoding="utf-8"))
    assert hang["stall_s"] == 120.0
    assert _stall_rollups(alerts) == []


def test_recovered_names_the_blocked_main_frame(tmp_path, monkeypatch):
    """The RECOVERED line names the bot and the injected main-thread frame.

    No watchdog thread. A live thread plus ``disable()`` that did not join
    raced later tests that patch ``os.replace``.
    """
    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(hb.time, "time", lambda: clock["t"])
    alerts: list[str] = []
    root = tmp_path / "live"
    bot = root / "TXF_Blocked"
    bot.mkdir(parents=True)
    writer = HeartbeatWriter(str(bot))
    writer.write()
    watchdog = HangWatchdog(
        str(bot), hung_s=90.0, alert_fn=alerts.append,
        top_frame_fn=lambda: "blocked_on_purpose (live.py:10)")
    watchdog._check_once(0.0)
    watchdog._check_once(100.0)
    clock["t"] = 1_700_000_140.0
    writer.write()
    watchdog._check_once(140.0)
    assert len(_p1(alerts)) == 1
    assert _recovered(alerts) == [
        "[HANG] TXF_Blocked RECOVERED after 140 s "
        "(reason=heartbeat, top frame=blocked_on_purpose (live.py:10))"
    ]
    assert watchdog._thread is None
    hang = json.loads((bot / "hang.json").read_text(encoding="utf-8"))
    assert hang["stall_s"] == 140.0
    assert hang["recovered_at"] == 1_700_000_140.0


def test_two_bots_do_not_roll_up_three_do_and_real_p1_is_kept(
        tmp_path, monkeypatch):
    """Two HUNG bots send no rollup. A third inside 120 s sends one.

    The real bot's own P1 is in its alert list whether it hung first or
    the rollup was sent by someone else. Paper bots keep their P1 too.
    One recovery rollup follows, with the max stall.
    """
    assert host_stall.HOST_STALL_WINDOW_S == 120.0
    assert host_stall.HOST_STALL_MIN_BOTS == 3
    clock = {"t": 5_000.0}
    monkeypatch.setattr(hb.time, "time", lambda: clock["t"])

    pair = tmp_path / "pair"
    pair_a: list[str] = []
    pair_b: list[str] = []
    wd_a, writer_a = _boot(pair, "TXF_A", "paper", clock, 5_000.0, pair_a)
    wd_b, writer_b = _boot(pair, "TXF_B", "paper", clock, 5_010.0, pair_b)
    assert _stall_rollups(pair_a + pair_b) == []
    assert len(_p1(pair_a)) == 1 and len(_p1(pair_b)) == 1
    _recover(writer_a, wd_a, clock, 5_200.0, 150.0)
    _recover(writer_b, wd_b, clock, 5_210.0, 160.0)
    assert _recovery_rollups(pair_a + pair_b) == []
    assert len(_recovered(pair_a)) == 1 and len(_recovered(pair_b)) == 1

    fleet = tmp_path / "fleet"
    real: list[str] = []
    paper1: list[str] = []
    paper2: list[str] = []
    wd_real, writer_real = _boot(
        fleet, "TXF_Real", "REAL", clock, 10_000.0, real)
    wd_p1, writer_p1 = _boot(
        fleet, "TXF_Paper1", "paper", clock, 10_040.0, paper1)
    assert _stall_rollups(real + paper1) == []
    wd_p2, writer_p2 = _boot(
        fleet, "TXF_Paper2", "paper", clock, 10_090.0, paper2)

    assert len(_p1(real)) == 1
    assert "mode=REAL" in _p1(real)[0]
    assert len(_p1(paper1)) == 1
    assert len(_p1(paper2)) == 1
    rollups = _stall_rollups(real + paper1 + paper2)
    assert len(rollups) == 1
    p1_at = next(i for i, item in enumerate(paper2) if "P1 HUNG" in item)
    roll_at = next(i for i, item in enumerate(paper2) if "bots stalled" in item)
    assert p1_at < roll_at
    assert rollups[0] == (
        "[HANG] host stall: 3 bots stalled within 90 s; "
        "per-bot alerts were sent"
    )
    marker = json.loads((fleet / "host_stall.json").read_text(encoding="utf-8"))
    assert marker["v"] == 1
    assert marker["window_s"] == 120
    assert marker["episodes"]["TXF_Real"]["real"] is True
    assert marker["rollup"]["bots"] == ["TXF_Real", "TXF_Paper1", "TXF_Paper2"]

    _recover(writer_real, wd_real, clock, 10_200.0, 150.0)
    _recover(writer_p1, wd_p1, clock, 10_210.0, 170.0)
    assert _recovery_rollups(real + paper1 + paper2) == []
    _recover(writer_p2, wd_p2, clock, 10_220.0, 180.0)
    recovery = _recovery_rollups(real + paper1 + paper2)
    assert recovery == ["[HANG] host stall: 3/3 recovered, max stall 180 s"]
    assert len(_p1(real)) == 1
    assert len(_recovered(real)) == 1


def test_hangs_outside_120s_do_not_roll_up(tmp_path, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(hb.time, "time", lambda: clock["t"])
    root = tmp_path / "wide"
    lists = [[], [], []]
    _boot(root, "TXF_A", "paper", clock, 0.0, lists[0])
    _boot(root, "TXF_B", "paper", clock, 60.0, lists[1])
    _boot(root, "TXF_C", "REAL", clock, 120.01, lists[2])
    assert _stall_rollups(lists[0] + lists[1] + lists[2]) == []
    assert len(_p1(lists[2])) == 1
    assert "mode=REAL" in _p1(lists[2])[0]

    edge = tmp_path / "edge"
    edge_lists = [[], [], []]
    _boot(edge, "TXF_A", "paper", clock, 1_000.0, edge_lists[0])
    _boot(edge, "TXF_B", "paper", clock, 1_060.0, edge_lists[1])
    _boot(edge, "TXF_C", "paper", clock, 1_120.0, edge_lists[2])
    rollups = _stall_rollups(edge_lists[0] + edge_lists[1] + edge_lists[2])
    assert rollups == [
        "[HANG] host stall: 3 bots stalled within 120 s; per-bot alerts were sent"
    ]


def test_stale_and_corrupt_markers_do_not_break_the_rollup(tmp_path, monkeypatch):
    clock = {"t": 20_000.0}
    monkeypatch.setattr(hb.time, "time", lambda: clock["t"])
    root = tmp_path / "live"
    root.mkdir()
    episode_dir = root / "host_stall.d"
    episode_dir.mkdir()
    now = 20_000.0
    old = {
        "bot": "TXF_Old",
        "pid": 1,
        "hung_at": now - host_stall.HOST_STALL_STALE_S - 5,
        "reason": "heartbeat",
        "real": False,
        "recovered_at": None,
        "stall_s": None,
    }
    aged = {
        "bot": "TXF_Aged",
        "pid": 2,
        "hung_at": now - 300.0,
        "reason": "heartbeat",
        "real": False,
        "recovered_at": None,
        "stall_s": None,
    }
    (episode_dir / "TXF_Old.json").write_text(json.dumps(old), encoding="utf-8")
    (episode_dir / "TXF_Aged.json").write_text(json.dumps(aged), encoding="utf-8")
    (episode_dir / "broken.json").write_text("{not-json", encoding="utf-8")
    (root / "host_stall.json").write_text("{not-json", encoding="utf-8")
    (root / "host_stall.claim.1").write_text("{not-json", encoding="utf-8")

    lists = [[], [], []]
    _boot(root, "TXF_A", "paper", clock, now, lists[0])
    _boot(root, "TXF_B", "paper", clock, now + 1, lists[1])
    _boot(root, "TXF_C", "REAL", clock, now + 2, lists[2])
    rollups = _stall_rollups(lists[0] + lists[1] + lists[2])
    assert rollups == [
        "[HANG] host stall: 3 bots stalled within 2 s; per-bot alerts were sent"
    ]
    assert len(_p1(lists[2])) == 1
    episodes = host_stall.read_episodes(str(root), now=now + 2)
    assert "TXF_Old" not in episodes
    assert "TXF_Aged" in episodes
    assert "TXF_A" in episodes and "TXF_C" in episodes
    marker = json.loads((root / "host_stall.json").read_text(encoding="utf-8"))
    assert marker["v"] == 1
    assert "TXF_Old" not in marker["episodes"]
    claim = _latest_claim(root)
    assert claim["bots"] == ["TXF_A", "TXF_B", "TXF_C"]
    assert (episode_dir / "broken.json").is_file()


def test_concurrent_writers_keep_every_bot_and_one_rollup(tmp_path):
    root = tmp_path / "live"
    root.mkdir()
    alerts: list[str] = []
    lock = threading.Lock()

    def alert(message: str) -> None:
        with lock:
            alerts.append(message)

    def work(i: int) -> None:
        bot = root / f"TXF_B{i}"
        bot.mkdir()
        host_stall.record_hung(
            str(bot),
            reason="heartbeat",
            real=(i == 0),
            now=8_000.0 + i,
            pid=1_000 + i,
            alert_fn=alert,
        )

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    episodes = host_stall.read_episodes(str(root), now=8_010.0)
    assert set(episodes) == {f"TXF_B{i}" for i in range(6)}
    assert episodes["TXF_B0"]["real"] is True
    stalls = _stall_rollups(alerts)
    assert len(stalls) == 1
    assert "per-bot alerts were sent" in stalls[0]
    marker = json.loads((root / "host_stall.json").read_text(encoding="utf-8"))
    assert marker["v"] == 1
    assert isinstance(marker["episodes"], dict)


def test_recovery_rollup_waits_for_every_bot_in_the_window(tmp_path, monkeypatch):
    """Bots at 0/1/2/3 s. Recovering the first three must not say 3/3."""
    clock = {"t": 0.0}
    monkeypatch.setattr(hb.time, "time", lambda: clock["t"])
    root = tmp_path / "live"
    bots = []
    for i, name in enumerate(("TXF_A", "TXF_B", "TXF_C", "TXF_D")):
        alerts: list[str] = []
        wd, writer = _boot(root, name, "paper", clock, float(i), alerts)
        bots.append((wd, writer, alerts))
    early = [item for _wd, _writer, alerts in bots[:3] for item in alerts]
    assert len(_stall_rollups(early)) == 1
    for i in range(3):
        wd, writer, _alerts = bots[i]
        _recover(writer, wd, clock, 50.0 + i, 150.0 + i)
    mid = [item for _wd, _writer, alerts in bots for item in alerts]
    assert _recovery_rollups(mid) == []
    wd, writer, _alerts = bots[3]
    _recover(writer, wd, clock, 60.0, 180.0)
    done = [item for _wd, _writer, alerts in bots for item in alerts]
    assert _recovery_rollups(done) == [
        "[HANG] host stall: 4/4 recovered, max stall 180 s"
    ]


def test_unrecovered_claim_outside_the_window_does_not_block(tmp_path):
    """A claim whose bot never recovers must not suppress the next window."""
    root = tmp_path / "live"
    root.mkdir()
    old: list[str] = []
    base = 10_000.0
    for i, name in enumerate(("TXF_OldA", "TXF_OldB", "TXF_OldC")):
        bot = root / name
        bot.mkdir()
        host_stall.record_hung(
            str(bot), reason="heartbeat", real=False,
            now=base - 500.0 + i, pid=10 + i, alert_fn=old.append)
    assert len(_stall_rollups(old)) == 1
    # Still inside 120 s of that claim: a second trio is the same incident.
    same: list[str] = []
    for i, name in enumerate(("TXF_SameA", "TXF_SameB", "TXF_SameC")):
        bot = root / name
        bot.mkdir()
        host_stall.record_hung(
            str(bot), reason="heartbeat", real=False,
            now=base - 400.0 + i, pid=20 + i, alert_fn=same.append)
    assert _stall_rollups(same) == []
    fresh: list[str] = []
    for i, name in enumerate(("TXF_NewA", "TXF_NewB", "TXF_NewC")):
        bot = root / name
        bot.mkdir()
        host_stall.record_hung(
            str(bot), reason="heartbeat", real=True if i == 0 else False,
            now=base + i, pid=30 + i, alert_fn=fresh.append)
    assert len(_stall_rollups(fresh)) == 1
    assert "per-bot alerts were sent" in _stall_rollups(fresh)[0]
    assert _latest_claim(root)["bots"] == ["TXF_NewA", "TXF_NewB", "TXF_NewC"]


def test_two_writers_replace_a_stale_claim_once(tmp_path):
    """Eight writers publish the next generation. Exactly one links it.

    Episodes are on disk before any publisher runs, so every writer sees
    the same full window. The barrier then sits between choosing
    generation 2 and ``os.link``. Generation 1 is not renamed or removed.
    """
    root = tmp_path / "live"
    root.mkdir()
    stale = {
        "generation": 1,
        "bots": ["TXF_Gone"],
        "hung_at": {"TXF_Gone": 1.0},
        "span_s": 0.0,
        "sent_at": 1.0,
    }
    original = json.dumps(stale)
    (root / "host_stall.claim.1").write_text(original, encoding="utf-8")
    episode_dir = root / "host_stall.d"
    episode_dir.mkdir()
    now = 20_000.0
    for i in range(8):
        episode = {
            "bot": f"TXF_R{i}",
            "pid": 100 + i,
            "hung_at": now + i,
            "reason": "heartbeat",
            "real": False,
            "recovered_at": None,
            "stall_s": None,
        }
        (episode_dir / f"TXF_R{i}.json").write_text(
            json.dumps(episode), encoding="utf-8")
    alerts: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def alert(message: str) -> None:
        with lock:
            alerts.append(message)

    def hooked() -> None:
        barrier.wait(timeout=5)

    episodes = host_stall.read_episodes(str(root), now + 10)
    host_stall._before_claim_create = hooked
    try:
        def work() -> None:
            host_stall._maybe_send_stall(
                str(root), episodes, now + 10, alert)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        host_stall._before_claim_create = None
    assert len(_stall_rollups(alerts)) == 1
    assert (root / "host_stall.claim.1").read_text(encoding="utf-8") == original
    claim = _latest_claim(root)
    assert claim["generation"] == 2
    assert "TXF_Gone" not in claim["bots"]
    assert len(claim["bots"]) == 8


def _hang_named(root: Path, names: tuple[str, ...], t0: float, alerts: list[str]) -> None:
    for i, name in enumerate(names):
        bot = root / name
        bot.mkdir(exist_ok=True)
        host_stall.record_hung(
            str(bot), reason="heartbeat", real=False,
            now=t0 + i, pid=i + 1, alert_fn=alerts.append)


def _recover_named(root: Path, names: tuple[str, ...], t0: float, stall: float,
                    alerts: list[str]) -> None:
    for i, name in enumerate(names):
        host_stall.record_recovered(
            str(root / name), stall_s=stall + i, recovered_at=t0 + i,
            now=t0 + i, alert_fn=alerts.append)


def test_same_bots_roll_up_again_after_the_window(tmp_path):
    """A later incident with the same names gets its own rollup and K/K.

    Name overlap with an old claim is not enough. The new hung_at values
    are more than 120 s after the first claim's window.
    """
    root = tmp_path / "live"
    root.mkdir()
    alerts: list[str] = []
    names = ("TXF_A", "TXF_B", "TXF_C")
    _hang_named(root, names, 1_000.0, alerts)
    assert len(_stall_rollups(alerts)) == 1
    _recover_named(root, names, 1_100.0, 10.0, alerts)
    assert _recovery_rollups(alerts) == [
        "[HANG] host stall: 3/3 recovered, max stall 12 s"
    ]
    _hang_named(root, names, 2_000.0, alerts)
    assert len(_stall_rollups(alerts)) == 2
    assert _latest_claim(root)["generation"] == 2
    _recover_named(root, names, 2_100.0, 20.0, alerts)
    assert _recovery_rollups(alerts) == [
        "[HANG] host stall: 3/3 recovered, max stall 12 s",
        "[HANG] host stall: 3/3 recovered, max stall 22 s",
    ]


def test_rehang_inside_the_window_stays_on_the_same_claim(tmp_path):
    """One bot hanging again inside the claim window does not roll up twice."""
    root = tmp_path / "live"
    root.mkdir()
    alerts: list[str] = []
    names = ("TXF_A", "TXF_B", "TXF_C")
    _hang_named(root, names, 1_000.0, alerts)
    assert len(_stall_rollups(alerts)) == 1
    # Claim end is 1002. 1050 is still inside [882, 1122].
    host_stall.record_hung(
        str(root / "TXF_A"), reason="heartbeat", real=False,
        now=1_050.0, pid=1, alert_fn=alerts.append)
    assert len(_stall_rollups(alerts)) == 1
    assert _latest_claim(root)["generation"] == 1


def test_later_hang_does_not_swallow_the_original_recovery(tmp_path):
    """A bot more than 120 s later must not erase the first group's K/K."""
    root = tmp_path / "live"
    root.mkdir()
    alerts: list[str] = []
    for i, name in enumerate(("TXF_A", "TXF_B", "TXF_C")):
        bot = root / name
        bot.mkdir()
        host_stall.record_hung(
            str(bot), reason="heartbeat", real=False,
            now=1_000.0 + i, pid=i + 1, alert_fn=alerts.append)
    assert len(_stall_rollups(alerts)) == 1
    late = root / "TXF_D"
    late.mkdir()
    host_stall.record_hung(
        str(late), reason="heartbeat", real=False,
        now=1_200.0, pid=4, alert_fn=alerts.append)
    assert len(_stall_rollups(alerts)) == 1
    for i, name in enumerate(("TXF_A", "TXF_B", "TXF_C", "TXF_D")):
        host_stall.record_recovered(
            str(root / name), stall_s=30.0 + i, recovered_at=1_300.0 + i,
            now=1_300.0 + i, alert_fn=alerts.append)
    assert _recovery_rollups(alerts) == [
        "[HANG] host stall: 3/3 recovered, max stall 32 s"
    ]


def test_holiday_cache_misses_when_the_callable_changes(monkeypatch):
    """A different country_holidays callable must not reuse the cached year."""
    import src.market_data.holidays as hol

    hol._tw_holiday_cache.clear()
    missing = Path("/tmp/tai-robot-no-closures-233-callable")
    monkeypatch.setattr(hol, "closures_override_path", lambda: missing)
    hol._closures_cache = None
    hol._closures_checked_mono = None
    assert hol.is_taifex_holiday(date(2026, 4, 15)) is False
    real = hol._holidays.country_holidays
    calls = {"n": 0}

    def wrapped(code, years=None, **kwargs):
        calls["n"] += 1
        return real(code, years=years, **kwargs)

    monkeypatch.setattr(hol._holidays, "country_holidays", wrapped)
    assert hol.is_taifex_holiday(date(2026, 4, 16)) is False
    assert calls["n"] == 1
    hol._tw_holiday_cache.clear()


def test_holiday_failure_is_retried_on_the_next_call(monkeypatch, caplog):
    import src.market_data.holidays as hol

    hol._tw_holiday_cache.clear()
    hol._tw_holidays_broken = False
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise RuntimeError("TW calendar down")

    monkeypatch.setattr(hol._holidays, "country_holidays", boom)
    missing = Path("/tmp/tai-robot-no-closures-233-retry")
    monkeypatch.setattr(hol, "closures_override_path", lambda: missing)
    hol._closures_cache = None
    hol._closures_checked_mono = None
    with caplog.at_level(logging.WARNING, logger="src.market_data.holidays"):
        assert hol.is_taifex_holiday(date(2026, 4, 15)) is False
        assert hol.is_taifex_holiday(date(2026, 4, 16)) is False
    assert calls["n"] == 2
    warnings = [r for r in caplog.records if "weekend-only" in r.getMessage()]
    assert len(warnings) == 1
    hol._tw_holiday_cache.clear()
    hol._tw_holidays_broken = False


def test_holiday_answers_unchanged_and_built_once_per_year(monkeypatch):
    import src.market_data.holidays as hol

    hol._tw_holiday_cache.clear()
    missing = Path("/tmp/tai-robot-no-closures-233")
    monkeypatch.setattr(hol, "closures_override_path", lambda: missing)
    hol._closures_cache = None
    hol._closures_checked_mono = None
    real = hol._holidays.country_holidays
    direct = frozenset(real("TW", years=2026))
    calls: list[int] = []

    def wrapped(code, years=None, **kwargs):
        calls.append(int(years))
        return real(code, years=years, **kwargs)

    monkeypatch.setattr(hol._holidays, "country_holidays", wrapped)
    hol._tw_holiday_cache.clear()
    dates = [date(2026, month, 10) for month in range(1, 13)]
    first = [hol.is_taifex_holiday(day) for day in dates]
    second = [hol.is_taifex_holiday(day) for day in dates]
    assert first == second
    for day, answer in zip(dates, first):
        if day.weekday() >= 5:
            assert answer is True
        else:
            assert answer is (day in direct)
    assert calls.count(2026) == 1
    later = date(2027, 4, 14)
    while later.weekday() >= 5:
        later += timedelta(days=1)
    assert hol.is_taifex_holiday(later) is (later in frozenset(real("TW", years=2027)))
    assert calls.count(2027) == 1
    assert calls.count(2026) == 1
    hol._tw_holiday_cache.clear()


def test_closure_stat_throttled_and_edit_applies_after_window(
        tmp_path, monkeypatch):
    import src.market_data.holidays as hol

    assert hol.CLOSURE_STAT_INTERVAL_S == 60.0
    path = tmp_path / "taifex_closures.yaml"
    path.write_text("2026-04-16\n", encoding="utf-8")
    monkeypatch.setattr(hol, "closures_override_path", lambda: path)
    hol._closures_cache = None
    hol._closures_checked_mono = None
    clock = {"t": 10_000.0}
    monkeypatch.setattr(hol, "_monotonic", lambda: clock["t"])
    stats = {"n": 0}
    real_stat = Path.stat

    def counting(self, *args, **kwargs):
        if self == path:
            stats["n"] += 1
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting)
    assert hol.is_taifex_holiday(date(2026, 4, 16)) is True
    assert hol.is_taifex_holiday(date(2026, 4, 17)) is False
    assert [hol.is_taifex_holiday(date(2026, 4, 16)) for _ in range(5)] == (
        [True, True, True, True, True])
    assert stats["n"] == 1

    path.write_text("2026-04-17\n", encoding="utf-8")
    os_utime = time.time() + 30
    os.utime(path, (os_utime, os_utime))
    clock["t"] = 10_000.0 + hol.CLOSURE_STAT_INTERVAL_S - 0.001
    assert hol.is_taifex_holiday(date(2026, 4, 16)) is True
    assert hol.is_taifex_holiday(date(2026, 4, 17)) is False
    assert stats["n"] == 1

    clock["t"] = 10_000.0 + hol.CLOSURE_STAT_INTERVAL_S
    assert hol.is_taifex_holiday(date(2026, 4, 16)) is False
    assert hol.is_taifex_holiday(date(2026, 4, 17)) is True
    assert stats["n"] == 2
    assert hol.is_taifex_holiday(date(2026, 4, 17)) is True
    assert stats["n"] == 2
    hol._closures_cache = None
    hol._closures_checked_mono = None
