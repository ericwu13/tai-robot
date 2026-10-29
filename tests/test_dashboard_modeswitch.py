"""Dashboard side of the live paper <-> auto switch.

The dashboard only writes ``mode_request.json``; the bot checks the real
account and answers in ``mode_request_result.json``. Real money (auto) is
offered only when the running process advertises the channel
(``capabilities.json`` with the lock's pid), and only with the bot name,
「實單」 and a loss limit.
"""

from __future__ import annotations

import json

import pytest

from tests.test_dashboard import (  # noqa: F401  (fixture)
    ALIVE_PID, FAKE_CHANNEL, _get, _post, _set_mode, control_server,
)


def _caps(bot_dir, pid=ALIVE_PID, mode_request=1):
    (bot_dir / "capabilities.json").write_text(json.dumps(
        {"mode_request": mode_request, "pid": pid, "version": "2.19.11"}))


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
    (alpha / "mode_request_result.json").write_text(json.dumps(
        {"request_id": "x", "mode": "auto", "status": "checking", "reason": "", "at": "2026-10-28T10:00:00"}))
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
