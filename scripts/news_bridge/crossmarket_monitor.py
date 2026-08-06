"""W2 cross-market monitor bridge — the primary fast trigger (stdlib only).

Standalone stand-in for the n8n W2 workflow (docs/news_framework_n8n_setup.md §3).
Polls US quotes (Yahoo v8 chart API, keyless) during the Taiwan night session,
and when the semiconductor complex moves hard it writes a circuit-breaker
signal for the bot and posts a Discord alert.

Safety asymmetry (deliberate, mirrors the design doc):
- DOWNSIDE breach  -> writes `risk_off` automatically (bounded cost if wrong).
- UPSIDE breach    -> Discord alert only. Entering a position always requires
  the human: run `--force-fire deploy_long` (or deploy_short) yourself —
  that IS the "tap" until n8n W4 exists.

Usage:
    # one check, print only (no files written unless a threshold fires)
    python crossmarket_monitor.py --signal-out C:/n8n-bridge/signal.json --once

    # run continuously every 120s (night session hours only)
    python crossmarket_monitor.py --signal-out C:/n8n-bridge/signal.json --loop 120

    # end-to-end pipeline test: force a signal right now (fresh UUID/stamp)
    python crossmarket_monitor.py --signal-out C:/n8n-bridge/signal.json --force-fire risk_off

    # optional Discord alerts
    set NEWS_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...   (or --discord-webhook)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TZ_TAIPEI = timezone(timedelta(hours=8))

# Symbol -> (downside %, upside %) intraday-move thresholds vs previous close.
THRESHOLDS = {
    "SOXX": (-2.5, 2.5),   # SOX proxy — closest TAIEX correlate
    "TSM":  (-3.5, 3.5),   # TSMC ADR
    "QQQ":  (-2.0, 2.0),   # Nasdaq proxy
}
# Regime-vote thresholds — ALL symbols must breach for a vote to fire.
# Lower than signal thresholds (a vote accelerates confirmation, it does
# not flatten the book).
VOTE_THRESHOLDS = {
    "SOXX": (-2.5, 2.5),
    "TSM":  (-2.0, 2.0),
    "QQQ":  (-2.0, 2.0),
}
QUOTE_MAX_AGE_SEC = 600    # discard quotes older than 10 min (closed market)
NIGHT_START, NIGHT_END = 15, 5   # TPE hours the monitor is active (15:00-05:00)

_UA = {"User-Agent": "Mozilla/5.0 (tai-robot news bridge)"}


def fetch_quote(symbol: str) -> dict | None:
    """Return {price, prev_close, pct, quote_time} or None on any failure."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?interval=1m&range=1d")
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        meta = data["chart"]["result"][0]["meta"]
        price = float(meta["regularMarketPrice"])
        prev = float(meta.get("chartPreviousClose") or meta.get("previousClose"))
        qtime = int(meta["regularMarketTime"])
        return {
            "price": price,
            "prev_close": prev,
            "pct": (price - prev) / prev * 100.0,
            "quote_time": qtime,
        }
    except Exception as e:                                    # noqa: BLE001
        print(f"  [{symbol}] fetch failed: {type(e).__name__}: {e}")
        return None


def write_signal(path: str, action: str, reason: str, source: str) -> str:
    sid = str(uuid.uuid4())
    payload = {
        "version": 1,
        "signal_id": sid,
        "issued_at": datetime.now(_TZ_TAIPEI).isoformat(timespec="seconds"),
        "action": action,
        "severity": "high",
        "source": source,
        "reason": reason,
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(out) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, out)
    print(f"  SIGNAL WRITTEN: {action} ({sid[:8]}...) -> {out}")
    return sid


def post_discord(webhook: str | None, msg: str) -> None:
    if not webhook:
        return
    try:
        body = json.dumps({"content": msg}).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=body,
            headers={**_UA, "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        print("  discord alert sent")
    except Exception as e:                                    # noqa: BLE001
        print(f"  discord post failed: {type(e).__name__}: {e}")


def load_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def night_session_key(now: datetime) -> str:
    """Return ``"YYYY-MM-DD|NIGHT"`` for the night session containing *now*."""
    if now.hour >= NIGHT_START:
        return f"{now.strftime('%Y-%m-%d')}|NIGHT"
    return f"{(now - timedelta(days=1)).strftime('%Y-%m-%d')}|NIGHT"


def write_regime_vote(path: str, direction: str, session_key: str) -> None:
    """Write the regime-vote sidecar file atomically."""
    payload = {
        "version": 1,
        "direction": direction,
        "expires_after_session": session_key,
        "source": "W2",
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(out) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, out)
    print(f"  VOTE WRITTEN: {direction} for {session_key} -> {out}")


def in_night_window(now: datetime) -> bool:
    return now.hour >= NIGHT_START or now.hour < NIGHT_END


def check_once(args, state: dict) -> dict:
    """One polling pass. Returns the (possibly updated) dedup state."""
    now = datetime.now(_TZ_TAIPEI)
    print(f"[{now:%Y-%m-%d %H:%M:%S}] checking "
          f"{', '.join(THRESHOLDS)} (min move override: {args.min_move or '-'})")

    fired_down, fired_up, details = [], [], []
    quotes: dict[str, dict | None] = {}
    for sym, (down_th, up_th) in THRESHOLDS.items():
        if args.min_move is not None:
            down_th, up_th = -args.min_move, args.min_move
        q = fetch_quote(sym)
        if q is None:
            quotes[sym] = None
            continue
        age = time.time() - q["quote_time"]
        stale = age > QUOTE_MAX_AGE_SEC
        line = (f"  [{sym}] {q['pct']:+.2f}% (px {q['price']:.2f} vs pc "
                f"{q['prev_close']:.2f}, quote age {age:.0f}s"
                + (", STALE — ignored" if stale and not args.ignore_freshness else "") + ")")
        print(line)
        if stale and not args.ignore_freshness:
            quotes[sym] = None
            continue
        quotes[sym] = q
        details.append(f"{sym} {q['pct']:+.2f}%")
        if q["pct"] <= down_th:
            fired_down.append(f"{sym} {q['pct']:+.2f}%")
        elif q["pct"] >= up_th:
            fired_up.append(f"{sym} {q['pct']:+.2f}%")

    # One fire per direction per US-session date (dedup across restarts).
    us_date = (now - timedelta(hours=13)).strftime("%Y-%m-%d")  # rough US session key
    if fired_down and state.get(f"down:{us_date}") is None:
        reason = "downside breach: " + ", ".join(fired_down)
        sid = write_signal(args.signal_out, "risk_off", reason, "crossmarket-monitor")
        state[f"down:{us_date}"] = sid
        post_discord(args.discord_webhook,
                     f"🔴 **跨市場警報 Cross-market DOWNSIDE** — {reason}\n"
                     f"已寫入 risk_off（自動）。要進空單請手動: "
                     f"`--force-fire deploy_short`")
    elif fired_up and state.get(f"up:{us_date}") is None:
        state[f"up:{us_date}"] = "alerted"
        post_discord(args.discord_webhook,
                     f"🟢 **跨市場警報 Cross-market UPSIDE** — "
                     + ", ".join(fired_up)
                     + "\n未自動下單。要進多單請手動: `--force-fire deploy_long`")
        print("  UPSIDE breach — Discord alert only (no auto signal, by design)")
    elif fired_down or fired_up:
        print("  breach already fired for this US session — deduped")

    # Regime vote: ALL vote-threshold symbols must breach in the same
    # direction.  Separate from the signal (which fires on ANY single
    # breach) — a vote accelerates regime confirmation, it doesn't
    # flatten the book.
    vote_out = getattr(args, "vote_out", None)
    if vote_out and state.get(f"vote:{us_date}") is None:
        vote_up, vote_down = 0, 0
        for sym, (vd, vu) in VOTE_THRESHOLDS.items():
            q = quotes.get(sym)
            if q is None:
                break
            if q["pct"] >= vu:
                vote_up += 1
            elif q["pct"] <= vd:
                vote_down += 1
        else:
            sess_key = night_session_key(now)
            if vote_up == len(VOTE_THRESHOLDS):
                write_regime_vote(vote_out, "trending-up", sess_key)
                state[f"vote:{us_date}"] = "trending-up"
                post_discord(args.discord_webhook,
                             f"📊 **Regime vote** — trending-up ({', '.join(details)})")
            elif vote_down == len(VOTE_THRESHOLDS):
                write_regime_vote(vote_out, "trending-down", sess_key)
                state[f"vote:{us_date}"] = "trending-down"
                post_discord(args.discord_webhook,
                             f"📊 **Regime vote** — trending-down ({', '.join(details)})")

    return state


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross-market monitor -> signal.json")
    ap.add_argument("--signal-out", required=True, help="signal.json path the bot reads")
    ap.add_argument("--vote-out", default=None,
                    help="regime_vote.json path (regime confirmation acceleration)")
    ap.add_argument("--once", action="store_true", help="single check then exit")
    ap.add_argument("--loop", type=int, metavar="SEC",
                    help="poll every SEC seconds (night window only)")
    ap.add_argument("--force-fire", metavar="ACTION",
                    choices=["risk_off", "clear", "deploy_short", "deploy_long"],
                    help="write ACTION now (pipeline test / manual tap) and exit")
    ap.add_argument("--min-move", type=float, default=None,
                    help="TEST ONLY: replace all thresholds with this abs %% move")
    ap.add_argument("--ignore-freshness", action="store_true",
                    help="TEST ONLY: accept stale quotes (closed market)")
    ap.add_argument("--discord-webhook",
                    default=os.environ.get("NEWS_DISCORD_WEBHOOK") or None)
    args = ap.parse_args()

    if args.force_fire:
        write_signal(args.signal_out, args.force_fire,
                     f"manual --force-fire {args.force_fire}", "manual-tap")
        post_discord(args.discord_webhook,
                     f"🖐️ 手動訊號 manual signal written: `{args.force_fire}`")
        return 0

    state_path = Path(args.signal_out).parent / "monitor_state.json"
    state = load_state(state_path)

    if args.loop:
        print(f"looping every {args.loop}s (active {NIGHT_START}:00-{NIGHT_END}:00 TPE)")
        while True:
            if in_night_window(datetime.now(_TZ_TAIPEI)):
                state = check_once(args, state)
                save_state(state_path, state)
            else:
                print(f"[{datetime.now(_TZ_TAIPEI):%H:%M}] outside night window — idle")
            time.sleep(args.loop)
    else:
        state = check_once(args, state)
        save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
