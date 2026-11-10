"""Apply audited real/paper verdicts to a COPY of a bot's session.json.

usage: python patch.py <session.json> <match.json> <out_session.json> <out_audit.json>
       [--override IDX=VERDICT ...]

Changes only real-vs-paper fields:
  REAL / REAL_ENTRY_ONLY  -> source = "real"
  PAPER                   -> source = "paper"; planted real_entry_price /
                             real_exit_price (+ their dt, real_exit_estimated)
                             cleared
  anything else           -> unchanged
Each trade is matched by index AND (side, entry_dt, exit_dt); a mismatch
aborts. Every change is written to the audit file with old -> new values.
"""
from __future__ import annotations

import json
import sys

PAPER_CLEAR = {"real_entry_price": 0, "real_entry_dt": "", "real_exit_price": 0,
               "real_exit_dt": "", "real_exit_estimated": False}


def main(argv):
    sess_path, match_path, out_path, audit_path = argv[1:5]
    overrides = {}
    if "--override" in argv:
        for item in argv[argv.index("--override") + 1:]:
            i, _, v = item.partition("=")
            overrides[int(i)] = v
    with open(sess_path, encoding="utf-8") as fh:
        sess = json.load(fh)
    with open(match_path, encoding="utf-8") as fh:
        match = json.load(fh)
    trades = sess["broker"]["trades"]
    changes = []
    for row in match["rows"]:
        i = row["idx"]
        t = trades[i]
        key = (str(t.get("side")).upper(), t.get("entry_dt"), t.get("exit_dt"))
        if key != (row["side"], row["entry_dt"], row["exit_dt"]):
            raise SystemExit(f"trade #{i} changed since the hunt: {key}")
        verdict = overrides.get(i, row["verdict"])
        new = {}
        if verdict in ("REAL", "REAL_ENTRY_ONLY"):
            if t.get("source") != "real":
                new["source"] = "real"
        elif verdict == "PAPER":
            if t.get("source") != "paper":
                new["source"] = "paper"
            for k, v in PAPER_CLEAR.items():
                if k in t and t[k] != v and (t[k] or v):
                    new[k] = v
        if new:
            old = {k: t.get(k) for k in new}
            t.update(new)
            ev = row["evidence"]
            changes.append({"idx": i, "side": row["side"], "entry_dt": row["entry_dt"],
                            "exit_dt": row["exit_dt"], "verdict": verdict,
                            "entry_seq": (ev.get("entry") or {}).get("seq", ""),
                            "exit_seq": (ev.get("exit") or {}).get("seq", ""),
                            "old": old, "new": new})
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(sess, fh, ensure_ascii=False, indent=2)
    with open(audit_path, "w", encoding="utf-8") as fh:
        json.dump({"bot": sess.get("bot_name"), "changes": changes}, fh,
                  ensure_ascii=False, indent=1)
    print(f"{sess.get('bot_name')}: {len(changes)} trade(s) changed of {len(trades)}")


if __name__ == "__main__":
    main(sys.argv)
