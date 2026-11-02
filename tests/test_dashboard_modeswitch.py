"""Dashboard side of the live paper <-> auto switch.

The dashboard only writes ``mode_request.json``; the bot checks the real
account and answers in ``mode_request_result.json``. Real money (auto) is
offered only when the running process advertises the channel
(``capabilities.json`` with the lock's pid), and only with the bot name,
「實單」 and a loss limit.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

import pytest

from scripts.dashboard import control, server
from scripts.monitor.common import TZ_TPE

from tests.test_dashboard import (  # noqa: F401  (fixture)
    ALIVE_PID, FAKE_CHANNEL, _get, _post, _set_mode, control_server,
)


def _caps(bot_dir, pid=ALIVE_PID, mode_request=1):
    (bot_dir / "capabilities.json").write_text(json.dumps(
        {"mode_request": mode_request, "pid": pid, "version": "2.19.11"}))


def _checking(bot_dir, age_s):
    at = (datetime.now(TZ_TPE) - timedelta(seconds=age_s)).isoformat(timespec="seconds")
    (bot_dir / "mode_request_result.json").write_text(json.dumps(
        {"request_id": "x", "mode": "auto", "status": "checking", "reason": "", "at": at}))


def _auto(url, bot="TMF00_alpha", name="alpha", ack="實單", limit=10000, **extra):
    body = {"id": bot, "action": "auto", "confirm": name, "real_ack": ack,
            "loss_limit": limit, **extra}
    return _post(url, body)


def test_auto_needs_the_live_channel_of_the_running_process(control_server):
    url, base, log = control_server
    alpha = base / "TMF00_alpha"
    status, body = _auto(url)                                   # no capabilities.json
    assert status == 409 and "不支援" in body["message"]
    _caps(alpha, pid=999999)                                    # left by another process
    assert _auto(url)[0] == 409
    assert not (alpha / "mode_request.json").exists()
    _caps(alpha)                                                # this process
    status, body = _auto(url)
    assert (status, body["ok"]) == (200, True)
    req = json.loads((alpha / "mode_request.json").read_text(encoding="utf-8"))
    assert req["mode"] == "auto" and req["loss_limit"] == 10000
    assert len(req["request_id"]) == 32 and req["requested_by"] == "127.0.0.1"
    assert not (alpha / "mode_override.json").exists()          # never the legacy file


@pytest.mark.parametrize("name,ack,limit", [
    ("wrong", "實單", 10000), ("alpha", "", 10000), ("alpha", "auto", 10000),
    ("alpha", "實單", 0), ("alpha", "實單", -5), ("alpha", "實單", 10_000_001),
])
def test_auto_needs_name_ack_and_a_sane_loss_limit(control_server, name, ack, limit):
    url, base, _ = control_server
    _caps(base / "TMF00_alpha")
    status, body = _auto(url, name=name, ack=ack, limit=limit)
    assert status == 409 and body["ok"] is False
    assert not (base / "TMF00_alpha" / "mode_request.json").exists()


@pytest.mark.parametrize("limit", ["10000", 1.5, True, None])
def test_loss_limit_must_be_a_json_integer(control_server, limit):
    url, base, _ = control_server
    _caps(base / "TMF00_alpha")
    status, _ = _auto(url, limit=limit)
    assert status in (400, 409)
    assert not (base / "TMF00_alpha" / "mode_request.json").exists()


def test_auto_refused_when_holding_already_auto_or_busy(control_server):
    url, base, _ = control_server
    alpha, real = base / "TMF00_alpha", base / "TMF00_real"
    _caps(alpha); _caps(real)
    assert _auto(url, bot="TMF00_real", name="real")[0] == 409            # already auto
    _set_mode(alpha, "paper", position={"position_size": 1, "position_side": "LONG",
                                        "entry_price": 45000})
    status, body = _auto(url)
    assert status == 409 and "持倉" in body["message"]
    _set_mode(alpha, "paper", position={"position_size": 0})
    _checking(alpha, 5)
    status, body = _auto(url)
    assert status == 409 and "處理中" in body["message"]
    (alpha / "mode_request_result.json").unlink()
    assert _auto(url)[0] == 200
    assert _auto(url)[0] == 409                                            # request queued


def test_paper_uses_the_live_channel_when_the_bot_has_it(control_server):
    url, base, _ = control_server
    real = base / "TMF00_real"
    status, _ = _post(url, {"id": "TMF00_real", "action": "paper", "confirm": "paper"})
    assert status == 200 and (real / "mode_override.json").exists()       # legacy bot
    (real / "mode_override.json").unlink()
    _caps(real)
    status, body = _post(url, {"id": "TMF00_real", "action": "paper", "confirm": "paper"})
    assert status == 200 and "數秒" in body["message"]
    req = json.loads((real / "mode_request.json").read_text(encoding="utf-8"))
    assert req["mode"] == "paper" and "loss_limit" not in req
    assert not (real / "mode_override.json").exists()


@pytest.mark.parametrize("status,shown", [
    ("checking", "checking"), ("accepted", "accepted"), ("refused", "refused"), ("bogus", None),
])
def test_bot_answer_is_surfaced(control_server, status, shown):
    url, base, _ = control_server
    alpha = base / "TMF00_alpha"
    _caps(alpha)
    (alpha / "mode_request_result.json").write_text(json.dumps(
        {"request_id": "ab12", "mode": "auto", "status": status,
         "reason": f"帳戶已有未平倉部位 channel={FAKE_CHANNEL}", "at": "2026-10-28T10:00:00"}),
        encoding="utf-8")
    fleet = json.loads(_get(url + "/api/fleet")[2])
    bot = next(b for b in fleet["bots"] if b["id"] == "TMF00_alpha")
    assert bot["supports_mode_request"] is True
    result = bot["pending"]["result"]
    if shown is None:
        assert result is None
    else:
        assert result["status"] == shown and result["at"] == "2026-10-28 10:00:00"
        assert FAKE_CHANNEL not in result["reason"]


def test_queued_request_shows_as_pending(control_server):
    url, base, _ = control_server
    _caps(base / "TMF00_alpha")
    assert _auto(url)[0] == 200
    fleet = json.loads(_get(url + "/api/fleet")[2])
    bot = next(b for b in fleet["bots"] if b["id"] == "TMF00_alpha")
    assert bot["pending"]["request"] == "auto"


def test_auto_is_audited(control_server):
    url, base, log = control_server
    _caps(base / "TMF00_alpha")
    _auto(url, name="wrong")
    _auto(url)
    import csv
    with open(log, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [(r["action"], r["result"]) for r in rows] == [("auto", "refused"), ("auto", "accepted")]


def test_stale_checking_result_stops_blocking(control_server):
    """A bot that died, or failed its final write, mid-check must not lock
    both buttons forever."""
    url, base, _ = control_server
    alpha = base / "TMF00_alpha"
    _caps(alpha)
    _checking(alpha, control.MODE_CHECKING_STALE_S + 5)
    assert _auto(url)[0] == 200


def test_auto_refused_while_a_paper_override_is_queued(control_server):
    """A queued mode_override.json would revert the switch at the next bar."""
    url, base, _ = control_server
    alpha = base / "TMF00_alpha"
    _caps(alpha)
    (alpha / "mode_override.json").write_text(json.dumps({"trading_mode": "paper"}))
    status, body = _auto(url)
    assert status == 409 and "等待套用" in body["message"]
    assert not (alpha / "mode_request.json").exists()


def test_concurrent_requests_queue_exactly_one(control_server, monkeypatch):
    url, base, log = control_server
    _caps(base / "TMF00_alpha")
    real_write = control._atomic_write_json

    def _slow_write(path, payload):
        time.sleep(0.3)            # widen the check -> write window
        real_write(path, payload)

    monkeypatch.setattr(control, "_atomic_write_json", _slow_write)
    results = []
    threads = [threading.Thread(target=lambda: results.append(_auto(url)[0]))
               for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409, 409]
    import csv
    with open(log, encoding="utf-8") as f:
        outcomes = sorted(r["result"] for r in csv.DictReader(f))
    assert outcomes == ["accepted", "refused", "refused"]   # refused, not a write error


def test_dashboard_writes_use_their_own_temp_file(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def _spy(src, dst):
        seen.append(os.path.basename(src))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)
    for _ in range(2):
        control._atomic_write_json(str(tmp_path / "mode_request.json"), {"a": 1})
    assert len(set(seen)) == 2
    assert sorted(os.listdir(tmp_path)) == ["mode_request.json"]


def _page():
    return open(os.path.join(os.path.dirname(server.__file__), "static", "index.html"),
                encoding="utf-8").read()


def test_page_uses_the_same_checking_age_limit():
    m = re.search(r"const CHECKING_STALE_S = (\d+);", _page())
    assert m and int(m.group(1)) == control.MODE_CHECKING_STALE_S


def test_page_shows_switched_only_when_the_bot_is_in_that_mode():
    """After a fill-timeout downgrade the bot is back in paper; the
    15-minute 已切換為實單 chip must not keep saying otherwise."""
    page = _page()
    body = page[page.index("function requestChip"):page.index("function pendingChip")]
    assert re.search(r'r\.status === "accepted" && tradingMode === r\.mode', body), body
    assert "requestChip(b.pending, b.trading_mode)" in page
