"""PyInstaller runtime hook: frozen-EXE fixups for comtypes and pythonnet.

1. comtypes.gen: writable cache directory next to EXE. The package is
   created there when the frozen bundle did not collect it. Installed
   for the GUI and for CLI subcommands (deploy needs COM). Skipped for
   a multiprocessing spawn child and for the key-free grid-smoke parent
   via src.paths.skips_gui_and_com — the same helper run_backtest uses.
2. pythonnet: set PYTHONNET_PYDLL so Python.Runtime.dll finds the Python
   shared library in both the main process and multiprocessing children.
3. PATH: prepend _MEIPASS so the .NET CLR can resolve native DLLs.
"""
import os
import sys


def should_install_comtypes(argv=None) -> bool:
    """False only for a spawn child or the grid-smoke parent.

    CLI deploy/start and the GUI still return True so the gen cache is
    installed. The argv test lives in src.paths (one helper).
    """
    from src.paths import skips_gui_and_com
    return not skips_gui_and_com(argv)


def _install_comtypes_gen(cache_dir, init_py):
    """Register ``comtypes.gen`` from ``cache_dir`` when it was not bundled.

    Returns the module, or None when ``comtypes`` itself is missing
    (``ModuleNotFoundError.name == 'comtypes'``). Any other missing
    import is re-raised so a broken ``comtypes`` package still crashes.
    """
    import importlib.util
    try:
        import comtypes
    except ModuleNotFoundError as e:
        if e.name != 'comtypes':
            raise
        return None

    spec = importlib.util.spec_from_file_location(
        'comtypes.gen',
        init_py,
        submodule_search_locations=[cache_dir],
    )
    if spec is None or spec.loader is None:
        import types
        module = types.ModuleType('comtypes.gen')
        module.__path__ = [cache_dir]
        module.__file__ = init_py
        module.__package__ = 'comtypes.gen'
    else:
        module = importlib.util.module_from_spec(spec)
        # Register before exec so a relative import inside __init__ resolves.
        sys.modules['comtypes.gen'] = module
        comtypes.gen = module
        spec.loader.exec_module(module)
        return module

    sys.modules['comtypes.gen'] = module
    comtypes.gen = module
    return module


def ensure_comtypes_gen(cache_dir):
    """Make ``comtypes.gen`` importable and point it at ``cache_dir``.

    The comtypes wheels (including 1.4.15) do not ship ``comtypes/gen``.
    An unguarded ``import comtypes.gen`` in this hook then raises
    ModuleNotFoundError and the EXE exits before the UI starts. Create
    the package in the writable cache only when that module itself is
    missing (``e.name == 'comtypes.gen'``). A gen package that fails
    because one of its own imports is missing is re-raised. When the
    import already works, still prefer ``cache_dir`` so generated
    wrappers are written somewhere the bundle can update.
    """
    import importlib

    os.makedirs(cache_dir, exist_ok=True)
    init_py = os.path.join(cache_dir, '__init__.py')
    if not os.path.exists(init_py):
        with open(init_py, 'w', encoding='utf-8') as fh:
            fh.write('# comtypes.gen package, directory for generated files.\n')

    try:
        import comtypes.gen as gen
    except ModuleNotFoundError as e:
        # Only an absent comtypes.gen is recoverable. A broken package
        # whose own import failed (e.name is that other module) must crash.
        if e.name != 'comtypes.gen':
            raise
        gen = _install_comtypes_gen(cache_dir, init_py)

    if gen is None:
        return

    paths = list(getattr(gen, '__path__', []) or [])
    if cache_dir not in paths:
        paths.insert(0, cache_dir)
    gen.__path__ = paths
    sys.modules['comtypes.gen'] = gen
    comtypes_mod = sys.modules.get('comtypes')
    if comtypes_mod is not None:
        comtypes_mod.gen = gen

    importlib.invalidate_caches()

    code_cache = sys.modules.get('comtypes.client._code_cache')
    if code_cache is None:
        try:
            import comtypes.client._code_cache as code_cache
        except (ImportError, ModuleNotFoundError):
            code_cache = None
    if code_cache is not None:
        try:
            code_cache.gen_dir = cache_dir
        except AttributeError:
            pass

    client = sys.modules.get('comtypes.client')
    if client is not None:
        try:
            client.gen_dir = cache_dir
        except AttributeError:
            pass


if getattr(sys, 'frozen', False):
    _bundle_dir = sys._MEIPASS

    # ── pythonnet / .NET CLR ──
    # Python.Runtime.dll (a .NET assembly loaded by clr_loader) needs to
    # P/Invoke into python3xx.dll.  In a frozen EXE the DLL search path
    # of multiprocessing child processes may not include _MEIPASS, causing
    # "Failed to resolve Python.Runtime.Loader.Initialize".
    _pydll = os.path.join(_bundle_dir, 'python313.dll')
    if os.path.isfile(_pydll):
        os.environ.setdefault('PYTHONNET_PYDLL', _pydll)

    # Strip Mark-of-the-Web from Python.Runtime.dll.  When users download
    # the release ZIP from the internet, Windows tags extracted files with a
    # Zone.Identifier NTFS alternate data stream.  .NET Framework refuses to
    # load assemblies from the "internet zone", causing pyclr_get_function to
    # return NULL ("Failed to resolve Python.Runtime.Loader.Initialize").
    _prt_dll = os.path.join(_bundle_dir, 'pythonnet', 'runtime',
                            'Python.Runtime.dll')
    _zone = _prt_dll + ':Zone.Identifier'
    try:
        if os.path.exists(_zone):
            os.remove(_zone)
    except OSError:
        pass

    # Ensure _MEIPASS is on PATH so .NET CLR and cffi can find native DLLs
    _path = os.environ.get('PATH', '')
    if _bundle_dir not in _path:
        os.environ['PATH'] = _bundle_dir + os.pathsep + _path

    # ── comtypes ──
    # PATH / PYTHONNET above do not import comtypes. The GUI and CLI
    # (deploy needs COM) install the gen cache. Spawn children and the
    # grid-smoke parent skip it.
    if should_install_comtypes():
        cache_dir = os.path.join(
            os.path.dirname(sys.executable), '_comtypes_cache')
        ensure_comtypes_gen(cache_dir)
