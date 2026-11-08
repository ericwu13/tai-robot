"""工作流程 view (``GET /api/workflows``): one row per news-bridge workflow.

W1 calendar, W2 cross-market, W3 RSS, W4 chips, W5 manual tap — status,
schedule, last run, next due and last result.  The source of truth per
row, in order:

1. ``<bridge dir>/runs/<W>.jsonl`` (``src/news/bridge_runs``) — every
   pass with ok/exit/host.  Bridges on an older checkout do not write it.
2. The bridge's own liveness stamp (events.json ``updated_at``,
   monitor_state / rss state / .chips_state ``last_check``).

The run log carries the writing host, so a dashboard serving another
machine's — or a frozen — bridge directory says so instead of looking
healthy-but-old.  Read-only; files arrive through the caller's
stat-gated readers.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

from scripts.dashboard.bridge_panels import (
    CHIPS_STATE_NAME, W2_STATE_NAME, _beside, _str, _ts, w2_stale, w2_window_start)

_TZ_TPE = timezone(timedelta(hours=8))

W1_STALE_S = 26 * 3600            # daily 10:00 + slack
W3_STALE_S = 70 * 60              # every 30 min: two missed passes
W4_SLOTS = ((4, 15), (16, 15), (18, 15), (20, 15), (22, 15))
W4_GRACE_S = 20 * 60
RUN_TAIL = 800                    # ~a day of W2 passes

WORKFLOWS = (
    ("W1", "行事曆", "每日 10:00", "events.json：重大事件前暫停交易"),
    ("W2", "跨市場報價", "每 2 分鐘（08:00–05:00）", "美股半導體／亞歐指數，達門檻寫 risk_off 與投票"),
    ("W3", "RSS 新聞評分", "每 30 分鐘", "Gemini 評分新聞，盤別累計達門檻投票"),
    ("W4", "籌碼（外資未平倉）", "每日 04:15、16:15–22:15 每 2 小時", "TAIFEX 外資台指淨未平倉變化投票"),
    ("W5", "手動訊號", "手動（Discord 連結）", "手動寫入 risk_off / deploy / clear"),
)

# Run-log text is free-form (exception messages); local paths stay on the
# server like every other path the dashboard knows.
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/(?:Users|home|tmp|var|opt|mnt)/)[^\s'\"]*")


def scrub_paths(text: str) -> str:
    """``C:\\a\\b\\x.json`` → ``…x.json`` (keeps the file name, drops the folder)."""
    return _PATH.sub(lambda m: "…" + re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1], text)


STATUS_LABELS = {"ok": "正常", "stale": "停滯", "bad": "失敗", "off": "未部署",
                 "unknown": "狀態不明", "idle": "閒置時段"}


def _floor_to(now: datetime, minutes: int) -> datetime:
    return now.replace(minute=now.minute - now.minute % minutes, second=0, microsecond=0)


def next_due(workflow: str, now: datetime) -> datetime | None:
    now = now.astimezone(_TZ_TPE)
    if workflow == "W1":
        due = now.replace(hour=10, minute=0, second=0, microsecond=0)
        return due if due > now else due + timedelta(days=1)
    if workflow == "W2":
        due = _floor_to(now, 2) + timedelta(minutes=2)
        if w2_window_start(due) is None:
            due = due.replace(hour=8, minute=0)
        return due
    if workflow == "W3":
        return _floor_to(now, 30) + timedelta(minutes=30)
    if workflow == "W4":
        for day in (0, 1):
            base = now + timedelta(days=day)
            for h, m in W4_SLOTS:
                due = base.replace(hour=h, minute=m, second=0, microsecond=0)
                if due > now:
                    return due
    return None


def recent_w4_slots(now: datetime, n: int = 2) -> list[datetime]:
    """The *n* most recent W4 slots at or before *now*, newest first."""
    now = now.astimezone(_TZ_TPE)
    slots = []
    for day in (0, -1, -2):
        base = now + timedelta(days=day)
        for h, m in W4_SLOTS:
            slot = base.replace(hour=h, minute=m, second=0, microsecond=0)
            if slot <= now:
                slots.append(slot)
    return sorted(slots, reverse=True)[:n]


def _stale(workflow: str, now: datetime, last: datetime | None) -> tuple[bool, bool]:
    """``(stale, idle)``."""
    if workflow == "W2":
        return w2_stale(now, last)
    if last is None:
        return True, False
    age = (now - last).total_seconds()
    if workflow == "W1":
        return age > W1_STALE_S, False
    if workflow == "W3":
        return age > W3_STALE_S, False
    if workflow == "W4":
        # Missed the latest slot (after its grace), or the one before it.
        latest, previous = recent_w4_slots(now)
        slack = timedelta(minutes=5)
        missed_latest = (last < latest - slack
                         and (now - latest).total_seconds() > W4_GRACE_S)
        return missed_latest or last < previous - slack, False
    return False, False


def _files(paths: dict) -> dict:
    """Where each workflow's liveness stamp and run log live."""
    signal = paths.get("signal_path") or ""
    vote = paths.get("regime_vote_path") or ""
    rss = paths.get("rss_state_file") or ""
    events = paths.get("events_path") or ""
    events_guess = False
    if not events and (signal or vote):
        events, events_guess = _beside(signal or vote, "events.json"), True
    dirs = {"W1": os.path.dirname(events) if events else "",
            "W2": os.path.dirname(signal) if signal else "",
            "W3": os.path.dirname(rss) if rss else "",
            "W4": os.path.dirname(vote) if vote else "",
            "W5": os.path.dirname(signal) if signal else ""}
    return {"dirs": dirs, "events": events, "events_guess": events_guess,
            "w2_state": _beside(signal, W2_STATE_NAME), "w3_state": rss,
            "w4_state": _beside(vote, CHIPS_STATE_NAME)}


def _stamp(workflow: str, files: dict, read_json) -> dict:
    """Liveness from the bridge's own files: ``{last, result, error, failing}``."""
    out = {"last": None, "result": "", "error": "", "failing": False, "found": False}
    if workflow == "W1" and files["events"]:
        data = read_json(files["events"])
        if isinstance(data, dict):
            out["found"] = True
            out["last"] = _ts(data.get("updated_at"))
            events = data.get("events")
            out["result"] = (f"{len(events)} 筆事件" if isinstance(events, list) else "")
    elif workflow in ("W2", "W4"):
        path = files["w2_state" if workflow == "W2" else "w4_state"]
        data = read_json(path) if path else None
        if isinstance(data, dict):
            out["found"] = True
            out["last"] = _ts(data.get("last_check"))
            out["result"] = _str(data.get("last_result"))
    elif workflow == "W3" and files["w3_state"]:
        data = read_json(files["w3_state"])
        if isinstance(data, dict):
            out["found"] = True
            out["last"] = _ts(data.get("last_check"))
            streak = data.get("error_streak")
            if isinstance(streak, int) and not isinstance(streak, bool) and streak > 0:
                out["failing"] = True
                out["error"] = f"連續 {streak} 輪評分失敗：{_str(data.get('last_error'), 200)}"
            n = data.get("last_articles_scored")
            net = data.get("session_net")
            bits = []
            if isinstance(n, int):
                bits.append(f"評分 {n} 則")
            if isinstance(net, (int, float)) and not isinstance(net, bool):
                bits.append(f"盤別累計 {net:+.2f}")
            out["result"] = "・".join(bits)
    return out


def _runs(workflow: str, files: dict, read_runs) -> list[dict]:
    d = files["dirs"].get(workflow)
    if not d or read_runs is None:
        return []
    try:
        from src.news import bridge_runs
    except Exception:  # noqa: BLE001
        return []
    return bridge_runs.parse_runs("\n".join(read_runs(str(bridge_runs.runs_path(d, workflow))) or []))


def _row(workflow: str, name: str, schedule: str, desc: str, now: datetime,
         files: dict, read_json, read_runs, redact) -> dict:
    runs = _runs(workflow, files, read_runs)
    stamp = _stamp(workflow, files, read_json)
    last_run = runs[-1] if runs else None
    run_at = _ts(last_run.get("finished_at")) if last_run else None
    last = max((t for t in (run_at, stamp["last"]) if t), default=None)
    day_ago = now - timedelta(days=1)
    recent = [r for r in runs if (_ts(r.get("finished_at")) or day_ago) > day_ago]
    row = {
        "id": workflow, "name": name, "schedule": schedule, "desc": desc,
        "last_run": last.isoformat(timespec="seconds") if last else "",
        "age_s": int((now - last).total_seconds()) if last else None,
        "next_due": "", "result": "", "error": "", "host": "",
        "runs_24h": len(recent), "fails_24h": sum(1 for r in recent if not r.get("ok")),
        "source": "runs" if runs else "state" if stamp["found"] else "none",
        "note": "",
    }
    due = next_due(workflow, now)
    row["next_due"] = due.isoformat(timespec="seconds") if due else ""
    if last_run:
        row["host"] = _str(last_run.get("host"), 64)
        row["result"] = redact(_str(last_run.get("result")))
        if not last_run.get("ok"):
            row["error"] = redact(_str(last_run.get("error")) or
                                  f"結束代碼 {last_run.get('exit_code')}")
    if stamp["result"] and (not row["result"] or (stamp["last"] and run_at
                                                  and stamp["last"] > run_at)):
        row["result"] = redact(stamp["result"])
    if stamp["failing"]:
        row["error"] = redact(stamp["error"])
    if workflow == "W1" and files["events_guess"] and stamp["found"]:
        row["note"] = "未設定 news.events_path，讀取橋接目錄的 events.json"

    if workflow == "W5":
        row["status"] = "ok" if runs else "off"
        if not runs:
            row["note"] = "此主機沒有 n8n，手動訊號未部署；需要時在橋接主機執行 --force-fire"
    elif not runs and not stamp["found"]:
        row["status"] = "unknown"
    else:
        stale, idle = _stale(workflow, now, last)
        if (last_run and not last_run.get("ok")) or stamp["failing"]:
            row["status"] = "bad"
        elif stale:
            row["status"] = "stale"
        elif idle:
            row["status"] = "idle"
        else:
            row["status"] = "ok"
    row["status_label"] = STATUS_LABELS[row["status"]]
    return row


def workflows(now: datetime, paths: dict | None, read_json, read_runs=None,
              host: str = "", redact=lambda s: s) -> dict:
    paths = paths or {}
    if not any(paths.get(k) for k in ("signal_path", "regime_vote_path",
                                      "rss_state_file", "events_path")):
        return {"available": False, "now": now.isoformat(timespec="seconds"),
                "host": host, "rows": [], "data_hosts": [], "foreign_host": False,
                "all_stale": False}
    files = _files(paths)
    clean = lambda text: scrub_paths(redact(text))  # noqa: E731
    rows = [_row(w, name, sched, desc, now, files, read_json, read_runs, clean)
            for w, name, sched, desc in WORKFLOWS]
    data_hosts = sorted({r["host"] for r in rows if r["host"]})
    live = [r for r in rows if r["id"] != "W5" and r["status"] != "unknown"]
    return {
        "available": True,
        "now": now.isoformat(timespec="seconds"),
        "host": host,
        "data_hosts": data_hosts,
        "foreign_host": bool(host) and any(h.lower() != host.lower() for h in data_hosts),
        "all_stale": bool(live) and all(r["status"] == "stale" for r in live),
        "rows": rows,
    }
