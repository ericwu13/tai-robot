"""Headless CLI for tai-robot — deploy, stop and inspect live bots, run backtests.

Runs the same live-bot machinery as run_backtest.py (the Tk workbench)
on a hidden window, so nothing about order handling, fill tracking,
reconnects or session-end close differs between a GUI bot and a CLI bot.

Examples:
  python run_bot_cli.py strategies
  python run_bot_cli.py deploy --symbol TMF00 --bot night1 --strategy "H4 Bollinger" --mode paper
  python run_bot_cli.py deploy --symbol TMF00 --bot regime1 --regime \\
      --long-strategy "H4 Bollinger Long" --short-strategy "AI: SomeShort" --news
  python run_bot_cli.py stop --symbol TMF00 --bot night1 --wait 60
  python run_bot_cli.py list
  python run_bot_cli.py status --symbol TMF00 --bot night1 --json
  python run_bot_cli.py backtest --symbol TX00 --strategy "1m SMA" --start 20260101 --end 20260301

Exit codes: 0 ok · 1 usage · 2 login/connection · 3 COM unavailable ·
4 deploy refused · 5 bot stopped on its own · 6 timeout · 7 backtest failed.
"""

from __future__ import annotations

import os
import sys
import time

# Same sys.path setup as run_backtest.py so `import run_backtest` works
# from any cwd.
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.live.headless import (  # noqa: E402
    EXIT_OK, EXIT_TIMEOUT, EXIT_USAGE, HeadlessConfigError, bot_info,
    bots_as_json, build_parser, format_bot_table, list_bots, parse_yyyymmdd,
    request_stop,
)


def _base_dir() -> str:
    return os.path.join(project_root, "data", "live")


def cmd_list(args) -> int:
    bots = list_bots(_base_dir(), args.symbol)
    print(bots_as_json(bots) if args.json else format_bot_table(bots))
    return EXIT_OK


def cmd_status(args) -> int:
    info = bot_info(_base_dir(), args.symbol, args.bot)
    if args.json:
        print(bots_as_json([info]))
    else:
        print(format_bot_table([info]))
        print(f"dir: {info.bot_dir}")
    return EXIT_OK


def cmd_stop(args) -> int:
    info = bot_info(_base_dir(), args.symbol, args.bot)
    if not info.running:
        print(f"{args.symbol}_{args.bot} is not running (no live .lock)")
        return EXIT_USAGE
    path = request_stop(info.bot_dir)
    print(f"STOP requested for {args.symbol}_{args.bot} (PID {info.pid}): {path}")
    print("note: only bots started by run_bot_cli.py poll the STOP file; "
          "a GUI-deployed bot must be stopped from the workbench")
    if not args.wait:
        return EXIT_OK
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        time.sleep(1)
        if not bot_info(_base_dir(), args.symbol, args.bot).running:
            print("bot stopped")
            return EXIT_OK
    print(f"bot still running after {args.wait}s", file=sys.stderr)
    return EXIT_TIMEOUT


def cmd_strategies(args) -> int:
    import run_backtest as rb
    from src.ai.strategy_store import StrategyStore
    rb.load_saved_ai_strategies(StrategyStore(os.path.join(project_root, "strategies")))
    for name, cls in rb.STRATEGIES.items():
        note = "  (news-only, not a regime leg)" if name in rb.NEWS_ONLY_STRATEGIES else ""
        print(f"{name}    [type={cls.kline_type} min={cls.kline_minute}]{note}")
    return EXIT_OK


def cmd_deploy(args) -> int:
    from src.live.headless_app import run_deploy
    return run_deploy(args)


def cmd_backtest(args) -> int:
    parse_yyyymmdd(args.start)
    parse_yyyymmdd(args.end)
    from src.live.headless_app import run_backtest
    return run_backtest(args)


COMMANDS = {
    "deploy": cmd_deploy,
    "stop": cmd_stop,
    "list": cmd_list,
    "status": cmd_status,
    "strategies": cmd_strategies,
    "backtest": cmd_backtest,
}


def main(argv: list[str] | None = None) -> int:
    # Strategy names and log lines are bilingual; a cp950 console would
    # otherwise mojibake them (or raise on redirect).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except HeadlessConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
