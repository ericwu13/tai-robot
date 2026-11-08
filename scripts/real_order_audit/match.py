"""Match a bot's session.json trades to the real orders in its own logs.

usage: python match.py <evidence.txt> [--json out.json]

<evidence.txt> is the output of extract.ps1 (lines E|file|logline, H|hour,
S|session.json, D|decisions row). Pure read: prints one verdict per trade.

Signature (see real_order_signature.md):
  REAL leg  = a "REAL ORDER: <BUY|SELL> ..." line followed by
              "REAL ORDER OK: <13-digit seq>" with side == the leg's side,
              sent within [-120 s, +300 s] of the leg's dt.
  PAPER     = neither leg has such an order AND the bot's own log has lines
              in the hour of both legs (coverage), so absence is evidence.
  UNKNOWN   = no matched order and a leg's hour has no log coverage.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta

TS = re.compile(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.\d+)?\]")
ORDER = re.compile(r"REAL ORDER: (BUY|SELL) (\S+) .*sim_price=(-?\d+)")
PARAMS = re.compile(r"REAL ORDER PARAMS: .*newclose=(\d)")
RESULT = re.compile(r"REAL ORDER RESULT: code=(-?\d+) message=(.*)$")
OK = re.compile(r"REAL ORDER OK: (\d{13})")
BAD = re.compile(r"REAL ORDER (FAILED|BLOCKED|ERROR)")
FILL = re.compile(r"REAL FILL \((entry|exit)\): seq=(\d+) price=(\d+).*accepted=(True|False)")
DEPLOY = re.compile(r"Deploying live bot: .* mode=(\w+)")

WIN_BEFORE = timedelta(seconds=120)
WIN_AFTER = timedelta(seconds=300)


def _dt(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip().replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(text.strip()[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            return None


def load(path: str):
    events, hours, session, decisions = [], set(), {}, []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\r\n")
            kind, _, rest = raw.partition("|")
            if kind == "E":
                fname, _, line = rest.partition("|")
                m = TS.search(line)
                if m:
                    events.append((_dt(m.group(1)), fname, line))
            elif kind == "H":
                hours.add(rest.strip())
            elif kind == "S":
                session = json.loads(rest)
            elif kind == "D":
                decisions.append(rest)
    events.sort(key=lambda e: e[0])
    return events, hours, session, decisions


def orders_from(events):
    """One record per REAL ORDER: line, completed by its PARAMS/RESULT/OK."""
    orders, fills, deploys, cur = [], {}, [], None
    by_seq = {}
    for ts, fname, line in events:
        if m := ORDER.search(line):
            cur = {"ts": ts, "side": m.group(1), "symbol": m.group(2),
                   "sim": int(m.group(3)), "newclose": None, "code": None,
                   "seq": "", "ok": False, "failed": "", "file": fname}
            orders.append(cur)
        elif (m := PARAMS.search(line)) and cur:
            cur["newclose"] = int(m.group(1))
        elif (m := RESULT.search(line)) and cur:
            cur["code"] = int(m.group(1))
        elif (m := OK.search(line)):
            seq = m.group(1)
            if seq in by_seq:          # same line seen in debug + cli_stdout
                continue
            if cur and not cur["ok"]:
                cur.update(ok=True, seq=seq)
                by_seq[seq] = cur
        elif (m := BAD.search(line)) and cur and not cur["ok"]:
            cur["failed"] = m.group(1)
        elif m := FILL.search(line):
            fills.setdefault(m.group(2), []).append(
                {"kind": m.group(1), "price": int(m.group(3)),
                 "accepted": m.group(4) == "True", "ts": ts.strftime("%Y-%m-%d %H:%M:%S")})
        elif m := DEPLOY.search(line):
            deploys.append((ts, m.group(1)))
    # The same order logged twice (debug + cli_stdout) yields duplicate
    # records; keep the first per (ts, side, sim).
    uniq, seen = [], set()
    for o in orders:
        key = (o["ts"], o["side"], o["sim"])
        if key in seen:
            if o["ok"]:
                for u in uniq:
                    if (u["ts"], u["side"], u["sim"]) == key and not u["ok"]:
                        u.update(ok=True, seq=o["seq"])
            continue
        seen.add(key)
        uniq.append(o)
    return uniq, fills, deploys


def mode_at(deploys, when):
    mode = ""
    for ts, m in deploys:
        if ts <= when:
            mode = m
    return mode


def pick(orders, used, side, when, want_new):
    best = None
    for i, o in enumerate(orders):
        if i in used or o["side"] != side:
            continue
        if not (when - WIN_BEFORE <= o["ts"] <= when + WIN_AFTER):
            continue
        nc_ok = (o["newclose"] == 0) if want_new else (o["newclose"] in (1, 2, None))
        score = (0 if o["ok"] else 1, 0 if nc_ok else 1, abs((o["ts"] - when).total_seconds()))
        if best is None or score < best[0]:
            best = (score, i)
    return best[1] if best else None


def classify(path: str) -> dict:
    events, hours, session, _ = load(path)
    orders, fills, deploys = orders_from(events)
    trades = (session.get("broker") or {}).get("trades") or []
    used: set[int] = set()
    rows = []
    for idx, t in enumerate(trades):
        side = str(t.get("side") or "").upper()
        e_dt, x_dt = _dt(str(t.get("entry_dt") or "")), _dt(str(t.get("exit_dt") or ""))
        e_side, x_side = ("BUY", "SELL") if side == "LONG" else ("SELL", "BUY")
        legs = {}
        for leg, when, s, new in (("entry", e_dt, e_side, True), ("exit", x_dt, x_side, False)):
            if when is None:
                legs[leg] = {"order": None, "covered": False}
                continue
            j = pick(orders, used, s, when, new)
            o = orders[j] if j is not None else None
            if o is not None and o["ok"]:
                used.add(j)
            legs[leg] = {"order": o, "covered": when.strftime("%Y-%m-%d %H") in hours,
                         "mode": mode_at(deploys, when)}
        e_ok = bool(legs["entry"]["order"] and legs["entry"]["order"]["ok"])
        x_ok = bool(legs["exit"]["order"] and legs["exit"]["order"]["ok"])
        if e_ok and x_ok:
            verdict = "REAL"
        elif e_ok:
            verdict = "REAL_ENTRY_ONLY"
        elif x_ok:
            verdict = "REAL_EXIT_ONLY"
        elif legs["entry"]["covered"] and legs["exit"]["covered"]:
            verdict = "PAPER"
        else:
            verdict = "UNKNOWN"
        ev = {}
        for leg in ("entry", "exit"):
            o = legs[leg]["order"]
            ev[leg] = None if o is None else {
                "sent": o["ts"].strftime("%Y-%m-%d %H:%M:%S"), "side": o["side"],
                "sim": o["sim"], "seq": o["seq"], "ok": o["ok"], "code": o["code"],
                "failed": o["failed"], "file": o["file"],
                "fills": fills.get(o["seq"], []) if o["seq"] else []}
            ev[leg + "_covered"] = legs[leg]["covered"]
            ev[leg + "_mode"] = legs[leg].get("mode", "")
        cur_real = (str(t.get("source") or "") == "real"
                    or bool(t.get("real_entry_price") or t.get("real_exit_price")))
        rows.append({
            "idx": idx, "side": side, "entry_dt": t.get("entry_dt"), "exit_dt": t.get("exit_dt"),
            "entry_price": t.get("entry_price"), "exit_price": t.get("exit_price"),
            "real_entry_price": t.get("real_entry_price"), "real_exit_price": t.get("real_exit_price"),
            "source": t.get("source", ""), "dashboard_real_now": cur_real,
            "verdict": verdict, "evidence": ev,
            "mismatch": cur_real != (verdict != "PAPER" and verdict != "UNKNOWN") or verdict == "UNKNOWN",
        })
    orphans = [{"sent": o["ts"].strftime("%Y-%m-%d %H:%M:%S"), "side": o["side"],
                "sim": o["sim"], "seq": o["seq"], "file": o["file"]}
               for i, o in enumerate(orders) if o["ok"] and i not in used]
    attempts = [o for o in orders if not o["ok"]]
    return {"bot": session.get("bot_name", ""), "trades": len(trades),
            "orders_ok": sum(1 for o in orders if o["ok"]), "orders_not_ok": len(attempts),
            "rows": rows, "orphan_orders": orphans}


def main(argv):
    path = argv[1]
    out = classify(path)
    counts = {}
    for r in out["rows"]:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(f"bot={out['bot']} trades={out['trades']} orders_ok={out['orders_ok']} "
          f"orders_not_ok={out['orders_not_ok']} verdicts={counts} orphans={len(out['orphan_orders'])}")
    for r in out["rows"]:
        if r["verdict"] == "PAPER" and not r["dashboard_real_now"]:
            continue
        e, x = r["evidence"]["entry"], r["evidence"]["exit"]
        print(f"  #{r['idx']:>3} {r['verdict']:<16} {'MISMATCH ' if r['mismatch'] else ''}"
              f"{r['side']:<5} {r['entry_dt']} -> {r['exit_dt']} src={r['source']!r} "
              f"realE={r['real_entry_price']} realX={r['real_exit_price']} "
              f"eseq={(e or {}).get('seq','')}{'' if not e or e['ok'] else '(' + (e['failed'] or 'notOK') + ')'} "
              f"xseq={(x or {}).get('seq','')}{'' if not x or x['ok'] else '(' + (x['failed'] or 'notOK') + ')'}")
    for o in out["orphan_orders"]:
        print(f"  ORPHAN order {o['sent']} {o['side']} sim={o['sim']} seq={o['seq']} ({o['file']})")
    if "--json" in argv:
        with open(argv[argv.index("--json") + 1], "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv)
