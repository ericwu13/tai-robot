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
- The live channel is advertised on the headless marker (``headless.json``
  ``mode_request: 1``), not a separate capabilities file. The dashboard
  accepts it only when that marker's pid, lock pid and ``started_at``
  checks pass.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from .main_heartbeat import atomic_write_json, call_retrying_permission_error

REQUEST_FILENAME = "mode_request.json"
RESULT_FILENAME = "mode_request_result.json"

REQUEST_MODES = ("paper", "auto")

# Confirmed by Eric on 2026-11-02. A per-bot settings value may lower
# this cap and must never raise it.
MAX_MODE_SWITCH_LOSS_LIMIT_NTD = 20_000

# A request older than this, or timestamped more than the future skew
# ahead of the bot's clock, is refused. Missing timestamps are refused.
REQUEST_MAX_AGE_S = 60
REQUEST_FUTURE_SKEW_S = 5

STATUS_CHECKING = "checking"
STATUS_ACCEPTED = "accepted"
STATUS_REFUSED = "refused"

# At most one Discord notice per this many seconds for refused auto requests.
REFUSED_NOTICE_INTERVAL_S = 300.0

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


def effective_loss_cap(per_bot) -> int:
    """The hard cap, or a per-bot value that lowers it. Never raised.

    A missing, non-integer, non-positive, or higher value leaves the
    confirmed cap in place.
    """
    if _is_int(per_bot) and 0 < per_bot < MAX_MODE_SWITCH_LOSS_LIMIT_NTD:
        return int(per_bot)
    return MAX_MODE_SWITCH_LOSS_LIMIT_NTD


def loss_limit_reason(loss_limit, cap: int) -> str | None:
    """Why an auto ``loss_limit`` is refused, or None when it is inside ``cap``."""
    if not _is_int(loss_limit) or loss_limit <= 0:
        return "全自動需要有效的每日虧損上限 loss_limit（正整數）"
    if loss_limit > cap:
        return f"每日虧損上限 {loss_limit:,} 超過允許上限 {cap:,} 新台幣"
    return None


def parse_dashboard_auto_opt_in(raw) -> frozenset[str]:
    """Bot names whose settings value is boolean True. Anything else is off."""
    if not isinstance(raw, dict):
        return frozenset()
    return frozenset(
        str(name) for name, flag in raw.items()
        if flag is True and str(name).strip()
    )


def parse_mode_switch_loss_caps(raw) -> dict[str, int]:
    """Per-bot ceilings that lower the hard cap. Higher values are dropped."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for name, value in raw.items():
        if _is_int(value) and 0 < value < MAX_MODE_SWITCH_LOSS_LIMIT_NTD:
            out[str(name)] = int(value)
    return out


def expiry_reason(requested_at: str, now: datetime) -> str | None:
    """Why ``requested_at`` is too old, too far ahead, or missing.

    ``age > 60`` s is refused (exactly 60 s is still accepted). A timestamp
    more than 5 s in the future is refused (exactly 5 s is accepted).
    A naive timestamp is read in ``now``'s timezone.
    """
    if not isinstance(requested_at, str) or not requested_at.strip():
        return "請求缺少時間 requested_at"
    try:
        at = datetime.fromisoformat(requested_at.strip())
    except ValueError:
        return "請求時間 requested_at 無法解析"
    if at.tzinfo is None:
        at = at.replace(tzinfo=now.tzinfo)
    age = (now - at).total_seconds()
    if age > REQUEST_MAX_AGE_S:
        return "請求已超過 60 秒，已拒絕"
    if age < -REQUEST_FUTURE_SKEW_S:
        return "請求時間超前超過 5 秒，已拒絕"
    return None


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
        self.clock = clock or time.monotonic
        self._handled: deque[str] = deque(maxlen=history)
        self.pending: ModeRequest | None = None
        self.timeout_s: float = 0.0
        self._deadline: float = 0.0
        # AccountMonitor.rights_seq when the query went out; only a reply
        # stored after it answers this request.
        self.rights_seq_at_start: int = 0
        # Final result whose write failed; retried on the next poll so the
        # dashboard is not left showing "checking".
        self.unwritten: tuple[str, dict] | None = None
        self._last_notice: float | None = None

    def seen(self, request_id: str) -> bool:
        return bool(request_id) and request_id in self._handled

    def mark_handled(self, request_id: str) -> None:
        if request_id and request_id not in self._handled:
            self._handled.append(request_id)

    def start(self, req: ModeRequest, timeout_s: float, *, rights_seq: int = 0) -> None:
        self.pending = req
        self.timeout_s = float(timeout_s)
        self._deadline = self.clock() + self.timeout_s
        self.rights_seq_at_start = int(rights_seq)

    def poll(self, snapshot_received: bool) -> str:
        """An answered snapshot wins over the deadline."""
        if self.pending is None:
            return self.IDLE
        if snapshot_received:
            return self.ANSWERED
        if self.clock() >= self._deadline:
            return self.TIMEOUT
        return self.WAITING

    def allow_refused_notice(self) -> bool:
        """Rate limit for Discord notices about refused auto requests."""
        now = self.clock()
        if (self._last_notice is not None
                and now - self._last_notice < REFUSED_NOTICE_INTERVAL_S):
            return False
        self._last_notice = now
        return True

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


def clear_stale_request(bot_dir: str) -> None:
    """Drop a request/result left from a previous run (deploy start).

    The headless marker is not touched here: it is rewritten after this
    deploy holds the lock, and a stale one fails the pid check.
    """
    for path in (request_path(bot_dir), result_path(bot_dir)):
        _remove(path)
