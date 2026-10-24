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
    assert "SKCOM.dll is missing" in out
    assert "CTSecuritiesATL.dll is missing" in out


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
    assert "SKCOM.dll is missing" in out
    assert "CTSecuritiesATL.dll is missing" in out


def test_non_broker_dll_does_not_count(tmp_path, capsys):
    root = tmp_path / "stray"
    _stage(root, "notbroker.dll")
    with pytest.raises(SystemExit) as exc:
        build_release.prepare_capital_sdk_binaries(
            environ={}, project_root=str(root),
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "TAI_CAPITAL_SDK_DIR" in out
    assert "SKCOM.dll is missing" in out
    assert "CTSecuritiesATL.dll is missing" in out


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


@pytest.mark.parametrize("value", ["true", "yes", "TRUE", "0"])
def test_unrecognized_opt_out_warns_and_fails_closed(tmp_path, capsys, value):
    root = tmp_path / "ci"
    root.mkdir()
    with pytest.raises(SystemExit) as exc:
        build_release.prepare_capital_sdk_binaries(
            environ={build_release.ENV_ALLOW_MISSING_CAPITAL_SDK: value},
            project_root=str(root),
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "Only '1' opts out" in out
    assert repr(value) in out
    assert "SKCOM.dll is missing" in out


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
    assert "dll_binaries = prepare_capital_sdk_binaries(project_root=SPECPATH)" in spec
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
    assert "CTSecuritiesATL.dll" in readme
    assert "Only the value `1` opts out" in readme
    assert "SPECPATH" in readme
    assert "relative value is resolved against the project root" in readme
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


# ── both required DLLs, spec path and build() ───────────────────────


_PARTIAL_DLL_CASES = [
    ({"SKCOM.dll": b"SK"}, "CTSecuritiesATL.dll is missing"),
    ({"CTSecuritiesATL.dll": b"CT"}, "SKCOM.dll is missing"),
    ({"SKCOM.dll": b"", "CTSecuritiesATL.dll": b"CT"}, "SKCOM.dll is empty"),
    (
        {"SKCOM.dll": b"SK", "CTSecuritiesATL.dll": b""},
        "CTSecuritiesATL.dll is empty",
    ),
]


def _other_problem_phrases(problem: str) -> list[str]:
    phrases = []
    for name in build_release.REQUIRED_BROKER_DLLS:
        for kind in ("is missing", "is empty"):
            phrase = f"{name} {kind}"
            if phrase != problem:
                phrases.append(phrase)
    return phrases


@pytest.mark.parametrize("payloads,problem", _PARTIAL_DLL_CASES)
def test_spec_path_rejects_partial_or_empty_dll(tmp_path, capsys, payloads, problem):
    """The spec calls ``prepare_capital_sdk_binaries(project_root=SPECPATH)``."""
    root = tmp_path / "spec-root"
    libs = _libs(root)
    libs.mkdir(parents=True)
    for name, data in payloads.items():
        (libs / name).write_bytes(data)
    with pytest.raises(SystemExit) as exc:
        build_release.prepare_capital_sdk_binaries(
            environ={}, project_root=str(root),
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert problem in out
    for phrase in _other_problem_phrases(problem):
        assert phrase not in out


def test_spec_path_both_nonzero_matches_historical_glob(tmp_path, monkeypatch, capsys):
    _stage(tmp_path, "SKCOM.dll", "CTSecuritiesATL.dll", "SKProxyLIB.dll")
    monkeypatch.chdir(tmp_path)
    got = build_release.prepare_capital_sdk_binaries(
        environ={}, project_root=str(tmp_path),
    )
    assert got == _historical_spec_binaries(build_release.DEFAULT_CAPITAL_SDK_LIBS)
    assert all(not os.path.isabs(src) for src, _dest in got)
    assert all(dest == "libs" for _src, dest in got)
    out = capsys.readouterr().out
    assert "ERROR" not in out
    assert "WARNING" not in out


def _silence_build_side_effects(monkeypatch):
    calls = []

    def fake_run(cmd, cwd=None):
        calls.append(list(cmd))
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(build_release.subprocess, "run", fake_run)
    monkeypatch.setattr(build_release, "strategies_datas", lambda _root: [])
    monkeypatch.setattr(build_release, "warn_build_requirements", lambda _root: {})
    return calls


@pytest.mark.parametrize("payloads,problem", _PARTIAL_DLL_CASES)
def test_build_rejects_partial_or_empty_dll(monkeypatch, tmp_path, capsys, payloads, problem):
    libs = tmp_path / "libs"
    libs.mkdir()
    for name, data in payloads.items():
        (libs / name).write_bytes(data)
    monkeypatch.setenv(build_release.ENV_CAPITAL_SDK_DIR, str(libs))
    monkeypatch.setenv(build_release.ENV_ALLOW_MISSING_CAPITAL_SDK, "")
    calls = _silence_build_side_effects(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        build_release.build()
    assert exc.value.code == 1
    assert calls == []
    out = capsys.readouterr().out
    assert problem in out
    for phrase in _other_problem_phrases(problem):
        assert phrase not in out
    assert "=== Building" not in out


def test_build_both_nonzero_reaches_pyinstaller(monkeypatch, tmp_path, capsys):
    libs = tmp_path / "libs"
    libs.mkdir()
    (libs / "SKCOM.dll").write_bytes(b"SK")
    (libs / "CTSecuritiesATL.dll").write_bytes(b"CT")
    (libs / "SKProxyLIB.dll").write_bytes(b"PX")
    monkeypatch.setenv(build_release.ENV_CAPITAL_SDK_DIR, str(libs))
    monkeypatch.setenv(build_release.ENV_ALLOW_MISSING_CAPITAL_SDK, "")
    calls = _silence_build_side_effects(monkeypatch)
    build_release.build()
    assert calls and calls[0][1:3] == ["-m", "PyInstaller"]
    out = capsys.readouterr().out
    assert "ERROR" not in out
    assert "is missing" not in out
    assert "is empty" not in out


def test_relative_sdk_dir_resolves_against_project_root_not_cwd(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    rel = os.path.join("vendor", "libs")
    libs = project / "vendor" / "libs"
    libs.mkdir(parents=True)
    (libs / "SKCOM.dll").write_bytes(b"SK")
    (libs / "CTSecuritiesATL.dll").write_bytes(b"CT")
    elsewhere = tmp_path / "elsewhere"
    decoy = elsewhere / "vendor" / "libs"
    decoy.mkdir(parents=True)
    (decoy / "SKCOM.dll").write_bytes(b"NO")
    (decoy / "CTSecuritiesATL.dll").write_bytes(b"NO")
    monkeypatch.chdir(elsewhere)
    got = build_release.prepare_capital_sdk_binaries(
        environ={build_release.ENV_CAPITAL_SDK_DIR: rel},
        project_root=str(project),
    )
    blobs = {os.path.basename(src): Path(src).read_bytes() for src, dest in got}
    assert blobs == {"CTSecuritiesATL.dll": b"CT", "SKCOM.dll": b"SK"}
    assert {dest for _src, dest in got} == {"libs"}


def test_build_passes_script_directory_as_project_root(monkeypatch):
    seen = {}

    def fake_prepare(environ=None, *, project_root=None, allow_missing_flag=False):
        seen["project_root"] = project_root
        env = os.environ if environ is None else environ
        seen["sdk"] = env.get(build_release.ENV_CAPITAL_SDK_DIR)
        return []

    monkeypatch.setattr(build_release, "prepare_capital_sdk_binaries", fake_prepare)
    monkeypatch.setenv(
        build_release.ENV_CAPITAL_SDK_DIR, os.path.join("vendor", "libs"),
    )
    _silence_build_side_effects(monkeypatch)
    build_release.build()
    root = os.path.dirname(os.path.abspath(build_release.__file__))
    assert seen["project_root"] == root
    assert seen["sdk"] == os.path.join("vendor", "libs")


def test_build_unrecognized_opt_out_fails_before_pyinstaller(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.setenv(build_release.ENV_CAPITAL_SDK_DIR, str(tmp_path / "missing"))
    monkeypatch.setenv(build_release.ENV_ALLOW_MISSING_CAPITAL_SDK, "yes")
    calls = []
    monkeypatch.setattr(
        build_release.subprocess, "run", lambda *_a, **_k: calls.append(True),
    )
    with pytest.raises(SystemExit) as exc:
        build_release.build()
    assert exc.value.code == 1
    assert calls == []
    out = capsys.readouterr().out
    assert "Only '1' opts out" in out
    assert repr("yes") in out


def test_unrecognized_opt_out_does_not_drop_a_present_sdk(tmp_path, capsys):
    libs = _stage(tmp_path, "SKCOM.dll", "CTSecuritiesATL.dll")
    got = build_release.prepare_capital_sdk_binaries(
        environ={build_release.ENV_ALLOW_MISSING_CAPITAL_SDK: "yes"},
        project_root=str(tmp_path),
    )
    assert got == _historical_spec_binaries(str(libs))
    out = capsys.readouterr().out
    assert "Only '1' opts out" in out
    assert "ERROR" not in out


def test_required_broker_dlls_are_the_shared_pair():
    assert build_release.REQUIRED_BROKER_DLLS == ("SKCOM.dll", "CTSecuritiesATL.dll")
