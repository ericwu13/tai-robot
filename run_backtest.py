"""AI Strategy Workbench: Chat with Claude to generate, backtest, and export strategies.

Two backtest buttons:
  - API Backtest — fetches from Capital API (logs in on first use)
  - TV Backtest  — local CSV first, then TradingView download as fallback

Multi-symbol support via _SYMBOL_CONFIG (TX00, MTX00).

Usage:
  python run_backtest.py
"""

from version import APP_VERSION

import os
import sys
import inspect
import queue
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, simpledialog, messagebox
from datetime import datetime, time as dt_time, timedelta
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
from src.ai.prompts import STRATEGY_SYSTEM_PROMPT, STRATEGY_CODE_CONTEXT, CODE_GEN_SYSTEM_PROMPT, CHAT_RECAP_PROMPT
from src.ai.code_sandbox import (
    extract_python_code, load_strategy_from_source,
    CodeValidationError, CodeExecutionError,
)
from src.ai.strategy_store import StrategyStore
from src.ai.pine_exporter import export_to_pine

# Code generation uses higher token limit to avoid truncation (issue #7)
_CODE_GEN_MAX_TOKENS = 65536

# Live trading modules
from src.live.live_runner import LiveRunner, LiveState, is_market_open, seconds_until_market_open, minutes_until_session_close, _taipei_now, _TZ_TAIPEI
from src.live.trading_guard import TradingGuard
from src.live.tick_watchdog import TickWatchdog
from src.live.session_store import load_session, session_summary

# TAIFEX public data (no API key needed)
from src.data_sources.taifex import fetch_futures_daily, parse_taifex_csv
from src.data_sources.cache import (
    get_cache_path, save_bars_csv, load_bars_csv, cache_covers_range,
)

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
skC = skQ = skR = skO = None

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
    global _com_available, skC, skQ, skR, skO
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
        skO = comtypes.client.CreateObject(sk.SKOrderLib, interface=sk.ISKOrderLib)

        # ── Phase 3: Restore default DLL search order ──
        # None = default Windows search (app dir → System32 → Windows → PATH).
        # This lets SKCOM's internal network calls find WinHTTP, Schannel, etc.
        kernel32.SetDllDirectoryW(None)

        _com_available = True
    except Exception as e:
        print(f"COM not available: {e}")
        traceback.print_exc()
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
    "TX00": {"prefix": "TXF1", "tv": "TXF1!", "pv": 200, "tick_divisor": 100,
             "taifex_id": "TX", "order_symbol": "TXFD0", "init_margin": 322000},
    "MTX00": {"prefix": "TMF1", "tv": "TMF1!", "pv": 50, "tick_divisor": 100,
              "taifex_id": "MTX", "order_symbol": "MTXFD0",
              "kline_symbol": "TX00", "tick_symbol": "TX00", "init_margin": 80500},
    "TMF00": {"prefix": "IMF1", "tv": "IMF1!", "pv": 10, "tick_divisor": 100,
              "taifex_id": "TMF", "order_symbol": "TM0000",
              "kline_symbol": "TX00", "tick_symbol": "TX00", "init_margin": 16100},
}

def _resolve_order_symbol(symbol: str) -> str:
    """Resolve the order symbol for a given COM quote symbol."""
    cfg = _SYMBOL_CONFIG.get(symbol, {})
    order_sym = cfg.get("order_symbol", symbol)
    if order_sym == "auto":
        product_code = cfg.get("taifex_id", symbol)
        order_sym = _get_near_month_symbol(product_code)
    return order_sym


_MONTH_CODES = "ABCDEFGHIJKL"  # A=Jan .. L=Dec


def _get_near_month_symbol(product_code: str) -> str:
    """Compute near-month futures order symbol like TMFC6.

    Format: {product}{month_letter}{year_digit}
    Month letters: A=Jan, B=Feb, C=Mar, D=Apr, ... L=Dec
    Year digit: last digit of year (6=2026)

    Taiwan futures settle on the 3rd Wednesday of the expiry month.
    If today is past the 3rd Wednesday, use next month.
    """
    import calendar
    now = _taipei_now()
    year, month = now.year, now.month
    # Find 3rd Wednesday of current month
    cal = calendar.monthcalendar(year, month)
    wed_count = 0
    third_wed_day = None
    for week in cal:
        if week[2] != 0:  # Wednesday exists in this week
            wed_count += 1
            if wed_count == 3:
                third_wed_day = week[2]
                break
    # If past settlement day, roll to next month
    if now.day > third_wed_day:
        month += 1
        if month > 12:
            month = 1
            year += 1
    month_letter = _MONTH_CODES[month - 1]
    year_digit = year % 10
    return f"{product_code}{month_letter}{year_digit}"


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
# Thread-safe queue for COM connection/UI events → main thread.
# OnConnection fires on a true COM background thread (unlike KLine callbacks
# which fire synchronously on the main thread). Neither Tkinter calls NOR
# root.after() are safe from COM threads — only queue.put_nowait() is safe.
_ui_queue: queue.Queue = queue.Queue()


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


def _fmt_money(val: str) -> str:
    """Format a numeric string with comma separators."""
    try:
        n = float(val)
        if n == int(n):
            return f"{int(n):,}"
        return f"{n:,.2f}"
    except (ValueError, TypeError):
        return val


def _log(msg):
    tpe = _taipei_now()
    local = datetime.now()
    ts_tpe = tpe.strftime("%H:%M:%S")
    ts_local = local.strftime("%H:%M:%S")
    if ts_tpe == ts_local:
        line = f"[{ts_tpe}] {msg}"
    else:
        line = f"[{ts_tpe} TPE / {ts_local} local] {msg}"
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
    def OnNotifyStockList(self, sMarketNo, bstrStockData):
        _ui_queue.put_nowait(("stock_list", (sMarketNo, bstrStockData)))

    def OnConnection(self, nKind, nCode):
        kind_names = {3001: "Reply", 3002: "Quote", 3003: "Ready",
                      3021: "ConnError", 3033: "Abnormal"}
        # Only queue.put_nowait() is safe from COM background threads.
        # root.after() is NOT safe — it calls into Tcl/Tk C code which
        # is not thread-safe and corrupts the GIL in Python 3.13.
        _ui_queue.put_nowait(("log", f"報價連線 QUOTE CONN: {kind_names.get(nKind, nKind)} code={nCode}"))
        if nKind == 3003 and nCode == 0:
            _ui_queue.put_nowait(("conn", "ready"))
        elif nKind == 3002 and nCode == 0:
            _ui_queue.put_nowait(("conn", "quote"))
        elif nKind in (3021, 3033):
            _ui_queue.put_nowait(("conn", "disconnected"))

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
        _ui_queue.put_nowait(("log", f"回報 REPLY: {bstrMessage}"))
        return -1

    def OnConnect(self, bstrUserID, nErrorCode):
        _ui_queue.put_nowait(("log", f"回報連線 REPLY CONN: code={nErrorCode}"))

    def OnComplete(self, bstrUserID):
        pass

    def OnNewData(self, bstrUserID, bstrData):
        pass


class SKOrderLibEvents:
    def OnAccount(self, bstrLogInID, bstrAccountData):
        _ui_queue.put_nowait(("account", bstrAccountData))

    def OnAsyncOrder(self, nThreadID, nCode, bstrMessage):
        _ui_queue.put_nowait(("order_result", (nCode, bstrMessage)))

    def OnOpenInterest(self, bstrData):
        _ui_queue.put_nowait(("open_interest", bstrData))

    def OnFutureRights(self, bstrData):
        _ui_queue.put_nowait(("future_rights", bstrData))


def _parse_open_interest(bstr: str) -> dict | None:
    """Parse OnOpenInterest callback data into a dict.

    Returns None for error/empty codes (001, 970).
    Fields: market, account, product, side(B/S), qty, daytrade_qty, avg_cost,
            fee, tax_rate, login_id
    """
    vals = bstr.split(",")
    if len(vals) < 10 or vals[0] in ("001", "970", "980"):
        return None
    try:
        return {
            "product": vals[2].strip(),
            "side": vals[3].strip(),        # B=long, S=short
            "qty": int(vals[4].strip() or 0),
            "daytrade_qty": int(vals[5].strip() or 0),
            "avg_cost": vals[6].strip(),
            "fee": vals[7].strip(),
            "tax_rate": vals[8].strip(),
        }
    except (ValueError, IndexError):
        return None


def _parse_future_rights(bstr: str) -> dict | None:
    """Parse OnFutureRights callback data into a dict.

    Returns None for sentinel/error codes (##, 970, 980).
    Key fields: balance, float_pnl, equity, excess_margin, unrealized,
                orig_margin, maint_margin, maint_rate, currency, available
    """
    vals = bstr.split(",")
    if len(vals) < 35 or vals[0].strip() in ("##", "970", "980"):
        return None
    try:
        return {
            "balance": vals[0].strip(),         # 帳戶餘額
            "float_pnl": vals[1].strip(),       # 浮動損益
            "realized_cost": vals[2].strip(),   # 已實現費用
            "tax": vals[3].strip(),             # 交易稅
            "equity": vals[6].strip(),          # 權益數
            "excess_margin": vals[7].strip(),   # 超額保證金
            "realized_pnl": vals[11].strip(),   # 期貨平倉損益
            "unrealized": vals[12].strip(),     # 盤中未實現
            "orig_margin": vals[13].strip(),    # 原始保證金
            "maint_margin": vals[14].strip(),   # 維持保證金
            "maint_rate": vals[24].strip(),     # 維持率
            "currency": vals[25].strip(),       # 幣別
            "available": vals[31].strip(),      # 可用餘額
            "risk": vals[34].strip(),           # 風險指標
        }
    except (ValueError, IndexError):
        return None


class BacktestApp:
    def __init__(self, root: tk.Tk):
        global _app
        _app = self

        self.root = root
        self.root.title(f"tai-robot AI 策略工作台 AI Strategy Workbench v{APP_VERSION}")
        self.root.geometry("1400x850")
        self.root.minsize(1100, 650)

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
        # Chat auto-load removed — user must explicitly use "Load Chat"

        self.status_var.set("就緒 Ready")

        # Auto-save chat on window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        # Start draining COM UI events on the main thread
        self._drain_ui_queue()

    # ══════════════════════════════════════════════════════════════
    #  COM → UI QUEUE DRAIN (main thread)
    # ══════════════════════════════════════════════════════════════

    def _drain_ui_queue(self):
        """Drain COM connection/UI events on the main thread.

        COM callbacks (OnConnection, OnReplyMessage, etc.) fire on background
        threads. Neither Tkinter calls NOR root.after() are safe from those
        threads. The only safe primitive is queue.put_nowait(). This method
        runs on the main thread via root.after() and processes queued events.
        """
        try:
            while True:
                kind, data = _ui_queue.get_nowait()
                if kind == "log":
                    _log(data)
                elif kind == "conn":
                    if data == "ready":
                        self._quote_connected = True
                        self.btn_api.config(state=tk.NORMAL)
                        self.btn_deploy.config(state=tk.NORMAL)
                        self.btn_login.config(state=tk.DISABLED)
                        self.btn_reconnect.config(state=tk.NORMAL)
                        self.status_var.set("已連線 Connected - Ready")
                        self.login_status_var.set("已連線 Connected")
                        self._on_reconnected()
                    elif data == "quote" and not self._quote_connected:
                        self._quote_connected = True
                        self.btn_api.config(state=tk.NORMAL)
                        self.btn_deploy.config(state=tk.NORMAL)
                        self.btn_login.config(state=tk.DISABLED)
                        self.btn_reconnect.config(state=tk.NORMAL)
                        self.status_var.set("已連線 Connected (Quote) - Ready")
                        self.login_status_var.set("已連線 Connected")
                        self._on_reconnected()
                    elif data == "disconnected":
                        self._on_disconnected()
                elif kind == "account":
                    # Parse account data: "TF,branch,,account,..." format
                    parts = data.split(",") if isinstance(data, str) else []
                    if len(parts) >= 4 and parts[0] == "TF":
                        acct = parts[1] + parts[3]
                        self._futures_account = acct
                        _log(f"期貨帳號 Futures account: {acct}")
                elif kind == "stock_list":
                    market_no, stock_data = data
                    if market_no in (2, 7, 9):  # futures markets
                        raw = stock_data or ""
                        if self._live_runner:
                            cfg = _SYMBOL_CONFIG.get(self._live_runner.symbol, {})
                            pid = cfg.get("taifex_id", "")
                            if pid and pid in raw:
                                # Log raw entries containing the product ID
                                entries = [e.strip() for e in raw.split(";") if e.strip()]
                                matches = [e for e in entries if pid in e]
                                if matches:
                                    _log(f"期貨商品(mkt={market_no}) '{pid}': {matches[:20]}")
                        else:
                            entries = [e.strip() for e in raw.split(";") if e.strip()]
                            _log(f"期貨商品列表(mkt={market_no}): {len(entries)} symbols")
                elif kind == "open_interest":
                    parsed = _parse_open_interest(data)
                    if parsed:
                        # Accumulate positions (callback fires once per position)
                        self._real_positions.append(parsed)
                        self._update_real_account_display()
                    else:
                        first = data.split(",")[0].strip() if data else ""
                        if first == "001":
                            # No open positions
                            self._real_positions.clear()
                            self.real_pos_var.set("Flat (無持倉)")
                        # Ignore ## sentinel and error codes
                    _log(f"未平倉 OpenInterest: {data}")
                elif kind == "future_rights":
                    parsed = _parse_future_rights(data)
                    if parsed:
                        self._real_rights = parsed
                        try:
                            self._update_real_account_display()
                        except Exception as e:
                            _log(f"帳戶顯示錯誤 Account display error: {e}")
                        _log(f"帳戶資料 Account: equity={parsed.get('equity')} "
                             f"available={parsed.get('available')} "
                             f"float_pnl={parsed.get('float_pnl')}")
                    _log(f"權益數 FutureRights: {data}")
                elif kind == "order_result":
                    code, msg = data
                    if code == 0:
                        _log(f"委託回報 Order OK: {msg}")
                        if self._live_runner:
                            self._live_log_msg(f"實單成功 Order OK: {msg}", "entry")
                    else:
                        err = skC.SKCenterLib_GetReturnCodeMessage(code) if skC else str(code)
                        _log(f"委託失敗 Order FAILED: code={code} {err} {msg}")
                        if self._live_runner:
                            self._live_log_msg(f"實單失敗 Order FAILED: {err}", "exit")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui_queue)

    # ══════════════════════════════════════════════════════════════
    #  CONNECTION MONITORING & RECONNECTION
    # ══════════════════════════════════════════════════════════════

    _RECONNECT_DELAYS = [5, 10, 20, 30, 60]  # seconds, escalating backoff
    _MAX_RECONNECT_ATTEMPTS = 10

    def _on_disconnected(self):
        """Handle connection loss: pause live feed, start auto-reconnect."""
        if not self._quote_connected:
            return  # already handling disconnect
        self._quote_connected = False
        self.status_var.set("斷線 Disconnected")
        self.login_status_var.set("斷線 Disconnected")
        self.btn_login.config(state=tk.NORMAL)
        self.btn_reconnect.config(state=tk.NORMAL)
        _log("斷線 Connection lost")

        # Pause live tick feed (keep runner state intact)
        if self._live_runner and self._live_tick_active:
            self._live_tick_active = False
            self._live_log_msg("斷線 Connection lost — pausing tick feed", "status")

        # Start auto-reconnect
        self._reconnect_attempt = 0
        self._schedule_reconnect()

    def _manual_reconnect(self):
        """Manual reconnect triggered by user button click."""
        # Cancel any pending auto-reconnect timer
        if self._reconnect_timer_id:
            self.root.after_cancel(self._reconnect_timer_id)
            self._reconnect_timer_id = None

        self._quote_connected = False
        self.btn_reconnect.config(state=tk.DISABLED)
        self.status_var.set("手動重連中 Manual reconnecting...")
        self.login_status_var.set("重連中 Reconnecting...")
        _log("手動重連 Manual reconnect triggered")

        # Reset attempt counter and try immediately
        self._reconnect_attempt = 0
        self._attempt_reconnect()

    def _schedule_reconnect(self):
        """Schedule the next reconnection attempt with exponential backoff.

        During off-market hours, defers reconnection until ~2 min before
        the next session opens to avoid wasting attempts on idle connections
        that the server will drop.
        """
        if self._reconnect_attempt >= self._MAX_RECONNECT_ATTEMPTS:
            # Check if we should defer to next market open instead of giving up
            secs = seconds_until_market_open()
            if secs > 0 and self._live_runner:
                # Market closed — schedule one final attempt near market open
                defer_secs = max(secs - 120, 60)  # 2 min before open, min 1 min
                defer_mins = defer_secs // 60
                self._reconnect_attempt = 0  # reset counter for fresh cycle
                msg = (f"休市中 Market closed — reconnecting in ~{defer_mins}m "
                       f"(before next session)")
                self.btn_reconnect.config(state=tk.NORMAL)
                self.status_var.set(msg)
                _log(msg)
                self._live_log_msg(msg, "status")
                self._reconnect_timer_id = self.root.after(
                    defer_secs * 1000, self._attempt_reconnect)
                return

            msg = "自動重連失敗 Auto-reconnect failed — use Reconnect or Login button"
            self.btn_reconnect.config(state=tk.NORMAL)
            self.status_var.set(msg)
            _log(msg)
            if self._live_runner:
                self._live_log_msg(msg, "status")
            return

        # During off-market hours, don't burn attempts — defer to near market open
        if not is_market_open() and self._live_runner:
            secs = seconds_until_market_open()
            if secs > 300:  # more than 5 min until market open
                defer_secs = max(secs - 120, 60)
                defer_mins = defer_secs // 60
                msg = (f"休市中 Market closed — deferring reconnect ~{defer_mins}m "
                       f"(before next session)")
                self.status_var.set(msg)
                _log(msg)
                self._live_log_msg(msg, "status")
                self._reconnect_timer_id = self.root.after(
                    defer_secs * 1000, self._attempt_reconnect)
                return

        idx = min(self._reconnect_attempt, len(self._RECONNECT_DELAYS) - 1)
        delay = self._RECONNECT_DELAYS[idx]
        self._reconnect_attempt += 1

        msg = f"重連中 Reconnecting in {delay}s (attempt {self._reconnect_attempt}/{self._MAX_RECONNECT_ATTEMPTS})..."
        self.status_var.set(msg)
        _log(msg)
        if self._live_runner:
            self._live_log_msg(msg, "status")

        self._reconnect_timer_id = self.root.after(delay * 1000, self._attempt_reconnect)

    def _attempt_reconnect(self):
        """Try to re-login and reconnect to quote service."""
        self._reconnect_timer_id = None
        if self._quote_connected:
            return  # already reconnected (e.g. by manual login)

        _log(f"嘗試重連 Attempting reconnect #{self._reconnect_attempt}")

        try:
            if not _com_available:
                self._schedule_reconnect()
                return

            # Re-login
            user_id = self.login_user_var.get().strip()
            password = self.login_pass_var.get().strip()
            if not user_id or not password:
                _log("重連失敗 Reconnect failed: no credentials")
                self._schedule_reconnect()
                return

            code = skC.SKCenterLib_LoginSetQuote(user_id, password, "Y")
            if code != 0 and code < 2000:
                msg = skC.SKCenterLib_GetReturnCodeMessage(code)
                _log(f"重連登入失敗 Reconnect login failed: {msg}")
                self._schedule_reconnect()
                return

            self._logged_in = True
            skR.SKReplyLib_ConnectByID(user_id)
            skQ.SKQuoteLib_EnterMonitorLONG()

            # Poll for connection (OnConnection callback will set _quote_connected)
            self.root.after(3000, self._check_reconnection)

        except Exception as e:
            _log(f"重連異常 Reconnect error: {e}")
            self._schedule_reconnect()

    def _check_reconnection(self):
        """Poll IsConnected after reconnect login attempt."""
        if self._quote_connected:
            return  # success, handled by _on_reconnected via _drain_ui_queue
        try:
            ic = skQ.SKQuoteLib_IsConnected()
            if ic == 1:
                self._quote_connected = True
                self.btn_api.config(state=tk.NORMAL)
                self.btn_deploy.config(state=tk.NORMAL)
                self.btn_login.config(state=tk.DISABLED)
                self.btn_reconnect.config(state=tk.NORMAL)
                self.status_var.set("已連線 Connected - Ready")
                self.login_status_var.set("已連線 Connected")
                self._on_reconnected()
                return
        except Exception:
            pass
        # Not connected yet — schedule next reconnect attempt
        self._schedule_reconnect()

    def _on_reconnected(self):
        """Handle successful reconnection: re-subscribe ticks if live bot is running."""
        self._reconnect_attempt = 0
        if self._reconnect_timer_id:
            self.root.after_cancel(self._reconnect_timer_id)
            self._reconnect_timer_id = None

        if self._live_runner and self._live_runner.state == LiveState.RUNNING:
            self._live_log_msg("已重連 Reconnected — resubscribing ticks", "status")
            _log("已重連 Reconnected — resubscribing ticks for live bot")
            self._resubscribe_ticks()

    _RESUBSCRIBE_MAX_RETRIES = 3
    _RESUBSCRIBE_RETRY_DELAY = 5000  # ms

    def _resubscribe_ticks(self, _retry: int = 0):
        """Re-subscribe to tick feed after reconnection."""
        if not self._live_runner:
            return
        # Use the stored COM tick symbol (e.g. TX00 for MTX00/TMF00)
        com_symbol = getattr(self, '_live_tick_com_symbol', self._live_runner.symbol)

        # Reset history tracking so the history→live transition fires again
        # and suppress_strategy gets re-enabled then cleared properly.
        self._live_history_done = False
        self._live_tick_count = 0
        self._live_history_tick_count = 0
        self._live_runner.suppress_strategy = True

        try:
            result = skQ.SKQuoteLib_RequestTicks(0, com_symbol)
            code = result[0] if isinstance(result, (list, tuple)) else result
            if code != 0 and code >= 3000:
                msg = skC.SKCenterLib_GetReturnCodeMessage(code)
                self._live_log_msg(f"重新訂閱失敗 Resubscribe failed: {msg}", "status")
                if _retry < self._RESUBSCRIBE_MAX_RETRIES:
                    self._live_log_msg(
                        f"重試訂閱 Retrying tick subscribe in {self._RESUBSCRIBE_RETRY_DELAY // 1000}s "
                        f"(attempt {_retry + 1}/{self._RESUBSCRIBE_MAX_RETRIES})...", "status")
                    next_retry = _retry + 1
                    self.root.after(self._RESUBSCRIBE_RETRY_DELAY,
                                   lambda r=next_retry: self._resubscribe_ticks(r))
                return

            self._live_tick_active = True
            self._tick_watchdog.on_tick()
            self._tick_watchdog.set_grace(30)
            self._live_log_msg(f"已重新訂閱 Tick resubscription active for {com_symbol}", "status")
            # Restart tick drain if not already running
            self._drain_tick_queue()
        except Exception as e:
            self._live_log_msg(f"重新訂閱異常 Resubscribe error: {e}", "status")

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
        # Real trading state
        self._trading_mode: str = "paper"  # "paper" or "semi_auto"
        self._futures_account: str = ""  # full account for order submission
        self._order_confirm_dlg = None  # active confirmation dialog
        self._order_confirm_timer_id = None  # countdown timer
        self._trading_guard = TradingGuard()  # safety checks for real orders
        # Reconnection state
        self._reconnect_attempt: int = 0
        self._reconnect_timer_id = None
        self._tick_watchdog = TickWatchdog()  # tick health monitoring
        self._live_poll_id = None  # root.after() id for cancellation
        self._live_warmup_mode: bool = False
        self._live_warmup_data: list[str] = []
        self._live_polling: bool = False
        self._live_poll_data: list[str] = []
        # Tick-based live data feed
        self._live_tick_active: bool = False
        self._live_bar_builder: BarBuilder | None = None
        self._live_tick_symbol: str = ""
        self._live_tick_com_symbol: str = ""
        self._live_last_tick_price: int = 0
        self._last_real_order_side: int | None = None  # 0=BUY, 1=SELL, None=unknown
        # Real position tracking and safety checks live in self._trading_guard
        # Real account data from API
        self._real_positions: list[dict] = []
        self._real_rights: dict = {}
        self._real_account_poll_id = None
        self._live_history_done: bool = False
        self._live_tick_count: int = 0
        self._live_history_tick_count: int = 0

    def _build_chat_panel(self, parent):
        # ── Header ──
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, padx=4, pady=(4, 2))

        ttk.Label(header, text="AI 策略工作台", font=("", 13, "bold")).pack(side=tk.LEFT)
        ttk.Button(header, text="New Chat", width=9, command=self._reset_chat).pack(side=tk.RIGHT, padx=2)
        ttk.Button(header, text="Load Chat", width=9, command=self._load_chat_session).pack(side=tk.RIGHT, padx=2)
        ttk.Button(header, text="Save Chat", width=9, command=self._save_chat_session).pack(side=tk.RIGHT, padx=2)
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
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill=tk.X, padx=4, pady=(4, 2))

        # ── Row 1: Symbol + Strategy ──
        row1 = ttk.Frame(ctrl)
        row1.pack(fill=tk.X, pady=(0, 1))

        ttk.Label(row1, text="商品 Symbol:").grid(row=0, column=0, sticky=tk.W, padx=(4, 2))
        self.symbol_var = tk.StringVar(value="TX00")
        self.symbol_combo = ttk.Combobox(row1, textvariable=self.symbol_var, width=8,
                                          state="readonly", values=list(_SYMBOL_CONFIG.keys()))
        self.symbol_combo.grid(row=0, column=1, padx=(0, 8))
        self.symbol_combo.bind("<<ComboboxSelected>>", lambda e: self._on_symbol_changed())

        ttk.Label(row1, text="策略 Strategy:").grid(row=0, column=2, sticky=tk.W, padx=(0, 2))
        self.strategy_var = tk.StringVar(value=list(STRATEGIES.keys())[0])
        self.strategy_combo = ttk.Combobox(row1, textvariable=self.strategy_var, width=28,
                                            state="readonly", values=list(STRATEGIES.keys()))
        self.strategy_combo.grid(row=0, column=3, padx=(0, 4))
        self.strategy_var.trace_add("write", self._on_strategy_changed)
        ttk.Button(row1, text="原始碼 Source", command=self._show_strategy_source).grid(row=0, column=4, padx=2)

        # ── Row 2: Login ──
        row2 = ttk.Frame(ctrl)
        row2.pack(fill=tk.X, pady=(1, 2))

        ttk.Label(row2, text="帳號 User ID:").grid(row=0, column=0, sticky=tk.W, padx=(4, 2))
        self.login_user_var = tk.StringVar(value=self._settings.get("user_id", ""))
        ttk.Entry(row2, textvariable=self.login_user_var, width=14).grid(row=0, column=1, padx=(0, 4))

        ttk.Label(row2, text="密碼 Password:").grid(row=0, column=2, sticky=tk.W, padx=(0, 2))
        self.login_pass_var = tk.StringVar(value=self._settings.get("password", ""))
        ttk.Entry(row2, textvariable=self.login_pass_var, width=14, show="*").grid(row=0, column=3, padx=(0, 4))

        self.btn_login = ttk.Button(row2, text="登入 Login", command=self._manual_login)
        self.btn_login.grid(row=0, column=4, padx=2)

        self.btn_reconnect = ttk.Button(row2, text="重新連線 Reconnect", command=self._manual_reconnect,
                                         state=tk.DISABLED)
        self.btn_reconnect.grid(row=0, column=5, padx=2)

        self.login_status_var = tk.StringVar(value="")
        ttk.Label(row2, textvariable=self.login_status_var, foreground="gray").grid(row=0, column=6, padx=4)

        # ── Action buttons (grid layout — wraps gracefully) ──
        btn_frame = ttk.Frame(ctrl)
        btn_frame.pack(fill=tk.X, pady=(2, 2))

        self.btn_tv = ttk.Button(btn_frame, text="TV回測 TV Backtest",
                                 command=self._do_fetch_tv)
        self.btn_tv.grid(row=0, column=0, padx=3, pady=1, sticky=tk.W)

        self.btn_api = ttk.Button(btn_frame, text="API回測 API Backtest",
                                   command=self._do_fetch_api, state=tk.DISABLED)
        self.btn_api.grid(row=0, column=1, padx=3, pady=1, sticky=tk.W)

        self.btn_taifex = ttk.Button(btn_frame, text="TAIFEX回測 TAIFEX Backtest",
                                      command=self._do_fetch_taifex)
        self.btn_taifex.grid(row=0, column=2, padx=3, pady=1, sticky=tk.W)

        self.btn_deploy = ttk.Button(btn_frame, text="部署機器人 Deploy Bot",
                                      command=self._toggle_live, state=tk.DISABLED)
        self.btn_deploy.grid(row=0, column=3, padx=3, pady=1, sticky=tk.W)

        self.btn_chart_all = ttk.Button(btn_frame, text="K線圖 K Chart",
                                        command=self._show_chart_all, state=tk.DISABLED)
        self.btn_chart_all.grid(row=0, column=4, padx=3, pady=1, sticky=tk.W)

        self.btn_export = ttk.Button(btn_frame, text="匯出交易 Export Trades",
                                     command=self._do_export, state=tk.DISABLED)
        self.btn_export.grid(row=0, column=5, padx=3, pady=1, sticky=tk.W)

        self.btn_review = ttk.Button(btn_frame, text="AI檢視 AI Review",
                                     command=self._review_trades, state=tk.DISABLED)
        self.btn_review.grid(row=0, column=7, padx=3, pady=1, sticky=tk.W)

        tf_frame = ttk.Frame(btn_frame)
        tf_frame.grid(row=0, column=6, padx=3, pady=1, sticky=tk.W)
        ttk.Label(tf_frame, text="Chart TF:").pack(side=tk.LEFT, padx=(0, 2))
        self.chart_tf_var = tk.StringVar(value="Native")
        self.chart_tf_combo = ttk.Combobox(
            tf_frame, textvariable=self.chart_tf_var,
            values=list(_LIVE_CHART_TIMEFRAMES.keys()),
            state=tk.DISABLED, width=7,
        )
        self.chart_tf_combo.pack(side=tk.LEFT)

        self.btn_toggle_settings = ttk.Button(btn_frame, text="▶ 設定 Settings",
                                               command=self._toggle_settings)
        self.btn_toggle_settings.grid(row=0, column=6, padx=3, pady=1, sticky=tk.W)

        # Status on its own row so it never clips the buttons above
        self.status_var = tk.StringVar(value="初始化中 Initializing...")
        ttk.Label(ctrl, textvariable=self.status_var, foreground="gray",
                  font=("", 9)).pack(fill=tk.X, padx=6, pady=(0, 1))

        # ── Collapsible backtest settings ──
        self._settings_visible = False
        self._settings_frame = ttk.LabelFrame(ctrl, text="回測參數 Backtest Settings", padding=6)
        # Hidden by default — toggled by _toggle_settings

        sf = self._settings_frame

        # Row 0: Balance + Point Value
        row_a = ttk.Frame(sf)
        row_a.pack(fill=tk.X, pady=2)
        ttk.Label(row_a, text="初始資金 Balance:").pack(side=tk.LEFT, padx=(4, 2))
        self.balance_var = tk.StringVar(value="1000000")
        ttk.Entry(row_a, textvariable=self.balance_var, width=12).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(row_a, text="每點價值 Pt Value:").pack(side=tk.LEFT, padx=(0, 2))
        self.pv_var = tk.StringVar(value="200")
        ttk.Entry(row_a, textvariable=self.pv_var, width=6).pack(side=tk.LEFT, padx=(0, 4))

        # Row 1: Start + End + Quick period buttons
        row_b = ttk.Frame(sf)
        row_b.pack(fill=tk.X, pady=2)
        ttk.Label(row_b, text="起始 Start:").pack(side=tk.LEFT, padx=(4, 2))
        default_start = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
        self.start_var = tk.StringVar(value=default_start)
        ttk.Entry(row_b, textvariable=self.start_var, width=10).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(row_b, text="結束 End:").pack(side=tk.LEFT, padx=(0, 2))
        self.end_var = tk.StringVar(value=datetime.now().strftime("%Y%m%d"))
        ttk.Entry(row_b, textvariable=self.end_var, width=10).pack(side=tk.LEFT, padx=(0, 8))
        for label, days in [("3月", 90), ("6月", 180), ("1年", 365), ("2年", 730), ("4年", 1461)]:
            ttk.Button(row_b, text=label, width=4,
                       command=lambda d=days: self._set_period(d)).pack(side=tk.LEFT, padx=1)

        # Row 2: BB + SL/TP params
        row_c = ttk.Frame(sf)
        row_c.pack(fill=tk.X, pady=2)
        for lbl, var_name, val, w in [
            ("BB週期:", "bb_period_var", "20", 6),
            ("BB Std:", "bb_std_var", "2.0", 6),
            ("SL Offset:", "sl_offset_var", "20", 6),
            ("TP Offset:", "tp_offset_var", "50", 6),
        ]:
            ttk.Label(row_c, text=lbl).pack(side=tk.LEFT, padx=(8, 2))
            sv = tk.StringVar(value=val)
            setattr(self, var_name, sv)
            ttk.Entry(row_c, textvariable=sv, width=w).pack(side=tk.LEFT, padx=(0, 4))

        # Row 3: ATR params
        row_d = ttk.Frame(sf)
        row_d.pack(fill=tk.X, pady=2)
        for lbl, var_name, val, w in [
            ("ATR期數:", "atr_period_var", "14", 6),
            ("SL×ATR:", "sl_mult_var", "1.0", 6),
            ("TP×ATR:", "tp_mult_var", "0.5", 6),
        ]:
            ttk.Label(row_d, text=lbl).pack(side=tk.LEFT, padx=(8, 2))
            sv = tk.StringVar(value=val)
            setattr(self, var_name, sv)
            ttk.Entry(row_d, textvariable=sv, width=w).pack(side=tk.LEFT, padx=(0, 4))

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
        self._trade_sort_col = None
        self._trade_sort_reverse = False
        for col, text, w in [
            ("num", "#", 40), ("tag", "標籤 Tag", 80), ("side", "方向 Side", 55),
            ("entry_time", "進場時間 Entry Time", 135), ("entry_price", "進場價 Entry", 80),
            ("exit_time", "出場時間 Exit Time", 135), ("exit_price", "出場價 Exit", 80),
            ("pnl", "損益 P&L", 100), ("bars_held", "持倉K棒 Bars", 60),
        ]:
            self.trade_tree.heading(col, text=text,
                                   command=lambda c=col: self._sort_trade_tree(c))
            self.trade_tree.column(col, width=w, anchor=tk.E if col != "tag" else tk.W)
        vsb = ttk.Scrollbar(trades_frame, orient="vertical", command=self.trade_tree.yview)
        self.trade_tree.configure(yscrollcommand=vsb.set)
        self.trade_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Live tab
        live_frame = ttk.Frame(notebook)
        notebook.add(live_frame, text="即時 Live")

        # Bot name display (set via popup on Deploy)
        bot_name_frame = ttk.Frame(live_frame)
        bot_name_frame.pack(fill=tk.X, padx=4, pady=(4, 0))
        ttk.Label(bot_name_frame, text="機器人名稱 Bot Name:").pack(side=tk.LEFT, padx=(0, 4))
        self.bot_name_var = tk.StringVar(value="(未設定 Not set)")
        self.bot_name_label = ttk.Label(bot_name_frame, textvariable=self.bot_name_var,
                                         font=("Consolas", 10, "bold"))
        self.bot_name_label.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(bot_name_frame, text="|").pack(side=tk.LEFT, padx=4)
        ttk.Label(bot_name_frame, text="模式 Mode:").pack(side=tk.LEFT, padx=(0, 4))
        self.trading_mode_var = tk.StringVar(value="--")
        self.trading_mode_label = ttk.Label(bot_name_frame, textvariable=self.trading_mode_var,
                                             font=("Consolas", 10, "bold"))
        self.trading_mode_label.pack(side=tk.LEFT)

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

        # Manual order buttons
        order_frame = ttk.LabelFrame(live_frame, text="手動下單 Manual Order", padding=4)
        order_frame.pack(fill=tk.X, padx=4, pady=(2, 2))
        self.btn_manual_buy = ttk.Button(order_frame, text="買進 BUY",
                                          command=lambda: self._manual_order(0))
        self.btn_manual_buy.pack(side=tk.LEFT, padx=4)
        self.btn_manual_sell = ttk.Button(order_frame, text="賣出 SELL",
                                           command=lambda: self._manual_order(1))
        self.btn_manual_sell.pack(side=tk.LEFT, padx=4)
        self.btn_manual_close = ttk.Button(order_frame, text="平倉 CLOSE",
                                            command=self._manual_close)
        self.btn_manual_close.pack(side=tk.LEFT, padx=4)
        # Disable until live bot is running with semi-auto
        for btn in (self.btn_manual_buy, self.btn_manual_sell, self.btn_manual_close):
            btn.config(state=tk.DISABLED)

        # Real account panel
        acct_frame = ttk.LabelFrame(live_frame, text="實帳戶 Real Account", padding=4)
        acct_frame.pack(fill=tk.X, padx=4, pady=(2, 2))

        self.real_pos_var = tk.StringVar(value="--")
        self.real_equity_var = tk.StringVar(value="--")
        self.real_available_var = tk.StringVar(value="--")
        self.real_pnl_var = tk.StringVar(value="--")
        self.real_realized_var = tk.StringVar(value="--")
        self.real_fees_var = tk.StringVar(value="--")
        self.real_net_var = tk.StringVar(value="--")
        self.real_maint_var = tk.StringVar(value="--")
        self.real_fills_var = tk.StringVar(value="--")
        self.real_loss_limit_var = tk.StringVar(value="--")

        row0 = ttk.Frame(acct_frame)
        row0.pack(fill=tk.X, pady=(0, 2))
        for label, var in [
            ("持倉 Pos:", self.real_pos_var),
            ("浮動 Float:", self.real_pnl_var),
            ("平倉損益 Realized:", self.real_realized_var),
            ("費用 Fee+Tax:", self.real_fees_var),
            ("淨損益 Net:", self.real_net_var),
        ]:
            ttk.Label(row0, text=label).pack(side=tk.LEFT, padx=(4, 2))
            lbl = ttk.Label(row0, textvariable=var, font=("Consolas", 10, "bold"))
            lbl.pack(side=tk.LEFT, padx=(0, 8))

        row1 = ttk.Frame(acct_frame)
        row1.pack(fill=tk.X, pady=(0, 2))
        for label, var in [
            ("權益 Equity:", self.real_equity_var),
            ("可用 Avail:", self.real_available_var),
            ("維持率 Maint%:", self.real_maint_var),
            ("虧損上限 Loss Limit:", self.real_loss_limit_var),
            ("今日成交 Trades:", self.real_fills_var),
        ]:
            ttk.Label(row1, text=label).pack(side=tk.LEFT, padx=(4, 2))
            ttk.Label(row1, textvariable=var, font=("Consolas", 10, "bold")).pack(
                side=tk.LEFT, padx=(0, 8))

        ttk.Button(row1, text="刷新 Refresh", width=12,
                   command=self._query_real_account).pack(side=tk.RIGHT, padx=4)

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
        self.status_var.set(f"AI: {provider} / {model}")
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
                _log(f"Chat error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
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
        """Remove the last system message (Thinking..., Generating code..., Recapping..., etc)."""
        self.chat_display.config(state=tk.NORMAL)
        content = self.chat_display.get("1.0", tk.END)
        # Search for known system message patterns (newest first)
        markers = [
            "Thinking...\n",
            "Generating code...\n",
            "正在回顧對話上下文 Recapping conversation context...\n",
            "匯出中 Exporting to Pine Script...\n",
        ]
        idx = -1
        marker_text = ""
        for m in markers:
            pos = content.rfind(m)
            if pos > idx:
                idx = pos
                marker_text = m
        if idx >= 0:
            before = content[:idx]
            line = before.count("\n") + 1
            col = len(before) - before.rfind("\n") - 1
            start = f"{line}.{col}"
            end_idx = idx + len(marker_text) + 1  # +1 for trailing \n
            after_before = content[:end_idx]
            end_line = after_before.count("\n") + 1
            end_col = len(after_before) - after_before.rfind("\n") - 1
            end = f"{end_line}.{end_col}"
            self.chat_display.delete(start, end)
        self.chat_display.config(state=tk.DISABLED)

    def _generate_strategy(self):
        """Ask AI to generate strategy code based on the conversation so far.

        Uses a SEPARATE one-shot API call with only a condensed summary of the
        conversation, not the full chat history.  This avoids output truncation
        when the conversation is long.
        """
        if not self._ensure_chat_client():
            return

        if not self._chat_client.conversation:
            self._append_chat("error", "Chat with the AI first to discuss a strategy idea.")
            return

        self._codegen_conversation_summary = ChatClient.build_summary(
            self._chat_client.conversation)

        gen_msg = (
            "Based on this strategy discussion, write the complete strategy code.\n\n"
            "## Conversation Summary\n"
            + self._codegen_conversation_summary + "\n\n"
            + STRATEGY_CODE_CONTEXT
        )

        self._append_chat("user", "Generate Strategy")
        self.btn_send.config(state=tk.DISABLED)
        self.btn_generate.config(state=tk.DISABLED)
        self._append_chat("system", "Generating code...")

        # Use a one-shot API call (not the chat conversation) to avoid bloat
        client = self._chat_client

        def _worker():
            try:
                response = client.one_shot(gen_msg, system_prompt=CODE_GEN_SYSTEM_PROMPT,
                                          max_tokens=_CODE_GEN_MAX_TOKENS)
                self.root.after(0, lambda: self._on_generate_response(response))
            except Exception as e:
                _log(f"Generate error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
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

        # Detect truncated response
        truncated = "[WARNING: Response truncated" in response

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
        truncation_hint = (
            "Your previous response was truncated due to token limit. "
            "Rewrite the SAME strategy more concisely: combine conditions, "
            "reduce helper variables, keep under 150 lines. "
            "Do NOT replace it with a different/simpler strategy.\n\n"
        ) if truncated else ""
        try:
            strategy_cls = load_strategy_from_source(source)
            self._on_strategy_generated(source, strategy_cls)
        except (CodeValidationError, CodeExecutionError) as e:
            self._generation_retry(
                f"{truncation_hint}The generated code had errors:\n{e}\n\n"
                "Please fix the code and output a corrected version.",
                retries_left,
            )
        except Exception as e:
            _log(f"Strategy load error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
            self._generation_retry(
                f"{truncation_hint}Unexpected error loading strategy:\n{e}\n\n"
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

        # Include conversation summary so the AI retains strategy context on retry
        summary = getattr(self, "_codegen_conversation_summary", "")
        summary_section = (
            "## Conversation Summary (for context — generate the SAME strategy)\n"
            + summary + "\n\n"
        ) if summary else ""
        retry_msg = summary_section + error_msg + "\n\n" + STRATEGY_CODE_CONTEXT
        remaining = retries_left - 1

        def _worker():
            try:
                resp = self._chat_client.one_shot(retry_msg, system_prompt=CODE_GEN_SYSTEM_PROMPT,
                                                  max_tokens=_CODE_GEN_MAX_TOKENS)
                self.root.after(0, lambda: self._on_generate_response(resp, retries_left=remaining))
            except Exception as e:
                _log(f"Retry error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
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

        self.status_var.set(f"策略已載入 Strategy loaded: {strategy_cls.__name__}")
        self._append_chat("system",
                          f"策略已載入 Strategy loaded: **{strategy_cls.__name__}**\n"
                          f"已設為目前策略，可直接點選回測按鈕執行。\n"
                          f"Strategy is ready. Click a backtest button to run.")

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

        self.status_var.set("匯出中 Exporting to Pine Script...")
        self.btn_pine.config(state=tk.DISABLED)

        source = self._ai_strategy_source

        def _worker():
            try:
                pine = export_to_pine(self._chat_client, source)
                self.root.after(0, lambda: self._show_pine_popup(pine))
            except Exception as e:
                _log(f"Pine export error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
                self.root.after(0, lambda: self._on_pine_error(str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _show_pine_popup(self, pine_code: str):
        """Show Pine Script in a popup window with Copy button."""
        self._remove_last_system_line()
        self.status_var.set("Pine Script 匯出完成 Export complete.")
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
        self.status_var.set(f"策略已儲存 Strategy saved: {os.path.basename(path)}")
        self._refresh_saved_combo()

    def _load_saved_strategies(self):
        """Auto-load all saved AI strategies into STRATEGIES on startup."""
        self._refresh_saved_combo()
        entries = self._strategy_store.list_strategies()
        loaded = 0
        for entry in entries:
            class_name = entry["class_name"]
            name = f"AI: {class_name}"
            if name in STRATEGIES:
                continue  # already loaded
            source = self._strategy_store.load_source(class_name)
            if not source:
                continue
            try:
                strategy_cls = load_strategy_from_source(source)
                STRATEGIES[name] = strategy_cls
                loaded += 1
            except Exception:
                _log(f"Failed to auto-load strategy: {class_name}")
        if loaded:
            self.strategy_combo.config(values=list(STRATEGIES.keys()))
            _log(f"Auto-loaded {loaded} saved AI strategies")

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
            self.status_var.set(f"已載入策略 Loaded: {class_name}")
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
        self.status_var.set(f"已刪除 Deleted: {class_name}")

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

            self.status_var.set(f"設定已儲存 Settings saved. Provider: {provider}")
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

    def _save_chat_session(self):
        """Save current chat conversation to a JSON file."""
        if not self._chat_client or not self._chat_client.conversation:
            messagebox.showinfo("Save Chat", "No conversation to save.")
            return

        chat_dir = os.path.join("data", "chats")
        os.makedirs(chat_dir, exist_ok=True)

        default_name = f"chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path = filedialog.asksaveasfilename(
            initialdir=chat_dir,
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile=default_name,
        )
        if not path:
            return

        import json
        session = {
            "conversation": self._chat_client.conversation,
            "display_text": self._build_display_text(),
            "provider": self._settings.get("ai_provider", "anthropic"),
            "model": self._chat_client.model if self._chat_client else "",
            "saved_at": datetime.now().isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, indent=2, ensure_ascii=False)
        self.status_var.set(f"Chat saved: {os.path.basename(path)}")
        _log(f"Chat session saved to {path}")

    def _load_chat_session(self):
        """Load a saved chat conversation from a JSON file."""
        chat_dir = os.path.join("data", "chats")
        os.makedirs(chat_dir, exist_ok=True)

        path = filedialog.askopenfilename(
            initialdir=chat_dir,
            filetypes=[("JSON", "*.json"), ("All", "*.*")],
        )
        if not path:
            return

        import json
        try:
            with open(path, "r", encoding="utf-8") as f:
                session = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            messagebox.showerror("Load Chat", f"Failed to load: {e}")
            return

        # Ensure chat client exists with matching provider
        saved_provider = session.get("provider", "anthropic")
        if not self._chat_client or self._settings.get("ai_provider") != saved_provider:
            if not self._ensure_chat_client():
                return

        # Restore conversation history
        self._chat_client.conversation = session.get("conversation", [])

        # Rebuild display from conversation (filters system messages)
        display_text = self._build_display_text()
        self.chat_display.config(state=tk.NORMAL)
        self.chat_display.delete("1.0", tk.END)
        if display_text:
            if not display_text.endswith("\n\n"):
                display_text = display_text.rstrip("\n") + "\n\n"
            self.chat_display.insert(tk.END, display_text)
        self.chat_display.see(tk.END)
        self.chat_display.config(state=tk.DISABLED)

        n_msgs = len(self._chat_client.conversation)
        self.status_var.set(f"已載入對話 Chat loaded: {os.path.basename(path)} ({n_msgs} msgs)")
        _log(f"Chat session loaded from {path} ({n_msgs} messages)")

        # Ask AI to summarize its understanding of the conversation
        if n_msgs > 0:
            self._send_context_recap()

    def _send_context_recap(self):
        """After loading a chat session, ask the AI to summarize its understanding."""
        if not self._chat_client:
            return

        self._append_chat("system", "正在回顧對話上下文 Recapping conversation context...")
        self.btn_send.config(state=tk.DISABLED)

        def _worker():
            try:
                response = self._chat_client.send_message(CHAT_RECAP_PROMPT)
                # Remove the recap prompt from conversation so it doesn't pollute saves.
                # send_message appends user + assistant, remove both and just keep assistant.
                conv = self._chat_client.conversation
                # Find and remove the recap user message (second to last)
                if len(conv) >= 2 and conv[-2].get("role") == "user":
                    conv.pop(-2)
                self.root.after(0, lambda: self._on_recap_response(response))
            except Exception as e:
                _log(f"Recap error: {e}")
                # Remove the failed user message too
                conv = self._chat_client.conversation
                if conv and conv[-1].get("role") == "user":
                    conv.pop()
                self.root.after(0, lambda: self._on_recap_error(str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_recap_response(self, response: str):
        """Handle the context recap response."""
        self._remove_last_system_line()
        self._append_chat("assistant", f"📋 對話回顧 Context Recap:\n{response}")
        self.btn_send.config(state=tk.NORMAL)

    def _on_recap_error(self, err: str):
        """Handle recap error — not critical, just log it."""
        self._remove_last_system_line()
        self.status_var.set(f"Context recap failed: {err}")
        self.btn_send.config(state=tk.NORMAL)

    _AUTO_CHAT_PATH = os.path.join("data", "chats", "_last_session.json")

    def _build_display_text(self) -> str:
        """Build display text from conversation history only (no system messages)."""
        if not self._chat_client or not self._chat_client.conversation:
            return ""
        provider = self._settings.get("ai_provider", PROVIDER_ANTHROPIC)
        ai_name = "Gemini" if provider == PROVIDER_GOOGLE else "Claude"
        parts = []
        for msg in self._chat_client.conversation:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                parts.append(f"You: {content}")
            elif role == "assistant":
                parts.append(f"{ai_name}: {content}")
        return "\n\n".join(parts)

    def _auto_save_chat(self):
        """Auto-save current conversation for next startup."""
        if not self._chat_client or not self._chat_client.conversation:
            return
        import json
        os.makedirs(os.path.dirname(self._AUTO_CHAT_PATH), exist_ok=True)
        session = {
            "conversation": self._chat_client.conversation,
            "display_text": self._build_display_text(),
            "provider": self._settings.get("ai_provider", "anthropic"),
            "model": self._chat_client.model if self._chat_client else "",
            "saved_at": datetime.now().isoformat(),
        }
        try:
            with open(self._AUTO_CHAT_PATH, "w", encoding="utf-8") as f:
                json.dump(session, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def _on_closing(self):
        """Handle window close: auto-save chat, then destroy."""
        self._auto_save_chat()
        self.root.destroy()

    # ══════════════════════════════════════════════════════════════
    #  EXISTING BACKTEST METHODS (unchanged logic)
    # ══════════════════════════════════════════════════════════════

    def _set_period(self, days):
        self.end_var.set(datetime.now().strftime("%Y%m%d"))
        self.start_var.set((datetime.now() - timedelta(days=days)).strftime("%Y%m%d"))

    def _on_strategy_changed(self, *_args):
        """Update UI when strategy selection changes."""
        pass

    def _toggle_settings(self):
        """Show/hide backtest settings panel."""
        if self._settings_visible:
            self._settings_frame.pack_forget()
            self.btn_toggle_settings.config(text="▶ 設定 Settings")
            self._settings_visible = False
        else:
            self._settings_frame.pack(fill=tk.X, pady=(2, 0))
            self.btn_toggle_settings.config(text="▼ 設定 Settings")
            self._settings_visible = True

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
            # AI-generated: try saved file first, then memory
            saved = self._strategy_store.load_source(cls.__name__)
            if saved:
                source = saved
                filepath = f"(saved: strategies/{cls.__name__})"
            elif self._ai_strategy_source and cls is self._ai_strategy_cls:
                source = self._ai_strategy_source
                filepath = "(AI generated — unsaved)"
            else:
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
        self.btn_taifex.config(state=tk.NORMAL)

    def _disable_buttons(self):
        """Disable buttons during a run."""
        self.btn_api.config(state=tk.DISABLED)
        self.btn_tv.config(state=tk.DISABLED)
        self.btn_taifex.config(state=tk.DISABLED)
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

            # Initialize order service for real trading
            if skO is not None:
                try:
                    skO.SKOrderLib_Initialize()
                    skO.ReadCertByID(user_id)
                    skO.GetUserAccount()
                    _log("委託服務初始化 Order service initialized (cert verified)")
                except Exception as e:
                    _log(f"委託服務初始化失敗 Order service init failed: {e}")

            self.status_var.set("連線中 Connecting...")
            self.root.after(3000, self._check_connection)

        except Exception as e:
            _log(f"初始化錯誤 Init error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
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
            _log(f"連線檢查錯誤: [{type(e).__name__}] {e}\n{traceback.format_exc()}")

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
        elif minute_num >= 15:    # 15m
            bars_per_tday = 56
        elif minute_num >= 5:     # 5m
            bars_per_tday = 60
        else:                     # 1m
            bars_per_tday = 300
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
        # MTX00/TMF00 share TX00's KLine data (same TAIEX index, different point values)
        cfg = _SYMBOL_CONFIG.get(symbol, {})
        kline_sym = cfg.get("kline_symbol", symbol)
        sym_note = f" (via {kline_sym})" if kline_sym != symbol else ""
        _log(f"請求K線 [{n}/{total}] {symbol}{sym_note} type={kline_type} "
             f"{chunk_start}~{chunk_end} min={minute_num}")

        self._fetch_symbol = symbol
        self._fetch_kline_type = kline_type
        self._fetch_minute_num = minute_num

        try:
            code = skQ.SKQuoteLib_RequestKLineAMByDate(
                kline_sym, kline_type, 1, 0, chunk_start, chunk_end, minute_num)

            if code != 0:
                msg = skC.SKCenterLib_GetReturnCodeMessage(code)
                _log(f"請求結果 Result: code={code} {msg}")
                if code >= 3000:
                    self.status_var.set(f"錯誤 Error: {msg}")
                    self._enable_buttons()
                    return

        except Exception as e:
            _log(f"查詢錯誤 Fetch error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
            self._enable_buttons()

    def _do_fetch_taifex(self):
        """Fetch daily bars from TAIFEX public API (no account needed)."""
        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            self.status_var.set("請選擇策略 Select a strategy")
            return

        symbol = self.symbol_var.get().strip()
        cfg = _SYMBOL_CONFIG.get(symbol)
        if not cfg or "taifex_id" not in cfg:
            self.status_var.set(f"TAIFEX不支援此商品 Unsupported symbol: {symbol}")
            return

        commodity_id = cfg["taifex_id"]
        prefix = cfg["prefix"]

        # Warn if strategy uses intraday bars
        if strategy_cls.kline_type != 4:
            if not messagebox.askokcancel(
                "TAIFEX僅有日K TAIFEX Daily Only",
                f"TAIFEX僅提供日K線資料。\n"
                f"目前策略使用 {strategy_cls.kline_minute} 分K。\n"
                f"是否仍要使用日K回測？\n\n"
                f"TAIFEX only provides daily bars.\n"
                f"Current strategy uses {strategy_cls.kline_minute}-min bars.\n"
                f"Continue with daily bars anyway?",
            ):
                return

        # Parse date range from GUI
        try:
            start_str = self.start_var.get().strip()
            end_str = self.end_var.get().strip()
            start_date = datetime.strptime(start_str, "%Y%m%d").date()
            end_date = datetime.strptime(end_str, "%Y%m%d").date()
        except ValueError:
            self.status_var.set("日期格式錯誤 Invalid date format (YYYYMMDD)")
            return

        # Reuse in-memory bars if same source + symbol
        if (self._data_source.startswith("TAIFEX") and self._raw_bars
                and self._raw_bars_key == (symbol, 4, 1)):
            _log("重新使用TAIFEX資料 Re-using TAIFEX data with date filter")
            self._execute_backtest(list(self._raw_bars))
            return

        # Check local cache
        cache_path = get_cache_path("taifex", f"{commodity_id}_daily", ".csv")
        if cache_covers_range(cache_path, start_date, end_date):
            _log(f"載入TAIFEX快取 Loading cached: {cache_path.name}")
            bars = load_bars_csv(cache_path, symbol=prefix, interval=86400)
            if bars:
                self._data_source = f"TAIFEX ({cache_path.name})"
                self._raw_bars_key = (symbol, 4, 1)
                _log(f"載入完成 Loaded {len(bars)} bars from cache")
                self._execute_backtest(bars)
                return

        # Fetch from TAIFEX API in background thread
        self._disable_buttons()
        self.status_var.set(f"從TAIFEX下載中... Downloading from TAIFEX ({start_date} ~ {end_date})")
        _log(f"開始下載TAIFEX資料 Fetching {commodity_id} from {start_date} to {end_date}")

        def _fetch():
            try:
                def _progress(cur, total):
                    self.root.after(0, lambda c=cur, t=total:
                        self.status_var.set(f"TAIFEX下載中 {c}/{t} chunks..."))

                bars = fetch_futures_daily(
                    commodity_id, start_date, end_date,
                    symbol=prefix, price_multiplier=1,
                    on_progress=_progress,
                )

                def _done():
                    if not bars:
                        self.status_var.set("TAIFEX無資料 No data returned")
                        self._enable_buttons()
                        return
                    # Save to cache (merge with existing)
                    existing = load_bars_csv(cache_path, symbol=prefix, interval=86400)
                    if existing:
                        seen = {b.dt for b in bars}
                        merged = bars + [b for b in existing if b.dt not in seen]
                        merged.sort(key=lambda b: b.dt)
                    else:
                        merged = bars
                    save_bars_csv(merged, cache_path)
                    _log(f"TAIFEX下載完成 {len(bars)} bars fetched, {len(merged)} total cached")
                    self._data_source = f"TAIFEX ({commodity_id})"
                    self._raw_bars_key = (symbol, 4, 1)
                    self._execute_backtest(merged)

                self.root.after(0, _done)
            except Exception as e:
                self.root.after(0, lambda: [
                    _log(f"TAIFEX錯誤 Error: {e}"),
                    self.status_var.set(f"TAIFEX錯誤: {e}"),
                    self._enable_buttons(),
                ])

        threading.Thread(target=_fetch, daemon=True).start()

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

        _NET_ERRORS = (ConnectionError, OSError, TimeoutError)
        try:
            import websocket
            _NET_ERRORS = (ConnectionError, OSError, TimeoutError,
                           websocket.WebSocketException)
        except ImportError:
            pass

        max_retries = 3
        df = None
        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                tv = TvDatafeed()
                df = tv.get_hist(symbol=symbol, exchange=exchange,
                                 interval=tv_interval, n_bars=5000)
                if df is not None and not df.empty:
                    break
                _log(f"TradingView第{attempt}次無資料 Attempt {attempt}/{max_retries}: no data")
            except _NET_ERRORS as e:
                last_err = e
                _log(f"TradingView第{attempt}次連線失敗 Attempt {attempt}/{max_retries} "
                     f"network error: [{type(e).__name__}] {e}")
                if attempt < max_retries:
                    import time
                    time.sleep(2)
            except Exception as e:
                _log(f"TradingView錯誤 TV error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
                self.status_var.set(f"TradingView錯誤: {e}")
                self._enable_buttons()
                return

        if df is None or df.empty:
            if last_err:
                _log(f"TradingView連線失敗（已重試{max_retries}次）Network error after "
                     f"{max_retries} retries. 請確認網路連線 Please check internet connection.")
                self.status_var.set("TradingView連線失敗 Connection failed (請確認網路)")
            else:
                _log("TradingView無資料 No data from TradingView")
                self.status_var.set("TradingView無資料 No data")
            self._enable_buttons()
            return

        try:
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
            _log(f"TradingView錯誤 TV error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
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

        def _backtest_worker():
            try:
                result = engine.run(bars)
                self.root.after(0, lambda: self._on_backtest_done(result, bars, initial_balance))
            except Exception as e:
                tb = traceback.format_exc()
                self.root.after(0, lambda: self._on_backtest_error(e, tb))

        threading.Thread(target=_backtest_worker, daemon=True).start()

    def _on_backtest_done(self, result, bars, initial_balance):
        """Handle backtest completion on the main thread."""
        # Recalculate metrics with initial balance
        result.metrics = calculate_metrics(
            result.trades, result.equity_curve, initial_balance=initial_balance)

        self._last_result = result
        self._last_bars = bars
        self._display_results(result, bars)

        self._enable_buttons()
        self.btn_export.config(state=tk.NORMAL)
        if result.trades:
            self.btn_review.config(state=tk.NORMAL)
        if _LWC_AVAILABLE and result.trades:
            self.btn_chart_all.config(state=tk.NORMAL)
        self.status_var.set(
            f"完成 Done: {result.metrics.total_trades} trades, "
            f"win rate {result.metrics.win_rate * 100:.1f}%, "
            f"P&L {result.metrics.total_pnl:+,}")

    def _on_backtest_error(self, error, tb):
        """Handle backtest error on the main thread."""
        _log(f"回測錯誤 Backtest error:\n{tb}")
        self.status_var.set(f"回測錯誤 Backtest error: {error}")
        self._append_chat("error", f"Backtest runtime error:\n{error}")
        self._enable_buttons()

    def _display_results(self, result, bars: list[Bar] | None = None):
        # Metrics report
        self.metrics_text.delete("1.0", tk.END)

        # Data source header
        symbol = self.symbol_var.get().strip()
        source = self._data_source or "unknown"
        header_lines = [f" 商品 Symbol:  {symbol}", f" 資料來源 Source:  {source}"]
        if bars:
            # For live mode, show only live-trading range (exclude warmup history)
            is_live = self._live_runner and self._live_runner.state != LiveState.IDLE
            if is_live:
                live_bars = self._live_runner.get_live_bars()
                if live_bars:
                    header_lines.append(
                        f" 資料範圍 Range:  {live_bars[0].dt.strftime('%Y-%m-%d %H:%M')} ~ "
                        f"{live_bars[-1].dt.strftime('%Y-%m-%d %H:%M')}")
                    header_lines.append(
                        f" K棒數量 Bars:  {len(live_bars)} 即時 live + "
                        f"{len(bars) - len(live_bars)} 暖機 warmup")
                else:
                    header_lines.append(f" K棒數量 Bars:  {len(bars)} (暖機中 warming up)")
            else:
                header_lines.append(
                    f" 資料範圍 Range:  {bars[0].dt.strftime('%Y-%m-%d %H:%M')} ~ "
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

            # Prefer stored datetimes; fall back to bar index lookup
            entry_dt = t.entry_dt or ""
            exit_dt = t.exit_dt or ""
            if not entry_dt and bars and 0 <= t.entry_bar_index < len(bars):
                entry_dt = bars[t.entry_bar_index].dt.strftime("%Y-%m-%d %H:%M")
            if not exit_dt and bars and 0 <= t.exit_bar_index < len(bars):
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

    def _sort_trade_tree(self, col: str):
        """Sort trade tree by column header click (toggle ascending/descending)."""
        if self._trade_sort_col == col:
            self._trade_sort_reverse = not self._trade_sort_reverse
        else:
            self._trade_sort_col = col
            self._trade_sort_reverse = False

        # Numeric columns need numeric sorting
        numeric_cols = {"num", "entry_price", "exit_price", "pnl", "bars_held"}

        items = []
        for iid in self.trade_tree.get_children():
            values = self.trade_tree.item(iid, "values")
            tags = self.trade_tree.item(iid, "tags")
            items.append((iid, values, tags))

        col_idx = list(self.trade_tree["columns"]).index(col)

        def sort_key(item):
            val = item[1][col_idx]
            if col in numeric_cols:
                # Strip commas and +/- formatting
                cleaned = str(val).replace(",", "").replace("+", "")
                try:
                    return float(cleaned)
                except ValueError:
                    return 0
            return str(val)

        items.sort(key=sort_key, reverse=self._trade_sort_reverse)

        for idx, (iid, values, tags) in enumerate(items):
            self.trade_tree.move(iid, "", idx)

        # Update heading to show sort direction
        arrow = " ▼" if self._trade_sort_reverse else " ▲"
        col_texts = {
            "num": "#", "tag": "標籤 Tag", "side": "方向 Side",
            "entry_time": "進場時間 Entry Time", "entry_price": "進場價 Entry",
            "exit_time": "出場時間 Exit Time", "exit_price": "出場價 Exit",
            "pnl": "損益 P&L", "bars_held": "持倉K棒 Bars",
        }
        for c, text in col_texts.items():
            display = text + arrow if c == col else text
            self.trade_tree.heading(c, text=display)

    def _chart_kwargs(self) -> dict:
        try:
            bb_period = int(self.bb_period_var.get())
            bb_std = float(self.bb_std_var.get())
        except ValueError:
            bb_period, bb_std = 20, 2.0
        return dict(bb_period=bb_period, bb_std=bb_std)

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
            _log(f"圖表錯誤 Chart error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
        except Exception as e:
            self.root.after(0, lambda: self.status_var.set(f"圖表錯誤 Chart error: {e}"))
            _log(f"圖表錯誤 Chart error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")

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

    def _review_trades(self):
        """Feed backtest/live results into AI chat for strategy review."""
        result = (self._live_runner.get_result()
                  if self._live_runner and self._live_runner.state != LiveState.IDLE
                  else self._last_result)
        if not result or not result.trades:
            self.status_var.set("無交易紀錄 No trades to review")
            return
        if not self._ensure_chat_client():
            return

        # Build trade summary for AI context
        report = format_report(result.strategy_name, result.metrics)

        # Include individual trade details (cap at 100 trades for token budget)
        trade_lines = []
        for i, t in enumerate(result.trades[:100], 1):
            bars_held = t.exit_bar_index - t.entry_bar_index
            entry_dt = t.entry_dt or f"bar#{t.entry_bar_index}"
            exit_dt = t.exit_dt or f"bar#{t.exit_bar_index}"
            trade_lines.append(
                f"  {i}. {t.side.value} {t.tag}: "
                f"entry={entry_dt} @{t.entry_price:,} → "
                f"exit={exit_dt} @{t.exit_price:,} "
                f"P&L={t.pnl:+,} ({bars_held} bars)"
            )
        if len(result.trades) > 100:
            trade_lines.append(f"  ... ({len(result.trades) - 100} more trades omitted)")

        # Resolve strategy source code for AI context
        strategy_source = ""
        strategy_name = self.strategy_var.get()
        strategy_cls = STRATEGIES.get(strategy_name)
        if self._ai_strategy_source and strategy_name.startswith("AI:"):
            strategy_source = self._ai_strategy_source
        elif strategy_cls:
            # Try saved strategies first, then built-in via inspect
            source = self._strategy_store.load_source(strategy_cls.__name__)
            if source:
                strategy_source = source
            else:
                try:
                    strategy_source = inspect.getsource(strategy_cls)
                except (TypeError, OSError):
                    pass

        source_section = ""
        if strategy_source:
            source_section = (
                f"\n\n策略原始碼 Strategy Source Code:\n"
                f"```python\n{strategy_source}\n```"
            )

        context = (
            f"以下是回測/實盤結果，請根據策略原始碼分析交易表現並提出優化建議。\n"
            f"Below are the backtest/live results. Analyze the trading performance "
            f"based on the strategy source code and suggest improvements.\n\n"
            f"{report}\n\n"
            f"交易明細 Trade Details:\n" + "\n".join(trade_lines)
            + source_section
        )

        # Send as user message to the AI
        self._append_chat("user", "AI Review: 請分析以下交易紀錄\n" + context)
        self.btn_send.config(state=tk.DISABLED)
        self._append_chat("system", "Analyzing trades...")

        def _worker():
            try:
                response = self._chat_client.send_message(context)
                self.root.after(0, lambda: self._on_chat_response(response))
            except Exception as e:
                _log(f"Review error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
                err_msg = str(e)
                self.root.after(0, lambda: self._on_chat_error(err_msg))

        threading.Thread(target=_worker, daemon=True).start()

    # ══════════════════════════════════════════════════════════════
    #  LIVE TRADING METHODS
    # ══════════════════════════════════════════════════════════════

    def _show_bot_session_dialog(self, symbol: str, base_dir: str):
        """Show dialog to pick existing bot session or create new one.

        Returns (bot_name, resume_session) or None if cancelled.
        resume_session is a dict (from session.json) or None for new bots.
        """
        # Scan for existing bot directories for this symbol
        prefix = f"{symbol}_"
        existing_bots = []  # [(bot_name, session_data_or_None)]
        if os.path.isdir(base_dir):
            for entry in sorted(os.listdir(base_dir)):
                full = os.path.join(base_dir, entry)
                if os.path.isdir(full) and entry.startswith(prefix):
                    bot_name = entry[len(prefix):]
                    if not bot_name:
                        continue
                    sess = load_session(os.path.join(full, "session.json"))
                    existing_bots.append((bot_name, sess))

        # Build dialog
        dlg = tk.Toplevel(self.root)
        dlg.title("選擇機器人 Select Bot Session")
        dlg.geometry("680x520")
        dlg.transient(self.root)
        dlg.grab_set()

        result = [None]  # mutable container for return value

        # Existing sessions list
        if existing_bots:
            ttk.Label(dlg, text="載入現有機器人 Load Existing Bot:",
                      font=("", 10, "bold")).pack(anchor=tk.W, padx=10, pady=(10, 4))

            list_frame = ttk.Frame(dlg)
            list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 4))

            columns = ("name", "strategy", "mode", "trades", "pnl", "position", "saved")
            tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=8)
            for col, text, w in [
                ("name", "名稱 Name", 90), ("strategy", "策略 Strategy", 110),
                ("mode", "模式 Mode", 70),
                ("trades", "交易數 Trades", 55), ("pnl", "損益 P&L", 60),
                ("position", "持倉 Position", 60), ("saved", "上次儲存 Last Saved", 120),
            ]:
                tree.heading(col, text=text)
                tree.column(col, width=w, anchor=tk.W if col in ("name", "strategy") else tk.CENTER)

            vsb = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=vsb.set)
            tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            vsb.pack(side=tk.RIGHT, fill=tk.Y)

            _MODE_SHORT = {"paper": "模擬", "semi_auto": "半自動", "auto": "全自動"}
            for bot_name, sess in existing_bots:
                if sess:
                    strat = sess.get("strategy", "?")
                    mode = _MODE_SHORT.get(sess.get("trading_mode", ""), "?")
                    broker = sess.get("broker", {})
                    trades_count = len(broker.get("trades", []))
                    pnl = broker.get("cumulative_pnl", 0)
                    pos = broker.get("position_size", 0)
                    pos_side = broker.get("position_side", "")
                    pos_str = f"{pos_side} {pos}" if pos > 0 else "Flat"
                    saved = sess.get("saved_at", "?")
                else:
                    strat, mode = "?", "?"
                    trades_count, pnl, pos_str, saved = "?", "?", "?", "(no session)"
                tree.insert("", tk.END, values=(bot_name, strat, mode, trades_count, pnl, pos_str, saved))

            def on_load():
                sel = tree.selection()
                if not sel:
                    messagebox.showwarning("Select", "請選擇一個機器人 Please select a bot.", parent=dlg)
                    return
                idx = tree.index(sel[0])
                name, sess = existing_bots[idx]
                result[0] = (name, sess, mode_var.get(), loss_var.get())
                dlg.destroy()

            btn_row = ttk.Frame(dlg)
            btn_row.pack(pady=(0, 8))
            ttk.Button(btn_row, text="載入選取 Load Selected", command=on_load).pack(side=tk.LEFT, padx=4)

            def on_delete():
                sel = tree.selection()
                if not sel:
                    messagebox.showwarning("Select", "請選擇一個機器人 Please select a bot.", parent=dlg)
                    return
                idx = tree.index(sel[0])
                name, sess = existing_bots[idx]
                if not messagebox.askyesno(
                    "刪除機器人 Delete Bot",
                    f"確定要刪除 '{name}' 嗎？所有資料將被移除。\n"
                    f"Delete '{name}'? All data will be removed.",
                    parent=dlg,
                ):
                    return
                import shutil
                bot_dir = os.path.join(base_dir, f"{prefix}{name}")
                try:
                    shutil.rmtree(bot_dir)
                    tree.delete(sel[0])
                    existing_bots.pop(idx)
                    self._live_log_msg(f"已刪除機器人 Bot deleted: {name}", "status")
                except Exception as e:
                    messagebox.showerror("Error", f"刪除失敗: {e}", parent=dlg)

            ttk.Button(btn_row, text="刪除 Delete", command=on_delete).pack(side=tk.LEFT, padx=4)

            # Pre-select saved trading mode and loss limit when bot is selected
            def on_select(event=None):
                sel = tree.selection()
                if not sel:
                    return
                idx = tree.index(sel[0])
                _, sess = existing_bots[idx]
                if sess:
                    saved_mode = sess.get("trading_mode", "paper")
                    if saved_mode in ("paper", "semi_auto", "auto"):
                        mode_var.set(saved_mode)
                    saved_limit = sess.get("daily_loss_limit")
                    if saved_limit is not None:
                        loss_var.set(str(saved_limit))

            tree.bind("<<TreeviewSelect>>", on_select)

            # Double-click to load
            tree.bind("<Double-1>", lambda e: on_load())

        else:
            ttk.Label(dlg, text=f"沒有 {symbol} 的現有機器人 No existing bots for {symbol}.",
                      foreground="gray").pack(anchor=tk.W, padx=10, pady=(10, 4))

        # New bot section
        ttk.Separator(dlg, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=4)
        ttk.Label(dlg, text="建立新機器人 Create New Bot:",
                  font=("", 10, "bold")).pack(anchor=tk.W, padx=10, pady=(4, 4))

        new_frame = ttk.Frame(dlg)
        new_frame.pack(fill=tk.X, padx=10, pady=(0, 8))
        ttk.Label(new_frame, text="名稱 Name:").pack(side=tk.LEFT, padx=(0, 4))
        new_name_var = tk.StringVar(value=self.strategy_var.get().replace(" ", "_"))
        new_entry = ttk.Entry(new_frame, textvariable=new_name_var, width=25)
        new_entry.pack(side=tk.LEFT, padx=(0, 8))

        existing_names = {b[0] for b in existing_bots}

        def on_create():
            name = new_name_var.get().strip()
            if not name:
                messagebox.showwarning("Name", "請輸入名稱 Please enter a name.", parent=dlg)
                return
            name = name.replace(" ", "_").replace("/", "_").replace("\\", "_")
            if name in existing_names:
                messagebox.showerror("Name Conflict",
                                     f"名稱 '{name}' 已存在 Name already exists.\n"
                                     "請使用「載入」或輸入新名稱\n"
                                     "Use 'Load' or enter a different name.", parent=dlg)
                return
            result[0] = (name, None, mode_var.get(), loss_var.get())
            dlg.destroy()

        ttk.Button(new_frame, text="建立 Create", command=on_create).pack(side=tk.LEFT)

        # Trading mode selector
        ttk.Separator(dlg, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10, pady=4)
        mode_frame = ttk.LabelFrame(dlg, text="交易模式 Trading Mode")
        mode_frame.pack(fill=tk.X, padx=10, pady=(0, 8))
        mode_var = tk.StringVar(value="paper")
        ttk.Radiobutton(mode_frame, text="模擬 Paper (模擬交易，不下單)",
                        variable=mode_var, value="paper").pack(anchor=tk.W, padx=10, pady=2)
        semi_auto_rb = ttk.Radiobutton(
            mode_frame,
            text="半自動 Semi-Auto (模擬成交後確認下單，10秒未回應則跳過)",
            variable=mode_var, value="semi_auto")
        semi_auto_rb.pack(anchor=tk.W, padx=10, pady=2)
        auto_rb = ttk.Radiobutton(
            mode_frame,
            text="全自動 Auto (模擬成交後自動下單，無需確認)",
            variable=mode_var, value="auto")
        auto_rb.pack(anchor=tk.W, padx=10, pady=2)
        # Disable real trading modes if not connected or no account
        if not (self._logged_in and self._futures_account):
            semi_auto_rb.config(state=tk.DISABLED)
            auto_rb.config(state=tk.DISABLED)
            ttk.Label(mode_frame, text="  (需先登入才能使用半/全自動 Login required for semi/auto)",
                      foreground="gray").pack(anchor=tk.W, padx=10)

        # Daily loss limit (semi-auto only)
        loss_frame = ttk.Frame(mode_frame)
        loss_frame.pack(anchor=tk.W, padx=30, pady=(0, 4))
        ttk.Label(loss_frame, text="每日虧損上限 Daily Loss Limit (NTD):").pack(side=tk.LEFT)
        loss_var = tk.StringVar(value="1000")
        loss_entry = ttk.Entry(loss_frame, textvariable=loss_var, width=8)
        loss_entry.pack(side=tk.LEFT, padx=4)

        # Cancel
        ttk.Button(dlg, text="取消 Cancel", command=dlg.destroy).pack(pady=(0, 10))

        # Wait for dialog to close
        self.root.wait_window(dlg)
        return result[0]

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

        symbol = self.symbol_var.get().strip()
        if not symbol:
            return

        # Show bot session picker dialog
        base_dir = os.path.join(project_root, "data", "live")
        result = self._show_bot_session_dialog(symbol, base_dir)
        if result is None:  # cancelled
            return
        bot_name, resume_session, trading_mode, loss_limit_str = result
        self._trading_mode = trading_mode
        try:
            loss_limit = max(0, int(loss_limit_str))
        except (ValueError, TypeError):
            loss_limit = 1000
        self._trading_guard = TradingGuard(daily_loss_limit=loss_limit)
        self._trading_guard.reset()
        self.bot_name_var.set(bot_name)
        _MODE_LABELS = {"paper": "模擬 Paper", "semi_auto": "半自動 Semi-Auto", "auto": "全自動 Auto"}
        self.trading_mode_var.set(_MODE_LABELS.get(trading_mode, trading_mode))

        # If resuming, restore strategy + symbol from saved session
        if resume_session:
            saved_strategy = resume_session.get("strategy", "")
            saved_symbol = resume_session.get("symbol", "")
            # Find matching strategy in dropdown
            matched = False
            for name in STRATEGIES:
                if name == saved_strategy or name.endswith(saved_strategy):
                    self.strategy_var.set(name)
                    matched = True
                    break
            if not matched:
                # Try AI strategies
                for name in STRATEGIES:
                    if name.startswith("AI:") and saved_strategy in name:
                        self.strategy_var.set(name)
                        matched = True
                        break
            if not matched:
                messagebox.showerror("Strategy Not Found",
                                     f"找不到策略 Strategy '{saved_strategy}' not found.\n"
                                     "請確認策略已載入 Please ensure the strategy is loaded.")
                return
            if saved_symbol and saved_symbol in [v for v in self.symbol_combo['values']]:
                self.symbol_var.set(saved_symbol)
                self._on_symbol_changed()
            # Restore point value from session
            saved_pv = resume_session.get("point_value")
            if saved_pv:
                self.pv_var.set(str(saved_pv))

        # Re-read strategy after potential update from session
        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            self.status_var.set("請選擇策略 Select a strategy")
            return

        # Check for lock conflict (another instance using the same bot name)
        symbol = self.symbol_var.get().strip()
        bot_dir = LiveRunner.bot_dir_for(base_dir, symbol, bot_name)
        is_locked, lock_pid = LiveRunner.check_lock(bot_dir)
        if is_locked:
            messagebox.showerror(
                "Bot Name Conflict",
                f"機器人名稱 '{bot_name}' 已被另一個程式佔用 (PID {lock_pid})。\n"
                f"Bot name '{bot_name}' is already in use by another instance.\n\n"
                "請使用不同的名稱 Please use a different name.",
            )
            return

        # Semi-auto: check for pre-existing real positions before deploy
        if trading_mode in ("semi_auto", "auto") and self._futures_account:
            # Refresh real positions synchronously
            try:
                user_id = self.login_user_var.get().strip()
                self._real_positions.clear()
                skO.GetOpenInterestGW(user_id, self._futures_account, 1)
                # Drain UI queue to process the callback
                self.root.update_idletasks()
                time.sleep(0.5)
                self._drain_ui_queue()
            except Exception as e:
                _log(f"部署前持倉查詢失敗 Pre-deploy position check failed: {e}")

            if self._real_positions:
                pos_parts = []
                for p in self._real_positions:
                    side = "多 LONG" if p["side"] == "B" else "空 SHORT"
                    pos_parts.append(f"{side} x{p['qty']} {p['product']}")
                pos_str = ", ".join(pos_parts)
                proceed = messagebox.askyesno(
                    "帳戶有持倉 Existing Position",
                    f"帳戶已有未平倉部位：{pos_str}\n"
                    f"Account has open positions: {pos_str}\n\n"
                    "半自動模式下，機器人不會送出實單，直到持倉清空。\n"
                    "In semi-auto mode, the bot will NOT send real orders\n"
                    "until existing positions are closed.\n\n"
                    "是否仍要部署？（可用手動按鈕平倉後恢復）\n"
                    "Deploy anyway? (Use manual buttons to close, then orders resume)",
                )
                if not proceed:
                    return
                # Deploy but guard.real_entry_confirmed stays False
                # so no auto exits are sent for positions we didn't create

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
            strategy, symbol, point_value=point_value,
            log_dir=log_dir, bot_name=bot_name,
            strategy_display_name=self.strategy_var.get(),
        )
        self._live_runner.acquire_lock()
        self._live_runner.trading_mode = trading_mode
        self._live_runner.daily_loss_limit = self._trading_guard.daily_loss_limit

        # Debug: log resolved order symbol and query stock list
        order_sym = _resolve_order_symbol(symbol)
        _log(f"委託商品 Order symbol: {symbol} -> {order_sym}")
        if _com_available and skQ and self._quote_connected:
            try:
                # Query multiple market types to find TMF order codes
                # 2=期貨T盤, 7=期貨全盤, 9=客製化期貨
                for mkt in (2, 7, 9):
                    rc = skQ.SKQuoteLib_RequestStockList(mkt)
                    if isinstance(rc, int) and rc != 0:
                        _log(f"商品列表查詢 RequestStockList({mkt}) code={rc}")
            except Exception as e:
                _log(f"商品列表查詢失敗 RequestStockList error: {e}")

        # Restore previous session if resuming
        if resume_session:
            n = self._live_runner.restore_session(resume_session)
            self._live_log_msg(f"恢復交易紀錄 Resumed session: {n} trades restored", "status")

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
        self._update_manual_order_buttons()

        mode_label = "模擬 Paper" if trading_mode == "paper" else "半自動 Semi-Auto"
        self._live_log_msg(
            f"部署中 Deploying: {strategy.name} on {symbol} [{bot_name}] "
            f"模式={mode_label}", "status")
        _log(f"部署即時機器人 Deploying live bot: {strategy.name} on {symbol} [{bot_name}] mode={trading_mode}")

        if trading_mode == "semi_auto":
            self._live_log_msg(
                "*** 半自動模式 SEMI-AUTO MODE — 模擬成交後將提示下單確認 ***", "exit")
        elif trading_mode == "auto":
            self._live_log_msg(
                "*** 全自動模式 AUTO MODE — 模擬成交後自動下單 ***", "exit")

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
        # MTX00/TMF00 share TX00's KLine data (same TAIEX index)
        cfg = _SYMBOL_CONFIG.get(symbol, {})
        kline_sym = cfg.get("kline_symbol", symbol)
        _log(f"COM暖機查詢 COM warmup fetch: {kline_sym} (for {symbol}) type={kline_type} "
             f"min={kline_minute} {start_str}~{end_str}")

        try:
            code = skQ.SKQuoteLib_RequestKLineAMByDate(
                kline_sym, kline_type, 1, 0, start_str, end_str, kline_minute)
            if code != 0 and code >= 3000:
                msg = skC.SKCenterLib_GetReturnCodeMessage(code)
                _log(f"暖機查詢失敗 Warmup fetch failed: {msg}")
                self._live_warmup_mode = False
                self._stop_live()
        except Exception as e:
            _log(f"暖機錯誤 Warmup error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
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

                _net_errors = (ConnectionError, OSError, TimeoutError)
                try:
                    import websocket
                    _net_errors = (ConnectionError, OSError, TimeoutError,
                                   websocket.WebSocketException)
                except ImportError:
                    pass

                df = None
                max_retries = 3
                last_err = None
                for attempt in range(1, max_retries + 1):
                    try:
                        tv = TvDatafeed()
                        df = tv.get_hist(symbol=tv_symbol, exchange="TAIFEX",
                                         interval=tv_interval, n_bars=5000)
                        if df is not None and not df.empty:
                            break
                    except _net_errors as e:
                        last_err = e
                        _log(f"TV暖機第{attempt}次連線失敗 TV warmup attempt {attempt}/{max_retries} "
                             f"network error: [{type(e).__name__}] {e}")
                        if attempt < max_retries:
                            import time
                            time.sleep(2)

                if df is None or df.empty:
                    if last_err:
                        msg = f"TV連線失敗 Connection failed: {last_err}"
                    else:
                        msg = "TV無資料 No TV data"
                    self.root.after(0, lambda m=msg: self._live_log_msg(m, "status"))
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
                    self._update_live_results()  # enable chart button & populate _last_bars
                    self._update_manual_order_buttons()
                    self._start_account_polling()
                    self._start_live_tick_subscription()

                self.root.after(0, _finish)
            except Exception as e:
                _log(f"TV暖機錯誤: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
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
        self._update_live_results()  # enable chart button & populate _last_bars
        self._update_manual_order_buttons()
        self._start_account_polling()
        self._start_live_tick_subscription()

    # ── Tick-based live data feed ──

    def _start_live_tick_subscription(self):
        """Subscribe to real-time ticks via COM and build 1-min bars."""
        if not self._live_runner:
            return

        # Reload saved 1-min bars from CSV (for resumed sessions)
        n_reloaded = self._live_runner.reload_1m_bars()
        if n_reloaded > 0:
            self._live_log_msg(
                f"已載入歷史1分K Reloaded {n_reloaded} saved 1m bars from CSV", "status")

        symbol = self._live_runner.symbol
        # MTX00/TMF00 use TX00 ticks (same TAIEX index prices)
        cfg = _SYMBOL_CONFIG.get(symbol, {})
        tick_sym = cfg.get("tick_symbol", symbol)
        self._live_tick_symbol = symbol  # keep original for logging/orders
        self._live_tick_com_symbol = tick_sym  # actual COM subscription symbol
        self._live_bar_builder = BarBuilder(symbol, interval=60)

        if not _com_available or not self._quote_connected:
            self._live_log_msg("未連線 Not connected, cannot subscribe to ticks", "status")
            self._stop_live()
            return

        # Suppress strategy during history tick catchup — no trades on old data
        self._live_runner.suppress_strategy = True

        sym_note = f" (via {tick_sym})" if tick_sym != symbol else ""
        self._live_log_msg(f"訂閱即時報價 Subscribing to ticks: {symbol}{sym_note}...", "status")
        try:
            result = skQ.SKQuoteLib_RequestTicks(0, tick_sym)
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
            self._tick_watchdog.on_tick()
            self._tick_watchdog.active = True
            self._live_log_msg(f"已訂閱 Tick subscription active for {symbol}", "status")
            _log(f"即時報價訂閱成功 Tick subscription OK: {symbol}, result={result}")
        except Exception as e:
            _log(f"報價訂閱錯誤 Tick subscribe error: [{type(e).__name__}] {e}\n{traceback.format_exc()}")
            self._live_log_msg(f"訂閱錯誤 Subscribe error: {e}", "status")
            self._stop_live()

        # Start draining tick queue on main thread
        self._drain_tick_queue()
        # Schedule periodic status updates (every 30s)
        self._schedule_status_update()

    # Tick watchdog thresholds now live in TickWatchdog class

    def _schedule_status_update(self):
        """Periodically update the live status panel + tick watchdog."""
        if not self._live_runner or self._live_runner.state != LiveState.RUNNING:
            return
        self._update_live_status()
        self._check_tick_watchdog()
        self._check_session_end_close()
        self._live_poll_id = self.root.after(30000, self._schedule_status_update)

    _SESSION_END_CLOSE_MINUTES = 2  # force close N minutes before session end

    def _check_session_end_close(self):
        """Force-close open positions before market session ends.

        Prevents positions from staying open over weekends or overnight gaps.
        Triggers 2 minutes before session close (13:43 AM, 04:58 night).
        """
        if not self._live_runner or self._live_runner.broker.position_size == 0:
            return
        mins = minutes_until_session_close()
        if mins is None:
            return
        if mins > self._SESSION_END_CLOSE_MINUTES:
            return

        # Force close the position
        runner = self._live_runner
        bars = runner._aggregated_bars
        if not bars:
            return
        last_bar = bars[-1]
        last_dt = last_bar.dt.strftime("%Y-%m-%d %H:%M") if last_bar.dt else ""
        side = runner.broker.trades[-1].side.value if runner.broker.trades else ""

        self._live_log_msg(
            f"盤前自動平倉 Session-end auto close: {mins}min to close, "
            f"force closing position", "exit")
        runner.broker.force_close(runner._bar_index, last_bar.close, last_dt)
        runner._log_decision(
            last_bar, "FORCE_CLOSE", side,
            "session_end", last_bar.close, f"auto close {mins}min before session end",
        )
        runner._auto_save_session()

    def _check_tick_watchdog(self):
        """Delegate tick health check to TickWatchdog and act on the result."""
        wd = self._tick_watchdog
        wd.active = self._live_tick_active
        action = wd.check()

        if action is None:
            return

        mins = wd.elapsed_minutes()

        if action == "session_resubscribe":
            _log("Tick watchdog: session transition — resubscribing for new session")
            self._live_log_msg(
                "新盤重新訂閱 New session — re-subscribing ticks", "status")
            self._resubscribe_ticks()

        elif action == "reconnect":
            _log(f"Tick watchdog: no ticks for {mins}m, forcing full reconnect")
            self._live_log_msg(
                f"強制重連 Force reconnect — no ticks for {mins}m", "status")
            if _com_available:
                try:
                    self._on_disconnected()
                except Exception:
                    self._on_disconnected()

        elif action == "resubscribe":
            _log(f"Tick watchdog: no ticks for {mins}m, re-subscribing")
            self._live_log_msg(
                f"重新訂閱 Re-subscribing ticks — no ticks for {mins}m", "status")
            self._resubscribe_ticks()

        elif action == "warn":
            self._live_log_msg(
                f"警告 No ticks for {mins}m — connection may be lost", "status")
            _log(f"Tick watchdog: no ticks for {mins}m")
            # Check if COM connection is alive
            if _com_available:
                try:
                    ic = skQ.SKQuoteLib_IsConnected()
                    if ic != 1:
                        self._on_disconnected()
                except Exception:
                    self._on_disconnected()

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
        if not is_history or not self._live_history_done:
            # Update watchdog: live ticks always, history ticks only during replay
            self._tick_watchdog.on_tick()

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
        self._live_last_tick_price = price

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

        # Semi-auto / Auto: handle real orders on fills
        if self._trading_mode in ("semi_auto", "auto") and action in ("ENTRY_FILL", "TRADE_CLOSE", "FORCE_CLOSE"):
            self._handle_semi_auto_order(decision)

    # ── Semi-auto real order handling ──

    def _handle_semi_auto_order(self, decision):
        """Handle semi-auto/auto order for a simulated fill.

        Delegates the decision to TradingGuard.decide(), then executes
        the result (send, skip, block, or show dialog).

        Note: callbacks fire via root.after(0) and may arrive after
        _live_runner is cleared during stop. Guard against this.
        """
        # Guard: callback may fire after runner is cleared during stop
        if not self._live_runner:
            return

        action = decision["action"]
        side = decision["side"]
        price = decision["price"]

        guard = self._trading_guard
        verdict, details = guard.decide(self._trading_mode, action, side)
        buy_sell = details["buy_sell"]
        action_type = details["action_type"]
        new_close = details["new_close"]
        order_desc = ("買進 BUY" if buy_sell == 0 else "賣出 SELL")

        symbol = self._live_runner.symbol if self._live_runner else "?"
        order_symbol = _resolve_order_symbol(symbol)

        if verdict == guard.BLOCK_ENTRY:
            self._live_log_msg(
                f"實單暫停 Order blocked: {details['reason']}", "status")
            return

        if verdict == guard.SKIP_EXIT:
            self._live_log_msg(
                f"跳過平倉(無實倉) Skip: {details['reason']}", "status")
            self._log_order_decision("REAL_ORDER_SKIPPED", details["reason"])
            return

        if verdict == guard.SEND_EXIT:
            label = "強制平倉" if action == "FORCE_CLOSE" else "平倉"
            self._live_log_msg(
                f"{label}自動送單 Auto-sending {action.lower()}: {order_desc} {order_symbol}", "exit")
            self._log_order_decision("REAL_ORDER_AUTO", f"{action.lower()} {order_desc} {order_symbol}")
            self._send_real_order(buy_sell, order_symbol, action_type, price, new_close=new_close)
            guard.on_exit_sent()
            return

        if verdict == guard.SEND_ENTRY:
            self._live_log_msg(
                f"自動進場 Auto-sending entry: {order_desc} {order_symbol}", "entry")
            self._log_order_decision("REAL_ORDER_AUTO", f"entry {order_desc} {order_symbol}")
            self._send_real_order(buy_sell, order_symbol, action_type, price, new_close=new_close)
            return

        if verdict == guard.CONFIRM_ENTRY:
            self._show_order_confirm_dialog(
                buy_sell, order_symbol, order_desc, price, action_type, new_close=new_close)

    def _show_order_confirm_dialog(self, buy_sell, order_symbol, order_desc, price, action_type, price_source="", new_close=2):
        """Show a non-blocking 10-second confirmation dialog for a real order."""
        # Dismiss any existing dialog
        self._dismiss_order_dialog("replaced")

        dlg = tk.Toplevel(self.root)
        dlg.title("確認下單 Confirm Order")
        dlg.geometry("440x230")
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)

        # Don't use grab_set — must not block main thread
        self._order_confirm_dlg = dlg
        countdown = [10]  # mutable for closure

        # Header
        color = "#228B22" if buy_sell == 0 else "#DC143C"
        action_label = "進場 ENTRY" if action_type == "entry" else "出場 EXIT"
        header = tk.Label(dlg, text=f"{action_label}: {order_desc}",
                         font=("", 16, "bold"), fg=color)
        header.pack(pady=(15, 5))

        # Order details
        symbol = self._live_runner.symbol if self._live_runner else "?"
        cfg = _SYMBOL_CONFIG.get(symbol, {})
        pv = cfg.get("pv", "?")
        tk.Label(dlg, text=f"商品 Symbol: {order_symbol} ({symbol})  |  每點 PV: {pv} NTD",
                font=("", 11)).pack(pady=2)
        src_label = f" ({price_source})" if price_source else ""
        tk.Label(dlg, text=f"參考價格 Ref Price: {price:,}{src_label}  |  數量 Qty: 1 口",
                font=("", 11)).pack(pady=2)
        tk.Label(dlg, text="實單將以市價IOC送出 Will send as IOC market order",
                font=("", 9), fg="gray").pack(pady=2)

        countdown_label = tk.Label(dlg, text=f"自動跳過 Auto-skip in {countdown[0]}s",
                                  font=("", 10), fg="orange")
        countdown_label.pack(pady=5)

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(pady=10)

        def on_confirm():
            self._dismiss_order_dialog("confirmed")
            self._send_real_order(buy_sell, order_symbol, action_type, price, new_close=new_close)

        def on_skip():
            self._dismiss_order_dialog("skipped")

        ttk.Button(btn_frame, text="確認送出 Confirm", command=on_confirm).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="跳過 Skip", command=on_skip).pack(side=tk.LEFT, padx=10)

        # Audio alert
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass

        # Countdown timer
        def tick():
            if not dlg.winfo_exists():
                return
            countdown[0] -= 1
            if countdown[0] <= 0:
                self._dismiss_order_dialog("timeout")
                return
            countdown_label.config(text=f"自動跳過 Auto-skip in {countdown[0]}s")
            self._order_confirm_timer_id = dlg.after(1000, tick)

        self._order_confirm_timer_id = dlg.after(1000, tick)

        # Handle window close
        dlg.protocol("WM_DELETE_WINDOW", on_skip)

    def _dismiss_order_dialog(self, reason):
        """Close the order confirmation dialog and log the outcome."""
        if self._order_confirm_timer_id and self._order_confirm_dlg:
            try:
                self._order_confirm_dlg.after_cancel(self._order_confirm_timer_id)
            except Exception:
                pass
            self._order_confirm_timer_id = None

        if self._order_confirm_dlg:
            try:
                self._order_confirm_dlg.destroy()
            except Exception:
                pass
            self._order_confirm_dlg = None

        if reason == "timeout":
            self._live_log_msg("實單跳過 Order skipped (timeout 10s)", "status")
            self._log_order_decision("REAL_ORDER_TIMEOUT", "timeout 10s")
        elif reason == "skipped":
            self._live_log_msg("實單跳過 Order skipped by user", "status")
            self._log_order_decision("REAL_ORDER_SKIPPED", "user skipped")
        elif reason == "confirmed":
            pass  # logged in _send_real_order
        # "replaced" = new dialog replaced old one, no log needed

    def _send_real_order(self, buy_sell, order_symbol, action_type, sim_price, new_close=2):
        """Build FUTUREORDER and send via COM SKOrderLib.

        new_close: 0=new position, 1=close position, 2=auto (exchange decides).
        Semi-auto uses 0 for entries and 1 for exits; manual orders use 2.
        """
        if not _com_available or skO is None:
            self._live_log_msg("實單失敗 Order FAILED: COM not available", "exit")
            return
        if not self._futures_account:
            self._live_log_msg("實單失敗 Order FAILED: no futures account", "exit")
            return

        # Margin check for new positions (entries)
        if action_type == "entry" and self._real_rights:
            try:
                available = float(self._real_rights.get("available", "0"))
                symbol = self._live_runner.symbol if self._live_runner else ""
                cfg = _SYMBOL_CONFIG.get(symbol, {})
                required = cfg.get("init_margin", 0)
                allowed, reason = TradingGuard.check_margin(available, required)
                if not allowed:
                    msg = f"保證金不足 {reason}"
                    self._live_log_msg(msg, "exit")
                    self._log_order_decision("REAL_ORDER_BLOCKED", msg)
                    _log(f"REAL ORDER BLOCKED: {msg}")
                    return
            except (ValueError, TypeError):
                pass  # can't parse — proceed with order, let exchange decide

        try:
            import comtypes.gen.SKCOMLib as sk

            oOrder = sk.FUTUREORDER()
            oOrder.bstrFullAccount = self._futures_account
            oOrder.bstrStockNo = order_symbol
            oOrder.sBuySell = buy_sell
            # IOC for entries (cancel if no fill), ROD for exits (must fill)
            oOrder.sTradeType = 1 if action_type == "entry" else 0  # 0=ROD, 1=IOC
            oOrder.sDayTrade = 0      # non-daytrade
            oOrder.bstrPrice = "M"    # market price (M=市價)
            oOrder.nQty = 1           # safety: max 1 contract
            oOrder.sNewClose = new_close  # 0=new, 1=close, 2=auto
            oOrder.sReserved = 0      # during session

            user_id = self.login_user_var.get().strip()
            side_str = "BUY" if buy_sell == 0 else "SELL"
            order_type_str = "IOC" if action_type == "entry" else "ROD"
            self._live_log_msg(
                f"送出實單 Sending: {side_str} {order_symbol} x1 {order_type_str} MKT "
                f"(模擬價={sim_price:,})", action_type)
            _log(f"REAL ORDER: {side_str} {order_symbol} acct={self._futures_account} "
                 f"sim_price={sim_price}")
            nc = oOrder.sNewClose
            nc_label = {0: "new", 1: "close", 2: "auto"}.get(nc, str(nc))
            _log(f"REAL ORDER PARAMS: acct={self._futures_account} stock={order_symbol} "
                 f"buy_sell={buy_sell} trade_type=1(IOC) price=M(MKT) qty=1 newclose={nc}({nc_label}) reserved=0")

            # Synchronous send — returns immediately for IOC market orders
            message, code = skO.SendFutureOrderCLR(user_id, False, oOrder)
            _log(f"REAL ORDER RESULT: code={code} message={message}")

            if code == 0:
                self._live_log_msg(f"實單已送出 Order sent: {message}", action_type)
                _log(f"REAL ORDER OK: {message}")
                self._last_real_order_side = buy_sell  # track for close button
                if action_type == "entry":
                    self._trading_guard.on_entry_sent()
                elif action_type == "exit":
                    self._trading_guard.on_exit_sent()
                self.root.after(3000, self._query_real_account)  # refresh after fill
                self._log_order_decision(
                    "REAL_ORDER_SENT",
                    f"{side_str} {order_symbol} x1 MKT sim={sim_price}",
                )
            else:
                err_msg = skC.SKCenterLib_GetReturnCodeMessage(code) if skC else str(code)
                self._live_log_msg(f"實單失敗 Order FAILED: code={code} {err_msg} | {message}", "exit")
                _log(f"REAL ORDER FAILED: code={code} err={err_msg} msg={message}")
                self._log_order_decision(
                    "REAL_ORDER_FAILED",
                    f"code={code} {err_msg}",
                )

        except Exception as e:
            self._live_log_msg(f"實單異常 Order error: {e}", "exit")
            _log(f"REAL ORDER ERROR: {e}\n{traceback.format_exc()}")
            self._log_order_decision("REAL_ORDER_ERROR", str(e))

    # ── Manual order buttons ──

    def _get_latest_price(self) -> tuple[int, str]:
        """Get the most recent price and its source label."""
        if self._live_last_tick_price:
            return self._live_last_tick_price, "即時 tick"
        if self._live_runner:
            bars_1m = self._live_runner._1m_bars
            if bars_1m:
                return bars_1m[-1].close, "1m bar"
            if self._live_runner._aggregated_bars:
                return self._live_runner._aggregated_bars[-1].close, "agg bar"
        return 0, "N/A"

    def _manual_order(self, buy_sell: int):
        """Send a manual entry order (0=buy, 1=sell) with confirmation dialog."""
        if not self._live_runner:
            return
        symbol = self._live_runner.symbol
        order_symbol = _resolve_order_symbol(symbol)
        order_desc = "買進 BUY" if buy_sell == 0 else "賣出 SELL"
        price, price_src = self._get_latest_price()
        self._show_order_confirm_dialog(buy_sell, order_symbol, order_desc, price, action_type="entry", price_source=price_src)

    def _manual_close(self):
        """Send a manual close (flatten) order — reverse of current position."""
        if not self._live_runner:
            return
        symbol = self._live_runner.symbol
        order_symbol = _resolve_order_symbol(symbol)
        # Priority: real API position > last real order > simulated broker
        if self._real_positions:
            # Use real position from API
            pos = self._real_positions[0]
            buy_sell = 1 if pos["side"] == "B" else 0  # B(long)→SELL, S(short)→BUY
        elif self._last_real_order_side is not None:
            buy_sell = 1 - self._last_real_order_side
        elif self._live_runner.broker.position_size != 0:
            side = self._live_runner.broker.trades[-1].side.value if self._live_runner.broker.trades else "LONG"
            buy_sell = 1 if side == "LONG" else 0
        else:
            self._live_log_msg("無持倉紀錄 No position record — use BUY or SELL directly", "status")
            return
        order_desc = "平倉賣 CLOSE SELL" if buy_sell == 1 else "平倉買 CLOSE BUY"
        price, price_src = self._get_latest_price()
        self._show_order_confirm_dialog(buy_sell, order_symbol, order_desc, price, action_type="exit", price_source=price_src)

    def _update_manual_order_buttons(self):
        """Enable/disable manual order buttons based on live state."""
        if (self._live_runner
                and self._live_runner.state == LiveState.RUNNING
                and self._logged_in
                and self._futures_account):
            state = tk.NORMAL
        else:
            state = tk.DISABLED
        for btn in (self.btn_manual_buy, self.btn_manual_sell, self.btn_manual_close):
            btn.config(state=state)

    def _query_real_account(self):
        """Query real account positions, equity, and fills from Capital API."""
        if not _com_available or skO is None or not self._logged_in or not self._futures_account:
            return
        user_id = self.login_user_var.get().strip()
        try:
            self._real_positions.clear()  # will be rebuilt from callbacks
            skO.GetOpenInterestGW(user_id, self._futures_account, 1)
            skO.GetFutureRights(user_id, self._futures_account, 1)  # 1=TWD
            # Synchronous order/fill queries
            try:
                # GetOrderReport(5) = filled orders (已成) — has price/qty
                orders_raw = skO.GetOrderReport(user_id, self._futures_account, 5)
                self._parse_and_display_fills(orders_raw, "委託(已成)")
                # Also get FulfillReport for comparison
                fills_raw = skO.GetFulfillReport(user_id, self._futures_account, 4)
                self._parse_and_display_fills(fills_raw, "成交(同商品)")
            except Exception as e:
                _log(f"成交查詢失敗 Fills query error: {e}")
        except Exception as e:
            _log(f"帳戶查詢失敗 Account query error: {e}")

    def _start_account_polling(self):
        """Start polling real account data every 30 seconds."""
        if self._real_account_poll_id:
            self.root.after_cancel(self._real_account_poll_id)
        self._query_real_account()
        self._real_account_poll_id = self.root.after(30000, self._start_account_polling)

    def _stop_account_polling(self):
        """Stop the account polling timer."""
        if self._real_account_poll_id:
            self.root.after_cancel(self._real_account_poll_id)
            self._real_account_poll_id = None

    def _update_real_account_display(self):
        """Update the real account UI labels from parsed data."""
        # Positions
        if self._real_positions:
            parts = []
            for p in self._real_positions:
                side = "LONG" if p["side"] == "B" else "SHORT"
                parts.append(f"{side} x{p['qty']} {p['product']} @{p['avg_cost']}")
            self.real_pos_var.set(" | ".join(parts))
        # Equity / balance
        r = self._real_rights
        if r:
            self.real_equity_var.set(f"{_fmt_money(r.get('equity', '--'))}")
            self.real_available_var.set(f"{_fmt_money(r.get('available', '--'))}")
            self.real_pnl_var.set(f"{_fmt_money(r.get('float_pnl', '--'))}")
            self.real_realized_var.set(f"{_fmt_money(r.get('realized_pnl', '--'))}")
            # Fee + Tax combined, and Net P&L
            try:
                realized = int(float(r.get('realized_pnl', '0') or '0'))
                fee = int(float(r.get('realized_cost', '0') or '0'))
                tax = int(float(r.get('tax', '0') or '0'))
                float_pnl = int(float(r.get('float_pnl', '0') or '0'))
                self.real_fees_var.set(f"{fee + tax:,} ({fee:,}+{tax:,})")
                # Net = realized P&L - fees - tax + floating
                net = realized - fee - tax + float_pnl
                self.real_net_var.set(f"{net:+,}")
                # Update daily loss limit display and check
                guard = self._trading_guard
                if self._trading_mode in ("semi_auto", "auto") and guard.daily_loss_limit > 0:
                    if guard.paused:
                        self.real_loss_limit_var.set(
                            f"{net:+,} / -{guard.daily_loss_limit:,} [已暫停 PAUSED]")
                    else:
                        self.real_loss_limit_var.set(
                            f"{net:+,} / -{guard.daily_loss_limit:,}")
                    triggered = guard.update_pnl(net)
                    if triggered:
                        self._live_log_msg(
                            f"已達每日虧損上限 Daily loss limit: net={net:+,} "
                            f"< -{guard.daily_loss_limit:,} NTD — 實單暫停 orders paused",
                            "exit")
                else:
                    self.real_loss_limit_var.set("--")
            except (ValueError, TypeError):
                self.real_fees_var.set(f"{r.get('realized_cost', '--')}+{r.get('tax', '--')}")
                self.real_net_var.set("--")
            maint = r.get("maint_rate", "--")
            self.real_maint_var.set(f"{maint}%")

    def _parse_and_display_fills(self, fills_raw, label="成交"):
        """Parse GetFulfillReport/GetOrderReport result and display."""
        # COM may return (string, code) tuple or just string
        if isinstance(fills_raw, (tuple, list)):
            fills_raw = fills_raw[0] if fills_raw else ""
        if not fills_raw or not isinstance(fills_raw, str):
            _log(f"{label}(raw type): {type(fills_raw)}")
            return
        lines = [l.strip() for l in fills_raw.split("\n") if l.strip()]
        if not lines or lines[0].startswith("001") or lines[0].startswith("##"):
            if "成交" in label:
                self.real_fills_var.set("無成交 No trades today")
            return
        # Parse FulfillReport(4) fields: [8]=product [15]=B/S [19]=price [20]=qty [21]=N/O [22]=session [23]=date
        parsed_fills = []
        for line in lines:
            fields = line.split(",")
            if len(fields) >= 24:
                side = fields[15].strip()
                price = fields[19].strip()
                qty = fields[20].strip()
                new_close = fields[21].strip()  # N=new, O=close/offset
                date = fields[23].strip()
                side_str = "BUY" if side == "B" else "SELL"
                nc_str = "開" if new_close == "N" else "平"
                try:
                    price_f = float(price)
                    parsed_fills.append(f"{date} {side_str}{nc_str} x{qty} @{price_f:,.1f}")
                except ValueError:
                    parsed_fills.append(f"{date} {side_str}{nc_str} x{qty} @{price}")
        if "成交" in label:
            self.real_fills_var.set(f"{len(parsed_fills)} 筆 trades")
        # Log new trades to live event log (only once, not on every poll)
        count_key = f"_prev_{label}_count"
        prev_count = getattr(self, count_key, 0)
        if self._live_runner and len(parsed_fills) > prev_count:
            for fill in parsed_fills[prev_count:]:
                self._live_log_msg(f"實{label}: {fill}", "entry")
        setattr(self, count_key, len(parsed_fills))

    def _log_order_decision(self, action: str, reason: str) -> None:
        """Log a real-order event to the CSV decision log."""
        if not self._live_runner or not self._live_runner.csv_logger:
            return
        now = _taipei_now()
        self._live_runner.csv_logger.log_decision(
            dt=now,
            bar_dt=now,
            strategy=self._live_runner.strategy_display_name,
            action=action,
            side="",
            tag="",
            price=0,
            reason=reason,
        )

    def _live_log_msg(self, msg, tag="status"):
        """Append message to the Live tab log."""
        tpe = _taipei_now()
        local = datetime.now()
        ts_tpe = tpe.strftime("%H:%M:%S")
        ts_local = local.strftime("%H:%M:%S")
        if ts_tpe == ts_local:
            line = f"[{ts_tpe}] {msg}\n"
        else:
            line = f"[{ts_tpe} TPE / {ts_local} local] {msg}\n"
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
            self.btn_chart_all.config(state=tk.NORMAL)
        if result.trades:
            self.btn_export.config(state=tk.NORMAL)
            self.btn_review.config(state=tk.NORMAL)

        # Update trade list and metrics
        self._display_results(result, bars)

    def _stop_live(self):
        """Stop the live bot and restore UI."""
        self._stop_account_polling()
        if self._live_poll_id:
            self.root.after_cancel(self._live_poll_id)
            self._live_poll_id = None
        if self._reconnect_timer_id:
            self.root.after_cancel(self._reconnect_timer_id)
            self._reconnect_timer_id = None
        self._reconnect_attempt = 0

        self._live_warmup_mode = False
        self._live_polling = False
        self._live_tick_active = False
        self._tick_watchdog.reset()
        self._live_bar_builder = None
        self._live_tick_symbol = ""
        self._live_tick_com_symbol = ""
        self._live_last_tick_price = 0
        self._live_history_done = False
        self._live_tick_count = 0
        self._live_history_tick_count = 0

        # Close live chart before stopping runner
        if self._live_chart and self._live_chart.is_alive:
            self._live_chart.close()
        self._live_chart = None

        if self._live_runner:
            # Close real position before stopping if semi-auto has one open.
            # Must happen BEFORE runner.stop() which resets state asynchronously.
            if (self._trading_mode in ("semi_auto", "auto")
                    and self._trading_guard.real_entry_confirmed
                    and self._live_runner.broker.position_size > 0):
                runner = self._live_runner
                side_val = runner.broker.position_side.value if runner.broker.position_size > 0 else "LONG"
                buy_sell = 1 if side_val == "LONG" else 0  # reverse to close
                order_symbol = _resolve_order_symbol(runner.symbol)
                price = runner._aggregated_bars[-1].close if runner._aggregated_bars else 0
                order_desc = "賣出 SELL" if buy_sell == 1 else "買進 BUY"
                self._live_log_msg(
                    f"停止平倉 Stop-close: {order_desc} {order_symbol}", "exit")
                self._log_order_decision("REAL_ORDER_AUTO", f"stop_close {order_desc} {order_symbol}")
                self._send_real_order(buy_sell, order_symbol, "exit", price, new_close=1)
                self._trading_guard.on_exit_sent()

            # Suppress strategy before stop — prevent new entries during shutdown.
            # stop() flushes the aggregator's partial bar and runs _process_aggregated_bar,
            # which would execute the strategy and potentially generate new entries.
            self._live_runner.suppress_strategy = True
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
            self._trading_guard.reset()

        # Restore UI
        self.btn_deploy.config(text="部署機器人 Deploy Bot")
        self._update_manual_order_buttons()
        self.chart_tf_combo.config(state=tk.DISABLED)
        self.chart_tf_var.set("Native")
        if self._quote_connected:
            self.btn_api.config(state=tk.NORMAL)
            self.btn_deploy.config(state=tk.NORMAL)
        self.btn_tv.config(state=tk.NORMAL)
        self.symbol_combo.config(state="readonly")
        self.strategy_combo.config(state="readonly")
        self.bot_name_var.set("(未設定 Not set)")
        self.trading_mode_var.set("--")
        self.status_var.set("就緒 Ready")


def main():
    _init_com()

    if _com_available:
        import comtypes.client
        SKQuoteEvent = SKQuoteLibEvents()
        SKQuoteLibEventHandler = comtypes.client.GetEvents(skQ, SKQuoteEvent)
        SKReplyEvent = SKReplyLibEvent()
        SKReplyLibEventHandler = comtypes.client.GetEvents(skR, SKReplyEvent)
        if skO is not None:
            SKOrderEvent = SKOrderLibEvents()
            SKOrderLibEventHandler = comtypes.client.GetEvents(skO, SKOrderEvent)

    root = tk.Tk()
    app = BacktestApp(root)
    root.mainloop()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
