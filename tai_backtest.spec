# -*- mode: python ; coding: utf-8 -*-
import glob
import os
import importlib

# Collect lightweight_charts JS assets
_lwc_pkg = importlib.import_module('lightweight_charts')
_lwc_dir = os.path.dirname(_lwc_pkg.__file__)
_lwc_js = os.path.join(_lwc_dir, 'js')

# Collect SDK DLLs
sdk_libs = os.path.join(
    'CapitalAPI_2.13.57', 'CapitalAPI_2.13.57_PythonExample',
    'SKDLLPythonTester', 'libs',
)
dll_binaries = []
if os.path.isdir(sdk_libs):
    for dll in glob.glob(os.path.join(sdk_libs, '*.dll')):
        dll_binaries.append((dll, 'libs'))

a = Analysis(
    ['run_backtest.py'],
    pathex=[],
    binaries=dll_binaries,
    datas=[
        ('src', 'src'),
        ('strategies', 'strategies'),
        ('settings.example.yaml', '.'),
        (_lwc_js, 'lightweight_charts/js'),
    ],
    hiddenimports=[
        # GUI / core
        'lightweight_charts', 'pandas', 'yaml', 'tkinter',
        'httpx', 'httpcore', 'h11', 'certifi', 'anyio', 'sniffio',
        # TAIFEX settlement-day detection (3rd Wed early close)
        'holidays',
        # COM (Capital API) — include submodules PyInstaller's analysis misses
        'comtypes', 'comtypes.client', 'comtypes.stream',
        'comtypes.client._code_cache', 'comtypes.client._generate',
        # TradingView data feed (optional)
        'tvDatafeed', 'websocket', 'websocket._abnf', 'websocket._core',
        'websocket._exceptions', 'websocket._http', 'websocket._logging',
        'websocket._socket', 'websocket._ssl_compat', 'websocket._url',
        'websocket._utils',
        # AI modules
        'src.ai', 'src.ai.chat_client', 'src.ai.prompts',
        'src.ai.code_sandbox', 'src.ai.strategy_store', 'src.ai.pine_exporter',
        # Indicators
        'src.strategy.indicators', 'src.strategy.indicators.ma',
        'src.strategy.indicators.rsi', 'src.strategy.indicators.macd',
        'src.strategy.indicators.bollinger', 'src.strategy.indicators.atr',
        # Strategy examples
        'src.strategy.examples',
        'src.strategy.examples.h4_bollinger_long',
        'src.strategy.examples.h4_bollinger_atr_long',
        'src.strategy.examples.daily_bollinger_long',
        'src.strategy.examples.h4_midline_touch_long',
        'src.strategy.examples.m1_bollinger_atr_long',
        'src.strategy.examples.m1_sma_cross',
        'src.strategy.examples.ma_crossover',
        'src.strategy.examples.rsi_reversal',
        'src.strategy.examples.bollinger_breakout',
        # Market data
        'src.market_data.data_store', 'src.market_data.bar_builder',
        'src.market_data.models',
        # Backtest
        'src.backtest', 'src.backtest.engine', 'src.backtest.broker',
        'src.backtest.chart', 'src.backtest.data_loader',
        'src.backtest.report', 'src.backtest.metrics', 'src.backtest.strategy',
        # Live
        'src.live', 'src.live.bar_aggregator', 'src.live.csv_logger',
        'src.live.live_runner',
        # Utils
        'src.utils.time_utils',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['runtime_hook_comtypes.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='tai_backtest',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='tai_backtest',
)
