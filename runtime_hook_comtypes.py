"""PyInstaller runtime hook: frozen-EXE fixups for comtypes and pythonnet.

1. comtypes.gen: writable cache directory next to EXE.
2. pythonnet: set PYTHONNET_PYDLL so Python.Runtime.dll finds the Python
   shared library in both the main process and multiprocessing children.
3. PATH: prepend _MEIPASS so the .NET CLR can resolve native DLLs.
"""
import os
import sys

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

    # Ensure _MEIPASS is on PATH so .NET CLR and cffi can find native DLLs
    _path = os.environ.get('PATH', '')
    if _bundle_dir not in _path:
        os.environ['PATH'] = _bundle_dir + os.pathsep + _path

    # ── comtypes ──
    cache_dir = os.path.join(os.path.dirname(sys.executable), '_comtypes_cache')
    os.makedirs(cache_dir, exist_ok=True)

    import comtypes.gen
    comtypes.gen.__path__.insert(0, cache_dir)

    try:
        import comtypes.client._code_cache as _cc
        _cc.gen_dir = cache_dir
    except (ImportError, AttributeError):
        pass
