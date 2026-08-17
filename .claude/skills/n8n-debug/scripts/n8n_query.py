"""Read-only n8n diagnostics for the tai-robot news bridge.

Every command works without the n8n UI, REST API key, or user input —
it reads the n8n SQLite DB (immutable ?mode=ro URI), the event log, and
the Windows service state.  Safe to run while the n8n service is up.

Usage (always with the SYSTEM python so results match the service env):
    C:/Python313/python.exe .claude/skills/n8n-debug/scripts/n8n_query.py status
    ... n8n_query.py execs [--workflow W3] [--errors] [--limit 20]
    ... n8n_query.py detail 4923 [--grep PATTERN | --all]
    ... n8n_query.py events [--pattern failed] [--tail 40]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB_PATH = Path.home() / ".n8n" / "database.sqlite"
EVENT_LOG = Path.home() / ".n8n" / "n8nEventLog.log"
HEALTH_URL = "http://localhost:5678/healthz"

# Markers that pull error evidence out of n8n's flattened execution JSON.
ERROR_MARKERS = ("error", "traceback", "command failed", "not recognized",
                 "no module", "modulenotfound", "exit code", "enoent",
                 "timed out", "econnrefused")


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"n8n DB not found at {DB_PATH} — is N8N_USER_FOLDER set to "
                 f"C:\\Users\\eric8 (DB lands in ~/.n8n)?")
    return sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)


def cmd_status(_args) -> None:
    # 1. Windows service state
    try:
        out = subprocess.run(["sc.exe", "query", "n8n-service"],
                             capture_output=True, text=True, timeout=10).stdout
        state = next((ln.strip() for ln in out.splitlines() if "STATE" in ln),
                     "STATE line not found")
        print(f"service : {state}")
        if "PAUSED" in state:
            print("  !! NSSM PAUSED = app crash-looped and NSSM gave up. "
                  "Fix the cause, then nssm stop + nssm start (admin).")
    except Exception as e:  # noqa: BLE001
        print(f"service : query failed ({e})")

    # 2. HTTP healthz
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            print(f"healthz : {resp.status} {resp.read().decode()[:60]}")
    except Exception as e:  # noqa: BLE001
        print(f"healthz : UNREACHABLE ({type(e).__name__}) — service down or "
              f"port conflict (check n8n-service.log for 'port 5678 is "
              f"already in use')")

    # 3. Workflows + their latest execution
    con = _connect()
    cols = [r[1] for r in con.execute("pragma table_info(workflow_entity)")]
    arch = ", isArchived" if "isArchived" in cols else ""
    print("\nworkflows (active=1 means its trigger is registered):")
    for row in con.execute(
            f"select id, name, active{arch} from workflow_entity order by name"):
        wid, name, active = row[0], row[1], row[2]
        archived = row[3] if arch else 0
        last = con.execute(
            "select id, status, startedAt from execution_entity "
            "where workflowId = ? order by id desc limit 1", (wid,)).fetchone()
        last_txt = f"last exec #{last[0]} {last[1]} @ {last[2]}" if last else "never executed"
        flag = "ARCHIVED" if archived else ("active" if active else "INACTIVE")
        print(f"  [{flag:8}] {name}  ({wid})  {last_txt}")


def cmd_execs(args) -> None:
    con = _connect()
    where, params = [], []
    if args.workflow:
        where.append("w.name like ?")
        params.append(f"%{args.workflow}%")
    if args.errors:
        where.append("e.status = 'error'")
    sql = ("select e.id, w.name, e.status, e.mode, e.startedAt, e.stoppedAt "
           "from execution_entity e join workflow_entity w on w.id = e.workflowId ")
    if where:
        sql += "where " + " and ".join(where) + " "
    sql += "order by e.id desc limit ?"
    params.append(args.limit)
    rows = con.execute(sql, params).fetchall()
    if not rows:
        print("no executions match")
        return
    for eid, name, status, mode, started, stopped in rows:
        print(f"#{eid}  {status:7} {mode:8} {started} .. {stopped or '-'}  {name}")
    print("\ntimes are UTC (TPE = +8).  'trigger' mode = the cron fired; "
          "'manual' = someone clicked Execute.")


def cmd_detail(args) -> None:
    con = _connect()
    head = con.execute(
        "select e.status, e.mode, e.startedAt, w.name from execution_entity e "
        "join workflow_entity w on w.id = e.workflowId where e.id = ?",
        (args.exec_id,)).fetchone()
    if head is None:
        sys.exit(f"execution {args.exec_id} not found")
    print(f"execution #{args.exec_id}: {head[3]} — {head[0]} ({head[1]}) @ {head[2]} UTC\n")

    raw = con.execute("select data from execution_data where executionId = ?",
                      (args.exec_id,)).fetchone()
    if raw is None:
        sys.exit("no execution_data row (pruned?) — check n8nEventLog instead")
    text = raw[0] if isinstance(raw[0], str) else raw[0].decode("utf-8", "replace")

    # n8n stores a flattened pointer-array JSON; the evidence lives in its
    # string elements.  json.loads already restores real newlines — do NOT
    # post-process backslashes: Windows paths legitimately contain \n
    # sequences (scripts\news_bridge, C:\Users\eric8\n8n-bridge).
    try:
        strings = [s for s in json.loads(text) if isinstance(s, str)]
    except ValueError:
        strings = [text]

    if args.grep:
        needles = [args.grep.lower()]
    elif args.all:
        needles = None
    else:
        needles = list(ERROR_MARKERS)

    shown = 0
    for s in strings:
        if needles is not None and not any(n in s.lower() for n in needles):
            continue
        if len(s) < 8:      # skip keys/ids noise
            continue
        print("─" * 60)
        print(s[:3000])
        shown += 1
        if shown >= args.max_blocks:
            print(f"… (more blocks suppressed, raise --max-blocks)")
            break
    if shown == 0:
        print("no matching strings — run with --all to dump everything, or "
              "--grep <word> (e.g. --grep stdout, --grep taifex)")


def cmd_events(args) -> None:
    if not EVENT_LOG.exists():
        sys.exit(f"event log not found at {EVENT_LOG}")
    lines = EVENT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    pat = (args.pattern or "").lower()
    hits = []
    for ln in lines:
        if '"$$EventMessageConfirm"' in ln:   # heartbeat noise
            continue
        if pat and pat not in ln.lower():
            continue
        try:
            ev = json.loads(ln)
            payload = ev.get("payload", {})
            hits.append(f"{ev.get('ts', '?')}  {ev.get('eventName', '?')}  "
                        f"exec={payload.get('executionId', '-')}  "
                        f"{payload.get('workflowName', '')}")
        except ValueError:
            hits.append(ln[:200])
    for ln in hits[-args.tail:]:
        print(ln)
    if not hits:
        print("no matching events (rotated files: n8nEventLog-1/2/3.log)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="service + healthz + workflow/exec overview")

    p = sub.add_parser("execs", help="recent executions")
    p.add_argument("--workflow", help="filter by workflow name substring")
    p.add_argument("--errors", action="store_true", help="failed only")
    p.add_argument("--limit", type=int, default=15)

    p = sub.add_parser("detail", help="per-node evidence from one execution")
    p.add_argument("exec_id", type=int)
    p.add_argument("--grep", help="custom marker instead of error markers")
    p.add_argument("--all", action="store_true", help="dump all string blocks")
    p.add_argument("--max-blocks", type=int, default=8)

    p = sub.add_parser("events", help="event-bus log (did the cron fire?)")
    p.add_argument("--pattern", help="substring filter, e.g. failed / W3 / success")
    p.add_argument("--tail", type=int, default=40)

    args = ap.parse_args()
    {"status": cmd_status, "execs": cmd_execs,
     "detail": cmd_detail, "events": cmd_events}[args.cmd](args)


if __name__ == "__main__":
    main()
