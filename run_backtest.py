"""Backtest GUI: Run backtest with results display.

Data sources (in priority order):
  1. Capital API (COM) — if available and connected
  2. Cached CSV from data/ — auto-selected by strategy timeframe
  3. TradingView download — via TV Data button

Usage:
  python run_backtest.py
"""

import os
import sys
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog
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
from src.backtest.chart import plot_backtest, _MPF_AVAILABLE
from src.strategy.examples.h4_bollinger_long import H4BollingerLongStrategy
from src.strategy.examples.h4_bollinger_atr_long import H4BollingerAtrLongStrategy
from src.strategy.examples.daily_bollinger_long import DailyBollingerLongStrategy

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
            break
    return cfg


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
        self.root.title("tai-robot 回測系統 Backtest System")
        self.root.geometry("1200x800")
        self.root.minsize(1000, 650)

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

        self._build_ui()

        if _com_available:
            self.root.after(200, self._do_login)
        else:
            # Always enable Run Backtest — will auto-load cached CSV
            self.btn_fetch.config(state=tk.NORMAL)
            self.status_var.set("就緒 Ready (cached data)")

    def _build_ui(self):
        # ── Control panel ──
        ctrl = ttk.LabelFrame(self.root, text="回測設定 Backtest Settings", padding=8)
        ctrl.pack(fill=tk.X, padx=8, pady=(8, 4))

        # Row 0: Symbol + KLine type
        ttk.Label(ctrl, text="商品 Symbol:").grid(row=0, column=0, sticky=tk.W, padx=4)
        self.symbol_var = tk.StringVar(value="TX00")
        ttk.Entry(ctrl, textvariable=self.symbol_var, width=10).grid(row=0, column=1, padx=4)

        ttk.Label(ctrl, text="策略 Strategy:").grid(row=0, column=2, sticky=tk.W, padx=4)
        self.strategy_var = tk.StringVar(value=list(STRATEGIES.keys())[0])
        ttk.Combobox(ctrl, textvariable=self.strategy_var, width=22,
                     state="readonly", values=list(STRATEGIES.keys())).grid(row=0, column=3, padx=4)

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

        # ── Results area ──
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

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

        self._last_result = None
        self._last_bars: list[Bar] = []

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
            # All chunks done, run backtest
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

        # Store params for callback
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

            # Convert DataFrame to Bar list
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
            # Fetch next chunk
            self.root.after(500, lambda: self._fetch_next_chunk(
                self._fetch_symbol, self._fetch_kline_type, self._fetch_minute_num))
        else:
            # All chunks done
            self._run_backtest()

    def _get_strategy_interval(self) -> int:
        strategy_cls = STRATEGIES.get(self.strategy_var.get())
        if not strategy_cls:
            return 14400
        kt = strategy_cls.kline_type
        km = strategy_cls.kline_minute
        return INTERVAL_SECONDS.get((kt, km), 14400)

    def _merge_cached_data(self, bars: list[Bar], symbol: str, interval: int) -> list[Bar]:
        """Merge API bars with cached data for longer history.

        Priority: API data first. Cache fills gaps before/after API range.
        API and cache may have different bar alignments, so they are NOT mixed
        within the same time period — only stitched at boundaries.
        Date filtering is handled later by _execute_backtest().
        """
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
            # Use cached BEFORE API range + API + cached AFTER API range
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
            # No API data, use cache only
            merged = cached_bars
            _log(f"使用快取 Using cache only: {len(merged)} bars")

        return merged

    def _run_backtest(self):
        """Called after all KLine data arrives from COM API."""
        symbol = self.symbol_var.get().strip()
        interval = self._get_strategy_interval()

        _log(f"解析K線資料 Parsing {len(self.kline_data)} KLine strings...")
        bars = parse_kline_strings(self.kline_data, symbol=symbol, interval=interval)

        # Deduplicate bars by datetime (overlapping chunks may return same bars)
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

        # Merge with cached TradingView data for longer history
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
        if not cache_file:
            self.status_var.set(f"無快取資料 No cached data for type={kt} min={km}")
            return

        cache_path = os.path.join(_CACHE_DIR, cache_file)
        if not os.path.exists(cache_path):
            self.status_var.set(f"找不到資料 File not found: {cache_file}")
            return

        symbol = self.symbol_var.get().strip()
        interval = self._get_strategy_interval()

        _log(f"載入快取 Loading cached data: {cache_file}")
        bars = load_bars_from_csv(cache_path, symbol=symbol, interval=interval)
        _log(f"載入完成 Loaded {len(bars)} bars from {cache_file}")

        self._execute_backtest(bars)

    def _execute_backtest(self, bars: list[Bar]):
        if not bars:
            self.status_var.set("無資料 No data")
            self.btn_fetch.config(state=tk.NORMAL)
            return

        # Apply date filter from GUI (if bars span wider than requested range)
        try:
            dt_start = datetime.strptime(self.start_var.get().strip(), "%Y%m%d")
            dt_end = datetime.strptime(self.end_var.get().strip(), "%Y%m%d") + timedelta(days=1)
            before = len(bars)
            bars = [b for b in bars if dt_start <= b.dt < dt_end]
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

        # Pass the right params based on strategy type
        if strategy_cls is H4BollingerAtrLongStrategy:
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

        result = engine.run(bars)

        # Recalculate metrics with initial balance
        result.metrics = calculate_metrics(
            result.trades, result.equity_curve, initial_balance=initial_balance)

        self._last_result = result
        self._last_bars = bars
        self._display_results(result, bars)

        self.btn_fetch.config(state=tk.NORMAL)
        self.btn_export.config(state=tk.NORMAL)
        if _MPF_AVAILABLE and result.trades:
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

            # Look up bar datetimes
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
        """Return 0-based trade index from selected row in trade_tree, or None."""
        sel = self.trade_tree.selection()
        if not sel:
            return None
        values = self.trade_tree.item(sel[0], "values")
        if values:
            return int(values[0]) - 1  # column 0 is 1-based "#"
        return None

    def _chart_kwargs(self) -> dict:
        """Common kwargs for plot_backtest calls."""
        try:
            bb_period = int(self.bb_period_var.get())
            bb_std = float(self.bb_std_var.get())
        except ValueError:
            bb_period, bb_std = 20, 2.0
        return dict(bb_period=bb_period, bb_std=bb_std)

    def _show_chart(self):
        """Show chart zoomed to the selected trade, or first trade if none selected."""
        if not self._last_result or not self._last_bars:
            return
        try:
            strategy_name = self.strategy_var.get()
            focus = self._get_selected_trade_index()
            if focus is None:
                focus = 0  # default to first trade
            plot_backtest(self._last_bars, self._last_result.trades,
                          title=strategy_name, focus_trade_index=focus,
                          **self._chart_kwargs())
        except ImportError as e:
            self.status_var.set(str(e))
            _log(f"圖表錯誤 Chart error: {e}")
        except Exception as e:
            _log(f"圖表錯誤 Chart error: {e}")
            self.status_var.set(f"圖表錯誤 Chart error: {e}")

    def _show_chart_all(self):
        """Show full chart with all bars and trades."""
        if not self._last_result or not self._last_bars:
            return
        try:
            strategy_name = self.strategy_var.get()
            plot_backtest(self._last_bars, self._last_result.trades,
                          title=strategy_name, **self._chart_kwargs())
        except ImportError as e:
            self.status_var.set(str(e))
            _log(f"圖表錯誤 Chart error: {e}")
        except Exception as e:
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
