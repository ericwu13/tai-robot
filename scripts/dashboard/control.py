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
import re
import subprocess
import sys
import threading
from datetime import datetime

from scripts.dashboard import snapshot
from scripts.dashboard.snapshot import mode_override_path, pending
from scripts.monitor.common import TZ_TPE
from src.live.headless import request_stop

ACTIONS = ("stop", "paper")
# Lock held by a live process whose main loop is pumping. A HUNG loop never
# reads STOP; accepting it would stop (and close) whenever the loop
# recovers, possibly hours later, so it is refused.
_ALIVE_STATES = ("RUNNING", "LAGGING", "NO_HEARTBEAT")
# Only a headless deploy polls STOP (src/live/headless_app.py). A GUI bot
# holds the same kind of lock, so the process command line decides.
_HEADLESS_CMD = re.compile(r"(run_bot_cli|tai_backtest)\S*\s.*\bdeploy\b", re.IGNORECASE)

_log_lock = threading.Lock()


class ControlRefused(Exception):
    """A request that must not be applied; the message is shown to the user."""


def process_command_line(pid) -> str | None:
    """The command line of ``pid``, or None when it cannot be read."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if os.name != "nt":
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True, timeout=15, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def is_headless(cmdline: str | None) -> bool | None:
    """True for a headless ``deploy``, False for anything else, None if unknown."""
    if not cmdline:
        return None
    return bool(_HEADLESS_CMD.search(cmdline))


def position_key(summary: dict) -> str:
    """What the stop dialog warned about: "flat" or "SIDE:size"."""
    pos = summary.get("position")
    if not pos:
        return "flat"
    return f"{pos.get('side', '')}:{pos.get('size', '')}"


def check(action: str, summary: dict, bot_dir: str, confirm: str,
          position_ack: str = "") -> None:
    """Raise ControlRefused unless ``action`` may be applied right now."""
    if action not in ACTIONS:
        raise ControlRefused("不支援的操作")
    if summary.get("state") == "HUNG":
        raise ControlRefused("機器人主程式卡住，不會讀取控制指令")
    if summary.get("state") not in _ALIVE_STATES:
        raise ControlRefused("機器人目前沒有在執行")
    now_pending = pending(bot_dir)
    if now_pending["stop"]:
        raise ControlRefused("已送出停止指令，等待機器人停止中")
    if action == "stop":
        if confirm != summary.get("bot_name"):
            raise ControlRefused("請輸入機器人名稱以確認停止")
        # The dialog's market-close warning came from a snapshot. If the bot
        # entered or exited since, the viewer must see the new warning first.
        if position_ack != position_key(summary):
            raise ControlRefused("持倉狀態已改變，請重新開啟停止視窗確認")
        headless = is_headless(process_command_line(summary.get("pid")))
        if headless is None:
            raise ControlRefused("無法確認機器人的執行方式，未送出停止指令")
        if not headless:
            raise ControlRefused("這是 GUI 部署的機器人，不會讀取停止檔；請在機器人視窗中停止")
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
           pid_alive_fn=None) -> tuple[bool, str]:
    """Validate, apply and log one control request. ``(ok, message)``."""
    bot_dir = snapshot.find_bot_dir(base_dir, bot_id)
    if not bot_dir:
        log_request(log_path, client=client, bot_id=bot_id, action=action,
                    result="refused", detail="no such bot")
        return False, "找不到這個機器人"
    kw = {"pid_alive_fn": pid_alive_fn} if pid_alive_fn else {}
    summary = snapshot.bot_summary(now, bot_dir, **kw)
    try:
        check(action, summary, bot_dir, confirm, position_ack)
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
