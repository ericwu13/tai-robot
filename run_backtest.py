"""AI Strategy Workbench: Chat with Claude to generate, backtest, and export strategies.

Two backtest buttons:
  - API Backtest — fetches from Capital API (logs in on first use)
  - TV Backtest  — local CSV first, then TradingView download as fallback

Multi-symbol support via _SYMBOL_CONFIG (TX00, MTX00).

Usage:
  python run_backtest.py
"""

import os
import sys
import inspect
import queue
import threading
import traceback
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, simpledialog, messagebox
from datetime import datetime, timedelta
from pathlib import Path

# Ensure src is importable — handle PyInstaller frozen EXE
if getattr(sys, 'frozen', False):
    # --onedir: EXE is in dist/tai_backtest/, _MEIPASS == EXE dir
    # --onefile: EXE extracts to temp _MEI dir
    bundle_root = sys._MEIPASS
    project_root = os.path.dirname(sys.executable)
else:
    project_root = os.path.dirname(os.path.abspath(__file__))
    bundle_root = project_root

if project_root not in sys.path:
    sys.path.insert(0, project_root)
if bundle_root not in sys.path:
    sys.path.insert(0, bundle_root)

try:
    import yaml
except ImportError:
    yaml = None

from src.market_data.models import Bar, Tick
from src.market_data.bar_builder import BarBuilder
from src.utils.time_utils import combine_sk_datetime
from src.backtest.engine import BacktestEngine
from src.backtest.data_loader import parse_kline_strings, load_bars_from_csv
from src.backtest.report import format_report, export_trades_csv
from src.backtest.metrics import calculate_metrics
from src.backtest.strategy import BacktestStrategy
from src.backtest.chart import plot_backtest, LiveChart, _LWC_AVAILABLE

# In frozen EXE, patch lightweight_charts INDEX to file:// URL.
# This MUST run at module level (not inside __main__) so the multiprocessing
# child process (where the webview actually runs) also gets the patch.
# pywebview's HTTP server can silently fail in frozen EXEs; file:// bypasses it.
if getattr(sys, 'frozen', False) and _LWC_AVAILABLE:
    import lightweight_charts.abstract as _lwc_abs
    _js_dir = os.path.join(bundle_root, 'lightweight_charts', 'js')
    _lwc_abs.INDEX = 'file:///' + os.path.join(
        _js_dir, 'index.html').replace('\\', '/')

from src.strategy.examples.h4_bollinger_long import H4BollingerLongStrategy
from src.strategy.examples.h4_bollinger_atr_long import H4BollingerAtrLongStrategy
from src.strategy.examples.daily_bollinger_long import DailyBollingerLongStrategy
from src.strategy.examples.h4_midline_touch_long import H4MidlineTouchLongStrategy
from src.strategy.examples.m1_bollinger_atr_long import M1BollingerAtrLongStrategy
from src.strategy.examples.m1_sma_cross import M1SmaCrossStrategy

# AI modules
from src.ai.chat_client import ChatClient, PROVIDER_ANTHROPIC, PROVIDER_GOOGLE, DEFAULT_MODELS
from src.ai.prompts import STRATEGY_SYSTEM_PROMPT, STRATEGY_CODE_CONTEXT
from src.ai.code_sandbox import (
    extract_python_code, load_strategy_from_source,
    CodeValidationError, CodeExecutionError,
)
from src.ai.strategy_store import StrategyStore
from src.ai.pine_exporter import export_to_pine

# Live trading modules
from src.live.live_runner import LiveRunner, LiveState, is_market_open, _taipei_now

# TradingView data feed (optional, for longer history)
try:
    from tvDatafeed import TvDatafeed, Interval as TvInterval
    _tv_available = True
except ImportError:
    _tv_available = False

# Map strategy kline params to tvDatafeed intervals
_TV_INTERVALS = {
    (0, 1): "in_1_minute", (0, 5): "in_5_minute", (0, 15): "in_15_minute",
    (0, 30): "in_30_minute", (0, 60): "in_1_hour", (0, 120): "in_2_hour",
    (0, 180): "in_3_hour", (0, 240): "in_4_hour",
    (4, 1): "in_daily", (5, 1): "in_weekly", (6, 1): "in_monthly",
}

# Registry of available backtest strategies
STRATEGIES: dict[str, type[BacktestStrategy]] = {
    "1分K均線交叉 1m SMA Cross": M1SmaCrossStrategy,
    "H4 布林多單 H4 Bollinger Long": H4BollingerLongStrategy,
    "H4 布林ATR多單 H4 Bollinger ATR Long": H4BollingerAtrLongStrategy,
    "日線布林多單 Daily Bollinger Long": DailyBollingerLongStrategy,
    "H4 中線戰法多單 H4 Midline Touch Long": H4MidlineTouchLongStrategy,
    "1分K布林ATR多單 1m Bollinger ATR Long": M1BollingerAtrLongStrategy,
}

# ── COM setup (only if not using CSV mode) ──
_com_available = False
skC = skQ = skR = None

def _init_com():
    """Initialise Capital API COM objects (SKCOM).

    DLL search path handling is critical for PyInstaller frozen EXEs:
      - PyInstaller's bootloader calls SetDllDirectoryW(_MEIPASS), which
        overrides the Windows DLL search order.
      - COM's CoCreateInstance uses LoadLibrary (NOT LoadLibraryEx), so it
        respects SetDllDirectoryW but ignores os.add_dll_directory().
      - We must call SetDllDirectoryW(libs_path) so COM can find SKCOM.dll's
        sibling DLLs, then restore with SetDllDirectoryW(None) so SKCOM's
        runtime network calls can find system DLLs (WinHTTP, Schannel, etc.).
    """
    global _com_available, skC, skQ, skR
    try:
        import ctypes
        import comtypes
        import comtypes.client

        frozen = getattr(sys, 'frozen', False)

        # Locate DLL directory: bundled libs first (deployed EXE), SDK fallback (dev)
        bundled_libs = os.path.join(bundle_root, "libs")
        sdk_libs = os.path.abspath(os.path.join(
            project_root,
            "CapitalAPI_2.13.57", "CapitalAPI_2.13.57_PythonExample",
            "SKDLLPythonTester", "libs",
        ))

        if frozen and os.path.isdir(bundled_libs):
            libs_path = bundled_libs
        elif os.path.isdir(sdk_libs):
            libs_path = sdk_libs
        elif os.path.isdir(bundled_libs):
            libs_path = bundled_libs
        else:
            print("COM not available: no SDK or bundled libs found")
            return

        # ── Phase 1: Configure DLL search paths ──
        os.environ["PATH"] = libs_path + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(libs_path)
        except (OSError, AttributeError):
            pass
        # SetDllDirectoryW — this is what COM's LoadLibrary actually uses.
        # In frozen EXEs, PyInstaller set it to _MEIPASS; override to libs.
        kernel32 = ctypes.windll.kernel32
        kernel32.SetDllDirectoryW(libs_path)

        # ── Phase 2: Load typelib and create COM objects ──
        dll_path = os.path.join(libs_path, "SKCOM.dll")
        comtypes.client.GetModule(dll_path)
        import comtypes.gen.SKCOMLib as sk

        try:
            skC = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
        except OSError:
            # COM not registered — register from libs_path (requires admin once)
            import subprocess
            print(f"COM not registered. Attempting regsvr32 from: {libs_path}")
            reg_ok = True
            for dll_name in ("SKCOM.dll", "CTSecuritiesATL.dll"):
                dll_to_reg = os.path.join(libs_path, dll_name)
                if os.path.isfile(dll_to_reg):
                    r = subprocess.run(
                        ["regsvr32", "/s", dll_to_reg],
                        capture_output=True, text=True,
                    )
                    if r.returncode != 0:
                        print(f"  regsvr32 FAILED for {dll_name} (code {r.returncode})")
                        reg_ok = False
                    else:
                        print(f"  regsvr32 OK: {dll_name}")
                else:
                    print(f"  WARNING: {dll_name} not found in {libs_path}")
                    reg_ok = False
            if not reg_ok:
                print("COM registration failed — right-click the EXE > Run as administrator")
            try:
                skC = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
            except OSError:
                print("COM CreateObject failed after registration attempt.")
                print("Fix: right-click tai_backtest.exe > Run as administrator (once)")
                raise

        skQ = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
        skR = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)

        # ── Phase 3: Restore default DLL search order ──
        # None = default Windows search (app dir → System32 → Windows → PATH).
        # This lets SKCOM's internal network calls find WinHTTP, Schannel, etc.
        kernel32.SetDllDirectoryW(None)

        _com_available = True
    except Exception as e:
        print(f"COM not available: {e}")
        _com_available = False


def _load_settings():
    cfg = {"user_id": "", "password": "", "authority_flag": 0}
    # Check user-writable project_root first, then parent dir (for dist/), then bundle
    search_paths = [
        os.path.join(project_root, "settings.yaml"),
        os.path.join(project_root, "settings.example.yaml"),
    ]
    if getattr(sys, 'frozen', False):
        parent = os.path.dirname(project_root)
        search_paths.insert(1, os.path.join(parent, "settings.yaml"))
    search_paths.append(os.path.join(bundle_root, "settings.example.yaml"))
    for path in search_paths:
        if os.path.exists(path) and yaml:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            creds = data.get("credentials", {})
            cfg["user_id"] = creds.get("user_id", "")
            cfg["password"] = creds.get("password", "")
            cfg["authority_flag"] = creds.get("authority_flag", 0)
            # AI settings
            ai = data.get("ai", {})
            cfg["ai_provider"] = ai.get("provider", PROVIDER_ANTHROPIC)
            cfg["anthropic_api_key"] = ai.get("anthropic_api_key", "")
            cfg["google_api_key"] = ai.get("google_api_key", "")
            cfg["ai_model"] = ai.get("model", "")
            cfg["ai_max_tokens"] = ai.get("max_tokens", 16384)
            break
    return cfg


def _save_ai_settings(provider: str = "", anthropic_key: str = "",
                      google_key: str = "", model: str = "", max_tokens: int = 0):
    """Persist AI settings to settings.yaml."""
    if not yaml:
        return
    path = os.path.join(project_root, "settings.yaml")
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    ai = data.setdefault("ai", {})
    if provider:
        ai["provider"] = provider
    if anthropic_key:
        ai["anthropic_api_key"] = anthropic_key
    if google_key:
        ai["google_api_key"] = google_key
    if model:
        ai["model"] = model
    if max_tokens:
        ai["max_tokens"] = max_tokens
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)


INTERVAL_SECONDS = {
    (0, 240): 14400,
    (0, 60): 3600,
    (0, 30): 1800,
    (0, 15): 900,
    (0, 5): 300,
    (0, 1): 60,
    (4, 1): 86400,
}

_CACHE_DIR = os.path.join(project_root, "data")

# Multi-symbol configuration: COM symbol -> (csv_prefix, tv_symbol, point_value)
_SYMBOL_CONFIG = {
    "TX00": {"prefix": "TXF1", "tv": "TXF1!", "pv": 200, "tick_divisor": 100},
    "MTX00": {"prefix": "TMF1", "tv": "TMF1!", "pv": 50, "tick_divisor": 100},
}

_CACHE_SUFFIXES = {
    (0, 15): "_15m.csv",
    (0, 60): "_1H.csv",
    (0, 240): "_H4.csv",
    (4, 1): "_1D.csv",
}


_LIVE_CHART_TIMEFRAMES = {
    "Native": None,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1H": 3600,
    "4H": 14400,
}


def _get_cache_file(symbol: str, kline_key: tuple) -> str | None:
    """Return the cache CSV filename for a given symbol and kline key, or None."""
    cfg = _SYMBOL_CONFIG.get(symbol)
    if not cfg:
        return None
    suffix = _CACHE_SUFFIXES.get(kline_key)
    if not suffix:
        return None
    return cfg["prefix"] + suffix

_app = None

# Thread-safe queue for COM tick callbacks → main thread.
# COM callbacks fire on background threads; touching Tkinter or most Python objects
# from those threads crashes the GIL in Python 3.13. We put raw tick tuples into
# this queue and drain them on the main thread via root.after().
_tick_queue: queue.Queue = queue.Queue()


def should_reuse_bars(
    raw_bars: list, raw_bars_key: tuple,
    symbol: str, kline_type: int, kline_minute: int,
) -> bool:
    """Return True if raw_bars can be reused for the given symbol and timeframe."""
    if not raw_bars:
        return False
    return raw_bars_key == (symbol, kline_type, kline_minute)


def filter_bars_by_date(
    bars: list, start_date: str, end_date: str,
) -> list:
    """Filter bars to [start_date, end_date] inclusive. Dates are YYYYMMDD strings."""
    dt_start = datetime.strptime(start_date, "%Y%m%d")
    dt_end = datetime.strptime(end_date, "%Y%m%d") + timedelta(days=1)
    return [b for b in bars if dt_start <= b.dt < dt_end]


def _log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:12]
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if _app and hasattr(_app, "log_text"):
        try:
            # Only touch Tkinter widgets from the main thread; COM callbacks
            # run on background threads and touching Tk there crashes the GIL.
            if threading.current_thread() is threading.main_thread():
                _app.log_text.insert(tk.END, line + "\n")
                _app.log_text.see(tk.END)
        except Exception:
            pass


# ── COM Event handlers ──

class SKQuoteLibEvents:
    def OnConnection(self, nKind, nCode):
        kind_names = {3001: "Reply", 3002: "Quote", 3003: "Ready",
                      3021: "ConnError", 3033: "Abnormal"}
        _log(f"報價連線 QUOTE CONN: {kind_names.get(nKind, nKind)} code={nCode}")
        if _app:
            if nKind == 3003 and nCode == 0:
                _app._quote_connected = True
                _app.btn_api.config(state=tk.NORMAL)
                _app.btn_deploy.config(state=tk.NORMAL)
                _app.btn_login.config(state=tk.DISABLED)
                _app.status_var.set("已連線 Connected - Ready")
                _app.login_status_var.set("已連線 Connected")
            elif nKind == 3002 and nCode == 0 and not _app._quote_connected:
                _app._quote_connected = True
                _app.btn_api.config(state=tk.NORMAL)
                _app.btn_deploy.config(state=tk.NORMAL)
                _app.btn_login.config(state=tk.DISABLED)
                _app.status_var.set("已連線 Connected (Quote) - Ready")
                _app.login_status_var.set("已連線 Connected")

    def OnNotifyKLineData(self, bstrStockNo, bstrData):
        if not _app:
            return
        # Route data based on current mode
        if _app._live_warmup_mode:
            _app._live_warmup_data.append(bstrData)
        elif _app._live_polling:
            _app._live_poll_data.append(bstrData)
        else:
            _app.kline_data.append(bstrData)
            _app._chunk_bar_count += 1
            n = len(_app.kline_data)
            if n <= 3:
                _log(f"K線原始資料 Raw KLine [{n}]: {bstrData!r}")

    def OnKLineComplete(self, nCode):
        if not _app:
            return
        if _app._live_warmup_mode:
            _log(f"暖機完成 Warmup KLine complete: {len(_app._live_warmup_data)} bars, code={nCode}")
            _app.root.after(100, _app._on_live_warmup_complete)
        elif _app._live_polling:
            _log(f"即時輪詢完成 Live poll complete: {len(_app._live_poll_data)} bars, code={nCode}")
            _app.root.after(100, _app._on_live_poll_complete)
        else:
            chunk_n = _app._chunk_bar_count
            total_n = len(_app.kline_data)
            _log(f"K線完成 KLine complete: chunk={chunk_n} bars, total={total_n}, code={nCode}")
            _app.root.after(100, _app._on_chunk_complete)

    def OnNotifyQuoteLONG(self, sMarketNo, nStockIdx):
        pass

    def OnNotifyHistoryTicksLONG(self, sMarketNo, nStockIdx, nPtr,
                                lDate, lTimehms, lTimemillismicros,
                                nBid, nAsk, nClose, nQty, nSimulate):
        # Minimal work on COM thread — just enqueue raw data.
        # Accessing Python objects (like _app) from COM thread crashes GIL in 3.13.
        _tick_queue.put_nowait((lDate, lTimehms, lTimemillismicros,
                                nBid, nAsk, nClose, nQty, nSimulate, True))

    def OnNotifyTicksLONG(self, sMarketNo, nStockIdx, nPtr,
                          lDate, lTimehms, lTimemillismicros,
                          nBid, nAsk, nClose, nQty, nSimulate):
        _tick_queue.put_nowait((lDate, lTimehms, lTimemillismicros,
                                nBid, nAsk, nClose, nQty, nSimulate, False))

    def OnNotifyBest5LONG(self, *args):
        pass

    def OnNotifyServerTime(self, sHour, sMinute, sSecond, nTotal):
        pass


class SKReplyLibEvent:
    def OnReplyMessage(self, bstrUserID, bstrMessage):
        _log(f"回報 REPLY: {bstrMessage}")
        return -1

    def OnConnect(self, bstrUserID, nErrorCode):
        _log(f"回報連線 REPLY CONN: code={nErrorCode}")

    def OnComplete(self, bstrUserID):
        pass

    def OnNewData(self, bstrUserID, bstrData):
        pass


class BacktestApp:
    def __init__(self, root: tk.Tk):
        global _app
        _app = self

        self.root = root
        self.root.title("tai-robot AI 策略工作台 AI Strategy Workbench")
        self.root.geometry("1500x900")
        self.root.minsize(1200, 700)

        self._settings = _load_settings()
        self.kline_data: list[str] = []
        self._logged_in = False
        self._quote_connected = False
        self._fetch_chunks: list[tuple[str, str]] = []
        self._fetch_chunk_idx: int = 0
        self._fetch_symbol: str = ""
        self._fetch_kline_type: int = 0
        self._fetch_minute_num: int = 0
        self._chunk_bar_count: int = 0

        # AI state
        self._chat_client: ChatClient | None = None
        self._ai_strategy_source: str = ""
        self._ai_strategy_cls: type[BacktestStrategy] | None = None
        self._strategy_store = StrategyStore(os.path.join(project_root, "strategies"))

        self._build_ui()
        self._load_saved_strategies()

        self.status_var.set("就緒 Ready")

    # ══════════════════════════════════════════════════════════════
    #  UI BUILD
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self):
        # Main horizontal split
        paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Left: Chat panel
        left = ttk.Frame(paned)
        paned.add(left, weight=2)
        self._build_chat_panel(left)

        # Right: Control panel + results notebook
        right = ttk.Frame(paned)
        paned.add(right, weight=3)
        self._build_control_panel(right)
        self._build_results_notebook(right)

        self._last_result = None
        self._last_bars: list[Bar] = []
        self._raw_bars: list[Bar] = []  # unfiltered bars from any source, for re-running
        self._raw_bars_key: tuple = ()  # (symbol, kline_type, kline_minute) of stored raw bars
        self._data_source: str = ""  # tracks where data came from for report
        self._pending_api_fetch: bool = False  # triggers fetch after login completes

        # Live trading state
        self._live_chart: LiveChart | None = None
        self._live_runner: LiveRunner | None = None
        self._live_poll_id = None  # root.after() id for cancellation
        self._live_warmup_mode: bool = False
        self._live_warmup_data: list[str] = []
        self._live_polling: bool = False
        self._live_poll_data: list[str] = []
        # Tick-based live data feed
        self._live_tick_active: bool = False
        self._live_bar_builder: BarBuilder | None = None
        self._live_tick_symbol: str = ""
        self._live_history_done: bool = False
        self._live_tick_count: int = 0
        self._live_history_tick_count: int = 0

    def _build_chat_panel(self, parent):
        # ── Header ──
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, padx=4, pady=(4, 2))

        ttk.Label(header, text="AI 策略工作台", font=("", 13, "bold")).pack(side=tk.LEFT)
        ttk.Button(header, text="New Chat", width=9, command=self._reset_chat).pack(side=tk.RIGHT, padx=2)
        ttk.Button(header, text="Settings", width=8, command=self._show_api_key_dialog).pack(side=tk.RIGHT, padx=2)

        # ── Chat display ──
        self.chat_display = scrolledtext.ScrolledText(
            parent, wrap=tk.WORD, font=("Consolas", 10),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
            state=tk.DISABLED, relief=tk.FLAT, padx=8, pady=8,
        )
        self.chat_display.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        # Chat text tags for styling
        self.chat_display.tag_configure("user", foreground="#569cd6", font=("Consolas", 10, "bold"))
        self.chat_display.tag_configure("assistant", foreground="#d4d4d4")
        self.chat_display.tag_configure("code", foreground="#ce9178", font=("Consolas", 9))
        self.chat_display.tag_configure("error", foreground="#f44747")
        self.chat_display.tag_configure("system", foreground="#6a9955")

        # ── Input area ──
        input_frame = ttk.Frame(parent)
        input_frame.pack(fill=tk.X, padx=4, pady=2)

        self.chat_input = tk.Text(
            input_frame, height=3, font=("Consolas", 10),
            bg="#252526", fg="#d4d4d4", insertbackground="white",
            relief=tk.FLAT, padx=6, pady=4,
        )
        self.chat_input.pack(fill=tk.X, expand=True)
        self.chat_input.bind("<Return>", self._on_chat_enter)
        self.chat_input.bind("<Shift-Return>", lambda e: None)  # allow newline

        # ── Action buttons ──
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(fill=tk.X, padx=4, pady=(2, 4))

        self.btn_send = ttk.Button(btn_frame, text="Send", width=7, command=self._send_chat)
        self.btn_send.pack(side=tk.LEFT, padx=2)

        self.btn_generate = ttk.Button(btn_frame, text="Generate Strategy",
                                        command=self._generate_strategy, state=tk.NORMAL)
        self.btn_generate.pack(side=tk.LEFT, padx=2)

        self.btn_pine = ttk.Button(btn_frame, text="Export Pine",
                                    command=self._export_pine, state=tk.DISABLED)
        self.btn_pine.pack(side=tk.LEFT, padx=2)

        self.btn_save_strategy = ttk.Button(btn_frame, text="Save Strategy",
                                             command=self._save_strategy, state=tk.DISABLED)
        self.btn_save_strategy.pack(side=tk.LEFT, padx=2)

        # ── Saved strategies dropdown ──
        saved_frame = ttk.Frame(parent)
        saved_frame.pack(fill=tk.X, padx=4, pady=(0, 4))

        ttk.Label(saved_frame, text="Saved:").pack(side=tk.LEFT, padx=2)
        self.saved_var = tk.StringVar()
        self.saved_combo = ttk.Combobox(saved_frame, textvariable=self.saved_var,
                                         state="readonly", width=30)
        self.saved_combo.pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)
        self.saved_combo.bind("<<ComboboxSelected>>", lambda e: self._load_saved_strategy())

        ttk.Button(saved_frame, text="Load", width=5,
                   command=self._load_saved_strategy).pack(side=tk.LEFT, padx=2)
        ttk.Button(saved_frame, text="Delete", width=6,
                   command=self._delete_saved_strategy).pack(side=tk.LEFT, padx=2)

    def _build_control_panel(self, parent):
        ctrl = ttk.LabelFrame(parent, text="回測設定 Backtest Settings", padding=8)
        ctrl.pack(fill=tk.X, padx=4, pady=(4, 2))

        # Row 0: Symbol + KLine type
        ttk.Label(ctrl, text="商品 Symbol:").grid(row=0, column=0, sticky=tk.W, padx=4)
        self.symbol_var = tk.StringVar(value="TX00")
        self.symbol_combo = ttk.Combobox(ctrl, textvariable=self.symbol_var, width=8,
                                          state="readonly", values=list(_SYMBOL_CONFIG.keys()))
        self.symbol_combo.grid(row=0, column=1, padx=4)
        self.symbol_combo.bind("<<ComboboxSelected>>", lambda e: self._on_symbol_changed())

        ttk.Label(ctrl, text="策略 Strategy:").grid(row=0, column=2, sticky=tk.W, padx=4)
        self.strategy_var = tk.StringVar(value=list(STRATEGIES.keys())[0])
        self.strategy_combo = ttk.Combobox(ctrl, textvariable=self.strategy_var, width=30,
                                            state="readonly", values=list(STRATEGIES.keys()))
        self.strategy_combo.grid(row=0, column=3, padx=4)
        ttk.Button(ctrl, text="原始碼", width=6, command=self._show_strategy_source).grid(row=0, column=4, padx=2)

        ttk.Label(ctrl, text="初始資金 Balance:").grid(row=0, column=5, sticky=tk.W, padx=4)
        self.balance_var = tk.StringVar(value="1000000")
        ttk.Entry(ctrl, textvariable=self.balance_var, width=12).grid(row=0, column=6, padx=4)

        ttk.Label(ctrl, text="每點價值 Pt Value:").grid(row=0, column=7, sticky=tk.W, padx=4)
        self.pv_var = tk.StringVar(value="200")
        ttk.Entry(ctrl, textvariable=self.pv_var, width=6).grid(row=0, column=8, padx=4)

        # Row 1: Date range + quick period buttons
        ttk.Label(ctrl, text="起始 Start:").grid(row=1, column=0, sticky=tk.W, padx=4, pady=4)
        default_start = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
        self.start_var = tk.StringVar(value=default_start)
        ttk.Entry(ctrl, textvariable=self.start_var, width=10).grid(row=1, column=1, padx=4)

        ttk.Label(ctrl, text="結束 End:").grid(row=1, column=2, sticky=tk.W, padx=4)
        self.end_var = tk.StringVar(value=datetime.now().strftime("%Y%m%d"))
        ttk.Entry(ctrl, textvariable=self.end_var, width=10).grid(row=1, column=3, padx=4)

        period_frame = ttk.Frame(ctrl)
        period_frame.grid(row=1, column=4, columnspan=4, padx=4, sticky=tk.W)
        for label, days in [("3月", 90), ("6月", 180), ("1年", 365), ("2年", 730), ("4年", 1461)]:
            ttk.Button(period_frame, text=label, width=4,
                       command=lambda d=days: self._set_period(d)).pack(side=tk.LEFT, padx=2)

        # Row 2: Strategy params
        ttk.Label(ctrl, text="BB週期:").grid(row=2, column=0, sticky=tk.W, padx=4, pady=4)
        self.bb_period_var = tk.StringVar(value="20")
        ttk.Entry(ctrl, textvariable=self.bb_period_var, width=6).grid(row=2, column=1, padx=4, sticky=tk.W)

        ttk.Label(ctrl, text="BB Std:").grid(row=2, column=2, sticky=tk.W, padx=4)
        self.bb_std_var = tk.StringVar(value="2.0")
        ttk.Entry(ctrl, textvariable=self.bb_std_var, width=6).grid(row=2, column=3, padx=4, sticky=tk.W)

        ttk.Label(ctrl, text="SL Offset:").grid(row=2, column=4, sticky=tk.W, padx=4)
        self.sl_offset_var = tk.StringVar(value="20")
        ttk.Entry(ctrl, textvariable=self.sl_offset_var, width=6).grid(row=2, column=5, padx=4, sticky=tk.W)

        ttk.Label(ctrl, text="TP Offset:").grid(row=2, column=6, sticky=tk.W, padx=4)
        self.tp_offset_var = tk.StringVar(value="50")
        ttk.Entry(ctrl, textvariable=self.tp_offset_var, width=6).grid(row=2, column=7, padx=4, sticky=tk.W)

        # Row 3: ATR strategy params
        ttk.Label(ctrl, text="ATR期數:").grid(row=3, column=0, sticky=tk.W, padx=4, pady=4)
        self.atr_period_var = tk.StringVar(value="14")
        ttk.Entry(ctrl, textvariable=self.atr_period_var, width=6).grid(row=3, column=1, padx=4, sticky=tk.W)

        ttk.Label(ctrl, text="SL×ATR:").grid(row=3, column=2, sticky=tk.W, padx=4)
        self.sl_mult_var = tk.StringVar(value="1.0")
        ttk.Entry(ctrl, textvariable=self.sl_mult_var, width=6).grid(row=3, column=3, padx=4, sticky=tk.W)

        ttk.Label(ctrl, text="TP×ATR:").grid(row=3, column=4, sticky=tk.W, padx=4)
        self.tp_mult_var = tk.StringVar(value="0.5")
        ttk.Entry(ctrl, textvariable=self.tp_mult_var, width=6).grid(row=3, column=5, padx=4, sticky=tk.W)

        # Row 4: Login
        ttk.Label(ctrl, text="帳號 User ID:").grid(row=4, column=0, sticky=tk.W, padx=4, pady=4)
        self.login_user_var = tk.StringVar(value=self._settings.get("user_id", ""))
        ttk.Entry(ctrl, textvariable=self.login_user_var, width=14).grid(row=4, column=1, padx=4, sticky=tk.W)

        ttk.Label(ctrl, text="密碼 Password:").grid(row=4, column=2, sticky=tk.W, padx=4)
        self.login_pass_var = tk.StringVar(value=self._settings.get("password", ""))
        ttk.Entry(ctrl, textvariable=self.login_pass_var, width=14, show="*").grid(row=4, column=3, padx=4, sticky=tk.W)

        self.btn_login = ttk.Button(ctrl, text="登入 Login", command=self._manual_login, width=10)
        self.btn_login.grid(row=4, column=4, padx=4, sticky=tk.W)

        self.login_status_var = tk.StringVar(value="")
        ttk.Label(ctrl, textvariable=self.login_status_var, foreground="gray").grid(
            row=4, column=5, columnspan=3, padx=4, sticky=tk.W)

        # Row 5: Action buttons
        btn_frame = ttk.Frame(ctrl)
        btn_frame.grid(row=5, column=0, columnspan=8, pady=(8, 0))

        self.btn_api = ttk.Button(btn_frame, text="API回測 API Backtest",
                                   command=self._do_fetch_api, state=tk.DISABLED)
        self.btn_api.pack(side=tk.LEFT, padx=4)

        self.btn_tv = ttk.Button(btn_frame, text="TV回測 TV Backtest",
                                 command=self._do_fetch_tv, state=tk.NORMAL)
        self.btn_tv.pack(side=tk.LEFT, padx=4)

        self.btn_export = ttk.Button(btn_frame, text="匯出交易 Export Trades",
                                     command=self._do_export, state=tk.DISABLED)
        self.btn_export.pack(side=tk.LEFT, padx=4)

        self.btn_chart = ttk.Button(btn_frame, text="顯示圖表 Show Chart",
                                    command=self._show_chart, state=tk.DISABLED)
        self.btn_chart.pack(side=tk.LEFT, padx=4)

        self.btn_chart_all = ttk.Button(btn_frame, text="全圖 Full Chart",
                                        command=self._show_chart_all, state=tk.DISABLED)
        self.btn_chart_all.pack(side=tk.LEFT, padx=4)

        self.btn_deploy = ttk.Button(btn_frame, text="部署機器人 Deploy Bot",
                                      command=self._toggle_live, state=tk.DISABLED)
        self.btn_deploy.pack(side=tk.LEFT, padx=4)

        ttk.Label(btn_frame, text="Chart TF:").pack(side=tk.LEFT, padx=(8, 2))
        self.chart_tf_var = tk.StringVar(value="Native")
        self.chart_tf_combo = ttk.Combobox(
            btn_frame, textvariable=self.chart_tf_var,
            values=list(_LIVE_CHART_TIMEFRAMES.keys()),
            state=tk.DISABLED, width=7,
        )
        self.chart_tf_combo.pack(side=tk.LEFT, padx=2)

        self.status_var = tk.StringVar(value="初始化中 Initializing...")
        ttk.Label(btn_frame, textvariable=self.status_var, foreground="gray").pack(side=tk.LEFT, padx=16)

    def _build_results_notebook(self, parent):
        notebook = ttk.Notebook(parent)
        notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=(2, 4))

        # Metrics tab
        metrics_frame = ttk.Frame(notebook)
        notebook.add(metrics_frame, text="績效報告 Report")
        self.metrics_text = scrolledtext.ScrolledText(metrics_frame, wrap=tk.WORD,
                                                       font=("Consolas", 11))
        self.metrics_text.pack(fill=tk.BOTH, expand=True)

        # Trade list tab
        trades_frame = ttk.Frame(notebook)
        notebook.add(trades_frame, text="交易明細 Trades")
        columns = ("num", "tag", "side", "entry_time", "entry_price",
                   "exit_time", "exit_price", "pnl", "bars_held")
        self.trade_tree = ttk.Treeview(trades_frame, columns=columns, show="headings", height=20)
        for col, text, w in [
            ("num", "#", 40), ("tag", "標籤 Tag", 80), ("side", "方向 Side", 55),
            ("entry_time", "進場時間 Entry Time", 135), ("entry_price", "進場價 Entry", 80),
            ("exit_time", "出場時間 Exit Time", 135), ("exit_price", "出場價 Exit", 80),
            ("pnl", "損益 P&L", 100), ("bars_held", "持倉K棒 Bars", 60),
        ]:
            self.trade_tree.heading(col, text=text)
            self.trade_tree.column(col, width=w, anchor=tk.E if col != "tag" else tk.W)
        vsb = ttk.Scrollbar(trades_frame, orient="vertical", command=self.trade_tree.yview)
        self.trade_tree.configure(yscrollcommand=vsb.set)
        self.trade_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Live tab
        live_frame = ttk.Frame(notebook)
        notebook.add(live_frame, text="即時 Live")

        # Status panel
        status_panel = ttk.LabelFrame(live_frame, text="即時狀態 Live Status", padding=6)
        status_panel.pack(fill=tk.X, padx=4, pady=(4, 2))

        self.live_state_var = tk.StringVar(value="IDLE")
        self.live_pos_var = tk.StringVar(value="Flat")
        self.live_pnl_var = tk.StringVar(value="0")
        self.live_bars_var = tk.StringVar(value="0 / 0")
        self.live_market_var = tk.StringVar(value="--")

        for i, (label, var) in enumerate([
            ("狀態 State:", self.live_state_var),
            ("持倉 Position:", self.live_pos_var),
            ("損益 P&L:", self.live_pnl_var),
            ("K棒 (即時/總計):", self.live_bars_var),
            ("盤勢 Market:", self.live_market_var),
        ]):
            ttk.Label(status_panel, text=label).grid(row=0, column=i*2, sticky=tk.W, padx=4)
            ttk.Label(status_panel, textvariable=var, font=("Consolas", 10, "bold")).grid(
                row=0, column=i*2+1, sticky=tk.W, padx=(0, 12))

        # Live event log
        self.live_log = scrolledtext.ScrolledText(live_frame, wrap=tk.WORD, font=("Consolas", 9),
                                                   bg="#1a1a2e", fg="#e0e0e0")
        self.live_log.pack(fill=tk.BOTH, expand=True, padx=4, pady=(2, 4))
        self.live_log.tag_configure("entry", foreground="#4caf50")
        self.live_log.tag_configure("exit", foreground="#f44336")
        self.live_log.tag_configure("bar", foreground="#90caf9")
        self.live_log.tag_configure("status", foreground="#ffc107")

        # Log tab
        log_frame = ttk.Frame(notebook)
        notebook.add(log_frame, text="紀錄 Log")
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ══════════════════════════════════════════════════════════════
    #  CHAT / AI METHODS
    # ══════════════════════════════════════════════════════════════

    def _ensure_chat_client(self) -> bool:
        """Initialize ChatClient if needed. Returns True if ready."""
        if self._chat_client is not None:
            return True

        provider = self._settings.get("ai_provider", PROVIDER_ANTHROPIC)
        if provider == PROVIDER_GOOGLE:
            api_key = self._settings.get("google_api_key", "")
        else:
            api_key = self._settings.get("anthropic_api_key", "")

        if not api_key:
            self._show_api_key_dialog()
            # Re-read after dialog
            provider = self._settings.get("ai_provider", PROVIDER_ANTHROPIC)
            if provider == PROVIDER_GOOGLE:
                api_key = self._settings.get("google_api_key", "")
            else:
                api_key = self._settings.get("anthropic_api_key", "")
            if not api_key:
                self._append_chat("error", "API key not set. Click Settings to configure.")
                return False

        model = self._settings.get("ai_model", "") or DEFAULT_MODELS.get(provider, "")
        max_tokens = self._settings.get("ai_max_tokens", 16384)
        self._chat_client = ChatClient(api_key, provider=provider, model=model, max_tokens=max_tokens)
        self._chat_client.set_system_prompt(STRATEGY_SYSTEM_PROMPT)
        self._append_chat("system", f"Using {provider} / {model}")
        return True

    def _on_chat_enter(self, event):
        """Handle Enter key in chat input — send message (Shift+Enter for newline)."""
        if not event.state & 0x1:  # not Shift
            self._send_chat()
            return "break"

    def _send_chat(self):
        """Send chat message to Claude in a background thread."""
        text = self.chat_input.get("1.0", tk.END).strip()
        if not text:
            return
        if not self._ensure_chat_client():
            return

        self.chat_input.delete("1.0", tk.END)
        self._append_chat("user", text)

        # Disable send while waiting
        self.btn_send.config(state=tk.DISABLED)
        self._append_chat("system", "Thinking...")

        def _worker():
            try:
                response = self._chat_client.send_message(text)
                self.root.after(0, lambda: self._on_chat_response(response))
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda: self._on_chat_error(err_msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_chat_response(self, response: str):
        """Handle Claude's response on the main thread."""
        # Remove "Thinking..." line
        self._remove_last_system_line()
        self._append_chat("assistant", response)
        self.btn_send.config(state=tk.NORMAL)

    def _on_chat_error(self, error: str):
        """Handle API error on the main thread."""
        self._remove_last_system_line()
        self._append_chat("error", f"Error: {error}")
        self.btn_send.config(state=tk.NORMAL)

    def _append_chat(self, role: str, text: str):
        """Append a styled message to the chat display."""
        self.chat_display.config(state=tk.NORMAL)

        provider = self._settings.get("ai_provider", PROVIDER_ANTHROPIC)
        ai_name = "Gemini" if provider == PROVIDER_GOOGLE else "Claude"
        prefix_map = {
            "user": "You: ",
            "assistant": f"{ai_name}: ",
            "error": "Error: " if not text.startswith("Error:") else "",
            "system": "",
            "code": "",
        }
        prefix = prefix_map.get(role, "")

        self.chat_display.insert(tk.END, prefix + text + "\n\n", role)
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

    def _remove_last_system_line(self):
        """Remove the last 'Thinking...' system message."""
        self.chat_display.config(state=tk.NORMAL)
        content = self.chat_display.get("1.0", tk.END)
        idx = content.rfind("Thinking...\n")
        if idx >= 0:
            # Calculate line.col position
            before = content[:idx]
            line = before.count("\n") + 1
            col = len(before) - before.rfind("\n") - 1
            start = f"{line}.{col}"
            # Find end of "Thinking...\n\n"
            end_idx = idx + len("Thinking...\n\n")
            after_before = content[:end_idx]
            end_line = after_before.count("\n") + 1
            end_col = len(after_before) - after_before.rfind("\n") - 1
            end = f"{end_line}.{end_col}"
            self.chat_display.delete(start, end)
        self.chat_display.config(state=tk.DISABLED)

    def _generate_strategy(self):
        """Ask AI to generate strategy code based on the conversation so far."""
        if not self._ensure_chat_client():
            return

        if not self._chat_client.conversation:
            self._append_chat("error", "Chat with the AI first to discuss a strategy idea.")
            return

        # Send a code-generation request with API context injected
        gen_msg = (
            "Based on our discussion, please write the complete strategy code now.\n\n"
            + STRATEGY_CODE_CONTEXT
        )
        self._append_chat("user", "Generate Strategy")
        self.btn_send.config(state=tk.DISABLED)
        self.btn_generate.config(state=tk.DISABLED)
        self._append_chat("system", "Generating code...")

        def _worker():
            try:
                response = self._chat_client.send_message(gen_msg)
                self.root.after(0, lambda: self._on_generate_response(response))
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda: self._on_chat_error(err_msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_generate_response(self, response: str, retries_left: int = 2):
        """Handle code generation response — extract, validate, load.

        On any error (missing code block, validation, execution), auto-retry
        by sending the error back to the AI with code context, up to retries_left times.
        """
        self._remove_last_system_line()
        self._append_chat("assistant", response)
        self.btn_send.config(state=tk.NORMAL)
        self.btn_generate.config(state=tk.NORMAL)

        # Try to extract code
        source = extract_python_code(response)
        if not source:
            self._generation_retry(
                "Your response did not contain a ```python code block. "
                "Please output the complete strategy class inside a single "
                "```python ... ``` code block.",
                retries_left,
            )
            return

        # Try to validate and load
        try:
            strategy_cls = load_strategy_from_source(source)
            self._on_strategy_generated(source, strategy_cls)
        except (CodeValidationError, CodeExecutionError) as e:
            self._generation_retry(
                f"The generated code had errors:\n{e}\n\n"
                "Please fix the code and output a corrected version.",
                retries_left,
            )
        except Exception as e:
            self._generation_retry(
                f"Unexpected error loading strategy:\n{e}\n\n"
                "Please fix the code and output a corrected version.",
                retries_left,
            )

    def _generation_retry(self, error_msg: str, retries_left: int):
        """Send error back to AI and retry code generation."""
        self._append_chat("error", error_msg.split("\n")[0])

        if retries_left <= 0 or not self._chat_client:
            self._append_chat("error", "Auto-retry exhausted. Please fix manually and try again.")
            return

        self._append_chat("system", f"Auto-retrying... ({retries_left} left)")
        self.btn_send.config(state=tk.DISABLED)
        self.btn_generate.config(state=tk.DISABLED)

        retry_msg = error_msg + "\n\n" + STRATEGY_CODE_CONTEXT
        remaining = retries_left - 1

        def _worker():
            try:
                resp = self._chat_client.send_message(retry_msg)
                self.root.after(0, lambda: self._on_generate_response(resp, retries_left=remaining))
            except Exception as e:
                err_msg = str(e)
                self.root.after(0, lambda: self._on_chat_error(err_msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_strategy_generated(self, source: str, strategy_cls: type[BacktestStrategy]):
        """Register a successfully loaded AI strategy."""
        self._ai_strategy_source = source
        self._ai_strategy_cls = strategy_cls
        name = f"AI: {strategy_cls.__name__}"

        STRATEGIES[name] = strategy_cls
        self.strategy_combo.config(values=list(STRATEGIES.keys()))
        self.strategy_var.set(name)

        self._append_chat("system",
                          f"Strategy loaded: {strategy_cls.__name__}\n"
                          f"Selected in dropdown: \"{name}\"\n"
                          "Click 'Run Backtest' to test it.")

        self.btn_generate.config(state=tk.NORMAL)
        self.btn_pine.config(state=tk.NORMAL)
        self.btn_save_strategy.config(state=tk.NORMAL)

    def _export_pine(self):
        """Export the current AI strategy to Pine Script in a popup."""
        if not self._ai_strategy_source:
            self._append_chat("error", "No AI strategy to export. Generate one first.")
            return
        if not self._ensure_chat_client():
            return

        self._append_chat("system", "Exporting to Pine Script...")
        self.btn_pine.config(state=tk.DISABLED)

        source = self._ai_strategy_source

        def _worker():
            try:
                pine = export_to_pine(self._chat_client, source)
                self.root.after(0, lambda: self._show_pine_popup(pine))
            except Exception as e:
                self.root.after(0, lambda: self._on_pine_error(str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _show_pine_popup(self, pine_code: str):
        """Show Pine Script in a popup window with Copy button."""
        self._remove_last_system_line()
        self._append_chat("system", "Pine Script export complete.")
        self.btn_pine.config(state=tk.NORMAL)

        popup = tk.Toplevel(self.root)
        popup.title("Pine Script Export")
        popup.geometry("700x600")

        text = scrolledtext.ScrolledText(popup, wrap=tk.WORD, font=("Consolas", 10))
        text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        text.insert(tk.END, pine_code)

        btn_frame = ttk.Frame(popup)
        btn_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        def copy():
            popup.clipboard_clear()
            popup.clipboard_append(pine_code)
            copy_btn.config(text="Copied!")
            popup.after(2000, lambda: copy_btn.config(text="Copy to Clipboard"))

        copy_btn = ttk.Button(btn_frame, text="Copy to Clipboard", command=copy)
        copy_btn.pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Close", command=popup.destroy).pack(side=tk.RIGHT, padx=4)

    def _on_pine_error(self, error: str):
        self._remove_last_system_line()
        self._append_chat("error", f"Pine export error: {error}")
        self.btn_pine.config(state=tk.NORMAL)

    def _save_strategy(self):
        """Save the current AI strategy to the strategies/ directory."""
        if not self._ai_strategy_source or not self._ai_strategy_cls:
            return

        class_name = self._ai_strategy_cls.__name__

        # Ask for description
        desc = simpledialog.askstring(
            "Save Strategy",
            f"Description for {class_name}:",
            parent=self.root,
        )
        if desc is None:
            return

        path = self._strategy_store.save(class_name, self._ai_strategy_source, desc)
        self._append_chat("system", f"Strategy saved: {path}")
        self._refresh_saved_combo()

    def _load_saved_strategies(self):
        """Load saved strategy names into the combo on startup."""
        self._refresh_saved_combo()

    def _refresh_saved_combo(self):
        entries = self._strategy_store.list_strategies()
        names = [e["class_name"] for e in entries]
        self.saved_combo.config(values=names)

    def _load_saved_strategy(self):
        """Load a saved strategy from the strategies/ directory."""
        class_name = self.saved_var.get()
        if not class_name:
            return

        source = self._strategy_store.load_source(class_name)
        if not source:
            self._append_chat("error", f"Could not load: {class_name}")
            return

        try:
            strategy_cls = load_strategy_from_source(source)
            self._on_strategy_generated(source, strategy_cls)
            self._append_chat("system", f"Loaded saved strategy: {class_name}")
        except (CodeValidationError, CodeExecutionError) as e:
            self._append_chat("error", f"Failed to load {class_name}: {e}")

    def _delete_saved_strategy(self):
        """Delete a saved strategy."""
        class_name = self.saved_var.get()
        if not class_name:
            return
        self._strategy_store.delete(class_name)
        self._refresh_saved_combo()
        self.saved_var.set("")
        self._append_chat("system", f"Deleted: {class_name}")

    def _show_api_key_dialog(self):
        """Show settings dialog with provider selection and API keys."""
        dialog = tk.Toplevel(self.root)
        dialog.title("AI Settings")
        dialog.geometry("450x280")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        # Provider selection
        ttk.Label(frame, text="Provider:", font=("", 10, "bold")).grid(
            row=0, column=0, sticky=tk.W, pady=(0, 8))
        provider_var = tk.StringVar(value=self._settings.get("ai_provider", PROVIDER_ANTHROPIC))
        provider_combo = ttk.Combobox(frame, textvariable=provider_var, state="readonly",
                                       values=[PROVIDER_ANTHROPIC, PROVIDER_GOOGLE], width=20)
        provider_combo.grid(row=0, column=1, sticky=tk.W, pady=(0, 8), padx=(8, 0))

        # Anthropic key
        ttk.Label(frame, text="Anthropic API Key:").grid(row=1, column=0, sticky=tk.W, pady=4)
        anth_key = self._settings.get("anthropic_api_key", "")
        anth_var = tk.StringVar(value=anth_key)
        anth_entry = ttk.Entry(frame, textvariable=anth_var, width=38, show="*")
        anth_entry.grid(row=1, column=1, sticky=tk.W, pady=4, padx=(8, 0))

        # Google key
        ttk.Label(frame, text="Google Gemini Key:").grid(row=2, column=0, sticky=tk.W, pady=4)
        goog_key = self._settings.get("google_api_key", "")
        goog_var = tk.StringVar(value=goog_key)
        goog_entry = ttk.Entry(frame, textvariable=goog_var, width=38, show="*")
        goog_entry.grid(row=2, column=1, sticky=tk.W, pady=4, padx=(8, 0))

        # Model override (optional)
        ttk.Label(frame, text="Model (optional):").grid(row=3, column=0, sticky=tk.W, pady=4)
        model_var = tk.StringVar(value=self._settings.get("ai_model", ""))
        ttk.Entry(frame, textvariable=model_var, width=38).grid(
            row=3, column=1, sticky=tk.W, pady=4, padx=(8, 0))

        # Default model hint
        def _update_hint(*args):
            p = provider_var.get()
            hint_label.config(text=f"Default: {DEFAULT_MODELS.get(p, '')}")
        hint_label = ttk.Label(frame, text="", foreground="gray")
        hint_label.grid(row=4, column=1, sticky=tk.W, padx=(8, 0))
        provider_var.trace_add("write", _update_hint)
        _update_hint()

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=5, column=0, columnspan=2, pady=(16, 0))

        def _save():
            provider = provider_var.get()
            ak = anth_var.get().strip()
            gk = goog_var.get().strip()
            model = model_var.get().strip()

            self._settings["ai_provider"] = provider
            self._settings["anthropic_api_key"] = ak
            self._settings["google_api_key"] = gk
            self._settings["ai_model"] = model

            _save_ai_settings(provider=provider, anthropic_key=ak,
                              google_key=gk, model=model)

            # Reset client so it picks up new settings
            if self._chat_client:
                self._chat_client.close()
                self._chat_client = None

            self._append_chat("system", f"Settings saved. Provider: {provider}")
            dialog.destroy()

        ttk.Button(btn_frame, text="Save", width=10, command=_save).pack(side=tk.LEFT, padx=8)
        ttk.Button(btn_frame, text="Cancel", width=10, command=dialog.destroy).pack(side=tk.LEFT, padx=8)

    def _reset_chat(self):
        """Clear chat display and reset conversation."""
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.delete("1.0", tk.END)
        self.chat_display.config(state=tk.DISABLED)
        if self._chat_client:
            self._chat_client.reset()
        self._append_chat("system",
                          "New chat started. Describe your trading strategy idea.\n"
                          "Example: '寫一個RSI反轉策略' or 'Create a dual MA crossover strategy'")

    # ══════════════════════════════════════════════════════════════
    #  EXISTING BACKTEST METHODS (unchanged logic)
    # ══════════════════════════════════════════════════════════════

    def _set_period(self, days):
        self.end_var.set(datetime.now().strftime("%Y%m%d"))
        self.start_var.set((datetime.now() - timedelta(days=days)).strftime("%Y%m%d"))

    def _on_symbol_changed(self):
        """Auto-set point value when symbol changes and clear cached bars."""
        symbol = self.symbol_var.get()
        cfg = _SYMBOL_CONFIG.get(symbol)
        if cfg:
            self.pv_var.set(str(cfg["pv"]))
        self._raw_bars = []

    def _show_strategy_source(self):
        """Show source code of the selected strategy in a popup window."""
        name = self.strategy_var.get()
        cls = STRATEGIES.get(name)
        if not cls:
            return
        try:
            source = inspect.getsource(cls)
            filepath = inspect.getfile(cls)
        except (OSError, TypeError):
            messagebox.showerror("錯誤", f"無法取得 {name} 的原始碼")
            return
        win = tk.Toplevel(self.root)
        win.title(f"原始碼 — {name}")
        win.geometry("800x600")
        text = scrolledtext.ScrolledText(
            win, wrap=tk.NONE, font=("Consolas", 10),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="white",
        )
        text.pack(fill=tk.BOTH, expand=True)
        text.insert(tk.END, f"# {filepath}\n\n{source}")
        text.config(state=tk.DISABLED)

    def _enable_buttons(self):
        """Re-enable buttons after a run completes."""
        if self._quote_connected:
            self.btn_api.config(state=tk.NORMAL)
            self.btn_deploy.config(state=tk.NORMAL)
        self.btn_tv.config(state=tk.NORMAL)

    def _disable_buttons(self):
        """Disable buttons during a run."""
        self.btn_api.config(state=tk.DISABLED)
        self.btn_tv.config(state=tk.DISABLED)
        self.btn_deploy.config(state=tk.DISABLED)

    # ── COM Login ──

    def _manual_login(self):
        """Login button handler — reads credentials from the form."""
        if not _com_available:
            self.login_status_var.set("COM不可用 COM unavailable")
            return
        if self._logged_in:
            self.login_status_var.set("已登入 Already logged in")
            return

        user_id = self.login_user_var.get().strip()
        password = self.login_pass_var.get().strip()
        if not user_id or not password:
            self.login_status_var.set("請輸入帳號密碼 Enter credentials")
            return

        self.btn_login.config(state=tk.DISABLED)
        self._do_login(user_id, password)

    def _do_login(self, user_id: str = "", password: str = ""):
        """Perform COM login with given credentials."""
        if not user_id:
            user_id = self.login_user_var.get().strip()
        if not password:
            password = self.login_pass_var.get().strip()

        try:
            authority_flag = self._settings.get("authority_flag", 0)

            log_dir = os.path.join(project_root, "CapitalLog_Backtest")
            os.makedirs(log_dir, exist_ok=True)
            skC.SKCenterLib_SetLogPath(log_dir)

            if authority_flag:
                skC.SKCenterLib_SetAuthority(authority_flag)

            _log(f"登入中 Logging in as {user_id}...")
            self.status_var.set("登入中 Logging in...")
            self.login_status_var.set("登入中...")

            code = skC.SKCenterLib_LoginSetQuote(user_id, password, "Y")
            if code != 0 and not (2000 <= code < 3000):
                msg = skC.SKCenterLib_GetReturnCodeMessage(code)
                _log(f"登入失敗 LOGIN FAILED: code={code} {msg}")
                if code == 1097:
                    _log("提示: 請確認已安裝群益API憑證 (從券商網站下載安裝)")
                    _log("Hint: Ensure Capital API certificate is installed (download from broker website)")
                self.status_var.set(f"登入失敗 Login failed: {msg}")
                self.login_status_var.set(f"登入失敗 {msg}")
                self.btn_login.config(state=tk.NORMAL)
                self._pending_api_fetch = False
                self._enable_buttons()
                return
            self._logged_in = True
            _log(f"登入成功 LOGIN OK (code={code})")
            self.login_status_var.set("登入成功 Logged in")

            skR.SKReplyLib_ConnectByID(user_id)
            code = skQ.SKQuoteLib_EnterMonitorLONG()
            _log(f"進入報價監控 EnterMonitorLONG: code={code}")
            self.status_var.set("連線中 Connecting...")
            self.root.after(3000, self._check_connection)

        except Exception as e:
            _log(f"初始化錯誤 Init error: {e}")
            self.status_var.set(f"錯誤 Error: {e}")
            self.login_status_var.set(f"錯誤 Error")
            self.btn_login.config(state=tk.NORMAL)
            self._pending_api_fetch = False
            self._enable_buttons()

    def _check_connection(self):
        try:
            ic = skQ.SKQuoteLib_IsConnected()
            if ic == 1:
                self._quote_connected = True
                self.btn_api.config(state=tk.NORMAL)
                self.btn_deploy.config(state=tk.NORMAL)
                self.btn_login.config(state=tk.DISABLED)
                self.status_var.set("已連線 Connected - Ready")
                self.login_status_var.set("已連線 Connected")
                if self._pending_api_fetch:
                    self._pending_api_fetch = False
                    self.root.after(100, self._do_fetch_api)
            elif not self._quote_connected:
                self.root.after(2000, self._check_connection)
        except Exception as e:
            _log(f"連線檢查錯誤: {e}")

    # ── Data fetch ──

    def _do_fetch_api(self):
        """Fetch from Capital API. Requires login first."""
        self._data_source = "API"

        if not self._quote_connected:
            self.status_var.set("請先登入 Please login first")
            self.login_status_var.set("請先登入 Login required")
            return

        symbol = self.symbol_var.get().strip()
        if not symbol:
            return

        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            return
        kline_type = strategy_cls.kline_type
        minute_num = strategy_cls.kline_minute
        start_date = self.start_var.get().strip()
        end_date = self.end_var.get().strip()

        # Split into adaptive chunks (API returns max ~316 bars per request)
        try:
            dt_start = datetime.strptime(start_date, "%Y%m%d")
            dt_end = datetime.strptime(end_date, "%Y%m%d")
        except ValueError:
            self.status_var.set("日期格式錯誤 Date format error (YYYYMMDD)")
            return

        # Estimate bars per trading day for each timeframe, then size chunks
        # to stay well under the 316-bar API cap (target ~250 bars/chunk).
        if kline_type == 4:       # Daily
            bars_per_tday = 1
        elif minute_num >= 240:   # H4
            bars_per_tday = 6
        elif minute_num >= 60:    # 1H
            bars_per_tday = 14
        elif minute_num >= 30:    # 30m
            bars_per_tday = 28
        else:                     # 15m or less
            bars_per_tday = 56
        trading_days = 250 // bars_per_tday
        chunk_days = max(5, int(trading_days * 7 / 5))
        self._fetch_chunks = []
        cursor = dt_start
        while cursor < dt_end:
            chunk_end = min(cursor + timedelta(days=chunk_days), dt_end)
            self._fetch_chunks.append((cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
            cursor = chunk_end + timedelta(days=1)

        self.kline_data = []
        self._fetch_chunk_idx = 0
        self._disable_buttons()

        total_days = (dt_end - dt_start).days
        n_chunks = len(self._fetch_chunks)
        _log(f"分段查詢 Fetching in {n_chunks} chunks ({total_days} days, {chunk_days}d/chunk)")

        self._fetch_next_chunk(symbol, kline_type, minute_num)

    def _fetch_next_chunk(self, symbol, kline_type, minute_num):
        """Fetch the next chunk of KLine data."""
        if self._fetch_chunk_idx >= len(self._fetch_chunks):
            _log(f"全部完成 All chunks fetched: {len(self.kline_data)} total KLine strings")
            self._run_backtest()
            return

        chunk_start, chunk_end = self._fetch_chunks[self._fetch_chunk_idx]
        n = self._fetch_chunk_idx + 1
        total = len(self._fetch_chunks)
        self.status_var.set(f"查詢中 Fetching chunk {n}/{total}: {chunk_start}~{chunk_end}")
        self._chunk_bar_count = 0
        _log(f"請求K線 [{n}/{total}] {symbol} type={kline_type} "
             f"{chunk_start}~{chunk_end} min={minute_num}")

        self._fetch_symbol = symbol
        self._fetch_kline_type = kline_type
        self._fetch_minute_num = minute_num

        try:
            code = skQ.SKQuoteLib_RequestKLineAMByDate(
                symbol, kline_type, 1, 0, chunk_start, chunk_end, minute_num)

            if code != 0:
                msg = skC.SKCenterLib_GetReturnCodeMessage(code)
                _log(f"請求結果 Result: code={code} {msg}")
                if code >= 3000:
                    self.status_var.set(f"錯誤 Error: {msg}")
                    self._enable_buttons()
                    return

        except Exception as e:
            _log(f"查詢錯誤 Fetch error: {e}")
            self._enable_buttons()

    def _do_fetch_tv(self):
        """Use TradingView data: local CSV first, re-use in-memory, or download live."""
        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            self.status_var.set("請選擇策略 Select a strategy")
            return

        # Fast re-run: reuse TV bars already in memory (e.g. date range change)
        symbol = self.symbol_var.get().strip()
        if (self._data_source.startswith("TradingView") and should_reuse_bars(
            self._raw_bars, self._raw_bars_key,
            symbol, strategy_cls.kline_type, strategy_cls.kline_minute,
        )):
            _log("重新使用TV資料 Re-using TV data with date filter")
            self._execute_backtest(list(self._raw_bars))
            return

        kt = strategy_cls.kline_type
        km = strategy_cls.kline_minute
        symbol = self.symbol_var.get().strip()
        cache_file = _get_cache_file(symbol, (kt, km))

        # Try local CSV first
        if cache_file:
            cache_path = os.path.join(_CACHE_DIR, cache_file)
            if os.path.exists(cache_path):
                self._data_source = f"TradingView ({cache_file})"
                interval = self._get_strategy_interval()
                _log(f"載入快取 Loading cached data: {cache_file}")
                bars = load_bars_from_csv(cache_path, symbol=symbol, interval=interval)
                _log(f"載入完成 Loaded {len(bars)} bars from {cache_file}")
                self._execute_backtest(bars)
                return

        # Fall back to live TradingView download
        if _tv_available:
            _log("本地無資料，從TradingView下載 No local data, fetching from TradingView...")
            self._fetch_tradingview_live()
            return

        self.status_var.set("無資料 No local CSV and tvDatafeed not installed.")
        self._enable_buttons()

    def _fetch_tradingview_live(self):
        """Download fresh data from TradingView API as fallback."""
        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            return

        kt = strategy_cls.kline_type
        km = strategy_cls.kline_minute
        tv_interval_name = _TV_INTERVALS.get((kt, km))
        if not tv_interval_name:
            self.status_var.set(f"TradingView不支援此週期 Unsupported interval: type={kt} min={km}")
            self._enable_buttons()
            return

        tv_interval = getattr(TvInterval, tv_interval_name)
        raw_symbol = self.symbol_var.get().strip()
        cfg = _SYMBOL_CONFIG.get(raw_symbol)
        symbol = cfg["tv"] if cfg else raw_symbol
        exchange = "TAIFEX"
        interval = INTERVAL_SECONDS.get((kt, km), 14400)

        self._data_source = "TradingView (live)"
        self._disable_buttons()
        self.status_var.set(f"從TradingView下載 Fetching from TradingView: {symbol} {tv_interval_name}...")
        self.root.update()

        _log(f"TradingView下載 Fetching {symbol}@{exchange} interval={tv_interval_name} n_bars=5000")

        try:
            tv = TvDatafeed()
            df = tv.get_hist(symbol=symbol, exchange=exchange,
                             interval=tv_interval, n_bars=5000)
            if df is None or df.empty:
                _log("TradingView無資料 No data from TradingView")
                self.status_var.set("TradingView無資料 No data")
                self._enable_buttons()
                return

            bars = []
            for dt_idx, row in df.iterrows():
                bars.append(Bar(
                    symbol=symbol, dt=dt_idx.to_pydatetime(),
                    open=round(row["open"]), high=round(row["high"]),
                    low=round(row["low"]), close=round(row["close"]),
                    volume=int(row.get("volume", 0)),
                    interval=interval,
                ))
            bars.sort(key=lambda b: b.dt)

            _log(f"TradingView完成 Got {len(bars)} bars: {bars[0].dt} ~ {bars[-1].dt}")
            self._execute_backtest(bars)

        except Exception as e:
            _log(f"TradingView錯誤 TV error: {e}")
            self.status_var.set(f"TradingView錯誤: {e}")
            self._enable_buttons()

    # ── Backtest execution ──

    def _on_chunk_complete(self):
        """Called after each KLine chunk completes. Fetches next chunk or runs backtest."""
        self._fetch_chunk_idx += 1
        if self._fetch_chunk_idx < len(self._fetch_chunks):
            self.root.after(500, lambda: self._fetch_next_chunk(
                self._fetch_symbol, self._fetch_kline_type, self._fetch_minute_num))
        else:
            self._run_backtest()

    def _get_strategy_interval(self) -> int:
        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            return 14400
        kt = strategy_cls.kline_type
        km = strategy_cls.kline_minute
        return INTERVAL_SECONDS.get((kt, km), 14400)

    def _run_backtest(self):
        """Called after all KLine data arrives from COM API."""
        symbol = self.symbol_var.get().strip()
        interval = self._get_strategy_interval()

        _log(f"解析K線資料 Parsing {len(self.kline_data)} KLine strings...")
        bars = parse_kline_strings(self.kline_data, symbol=symbol, interval=interval)

        seen = set()
        unique_bars = []
        for b in bars:
            if b.dt not in seen:
                seen.add(b.dt)
                unique_bars.append(b)
        if len(unique_bars) < len(bars):
            _log(f"去重 Deduplicated: {len(bars)} -> {len(unique_bars)} bars")
        bars = unique_bars

        if bars:
            _log(f"API資料 Parsed {len(bars)} API bars: {bars[0].dt} ~ {bars[-1].dt}")
        else:
            _log("API資料 Parsed 0 API bars")

        self._execute_backtest(bars)

    def _execute_backtest(self, bars: list[Bar]):
        if not bars:
            self.status_var.set("無資料 No data")
            self._enable_buttons()
            return

        # Store raw bars for re-running with different date ranges
        self._raw_bars = bars
        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if strategy_cls:
            sym = self.symbol_var.get().strip()
            self._raw_bars_key = (sym, strategy_cls.kline_type, strategy_cls.kline_minute)

        # Apply date filter from GUI
        try:
            start_str = self.start_var.get().strip()
            end_str = self.end_var.get().strip()
            before = len(bars)
            bars = filter_bars_by_date(bars, start_str, end_str)
            if bars and len(bars) < before:
                _log(f"日期篩選 Date filter: {before} -> {len(bars)} bars "
                     f"({bars[0].dt} ~ {bars[-1].dt})")
        except ValueError:
            pass

        # Notify user if data range is shorter than requested
        if bars:
            actual_start = bars[0].dt.strftime("%Y%m%d")
            actual_end = bars[-1].dt.strftime("%Y%m%d")
            req_start = self.start_var.get().strip()
            req_end = self.end_var.get().strip()
            if actual_start != req_start or actual_end != req_end:
                _log(f"資料範圍修正 Data range adjusted: {actual_start} ~ {actual_end}")
                ok = messagebox.askokcancel(
                    "資料範圍不足 Insufficient Data Range",
                    f"要求範圍 Requested: {req_start} ~ {req_end}\n"
                    f"實際範圍 Available: {actual_start} ~ {actual_end}\n"
                    f"共 {len(bars)} bars\n\n"
                    f"是否繼續回測？Continue with available data?",
                )
                if not ok:
                    self.status_var.set("已取消 Cancelled")
                    self._enable_buttons()
                    return
                # Update date fields so re-run won't show popup again
                self.start_var.set(actual_start)
                self.end_var.set(actual_end)

        if not bars:
            self.status_var.set("篩選後無資料 No data after date filter")
            self._enable_buttons()
            return

        # Read parameters
        try:
            point_value = int(self.pv_var.get())
            initial_balance = int(self.balance_var.get())
            bb_period = int(self.bb_period_var.get())
            bb_std = float(self.bb_std_var.get())
            sl_offset = int(self.sl_offset_var.get())
            tp_offset = int(self.tp_offset_var.get())
            atr_period = int(self.atr_period_var.get())
            sl_mult = float(self.sl_mult_var.get())
            tp_mult = float(self.tp_mult_var.get())
        except ValueError as e:
            self.status_var.set(f"參數錯誤 Param error: {e}")
            self._enable_buttons()
            return

        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            self.status_var.set("請選擇策略 Select a strategy")
            self._enable_buttons()
            return

        # Instantiate strategy: AI strategies use defaults, built-in ones use GUI params
        strategy_name = self.strategy_var.get()
        if strategy_name.startswith("AI:"):
            # AI-generated strategies have params baked into __init__ defaults
            strategy = strategy_cls()
        elif strategy_cls in (H4BollingerAtrLongStrategy, M1BollingerAtrLongStrategy):
            strategy = strategy_cls(
                bb_period=bb_period, bb_std=bb_std,
                atr_period=atr_period, sl_mult=sl_mult, tp_mult=tp_mult,
            )
        else:
            try:
                strategy = strategy_cls(
                    bb_period=bb_period, bb_std=bb_std,
                    sl_offset=sl_offset, tp_offset=tp_offset,
                )
            except TypeError:
                strategy = strategy_cls()
        engine = BacktestEngine(strategy, point_value=point_value)

        _log(f"開始回測 Running backtest: {len(bars)} bars, "
             f"balance={initial_balance:,}, point_value={point_value}")
        self.status_var.set("回測中 Running backtest...")

        try:
            result = engine.run(bars)
        except Exception as e:
            tb = traceback.format_exc()
            _log(f"回測錯誤 Backtest error:\n{tb}")
            self.status_var.set(f"回測錯誤 Backtest error: {e}")
            self._append_chat("error", f"Backtest runtime error:\n{e}")
            self._enable_buttons()
            return

        # Recalculate metrics with initial balance
        result.metrics = calculate_metrics(
            result.trades, result.equity_curve, initial_balance=initial_balance)

        self._last_result = result
        self._last_bars = bars
        self._display_results(result, bars)

        self._enable_buttons()
        self.btn_export.config(state=tk.NORMAL)
        if _LWC_AVAILABLE and result.trades:
            self.btn_chart.config(state=tk.NORMAL)
            self.btn_chart_all.config(state=tk.NORMAL)
        self.status_var.set(
            f"完成 Done: {result.metrics.total_trades} trades, "
            f"win rate {result.metrics.win_rate * 100:.1f}%, "
            f"P&L {result.metrics.total_pnl:+,}")

    def _display_results(self, result, bars: list[Bar] | None = None):
        # Metrics report
        self.metrics_text.delete("1.0", tk.END)

        # Data source header
        symbol = self.symbol_var.get().strip()
        source = self._data_source or "unknown"
        header_lines = [f" 商品 Symbol:  {symbol}", f" 資料來源 Source:  {source}"]
        if bars:
            header_lines.append(f" 資料範圍 Range:  {bars[0].dt.strftime('%Y-%m-%d %H:%M')} ~ "
                                f"{bars[-1].dt.strftime('%Y-%m-%d %H:%M')}")
            header_lines.append(f" K棒數量 Bars:  {len(bars)}")
        # Show live-specific info
        if self._live_runner and self._live_runner.state != LiveState.IDLE:
            status = self._live_runner.get_status()
            header_lines.append(f" 即時狀態 State:  {status['state']}")
            header_lines.append(f" 1分K / 聚合K  1m/Agg:  {status['bars_1m']} / {status['bars_agg']}")
        self.metrics_text.insert(tk.END, "\n".join(header_lines) + "\n\n")

        report = format_report(result.strategy_name, result.metrics)
        self.metrics_text.insert(tk.END, report)

        # Trade list
        for item in self.trade_tree.get_children():
            self.trade_tree.delete(item)
        for i, t in enumerate(result.trades, 1):
            bars_held = t.exit_bar_index - t.entry_bar_index
            pnl_str = f"{t.pnl:+,}"
            row_tag = "win" if t.pnl > 0 else "loss"

            entry_dt = ""
            exit_dt = ""
            if bars:
                if 0 <= t.entry_bar_index < len(bars):
                    entry_dt = bars[t.entry_bar_index].dt.strftime("%Y-%m-%d %H:%M")
                if 0 <= t.exit_bar_index < len(bars):
                    exit_dt = bars[t.exit_bar_index].dt.strftime("%Y-%m-%d %H:%M")

            self.trade_tree.insert("", tk.END, values=(
                i, t.tag, t.side.value, entry_dt, f"{t.entry_price:,}",
                exit_dt, f"{t.exit_price:,}", pnl_str, bars_held,
            ), tags=(row_tag,))

        self.trade_tree.tag_configure("win", foreground="green")
        self.trade_tree.tag_configure("loss", foreground="red")

        if self._live_runner and self._live_runner.state != LiveState.IDLE:
            _log(f"即時結果更新 Live results: {result.metrics.total_trades} trades")
        else:
            _log(f"回測完成 Backtest complete: {result.metrics.total_trades} trades")

    def _get_selected_trade_index(self) -> int | None:
        sel = self.trade_tree.selection()
        if not sel:
            return None
        values = self.trade_tree.item(sel[0], "values")
        if values:
            return int(values[0]) - 1
        return None

    def _chart_kwargs(self) -> dict:
        try:
            bb_period = int(self.bb_period_var.get())
            bb_std = float(self.bb_std_var.get())
        except ValueError:
            bb_period, bb_std = 20, 2.0
        return dict(bb_period=bb_period, bb_std=bb_std)

    def _show_chart(self):
        # Live-updating chart when bot is running on native TF
        if (self._live_runner and self._live_runner.state == LiveState.RUNNING
                and self.chart_tf_var.get() == "Native"):
            self._show_live_chart()
            return
        bars, result, show_trades = self._get_chart_data()
        if not result or not bars:
            return
        strategy_name = self.strategy_var.get()
        trades = list(result.trades) if show_trades else []
        focus = self._get_selected_trade_index() if show_trades else None
        if show_trades and focus is None:
            focus = 0
        # Append timeframe label in live mode
        if self._live_runner and self._live_runner.state in (LiveState.RUNNING, LiveState.STOPPED):
            strategy_name = f"{strategy_name} [{self.chart_tf_var.get()}]"
        kwargs = self._chart_kwargs()
        threading.Thread(
            target=self._run_chart, daemon=True,
            args=(list(bars), trades, strategy_name, focus, kwargs),
        ).start()

    def _show_chart_all(self):
        # Live-updating chart when bot is running on native TF
        if (self._live_runner and self._live_runner.state == LiveState.RUNNING
                and self.chart_tf_var.get() == "Native"):
            self._show_live_chart()
            return
        bars, result, show_trades = self._get_chart_data()
        if not result or not bars:
            return
        strategy_name = self.strategy_var.get()
        trades = list(result.trades) if show_trades else []
        # Append timeframe label in live mode
        if self._live_runner and self._live_runner.state in (LiveState.RUNNING, LiveState.STOPPED):
            strategy_name = f"{strategy_name} [{self.chart_tf_var.get()}]"
        kwargs = self._chart_kwargs()
        threading.Thread(
            target=self._run_chart, daemon=True,
            args=(list(bars), trades, strategy_name, None, kwargs),
        ).start()

    def _get_chart_data(self):
        """Return (bars, result, show_trades) from live runner or backtest.

        In live mode, the Chart TF dropdown selects the timeframe:
        - "Native" = strategy-interval bars with trade markers
        - Other = re-aggregated from 1m bars, no trade markers
        """
        if self._live_runner and self._live_runner.state in (LiveState.RUNNING, LiveState.STOPPED):
            result = self._live_runner.get_result()
            tf_label = self.chart_tf_var.get()
            interval = _LIVE_CHART_TIMEFRAMES.get(tf_label)
            if interval is None:
                # Native — strategy-interval bars with trades
                return self._live_runner.get_bars(), result, True
            # Re-aggregated bars — no trade markers
            return self._live_runner.get_bars_at_interval(interval), result, False
        return self._last_bars, self._last_result, True

    def _run_chart(self, bars, trades, title, focus, kwargs):
        try:
            _log(f"[CHART] Opening: {len(bars)} bars, {len(trades)} trades, focus={focus}")
            if bars:
                b = bars[0]
                _log(f"[CHART] First bar: dt={b.dt} O={b.open} H={b.high} L={b.low} C={b.close} V={b.volume} interval={b.interval}")
                b = bars[-1]
                _log(f"[CHART] Last bar:  dt={b.dt} O={b.open} H={b.high} L={b.low} C={b.close} V={b.volume} interval={b.interval}")
            plot_backtest(bars, trades, title=title,
                          focus_trade_index=focus, **kwargs)
            _log("[CHART] Chart closed normally")
        except ImportError as e:
            self.root.after(0, lambda: self.status_var.set(str(e)))
            _log(f"圖表錯誤 Chart error: {e}")
        except Exception as e:
            self.root.after(0, lambda: self.status_var.set(f"圖表錯誤 Chart error: {e}"))
            _log(f"圖表錯誤 Chart error: {e}")
            import traceback
            _log(traceback.format_exc())

    def _show_live_chart(self):
        """Open a live-updating chart for the running bot."""
        # Close existing live chart if still open
        if self._live_chart and self._live_chart.is_alive:
            self._live_chart.close()
            self._live_chart = None

        bars = self._live_runner.get_bars()
        result = self._live_runner.get_result()
        trades = list(result.trades)
        strategy_name = self.strategy_var.get()
        kwargs = self._chart_kwargs()

        self._live_chart = LiveChart(
            initial_bars=bars,
            initial_trades=trades,
            title=f"{strategy_name} [Live]",
            bb_period=kwargs.get('bb_period', 20),
            bb_std=kwargs.get('bb_std', 2.0),
        )
        _log(f"[LIVE CHART] Opening: {len(bars)} bars, {len(trades)} trades")
        threading.Thread(
            target=self._run_live_chart, daemon=True,
        ).start()

    def _run_live_chart(self):
        """Run the live chart (blocking, in a daemon thread)."""
        try:
            self._live_chart.run()
            _log("[LIVE CHART] Chart closed normally")
        except Exception as e:
            _log(f"[LIVE CHART] Error: {e}")
            _log(traceback.format_exc())
        finally:
            self.root.after(0, self._on_live_chart_closed)

    def _on_live_chart_closed(self):
        """Cleanup when live chart window is closed."""
        self._live_chart = None

    def _do_export(self):
        result = self._live_runner.get_result() if self._live_runner and self._live_runner.state != LiveState.IDLE else self._last_result
        if not result or not result.trades:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"backtest_trades_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
        if path:
            export_trades_csv(result.trades, path)
            self.status_var.set(f"已匯出 Exported: {path}")
            _log(f"匯出交易 Exported trades to {path}")

    # ══════════════════════════════════════════════════════════════
    #  LIVE TRADING METHODS
    # ══════════════════════════════════════════════════════════════

    def _toggle_live(self):
        """Toggle between Deploy Bot and Stop Bot."""
        if self._live_runner and self._live_runner.state == LiveState.RUNNING:
            self._stop_live()
        else:
            self._deploy_live()

    def _deploy_live(self):
        """Start live bot: create runner, fetch warmup, subscribe to ticks."""
        if not self._quote_connected and not _tv_available:
            self.status_var.set("請先登入 Please login first")
            self.login_status_var.set("請先登入 Login required")
            return

        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            self.status_var.set("請選擇策略 Select a strategy")
            return

        symbol = self.symbol_var.get().strip()
        if not symbol:
            return

        try:
            point_value = int(self.pv_var.get())
        except ValueError:
            point_value = 200

        # Instantiate strategy
        strategy_name = self.strategy_var.get()
        if strategy_name.startswith("AI:"):
            strategy = strategy_cls()
        else:
            try:
                bb_period = int(self.bb_period_var.get())
                bb_std = float(self.bb_std_var.get())
                atr_period = int(self.atr_period_var.get())
                sl_mult = float(self.sl_mult_var.get())
                tp_mult = float(self.tp_mult_var.get())
                sl_offset = int(self.sl_offset_var.get())
                tp_offset = int(self.tp_offset_var.get())
            except ValueError:
                bb_period, bb_std = 20, 2.0
                atr_period, sl_mult, tp_mult = 14, 1.0, 0.5
                sl_offset, tp_offset = 20, 50

            from src.strategy.examples.h4_bollinger_atr_long import H4BollingerAtrLongStrategy
            from src.strategy.examples.m1_bollinger_atr_long import M1BollingerAtrLongStrategy
            if strategy_cls in (H4BollingerAtrLongStrategy, M1BollingerAtrLongStrategy):
                strategy = strategy_cls(
                    bb_period=bb_period, bb_std=bb_std,
                    atr_period=atr_period, sl_mult=sl_mult, tp_mult=tp_mult,
                )
            else:
                try:
                    strategy = strategy_cls(
                        bb_period=bb_period, bb_std=bb_std,
                        sl_offset=sl_offset, tp_offset=tp_offset,
                    )
                except TypeError:
                    strategy = strategy_cls()

        log_dir = os.path.join(project_root, "data", "live")
        self._live_runner = LiveRunner(
            strategy, symbol, point_value=point_value, log_dir=log_dir,
        )

        # Register callbacks
        self._live_runner.on("on_bar", lambda b: self.root.after(0, self._on_live_bar, b))
        self._live_runner.on("on_decision", lambda d: self.root.after(0, self._on_live_decision, d))
        self._live_runner.on("on_status", lambda s: self.root.after(0, self._live_log_msg, s, "status"))

        # Set data source for live mode
        self._data_source = "即時交易 Live (tick)"

        # Disable controls while live bot is running
        self.btn_api.config(state=tk.DISABLED)
        self.btn_tv.config(state=tk.DISABLED)
        self.btn_deploy.config(text="停止機器人 Stop Bot")
        self.symbol_combo.config(state=tk.DISABLED)
        self.strategy_combo.config(state=tk.DISABLED)
        self.chart_tf_combo.config(state="readonly")
        self.chart_tf_var.set("Native")

        self._live_log_msg(f"部署中 Deploying: {strategy.name} on {symbol}", "status")
        _log(f"部署即時機器人 Deploying live bot: {strategy.name} on {symbol}")

        # Start warmup
        self._start_live_warmup()

    def _start_live_warmup(self):
        """Fetch historical bars at strategy's native timeframe for warmup."""
        runner = self._live_runner
        if not runner:
            return

        params = runner.get_warmup_params()
        kt = params["kline_type"]
        km = params["kline_minute"]
        days = params["days_back"]

        symbol = runner.symbol
        self._live_log_msg(f"暖機中 Warming up: fetching {days} days of data...", "status")

        # Always fetch fresh data for live warmup
        if _com_available and self._quote_connected:
            self._live_warmup_via_com(kt, km, days)
        elif _tv_available:
            self._live_warmup_via_tv(kt, km)
        else:
            self._live_log_msg("無資料來源 No data source for warmup", "status")
            self._stop_live()

    def _live_warmup_via_com(self, kline_type, kline_minute, days):
        """Fetch warmup data via COM API."""
        self._live_warmup_mode = True
        self._live_warmup_data = []

        dt_end = _taipei_now()
        dt_start = dt_end - timedelta(days=days)
        start_str = dt_start.strftime("%Y%m%d")
        end_str = dt_end.strftime("%Y%m%d")

        symbol = self._live_runner.symbol
        _log(f"COM暖機查詢 COM warmup fetch: {symbol} type={kline_type} "
             f"min={kline_minute} {start_str}~{end_str}")

        try:
            code = skQ.SKQuoteLib_RequestKLineAMByDate(
                symbol, kline_type, 1, 0, start_str, end_str, kline_minute)
            if code != 0 and code >= 3000:
                msg = skC.SKCenterLib_GetReturnCodeMessage(code)
                _log(f"暖機查詢失敗 Warmup fetch failed: {msg}")
                self._live_warmup_mode = False
                self._stop_live()
        except Exception as e:
            _log(f"暖機錯誤 Warmup error: {e}")
            self._live_warmup_mode = False
            self._stop_live()

    def _live_warmup_via_tv(self, kline_type, kline_minute):
        """Fetch warmup data via TradingView (in thread)."""
        runner = self._live_runner
        tv_interval_name = _TV_INTERVALS.get((kline_type, kline_minute))
        if not tv_interval_name:
            self._live_log_msg(f"TV不支援此週期 Unsupported interval for TV", "status")
            self._stop_live()
            return

        cfg = _SYMBOL_CONFIG.get(runner.symbol)
        tv_symbol = cfg["tv"] if cfg else runner.symbol
        interval_sec = INTERVAL_SECONDS.get((kline_type, kline_minute), 14400)

        self._live_log_msg(f"TradingView暖機 TV warmup: {tv_symbol}...", "status")

        def _worker():
            try:
                tv_interval = getattr(TvInterval, tv_interval_name)
                tv = TvDatafeed()
                df = tv.get_hist(symbol=tv_symbol, exchange="TAIFEX",
                                 interval=tv_interval, n_bars=5000)
                if df is None or df.empty:
                    self.root.after(0, lambda: self._live_log_msg("TV無資料 No TV data", "status"))
                    self.root.after(0, self._stop_live)
                    return

                kline_strings = []
                for dt_idx, row in df.iterrows():
                    dt = dt_idx.to_pydatetime()
                    kline_strings.append(
                        f"{dt.strftime('%m/%d/%Y %H:%M')},"
                        f"{round(row['open'])},{round(row['high'])},"
                        f"{round(row['low'])},{round(row['close'])},"
                        f"{int(row.get('volume', 0))}"
                    )

                def _finish():
                    count = runner.feed_warmup_bars(kline_strings)
                    self._live_log_msg(f"TV暖機完成 TV warmup done: {count} bars", "status")
                    self._update_live_status()
                    self._start_live_tick_subscription()

                self.root.after(0, _finish)
            except Exception as e:
                self.root.after(0, lambda: self._live_log_msg(f"TV暖機錯誤: {e}", "status"))
                self.root.after(0, self._stop_live)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_live_warmup_complete(self):
        """Called when COM warmup KLine data is complete."""
        self._live_warmup_mode = False

        if not self._live_runner:
            return

        data = list(self._live_warmup_data)
        self._live_warmup_data = []

        count = self._live_runner.feed_warmup_bars(data)
        self._live_log_msg(f"COM暖機完成 COM warmup done: {count} bars", "status")
        self._update_live_status()
        self._start_live_tick_subscription()

    # ── Tick-based live data feed ──

    def _start_live_tick_subscription(self):
        """Subscribe to real-time ticks via COM and build 1-min bars."""
        if not self._live_runner:
            return

        symbol = self._live_runner.symbol
        self._live_tick_symbol = symbol
        self._live_bar_builder = BarBuilder(symbol, interval=60)

        if not _com_available or not self._quote_connected:
            self._live_log_msg("未連線 Not connected, cannot subscribe to ticks", "status")
            self._stop_live()
            return

        # Suppress strategy during history tick catchup — no trades on old data
        self._live_runner.suppress_strategy = True

        self._live_log_msg(f"訂閱即時報價 Subscribing to ticks: {symbol}...", "status")
        try:
            result = skQ.SKQuoteLib_RequestTicks(0, symbol)
            # COM may return (code, stockIdx) tuple or just an int
            if isinstance(result, (list, tuple)):
                code = result[0]
            else:
                code = result
            if code != 0 and code >= 3000:
                msg = skC.SKCenterLib_GetReturnCodeMessage(code)
                self._live_log_msg(f"訂閱失敗 Tick subscribe failed: {msg}", "status")
                self._stop_live()
                return
            self._live_tick_active = True
            self._live_log_msg(f"已訂閱 Tick subscription active for {symbol}", "status")
            _log(f"即時報價訂閱成功 Tick subscription OK: {symbol}, result={result}")
        except Exception as e:
            _log(f"報價訂閱錯誤 Tick subscribe error: {e}")
            self._live_log_msg(f"訂閱錯誤 Subscribe error: {e}", "status")
            self._stop_live()

        # Start draining tick queue on main thread
        self._drain_tick_queue()
        # Schedule periodic status updates (every 30s)
        self._schedule_status_update()

    def _schedule_status_update(self):
        """Periodically update the live status panel."""
        if not self._live_runner or self._live_runner.state != LiveState.RUNNING:
            return
        self._update_live_status()
        self._live_poll_id = self.root.after(30000, self._schedule_status_update)

    def _drain_tick_queue(self):
        """Drain pending ticks from _tick_queue on the main thread.

        COM tick callbacks put raw tuples into _tick_queue from the COM thread.
        This method is scheduled via root.after() and processes up to 500 ticks
        per frame to keep the UI responsive, then reschedules itself.
        """
        if not self._live_tick_active:
            # Drain any remaining ticks then stop polling
            while not _tick_queue.empty():
                try:
                    _tick_queue.get_nowait()
                except queue.Empty:
                    break
            return
        count = 0
        while count < 500:
            try:
                tick_data = _tick_queue.get_nowait()
            except queue.Empty:
                break
            self._on_com_tick(*tick_data)
            count += 1
        # Reschedule: 10ms if ticks still pending, 50ms otherwise
        delay = 10 if not _tick_queue.empty() else 50
        self.root.after(delay, self._drain_tick_queue)

    def _on_com_tick(self, date: int, time_hms: int, time_millismicros: int,
                     bid: int, ask: int, close: int, qty: int, simulate: int,
                     is_history: bool = False):
        """Process a COM tick callback — runs on Tkinter main thread.

        Converts raw COM tick data to Tick, feeds to BarBuilder.
        When a 1-min bar completes, routes to LiveRunner.
        """
        if not self._live_runner or self._live_runner.state != LiveState.RUNNING:
            return
        if not self._live_bar_builder:
            return

        self._live_tick_count += 1

        # Detect transition from history to live ticks
        if not is_history and not self._live_history_done:
            self._live_history_done = True
            self._live_history_tick_count = self._live_tick_count
            # Enable strategy execution now that we're on live data
            self._live_runner.suppress_strategy = False
            status = self._live_runner.get_status()
            self._live_log_msg(
                f"歷史報價完成 History ticks done: {self._live_tick_count} ticks, "
                f"{status['bars_1m']} 1m bars built. Now receiving live ticks.",
                "status",
            )
            self._update_live_status()
            self._update_live_results()

        # Convert SK date/time integers to datetime (strip tz — bars are tz-naive)
        try:
            dt = combine_sk_datetime(date, time_hms, time_millismicros)
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
        except Exception:
            return

        # Skip ticks during closed market (settlement/reference data at 14:50 etc.)
        if not is_market_open(dt):
            return

        # Scale tick prices to match KLine convention (COM ticks are 100x KLine)
        cfg = _SYMBOL_CONFIG.get(self._live_tick_symbol, {})
        divisor = cfg.get("tick_divisor", 1)
        price = close // divisor
        bid_scaled = bid // divisor
        ask_scaled = ask // divisor

        # Log first few live ticks to verify timestamp/price convention
        if self._live_history_done and self._live_tick_count <= self._live_history_tick_count + 5:
            _log(f"[DEBUG TICK] raw: date={date} time={time_hms} ms={time_millismicros} "
                 f"-> dt={dt} price={close} scaled={price} qty={qty}")

        tick = Tick(
            symbol=self._live_tick_symbol,
            dt=dt,
            price=price,
            qty=qty,
            bid=bid_scaled,
            ask=ask_scaled,
            simulate=bool(simulate),
        )

        # Feed tick to BarBuilder — returns completed 1-min bar on boundary cross
        completed_1m = self._live_bar_builder.on_tick(tick)

        if completed_1m is not None:
            # Feed completed 1-min bar to LiveRunner
            agg_bar = self._live_runner.feed_1m_bar(completed_1m)

            # Only log live 1m bars (not the flood of historical catchup)
            if self._live_history_done:
                self._live_log_msg(
                    f"1分K 1m: {completed_1m.dt.strftime('%H:%M')} "
                    f"O={completed_1m.open} C={completed_1m.close} V={completed_1m.volume}",
                    "status",
                )

            # Push updates to live chart
            if self._live_history_done and self._live_chart and self._live_chart.is_alive:
                if agg_bar is not None:
                    self._live_chart.push_bar(agg_bar)
                else:
                    partial = self._live_runner.get_partial_bar()
                    if partial:
                        self._live_chart.push_partial(partial)

            if agg_bar is not None and self._live_history_done:
                self._live_log_msg(
                    f"聚合K棒 Aggregated bar: {agg_bar.dt.strftime('%H:%M')} "
                    f"O={agg_bar.open} H={agg_bar.high} "
                    f"L={agg_bar.low} C={agg_bar.close}",
                    "bar",
                )
                self._update_live_results()
                self._update_live_status()

    # ── Live UI updates ──

    def _on_live_bar(self, bar):
        """Callback when an aggregated bar is processed."""
        pass  # Status update handled in _on_live_poll_complete

    def _on_live_decision(self, decision):
        """Callback when a trading decision is made."""
        action = decision["action"]
        tag_map = {
            "ENTRY": "entry", "ENTRY_FILL": "entry",
            "EXIT_ORDER": "exit", "CLOSE": "exit",
            "TRADE_CLOSE": "exit", "FORCE_CLOSE": "exit",
        }
        tag = tag_map.get(action, "status")

        msg = (f"{decision['action']} {decision['side']} "
               f"tag={decision['tag']} price={decision['price']:,} "
               f"({decision['reason']})")
        self._live_log_msg(msg, tag)

        # Push trade marker to live chart on trade close
        if action == "TRADE_CLOSE" and self._live_chart and self._live_chart.is_alive:
            if self._live_runner and self._live_runner.broker.trades:
                trades = self._live_runner.broker.trades
                self._live_chart.push_trade(trades[-1], len(trades) - 1)

    def _live_log_msg(self, msg, tag="status"):
        """Append message to the Live tab log."""
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.live_log.insert(tk.END, line, tag)
        self.live_log.see(tk.END)

    def _update_live_status(self):
        """Update the Live tab status panel."""
        if not self._live_runner:
            return
        status = self._live_runner.get_status()
        self.live_state_var.set(status["state"])
        self.live_pos_var.set(status["position"])
        self.live_pnl_var.set(f"{status['pnl']:+,}")
        self.live_bars_var.set(f"{status['bars_1m']} 即時 / {status['bars_agg']} 總計")
        self.live_market_var.set("開盤 Open" if status["market_open"] else "休市 Closed")

    def _update_live_results(self):
        """Update results tabs with live data for chart/trades."""
        if not self._live_runner:
            return
        result = self._live_runner.get_result()
        bars = self._live_runner.get_bars()

        # Update result for chart buttons
        self._last_result = result
        self._last_bars = bars

        # Enable chart/export buttons
        if _LWC_AVAILABLE and bars:
            self.btn_chart.config(state=tk.NORMAL)
            self.btn_chart_all.config(state=tk.NORMAL)
        if result.trades:
            self.btn_export.config(state=tk.NORMAL)

        # Update trade list and metrics
        self._display_results(result, bars)

    def _stop_live(self):
        """Stop the live bot and restore UI."""
        if self._live_poll_id:
            self.root.after_cancel(self._live_poll_id)
            self._live_poll_id = None

        self._live_warmup_mode = False
        self._live_polling = False
        self._live_tick_active = False
        self._live_bar_builder = None
        self._live_tick_symbol = ""
        self._live_history_done = False
        self._live_tick_count = 0
        self._live_history_tick_count = 0

        # Close live chart before stopping runner
        if self._live_chart and self._live_chart.is_alive:
            self._live_chart.close()
        self._live_chart = None

        if self._live_runner:
            summary = self._live_runner.stop()
            self._live_log_msg(
                f"已停止 Stopped: {summary['trades']} trades, "
                f"P&L={summary['pnl']:+,}, "
                f"bars={summary['bars_1m']}(1m)/{summary['bars_agg']}(agg)",
                "status",
            )
            _log(f"即時機器人已停止 Live bot stopped: {summary}")

            # Update final results then clear runner so subsequent backtests
            # use _last_bars/_last_result instead of stale live data.
            self._update_live_results()
            self._update_live_status()
            self._live_runner = None

        # Restore UI
        self.btn_deploy.config(text="部署機器人 Deploy Bot")
        self.chart_tf_combo.config(state=tk.DISABLED)
        self.chart_tf_var.set("Native")
        if self._quote_connected:
            self.btn_api.config(state=tk.NORMAL)
            self.btn_deploy.config(state=tk.NORMAL)
        self.btn_tv.config(state=tk.NORMAL)
        self.symbol_combo.config(state="readonly")
        self.strategy_combo.config(state="readonly")
        self.status_var.set("就緒 Ready")


def main():
    _init_com()

    if _com_available:
        import comtypes.client
        SKQuoteEvent = SKQuoteLibEvents()
        SKQuoteLibEventHandler = comtypes.client.GetEvents(skQ, SKQuoteEvent)
        SKReplyEvent = SKReplyLibEvent()
        SKReplyLibEventHandler = comtypes.client.GetEvents(skR, SKReplyEvent)

    root = tk.Tk()
    app = BacktestApp(root)
    root.mainloop()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
