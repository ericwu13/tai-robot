"""EXE build: missing strategies dir, and the comtypes.gen runtime hook.

``strategies/`` is gitignored. PyInstaller aborts when that datas source
is missing, and bundling it would ship private AI strategies. The runtime
hook used to import ``comtypes.gen`` unconditionally; PyInstaller 6.22.3
+ comtypes 1.4.17 omit that package and the EXE dies at startup. These
tests run on Linux — they do not launch a Windows EXE.
"""

import importlib
import importlib.abc
import os
import sys
import types
from pathlib import Path

import pytest

import build_release
from runtime_hook_comtypes import _install_comtypes_gen, ensure_comtypes_gen

REPO = Path(__file__).resolve().parent.parent


def _comtypes_snapshot():
    return {
        name: sys.modules[name]
        for name in list(sys.modules)
        if name == "comtypes" or name.startswith("comtypes.")
    }


def _restore_comtypes(snapshot):
    for name in list(sys.modules):
        if name == "comtypes" or name.startswith("comtypes."):
            sys.modules.pop(name, None)
    sys.modules.update(snapshot)


# ── strategies datas ────────────────────────────────────────────────


def test_strategies_datas_missing_dir_stages_empty_placeholder(tmp_path):
    project = tmp_path / "fresh-clone"
    project.mkdir()
    datas = build_release.strategies_datas(str(project))
    assert len(datas) == 1
    source, dest = datas[0]
    assert dest == "strategies"
    assert Path(source) == project / "build" / "strategies_placeholder"
    assert Path(source).is_dir()
    names = set(os.listdir(source))
    assert names == {"README.txt"}
    assert not (project / "strategies").exists()
    text = (Path(source) / "README.txt").read_text(encoding="utf-8")
    assert "not packaged" in text


def test_strategies_datas_does_not_copy_private_strategies(tmp_path):
    project = tmp_path / "dev"
    private = project / "strategies"
    private.mkdir(parents=True)
    secret = private / "my_private_strategy.py"
    secret.write_text("SECRET_STRATEGY = 'do-not-ship'\n", encoding="utf-8")
    (private / "index.json").write_text('[{"class_name": "Mine"}]\n',
                                        encoding="utf-8")

    datas = build_release.strategies_datas(str(project))
    source = Path(datas[0][0])
    assert source.resolve() != private.resolve()
    staged = list(source.rglob("*"))
    staged_text = "\n".join(
        p.read_text(encoding="utf-8") for p in staged if p.is_file()
    )
    assert "SECRET_STRATEGY" not in staged_text
    assert "my_private_strategy.py" not in {p.name for p in staged}
    assert "index.json" not in {p.name for p in staged}
    assert secret.read_text(encoding="utf-8").startswith("SECRET_STRATEGY")


def test_strategies_datas_refuses_to_wipe_private_dir(tmp_path):
    project = tmp_path / "dev"
    private = project / "strategies"
    private.mkdir(parents=True)
    kept = private / "kept.py"
    kept.write_text("keep me\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="private strategies"):
        build_release.strategies_datas(str(project), staging_dir=str(private))
    assert kept.read_text(encoding="utf-8") == "keep me\n"

    with pytest.raises(RuntimeError, match="private strategies"):
        build_release.strategies_datas(
            str(project), staging_dir=str(private / "nested"),
        )
    assert kept.is_file()


def test_strategies_datas_refuses_to_wipe_project_root(tmp_path):
    project = tmp_path / "dev"
    project.mkdir()
    (project / "run_backtest.py").write_text("# entry\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="placeholder"):
        build_release.strategies_datas(str(project), staging_dir=str(project))
    assert (project / "run_backtest.py").is_file()


def test_spec_uses_placeholder_and_hiddenimports_comtypes_gen():
    spec = (REPO / "tai_backtest.spec").read_text(encoding="utf-8")
    assert "strategies_datas(SPECPATH)" in spec
    assert "sys.path.insert(0, SPECPATH)" in spec
    assert "('strategies', 'strategies')" not in spec
    assert '("strategies", "strategies")' not in spec
    assert "'comtypes.gen'" in spec


# ── pinned build requirements ───────────────────────────────────────


def test_requirements_build_pins_known_good_toolchain():
    text = (REPO / "requirements-build.txt").read_text(encoding="utf-8")
    pins = build_release.parse_pinned_requirements(text)
    assert pins["PyInstaller"] == "6.19.0"
    assert pins["comtypes"] == "1.4.15"
    # The 1.4.15 wheel has no gen/; hooks-contrib floats with PyInstaller.
    assert "does not ship comtypes/gen" in text
    assert "pyinstaller-hooks-contrib" in text
    assert "not pinned" in text
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "--install-build-deps" in readme
    assert "--no-install" in readme
    assert "does **not** pip-install" in readme
    assert ".venv-build" in readme
    assert "does not ship a `comtypes/gen/`" in readme
    assert "pip install -e ." in readme
    for package in (
        "pyyaml", "keyring", "httpx", "lightweight-charts", "pandas", "holidays",
    ):
        assert package in readme
    assert "pip install -e ." in text


def test_parse_pinned_requirements_rejects_unpinned_and_duplicates():
    with pytest.raises(ValueError, match="name==version"):
        build_release.parse_pinned_requirements("PyInstaller>=6.19.0\n")
    with pytest.raises(ValueError, match="duplicate"):
        build_release.parse_pinned_requirements(
            "comtypes==1.4.15\ncomtypes==1.4.17\n"
        )
    assert build_release.parse_pinned_requirements(
        "# comment\n\nPyInstaller==6.19.0  # pin\n"
    ) == {"PyInstaller": "6.19.0"}


def test_unsatisfied_pins_reports_missing_and_wrong():
    pins = {"PyInstaller": "6.19.0", "comtypes": "1.4.15"}
    installed = {"PyInstaller": "6.22.3"}
    bad = build_release.unsatisfied_pins(pins, installed.get)
    assert bad == {"PyInstaller": "6.22.3", "comtypes": None}


def test_installed_version_known_package_and_missing():
    assert build_release._installed_version("pytest")
    assert build_release._installed_version(
        "this-package-is-not-installed-xyz"
    ) is None


def _write_req(tmp_path, body="PyInstaller==6.19.0\ncomtypes==1.4.15\n"):
    (tmp_path / "requirements-build.txt").write_text(body, encoding="utf-8")


def test_ensure_build_requirements_installs_even_when_pins_match(tmp_path):
    """Matching pins still get ``pip install -e .`` on the target interpreter."""
    _write_req(tmp_path)
    target = str(tmp_path / ".venv-build" / "bin" / "python")
    calls = []
    checked = []
    matched = {"PyInstaller": "6.19.0", "comtypes": "1.4.15"}

    def version_of(name):
        checked.append(name)
        return matched[name]

    def runner(cmd):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0)

    pins = build_release.ensure_build_requirements(
        str(tmp_path),
        runner=runner,
        version_of=version_of,
        python=target,
    )
    assert pins["comtypes"] == "1.4.15"
    assert len(calls) == 1
    assert calls[0][0] == target
    assert calls[0][1:4] == ["-m", "pip", "install"]
    root = os.path.abspath(str(tmp_path))
    assert calls[0][calls[0].index("-e") + 1] == root
    assert calls[0][-2] == "-r"
    assert calls[0][-1].endswith("requirements-build.txt")
    # One pass over the pins before pip, and one after.
    assert checked == ["PyInstaller", "comtypes", "PyInstaller", "comtypes"]


def test_query_dist_version_missing_interpreter_exits(capsys):
    missing = "/no/such/interpreter"

    def runner(cmd, **kwargs):
        raise OSError(2, "No such file or directory", cmd[0])

    with pytest.raises(SystemExit) as exc:
        build_release.query_dist_version(
            missing, "PyInstaller", runner=runner,
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "could not query" in out
    assert "PyInstaller" in out
    assert missing in out
    assert "Traceback" not in out


def test_ensure_build_requirements_installs_when_unpinned_combo(tmp_path):
    _write_req(tmp_path)
    state = {"PyInstaller": "6.22.3", "comtypes": "1.4.17"}
    calls = []

    def runner(cmd):
        calls.append(list(cmd))
        state["PyInstaller"] = "6.19.0"
        state["comtypes"] = "1.4.15"
        return types.SimpleNamespace(returncode=0)

    build_release.ensure_build_requirements(
        str(tmp_path), runner=runner, version_of=state.get,
    )
    assert calls and calls[0][-2] == "-r"
    assert calls[0][-1].endswith("requirements-build.txt")
    assert "pip" in calls[0]
    root = os.path.abspath(str(tmp_path))
    editable = calls[0].index("-e")
    assert calls[0][editable + 1] == root


def test_ensure_build_requirements_exits_when_pip_fails(tmp_path):
    _write_req(tmp_path)

    def runner(cmd):
        return types.SimpleNamespace(returncode=1)

    with pytest.raises(SystemExit):
        build_release.ensure_build_requirements(
            str(tmp_path),
            runner=runner,
            version_of=lambda _name: None,
        )


def test_ensure_build_requirements_exits_when_still_wrong(tmp_path):
    _write_req(tmp_path)

    def runner(cmd):
        return types.SimpleNamespace(returncode=0)

    with pytest.raises(SystemExit):
        build_release.ensure_build_requirements(
            str(tmp_path),
            runner=runner,
            version_of=lambda _name: "9.9.9",
        )


def _record_subprocess(monkeypatch):
    calls = []

    def fake_run(cmd, cwd=None):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_release.subprocess, "run", fake_run)
    monkeypatch.setattr(build_release, "strategies_datas", lambda root: [])
    return calls


def test_build_default_never_calls_pip(monkeypatch):
    calls = _record_subprocess(monkeypatch)

    def boom(*_a, **_k):
        raise AssertionError("default build must not install")

    monkeypatch.setattr(build_release, "ensure_build_requirements", boom)
    build_release.build()
    assert calls
    assert all("pip" not in part for cmd in calls for part in cmd)
    assert calls[0][1:3] == ["-m", "PyInstaller"]


def _probe_result(store, dist_name):
    ver = store.get(dist_name)
    if ver:
        return types.SimpleNamespace(returncode=0, stdout=ver, stderr="")
    return types.SimpleNamespace(returncode=3, stdout="", stderr="")


def _install_into(store):
    store["PyInstaller"] = "6.19.0"
    store["comtypes"] = "1.4.15"


def _fake_target_run(calls, *, target, target_store, host_store):
    """Answer version probes from the interpreter named in argv[0].

    pip into ``target`` fills ``target_store`` only. pip into any other
    interpreter fills ``host_store``. A check that reads the host, or a
    pip/PyInstaller argv that is not ``target``, cannot satisfy a test
    that expects the target to end up pinned.
    """

    def fake_run(cmd, cwd=None, **kwargs):
        argv = list(cmd)
        calls.append(argv)
        if len(argv) >= 2 and argv[1] == "-c":
            store = target_store if argv[0] == target else host_store
            return _probe_result(store, argv[-1])
        if "pip" in argv:
            if argv[0] == target:
                _install_into(target_store)
            else:
                _install_into(host_store)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    return fake_run


def _assert_pip_and_pyinstaller(calls, interpreter):
    indexed = list(enumerate(calls))
    probes = [
        (i, cmd) for i, cmd in indexed if len(cmd) >= 2 and cmd[1] == "-c"
    ]
    pip_cmds = [
        (i, cmd) for i, cmd in indexed if "pip" in cmd and "-m" in cmd
    ]
    pyinstaller = [
        cmd for _i, cmd in indexed
        if len(cmd) >= 3 and cmd[1:3] == ["-m", "PyInstaller"]
    ]
    assert len(pip_cmds) == 1, "install was skipped or repeated"
    pip_at, pip = pip_cmds[0]
    assert pip[0] == interpreter
    assert pip[1:4] == ["-m", "pip", "install"]
    assert "-e" in pip
    root = os.path.dirname(os.path.abspath(build_release.__file__))
    assert os.path.abspath(pip[pip.index("-e") + 1]) == root
    assert pip[-2] == "-r"
    assert pip[-1].endswith("requirements-build.txt")
    before = [cmd for i, cmd in probes if i < pip_at]
    after = [cmd for i, cmd in probes if i > pip_at]
    assert before, "no version check before pip"
    assert after, "no version check after pip"
    assert all(cmd[0] == interpreter for cmd in before)
    assert all(cmd[0] == interpreter for cmd in after)
    assert {cmd[-1] for cmd in before} >= {"PyInstaller", "comtypes"}
    assert {cmd[-1] for cmd in after} >= {"PyInstaller", "comtypes"}
    assert pyinstaller and pyinstaller[0][0] == interpreter
    return pip


def test_build_install_build_deps_calls_pip(monkeypatch):
    """No .venv-build: pip, both pin checks, and PyInstaller use this interpreter."""
    calls = []
    store = {}
    monkeypatch.setattr(
        build_release.subprocess, "run",
        _fake_target_run(
            calls, target=sys.executable, target_store=store, host_store={},
        ),
    )
    monkeypatch.setattr(build_release, "strategies_datas", lambda root: [])
    monkeypatch.setattr(build_release, "_dedicated_build_python", lambda root: None)
    # In-process metadata stays empty. A check that ignores the target's
    # probe and reads this process fails the post-install check.
    monkeypatch.setattr(build_release, "_installed_version", lambda _name: None)

    build_release.build(install_requirements=True)

    _assert_pip_and_pyinstaller(calls, sys.executable)


def test_install_passes_when_host_lacks_pins_and_target_gains_them(
    monkeypatch, tmp_path,
):
    """Running Python lacks the pins. The target gains them, so the build passes."""
    win = tmp_path / ".venv-build" / "Scripts" / "python.exe"
    win.parent.mkdir(parents=True)
    win.write_text("", encoding="utf-8")
    target = str(win)
    assert build_release._dedicated_build_python(str(tmp_path)) == target

    calls = []
    target_store = {}
    host_store = {}
    real_lookup = build_release._dedicated_build_python

    def lookup(_root):
        return real_lookup(str(tmp_path))

    monkeypatch.setattr(build_release, "_dedicated_build_python", lookup)
    monkeypatch.setattr(build_release, "_installed_version", lambda _name: None)
    monkeypatch.setattr(build_release, "strategies_datas", lambda root: [])
    monkeypatch.setattr(
        build_release.subprocess, "run",
        _fake_target_run(
            calls, target=target, target_store=target_store, host_store=host_store,
        ),
    )

    build_release.build(install_requirements=True)

    _assert_pip_and_pyinstaller(calls, target)
    assert target_store == {"PyInstaller": "6.19.0", "comtypes": "1.4.15"}
    assert host_store == {}


def test_install_still_runs_when_host_has_pins_and_target_does_not(
    monkeypatch, tmp_path,
):
    """Host pins must not skip the install. The target is what gets checked."""
    target = str(tmp_path / ".venv-build" / "bin" / "python")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8"):
        pass
    pins = {"PyInstaller": "6.19.0", "comtypes": "1.4.15"}
    calls = []
    target_store = {}
    host_store = dict(pins)
    monkeypatch.setattr(
        build_release, "_dedicated_build_python", lambda _root: target,
    )
    monkeypatch.setattr(build_release, "_installed_version", lambda name: pins.get(name))
    monkeypatch.setattr(build_release, "strategies_datas", lambda root: [])
    monkeypatch.setattr(
        build_release.subprocess, "run",
        _fake_target_run(
            calls, target=target, target_store=target_store, host_store=host_store,
        ),
    )

    build_release.build(install_requirements=True)

    _assert_pip_and_pyinstaller(calls, target)
    assert target_store == pins


def test_dedicated_build_python_finds_windows_scripts(tmp_path):
    """The Windows candidate is ``.venv-build/Scripts/python.exe`` and nothing else."""
    win = tmp_path / ".venv-build" / "Scripts" / "python.exe"
    win.parent.mkdir(parents=True)
    win.write_text("", encoding="utf-8")
    assert not (tmp_path / ".venv-build" / "bin" / "python").exists()
    assert build_release._dedicated_build_python(str(tmp_path)) == str(win)


def test_install_opt_in_prefers_venv_build_python(monkeypatch, tmp_path, capsys):
    py = tmp_path / ".venv-build" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    chosen = build_release._python_for_install(str(tmp_path))
    assert chosen == str(py)
    out = capsys.readouterr().out
    assert "dedicated venv" in out
    assert str(py) in out


def test_install_opt_in_warns_with_active_interpreter(tmp_path, capsys):
    chosen = build_release._python_for_install(str(tmp_path))
    assert chosen == sys.executable
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert sys.executable in out
    assert "comtypes" in out


def test_no_install_is_a_noop_and_does_not_cancel_opt_in():
    assert build_release.resolve_install_request(
        install_build_deps=False, no_install=False,
    ) is False
    assert build_release.resolve_install_request(
        install_build_deps=False, no_install=True,
    ) is False
    assert build_release.resolve_install_request(
        install_build_deps=True, no_install=False,
    ) is True
    assert build_release.resolve_install_request(
        install_build_deps=True, no_install=True,
    ) is True


def test_main_wires_install_flags():
    import inspect
    src = inspect.getsource(build_release.main)
    assert '"--install-build-deps"' in src
    assert '"--no-install"' in src
    assert "resolve_install_request(" in src


def test_main_passes_install_build_deps(monkeypatch):
    """``main`` must hand the flag to ``build``, not merely accept it."""
    seen = []

    def fake_build(*, install_requirements=False):
        seen.append(install_requirements)

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

    assert run(["build_release.py", "--skip-release", "--install-build-deps"]) is True
    assert run(["build_release.py", "--skip-release"]) is False
    assert run(["build_release.py", "--skip-release", "--no-install"]) is False
    assert run([
        "build_release.py", "--skip-release",
        "--install-build-deps", "--no-install",
    ]) is True


# ── runtime hook (Linux, comtypes mocked) ───────────────────────────


def test_ensure_comtypes_gen_creates_package_when_import_fails(tmp_path):
    """Swallowed case: the missing module is ``comtypes.gen`` itself."""
    snapshot = _comtypes_snapshot()
    cache = tmp_path / "_comtypes_cache"
    pkg = tmp_path / "comtypes_pkg"
    pkg.mkdir()
    comtypes_mod = types.ModuleType("comtypes")
    comtypes_mod.__path__ = [str(pkg)]
    code_cache = types.ModuleType("comtypes.client._code_cache")
    code_cache.gen_dir = None
    try:
        sys.modules["comtypes"] = comtypes_mod
        sys.modules.pop("comtypes.gen", None)
        sys.modules.pop("comtypes.client", None)
        sys.modules["comtypes.client._code_cache"] = code_cache

        ensure_comtypes_gen(str(cache))

        init_py = cache / "__init__.py"
        assert init_py.is_file()
        assert "comtypes.gen" in init_py.read_text(encoding="utf-8")
        gen = sys.modules["comtypes.gen"]
        assert str(cache) in list(gen.__path__)
        assert comtypes_mod.gen is gen
        assert code_cache.gen_dir == str(cache)

        (cache / "SKCOMLib.py").write_text(
            "MARKER = 'generated'\n", encoding="utf-8",
        )
        importlib.invalidate_caches()
        loaded = importlib.import_module("comtypes.gen.SKCOMLib")
        assert loaded.MARKER == "generated"
    finally:
        _restore_comtypes(snapshot)


def test_ensure_comtypes_gen_prepends_cache_when_already_importable(tmp_path):
    snapshot = _comtypes_snapshot()
    cache = tmp_path / "_comtypes_cache"
    bundle_gen = tmp_path / "bundled_gen"
    bundle_gen.mkdir()
    comtypes_mod = types.ModuleType("comtypes")
    comtypes_mod.__path__ = [str(tmp_path / "comtypes_pkg")]
    gen = types.ModuleType("comtypes.gen")
    gen.__path__ = [str(bundle_gen)]
    gen.__package__ = "comtypes.gen"
    comtypes_mod.gen = gen
    code_cache = types.ModuleType("comtypes.client._code_cache")
    code_cache.gen_dir = str(bundle_gen)
    client = types.ModuleType("comtypes.client")
    client.gen_dir = str(bundle_gen)
    try:
        sys.modules["comtypes"] = comtypes_mod
        sys.modules["comtypes.gen"] = gen
        sys.modules["comtypes.client"] = client
        sys.modules["comtypes.client._code_cache"] = code_cache

        ensure_comtypes_gen(str(cache))

        assert list(gen.__path__)[0] == str(cache)
        assert str(bundle_gen) in list(gen.__path__)
        assert (cache / "__init__.py").is_file()
        assert code_cache.gen_dir == str(cache)
        assert client.gen_dir == str(cache)
    finally:
        _restore_comtypes(snapshot)


def test_ensure_comtypes_gen_reraises_when_gen_itself_is_broken(tmp_path):
    """Re-raised case: comtypes.gen imports something else that is missing."""
    snapshot = _comtypes_snapshot()
    root = tmp_path / "py"
    gen_dir = root / "comtypes" / "gen"
    gen_dir.mkdir(parents=True)
    (root / "comtypes" / "__init__.py").write_text("", encoding="utf-8")
    (gen_dir / "__init__.py").write_text(
        "import definitely_not_a_real_module_xyz\n", encoding="utf-8",
    )
    sys.path.insert(0, str(root))
    try:
        for name in list(sys.modules):
            if name == "comtypes" or name.startswith("comtypes."):
                sys.modules.pop(name, None)
        with pytest.raises(ModuleNotFoundError) as exc:
            ensure_comtypes_gen(str(tmp_path / "cache"))
        assert exc.value.name == "definitely_not_a_real_module_xyz"
    finally:
        sys.path.remove(str(root))
        _restore_comtypes(snapshot)


class _BlockComtypes(importlib.abc.MetaPathFinder):
    def __init__(self, missing_name):
        self.missing_name = missing_name

    def find_spec(self, fullname, path, target=None):
        if fullname == "comtypes" or fullname.startswith("comtypes."):
            raise ModuleNotFoundError(
                f"No module named {self.missing_name!r}",
                name=self.missing_name,
            )
        return None


def test_install_comtypes_gen_swallows_only_missing_comtypes(tmp_path):
    """Swallowed case for the installer: ``e.name == 'comtypes'``."""
    snapshot = _comtypes_snapshot()
    blocker = _BlockComtypes("comtypes")
    sys.meta_path.insert(0, blocker)
    cache = tmp_path / "cache"
    init_py = cache / "__init__.py"
    try:
        for name in list(sys.modules):
            if name == "comtypes" or name.startswith("comtypes."):
                sys.modules.pop(name, None)
        assert _install_comtypes_gen(str(cache), str(init_py)) is None
    finally:
        sys.meta_path.remove(blocker)
        _restore_comtypes(snapshot)


def test_install_comtypes_gen_reraises_other_missing_imports(tmp_path):
    """Re-raised case: comtypes failed because a different module is missing."""
    snapshot = _comtypes_snapshot()
    blocker = _BlockComtypes("comtypes.client")
    sys.meta_path.insert(0, blocker)
    try:
        for name in list(sys.modules):
            if name == "comtypes" or name.startswith("comtypes."):
                sys.modules.pop(name, None)
        with pytest.raises(ModuleNotFoundError) as exc:
            _install_comtypes_gen(str(tmp_path / "cache"),
                                  str(tmp_path / "cache" / "__init__.py"))
        assert exc.value.name == "comtypes.client"
    finally:
        sys.meta_path.remove(blocker)
        _restore_comtypes(snapshot)


def test_hook_source_calls_guarded_helper():
    src = (REPO / "runtime_hook_comtypes.py").read_text(encoding="utf-8")
    assert "def ensure_comtypes_gen" in src
    assert "e.name != 'comtypes.gen'" in src
    assert "e.name != 'comtypes'" in src
    guard = "if not _is_multiprocessing_child() and not _is_grid_smoke():"
    call = "\n        ensure_comtypes_gen(cache_dir)\n"
    assert src.count(call) == 1
    assert src.index(call) > src.index(guard)


def test_grid_checklist_builds_in_venv_build_not_system_python():
    text = (
        REPO / "docs/testing/grid_subprocess_windows_checklist.md"
    ).read_text(encoding="utf-8")
    assert ".venv-build\\Scripts\\python.exe" in text
    assert "pip install -e ." in text
    assert "--install-build-deps" in text
    assert "cf58c7c3f9836ac67ef57544fe0f167bccc3c7fb" in text
    assert "Do not run `Activate.ps1`" in text
    assert ".\\.venv\\Scripts\\Activate.ps1" not in text
    assert "py -3.13 -m venv .venv-build" in text
    assert "py -3.13 -m venv .venv\n" not in text
    assert "python build_release.py" not in text
