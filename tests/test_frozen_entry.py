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


def test_spawn_guard_runs_before_tk_import():
    text = (REPO / "run_backtest.py").read_text(encoding="utf-8")
    pre_tk = text[:text.index("\nimport tkinter")]
    assert pre_tk.count('if __name__ == "__main__":') == 1
    assert pre_tk.index("freeze_support()") < pre_tk.index("dispatch_entry")
    assert "route_argv(" not in pre_tk
    assert "_grid_smoke_main" in pre_tk
    assert "enable_dpi_awareness()" not in pre_tk
    assert "_init_com()" not in pre_tk
    bottom = text.split('if __name__ == "__main__":')[-1]
    assert bottom.index("dispatch_entry") < bottom.index("import run_bot_cli")
    assert "sys.exit(run_bot_cli.main(routed))" in bottom
    assert "\n    main()\n" in bottom
    assert "enable_dpi_awareness()" not in bottom
    assert "_init_com()" not in bottom


def test_spawn_child_never_calls_route_argv_or_gui(monkeypatch):
    calls = []
    real = hl.route_argv

    def spy(argv):
        calls.append(list(argv))
        return real(argv)

    monkeypatch.setattr(hl, "route_argv", spy)
    argv = ["tai_backtest.exe", "--multiprocessing-fork", "parent_pid=1", "pipe_handle=0"]
    assert paths.dispatch_entry(argv) == ("spawn", argv)
    assert calls == []
    # The flag only counts at argv[1]. A later copy is not a spawn child.
    later = ["tai_backtest.exe", "deploy", "--multiprocessing-fork"]
    assert paths.dispatch_entry(later)[0] == "cli"
    assert calls == [["deploy", "--multiprocessing-fork"]]


def test_grid_smoke_is_not_routed(monkeypatch):
    calls = []

    def boom(argv):
        calls.append(list(argv))
        return ("gui", list(argv))

    monkeypatch.setattr(hl, "route_argv", boom)
    argv = ["tai_backtest.exe", "--grid-subprocess-smoke", "--hang"]
    assert paths.dispatch_entry(argv) == ("grid_smoke", argv)
    assert calls == []
    assert paths.skips_gui_and_com(argv)
    assert paths.skips_gui_and_com(
        ["tai_backtest.exe", "--multiprocessing-fork", "x"])


def test_no_args_and_help_stay_gui_and_cli_still_routes():
    assert paths.dispatch_entry(["tai_backtest.exe"]) == ("gui", [])
    assert paths.dispatch_entry(["tai_backtest.exe", "--help"]) == ("gui", ["--help"])
    assert paths.dispatch_entry(["tai_backtest.exe", "-h"]) == ("gui", ["-h"])
    assert paths.dispatch_entry(["tai_backtest.exe", "nope"]) == ("gui", ["nope"])
    for cmd in CLI_COMMANDS:
        kind, routed = paths.dispatch_entry(
            ["tai_backtest.exe", cmd, "--symbol", "TMF00"])
        assert kind == "cli"
        assert routed == [cmd, "--symbol", "TMF00"]
    paper = ["tai_backtest.exe", "deploy", "--symbol", "TMF00", "--bot", "b",
             "--strategy", "x", "--mode", "paper"]
    assert paths.dispatch_entry(paper) == ("cli", paper[1:])
    assert not paths.skips_gui_and_com(paper)


def test_spawn_process_exits_before_gui():
    """A real interpreter: the fork argv must exit, not enter mainloop."""
    import subprocess
    proc = subprocess.run(
        [sys.executable, str(REPO / "run_backtest.py"),
         "--multiprocessing-fork", "parent_pid=1"],
        cwd=REPO, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    assert "Traceback" not in proc.stderr
    assert proc.stdout == ""


def test_cli_spawn_child_exits_zero_before_headless():
    """M9: the spawn child in run_bot_cli.py must SystemExit(0).

    Deleting that exit lets ``--multiprocessing-fork`` fall through into
    the headless imports and argparse. The process then does not return
    0 with empty stdout and stderr.
    """
    import subprocess
    proc = subprocess.run(
        [sys.executable, str(REPO / "run_bot_cli.py"),
         "--multiprocessing-fork", "parent_pid=1"],
        cwd=REPO, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert "Traceback" not in proc.stderr
    assert "usage:" not in proc.stderr.lower()


def test_grid_smoke_process_does_not_enter_gui():
    """The smoke flag finishes in grid_smoke, not in the Tk mainloop.

    ``--payload-bytes`` is not a GridOutcome, so #194's smoke exits 1.
    The router must not swallow the flag and open the GUI.
    """
    import subprocess
    proc = subprocess.run(
        [sys.executable, str(REPO / "run_backtest.py"),
         "--grid-subprocess-smoke", "--payload-bytes", "64",
         "--timeout-s", "20"],
        cwd=REPO, capture_output=True, text=True, timeout=40,
    )
    combined = proc.stdout + proc.stderr
    assert "mainloop" not in combined
    assert "Traceback" not in combined
    assert proc.returncode == 1
    assert "grid-smoke: mode=payload" in combined
    assert "not a grid outcome" in combined


def test_comtypes_hook_skips_only_spawn_and_smoke():
    import runtime_hook_comtypes as hook
    paper = ["tai_backtest.exe", "deploy", "--symbol", "TMF00", "--bot", "b",
             "--mode", "paper"]
    assert hook.should_install_comtypes(paper)
    assert hook.should_install_comtypes(["tai_backtest.exe", "list"])
    assert hook.should_install_comtypes(["tai_backtest.exe"])
    assert not hook.should_install_comtypes(
        ["tai_backtest.exe", "--multiprocessing-fork", "parent_pid=1"])
    assert not hook.should_install_comtypes(
        ["tai_backtest.exe", "--grid-subprocess-smoke"])
    text = (REPO / "runtime_hook_comtypes.py").read_text(encoding="utf-8")
    assert "def ensure_comtypes_gen(" in text
    assert "should_install_comtypes" in text
    assert text.index("if should_install_comtypes():") < text.index(
        "        ensure_comtypes_gen(cache_dir)")


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
    assert "runtime_hook_comtypes.py" in build_release.SOURCE_PATHS
    assert list(build_release.SOURCE_PATHS) == list(dict.fromkeys(build_release.SOURCE_PATHS))
    # Quoted hiddenimport names, once each. #194/#195 add more names in
    # other hunks; a restack that duplicates one of these fails here.
    import re
    names = re.findall(r"'((?:src\.|run_bot_cli|comtypes)[^']*)'", spec)
    assert names.count("run_bot_cli") == 1
    assert names.count("src.paths") == 1
    assert names.count("src.live.headless") == 1
    assert names.count("src.ai.strategy_store") == 1


def test_redeploy_recipe_names_exe_and_source_headless():
    text = "\n".join(REDEPLOY_RECIPE)
    assert "NO headless" not in text
    assert "tai_backtest.exe" in text
    assert "run_bot_cli.py" in text
    assert "python.exe" in text
    assert "semi_auto" in text
    assert "GUI" in text
    assert "mode_override.json" in text


def test_v2199_checklist_is_one_combined_oreopie_run():
    text = (REPO / "docs/testing/v2199_combined_windows_checklist.md").read_text(
        encoding="utf-8")
    assert "C:\\tai-test-177\\" in text
    assert ".venv-build\\Scripts\\python.exe" in text
    assert "pip install -e ." in text
    assert "requirements-build.txt" in text
    assert "build_release.py --skip-release" in text
    assert "--payload-bytes 131072" in text
    assert "--mode paper" in text
    assert "ExecutablePath" in text
    assert "Stop-Process -Id" in text
    assert "taskkill /IM tai_backtest.exe" in text
    assert "README.txt" in text
    assert "_comtypes_cache" in text
    assert "needs Eric's go-ahead" in text
    assert "Do not run `Activate.ps1`" in text
    assert "MainWindowHandle" in text
    assert "conhost.exe" in text
    assert "from src.paths import skips_gui_and_com" in text
    assert "(f) exit code (must be 1):" in text
    assert "outcome=crash" in text
    assert "not a grid outcome" in text
    assert "result_bytes` is greater than 131072" in text
    assert "(f) elapsed_s (must be below --timeout-s 60, no hang):" in text
    assert "An exit 1 without both" in text
    assert "(f) exit code (must be 0; exit 1 is a fail):" not in text


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
