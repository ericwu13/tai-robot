"""PyInstaller runtime hook: give comtypes.gen a writable cache directory.

In a frozen EXE, comtypes sets gen_dir = None (in-memory only).
This hook creates a writable cache next to the EXE so generated
COM wrapper modules persist across launches.
"""
import os
import sys

if getattr(sys, 'frozen', False):
    cache_dir = os.path.join(os.path.dirname(sys.executable), '_comtypes_cache')
    os.makedirs(cache_dir, exist_ok=True)

    import comtypes.gen
    comtypes.gen.__path__.insert(0, cache_dir)

    try:
        import comtypes.client._code_cache as _cc
        _cc.gen_dir = cache_dir
    except (ImportError, AttributeError):
        pass
