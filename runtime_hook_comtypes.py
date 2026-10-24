"""PyInstaller runtime hook: frozen-EXE fixups for comtypes and pythonnet.

1. comtypes.gen: writable cache directory next to EXE. Skipped in a
   multiprocessing spawn child (`--multiprocessing-fork`) so the grid
   sweep does not import COM.
2. pythonnet: set PYTHONNET_PYDLL so Python.Runtime.dll finds the Python
   shared library in both the main process and multiprocessing children.
3. PATH: prepend _MEIPASS so the .NET CLR can resolve native DLLs.
"""
import os
import sys


def _is_multiprocessing_child(argv=None) -> bool:
    """True when this frozen process is a spawn child, not the bot."""
    args = sys.argv if argv is None else argv
    return len(args) >= 2 and args[1] == "--multiprocessing-fork"


def _is_grid_smoke(argv=None) -> bool:
    """True for the key-free ``--grid-subprocess-smoke`` parent.

    That process must not import COM. The spawn child is recognized by
    ``_is_multiprocessing_child`` and is also skipped.
    """
    args = sys.argv if argv is None else argv
    return "--grid-subprocess-smoke" in args


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
    # The grid child is a frozen re-exec of this EXE. It must not import
    # COM. The smoke parent does not either. PATH / PYTHONNET above do
    # not import comtypes.
    if not _is_multiprocessing_child() and not _is_grid_smoke():
        cache_dir = os.path.join(
            os.path.dirname(sys.executable), '_comtypes_cache')
        os.makedirs(cache_dir, exist_ok=True)

        import comtypes.gen
        comtypes.gen.__path__.insert(0, cache_dir)

        try:
            import comtypes.client._code_cache as _cc
            _cc.gen_dir = cache_dir
        except (ImportError, AttributeError):
            pass
