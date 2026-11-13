"""W4 transport failures must keep the vote and exit non-zero.

These fail on master (dc8b72b): a DNS/HTTP error is swallowed into
"no TAIFEX data", the vote file is deleted, and the process exits 0.
A genuine empty dataset still deletes the vote and exits 0.

No test hits the network.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.monitor.check_bridge import check_bridge
from src.news.vote_status import _w4_status
from tests.test_chips_monitor import TPE, chips

_AT_0415 = datetime(2026, 11, 12, 4, 15, tzinfo=TPE)  # Thursday catch-up slot
_FRIDAY = datetime(2026, 8, 14, 16, 15, tzinfo=TPE)
_SATURDAY = datetime(2026, 8, 15, 16, 15, tzinfo=TPE)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(*_a, **_k):
        raise socket.gaierror(11001, "getaddrinfo failed")
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


class _Body:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self):
        return self._payload

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _gaierror():
    return urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))


def _seed_vote(base: str, payload: bytes = b'{"direction":"trending-up","source":"W4"}\n') -> Path:
    vote = chips.w4_vote_path(base)
    vote.parent.mkdir(parents=True, exist_ok=True)
    vote.write_bytes(payload)
    return vote


def _log(base: str) -> str:
    path = Path(base).parent / chips.LOG_NAME
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _patch_empty(monkeypatch):
    monkeypatch.setattr(chips, "fetch_taifex_foreign_net_oi", lambda _d: None)
    monkeypatch.setattr(chips, "fetch_twse_foreign_net_buy", lambda _d: None)


# ── 6. DNS at the 04:15 slot keeps the vote; TWSE transport does too ────

def test_getaddrinfo_at_0415_keeps_vote_file(tmp_path, monkeypatch):
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    monkeypatch.setattr(chips.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(_gaierror()))
    code = chips.run_once(base, None, _AT_0415)
    state = chips.load_state(chips.state_path_for(base))
    log = _log(base)
    assert code != 0
    assert vote.read_bytes() == before
    assert not state.get("last_decided_date")
    assert state.get("fetch_error_streak", 0) >= 1
    assert "fetch failed" in log.lower()
    assert "fetch failed" in (state.get("last_result") or "").lower()
    assert "no taifex data" not in log.lower()
    assert "no taifex data" not in (state.get("last_result") or "").lower()


def test_twse_transport_error_keeps_vote_and_does_not_decide(tmp_path, monkeypatch):
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    chips.save_state(chips.state_path_for(base), {"history": {"20260813": 0.0}})
    row = {"Date": "20260814", "ContractCode": "臺股期貨", "Item": "外資及陸資",
           "OpenInterest(Net)": "8,123"}

    def fake(req, timeout=None, context=None):
        if "twse.com.tw" in req.full_url:
            raise _gaierror()
        return _Body(json.dumps([row]).encode())

    monkeypatch.setattr(chips.urllib.request, "urlopen", fake)
    code = chips.run_once(base, "20260814", _FRIDAY)
    state = chips.load_state(chips.state_path_for(base))
    log = _log(base)
    assert code != 0
    assert vote.read_bytes() == before
    assert state.get("last_decided_date") != "20260814"
    assert "fetch failed" in log.lower()
    assert "no taifex data" not in log.lower()


def test_http_500_keeps_the_vote_and_exits_nonzero(tmp_path, monkeypatch):
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()

    def fake(req, timeout=None, context=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", hdrs=None, fp=None)

    monkeypatch.setattr(chips.urllib.request, "urlopen", fake)
    assert chips.run_once(base, "20260814", _FRIDAY) != 0
    assert vote.read_bytes() == before
    assert "no taifex data" not in _log(base).lower()


def test_network_failure_marks_the_run_and_exits_nonzero(tmp_path, monkeypatch):
    base = tmp_path / "regime_vote.json"
    vote = _seed_vote(str(base))
    before = vote.read_bytes()
    monkeypatch.setattr(chips.urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(_gaierror()))
    monkeypatch.setattr(sys, "argv", ["chips_monitor.py", "--base-path", str(base),
                                      "--date", "20260814"])
    assert chips.main() != 0
    assert vote.read_bytes() == before
    path = tmp_path / "runs" / "W4.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert rows[-1]["ok"] is False
    assert rows[-1]["exit_code"] not in (None, 0)
    assert "fetch failed" in (rows[-1].get("error") or "").lower()
    assert "no taifex data" not in (rows[-1].get("error") or "").lower()
    assert "no taifex data" not in _log(str(base)).lower()


# ── 7. a real empty day still deletes the vote and exits 0 ──────────────

def test_real_holiday_still_no_vote(tmp_path, monkeypatch):
    """Weekend and a confirmed weekday holiday say holiday and exit 0.
    An open weekday with an empty payload says API lag, not holiday.
    The existing no-data deletion (test_run_once_no_data_deletes_stale_vote) stays."""
    base = str(tmp_path / "regime_vote.json")
    _patch_empty(monkeypatch)

    vote = _seed_vote(base)
    code = chips.run_once(base, "20260815", _SATURDAY)
    state = chips.load_state(chips.state_path_for(base))
    assert code == 0
    assert not vote.exists()
    assert "TAIFEX holiday" in state["last_result"]
    assert "no TAIFEX data" in state["last_result"]

    vote = _seed_vote(base)
    code = chips.run_once(base, "20260814", _FRIDAY)
    state = chips.load_state(chips.state_path_for(base))
    assert code == 0
    assert not vote.exists()
    assert "no data yet (API lag)" in state["last_result"]
    assert "holiday" not in state["last_result"].lower()

    code = chips.run_once(base, "20260101", datetime(2026, 1, 1, 16, 15, tzinfo=TPE))
    state = chips.load_state(chips.state_path_for(base))
    assert code == 0
    assert "TAIFEX holiday" in state["last_result"]


def test_holiday_calendar_import_falls_back_to_weekend_only(tmp_path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "src.market_data.holidays":
            raise ImportError("calendar unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)
    _patch_empty(monkeypatch)
    base = str(tmp_path / "regime_vote.json")

    assert chips.run_once(base, "20260815", _SATURDAY) == 0
    weekend = chips.load_state(chips.state_path_for(base))["last_result"]
    assert "TAIFEX holiday" in weekend

    assert chips.run_once(base, "20260101", datetime(2026, 1, 1, 16, 15, tzinfo=TPE)) == 0
    weekday = chips.load_state(chips.state_path_for(base))["last_result"]
    assert "no data yet (API lag)" in weekday
    assert "holiday" not in weekday.lower()


def test_weekday_holiday_from_a_clean_script_launch(tmp_path):
    """The scheduled task runs ``python scripts/news_bridge/chips_monitor.py``.

    That puts ``scripts/news_bridge`` on sys.path, not the repo root, and
    pytest's rootdir is not there. A clean interpreter, cwd outside the
    repo, and an empty PYTHONPATH must still call 2026-10-09 a holiday
    (also 2026-01-01 and 2026-02-16). Before the repo-root insert those
    three weekdays printed API lag; a Saturday did not, because it never
    imports.
    """
    script = Path(chips.__file__).resolve()
    outside = tmp_path / "outside"
    outside.mkdir()
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('chips_clean', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "for key in ('20260101', '20260216', '20261009'):\n"
        "    print(key, mod.no_data_message(key))\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    env.pop("PYTHONSAFEPATH", None)
    proc = subprocess.run(
        [sys.executable, "-c", code, str(script)],
        cwd=str(outside),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    lines = dict(line.split(" ", 1) for line in proc.stdout.splitlines() if line[:8].isdigit())
    for key in ("20260101", "20260216", "20261009"):
        assert "holiday" in lines[key], (key, lines[key], proc.stderr)
        assert "API lag" not in lines[key]


def test_weekday_holiday_label_without_repo_on_syspath(monkeypatch):
    """chips_monitor.py is launched as a script, so sys.path[0] is
    scripts/news_bridge. A weekday holiday must still say TAIFEX holiday
    after the repo root is removed and the cached src package is dropped.
    pytest itself puts the root on sys.path, which hides the miss."""
    import sys
    root = Path(chips.__file__).resolve().parents[2]
    kept = []
    for entry in sys.path:
        try:
            if entry and Path(entry).resolve() == root:
                continue
        except OSError:
            pass
        kept.append(entry)
    monkeypatch.setattr(sys, "path", kept)
    for name in ("src.market_data.holidays", "src.market_data", "src"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    text = chips.no_data_message("20260101")
    assert "TAIFEX holiday" in text
    assert "API lag" not in text


class _Truncated:
    def __init__(self, exc):
        self._exc = exc
        self.status = 200

    def read(self):
        raise self._exc

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _assert_transport_keeps_vote(base, vote, before):
    state = chips.load_state(chips.state_path_for(base))
    log = _log(base)
    assert vote.read_bytes() == before
    assert state.get("last_decided_date") != "20260814"
    assert "fetch failed" in log.lower()
    assert "no taifex data" not in log.lower()


@pytest.mark.parametrize("exc_type", ["IncompleteRead", "BadStatusLine", "RemoteDisconnected"])
def test_http_exception_on_taifex_keeps_vote_and_exits_nonzero(tmp_path, monkeypatch, exc_type):
    """A cut-off body is a transport failure on the TAIFEX path.

    IncompleteRead and BadStatusLine are HTTPException, not OSError, so
    the generic handler used to treat them as empty data. RemoteDisconnected
    is also an OSError, so it was already a transport failure.
    """
    import http.client
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    exc = getattr(http.client, exc_type)("cut off")
    monkeypatch.setattr(
        chips.urllib.request, "urlopen", lambda *_a, **_k: _Truncated(exc))
    assert chips.run_once(base, "20260814", _FRIDAY) != 0
    _assert_transport_keeps_vote(base, vote, before)


@pytest.mark.parametrize("exc_type", ["IncompleteRead", "BadStatusLine", "RemoteDisconnected"])
def test_http_exception_on_twse_keeps_vote_and_exits_nonzero(tmp_path, monkeypatch, exc_type):
    """Same cut-off, after TAIFEX answered, on the TWSE path."""
    import http.client
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    chips.save_state(chips.state_path_for(base), {"history": {"20260813": 0.0}})
    row = {"Date": "20260814", "ContractCode": "臺股期貨", "Item": "外資及陸資",
           "OpenInterest(Net)": "8,123"}
    exc = getattr(http.client, exc_type)("cut off")

    def fake(req, timeout=None, context=None):
        if "twse.com.tw" in req.full_url:
            return _Truncated(exc)
        return _Body(json.dumps([row]).encode())

    monkeypatch.setattr(chips.urllib.request, "urlopen", fake)
    assert chips.run_once(base, "20260814", _FRIDAY) != 0
    _assert_transport_keeps_vote(base, vote, before)


def _http_error(code, reason):
    def fake(req, timeout=None, context=None):
        raise urllib.error.HTTPError(req.full_url, code, reason, hdrs=None, fp=None)
    return fake


def test_http_404_is_no_data_not_a_transport_failure(tmp_path, monkeypatch):
    """Flipped: a TAIFEX 404 is a fetch failure, not an empty dataset.

    The name is kept so the old no-data assertion is the one that changed.
    A 4xx is never a genuine no-data answer: TAIFEX takes no date, and a
    TWSE holiday is a 200 whose stat is not OK.
    """
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    monkeypatch.setattr(chips.urllib.request, "urlopen", _http_error(404, "Not Found"))
    assert chips.run_once(base, None, _AT_0415) != 0
    assert vote.read_bytes() == before
    state = chips.load_state(chips.state_path_for(base))
    assert not state.get("last_decided_date")
    log = _log(base)
    assert "fetch failed" in log.lower()
    assert "no taifex data" not in log.lower()


@pytest.mark.parametrize("code", [403, 429])
def test_taifex_http_4xx_at_0415_keeps_vote(tmp_path, monkeypatch, code):
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    monkeypatch.setattr(chips.urllib.request, "urlopen", _http_error(code, "denied"))
    assert chips.run_once(base, None, _AT_0415) != 0
    assert vote.read_bytes() == before
    assert not chips.load_state(chips.state_path_for(base)).get("last_decided_date")
    log = _log(base)
    assert "fetch failed" in log.lower()
    assert "no taifex data" not in log.lower()


@pytest.mark.parametrize("code", [403, 429, 404])
def test_twse_http_4xx_keeps_vote(tmp_path, monkeypatch, code):
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    chips.save_state(chips.state_path_for(base), {"history": {"20260813": 0.0}})
    row = {"Date": "20260814", "ContractCode": "臺股期貨", "Item": "外資及陸資",
           "OpenInterest(Net)": "8,123"}

    def fake(req, timeout=None, context=None):
        if "twse.com.tw" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, code, "denied", hdrs=None, fp=None)
        return _Body(json.dumps([row]).encode())

    monkeypatch.setattr(chips.urllib.request, "urlopen", fake)
    assert chips.run_once(base, "20260814", _FRIDAY) != 0
    assert vote.read_bytes() == before
    assert chips.load_state(chips.state_path_for(base)).get("last_decided_date") != "20260814"
    log = _log(base)
    assert "fetch failed" in log.lower()
    assert "no taifex data" not in log.lower()


def test_utf8_bom_200_is_parsed_as_data(monkeypatch):
    """A leading EF BB BF used to pass through json.load. utf-8-sig keeps that."""
    row = {"Date": "20260814", "ContractCode": "臺股期貨", "Item": "外資及陸資",
           "OpenInterest(Net)": "8,123"}
    body = b"\xef\xbb\xbf" + json.dumps([row]).encode("utf-8")
    monkeypatch.setattr(chips.urllib.request, "urlopen", lambda *_a, **_k: _Body(body))
    assert chips.fetch_taifex_tx_foreign_oi() == ("20260814", 8123.0)


def test_non_json_200_is_a_fetch_failure(tmp_path, monkeypatch):
    """A 200 block page is not an empty dataset."""
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    monkeypatch.setattr(
        chips.urllib.request, "urlopen",
        lambda *_a, **_k: _Body(b"<html><body>Access denied</body></html>"))
    assert chips.run_once(base, "20260814", _FRIDAY) != 0
    assert vote.read_bytes() == before
    log = _log(base)
    assert "fetch failed" in log.lower()
    assert "no taifex data" not in log.lower()


@pytest.mark.parametrize("body", [b"", b" \n\t"])
def test_empty_200_on_taifex_keeps_vote_and_exits_nonzero(tmp_path, monkeypatch, body):
    """A zero-byte or whitespace-only 200 is a fetch failure, not no-data."""
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    monkeypatch.setattr(chips.urllib.request, "urlopen", lambda *_a, **_k: _Body(body))
    assert chips.run_once(base, None, _AT_0415) != 0
    assert vote.read_bytes() == before
    assert not chips.load_state(chips.state_path_for(base)).get("last_decided_date")
    log = _log(base)
    assert "fetch failed" in log.lower()
    assert "no taifex data" not in log.lower()


@pytest.mark.parametrize("body", [b"", b" \n\t"])
def test_empty_200_on_twse_keeps_vote_and_exits_nonzero(tmp_path, monkeypatch, body):
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    chips.save_state(chips.state_path_for(base), {"history": {"20260813": 0.0}})
    row = {"Date": "20260814", "ContractCode": "臺股期貨", "Item": "外資及陸資",
           "OpenInterest(Net)": "8,123"}

    def fake(req, timeout=None, context=None):
        if "twse.com.tw" in req.full_url:
            return _Body(body)
        return _Body(json.dumps([row]).encode())

    monkeypatch.setattr(chips.urllib.request, "urlopen", fake)
    assert chips.run_once(base, "20260814", _FRIDAY) != 0
    assert vote.read_bytes() == before
    assert chips.load_state(chips.state_path_for(base)).get("last_decided_date") != "20260814"
    log = _log(base)
    assert "fetch failed" in log.lower()
    assert "no taifex data" not in log.lower()


@pytest.mark.parametrize("body", [
    b'{"message":"Too many requests"}',
    b"null",
    b'[{"ContractCode":"\xe8\x87\xba\xe6\x8c\x87\xe9\x81\xb8\xe6\x93\x87\xe6\xac\x8a","Item":"\xe5\xa4\x96\xe8\xb3\x87\xe5\x8f\x8a\xe9\x99\xb8\xe8\xb3\x87","OpenInterest(Net)":"1","Date":"20260814"}]',
])
def test_taifex_wrong_json_shape_keeps_vote(tmp_path, monkeypatch, body):
    """An error dict, JSON null, or a list with no TX rows is not no-data.

    ``[]`` stays no-data. A non-empty list that never mentions 臺股期貨 is
    treated as the wrong document: the published dataset always includes
    that contract, so keep the vote and retry.
    """
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    monkeypatch.setattr(chips.urllib.request, "urlopen", lambda *_a, **_k: _Body(body))
    assert chips.run_once(base, None, _AT_0415) != 0
    assert vote.read_bytes() == before
    assert not chips.load_state(chips.state_path_for(base)).get("last_decided_date")
    log = _log(base)
    assert "fetch failed" in log.lower()
    assert "no taifex data" not in log.lower()


def test_twse_error_dict_keeps_vote(tmp_path, monkeypatch):
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()
    chips.save_state(chips.state_path_for(base), {"history": {"20260813": 0.0}})
    row = {"Date": "20260814", "ContractCode": "臺股期貨", "Item": "外資及陸資",
           "OpenInterest(Net)": "8,123"}

    def fake(req, timeout=None, context=None):
        if "twse.com.tw" in req.full_url:
            return _Body(b'{"message":"Too many requests"}')
        return _Body(json.dumps([row]).encode())

    monkeypatch.setattr(chips.urllib.request, "urlopen", fake)
    assert chips.run_once(base, "20260814", _FRIDAY) != 0
    assert vote.read_bytes() == before
    assert chips.load_state(chips.state_path_for(base)).get("last_decided_date") != "20260814"
    log = _log(base)
    assert "fetch failed" in log.lower()
    assert "no taifex data" not in log.lower()


@pytest.mark.parametrize("exc", [KeyError("fields"), ValueError("bad cell")])
def test_unexpected_fetch_exception_keeps_vote_and_names_the_type(tmp_path, monkeypatch, exc):
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    before = vote.read_bytes()

    def fake(req, timeout=None, context=None):
        raise exc

    monkeypatch.setattr(chips.urllib.request, "urlopen", fake)
    assert chips.run_once(base, None, _AT_0415) != 0
    assert vote.read_bytes() == before
    assert not chips.load_state(chips.state_path_for(base)).get("last_decided_date")
    log = _log(base)
    assert type(exc).__name__ in log
    assert "no taifex data" not in log.lower()


def test_empty_json_200_is_still_no_data(tmp_path, monkeypatch):
    """A 200 with empty JSON, and a TWSE 200 whose stat is not OK, stay no-data."""
    base = str(tmp_path / "regime_vote.json")
    vote = _seed_vote(base)
    monkeypatch.setattr(chips.urllib.request, "urlopen", lambda *_a, **_k: _Body(b"[]"))
    assert chips.run_once(base, "20260814", _FRIDAY) == 0
    assert not vote.exists()
    assert "no TAIFEX data" in chips.load_state(chips.state_path_for(base))["last_result"]

    vote = _seed_vote(base)
    chips.save_state(chips.state_path_for(base), {"history": {"20260813": 0.0}})
    row = {"Date": "20260814", "ContractCode": "臺股期貨", "Item": "外資及陸資",
           "OpenInterest(Net)": "8,123"}

    def fake(req, timeout=None, context=None):
        if "twse.com.tw" in req.full_url:
            return _Body("{\"stat\":\"很抱歉，沒有符合條件的資料!\"}".encode())
        return _Body(json.dumps([row]).encode())

    monkeypatch.setattr(chips.urllib.request, "urlopen", fake)
    assert chips.run_once(base, "20260814", _FRIDAY) == 0
    assert not vote.exists()


# ── surfacing: check_bridge P2 ───────────────────────────────────────────

def test_w4_fetch_failure_is_p2(tmp_path):
    d = tmp_path / "bridge"
    d.mkdir()
    now = datetime(2026, 8, 25, 6, 0, tzinfo=TPE)
    fresh = "2026-08-25T05:40:00+08:00"
    message = ("TAIFEX fetch failed (network: getaddrinfo failed) "
               "— vote kept, no decision")
    (d / ".chips_state.json").write_text(json.dumps({
        "last_check": fresh,
        "last_result": message,
        "last_fetch_error": message,
        "fetch_error_streak": 2,
    }), encoding="utf-8")
    (d / "monitor_state.json").write_text(json.dumps({
        "last_check": fresh, "last_result": "fresh",
    }), encoding="utf-8")
    (d / "rss_scorer_state.json").write_text(json.dumps({
        "last_check": fresh, "session_net": 0.0, "error_streak": 0,
    }), encoding="utf-8")
    (d / "events.json").write_text(json.dumps({
        "version": 1, "updated_at": fresh, "events": [],
    }), encoding="utf-8")
    (d / "monitor.log").write_text("2026-08-25 05:50:00 TPE | fresh\n", encoding="utf-8")
    (d / "chips_monitor.log").write_text(
        "2026-08-24 16:15:04 TPE | vote trending-up\n"
        "2026-08-24 18:15:04 TPE | no vote\n",
        encoding="utf-8")
    w4 = _w4_status(str(d / "regime_vote.json"), now)
    assert getattr(w4, "fetch_failing", False) is True
    assert w4.fetch_error_streak == 2
    settings = {"news": {
        "signal_path": str(d / "signal.json"),
        "regime_vote_path": str(d / "regime_vote.json"),
        "events_path": str(d / "events.json"),
        "rss_state_file": str(d / "rss_scorer_state.json"),
    }}
    findings, _lines = check_bridge(now, settings, str(tmp_path / "live"))
    p2 = [f.message for f in findings if f.level == "P2"]
    assert any(m.startswith("W4 fetch failed ×2:") and "getaddrinfo failed" in m for m in p2), p2
    assert not any("no TAIFEX data" in m for m in p2)


def test_stale_fetch_failure_reports_stale_not_the_live_fetch(tmp_path):
    """A task that died after a failed fetch is stale. The fetch finding
    is for a bridge that is still checking in."""
    d = tmp_path / "bridge"
    d.mkdir()
    now = datetime(2026, 8, 25, 6, 0, tzinfo=TPE)
    old = (now - timedelta(days=5)).isoformat(timespec="seconds")
    fresh_w2 = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
    message = "TAIFEX fetch failed (network: getaddrinfo failed) — vote kept, no decision"
    (d / ".chips_state.json").write_text(json.dumps({
        "last_check": old, "last_result": message, "last_fetch_error": message,
        "fetch_error_streak": 3,
    }), encoding="utf-8")
    (d / "rss_scorer_state.json").write_text(json.dumps({
        "last_check": (now - timedelta(hours=5)).isoformat(timespec="seconds"),
        "session_net": 0.0, "error_streak": 0,
        "fetch_status": "failed", "fetch_error_streak": 4, "feeds_failed": 13,
        "feeds_ok": 0,
        "last_feed_errors": [{"url": "https://feeds.bloomberg.com/markets/news.rss",
                              "kind": "network", "message": "getaddrinfo failed"}],
    }), encoding="utf-8")
    (d / "monitor_state.json").write_text(json.dumps({
        "last_check": fresh_w2, "last_result": "fresh",
    }), encoding="utf-8")
    (d / "events.json").write_text(json.dumps({
        "version": 1, "updated_at": fresh_w2, "events": [],
    }), encoding="utf-8")
    (d / "monitor.log").write_text(
        f"{(now - timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')} TPE | fresh\n",
        encoding="utf-8")
    (d / "chips_monitor.log").write_text(
        "2026-08-24 16:15:04 TPE | vote trending-up\n"
        "2026-08-24 18:15:04 TPE | no vote\n",
        encoding="utf-8")
    settings = {"news": {
        "signal_path": str(d / "signal.json"),
        "regime_vote_path": str(d / "regime_vote.json"),
        "events_path": str(d / "events.json"),
        "rss_state_file": str(d / "rss_scorer_state.json"),
    }}
    findings, _lines = check_bridge(now, settings, str(tmp_path / "live"))
    p2 = [f.message for f in findings if f.level == "P2"]
    assert any(m.startswith("W4 bridge stale") for m in p2), p2
    assert any(m.startswith("W3 bridge stale") for m in p2), p2
    assert not any(m.startswith("W4 fetch failed") for m in p2), p2
    assert not any(m.startswith("W3 every feed failed") for m in p2), p2
