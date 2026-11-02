"""Opt-in bot controls for the dashboard (``--enable-control``).

Actions, and only for a process that left a live headless marker.
The marker's pid must equal the lock pid and be strictly alive (access
denied does not count). When the process creation time can be read,
``started_at`` must match it. The GUI live loop does not poll ``STOP``,
so a GUI bot is refused and nothing is written:

- ``stop``  -> the headless ``STOP`` file (``src.live.headless.request_stop``),
  checked every 2 s; the bot runs its normal ``_stop_live()``. A real-money
  bot holding a position closes it at market on the way out.
- ``paper`` -> ``mode_request.json`` when the marker advertises that
  channel, else ``mode_override.json`` (``trading_mode: paper``), consumed
  by the bot. Refused there while a position is open, a fill is pending,
  or a replay is running.
- ``auto``  -> ``mode_request.json`` only. Real orders. The bot must be
  RUNNING, its settings must opt it in, ``allow_live_override`` must be
  true, and the loss limit must be inside the confirmed cap (a per-bot
  value may lower it). A restart returns the bot to its command-line mode.

Nothing here can switch a bot to semi_auto. ``check`` and ``apply`` each
re-read the marker. ``apply`` refuses again immediately before any write,
so a marker that vanishes between the two calls still produces no file.
Every request is then logged (accepted or refused) to the control log.
"""

from __future__ import annotations

import csv
import json
import os
import uuid
import sys
import threading
from datetime import datetime

from scripts.dashboard import snapshot
from scripts.dashboard.snapshot import mode_override_path, mode_request_path, pending
from scripts.monitor.common import TZ_TPE
from src.live.headless import request_stop
from src.live.mode_request import effective_loss_cap

ACTIONS = ("stop", "paper", "auto")
# Lower bound for the real-money daily loss limit (NT$). The ceiling is
# MAX_MODE_SWITCH_LOSS_LIMIT_NTD, possibly lowered per bot. There is no
# separate multi-million ceiling.
LOSS_LIMIT_MIN = 1
REAL_ACK = "實單"
# The bot answers an auto request within seconds (OI_SNAPSHOT_TIMEOUT_S).
# A "checking" result older than this means the bot died or never wrote
# its answer; it stops blocking new requests (the bot itself still refuses
# a second request while one is in flight).
MODE_CHECKING_STALE_S = 120
# Lock held by a live process whose main loop is pumping. A HUNG loop never
# reads STOP; accepting it would stop (and close) whenever the loop
# recovers, possibly hours later, so it is refused.
_ALIVE_STATES = ("RUNNING", "LAGGING", "NO_HEARTBEAT")
# Shown on the page and returned by the API. A GUI bot has no headless
# marker; only the headless deploy polls STOP.
GUI_CONTROL_MESSAGE = "GUI 機器人，請在程式中操作"

_log_lock = threading.Lock()
# Serialises check() + apply(): the HTTP server is threaded, and two
# requests that both pass check() would both write a mode request.
_control_lock = threading.Lock()


class ControlRefused(Exception):
    """A request that must not be applied; the message is shown to the user."""


class LiveSwitchPolicy:
    """What settings.yaml allows this dashboard to request.

    Default is closed: ``allow_live_override`` false, no bot opted in.
    ``loss_caps`` holds only values that lower the confirmed cap.
    """

    def __init__(self, allow_live_override: bool = False, opt_in=(),
                 loss_caps=None):
        self.allow_live_override = allow_live_override is True
        self.opt_in = frozenset(opt_in)
        self.loss_caps = dict(loss_caps or {})

    def cap_for(self, bot_name: str) -> int:
        return effective_loss_cap(self.loss_caps.get(bot_name))


def position_key(summary: dict) -> str:
    """What the stop dialog warned about: "flat" or "SIDE:size"."""
    pos = summary.get("position")
    if not pos:
        return "flat"
    return f"{pos.get('side', '')}:{pos.get('size', '')}"


def _checking_in_progress(result: dict, now: datetime) -> bool:
    if result.get("status") != "checking":
        return False
    try:
        at = datetime.strptime(result.get("at") or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_TPE)
    except ValueError:
        return False
    age = (now - at).total_seconds()
    return -60 <= age < MODE_CHECKING_STALE_S


def _require_live_headless(bot_dir: str) -> None:
    """Refuse unless the marker is a live headless process holding the lock.

    Called from ``check`` and again from ``apply`` immediately before a
    write. Uses the strict pid check (access denied is not alive), not the
    lock-staleness helper. A GUI bot, a stale or reused pid, and a
    malformed marker all take this path, and none of them create a file.
    """
    if not snapshot.headless_live(bot_dir):
        raise ControlRefused(GUI_CONTROL_MESSAGE)


def check(action: str, summary: dict, bot_dir: str, confirm: str,
          position_ack: str = "", loss_limit=None, real_ack: str = "", *,
          now: datetime | None = None,
          policy: LiveSwitchPolicy | None = None) -> None:
    """Raise ControlRefused unless ``action`` may be applied right now."""
    policy = policy if policy is not None else LiveSwitchPolicy()
    if action not in ACTIONS:
        raise ControlRefused("不支援的操作")
    if summary.get("state") == "HUNG":
        raise ControlRefused("機器人主程式卡住，不會讀取控制指令")
    if summary.get("state") not in _ALIVE_STATES:
        raise ControlRefused("機器人目前沒有在執行")
    _require_live_headless(bot_dir)
    now_pending = pending(bot_dir)
    if now_pending["stop"]:
        raise ControlRefused("已送出停止指令，等待機器人停止中")
    result = now_pending.get("result") or {}
    if action in ("paper", "auto"):
        if now_pending.get("request") or _checking_in_progress(
                result, now or datetime.now(TZ_TPE)):
            raise ControlRefused("已有模式切換請求處理中，請等待結果")
        # A queued mode_override.json applies at the next bar and would
        # undo an auto switch made in the meantime.
        if now_pending["mode"]:
            raise ControlRefused("已有模式切換等待套用")
    if action == "stop":
        if confirm != summary.get("bot_name"):
            raise ControlRefused("請輸入機器人名稱以確認停止")
        # The dialog's market-close warning came from a snapshot. If the bot
        # entered or exited since, the viewer must see the new warning first.
        if position_ack != position_key(summary):
            raise ControlRefused("持倉狀態已改變，請重新開啟停止視窗確認")
        return
    if action == "auto":
        # Stop and paper may target a lagging bot. Real orders may not:
        # only a RUNNING loop will read the request while someone is watching.
        if summary.get("state") != "RUNNING":
            raise ControlRefused("只有運作中的機器人才可切換實單")
        # The bot re-checks everything (real account, fill pending, replay,
        # opt-in, loss cap, request age) before it switches.
        if not summary.get("supports_mode_request"):
            raise ControlRefused("此機器人的版本不支援即時切換實單，請先更新機器人")
        if not policy.allow_live_override:
            raise ControlRefused("設定 allow_live_override 為關閉，拒絕切換實單")
        bot_name = summary.get("bot_name") or ""
        if bot_name not in policy.opt_in:
            raise ControlRefused("此機器人未在設定中允許切換實單")
        if confirm != bot_name or real_ack != REAL_ACK:
            raise ControlRefused("請輸入機器人名稱與「實單」以確認")
        cap = policy.cap_for(bot_name)
        if (not isinstance(loss_limit, int) or isinstance(loss_limit, bool)
                or not LOSS_LIMIT_MIN <= loss_limit <= cap):
            raise ControlRefused(f"請輸入有效的每日虧損上限（1–{cap:,} 新台幣）")
        if summary.get("trading_mode") == "auto":
            raise ControlRefused("已經是實單模式")
        if summary.get("position"):
            raise ControlRefused("目前有模擬持倉，請等部位平倉後再切換")
        return
    # paper
    if confirm != "paper":
        raise ControlRefused("缺少確認")
    if summary.get("trading_mode") == "paper":
        raise ControlRefused("已經是模擬模式")
    if summary.get("position"):
        raise ControlRefused("目前有持倉，機器人會拒絕切換；請等部位平倉後再試")


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = f"{path}.{uuid.uuid4().hex}.dashboard.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _mode_request(mode: str, client: str, loss_limit=None) -> dict:
    req = {"request_id": uuid.uuid4().hex, "mode": mode, "requested_by": client,
           "requested_at": datetime.now(TZ_TPE).isoformat(timespec="seconds")}
    if loss_limit is not None:
        req["loss_limit"] = int(loss_limit)
    return req


def apply(action: str, bot_dir: str, client: str, *, live_channel: bool = False,
          loss_limit=None) -> str:
    """Write the control file. Returns the message shown to the user.

    The marker is read again here, after ``check``. A bot that is no longer
    headless between the two calls gets no ``STOP`` and no mode file.
    ``auto`` goes only through the live mode-request channel, where the
    bot checks the real account before switching. ``paper`` uses that
    channel when the bot has it (applies within seconds), else the legacy
    mode_override.json (applied at the next strategy bar).
    """
    if action not in ACTIONS:
        raise ControlRefused("不支援的操作")
    _require_live_headless(bot_dir)
    if action == "stop":
        request_stop(bot_dir, reason=f"dashboard stop from {client}")
        return "已送出停止指令；機器人會在數秒內停止（之後需重新部署才會再啟動）"
    if action == "auto" and live_channel:
        _atomic_write_json(mode_request_path(bot_dir), _mode_request("auto", client, loss_limit))
        return "已送出切換實單請求；機器人正在檢查帳戶持倉，結果會顯示在頁面上"
    if action == "paper" and live_channel:
        _atomic_write_json(mode_request_path(bot_dir), _mode_request("paper", client))
        return "已送出切換模擬指令；數秒內套用"
    if action == "paper":
        _atomic_write_json(mode_override_path(bot_dir), {"trading_mode": "paper"})
        return "已送出切換模擬指令；會在下一根策略 K 棒時套用"
    raise ControlRefused("不支援的操作")


def _cell(value) -> str:
    """Redacted, bounded, and never a spreadsheet formula."""
    text = snapshot.redact_line(str(value or ""))[:200]
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def log_request(log_path: str, *, client: str, bot_id: str, action: str,
                result: str, detail: str) -> None:
    """Append one audit row. Logging must never block or undo a control,
    but a failure to log is reported on stderr (the server's own log)."""
    row = [datetime.now(TZ_TPE).strftime("%Y-%m-%d %H:%M:%S")] + [
        _cell(v) for v in (client, bot_id, action, result, detail)]
    try:
        with _log_lock:
            new = not os.path.isfile(log_path)
            with open(log_path, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time_tpe", "client", "bot", "action", "result", "detail"])
                w.writerow(row)
    except OSError as exc:
        sys.stderr.write(f"dashboard: control log {log_path!r} not written "
                         f"({type(exc).__name__}): {row}\n")


def handle(now, base_dir: str, bot_id: str, action: str, confirm: str,
           client: str, log_path: str, *, position_ack: str = "",
           loss_limit=None, real_ack: str = "", pid_alive_fn=None,
           policy: LiveSwitchPolicy | None = None) -> tuple[bool, str]:
    """Validate, apply and log one control request. ``(ok, message)``."""
    bot_dir = snapshot.find_bot_dir(base_dir, bot_id)
    if not bot_dir:
        log_request(log_path, client=client, bot_id=bot_id, action=action,
                    result="refused", detail="no such bot")
        return False, "找不到這個機器人"
    alive = pid_alive_fn or snapshot.pid_alive
    # Fresh session: a same-size rewrite inside one timestamp tick must
    # not leave a closed position looking open.
    summary = snapshot.bot_summary(now, bot_dir, pid_alive_fn=alive, fresh=True)
    try:
        with _control_lock:
            check(action, summary, bot_dir, confirm, position_ack, loss_limit,
                  real_ack, policy=policy)
            message = apply(action, bot_dir, client,
                            live_channel=bool(summary.get("supports_mode_request")),
                            loss_limit=loss_limit)
    except ControlRefused as exc:
        log_request(log_path, client=client, bot_id=bot_id, action=action,
                    result="refused", detail=str(exc))
        return False, str(exc)
    except OSError as exc:
        log_request(log_path, client=client, bot_id=bot_id, action=action,
                    result="error", detail=f"{type(exc).__name__}: {exc}")
        return False, "寫入控制檔失敗，請稍後再試"
    log_request(log_path, client=client, bot_id=bot_id, action=action,
                result="accepted", detail=message)
    return True, message
