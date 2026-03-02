"""AI Strategy Workbench: Chat with Claude to generate, backtest, and export strategies.

Data sources (in priority order):
  1. Capital API (COM) — if available and connected
  2. Cached CSV from data/ — auto-selected by strategy timeframe
  3. TradingView download — via TV Data button

Usage:
  python run_backtest.py
"""

import os
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, simpledialog
from datetime import datetime, timedelta

# Ensure src is importable
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

try:
    import yaml
except ImportError:
    yaml = None

from src.market_data.models import Bar
from src.backtest.engine import BacktestEngine
from src.backtest.data_loader import parse_kline_strings, load_bars_from_csv
from src.backtest.report import format_report, export_trades_csv
from src.backtest.metrics import calculate_metrics
from src.backtest.strategy import BacktestStrategy
from src.backtest.chart import plot_backtest, _LWC_AVAILABLE
from src.strategy.examples.h4_bollinger_long import H4BollingerLongStrategy
from src.strategy.examples.h4_bollinger_atr_long import H4BollingerAtrLongStrategy
from src.strategy.examples.daily_bollinger_long import DailyBollingerLongStrategy
from src.strategy.examples.h4_midline_touch_long import H4MidlineTouchLongStrategy

# AI modules
from src.ai.chat_client import ChatClient, PROVIDER_ANTHROPIC, PROVIDER_GOOGLE, DEFAULT_MODELS
from src.ai.prompts import STRATEGY_SYSTEM_PROMPT, STRATEGY_CODE_CONTEXT
from src.ai.code_sandbox import (
    extract_python_code, load_strategy_from_source,
    CodeValidationError, CodeExecutionError,
)
from src.ai.strategy_store import StrategyStore
from src.ai.pine_exporter import export_to_pine

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
    "H4 布林多單 H4 Bollinger Long": H4BollingerLongStrategy,
    "H4 布林ATR多單 H4 Bollinger ATR Long": H4BollingerAtrLongStrategy,
    "日線布林多單 Daily Bollinger Long": DailyBollingerLongStrategy,
    "H4 中線戰法多單 H4 Midline Touch Long": H4MidlineTouchLongStrategy,
}

# ── COM setup (only if not using CSV mode) ──
_com_available = False
skC = skQ = skR = None

def _init_com():
    global _com_available, skC, skQ, skR
    try:
        import comtypes
        import comtypes.client

        libs_path = os.path.abspath(os.path.join(
            project_root,
            "CapitalAPI_2.13.57", "CapitalAPI_2.13.57_PythonExample",
            "SKDLLPythonTester", "libs",
        ))
        os.add_dll_directory(libs_path)
        os.environ["PATH"] = libs_path + os.pathsep + os.environ.get("PATH", "")

        dll_path = os.path.join(libs_path, "SKCOM.dll")
        comtypes.client.GetModule(dll_path)
        import comtypes.gen.SKCOMLib as sk

        skC = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
        skQ = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
        skR = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
        _com_available = True
    except Exception as e:
        print(f"COM not available: {e}")
        _com_available = False


def _load_settings():
    cfg = {"user_id": "", "password": "", "authority_flag": 0}
    for name in ("settings.yaml", "settings.example.yaml"):
        path = os.path.join(project_root, name)
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

# Cached data files (downloaded from TradingView)
_CACHE_DIR = os.path.join(project_root, "data")
_CACHE_FILES = {
    (0, 15): "TXF1_15m.csv",
    (0, 60): "TXF1_1H.csv",
    (0, 240): "TXF1_H4.csv",
    (4, 1): "TXF1_1D.csv",
}

_app = None


def should_reuse_bars(
    raw_bars: list, raw_bars_key: tuple,
    kline_type: int, kline_minute: int,
) -> bool:
    """Return True if raw_bars can be reused for the given timeframe."""
    if not raw_bars:
        return False
    return raw_bars_key == (kline_type, kline_minute)


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
                _app.btn_fetch.config(state=tk.NORMAL)
                _app.status_var.set("已連線 Connected - Ready")
            elif nKind == 3002 and nCode == 0 and not _app._quote_connected:
                _app._quote_connected = True
                _app.btn_fetch.config(state=tk.NORMAL)
                _app.status_var.set("已連線 Connected (Quote) - Ready")

    def OnNotifyKLineData(self, bstrStockNo, bstrData):
        if _app:
            _app.kline_data.append(bstrData)
            _app._chunk_bar_count += 1
            n = len(_app.kline_data)
            if n <= 3:
                _log(f"K線原始資料 Raw KLine [{n}]: {bstrData!r}")

    def OnKLineComplete(self, nCode):
        chunk_n = _app._chunk_bar_count if _app else 0
        total_n = len(_app.kline_data) if _app else 0
        _log(f"K線完成 KLine complete: chunk={chunk_n} bars, total={total_n}, code={nCode}")
        if _app:
            _app.root.after(100, _app._on_chunk_complete)

    def OnNotifyQuoteLONG(self, sMarketNo, nStockIdx):
        pass

    def OnNotifyTicksLONG(self, sMarketNo, nStockIdx, nPtr):
        pass

    def OnNotifyBest5LONG(self, sMarketNo, nStockIdx):
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

        if _com_available:
            self.root.after(200, self._do_login)
        else:
            self.btn_fetch.config(state=tk.NORMAL)
            self.status_var.set("就緒 Ready (cached data)")

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
        self._raw_bars_key: tuple = ()  # (kline_type, kline_minute) of stored raw bars

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
        ttk.Entry(ctrl, textvariable=self.symbol_var, width=10).grid(row=0, column=1, padx=4)

        ttk.Label(ctrl, text="策略 Strategy:").grid(row=0, column=2, sticky=tk.W, padx=4)
        self.strategy_var = tk.StringVar(value=list(STRATEGIES.keys())[0])
        self.strategy_combo = ttk.Combobox(ctrl, textvariable=self.strategy_var, width=30,
                                            state="readonly", values=list(STRATEGIES.keys()))
        self.strategy_combo.grid(row=0, column=3, padx=4)

        ttk.Label(ctrl, text="初始資金 Balance:").grid(row=0, column=4, sticky=tk.W, padx=4)
        self.balance_var = tk.StringVar(value="1000000")
        ttk.Entry(ctrl, textvariable=self.balance_var, width=12).grid(row=0, column=5, padx=4)

        ttk.Label(ctrl, text="每點價值 Pt Value:").grid(row=0, column=6, sticky=tk.W, padx=4)
        self.pv_var = tk.StringVar(value="200")
        ttk.Entry(ctrl, textvariable=self.pv_var, width=6).grid(row=0, column=7, padx=4)

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

        # Row 4: Action buttons
        btn_frame = ttk.Frame(ctrl)
        btn_frame.grid(row=4, column=0, columnspan=8, pady=(8, 0))

        self.btn_fetch = ttk.Button(btn_frame, text="開始回測 Run Backtest",
                                    command=self._do_fetch, state=tk.DISABLED)
        self.btn_fetch.pack(side=tk.LEFT, padx=4)

        self.btn_tv = ttk.Button(btn_frame, text="TradingView資料 TV Data",
                                 command=self._fetch_tradingview,
                                 state=tk.NORMAL if _tv_available else tk.DISABLED)
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
                self.root.after(0, lambda: self._on_chat_error(str(e)))

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
                self.root.after(0, lambda: self._on_chat_error(str(e)))

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
                self.root.after(0, lambda: self._on_chat_error(str(e)))

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

    # ── COM Login ──

    def _do_login(self):
        try:
            user_id = self._settings["user_id"]
            password = self._settings["password"]
            authority_flag = self._settings.get("authority_flag", 0)

            log_dir = os.path.join(project_root, "CapitalLog_Backtest")
            skC.SKCenterLib_SetLogPath(log_dir)

            if authority_flag:
                skC.SKCenterLib_SetAuthority(authority_flag)

            _log(f"登入中 Logging in as {user_id}...")
            self.status_var.set("登入中 Logging in...")

            code = skC.SKCenterLib_LoginSetQuote(user_id, password, "Y")
            if code != 0 and not (2000 <= code < 3000):
                msg = skC.SKCenterLib_GetReturnCodeMessage(code)
                _log(f"登入失敗 LOGIN FAILED: code={code} {msg}")
                self.status_var.set(f"登入失敗 Login failed: {msg}")
                return
            self._logged_in = True
            _log(f"登入成功 LOGIN OK (code={code})")

            skR.SKReplyLib_ConnectByID(user_id)
            code = skQ.SKQuoteLib_EnterMonitorLONG()
            _log(f"進入報價監控 EnterMonitorLONG: code={code}")
            self.status_var.set("連線中 Connecting...")
            self.root.after(3000, self._check_connection)

        except Exception as e:
            _log(f"初始化錯誤 Init error: {e}")
            self.status_var.set(f"錯誤 Error: {e}")

    def _check_connection(self):
        try:
            ic = skQ.SKQuoteLib_IsConnected()
            if ic == 1:
                self._quote_connected = True
                self.btn_fetch.config(state=tk.NORMAL)
                self.status_var.set("已連線 Connected - Ready")
            elif not self._quote_connected:
                self.root.after(2000, self._check_connection)
        except Exception as e:
            _log(f"連線檢查錯誤: {e}")

    # ── Data fetch ──

    def _do_fetch(self):
        # If we have previously downloaded bars matching this strategy's timeframe, reuse
        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if strategy_cls and should_reuse_bars(
            self._raw_bars, self._raw_bars_key,
            strategy_cls.kline_type, strategy_cls.kline_minute,
        ):
            _log("重新使用已下載資料 Re-using downloaded data with date filter")
            self._execute_backtest(list(self._raw_bars))
            return

        if not _com_available:
            self._run_backtest_cached()
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

        # Split into 80-day chunks (API returns ~3 months max for intraday)
        try:
            dt_start = datetime.strptime(start_date, "%Y%m%d")
            dt_end = datetime.strptime(end_date, "%Y%m%d")
        except ValueError:
            self.status_var.set("日期格式錯誤 Date format error (YYYYMMDD)")
            return

        chunk_days = 80
        self._fetch_chunks = []
        cursor = dt_start
        while cursor < dt_end:
            chunk_end = min(cursor + timedelta(days=chunk_days), dt_end)
            self._fetch_chunks.append((cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
            cursor = chunk_end + timedelta(days=1)

        self.kline_data = []
        self._fetch_chunk_idx = 0
        self.btn_fetch.config(state=tk.DISABLED)

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
                    self.btn_fetch.config(state=tk.NORMAL)
                    return

        except Exception as e:
            _log(f"查詢錯誤 Fetch error: {e}")
            self.btn_fetch.config(state=tk.NORMAL)

    def _fetch_tradingview(self):
        """Fetch historical data from TradingView (up to 5000 bars, ~3+ years for H4)."""
        if not _tv_available:
            self.status_var.set("需安裝 tvDatafeed: pip install tradingview-datafeed")
            return

        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            return

        kt = strategy_cls.kline_type
        km = strategy_cls.kline_minute
        tv_interval_name = _TV_INTERVALS.get((kt, km))
        if not tv_interval_name:
            self.status_var.set(f"TradingView不支援此週期 Unsupported interval: type={kt} min={km}")
            return

        tv_interval = getattr(TvInterval, tv_interval_name)
        symbol = self.symbol_var.get().strip().replace("TX00", "TXF1!")
        exchange = "TAIFEX"
        interval = INTERVAL_SECONDS.get((kt, km), 14400)

        self.btn_tv.config(state=tk.DISABLED)
        self.btn_fetch.config(state=tk.DISABLED)
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
                self.btn_tv.config(state=tk.NORMAL)
                self.btn_fetch.config(state=tk.NORMAL)
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
            self.btn_tv.config(state=tk.NORMAL)
            self._execute_backtest(bars)

        except Exception as e:
            _log(f"TradingView錯誤 TV error: {e}")
            self.status_var.set(f"TradingView錯誤: {e}")
            self.btn_tv.config(state=tk.NORMAL)
            self.btn_fetch.config(state=tk.NORMAL)

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

    def _merge_cached_data(self, bars: list[Bar], symbol: str, interval: int) -> list[Bar]:
        """Merge API bars with cached data for longer history."""
        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            return bars

        kt = strategy_cls.kline_type
        km = strategy_cls.kline_minute
        cache_file = _CACHE_FILES.get((kt, km))
        if not cache_file:
            return bars

        cache_path = os.path.join(_CACHE_DIR, cache_file)
        if not os.path.exists(cache_path):
            return bars

        cached_bars = load_bars_from_csv(cache_path, symbol=symbol, interval=interval)
        if not cached_bars:
            return bars

        _log(f"快取資料 Cache: {len(cached_bars)} bars from {cache_file} "
             f"({cached_bars[0].dt} ~ {cached_bars[-1].dt})")

        if bars:
            api_start = bars[0].dt
            api_end = bars[-1].dt
            cached_before = [b for b in cached_bars if b.dt < api_start]
            cached_after = [b for b in cached_bars if b.dt > api_end]
            merged = cached_before + bars + cached_after
            parts = []
            if cached_before:
                parts.append(f"{len(cached_before)} cached(before)")
            parts.append(f"{len(bars)} API")
            if cached_after:
                parts.append(f"{len(cached_after)} cached(after)")
            _log(f"合併完成 Merged: {' + '.join(parts)} = {len(merged)} total")
        else:
            merged = cached_bars
            _log(f"使用快取 Using cache only: {len(merged)} bars")

        return merged

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

        bars = self._merge_cached_data(bars, symbol, interval)

        if bars:
            _log(f"最終資料 Final: {len(bars)} bars: {bars[0].dt} ~ {bars[-1].dt}")
        else:
            _log("無資料 No data available (API or cache)")

        self._execute_backtest(bars)

    def _run_backtest_cached(self):
        """Load cached CSV matching the selected strategy's timeframe and run backtest."""
        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            self.status_var.set("請選擇策略 Select a strategy")
            return

        kt = strategy_cls.kline_type
        km = strategy_cls.kline_minute
        cache_file = _CACHE_FILES.get((kt, km))

        # Try cached CSV first
        if cache_file:
            cache_path = os.path.join(_CACHE_DIR, cache_file)
            if os.path.exists(cache_path):
                symbol = self.symbol_var.get().strip()
                interval = self._get_strategy_interval()
                _log(f"載入快取 Loading cached data: {cache_file}")
                bars = load_bars_from_csv(cache_path, symbol=symbol, interval=interval)
                _log(f"載入完成 Loaded {len(bars)} bars from {cache_file}")
                self._execute_backtest(bars)
                return

        # Fall back to previously downloaded data (e.g. from TV download)
        if self._raw_bars:
            _log("重新使用已下載資料 Re-using previously downloaded data")
            self._execute_backtest(self._raw_bars)
            return

        self.status_var.set(f"無資料 No data. Use 'TV Data' to download first.")
        self.btn_fetch.config(state=tk.NORMAL)

    def _execute_backtest(self, bars: list[Bar]):
        if not bars:
            self.status_var.set("無資料 No data")
            self.btn_fetch.config(state=tk.NORMAL)
            return

        # Store raw bars for re-running with different date ranges
        self._raw_bars = bars
        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if strategy_cls:
            self._raw_bars_key = (strategy_cls.kline_type, strategy_cls.kline_minute)

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

        if not bars:
            self.status_var.set("篩選後無資料 No data after date filter")
            self.btn_fetch.config(state=tk.NORMAL)
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
            self.btn_fetch.config(state=tk.NORMAL)
            return

        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            self.status_var.set("請選擇策略 Select a strategy")
            self.btn_fetch.config(state=tk.NORMAL)
            return

        # Instantiate strategy: AI strategies use defaults, built-in ones use GUI params
        strategy_name = self.strategy_var.get()
        if strategy_name.startswith("AI:"):
            # AI-generated strategies have params baked into __init__ defaults
            strategy = strategy_cls()
        elif strategy_cls is H4BollingerAtrLongStrategy:
            strategy = strategy_cls(
                bb_period=bb_period, bb_std=bb_std,
                atr_period=atr_period, sl_mult=sl_mult, tp_mult=tp_mult,
            )
        else:
            strategy = strategy_cls(
                bb_period=bb_period, bb_std=bb_std,
                sl_offset=sl_offset, tp_offset=tp_offset,
            )
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
            self.btn_fetch.config(state=tk.NORMAL)
            return

        # Recalculate metrics with initial balance
        result.metrics = calculate_metrics(
            result.trades, result.equity_curve, initial_balance=initial_balance)

        self._last_result = result
        self._last_bars = bars
        self._display_results(result, bars)

        self.btn_fetch.config(state=tk.NORMAL)
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
        if not self._last_result or not self._last_bars:
            return
        strategy_name = self.strategy_var.get()
        focus = self._get_selected_trade_index()
        if focus is None:
            focus = 0
        kwargs = self._chart_kwargs()
        bars = list(self._last_bars)
        trades = list(self._last_result.trades)
        threading.Thread(
            target=self._run_chart, daemon=True,
            args=(bars, trades, strategy_name, focus, kwargs),
        ).start()

    def _show_chart_all(self):
        if not self._last_result or not self._last_bars:
            return
        strategy_name = self.strategy_var.get()
        kwargs = self._chart_kwargs()
        bars = list(self._last_bars)
        trades = list(self._last_result.trades)
        threading.Thread(
            target=self._run_chart, daemon=True,
            args=(bars, trades, strategy_name, None, kwargs),
        ).start()

    def _run_chart(self, bars, trades, title, focus, kwargs):
        try:
            plot_backtest(bars, trades, title=title,
                          focus_trade_index=focus, **kwargs)
        except ImportError as e:
            self.root.after(0, lambda: self.status_var.set(str(e)))
            _log(f"圖表錯誤 Chart error: {e}")
        except Exception as e:
            self.root.after(0, lambda: self.status_var.set(f"圖表錯誤 Chart error: {e}"))
            _log(f"圖表錯誤 Chart error: {e}")
            self.status_var.set(f"圖表錯誤 Chart error: {e}")

    def _do_export(self):
        if not self._last_result or not self._last_result.trades:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"backtest_trades_{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
        if path:
            export_trades_csv(self._last_result.trades, path)
            self.status_var.set(f"已匯出 Exported: {path}")
            _log(f"匯出交易 Exported trades to {path}")


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
    main()
