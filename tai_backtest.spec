# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['run_backtest.py'],
    pathex=[],
    binaries=[],
    datas=[('data', 'data'), ('src', 'src'), ('strategies', 'strategies')],
    hiddenimports=[
        'lightweight_charts', 'pandas', 'yaml', 'tkinter',
        'httpx', 'httpcore', 'h11', 'certifi', 'anyio', 'sniffio',
        'src.ai', 'src.ai.chat_client', 'src.ai.prompts',
        'src.ai.code_sandbox', 'src.ai.strategy_store', 'src.ai.pine_exporter',
        'src.strategy.indicators', 'src.strategy.indicators.ma',
        'src.strategy.indicators.rsi', 'src.strategy.indicators.macd',
        'src.strategy.indicators.bollinger', 'src.strategy.indicators.atr',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='tai_backtest',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
