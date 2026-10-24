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
    assert "--no-install" in readme
    assert "dedicated build virtualenv" in readme
    assert "does not ship a `comtypes/gen/`" in readme


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


def test_ensure_build_requirements_skips_pip_when_pinned(tmp_path):
    _write_req(tmp_path)
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    pins = build_release.ensure_build_requirements(
        str(tmp_path),
        runner=runner,
        version_of={"PyInstaller": "6.19.0", "comtypes": "1.4.15"}.get,
    )
    assert pins["comtypes"] == "1.4.15"
    assert calls == []


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


def test_build_no_install_skips_pip(monkeypatch, capsys):
    order = []

    def fake_ensure(root):
        order.append("ensure")

    def fake_warn(root):
        order.append("warn")
        print("Not installing requirements-build.txt (--no-install).")
        return {}

    def fake_strategies(root):
        order.append("strategies")
        return []

    def fake_run(cmd, cwd=None):
        order.append("pyinstaller")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_release, "ensure_build_requirements", fake_ensure)
    monkeypatch.setattr(build_release, "warn_build_requirements", fake_warn)
    monkeypatch.setattr(build_release, "strategies_datas", fake_strategies)
    monkeypatch.setattr(build_release.subprocess, "run", fake_run)

    build_release.build(install_requirements=False)

    assert order == ["warn", "strategies", "pyinstaller"]
    assert "--no-install" in capsys.readouterr().out


def test_main_wires_no_install_flag():
    import inspect
    src = inspect.getsource(build_release.main)
    assert '"--no-install"' in src
    assert "install_requirements=not args.no_install" in src


def test_build_installs_pins_and_stages_strategies_before_pyinstaller(monkeypatch):
    order = []

    def fake_ensure(root):
        order.append(("ensure", root))

    def fake_strategies(root):
        order.append(("strategies", root))
        return []

    def fake_run(cmd, cwd=None):
        order.append(("pyinstaller", list(cmd), cwd))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_release, "ensure_build_requirements", fake_ensure)
    monkeypatch.setattr(build_release, "strategies_datas", fake_strategies)
    monkeypatch.setattr(build_release.subprocess, "run", fake_run)

    build_release.build()

    assert [step[0] for step in order] == ["ensure", "strategies", "pyinstaller"]
    cmd = order[2][1]
    assert cmd[:3] == [sys.executable, "-m", "PyInstaller"]
    assert "tai_backtest.spec" in cmd


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
    assert "ensure_comtypes_gen(cache_dir)" in src
