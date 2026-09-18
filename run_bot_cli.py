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
    EXIT_BOT_STOPPED, EXIT_OK, EXIT_TIMEOUT, EXIT_USAGE, HeadlessConfigError,
    bot_info, bots_as_json, build_parser, cli_stdout_path, format_bot_table,
    list_bots, parse_yyyymmdd, request_stop, spawn_detached, strip_detach_args,
    tail_lines, wait_for_ready,
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
    if args.detach:
        return launch_detached(args)
    from src.live.headless_app import run_deploy
    return run_deploy(args)


def launch_detached(args) -> int:
    """Re-run this deploy command as a detached child and (optionally) wait
    until it is live. The child owns the bot; this process only reports."""
    from src.live.live_runner import LiveRunner
    bot_dir = LiveRunner.bot_dir_for(_base_dir(), args.symbol, args.bot)
    stdout_path = cli_stdout_path(bot_dir)
    cmd = [sys.executable, os.path.abspath(__file__)] + strip_detach_args(args._raw_argv)
    os.makedirs(bot_dir, exist_ok=True)
    offset = os.path.getsize(stdout_path) if os.path.exists(stdout_path) else 0
    proc = spawn_detached(cmd, stdout_path, cwd=project_root)
    import subprocess
    print(f"spawned PID {proc.pid}: run_bot_cli.py {subprocess.list2cmdline(cmd[2:])}")
    print(f"stdout: {stdout_path}")
    if not args.wait_ready:
        return EXIT_OK
    state, code = wait_for_ready(proc.poll, bot_dir, stdout_path, args.wait_ready,
                                 proc.pid, start_offset=offset)
    tail = tail_lines(stdout_path, 15, start_offset=offset)
    if state == "ready":
        print(f"READY: {args.symbol}_{args.bot} is live (PID {proc.pid}, lock acquired, "
              "tick subscription active)")
        print("\n".join(tail))
        return EXIT_OK
    if state == "exited":
        print(f"EXITED: bot process ended with code {code} before becoming ready",
              file=sys.stderr)
        print("\n".join(tail), file=sys.stderr)
        return code if code else EXIT_BOT_STOPPED
    print(f"TIMEOUT: PID {proc.pid} still running but not ready after {args.wait_ready}s "
          "(long warmup/CSV reload?) — check `status` and the stdout log", file=sys.stderr)
    print("\n".join(tail), file=sys.stderr)
    return EXIT_TIMEOUT


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
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(raw_argv)
    args._raw_argv = raw_argv  # launch_detached re-issues the same command
    try:
        return COMMANDS[args.command](args)
    except HeadlessConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
