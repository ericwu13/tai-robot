"""W2 cross-market monitor bridge — the primary fast trigger (stdlib only).

Standalone stand-in for the n8n W2 workflow (docs/news_framework_n8n_setup.md §3).
Polls global equity proxies (Yahoo v8 chart API, keyless) and, when the
semiconductor complex moves hard, writes a circuit-breaker signal for the bot
and posts a Discord alert.

Two symbol tiers (see ``SYMBOLS``):

- ``"signal"`` — US/EU semis + Nasdaq proxies whose moves historically lead
  TAIEX.  A DOWNSIDE breach writes ``risk_off`` automatically; an UPSIDE
  breach is a Discord alert only.
- ``"alert"``  — wider context (EU/JP/KR indices, Nasdaq futures).  Discord
  alert only in BOTH directions; these symbols NEVER write signal.json.
  They exist to give the human a heads-up during hours when the signal-tier
  markets are closed.

Safety asymmetry (deliberate, mirrors the design doc):
- DOWNSIDE breach (signal tier) -> writes `risk_off` automatically (bounded
  cost if wrong).
- UPSIDE breach (any tier)      -> Discord alert only. Entering a position
  always requires the human: run `--force-fire deploy_long` (or deploy_short)
  yourself — that IS the "tap" until n8n W4 exists.

Session hand-offs are handled by the quote-freshness guard alone: whichever
markets are closed simply go stale and drop out of every decision, so one
symbol table covers Tokyo/Seoul, Frankfurt/Amsterdam and New York without any
per-symbol clock logic.  The allowance is PER SYMBOL (``max_age``), because
Yahoo is real-time only for US cash — it publishes CME futures ~10 min and
Asian/European quotes ~15-20 min delayed.  A single 10-minute guard silently
discarded a live KOSPI -4% print and every NQ=F quote ever.

Usage:
    # one check, print only (no files written unless a threshold fires)
    python crossmarket_monitor.py --signal-out C:/n8n-bridge/signal.json --once

    # run continuously every 120s (active 08:00-05:00 TPE, idle 05:00-08:00)
    python crossmarket_monitor.py --signal-out C:/n8n-bridge/signal.json --loop 120

    # end-to-end pipeline test: force a signal right now (fresh UUID/stamp)
    python crossmarket_monitor.py --signal-out C:/n8n-bridge/signal.json --force-fire risk_off

    # optional Discord alerts
    set NEWS_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...   (or --discord-webhook)

Liveness: every pass appends ONE line to ``<signal dir>/monitor.log`` (override
with ``--log-file``) and stamps ``last_check``/``last_result`` into
monitor_state.json, so "is the bridge still running?" is answerable from disk
without waiting for an alert that may never be due::

    2026-08-06 15:02:03 TPE | fresh 2/8 (^N225 -0.99%, ^KS11 -4.81%) | fired: alert-down | vote: -
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

# Per-symbol config.
#   tier    — "signal" (downside may auto-write risk_off) | "alert" (Discord only)
#   down/up — intraday-move % thresholds vs previous close
#   vote    — (down, up) thresholds for the regime vote (looser: a vote only
#             accelerates regime confirmation, it never flattens the book)
#   max_age — freshness allowance in seconds, matched to that feed's latency:
#               600  US cash equities/ETFs — Yahoo serves these real-time
#               900  CME futures — ~10 min delayed (NQ=F arrived at 603 s and
#                    604 s in two live runs while Globex was trading: a 600 s
#                    guard is chronically ~3 s too tight and drops it always)
#               1500 Asian/European cash — ~15-20 min delayed (^KS11 arrived
#                    at 1208 s while KOSPI was open and -4.07%)
#             Too tight and the symbol is dead weight; these values still
#             reject a CLOSED market, whose last print keeps ageing past the
#             window a few minutes later.  Falls back to QUOTE_MAX_AGE_SEC.
SYMBOLS = {
    # ── signal tier ───────────────────────────────────────────────────
    "SOXX":      {"tier": "signal", "down": -2.5, "up": 2.5, "vote": (-2.5, 2.5),
                  "max_age": 600,
                  "desc": "SOX proxy ETF — closest TAIEX correlate"},
    "TSM":       {"tier": "signal", "down": -3.5, "up": 3.5, "vote": (-2.0, 2.0),
                  "max_age": 600, "desc": "TSMC ADR"},
    "QQQ":       {"tier": "signal", "down": -2.0, "up": 2.0, "vote": (-2.0, 2.0),
                  "max_age": 600, "desc": "Nasdaq proxy"},
    "ASML.AS":   {"tier": "signal", "down": -3.0, "up": 3.0, "vote": (-3.0, 3.0),
                  "max_age": 1500,
                  "desc": "European semi bellwether (Euronext 15:00-23:30 TPE, delayed)"},
    # ── alert tier (never writes signal.json) ─────────────────────────
    "^STOXX50E": {"tier": "alert", "down": -2.0, "up": 2.0, "vote": (-2.0, 2.0),
                  "max_age": 1500, "desc": "Euro Stoxx 50 (delayed feed)"},
    "NQ=F":      {"tier": "alert", "down": -1.5, "up": 1.5, "vote": (-1.5, 1.5),
                  "max_age": 900,
                  "desc": "Nasdaq futures (near 24h, CME 10-min delayed on Yahoo)"},
    "^N225":     {"tier": "alert", "down": -2.0, "up": 2.0, "vote": (-2.0, 2.0),
                  "max_age": 1500, "desc": "Nikkei 225 (delayed feed)"},
    "^KS11":     {"tier": "alert", "down": -2.0, "up": 2.0, "vote": (-2.0, 2.0),
                  "max_age": 1500, "desc": "KOSPI (delayed feed)"},
}

# Per-symbol regime-vote thresholds, derived from the one table above.
VOTE_THRESHOLDS = {sym: cfg["vote"] for sym, cfg in SYMBOLS.items()}

# A vote fires when at least this many FRESH symbols breach their vote
# threshold in one direction and none breaches the other way.
VOTE_MIN_SYMBOLS = 2

QUOTE_MAX_AGE_SEC = 600    # default allowance for symbols with no max_age
# TPE hours.  The day window runs to 15:00 (not 14:00) so the Korea/Japan
# closing prints — 14:30-14:50 TPE, routinely the sharpest move of their
# session — are still fresh when the Taiwan night session opens at 15:00.
DAY_START, DAY_END = 8, 15        # Taiwan day session coverage
NIGHT_START, NIGHT_END = 15, 5    # night session coverage (contiguous with DAY_END)

# Liveness log — one line per pass, so "is the bridge even running?" is
# answerable from disk without a Discord alert having fired.
LOG_NAME = "monitor.log"
LOG_MAX_BYTES = 1_000_000
LOG_KEEP_LINES = 2000

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


def log_path_for(args) -> Path:
    """The liveness log path — ``--log-file`` or ``<signal dir>/monitor.log``.

    Defaulting off --signal-out means the deployed n8n command starts
    logging on its next tick with no edit to the workflow.
    """
    explicit = getattr(args, "log_file", None)
    return Path(explicit) if explicit else Path(args.signal_out).parent / LOG_NAME


def append_log(path: str | os.PathLike, line: str) -> None:
    """Append one liveness line, self-trimming.  Never raises: a monitoring
    pass that completed must not be reported as failed because the log
    write did."""
    try:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if out.stat().st_size > LOG_MAX_BYTES:
            with open(out, encoding="utf-8") as f:
                kept = f.readlines()[-LOG_KEEP_LINES:]
            tmp = str(out) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(kept)
            os.replace(tmp, out)
            print(f"  log trimmed to last {LOG_KEEP_LINES} lines")
    except Exception as e:                                    # noqa: BLE001
        print(f"  log write failed: {type(e).__name__}: {e}")


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


def in_active_window(now: datetime) -> bool:
    """True during the Taiwan day session (08:00-15:00) or the night
    session (15:00-05:00 TPE) — contiguous, so effectively 08:00-05:00
    with a 05:00-08:00 idle gap.  Loop mode only — ``--once`` is ungated."""
    if DAY_START <= now.hour < DAY_END:
        return True
    return now.hour >= NIGHT_START or now.hour < NIGHT_END


def _collect_quotes(args) -> tuple[dict[str, dict], dict[str, list[str]], list[str], int]:
    """Fetch every symbol once.

    Returns ``(fresh_quotes, breaches, details, fetch_failures)`` where
    *breaches* maps ``"<tier>_<direction>"`` to the human-readable breach
    lines and *fetch_failures* counts symbols whose fetch returned nothing.
    """
    fresh: dict[str, dict] = {}
    breaches: dict[str, list[str]] = {
        "signal_down": [], "signal_up": [], "alert_down": [], "alert_up": []}
    details: list[str] = []
    fetch_failures = 0

    for sym, cfg in SYMBOLS.items():
        down_th, up_th = cfg["down"], cfg["up"]
        if args.min_move is not None:
            down_th, up_th = -args.min_move, args.min_move
        q = fetch_quote(sym)
        if q is None:
            fetch_failures += 1
            continue
        age = time.time() - q["quote_time"]
        max_age = cfg.get("max_age", QUOTE_MAX_AGE_SEC)
        stale = age > max_age and not args.ignore_freshness
        print(f"  [{sym}] {q['pct']:+.2f}% (px {q['price']:.2f} vs pc "
              f"{q['prev_close']:.2f}, quote age {age:.0f}s"
              + (f", STALE > {max_age}s allowance — ignored" if stale else "") + ")")
        if stale:
            continue
        fresh[sym] = q
        details.append(f"{sym} {q['pct']:+.2f}%")
        line = f"{sym} {q['pct']:+.2f}%"
        if q["pct"] <= down_th:
            breaches[f"{cfg['tier']}_down"].append(line)
        elif q["pct"] >= up_th:
            breaches[f"{cfg['tier']}_up"].append(line)

    return fresh, breaches, details, fetch_failures


def _vote_direction(fresh: dict[str, dict]) -> str | None:
    """Return "trending-up"/"trending-down" when at least VOTE_MIN_SYMBOLS
    fresh symbols breach their vote threshold one way and none breaches the
    other way.  None otherwise."""
    up, down = [], []
    for sym, q in fresh.items():
        vote_down, vote_up = VOTE_THRESHOLDS[sym]
        if q["pct"] >= vote_up:
            up.append(sym)
        elif q["pct"] <= vote_down:
            down.append(sym)
    if len(up) >= VOTE_MIN_SYMBOLS and not down:
        return "trending-up"
    if len(down) >= VOTE_MIN_SYMBOLS and not up:
        return "trending-down"
    return None


def check_once(args, state: dict) -> dict:
    """One polling pass. Returns the (possibly updated) dedup state."""
    now = datetime.now(_TZ_TAIPEI)
    print(f"[{now:%Y-%m-%d %H:%M:%S}] checking "
          f"{', '.join(SYMBOLS)} (min move override: {args.min_move or '-'})")

    fresh, breaches, details, fetch_failures = _collect_quotes(args)
    fired: list[str] = []

    # One fire per direction per US-session date (dedup across restarts).
    # Signal-tier fires and alert-tier alerts keep SEPARATE keys so an
    # alert can never suppress a later signal-tier risk_off.
    us_date = (now - timedelta(hours=13)).strftime("%Y-%m-%d")  # rough US session key

    # ── signal tier: downside auto-fires, upside alerts only ──────────
    if breaches["signal_down"]:
        fired.append("signal-down")
        if state.get(f"down:{us_date}") is None:
            reason = "downside breach: " + ", ".join(breaches["signal_down"])
            sid = write_signal(args.signal_out, "risk_off", reason, "crossmarket-monitor")
            state[f"down:{us_date}"] = sid
            post_discord(args.discord_webhook,
                         f"🔴 **跨市場警報 Cross-market DOWNSIDE** — {reason}\n"
                         f"已寫入 risk_off（自動）。要進空單請手動: "
                         f"`--force-fire deploy_short`")
        else:
            fired[-1] += "(deduped)"
            print("  signal-tier downside already fired for this US session — deduped")

    if breaches["signal_up"]:
        fired.append("signal-up")
        if state.get(f"up:{us_date}") is None:
            state[f"up:{us_date}"] = "alerted"
            post_discord(args.discord_webhook,
                         "🟢 **跨市場警報 Cross-market UPSIDE** — "
                         + ", ".join(breaches["signal_up"])
                         + "\n未自動下單。要進多單請手動: `--force-fire deploy_long`")
            print("  UPSIDE breach — Discord alert only (no auto signal, by design)")
        else:
            fired[-1] += "(deduped)"
            print("  signal-tier upside already alerted for this US session — deduped")

    # ── alert tier: Discord only, both directions, never signal.json ──
    for direction, emoji, word in (("down", "🟠", "DOWNSIDE"), ("up", "🔵", "UPSIDE")):
        hits = breaches[f"alert_{direction}"]
        if not hits:
            continue
        fired.append(f"alert-{direction}")
        key = f"alert-{direction}:{us_date}"
        if state.get(key) is not None:
            fired[-1] += "(deduped)"
            print(f"  alert-tier {direction} already alerted for this US session — deduped")
            continue
        state[key] = "alerted"
        post_discord(args.discord_webhook,
                     f"{emoji} **跨市場觀察 Cross-market {word} (alert tier)** — "
                     + ", ".join(hits)
                     + "\n僅通知，未寫入訊號 (alert tier writes no signal).")
        print(f"  alert-tier {direction} breach — Discord alert only (never a signal)")

    # ── regime vote: >=2 fresh symbols agree, none disagrees ──────────
    # The direction is computed every pass (pure, cheap) so the log shows
    # what the vote logic saw even when no --vote-out is configured.
    direction = _vote_direction(fresh)
    vote_note = direction or "-"
    vote_out = getattr(args, "vote_out", None)
    if direction and vote_out:
        if state.get(f"vote:{us_date}") is None:
            write_regime_vote(vote_out, direction, night_session_key(now))
            state[f"vote:{us_date}"] = direction
            post_discord(args.discord_webhook,
                         f"📊 **Regime vote** — {direction} ({', '.join(details)})")
        else:
            vote_note = f"{direction}(deduped)"
            print("  vote already written for this US session — deduped")

    # ── liveness: one log line + machine-readable state per pass ──────
    summary = (f"fresh {len(fresh)}/{len(SYMBOLS)} ({', '.join(details) or 'none'})"
               f" | fired: {'+'.join(fired) or 'none'}"
               f" | vote: {vote_note}"
               + (f" | fetch-fail {fetch_failures}" if fetch_failures else ""))
    state["last_check"] = now.isoformat(timespec="seconds")
    state["last_result"] = summary
    append_log(log_path_for(args), f"{now:%Y-%m-%d %H:%M:%S} TPE | {summary}")

    return state


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross-market monitor -> signal.json")
    ap.add_argument("--signal-out", required=True, help="signal.json path the bot reads")
    ap.add_argument("--vote-out", default=None,
                    help="regime_vote.json path (regime confirmation acceleration)")
    ap.add_argument("--once", action="store_true", help="single check then exit")
    ap.add_argument("--loop", type=int, metavar="SEC",
                    help="poll every SEC seconds (active window only)")
    ap.add_argument("--force-fire", metavar="ACTION",
                    choices=["risk_off", "clear", "deploy_short", "deploy_long"],
                    help="write ACTION now (pipeline test / manual tap) and exit")
    ap.add_argument("--min-move", type=float, default=None,
                    help="TEST ONLY: replace all tier thresholds with this abs %% move")
    ap.add_argument("--ignore-freshness", action="store_true",
                    help="TEST ONLY: accept stale quotes (closed market)")
    ap.add_argument("--log-file", default=None, metavar="PATH",
                    help=f"liveness log (default: <signal dir>/{LOG_NAME})")
    ap.add_argument("--discord-webhook",
                    default=os.environ.get("NEWS_DISCORD_WEBHOOK") or None)
    args = ap.parse_args()

    if args.force_fire:
        sid = write_signal(args.signal_out, args.force_fire,
                           f"manual --force-fire {args.force_fire}", "manual-tap")
        post_discord(args.discord_webhook,
                     f"🖐️ 手動訊號 manual signal written: `{args.force_fire}`")
        append_log(log_path_for(args),
                   f"{datetime.now(_TZ_TAIPEI):%Y-%m-%d %H:%M:%S} TPE | "
                   f"force-fire {args.force_fire} -> {sid[:8]}")
        return 0

    state_path = Path(args.signal_out).parent / "monitor_state.json"
    state = load_state(state_path)

    if args.loop:
        print(f"looping every {args.loop}s (active {DAY_START:02d}:00-{NIGHT_END:02d}:00 "
              f"TPE, idle {NIGHT_END:02d}:00-{DAY_START:02d}:00)")
        while True:
            if in_active_window(datetime.now(_TZ_TAIPEI)):
                state = check_once(args, state)
                save_state(state_path, state)
            else:
                print(f"[{datetime.now(_TZ_TAIPEI):%H:%M}] outside active window — idle")
            time.sleep(args.loop)
    else:
        state = check_once(args, state)
        save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
