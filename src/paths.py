"""Writable install directory vs PyInstaller bundle directory.

Single frozen check for ``run_backtest.py`` and ``run_bot_cli.py``.

Frozen: ``project_root`` is the directory containing the EXE (the
user-writable install dir: ``settings.yaml``, ``data/live``, STOP files,
locks, ``cli_stdout.log``). ``bundle_root`` is ``sys._MEIPASS`` (the
``_internal`` tree). Source: both are the repository root, the directory
that contains ``run_backtest.py``.
"""

from __future__ import annotations

import os
import sys


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> str:
    """Install dir when frozen, otherwise the repository root."""
    if _frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_root() -> str:
    """PyInstaller extraction dir when frozen, otherwise ``project_root``."""
    if _frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return os.path.abspath(meipass)
        return os.path.dirname(os.path.abspath(sys.executable))
    return project_root()


# Frozen spawn children re-exec the EXE with this as argv[1]. Same token
# multiprocessing.spawn uses; #194's _is_multiprocessing_child() matches it.
MULTIPROCESSING_FORK = "--multiprocessing-fork"
# #194's key-free parent. It must not be treated as an unknown CLI token
# (that would open the GUI) and must not import COM.
GRID_SUBPROCESS_SMOKE = "--grid-subprocess-smoke"


def is_multiprocessing_child(argv: list[str] | None = None) -> bool:
    """True when this process is a spawn child, not the bot or the GUI.

    ``argv`` is the full ``sys.argv`` (argv[0] is the EXE or script).
    The flag is only recognized at argv[1], which is where the spawn
    start method puts it.
    """
    args = sys.argv if argv is None else argv
    return len(args) >= 2 and args[1] == MULTIPROCESSING_FORK


def is_grid_smoke(argv: list[str] | None = None) -> bool:
    """True for ``--grid-subprocess-smoke`` (anywhere in the full argv)."""
    args = sys.argv if argv is None else argv
    return GRID_SUBPROCESS_SMOKE in args


def skips_gui_and_com(argv: list[str] | None = None) -> bool:
    """One check for the two processes that must not open Tk or load COM.

    The runtime hook and ``run_backtest`` both call this. A spawn child
    and the grid-smoke parent are the only skips; a CLI ``deploy`` still
    installs the comtypes gen cache.
    """
    return is_multiprocessing_child(argv) or is_grid_smoke(argv)


def dispatch_entry(argv: list[str]) -> tuple[str, list[str]]:
    """Classify a full argv before the CLI router runs.

    Returns ``("spawn"|"grid_smoke"|"cli"|"gui", argv)``. Spawn and
    grid-smoke are decided here and do not call ``route_argv``. CLI and
    GUI receive ``argv`` without the program name (``route_argv``'s
    contract).
    """
    args = list(argv)
    if is_multiprocessing_child(args):
        return "spawn", args
    if is_grid_smoke(args):
        return "grid_smoke", args
    from src.live.headless import route_argv
    return route_argv(args[1:])
