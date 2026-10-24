"""EXE build fails when the Capital API broker DLLs are absent.

A worktree or fresh clone has no ``CapitalAPI_2.13.57`` tree. The spec
used to treat that as success and ship an EXE with an empty
``_internal/libs``. These tests do not run PyInstaller and do not
write SDK binaries into the repo.
"""

import glob
import os
import sys
from pathlib import Path

import pytest

import build_release

REPO = Path(__file__).resolve().parent.parent


def _libs(root: Path) -> Path:
    return root / build_release.DEFAULT_CAPITAL_SDK_LIBS


def _historical_spec_binaries(sdk_libs: str) -> list[tuple[str, str]]:
    """The loop ``tai_backtest.spec`` used before the fail-closed check."""
    dll_binaries = []
    if os.path.isdir(sdk_libs):
        for dll in glob.glob(os.path.join(sdk_libs, "*.dll")):
            dll_binaries.append((dll, "libs"))
    return dll_binaries


def _stage(root: Path, *names: str) -> Path:
    libs = _libs(root)
    libs.mkdir(parents=True)
    for name in names:
        (libs / name).write_bytes(b"dll:" + name.encode("ascii"))
    return libs


# ── resolver cases ──────────────────────────────────────────────────


def test_missing_dir_exits_and_names_path_and_env(tmp_path, capsys):
    root = tmp_path / "fresh-clone"
    root.mkdir()
    expected = str(_libs(root))
    with pytest.raises(SystemExit) as exc:
        build_release.prepare_capital_sdk_binaries(
            environ={}, project_root=str(root),
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert expected in out
    assert "TAI_CAPITAL_SDK_DIR" in out
    assert build_release.DEFAULT_CAPITAL_SDK_LIBS in out
    assert "SKCOM.dll" in out


def test_empty_dir_exits(tmp_path, capsys):
    root = tmp_path / "empty-sdk"
    libs = _libs(root)
    libs.mkdir(parents=True)
    (libs / "README.txt").write_text("not a dll\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        build_release.prepare_capital_sdk_binaries(
            environ={}, project_root=str(root),
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert str(libs) in out
    assert "TAI_CAPITAL_SDK_DIR" in out
    assert "SKCOM.dll" in out


def test_non_broker_dll_does_not_count(tmp_path, capsys):
    root = tmp_path / "stray"
    _stage(root, "notbroker.dll")
    with pytest.raises(SystemExit) as exc:
        build_release.prepare_capital_sdk_binaries(
            environ={}, project_root=str(root),
        )
    assert exc.value.code == 1
    assert "TAI_CAPITAL_SDK_DIR" in capsys.readouterr().out


def test_env_override_supplies_broker_dlls(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    other = tmp_path / "sdk-libs"
    other.mkdir()
    (other / "SKCOM.dll").write_bytes(b"SK")
    (other / "CTSecuritiesATL.dll").write_bytes(b"CT")
    (other / "notes.txt").write_text("skip\n", encoding="utf-8")
    got = build_release.prepare_capital_sdk_binaries(
        environ={build_release.ENV_CAPITAL_SDK_DIR: str(other)},
        project_root=str(root),
    )
    assert got == _historical_spec_binaries(str(other))
    names = sorted(os.path.basename(src) for src, dest in got)
    assert names == ["CTSecuritiesATL.dll", "SKCOM.dll"]
    assert {dest for _src, dest in got} == {"libs"}


def test_env_override_missing_does_not_fall_back(tmp_path, capsys):
    root = tmp_path / "checkout"
    _stage(root, "SKCOM.dll")
    missing = tmp_path / "no-such-libs"
    with pytest.raises(SystemExit) as exc:
        build_release.prepare_capital_sdk_binaries(
            environ={build_release.ENV_CAPITAL_SDK_DIR: str(missing)},
            project_root=str(root),
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert str(missing) in out
    assert "TAI_CAPITAL_SDK_DIR" in out


def test_opt_out_env_warns_and_continues(tmp_path, capsys):
    root = tmp_path / "ci"
    root.mkdir()
    got = build_release.prepare_capital_sdk_binaries(
        environ={build_release.ENV_ALLOW_MISSING_CAPITAL_SDK: "1"},
        project_root=str(root),
    )
    assert got == []
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "TAI_ALLOW_MISSING_CAPITAL_SDK" in out
    assert "--allow-missing-broker-sdk" in out
    assert str(_libs(root)) in out


def test_opt_out_flag_warns_without_env(tmp_path, capsys):
    root = tmp_path / "ci"
    root.mkdir()
    got = build_release.prepare_capital_sdk_binaries(
        environ={}, project_root=str(root), allow_missing_flag=True,
    )
    assert got == []
    assert "WARNING" in capsys.readouterr().out


def test_opt_out_requires_exact_one(tmp_path):
    root = tmp_path / "ci"
    root.mkdir()
    with pytest.raises(SystemExit) as exc:
        build_release.prepare_capital_sdk_binaries(
            environ={build_release.ENV_ALLOW_MISSING_CAPITAL_SDK: "true"},
            project_root=str(root),
        )
    assert exc.value.code == 1


def test_present_sdk_matches_historical_spec_glob(tmp_path, monkeypatch, capsys):
    """Default relative path, no env: same tuples the old spec appended."""
    _stage(tmp_path, "SKCOM.dll", "CTSecuritiesATL.dll", "SKProxyLIB.dll")
    (_libs(tmp_path) / "license.txt").write_text("not bundled\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    got = build_release.prepare_capital_sdk_binaries(environ={})
    expected = _historical_spec_binaries(build_release.DEFAULT_CAPITAL_SDK_LIBS)
    assert got == expected
    assert got
    assert all(not os.path.isabs(src) for src, _dest in got)
    assert all(dest == "libs" for _src, dest in got)
    staged = {os.path.basename(src): Path(src).read_bytes() for src, _d in got}
    again = {
        os.path.basename(src): Path(src).read_bytes()
        for src, _d in build_release.prepare_capital_sdk_binaries(
            environ={}, project_root=str(tmp_path),
        )
    }
    assert staged == again
    assert set(staged) == {"SKCOM.dll", "CTSecuritiesATL.dll", "SKProxyLIB.dll"}
    assert "license.txt" not in staged
    out = capsys.readouterr().out
    assert "WARNING" not in out
    assert "ERROR" not in out


def test_opt_out_with_sdk_present_still_bundles(tmp_path, capsys):
    libs = _stage(tmp_path, "SKCOM.dll", "CTSecuritiesATL.dll")
    got = build_release.prepare_capital_sdk_binaries(
        environ={build_release.ENV_ALLOW_MISSING_CAPITAL_SDK: "1"},
        project_root=str(tmp_path),
    )
    assert got == _historical_spec_binaries(str(libs))
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "still be bundled" in out


def test_default_path_constant_is_the_historical_spec_path():
    assert build_release.DEFAULT_CAPITAL_SDK_LIBS == os.path.join(
        "CapitalAPI_2.13.57",
        "CapitalAPI_2.13.57_PythonExample",
        "SKDLLPythonTester",
        "libs",
    )


# ── spec and build_release wiring ───────────────────────────────────


def test_spec_checks_sdk_and_adds_no_datas():
    spec = (REPO / "tai_backtest.spec").read_text(encoding="utf-8")
    assert "dll_binaries = prepare_capital_sdk_binaries()" in spec
    assert "if os.path.isdir(sdk_libs)" not in spec
    assert "binaries=dll_binaries" in spec
    datas = spec.split("datas=[", 1)[1].split("],", 1)[0]
    assert "('src', 'src')" in datas
    assert "settings.example.yaml" in datas
    assert "CapitalAPI" not in datas
    assert "SKCOM" not in datas
    assert ".dll" not in datas
    hidden = spec.split("hiddenimports=[", 1)[1].split("],", 1)[0]
    assert "build_release" not in hidden
    assert "capital_sdk" not in hidden


def test_readme_names_sdk_path_and_env():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "TAI_CAPITAL_SDK_DIR" in readme
    assert "TAI_ALLOW_MISSING_CAPITAL_SDK" in readme
    assert "--allow-missing-broker-sdk" in readme
    assert (
        "CapitalAPI_2.13.57/CapitalAPI_2.13.57_PythonExample/"
        "SKDLLPythonTester/libs"
    ) in readme


def test_build_exits_before_pyinstaller_when_sdk_missing(monkeypatch, tmp_path, capsys):
    missing = tmp_path / "missing-libs"
    monkeypatch.setenv(build_release.ENV_CAPITAL_SDK_DIR, str(missing))
    monkeypatch.setenv(build_release.ENV_ALLOW_MISSING_CAPITAL_SDK, "")
    calls = []

    def fake_run(*_args, **_kwargs):
        calls.append(True)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(build_release.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as exc:
        build_release.build()
    assert exc.value.code == 1
    assert calls == []
    out = capsys.readouterr().out
    assert str(missing) in out
    assert "TAI_CAPITAL_SDK_DIR" in out
    assert "=== Building" not in out


def test_build_flag_warns_and_exports_opt_out_before_pyinstaller(
    monkeypatch, tmp_path, capsys,
):
    missing = tmp_path / "missing-libs"
    monkeypatch.setenv(build_release.ENV_CAPITAL_SDK_DIR, str(missing))
    monkeypatch.setenv(build_release.ENV_ALLOW_MISSING_CAPITAL_SDK, "")
    seen = {}

    def fake_run(cmd, cwd=None):
        seen["cmd"] = list(cmd)
        seen["cwd"] = cwd
        seen["opt_out"] = os.environ.get(build_release.ENV_ALLOW_MISSING_CAPITAL_SDK)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(build_release.subprocess, "run", fake_run)
    monkeypatch.setattr(build_release, "strategies_datas", lambda _root: [])
    monkeypatch.setattr(build_release, "warn_build_requirements", lambda _root: {})
    build_release.build(allow_missing_broker_sdk=True)
    assert seen["cmd"][1:3] == ["-m", "PyInstaller"]
    assert seen["opt_out"] == "1"
    assert seen["cwd"] == os.path.dirname(os.path.abspath(build_release.__file__))
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "TAI_ALLOW_MISSING_CAPITAL_SDK" in out
    assert "--allow-missing-broker-sdk" in out


def test_main_forwards_allow_missing_broker_sdk(monkeypatch):
    seen = []

    def fake_build(*, install_requirements=False, allow_missing_broker_sdk=False):
        seen.append((install_requirements, allow_missing_broker_sdk))

    monkeypatch.setattr(build_release, "build", fake_build)
    monkeypatch.setattr(build_release, "git_commit_info", lambda: ("abc", False))
    monkeypatch.setattr(build_release, "verify_bundle_matches_source", lambda: None)
    monkeypatch.setattr(build_release, "write_build_stamp", lambda *_a, **_k: None)
    monkeypatch.setattr(build_release, "package", lambda: "dist/x.zip")
    monkeypatch.setattr(build_release, "write_checksum", lambda _path: "dist/x.zip.sha256")

    def run(argv):
        seen.clear()
        monkeypatch.setattr(sys, "argv", argv)
        build_release.main()
        assert len(seen) == 1
        return seen[0]

    assert run([
        "build_release.py", "--skip-release", "--allow-missing-broker-sdk",
    ]) == (False, True)
    assert run(["build_release.py", "--skip-release"]) == (False, False)
