"""Release-EXE headless routing (Option A) — pure tests, no Tk and no COM.

Covers argv routing, the frozen ``--detach`` child command, install-dir
roots, the semi_auto refusal, and the PyInstaller spec / SOURCE_PATHS
contract. Nothing here starts a bot.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

from scripts.monitor.check_deploy import REDEPLOY_RECIPE
from src.live import headless as hl
from src import paths

REPO = Path(__file__).resolve().parent.parent
CLI_COMMANDS = ("deploy", "stop", "list", "status", "strategies", "backtest")


# ── route_argv ───────────────────────────────────────────────────────

def test_route_empty_and_help_and_unknown_stay_on_gui():
    assert hl.route_argv([]) == ("gui", [])
    assert hl.route_argv(None) == ("gui", [])
    for argv in (["-h"], ["--help"], ["nope"], ["--version"], ["Deploy"]):
        assert hl.route_argv(argv) == ("gui", argv)


@pytest.mark.parametrize("cmd", CLI_COMMANDS)
def test_route_each_cli_command(cmd):
    argv = [cmd, "--symbol", "TMF00"]
    assert hl.route_argv(argv) == ("cli", argv)


def test_route_help_after_subcommand_stays_cli():
    argv = ["deploy", "--help"]
    assert hl.route_argv(argv) == ("cli", argv)


def test_cli_commands_match_parser_and_router():
    names = set()
    for action in hl.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            names = set(action.choices)
    assert names == set(hl.CLI_COMMANDS) == set(CLI_COMMANDS)


def test_gui_main_sequence_has_no_cli_import():
    text = (REPO / "run_backtest.py").read_text(encoding="utf-8")
    body = text.split("def main():", 1)[1].split('if __name__ == "__main__":', 1)[0]
    assert body.index("enable_dpi_awareness()") < body.index("_init_com()")
    assert body.index("_init_com()") < body.index("register_com_events()")
    assert "tk.Tk()" in body and "BacktestApp" in body and "mainloop()" in body
    assert "run_bot_cli" not in body


def test_dunder_main_freezes_then_routes_without_second_com_init():
    text = (REPO / "run_backtest.py").read_text(encoding="utf-8")
    block = text.split('if __name__ == "__main__":', 1)[1]
    assert block.index("freeze_support()") < block.index("route_argv")
    assert block.index("route_argv") < block.index("import run_bot_cli")
    assert "sys.exit(run_bot_cli.main(routed))" in block
    assert "enable_dpi_awareness()" not in block
    assert "_init_com()" not in block


# ── child_command ────────────────────────────────────────────────────

def _raw_deploy():
    return [
        "deploy", "--detach", "--symbol", "TMF00", "--bot", "b",
        "--wait-ready", "240", "--strategy", "s", "--wait-ready=5",
    ]


def test_child_command_frozen_has_no_py_and_strips_detach_once(tmp_path):
    exe = str(tmp_path / "tai_backtest.exe")
    script = str(tmp_path / "run_bot_cli.py")
    raw = _raw_deploy()
    cmd = hl.child_command(raw, frozen=True, executable=exe, script=script)
    assert cmd == [exe, "deploy", "--symbol", "TMF00", "--bot", "b", "--strategy", "s"]
    assert not any(tok.endswith(".py") for tok in cmd)
    assert "--detach" not in cmd and "--wait-ready" not in cmd
    # stripping an already-stripped argv is a no-op (exactly once)
    again = hl.child_command(cmd[1:], frozen=True, executable=exe, script=script)
    assert again == cmd


def test_child_command_source_keeps_script(tmp_path):
    exe = str(tmp_path / "python")
    script = str(tmp_path / "run_bot_cli.py")
    raw = _raw_deploy()
    cmd = hl.child_command(raw, frozen=False, executable=exe, script=script)
    assert cmd == [exe, script, "deploy", "--symbol", "TMF00", "--bot", "b",
                   "--strategy", "s"]
    again = hl.child_command(cmd[2:], frozen=False, executable=exe, script=script)
    assert again == cmd


def test_launch_detached_frozen_cwd_and_command(monkeypatch, tmp_path):
    exe_dir = tmp_path / "install"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "tai_backtest.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)

    monkeypatch.setattr(sys, "path", list(sys.path))
    import run_bot_cli

    captured = {}

    def fake_spawn(cmd, stdout_path, cwd=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["stdout"] = stdout_path

        class _Proc:
            pid = 99

            def poll(self):
                return None

        return _Proc()

    monkeypatch.setattr(run_bot_cli, "spawn_detached", fake_spawn)

    class _LR:
        @staticmethod
        def bot_dir_for(base, symbol, bot):
            captured["base"] = base
            d = tmp_path / f"{symbol}_{bot}"
            d.mkdir(exist_ok=True)
            return str(d)

    monkeypatch.setattr("src.live.live_runner.LiveRunner", _LR)

    raw = ["deploy", "--symbol", "TMF00", "--bot", "smoke", "--strategy", "x",
           "--detach", "--wait-ready", "0"]
    args = hl.build_parser().parse_args(raw)
    args._raw_argv = raw
    assert run_bot_cli.launch_detached(args) == 0
    assert captured["base"] == str(exe_dir / "data" / "live")
    assert captured["cwd"] == str(exe_dir)
    assert captured["cmd"][0] == str(exe_dir / "tai_backtest.exe")
    assert captured["cmd"][1] == "deploy"
    assert "--detach" not in captured["cmd"]
    assert "--wait-ready" not in captured["cmd"]
    assert not any(tok.endswith(".py") for tok in captured["cmd"])
    assert captured["stdout"].endswith("cli_stdout.log")


# ── project_root / bundle_root ───────────────────────────────────────

def test_roots_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    root = paths.project_root()
    expected = os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__)))
    assert root == expected
    assert paths.bundle_root() == root


def test_roots_frozen(monkeypatch, tmp_path):
    exe_dir = tmp_path / "install"
    exe_dir.mkdir()
    meipass = tmp_path / "_MEI999"
    meipass.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "tai_backtest.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    assert paths.project_root() == str(exe_dir)
    assert paths.bundle_root() == os.path.abspath(str(meipass))


def test_roots_frozen_without_meipass_uses_exe_dir(monkeypatch, tmp_path):
    exe_dir = tmp_path / "install"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "tai_backtest.exe"))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert paths.project_root() == str(exe_dir)
    assert paths.bundle_root() == str(exe_dir)


def test_cli_data_and_strategies_follow_frozen_root(monkeypatch, tmp_path):
    exe_dir = tmp_path / "install"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "tai_backtest.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    import run_bot_cli
    assert run_bot_cli._base_dir() == str(exe_dir / "data" / "live")
    assert run_bot_cli._strategies_dir() == str(exe_dir / "strategies")


# ── semi_auto stays refused, and names the GUI ──────────────────────

def test_headless_modes_are_paper_and_auto_only():
    assert hl.HEADLESS_MODES == ("paper", "auto")
    assert "semi_auto" not in hl.HEADLESS_MODES


def test_build_request_semi_auto_names_gui():
    args = argparse.Namespace(
        mode="semi_auto", loss_limit="1000", regime=False,
        long_strategy="", short_strategy="", strategy="x",
        news=False, news_tier2=False, news_directional=False, bot="b",
    )
    with pytest.raises(hl.HeadlessConfigError, match="GUI"):
        hl.build_deploy_request(args, None)


def test_semi_auto_cli_exits_nonzero_names_gui_and_builds_no_app(capsys, monkeypatch):
    import src.live.headless_app as ha
    import run_bot_cli

    calls = []

    def _boom(*_a, **_k):
        calls.append("HeadlessBotApp")
        raise AssertionError("HeadlessBotApp constructed")

    monkeypatch.setattr(ha, "HeadlessBotApp", _boom)
    monkeypatch.setattr(ha, "run_deploy", lambda *_a, **_k: calls.append("run_deploy"))
    with pytest.raises(SystemExit) as exc:
        run_bot_cli.main([
            "deploy", "--symbol", "TMF00", "--bot", "b1",
            "--strategy", "x", "--mode", "semi_auto",
        ])
    assert exc.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "GUI" in err
    assert calls == []


# ── spec / build / operator copy ─────────────────────────────────────

def test_spec_hiddenimports_and_source_paths():
    spec = (REPO / "tai_backtest.spec").read_text(encoding="utf-8")
    assert spec.count("Analysis(") == 1
    assert "['run_backtest.py']" in spec
    assert spec.count("EXE(") == 1
    assert "console=True" in spec
    for name in (
        "run_bot_cli",
        "src.paths",
        "src.live.headless",
        "src.live.headless_app",
        "src.live.deploy_request",
        "src.live.session_store",
        "src.ai.strategy_store",
    ):
        assert f"'{name}'" in spec
    import build_release
    assert "run_bot_cli.py" in build_release.SOURCE_PATHS
    assert "tai_backtest.spec" in build_release.SOURCE_PATHS


def test_redeploy_recipe_names_exe_and_source_headless():
    text = "\n".join(REDEPLOY_RECIPE)
    assert "NO headless" not in text
    assert "tai_backtest.exe" in text
    assert "run_bot_cli.py" in text
    assert "python.exe" in text
    assert "semi_auto" in text
    assert "GUI" in text
    assert "mode_override.json" in text


def test_install_notes_document_headless_exe():
    text = (REPO / "必看安裝說明.txt").read_text(encoding="utf-8")
    assert "無介面" in text
    for snippet in (
        "tai_backtest.exe list",
        "tai_backtest.exe strategies",
        "--mode paper --detach --wait-ready 240",
        "--json",
        "--wait 60",
        "--resume",
        "semi_auto",
        "TAI_USER_ID",
        "TAI_PASSWORD",
        "cli_stdout.log",
        "LocalSystem",
    ):
        assert snippet in text
