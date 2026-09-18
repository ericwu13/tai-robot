"""Pure helpers for the headless bot CLI (``run_bot_cli.py``).

Nothing in here imports Tk or COM — argument parsing, request building,
strategy-name resolution, the STOP-file control channel, and the bot
directory scan are all plain functions so they can be unit-tested and
reused by scripts/monitor. The Tk-bound driver lives in
``src/live/headless_app.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime

from .deploy_request import DeployRequest
from .session_store import load_session

# Modes the CLI may deploy. semi_auto needs a human clicking the 10-second
# order-confirm dialog — headless it would silently skip every real order,
# which is worse than refusing.
HEADLESS_MODES = ("paper", "auto")

STOP_FILENAME = "STOP"

# Exit codes (documented in README "Headless CLI")
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_LOGIN = 2
EXIT_NO_COM = 3
EXIT_DEPLOY_REFUSED = 4
EXIT_BOT_STOPPED = 5      # bot stopped on its own (warmup failure, etc.)
EXIT_TIMEOUT = 6
EXIT_BACKTEST_FAILED = 7

# Dialog titles the GUI asks through BacktestApp._confirm. The headless
# policy answers by title, so these strings are the contract with
# run_backtest.py — keep them byte-identical.
TITLE_STRATEGY_CHANGE = "策略更換 Strategy Change"
TITLE_EXISTING_POSITION = "帳戶有持倉 Existing Position"
TITLE_DATA_RANGE = "資料範圍不足 Insufficient Data Range"


class HeadlessConfigError(ValueError):
    """Invalid CLI inputs — reported to the user, exit code EXIT_USAGE."""


@dataclass
class HeadlessPolicy:
    """Answers to the questions the GUI would otherwise pop up."""

    use_selected_strategy: bool = False    # resume: deploy --strategy, not the saved one
    allow_existing_position: bool = False  # real account already holds a position
    accept_data_range: bool = True         # backtest: less data than requested

    def answer(self, title: str) -> bool:
        if title == TITLE_STRATEGY_CHANGE:
            return self.use_selected_strategy
        if title == TITLE_EXISTING_POSITION:
            return self.allow_existing_position
        if title == TITLE_DATA_RANGE:
            return self.accept_data_range
        # Unknown question: decline. A new GUI prompt must be wired here
        # explicitly before the CLI is allowed to say yes to it.
        return False


# ── Strategy names ──

def resolve_strategy_name(query: str, names) -> str:
    """Map a user-typed strategy query to one registry display name.

    Exact match first, then case-insensitive exact, then a unique
    case-insensitive substring. Raises HeadlessConfigError when nothing
    or more than one entry matches.
    """
    names = list(names)
    q = (query or "").strip()
    if not q:
        raise HeadlessConfigError("strategy name is empty")
    if q in names:
        return q
    ql = q.lower()
    exact_ci = [n for n in names if n.lower() == ql]
    if len(exact_ci) == 1:
        return exact_ci[0]
    subs = [n for n in names if ql in n.lower()]
    if len(subs) == 1:
        return subs[0]
    if not subs:
        raise HeadlessConfigError(
            f"strategy '{query}' not found; run `strategies` to list names")
    raise HeadlessConfigError(
        f"strategy '{query}' is ambiguous: " + " | ".join(subs))


# ── Deploy request ──

def build_deploy_request(args: argparse.Namespace,
                         resume_session: dict | None) -> DeployRequest:
    """Validate CLI args and build the DeployRequest the GUI path consumes."""
    mode = args.mode
    if mode not in HEADLESS_MODES:
        raise HeadlessConfigError(
            f"trading mode '{mode}' is not available headless "
            f"(choose one of {', '.join(HEADLESS_MODES)}; semi_auto needs "
            "the GUI's order-confirm dialog)")
    try:
        loss_limit = int(args.loss_limit)
    except (TypeError, ValueError):
        raise HeadlessConfigError(f"--loss-limit must be an integer, got {args.loss_limit!r}")
    if loss_limit < 0:
        raise HeadlessConfigError("--loss-limit must be >= 0")

    regime_enabled = bool(args.regime)
    regime_long = args.long_strategy or ""
    regime_short = args.short_strategy or ""
    resumed_regime = bool(resume_session and resume_session.get("regime_mode"))
    if regime_enabled and not resumed_regime and not (regime_long and regime_short):
        raise HeadlessConfigError(
            "--regime needs both --long-strategy and --short-strategy "
            "(unless resuming a regime session)")
    if not regime_enabled and not resumed_regime and not args.strategy \
            and not (resume_session and resume_session.get("strategy")):
        raise HeadlessConfigError("--strategy is required for a new non-regime bot")

    news_enabled = bool(args.news)
    if (args.news_tier2 or args.news_directional) and not news_enabled:
        raise HeadlessConfigError("--news-tier2 / --news-directional require --news")
    if news_enabled and not (regime_enabled or resumed_regime):
        raise HeadlessConfigError("--news only applies to regime bots (--regime)")

    return DeployRequest(
        bot_name=args.bot,
        resume_session=resume_session,
        trading_mode=mode,
        loss_limit=str(loss_limit),
        regime_enabled=regime_enabled,
        regime_long=regime_long,
        regime_short=regime_short,
        news_enabled=news_enabled,
        news_tier2_enabled=bool(args.news_tier2),
        news_directional=bool(args.news_directional),
        origin="cli",
    )


def policy_from_args(args: argparse.Namespace) -> HeadlessPolicy:
    return HeadlessPolicy(
        use_selected_strategy=bool(getattr(args, "strategy", "")),
        allow_existing_position=bool(getattr(args, "allow_existing_position", False)),
        accept_data_range=True,
    )


# ── STOP-file control channel ──

def stop_file_path(bot_dir: str) -> str:
    return os.path.join(bot_dir, STOP_FILENAME)


def stop_requested(bot_dir: str) -> bool:
    return os.path.isfile(stop_file_path(bot_dir))


def request_stop(bot_dir: str, reason: str = "cli stop") -> str:
    """Create the STOP sentinel; the running CLI bot polls for it."""
    os.makedirs(bot_dir, exist_ok=True)
    path = stop_file_path(bot_dir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {reason}\n")
    return path


def clear_stop_file(bot_dir: str) -> None:
    try:
        os.remove(stop_file_path(bot_dir))
    except OSError:
        pass


# ── Detached launch (deploy --detach) ──

CLI_STDOUT_NAME = "cli_stdout.log"
# Printed by BacktestApp._start_live_tick_subscription once RequestTicks
# succeeded (mirrored to stdout by HeadlessBotApp._live_log_msg). Login,
# warmup and CSV reload are all done by then — the bot is live.
READY_MARKER = "Tick subscription active"


def cli_stdout_path(bot_dir: str) -> str:
    return os.path.join(bot_dir, CLI_STDOUT_NAME)


def strip_detach_args(argv: list[str]) -> list[str]:
    """argv for the child: the same deploy command minus the launcher flags."""
    out: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--detach" or arg.startswith("--wait-ready="):
            continue
        if arg == "--wait-ready":
            skip_next = True
            continue
        out.append(arg)
    return out


def spawn_detached(cmd: list[str], stdout_path: str, cwd: str | None = None):
    """Start ``cmd`` so it survives this process; stdout+stderr append to a file.

    Returns the ``subprocess.Popen`` (``poll()`` still works — the parent
    keeps a handle — but the child is not killed when the parent exits).
    """
    import subprocess
    os.makedirs(os.path.dirname(stdout_path) or ".", exist_ok=True)
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    with open(stdout_path, "ab") as log:
        return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=cwd, **kwargs)


def _file_contains_after(path: str, marker: str, start_offset: int) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(start_offset)
            return marker.encode("utf-8") in f.read()
    except OSError:
        return False


def tail_lines(path: str, n: int = 15, start_offset: int = 0) -> list[str]:
    """Last ``n`` lines written at or after ``start_offset``."""
    try:
        with open(path, "rb") as f:
            f.seek(start_offset)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln.rstrip("\r") for ln in data.split("\n") if ln.strip()]
    return lines[-n:]


def wait_for_ready(poll_exit, bot_dir: str, stdout_path: str, timeout_s: float,
                   pid: int, *, start_offset: int = 0, marker: str = READY_MARKER,
                   sleep_s: float = 1.0) -> tuple[str, int | None]:
    """Block until the detached bot is live, died, or the timeout passes.

    Returns ``("ready", None)`` once the bot's own .lock names ``pid`` and
    the marker appeared in stdout AFTER ``start_offset`` (the log is
    appended across runs — an old run's marker must not count);
    ``("exited", code)`` if the child ended first; ``("timeout", None)``
    otherwise (the child is still running, e.g. a long CSV reload).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        code = poll_exit()
        if code is not None:
            return "exited", int(code)
        alive, lock_pid = read_lock(bot_dir)
        if alive and lock_pid == pid and _file_contains_after(stdout_path, marker, start_offset):
            return "ready", None
        if time.monotonic() >= deadline:
            return "timeout", None
        time.sleep(sleep_s)


# ── Bot directory scan ──

@dataclass
class BotInfo:
    symbol: str
    bot_name: str
    bot_dir: str
    running: bool
    pid: int
    strategy: str = ""
    trading_mode: str = ""
    trades: int = 0
    pnl: int = 0
    position: str = "Flat"
    saved_at: str = ""
    regime: bool = False
    log_age_min: float | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _latest_debug_log_age_min(bot_dir: str) -> float | None:
    newest = 0.0
    try:
        for name in os.listdir(bot_dir):
            if name.startswith("debug_") and name.endswith(".log"):
                newest = max(newest, os.path.getmtime(os.path.join(bot_dir, name)))
    except OSError:
        return None
    if not newest:
        return None
    return round((time.time() - newest) / 60.0, 1)


def read_lock(bot_dir: str) -> tuple[bool, int]:
    """Non-destructive lock probe: ``(owner_alive, pid)``.

    ``LiveRunner.check_lock`` deletes a stale lock as a side effect (it is
    a deploy-time cleanup); ``list``/``status`` must never modify a bot
    directory, so this reads the PID and tests liveness only.
    """
    from .live_runner import LiveRunner  # no Tk/COM in that module
    lock = os.path.join(bot_dir, ".lock")
    if not os.path.isfile(lock):
        return False, 0
    try:
        with open(lock) as f:
            pid = int(f.read().strip())
    except (ValueError, OSError):
        return False, 0
    return LiveRunner._pid_alive(pid), pid


def bot_info(base_dir: str, symbol: str, bot_name: str) -> BotInfo:
    """Inspect one bot directory: session.json + .lock liveness (read-only)."""
    from .live_runner import LiveRunner  # no Tk/COM in that module
    bot_dir = LiveRunner.bot_dir_for(base_dir, symbol, bot_name)
    locked, pid = read_lock(bot_dir)
    info = BotInfo(symbol=symbol, bot_name=bot_name, bot_dir=bot_dir,
                   running=bool(locked), pid=int(pid or 0))
    sess = load_session(os.path.join(bot_dir, "session.json"))
    if sess:
        broker = sess.get("broker", {}) or {}
        trades = broker.get("trades", []) or []
        info.strategy = sess.get("strategy", "") or ""
        if sess.get("regime_mode"):
            info.regime = True
            info.strategy = (f"Regime L={sess.get('long_strategy', '')} "
                             f"S={sess.get('short_strategy', '')}")
        info.trading_mode = sess.get("trading_mode", "") or ""
        info.trades = len(trades)
        try:
            info.pnl = int(broker.get("_cumulative_pnl", 0) or 0)
        except (TypeError, ValueError):
            info.pnl = 0
        if broker.get("position_size", 0):
            info.position = (f"{broker.get('position_side', '')} "
                             f"@ {broker.get('entry_price', 0):,}")
        info.saved_at = sess.get("saved_at", "") or ""
    info.log_age_min = _latest_debug_log_age_min(bot_dir)
    return info


def list_bots(base_dir: str, symbol: str | None = None) -> list[BotInfo]:
    """Every ``{symbol}_{bot}`` directory under data/live."""
    out: list[BotInfo] = []
    if not os.path.isdir(base_dir):
        return out
    for entry in sorted(os.listdir(base_dir)):
        full = os.path.join(base_dir, entry)
        if not os.path.isdir(full) or "_" not in entry:
            continue
        sym, _, name = entry.partition("_")
        if not sym or not name:
            continue
        if symbol and sym != symbol:
            continue
        out.append(bot_info(base_dir, sym, name))
    return out


def _display_width(text: str) -> int:
    """Terminal columns for ``text`` (CJK characters take two)."""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def format_bot_table(bots: list[BotInfo]) -> str:
    if not bots:
        return "(no bots under data/live)"
    rows = [("SYMBOL", "BOT", "RUN", "LOCK PID", "MODE", "TRADES", "PNL",
             "POSITION", "LOG AGE", "STRATEGY")]
    for b in bots:
        rows.append((
            b.symbol, b.bot_name, "yes" if b.running else "no",
            str(b.pid or ""), b.trading_mode, str(b.trades), f"{b.pnl:+,}",
            b.position, "" if b.log_age_min is None else f"{b.log_age_min:.0f}m",
            b.strategy,
        ))
    widths = [max(_display_width(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(_pad(cell, widths[i]) for i, cell in enumerate(r)).rstrip()
        for r in rows)


def bots_as_json(bots: list[BotInfo]) -> str:
    return json.dumps([b.as_dict() for b in bots], ensure_ascii=False, indent=2)


# ── Argument parser ──

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_bot_cli.py",
        description=("Headless tai-robot: deploy/stop live bots and run "
                     "backtests without the Tk workbench."),
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("deploy", help="deploy a live bot and run until stopped")
    d.add_argument("--symbol", required=True, help="e.g. TX00, MTX00, TMF00")
    d.add_argument("--bot", required=True, help="bot name (data/live/{symbol}_{bot})")
    d.add_argument("--mode", default="paper", choices=list(HEADLESS_MODES),
                   help="trading mode (default paper)")
    d.add_argument("--strategy", default="",
                   help="strategy display name or unique substring; on resume, "
                        "overrides the saved strategy")
    d.add_argument("--point-value", type=int, default=None,
                   help="NTD per point (default: symbol's value)")
    d.add_argument("--loss-limit", default="1000",
                   help="daily loss limit in NTD for real orders (0 = off)")
    resume = d.add_mutually_exclusive_group()
    resume.add_argument("--resume", action="store_true",
                        help="require an existing session.json and resume it")
    resume.add_argument("--new", action="store_true",
                        help="refuse to resume; the bot dir must not have a session")
    d.add_argument("--regime", action="store_true", help="regime-switching bot")
    d.add_argument("--long-strategy", default="", help="regime long leg")
    d.add_argument("--short-strategy", default="", help="regime short leg")
    d.add_argument("--news", action="store_true", help="news circuit breaker (regime only)")
    d.add_argument("--news-tier2", action="store_true")
    d.add_argument("--news-directional", action="store_true",
                   help="suppress only the conflicting leg")
    d.add_argument("--allow-existing-position", action="store_true",
                   help="deploy even if the real account already holds a position")
    d.add_argument("--login-timeout", type=int, default=90,
                   help="seconds to wait for the quote connection (default 90)")
    d.add_argument("--heartbeat-min", type=int, default=5,
                   help="print a status line every N minutes (0 = off)")
    d.add_argument("--detach", action="store_true",
                   help="spawn the bot as a detached background process "
                        f"(stdout -> {{bot_dir}}/{CLI_STDOUT_NAME}) and return")
    d.add_argument("--wait-ready", type=int, default=180, metavar="SECONDS",
                   help="with --detach: wait up to N seconds for the bot to reach "
                        "tick subscription before returning (0 = return at once)")

    s = sub.add_parser("stop", help="ask a CLI-deployed bot to stop (STOP file)")
    s.add_argument("--symbol", required=True)
    s.add_argument("--bot", required=True)
    s.add_argument("--wait", type=int, default=0,
                   help="seconds to wait for the lock to clear")

    ls = sub.add_parser("list", help="list bot directories and their state")
    ls.add_argument("--symbol", default=None)
    ls.add_argument("--json", action="store_true")

    st = sub.add_parser("status", help="show one bot's session/lock state")
    st.add_argument("--symbol", required=True)
    st.add_argument("--bot", required=True)
    st.add_argument("--json", action="store_true")

    sub.add_parser("strategies", help="list strategy display names (incl. saved AI ones)")

    b = sub.add_parser("backtest", help="run a backtest and print the report")
    b.add_argument("--symbol", required=True)
    b.add_argument("--strategy", required=True)
    b.add_argument("--start", required=True, help="YYYYMMDD")
    b.add_argument("--end", required=True, help="YYYYMMDD")
    b.add_argument("--source", default="tv", choices=["tv", "api"],
                   help="tv = TradingView download, api = Capital API (login)")
    b.add_argument("--point-value", type=int, default=None)
    b.add_argument("--balance", type=int, default=None)
    b.add_argument("--export", default="", help="write trades CSV to this path")
    b.add_argument("--timeout", type=int, default=600,
                   help="seconds before giving up on data fetch (default 600)")
    b.add_argument("--login-timeout", type=int, default=90)
    return p


def parse_yyyymmdd(value: str) -> str:
    try:
        datetime.strptime(value, "%Y%m%d")
    except (TypeError, ValueError):
        raise HeadlessConfigError(f"date must be YYYYMMDD, got {value!r}")
    return value
