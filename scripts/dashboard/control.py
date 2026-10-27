"""Opt-in bot controls for the dashboard (``--enable-control``).

Two actions, both through channels the bot already polls:

- ``stop``  -> the headless ``STOP`` file (``src.live.headless.request_stop``),
  checked every 2 s; the bot runs its normal ``_stop_live()``. A real-money
  bot holding a position closes it at market on the way out.
- ``paper`` -> ``mode_override.json`` with ``trading_mode: paper``, consumed
  by ``LiveRunner._check_mode_override`` at the next strategy bar and
  applied by ``_apply_mode_switch`` (refused there while a position is open,
  a fill is pending, or a replay is running).

Nothing here can switch a bot UP to semi_auto/auto: a runtime escalation
skips the deploy-time real-position check and loss limit, and the bot's
launcher brings it back to its command-line mode on restart anyway. Real
money stays with the deploy procedure.

Every request is checked against the bot's current files first, then
written atomically, then logged (accepted or refused) to the control log.
"""

from __future__ import annotations

import csv
import json
import os
import threading
from datetime import datetime

from scripts.dashboard import snapshot
from scripts.dashboard.snapshot import mode_override_path, pending
from scripts.monitor.common import TZ_TPE
from src.live.headless import request_stop

ACTIONS = ("stop", "paper")
# Lock held by a live process; a HUNG main loop never polls STOP, but the
# request is still accepted so it applies if the loop recovers.
_ALIVE_STATES = ("RUNNING", "LAGGING", "HUNG", "NO_HEARTBEAT")

_log_lock = threading.Lock()


class ControlRefused(Exception):
    """A request that must not be applied; the message is shown to the user."""


def check(action: str, summary: dict, bot_dir: str, confirm: str) -> None:
    """Raise ControlRefused unless ``action`` may be applied right now."""
    if action not in ACTIONS:
        raise ControlRefused("不支援的操作")
    if summary.get("state") not in _ALIVE_STATES:
        raise ControlRefused("機器人目前沒有在執行")
    now_pending = pending(bot_dir)
    if now_pending["stop"]:
        raise ControlRefused("已送出停止指令，等待機器人停止中")
    if action == "stop":
        if confirm != summary.get("bot_name"):
            raise ControlRefused("請輸入機器人名稱以確認停止")
        return
    # paper
    if confirm != "paper":
        raise ControlRefused("缺少確認")
    if summary.get("trading_mode") == "paper":
        raise ControlRefused("已經是模擬模式")
    if now_pending["mode"]:
        raise ControlRefused("已有模式切換等待套用")
    if summary.get("position"):
        raise ControlRefused("目前有持倉，機器人會拒絕切換；請等部位平倉後再試")


def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".dashboard.tmp"
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


def apply(action: str, bot_dir: str, client: str) -> str:
    """Write the control file. Returns the message shown to the user."""
    if action == "stop":
        request_stop(bot_dir, reason=f"dashboard stop from {client}")
        return "已送出停止指令；機器人會在數秒內停止（之後需重新部署才會再啟動）"
    if action == "paper":   # the only mode this module ever writes
        _atomic_write_json(mode_override_path(bot_dir), {"trading_mode": "paper"})
        return "已送出切換模擬指令；會在下一根策略 K 棒時套用"
    raise ControlRefused("不支援的操作")


def log_request(log_path: str, *, client: str, bot_id: str, action: str,
                result: str, detail: str) -> None:
    """Append one audit row. Logging must never block or undo a control."""
    row = [datetime.now(TZ_TPE).strftime("%Y-%m-%d %H:%M:%S"), client, bot_id,
           action, result, snapshot.redact_line(detail)]
    try:
        with _log_lock:
            new = not os.path.isfile(log_path)
            with open(log_path, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["time_tpe", "client", "bot", "action", "result", "detail"])
                w.writerow(row)
    except OSError:
        pass


def handle(now, base_dir: str, bot_id: str, action: str, confirm: str,
           client: str, log_path: str, *, pid_alive_fn=None) -> tuple[bool, str]:
    """Validate, apply and log one control request. ``(ok, message)``."""
    bot_dir = snapshot.find_bot_dir(base_dir, bot_id)
    if not bot_dir:
        return False, "找不到這個機器人"
    kw = {"pid_alive_fn": pid_alive_fn} if pid_alive_fn else {}
    summary = snapshot.bot_summary(now, bot_dir, **kw)
    try:
        check(action, summary, bot_dir, confirm)
        message = apply(action, bot_dir, client)
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
