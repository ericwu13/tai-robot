"""The dashboard and the bot agree on the live mode-switch files.

Each side has its own tests; these pin the hand-off between them: what
``scripts.dashboard.control`` writes, ``src.live.mode_request`` parses,
and what the bot writes back, the dashboard reads.
"""

from __future__ import annotations

import os

from scripts.dashboard import control, snapshot
from src.live import mode_request as mreq


def test_file_names_match():
    assert snapshot.MODE_REQUEST_FILENAME == mreq.REQUEST_FILENAME
    assert snapshot.MODE_RESULT_FILENAME == mreq.RESULT_FILENAME


def _live_marker(tmp_path, monkeypatch):
    from src.live.headless import write_headless_marker
    monkeypatch.setattr(snapshot, "pid_alive_strict", lambda pid: pid == 4242)
    monkeypatch.setattr(snapshot, "process_create_time", lambda pid: None)
    (tmp_path / ".lock").write_text("4242", encoding="utf-8")
    assert write_headless_marker(
        str(tmp_path), pid=4242, started_at="2026-10-29T21:00:00+08:00",
        mode_request=True)


def test_dashboard_auto_request_is_what_the_bot_accepts(tmp_path, monkeypatch):
    _live_marker(tmp_path, monkeypatch)
    msg = control.apply("auto", str(tmp_path), "100.64.0.9", live_channel=True, loss_limit=10000)
    assert "檢查" in msg
    req, err = mreq.consume_request(str(tmp_path))
    assert err is None and req is not None
    assert (req.mode, req.loss_limit) == ("auto", 10000)
    assert not os.path.exists(mreq.request_path(str(tmp_path)))   # consumed once


def test_dashboard_paper_request_is_what_the_bot_accepts(tmp_path, monkeypatch):
    _live_marker(tmp_path, monkeypatch)
    control.apply("paper", str(tmp_path), "127.0.0.1", live_channel=True)
    req, err = mreq.consume_request(str(tmp_path))
    assert err is None and req.mode == "paper"


def test_bot_result_and_marker_capability_are_what_the_dashboard_reads(tmp_path, monkeypatch):
    from src.live.headless import write_headless_marker
    mreq.write_result(str(tmp_path), mreq.result_payload(
        "ab12", "auto", mreq.STATUS_REFUSED, "帳戶已有未平倉部位",
        "2026-10-29T10:00:00+08:00"))                             # as the bot stamps it
    pend = snapshot.pending(str(tmp_path))
    assert pend["result"]["status"] == "refused" and pend["result"]["mode"] == "auto"
    assert pend["result"]["at"] == "2026-10-29 10:00:00"
    monkeypatch.setattr(snapshot, "process_create_time", lambda pid: None)
    monkeypatch.setattr(snapshot, "pid_alive_strict", lambda pid: pid == 4242)
    (tmp_path / ".lock").write_text("4242", encoding="utf-8")
    assert write_headless_marker(
        str(tmp_path), pid=4242, started_at="2026-10-29T21:00:00+08:00",
        mode_request=True)
    assert snapshot.supports_mode_request(str(tmp_path)) is True
    assert write_headless_marker(
        str(tmp_path), pid=4243, started_at="2026-10-29T21:00:00+08:00",
        mode_request=True)
    assert snapshot.supports_mode_request(str(tmp_path)) is False
