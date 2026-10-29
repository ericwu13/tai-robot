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
    assert snapshot.CAPABILITIES_FILENAME == mreq.CAPABILITIES_FILENAME


def test_dashboard_auto_request_is_what_the_bot_accepts(tmp_path):
    msg = control.apply("auto", str(tmp_path), "100.64.0.9", live_channel=True, loss_limit=10000)
    assert "檢查" in msg
    req, err = mreq.consume_request(str(tmp_path))
    assert err is None and req is not None
    assert (req.mode, req.loss_limit) == ("auto", 10000)
    assert not os.path.exists(mreq.request_path(str(tmp_path)))   # consumed once


def test_dashboard_paper_request_is_what_the_bot_accepts(tmp_path):
    control.apply("paper", str(tmp_path), "127.0.0.1", live_channel=True)
    req, err = mreq.consume_request(str(tmp_path))
    assert err is None and req.mode == "paper"


def test_bot_result_and_capabilities_are_what_the_dashboard_reads(tmp_path):
    mreq.write_result(str(tmp_path), mreq.result_payload(
        "ab12", "auto", mreq.STATUS_REFUSED, "帳戶已有未平倉部位",
        "2026-10-29T10:00:00+08:00"))                             # as the bot stamps it
    pend = snapshot.pending(str(tmp_path))
    assert pend["result"]["status"] == "refused" and pend["result"]["mode"] == "auto"
    assert pend["result"]["at"] == "2026-10-29 10:00:00"
    caps = mreq.capabilities_payload(4242, "2.19.11")
    import json
    with open(mreq.capabilities_path(str(tmp_path)), "w", encoding="utf-8") as f:
        json.dump(caps, f)
    assert snapshot.supports_mode_request(str(tmp_path), 4242) is True
    assert snapshot.supports_mode_request(str(tmp_path), 4243) is False
