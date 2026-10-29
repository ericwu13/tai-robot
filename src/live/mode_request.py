"""Dashboard-requested live trading-mode switch (paper <-> auto).

The dashboard writes ``mode_request.json`` into the bot directory. The
headless bot polls it every 2 s (``DeployDriver._poll`` ->
``BacktestApp._service_mode_request``), consumes it once, and answers in
``mode_request_result.json``. ``auto`` places REAL orders, so the bot
enforces every safety check itself, whatever the dashboard checked.

This module holds the pure parts — request parsing, the gate list, the
in-flight state machine, and the file contract. run_backtest.py owns
only the COM query and the apply.

File contract (the dashboard is built against it):

- ``mode_request.json`` (dashboard, atomic):
  ``{"request_id", "mode": "auto"|"paper", "loss_limit": int > 0 for
  auto, "requested_by", "requested_at"}``
- ``mode_request_result.json`` (bot, tmp + os.replace):
  ``{"request_id", "mode", "status": "checking"|"accepted"|"refused",
  "reason", "at"}``
- ``capabilities.json`` (headless app, after a successful deploy):
  ``{"mode_request": 1, "pid", "version"}``
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable

from .main_heartbeat import atomic_write_json, call_retrying_permission_error

REQUEST_FILENAME = "mode_request.json"
RESULT_FILENAME = "mode_request_result.json"
CAPABILITIES_FILENAME = "capabilities.json"
CAPABILITY_MODE_REQUEST = 1

REQUEST_MODES = ("paper", "auto")

STATUS_CHECKING = "checking"
STATUS_ACCEPTED = "accepted"
STATUS_REFUSED = "refused"

# A request is a handful of short fields. Anything larger is not one.
MAX_REQUEST_BYTES = 16 * 1024

_REQUEST_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,128}$")

_MODE_ZH = {"paper": "模擬", "semi_auto": "半自動", "auto": "全自動"}


def mode_label(mode: str) -> str:
    return _MODE_ZH.get(mode, mode)


class ModeRequestError(ValueError):
    """A request file that cannot be acted on. ``reason`` is zh-TW."""

    def __init__(self, reason: str, request_id: str = "", mode: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.request_id = request_id
        self.mode = mode


@dataclass(frozen=True)
class ModeRequest:
    request_id: str
    mode: str
    loss_limit: int | None
    requested_by: str = ""
    requested_at: str = ""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_request(text: str) -> ModeRequest:
    """Validate one request file body. Raises ``ModeRequestError``."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise ModeRequestError("請求檔格式錯誤（不是有效的 JSON）")
    if not isinstance(data, dict):
        raise ModeRequestError("請求檔格式錯誤（不是 JSON 物件）")

    rid = data.get("request_id")
    request_id = rid if isinstance(rid, str) and _REQUEST_ID_RE.match(rid) else ""
    raw_mode = data.get("mode")
    mode = raw_mode if isinstance(raw_mode, str) else ""
    if not request_id:
        raise ModeRequestError("請求缺少有效的 request_id", "", mode)
    if mode not in REQUEST_MODES:
        raise ModeRequestError(
            f"未知的交易模式：{raw_mode!r}（僅接受 paper / auto）",
            request_id, mode)

    meta = {}
    for key in ("requested_by", "requested_at"):
        value = data.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ModeRequestError(f"請求欄位 {key} 必須是字串", request_id, mode)
        meta[key] = value[:128]

    loss_limit = None
    if mode == "auto":
        raw_limit = data.get("loss_limit")
        if not _is_int(raw_limit) or raw_limit <= 0:
            raise ModeRequestError(
                f"全自動需要有效的每日虧損上限 loss_limit（正整數），收到 {raw_limit!r}",
                request_id, mode)
        loss_limit = int(raw_limit)
    return ModeRequest(
        request_id=request_id,
        mode=mode,
        loss_limit=loss_limit,
        requested_by=meta["requested_by"],
        requested_at=meta["requested_at"],
    )


def request_path(bot_dir: str) -> str:
    return os.path.join(bot_dir, REQUEST_FILENAME)


def result_path(bot_dir: str) -> str:
    return os.path.join(bot_dir, RESULT_FILENAME)


def capabilities_path(bot_dir: str) -> str:
    return os.path.join(bot_dir, CAPABILITIES_FILENAME)


def _remove(path: str, *, sleep_fn=None) -> bool:
    """Delete ``path``; True when it is gone. Retries a sharing violation."""
    try:
        call_retrying_permission_error(lambda: os.remove(path), sleep_fn=sleep_fn)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def consume_request(bot_dir: str, *, sleep_fn=None
                    ) -> tuple[ModeRequest | None, ModeRequestError | None]:
    """Read and delete the request file so it is processed once.

    ``(None, None)``: no request (or it could not be read this poll; the
    file stays for the next poll). ``(None, err)``: a malformed request,
    already deleted. ``(req, None)``: a valid request, already deleted.
    A file that cannot be deleted is still returned — the caller's
    request_id de-duplication keeps it from running twice.
    """
    path = request_path(bot_dir)
    if not os.path.isfile(path):
        return None, None
    try:
        def _read() -> bytes:
            with open(path, "rb") as fh:
                return fh.read(MAX_REQUEST_BYTES + 1)
        raw = call_retrying_permission_error(_read, sleep_fn=sleep_fn)
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, None
    _remove(path, sleep_fn=sleep_fn)
    if len(raw) > MAX_REQUEST_BYTES:
        return None, ModeRequestError("請求檔過大")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, ModeRequestError("請求檔編碼錯誤（需要 UTF-8）")
    try:
        return parse_request(text), None
    except ModeRequestError as err:
        return None, err


@dataclass(frozen=True)
class SwitchState:
    """What the gates look at. Built fresh by the glue on every check."""
    current_mode: str
    has_runner: bool
    sim_position_open: bool
    fill_pending: bool
    fill_poller_active: bool
    replaying: bool
    order_lib_ready: bool = False
    has_futures_account: bool = False
    guard_halted: bool = False
    guard_paused: bool = False
    link_entries_blocked: bool = False


def gate_reason(req: ModeRequest, state: SwitchState) -> str | None:
    """First reason to refuse ``req`` in ``state`` (zh-TW), or None."""
    if not state.has_runner:
        return "機器人未在執行（沒有即時 runner）"
    if req.mode not in REQUEST_MODES:
        return f"未知的交易模式：{req.mode!r}"
    if state.current_mode == req.mode:
        return f"已經是{mode_label(req.mode)}模式"
    if state.sim_position_open:
        return "模擬部位未平倉，請先平倉再切換"
    if state.fill_pending or state.fill_poller_active:
        return "真實委託等待成交確認中，請稍後再試"
    if state.replaying:
        return "歷史資料回放／重新載入中，請稍後再試"
    if req.mode == "auto":
        if not state.order_lib_ready:
            return "下單服務無法使用（COM 未連線或未登入）"
        if not state.has_futures_account:
            return "尚未取得期貨帳號，無法送出實單"
        if not _is_int(req.loss_limit) or req.loss_limit <= 0:
            return "全自動需要有效的每日虧損上限 loss_limit（正整數）"
        if state.guard_halted:
            return "實單系統已停止（成交確認逾時），需人工確認後重新部署"
        if state.guard_paused:
            return "今日已達每日虧損上限，實單已暫停"
        if state.link_entries_blocked:
            return "報價／回報連線未恢復，實單進場暫停中，請稍後再試"
    return None


def format_positions(positions: Iterable[dict]) -> str:
    """Open rows as ``多 x1 TXFD0`` — product, side and qty only."""
    parts = []
    for p in positions or ():
        try:
            qty = int(p.get("qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue
        side = "多" if p.get("side") == "B" else "空"
        product = str(p.get("product", "") or "?")
        parts.append(f"{side} x{qty} {product}")
    return "、".join(parts) or "（無法列出）"


class ModeRequestTracker:
    """One in-flight auto request plus the request_ids already handled.

    ``clock`` is monotonic seconds (injected by tests).
    """

    WAITING = "waiting"
    ANSWERED = "answered"
    TIMEOUT = "timeout"
    IDLE = "idle"

    def __init__(self, *, clock: Callable[[], float] | None = None,
                 history: int = 64):
        self._clock = clock or time.monotonic
        self._handled: deque[str] = deque(maxlen=history)
        self.pending: ModeRequest | None = None
        self.timeout_s: float = 0.0
        self._deadline: float = 0.0

    def seen(self, request_id: str) -> bool:
        return bool(request_id) and request_id in self._handled

    def mark_handled(self, request_id: str) -> None:
        if request_id and request_id not in self._handled:
            self._handled.append(request_id)

    def start(self, req: ModeRequest, timeout_s: float) -> None:
        self.pending = req
        self.timeout_s = float(timeout_s)
        self._deadline = self._clock() + self.timeout_s

    def poll(self, snapshot_received: bool) -> str:
        """An answered snapshot wins over the deadline."""
        if self.pending is None:
            return self.IDLE
        if snapshot_received:
            return self.ANSWERED
        if self._clock() >= self._deadline:
            return self.TIMEOUT
        return self.WAITING

    def finish(self) -> ModeRequest | None:
        req, self.pending = self.pending, None
        return req


def result_payload(request_id: str, mode: str, status: str, reason: str,
                   at: str) -> dict:
    return {
        "request_id": request_id,
        "mode": mode,
        "status": status,
        "reason": reason,
        "at": at,
    }


def _require_bot_dir(bot_dir: str) -> None:
    # The bot dir exists once the runner holds its lock. Never create one:
    # a wrong path would plant a phantom bot directory.
    if not bot_dir or not os.path.isdir(bot_dir):
        raise FileNotFoundError(f"bot directory does not exist: {bot_dir!r}")


def write_result(bot_dir: str, payload: dict) -> None:
    _require_bot_dir(bot_dir)
    atomic_write_json(result_path(bot_dir), payload)


def capabilities_payload(pid: int, version: str) -> dict:
    return {"mode_request": CAPABILITY_MODE_REQUEST, "pid": int(pid),
            "version": version}


def write_capabilities(bot_dir: str, pid: int, version: str) -> None:
    _require_bot_dir(bot_dir)
    atomic_write_json(capabilities_path(bot_dir), capabilities_payload(pid, version))


def clear_stale_request(bot_dir: str) -> None:
    """Drop a request/result left from a previous run (deploy start).

    ``capabilities.json`` goes too: it is rewritten after this deploy
    succeeds, and a stale one must not advertise controls meanwhile.
    """
    for path in (request_path(bot_dir), result_path(bot_dir),
                 capabilities_path(bot_dir)):
        _remove(path)
