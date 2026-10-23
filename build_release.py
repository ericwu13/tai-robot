"""Build and package tai_backtest EXE for release.

Usage:
    python build_release.py          # build + zip; does not pip-install
    python build_release.py --skip-build  # zip only (reuse existing dist/)
    python build_release.py --allow-dirty # permit uncommitted source
    python build_release.py --allow-missing-broker-sdk
        # CI / Linux: build without Capital API broker DLLs (warns).
    python build_release.py --skip-release
        # build + zip only. Does not pip-install (the default).
    python build_release.py --skip-release --install-build-deps
        # also pip-install -e . and requirements-build.txt into the
        # target interpreter (prefers .venv-build), then run PyInstaller
        # with that same interpreter.

The default never runs pip. ``--no-install`` is accepted and changes
nothing. ``--install-build-deps`` is the opt-in. It installs this
project (``pip install -e .``: pyyaml, keyring, httpx,
lightweight-charts, pandas, holidays) and ``requirements-build.txt``
into ``.venv-build`` when that interpreter exists; otherwise it warns
and names the active interpreter before installing there. The install
always runs, including when the PyInstaller and comtypes pins already
match, so the project packages are present. Pin checks before and after
the install query that same interpreter. Installing there can
downgrade ``comtypes`` in a dev or live venv.

The Capital API SDK (``CapitalAPI_2.13.57/.../SKDLLPythonTester/libs``)
is gitignored. ``prepare_capital_sdk_binaries()`` refuses to build when
that directory is missing either required broker DLL
(``REQUIRED_BROKER_DLLS``: ``SKCOM.dll`` and ``CTSecuritiesATL.dll``)
or either file is empty, so a fresh clone cannot ship an EXE with an
empty ``_internal/libs``. Set ``TAI_CAPITAL_SDK_DIR`` to the libs
directory when the SDK lives elsewhere. A relative value is resolved
against the project root (the spec's ``SPECPATH`` on a raw PyInstaller
run). Opt out only with ``TAI_ALLOW_MISSING_CAPITAL_SDK=1`` or
``--allow-missing-broker-sdk``; any other value warns and does not
opt out. The spec calls the same helper. When the default libs
directory is present, the bundled binaries are the historical ``*.dll``
glob into ``libs`` — no extra datas.

Build-integrity guard (added after the v2.13.0 mismatch, where the
published EXE was built from a checkout lacking the regime code):

  1. Refuses to package when tracked SOURCE (src/, run_backtest.py,
     run_bot_cli.py, tai_backtest.spec, version.py) has uncommitted
     changes — a dirty tree can't be traced back to a commit. Override
     with --allow-dirty.
  2. Refuses to package when the bundled ``_internal/src`` does not match
     the working-tree ``src`` — this is exactly the drift that shipped in
     v2.13.0, and it catches a stale ``dist/`` reused via --skip-build.
  3. Stamps ``BUILD_INFO.json`` (version + commit SHA + dirty flag +
     build time) into the zip so every artifact is traceable to a commit.
"""

import argparse
import filecmp
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

import yaml

from version import APP_VERSION as VERSION
DIST_DIR = os.path.join("dist", "tai_backtest")
ZIP_NAME = f"tai_backtest_v{VERSION}_win_x64.zip"

# Tracked paths whose uncommitted state would make the build untraceable.
SOURCE_PATHS = [
    "src", "run_backtest.py", "run_bot_cli.py", "tai_backtest.spec", "version.py",
]

# Pinned toolchain consumed by ``build()`` before PyInstaller runs.
# PyInstaller 6.22.3 + comtypes 1.4.17 omit ``comtypes.gen`` and the EXE
# dies in the runtime hook. Do not float these pins.
BUILD_REQUIREMENTS = "requirements-build.txt"

# Broker DLLs. The spec used to skip a missing directory and still exit 0.
# Keep this relative string identical to the historical ``sdk_libs`` join
# so a present SDK at the default path still globs the same ``*.dll`` files.
# ``REQUIRED_BROKER_DLLS`` is the only list of files the build demands.
ENV_CAPITAL_SDK_DIR = "TAI_CAPITAL_SDK_DIR"
ENV_ALLOW_MISSING_CAPITAL_SDK = "TAI_ALLOW_MISSING_CAPITAL_SDK"
REQUIRED_BROKER_DLLS = ("SKCOM.dll", "CTSecuritiesATL.dll")
DEFAULT_CAPITAL_SDK_LIBS = os.path.join(
    "CapitalAPI_2.13.57",
    "CapitalAPI_2.13.57_PythonExample",
    "SKDLLPythonTester",
    "libs",
)

# Files and directories to EXCLUDE from the release zip
EXCLUDE_FILES = set()  # settings.yaml is now included with default values
EXCLUDE_DIRS = {"CapitalLog_Backtest", "_comtypes_cache", "__pycache__", "data", "live"}

# Marker shipped in the empty strategies placeholder. Never a private strategy.
_STRATEGIES_PLACEHOLDER_NAME = "README.txt"
_STRATEGIES_PLACEHOLDER_TEXT = (
    "Empty placeholder bundled with tai_backtest.\n"
    "\n"
    "Private AI strategies live in the gitignored strategies/ directory\n"
    "next to the source checkout, or next to tai_backtest.exe after install.\n"
    "Those files are not packaged into the EXE.\n"
)


def _git(*args) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def git_commit_info() -> tuple[str, bool]:
    """Return (commit_sha, source_is_dirty) for the current checkout.

    ``source_is_dirty`` is True only when a tracked SOURCE_PATH has
    uncommitted changes — unrelated dirt (e.g. CLAUDE.md) is ignored so
    the guard doesn't block on docs edits.
    """
    try:
        sha = _git("rev-parse", "HEAD")
        porcelain = _git("status", "--porcelain", "--", *SOURCE_PATHS)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("WARNING: git unavailable — cannot verify build provenance")
        return "unknown", False
    return sha, bool(porcelain)


def _iter_rel_files(root: str):
    """Yield paths relative to ``root``, skipping __pycache__ and .pyc."""
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in files:
            if fname.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, fname)
            yield os.path.relpath(full, root)


def verify_bundle_matches_source() -> None:
    """Refuse to package if the bundled ``src`` differs from the working
    tree — the guard that would have caught the v2.13.0 stale-EXE ship.
    """
    bundled = os.path.join(DIST_DIR, "_internal", "src")
    if not os.path.isdir(bundled):
        print(f"ERROR: bundled source not found at {bundled} — build first.")
        sys.exit(1)

    src_files = set(_iter_rel_files("src"))
    bnd_files = set(_iter_rel_files(bundled))
    missing = sorted(src_files - bnd_files)
    extra = sorted(bnd_files - src_files)
    differ = [rel for rel in sorted(src_files & bnd_files)
              if not filecmp.cmp(os.path.join("src", rel),
                                 os.path.join(bundled, rel), shallow=False)]

    if missing or extra or differ:
        print("ERROR: bundled src/ does not match the working tree.")
        print("       The dist/ is stale or built from a different commit "
              "(this is the v2.13.0 failure mode). Rebuild without "
              "--skip-build.")
        for rel in missing:
            print(f"  missing in bundle: {rel}")
        for rel in extra:
            print(f"  extra in bundle:   {rel}")
        for rel in differ:
            print(f"  differs:           {rel}")
        sys.exit(1)
    print(f"Bundle integrity OK — {len(src_files)} source files match HEAD")


def write_build_stamp(sha: str, dirty: bool) -> None:
    """Stamp BUILD_INFO.json into DIST_DIR so it lands in the zip."""
    stamp = {
        "version": VERSION,
        "commit": sha,
        "dirty": dirty,
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = os.path.join(DIST_DIR, "BUILD_INFO.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2)
    flag = " (DIRTY)" if dirty else ""
    print(f"  Stamped BUILD_INFO.json: v{VERSION} @ {sha[:10]}{flag}")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_within(child: str, parent: str) -> bool:
    """True when ``child`` is strictly inside ``parent``."""
    child_n = _norm(child)
    parent_n = _norm(parent)
    if child_n == parent_n:
        return False
    try:
        return os.path.commonpath([child_n, parent_n]) == parent_n
    except ValueError:
        return False


def _assert_safe_strategies_staging(project_root: str, staging: str) -> None:
    """Refuse to wipe or bundle the private ``strategies/`` tree."""
    private = os.path.join(project_root, "strategies")
    if (
        _norm(staging) == _norm(private)
        or _is_within(staging, private)
        or _is_within(private, staging)
    ):
        raise RuntimeError(
            "refusing to bundle the private strategies directory: "
            f"{private}"
        )


def strategies_datas(
    project_root: str, *, staging_dir: str | None = None,
) -> list[tuple[str, str]]:
    """PyInstaller ``datas`` entries for an empty ``strategies`` directory.

    ``strategies/`` is gitignored. PyInstaller aborts when a datas source
    is missing, which is every fresh clone. Pointing datas at the real
    directory would also ship whatever private AI strategies happen to be
    on the build machine. Stage a marker-only directory under ``build/``
    (gitignored) and return that instead.
    """
    project_root = os.path.abspath(project_root or ".")
    staging = os.path.abspath(
        staging_dir
        or os.path.join(project_root, "build", "strategies_placeholder")
    )
    # Project-root check first: the private strategies path lives inside
    # the project, so a staging dir of the project itself would otherwise
    # be reported as a private-strategies overlap.
    if _norm(staging) == _norm(project_root) or _is_within(project_root, staging):
        raise RuntimeError(
            f"refusing to stage the strategies placeholder at {staging}"
        )
    _assert_safe_strategies_staging(project_root, staging)
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging, exist_ok=True)
    with open(os.path.join(staging, _STRATEGIES_PLACEHOLDER_NAME),
              "w", encoding="utf-8") as fh:
        fh.write(_STRATEGIES_PLACEHOLDER_TEXT)
    leaked = [
        name for name in os.listdir(staging)
        if name.endswith(".py") or name == "index.json"
    ]
    if leaked:
        raise RuntimeError(f"refusing to bundle strategy files: {leaked}")
    return [(staging, "strategies")]


def parse_pinned_requirements(text: str) -> dict[str, str]:
    """Parse ``name==version`` pins. Comments and blank lines are ignored.

    Every requirement must be an exact pin. A floating specifier would
    let the EXE toolchain drift back to the crashing combination.
    """
    pins: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "==" not in line:
            raise ValueError(
                f"{BUILD_REQUIREMENTS}:{lineno}: pin must be name==version "
                f"({raw.strip()!r})"
            )
        name, version = (part.strip() for part in line.split("==", 1))
        if (
            not name or not version
            or any(ch in name for ch in " <>,;[]@")
            or any(ch in version for ch in " <>,;[]@")
        ):
            raise ValueError(
                f"{BUILD_REQUIREMENTS}:{lineno}: pin must be name==version "
                f"({raw.strip()!r})"
            )
        if name in pins:
            raise ValueError(
                f"{BUILD_REQUIREMENTS}:{lineno}: duplicate pin for {name}"
            )
        pins[name] = version
    if not pins:
        raise ValueError(f"{BUILD_REQUIREMENTS} has no pins")
    return pins


def _installed_version(dist_name: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version(dist_name)
    except PackageNotFoundError:
        return None


# Query one distribution inside a chosen interpreter. Exit 3 means the
# distribution is not installed there. Any other non-zero exit is a
# probe failure (the interpreter itself could not run).
_VERSION_PROBE = (
    "import importlib.metadata, sys\n"
    "try:\n"
    "    print(importlib.metadata.version(sys.argv[1]), end='')\n"
    "except importlib.metadata.PackageNotFoundError:\n"
    "    raise SystemExit(3)\n"
)


def query_dist_version(python: str, dist_name: str, *, runner=None):
    """Return the installed version of ``dist_name`` inside ``python``.

    ``None`` when that interpreter does not have the distribution.
    ``runner`` is called like ``subprocess.run(cmd, capture_output=True,
    text=True)``.
    """
    cmd = [python, "-c", _VERSION_PROBE, dist_name]
    run = runner or subprocess.run
    try:
        result = run(cmd, capture_output=True, text=True)
    except OSError as exc:
        print(f"ERROR: could not query {dist_name} in {python}: {exc}")
        sys.exit(1)
    code = getattr(result, "returncode", 1)
    if code == 0:
        text = (getattr(result, "stdout", "") or "").strip()
        return text or None
    if code == 3:
        return None
    print(f"ERROR: could not query {dist_name} in {python} (exit {code})")
    sys.exit(1)


def unsatisfied_pins(
    pins: dict[str, str], version_of,
) -> dict[str, str | None]:
    """Return ``{name: installed_or_None}`` for pins that are not met."""
    bad: dict[str, str | None] = {}
    for name, want in pins.items():
        got = version_of(name)
        if got != want:
            bad[name] = got
    return bad


def ensure_build_requirements(
    project_root: str | None = None, *, runner=None, version_of=None,
    python: str | None = None,
) -> dict[str, str]:
    """Install this project and ``requirements-build.txt`` into ``python``.

    ``python`` is the interpreter that receives the packages (default:
    this process). The pip install always runs, including when the
    pins already match: those pins do not cover the project packages.
    Pin checks before and after the install query that same interpreter.
    ``runner`` and ``version_of`` exist so tests can avoid pip and a real
    environment. ``runner`` is called like ``subprocess.run(cmd)`` for
    pip and like ``subprocess.run(cmd, capture_output=True, text=True)``
    for version probes.
    """
    root = os.path.abspath(
        project_root or os.path.dirname(os.path.abspath(__file__)) or "."
    )
    py = python or sys.executable
    req_path = os.path.join(root, BUILD_REQUIREMENTS)
    with open(req_path, encoding="utf-8") as fh:
        pins = parse_pinned_requirements(fh.read())
    run = runner or subprocess.run
    if version_of is None:
        def version_of(name, _py=py, _run=run):
            return query_dist_version(_py, name, runner=_run)
    bad = unsatisfied_pins(pins, version_of)
    if not bad:
        listed = ", ".join(f"{name}=={ver}" for name, ver in pins.items())
        print(f"Build dependencies OK ({listed})")
    else:
        print(f"Build dependencies do not match {BUILD_REQUIREMENTS}:")
        for name, want in pins.items():
            if name in bad:
                have = bad[name] or "not installed"
                print(f"  {name}: want {want}, have {have}")
    # Always install. Matching pins do not mean the project (pyyaml,
    # keyring, httpx, lightweight-charts, pandas, holidays) is present.
    # -e . installs the project. -r pins the toolchain.
    cmd = [py, "-m", "pip", "install", "-e", root, "-r", req_path]
    print(f"Installing the project and pinned build dependencies with {py}")
    print(f"  {' '.join(cmd)}")
    result = run(cmd)
    if getattr(result, "returncode", 1) != 0:
        print(f"ERROR: failed to install {BUILD_REQUIREMENTS}")
        sys.exit(1)
    bad = unsatisfied_pins(pins, version_of)
    if bad:
        print("ERROR: build dependencies still mismatch after install: "
              f"{bad}")
        sys.exit(1)
    print("Build dependencies installed")
    return pins


def warn_build_requirements(project_root: str) -> dict[str, str | None]:
    """Report pin drift without running pip.

    Used by the default build (and the ``--no-install`` no-op). Returns
    the unsatisfied pins (empty when the active environment already
    matches). Does not run pip.
    """
    root = os.path.abspath(project_root or ".")
    req_path = os.path.join(root, BUILD_REQUIREMENTS)
    with open(req_path, encoding="utf-8") as fh:
        pins = parse_pinned_requirements(fh.read())
    bad = unsatisfied_pins(pins, _installed_version)
    print(f"Not installing {BUILD_REQUIREMENTS} "
          f"(default; pass --install-build-deps to opt in).")
    if not bad:
        listed = ", ".join(f"{name}=={ver}" for name, ver in pins.items())
        print(f"Build dependencies OK ({listed})")
        return bad
    print("WARNING: active environment does not match the pins:")
    for name, got in bad.items():
        have = got or "not installed"
        print(f"  {name}: want {pins[name]}, have {have}")
    print("The runtime hook still creates comtypes.gen when that package")
    print("is missing. The pins are secondary.")
    return bad


def _dedicated_build_python(project_root: str) -> str | None:
    """Return ``.venv-build``'s interpreter when that venv exists."""
    root = os.path.abspath(project_root or ".")
    candidates = [
        os.path.join(root, ".venv-build", "Scripts", "python.exe"),
        os.path.join(root, ".venv-build", "bin", "python"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _python_for_install(project_root: str) -> str:
    """Interpreter that ``--install-build-deps`` should pip into.

    Prefers ``.venv-build``. Otherwise warns and returns this process's
    interpreter, by full path, so the operator can see the target.
    """
    dedicated = _dedicated_build_python(project_root)
    if dedicated:
        print(f"Installing build dependencies into dedicated venv: {dedicated}")
        return dedicated
    print("WARNING: --install-build-deps will pip-install into this interpreter:")
    print(f"  {sys.executable}")
    print("The install is pip install -e . plus requirements-build.txt.")
    print("That can downgrade comtypes in a dev or live venv.")
    print("Prefer a dedicated build venv and re-run with its python:")
    print("  python -m venv .venv-build")
    print("  .venv-build/bin/python build_release.py --skip-release --install-build-deps")
    print("  Windows: .venv-build\\Scripts\\python build_release.py --skip-release --install-build-deps")
    return sys.executable


def resolve_install_request(*, install_build_deps: bool, no_install: bool) -> bool:
    """Whether this invocation should pip-install.

    The default is no. ``--no-install`` is accepted and does not change
    that. ``--install-build-deps`` is the only opt-in; the no-op alias
    does not cancel it.
    """
    if install_build_deps and no_install:
        print("NOTE: --no-install is a no-op and does not cancel "
              "--install-build-deps.")
    return bool(install_build_deps)


def resolve_capital_sdk_libs(environ=None, *, project_root=None) -> str:
    """Libs directory that should contain the required broker DLLs.

    Order: ``TAI_CAPITAL_SDK_DIR``, then the historical default path.
    An explicit env var that does not exist is not replaced with the
    default. A relative env var is joined to ``project_root`` (the spec
    directory on a raw PyInstaller run, the ``build_release.py``
    directory otherwise). With no env var, the default stays the
    relative historical string when ``project_root`` is omitted or is
    the working directory, so a present SDK produces the same binaries
    list as before.
    """
    if environ is None:
        environ = os.environ
    override = (environ.get(ENV_CAPITAL_SDK_DIR) or "").strip()
    if override:
        if os.path.isabs(override) or not project_root:
            return override
        return os.path.join(project_root, override)
    if project_root:
        return os.path.join(project_root, DEFAULT_CAPITAL_SDK_LIBS)
    return DEFAULT_CAPITAL_SDK_LIBS


def _sdk_glob_dir(resolved: str, *, project_root, has_override: bool) -> str:
    """Directory ``collect_capital_sdk_binaries`` should glob.

    A relative ``TAI_CAPITAL_SDK_DIR`` stays on ``resolved`` (already
    joined to ``project_root``). The default path stays the historical
    relative string when that string points at ``resolved``, so the
    PyInstaller TOC matches the pre-check spec.
    """
    if has_override or not project_root:
        return resolved
    relative = DEFAULT_CAPITAL_SDK_LIBS
    under_root = os.path.join(project_root, relative)
    if os.path.abspath(relative) == os.path.abspath(under_root):
        return relative
    return resolved


def _capital_sdk_opt_out(environ, allow_missing_flag: bool) -> bool:
    """True only for the flag or ``TAI_ALLOW_MISSING_CAPITAL_SDK=1``.

    Any other non-empty value prints a warning and does not opt out.
    """
    if allow_missing_flag:
        return True
    raw = environ.get(ENV_ALLOW_MISSING_CAPITAL_SDK)
    if raw is None:
        return False
    value = raw.strip()
    if value == "" or value == "1":
        return value == "1"
    print(
        f"WARNING: {ENV_ALLOW_MISSING_CAPITAL_SDK}={value!r} does not opt out "
        "of the Capital SDK check. Only '1' opts out. The build stays "
        "fail-closed when a required broker DLL is missing or empty."
    )
    return False


def collect_capital_sdk_binaries(libs_dir: str) -> list[tuple[str, str]]:
    """PyInstaller binaries tuples. Same loop the spec used to inline."""
    dll_binaries = []
    if os.path.isdir(libs_dir):
        for dll in glob.glob(os.path.join(libs_dir, "*.dll")):
            dll_binaries.append((dll, "libs"))
    return dll_binaries


def required_broker_dll_problems(libs_dir: str) -> list[str]:
    """``'{name} is missing'`` or ``'{name} is empty'`` for each bad DLL.

    Both names in ``REQUIRED_BROKER_DLLS`` are reported. A missing
    directory reports every required file as missing. Empty means size 0.
    """
    problems = []
    directory_exists = os.path.isdir(libs_dir)
    for name in REQUIRED_BROKER_DLLS:
        path = os.path.join(libs_dir, name)
        if not directory_exists or not os.path.isfile(path):
            problems.append(f"{name} is missing")
        elif os.path.getsize(path) == 0:
            problems.append(f"{name} is empty")
    return problems


def _fail_missing_capital_sdk(libs_dir: str, problems: list[str]) -> None:
    detail = "; ".join(problems)
    if not os.path.isdir(libs_dir):
        detail = f"directory does not exist; {detail}"
    required = ", ".join(REQUIRED_BROKER_DLLS)
    print(
        "ERROR: Capital API broker DLLs not found — refusing to build an EXE "
        "with an empty _internal/libs.\n"
        f"       Expected path: {libs_dir}.\n"
        f"       {detail}.\n"
        f"       Required (each with nonzero size): {required}.\n"
        f"       Default path: {DEFAULT_CAPITAL_SDK_LIBS}\n"
        f"       Set {ENV_CAPITAL_SDK_DIR} to the libs directory that contains "
        "them. A relative value is resolved against the project root.\n"
        f"       CI or Linux test builds can set {ENV_ALLOW_MISSING_CAPITAL_SDK}=1 "
        "(only the value 1 opts out) or pass --allow-missing-broker-sdk."
    )
    raise SystemExit(1)


def _warn_capital_sdk_opt_out(libs_dir: str, *, present: bool) -> None:
    banner = "!" * 72
    required = ", ".join(REQUIRED_BROKER_DLLS)
    if present:
        detail = (
            f"Required broker DLLs ({required}) were found at {libs_dir} "
            "and will still be bundled."
        )
    else:
        detail = (
            f"Required broker DLLs ({required}) were NOT all present at "
            f"{libs_dir}. The EXE will have no complete _internal/libs "
            "broker set."
        )
    print(
        f"{banner}\n"
        "WARNING: Capital API SDK opt-out is active "
        f"({ENV_ALLOW_MISSING_CAPITAL_SDK}=1 or --allow-missing-broker-sdk).\n"
        f"         {detail}\n"
        f"         Default path: {DEFAULT_CAPITAL_SDK_LIBS}\n"
        f"         Override the libs directory with {ENV_CAPITAL_SDK_DIR}.\n"
        f"{banner}"
    )


def prepare_capital_sdk_binaries(
    environ=None, *, project_root=None, allow_missing_flag: bool = False,
) -> list[tuple[str, str]]:
    """Return ``(dll, 'libs')`` tuples, or exit 1 if a required DLL is bad.

    Both names in ``REQUIRED_BROKER_DLLS`` must exist and be nonzero.
    Opt-out (``TAI_ALLOW_MISSING_CAPITAL_SDK=1`` or ``allow_missing_flag``)
    prints a warning and falls through to the historical glob (empty
    when the directory is missing). Any other value of that env var
    warns and does not opt out. A present SDK is still bundled in full
    (every ``*.dll``, not only the required pair).
    """
    if environ is None:
        environ = os.environ
    override = (environ.get(ENV_CAPITAL_SDK_DIR) or "").strip()
    libs_dir = resolve_capital_sdk_libs(environ, project_root=project_root)
    glob_dir = _sdk_glob_dir(
        libs_dir, project_root=project_root, has_override=bool(override),
    )
    # Historical spec loop, unchanged. The gate below only decides
    # whether a missing or empty required DLL aborts the build.
    binaries = collect_capital_sdk_binaries(glob_dir)
    problems = required_broker_dll_problems(libs_dir)
    opted_out = _capital_sdk_opt_out(environ, allow_missing_flag)
    if problems:
        if not opted_out:
            _fail_missing_capital_sdk(libs_dir, problems)
        _warn_capital_sdk_opt_out(libs_dir, present=False)
        return binaries
    if opted_out:
        _warn_capital_sdk_opt_out(libs_dir, present=True)
    return binaries


def build(*, install_requirements: bool = False, allow_missing_broker_sdk: bool = False):
    """Build the EXE. PyInstaller runs on the same interpreter as any install.

    With ``install_requirements``, pin checks, ``pip install -e .``,
    ``pip install -r requirements-build.txt``, and PyInstaller all use
    the target interpreter (``.venv-build`` when it exists).

    The Capital SDK check runs before pip and before PyInstaller. A
    missing or empty required broker DLL (``REQUIRED_BROKER_DLLS``)
    aborts with exit code 1 unless ``allow_missing_broker_sdk`` or
    ``TAI_ALLOW_MISSING_CAPITAL_SDK=1`` is set. Any other value of that
    env var warns and does not opt out. The flag is exported into the
    environment so the spec, which repeats the check, sees the same
    opt-out.
    """
    root = os.path.dirname(os.path.abspath(__file__)) or "."
    if allow_missing_broker_sdk:
        os.environ[ENV_ALLOW_MISSING_CAPITAL_SDK] = "1"
    prepare_capital_sdk_binaries(
        project_root=root,
        allow_missing_flag=allow_missing_broker_sdk,
    )
    print(f"=== Building tai_backtest v{VERSION} ===")
    py = sys.executable
    if install_requirements:
        py = _python_for_install(root)
        ensure_build_requirements(root, python=py)
    else:
        warn_build_requirements(root)
    strategies_datas(root)
    result = subprocess.run(
        [py, "-m", "PyInstaller", "tai_backtest.spec", "--noconfirm"],
        cwd=root,
    )
    if result.returncode != 0:
        print("BUILD FAILED")
        sys.exit(1)
    print("Build OK")


def package():
    print(f"\n=== Packaging {ZIP_NAME} ===")

    if not os.path.isdir(DIST_DIR):
        print(f"ERROR: {DIST_DIR} not found. Run build first.")
        sys.exit(1)

    # Copy extra files into dist before zipping
    project = os.path.dirname(os.path.abspath(__file__)) or "."
    extras = {
        os.path.join(project, "settings.example.yaml"): os.path.join(DIST_DIR, "settings.yaml"),
        os.path.join(project, "必看安裝說明.txt"): os.path.join(DIST_DIR, "必看安裝說明.txt"),
    }
    for src, dst in extras.items():
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            print(f"  Copied {os.path.basename(src)} -> {os.path.basename(dst)}")

    zip_path = os.path.join("dist", ZIP_NAME)
    included = 0
    excluded = 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(DIST_DIR):
            # Remove excluded dirs in-place so os.walk skips them
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

            for fname in files:
                if fname in EXCLUDE_FILES:
                    excluded += 1
                    continue
                fpath = os.path.join(root, fname)
                arcname = os.path.relpath(fpath, "dist")
                zf.write(fpath, arcname)
                included += 1

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"Packaged {included} files ({excluded} excluded)")
    print(f"Output: {zip_path} ({size_mb:.1f} MB)")
    return zip_path


def write_checksum(zip_path: str) -> str:
    """Compute the SHA-256 of the release zip and write a sidecar file.

    Writes ``<zip>.sha256`` in the ``sha256sum``-compatible format
    (``<hex>  <filename>``) next to the zip. Returns the sidecar path.
    """
    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    sha_path = zip_path + ".sha256"
    with open(sha_path, "w", encoding="utf-8") as f:
        f.write(f"{digest}  {os.path.basename(zip_path)}\n")
    print(f"  SHA-256: {digest}")
    print(f"  Wrote {sha_path}")
    return sha_path


def _strip_title(body: str) -> str:
    """Remove a leading ``# v{VERSION}`` line — redundant with the header."""
    lines = body.splitlines()
    if lines and lines[0].strip().lstrip("#").strip() == f"v{VERSION}":
        return "\n".join(lines[1:]).strip()
    return body


def _load_release_notes() -> str:
    """Fetch release notes from the published GitHub release.

    GitHub is the single source of truth — the local
    ``release_notes_v{VERSION}.md`` file is only used when *creating* the
    release (``gh release create --notes-file``), never for notification.
    """
    try:
        result = subprocess.run(
            ["gh", "release", "view", f"v{VERSION}", "--json", "body", "-q", ".body"],
            capture_output=True, encoding="utf-8", timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return _strip_title(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return ""


def verify_head_pushed() -> None:
    """Abort if the local HEAD has not been pushed to the remote.

    Without this, ``gh release create`` tags whatever commit the remote
    has — which may be an older commit if the version-bump hasn't been
    pushed yet.  This was the root cause of the v2.17.5 double-notification
    (tag landed on the wrong commit, release had to be recreated).
    """
    try:
        local = _git("rev-parse", "HEAD")
        remote = _git("rev-parse", "@{upstream}")
    except subprocess.CalledProcessError:
        print("ERROR: cannot determine remote tracking branch.")
        print("       Push your commits first: git push")
        sys.exit(1)

    if local != remote:
        behind_ahead = _git("rev-list", "--left-right", "--count",
                            "@{upstream}...HEAD")
        print(f"ERROR: local HEAD ({local[:10]}) is not pushed to the remote "
              f"({remote[:10]}).")
        print(f"       behind/ahead: {behind_ahead}")
        print("       Push first so the release tag lands on the correct commit:")
        print("         git push")
        sys.exit(1)
    print(f"  HEAD {local[:10]} confirmed on remote")


def _release_notes_path() -> str:
    project = os.path.dirname(os.path.abspath(__file__)) or "."
    return os.path.join(project, f"release_notes_v{VERSION}.md")


def verify_release_notes() -> str:
    """Return the release notes path, or abort if missing/empty."""
    path = _release_notes_path()
    if not os.path.isfile(path):
        print(f"ERROR: release notes file not found: {os.path.basename(path)}")
        print(f"       Create it before running the release build.")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        content = f.read().strip()
    if len(content) < 20:
        print(f"ERROR: {os.path.basename(path)} is empty or too short "
              f"({len(content)} chars).")
        print("       Write proper bilingual release notes before releasing.")
        sys.exit(1)
    print(f"  Release notes OK ({os.path.basename(path)}, {len(content)} chars)")
    return path


def create_github_release(zip_path: str, sha_path: str, sha: str,
                          notes_file: str) -> bool:
    """Create a GitHub release via ``gh``, attaching the zip and checksum.

    The tag is pinned to *sha* via ``--target`` so it always points at the
    version-bump commit, regardless of what the remote HEAD happens to be.

    Returns True on success, False on failure.
    """
    print(f"\n=== Creating GitHub release v{VERSION} ===")

    tag = f"v{VERSION}"

    cmd = ["gh", "release", "create", tag, zip_path, sha_path,
           "--title", tag, "--target", sha,
           "--notes-file", notes_file]

    result = subprocess.run(cmd, capture_output=True, encoding="utf-8")
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "already exists" in stderr.lower():
            print(f"  Release {tag} already exists — skipping creation")
            print(f"  (delete it first with: gh release delete {tag} --yes)")
            return False
        print(f"  ERROR: gh release create failed (exit {result.returncode})")
        if stderr:
            print(f"  {stderr}")
        return False

    url = result.stdout.strip()
    print(f"  GitHub release created: {url}")
    return True


def verify_github_release_exists() -> bool:
    """Return True if the GitHub release for this version is published."""
    tag = f"v{VERSION}"
    result = subprocess.run(
        ["gh", "release", "view", tag, "--json", "isDraft"],
        capture_output=True, encoding="utf-8",
    )
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
        return not data.get("isDraft", True)
    except (json.JSONDecodeError, KeyError):
        return False


def notify_release(sha: str, *, force: bool = False) -> None:
    """Send a release announcement to the Discord poster channel.

    Guarded: writes a marker file so the notification fires at most once
    per version.  Pass ``force=True`` to resend (e.g. after a botched
    first attempt).
    """
    marker = os.path.join("dist", f".notified_v{VERSION}")
    if os.path.isfile(marker) and not force:
        print(f"  Discord notification already sent for v{VERSION} "
              f"(marker: {marker}).  Use --force-notify to resend.")
        return

    settings_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)) or ".", "settings.yaml")
    if not os.path.isfile(settings_path):
        print("  No settings.yaml — skipping Discord notification")
        return

    with open(settings_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    notif = cfg.get("notifications", {})
    bot_token = str(notif.get("discord_bot_token", "") or "").strip()
    poster_channel = str(notif.get("discord_poster_channel_id", "") or "").strip()
    if not bot_token or not poster_channel:
        print("  Discord poster channel not configured — skipping notification")
        return

    role_id = str(notif.get("discord_poster_role_id", "") or "").strip()
    role_mention = f"<@&{role_id}>\n" if role_id else ""
    commit_str = f" (`{sha[:10]}`)" if sha and sha != "unknown" else ""
    header = f"🚀 **tai-robot v{VERSION} released**{commit_str}"

    repo_url = _git("remote", "get-url", "origin").removesuffix(".git")
    release_link = f"🔗 {repo_url}/releases/tag/v{VERSION}"

    notes = _load_release_notes()
    if notes:
        if len(notes) > 1500:
            notes = notes[:1500].rsplit("\n", 1)[0] + "\n..."
        content = f"{role_mention}{header}\n\n{notes}\n\n{release_link}"
    else:
        content = f"{role_mention}{header}\n\n{release_link}"
        print(f"  No release notes found on GitHub release v{VERSION} — sending without notes")

    import httpx
    url = f"https://discord.com/api/v10/channels/{poster_channel}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(url, json={"content": content}, headers=headers, timeout=10)
    if resp.status_code in (200, 201):
        print(f"  Sent release announcement to Discord poster channel")
        os.makedirs("dist", exist_ok=True)
        with open(marker, "w") as f:
            f.write(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    else:
        print(f"  WARNING: Discord API returned {resp.status_code}: {resp.text}")


def main():
    parser = argparse.ArgumentParser(description="Build and package tai_backtest release")
    parser.add_argument("--skip-build", action="store_true", help="Skip PyInstaller build, just zip")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Permit packaging with uncommitted source changes")
    parser.add_argument("--allow-missing-broker-sdk", action="store_true",
                        help="Build even when Capital API broker DLLs are "
                             "missing (CI / Linux). Prints a warning. Same "
                             "as TAI_ALLOW_MISSING_CAPITAL_SDK=1. The spec "
                             "sees that env var and warns too.")
    parser.add_argument("--skip-release", action="store_true",
                        help="Skip GitHub release creation (build + package only)")
    parser.add_argument("--install-build-deps", action="store_true",
                        help="pip-install -e . and requirements-build.txt "
                             "into the target interpreter, then run "
                             "PyInstaller with it. Uses .venv-build when "
                             "that interpreter exists; otherwise installs "
                             "into the active interpreter and prints its "
                             "path. Can downgrade comtypes in a dev or "
                             "live venv.")
    parser.add_argument("--no-install", action="store_true",
                        help="Accepted no-op. The build does not pip-install "
                             "unless --install-build-deps is also set.")
    parser.add_argument("--notify-only", action="store_true",
                        help="Skip build/package, just send Discord notification")
    parser.add_argument("--force-notify", action="store_true",
                        help="Resend Discord notification even if already sent for this version")
    args = parser.parse_args()

    sha, dirty = git_commit_info()

    if args.notify_only:
        if not verify_github_release_exists():
            print(f"ERROR: GitHub release v{VERSION} not found or still in draft.")
            print("       Publish the release first, then retry --notify-only.")
            sys.exit(1)
        notify_release(sha, force=args.force_notify)
        return

    if dirty and not args.allow_dirty:
        print("ERROR: tracked source (src/, run_backtest.py, version.py) has "
              "uncommitted changes.")
        print("       Commit them so the release is traceable to a commit, "
              "or pass --allow-dirty to override.")
        sys.exit(1)

    if not args.skip_release:
        notes_file = verify_release_notes()

    if not args.skip_build:
        build(
            install_requirements=resolve_install_request(
                install_build_deps=args.install_build_deps,
                no_install=args.no_install,
            ),
            allow_missing_broker_sdk=args.allow_missing_broker_sdk,
        )

    # Guard: the packaged binary must match the committed source. This is
    # the check that would have prevented the v2.13.0 stale-EXE ship.
    verify_bundle_matches_source()
    write_build_stamp(sha, dirty)

    zip_path = package()
    sha_path = write_checksum(zip_path)

    if args.skip_release:
        print(f"\n=== Done (--skip-release) ===")
        print(f"To create the GitHub release later:")
        notes_hint = _release_notes_path()
        print(f"  gh release create v{VERSION} {zip_path} {sha_path} "
              f"--title \"v{VERSION}\" --target {sha} "
              f"--notes-file {notes_hint}")
        return

    verify_head_pushed()
    released = create_github_release(zip_path, sha_path, sha, notes_file)
    if released:
        notify_release(sha, force=args.force_notify)
    else:
        print("  Skipping Discord notification — GitHub release not confirmed")

    print(f"\n=== Done ===")


if __name__ == "__main__":
    main()
