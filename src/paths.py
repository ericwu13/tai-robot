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
