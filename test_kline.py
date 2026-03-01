"""K-Line Historical Data from Capital API (COM + Tkinter message pump).

Follows the exact same pattern as the official Quote.py example:
  - COM objects created at module level
  - Event handlers registered at module level
  - Tkinter mainloop as the Windows message pump

Usage:
  python test_kline.py
"""

import os
import sys
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, timedelta
import csv

try:
    import yaml
except ImportError:
    yaml = None

try:
    import comtypes
    import comtypes.client
except ImportError:
    print("ERROR: comtypes not installed. Run: pip install comtypes")
    sys.exit(1)

# Setup DLL paths
libs_path = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "CapitalAPI_2.13.57", "CapitalAPI_2.13.57_PythonExample", "SKDLLPythonTester", "libs",
))
os.add_dll_directory(libs_path)
os.environ["PATH"] = libs_path + os.pathsep + os.environ.get("PATH", "")

# Load COM typelib (same pattern as reference Quote.py)
dll_path = os.path.join(libs_path, "SKCOM.dll")
comtypes.client.GetModule(dll_path)
import comtypes.gen.SKCOMLib as sk

# Create COM objects at MODULE LEVEL (exactly like reference Quote.py)
skC = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
skQ = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
skR = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)


def _load_settings():
    cfg = {"user_id": "", "password": "", "authority_flag": 0}
    base = os.path.dirname(os.path.abspath(__file__))
    for name in ("settings.yaml", "settings.example.yaml"):
        path = os.path.join(base, name)
        if os.path.exists(path) and yaml:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            creds = data.get("credentials", {})
            cfg["user_id"] = creds.get("user_id", "")
            cfg["password"] = creds.get("password", "")
            cfg["authority_flag"] = creds.get("authority_flag", 0)
            break
    return cfg


KLINE_TYPES = {
    "1分鐘 1min": (0, 1),
    "5分鐘 5min": (0, 5),
    "15分鐘 15min": (0, 15),
    "30分鐘 30min": (0, 30),
    "60分鐘 60min": (0, 60),
    "240分鐘 240min": (0, 240),
    "日線 Daily": (4, 1),
    "週線 Weekly": (5, 1),
    "月線 Monthly": (6, 1),
}

TRADE_SESSIONS = {
    "全盤 Full Session": 0,
    "日盤 AM Session": 1,
}

# Global reference to the app (for event callbacks)
_app = None


def _log(msg):
    """Log to both console and GUI (if available)."""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:12]
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if _app and hasattr(_app, 'log_text'):
        try:
            _app.log_text.insert(tk.END, line + "\n")
            _app.log_text.see(tk.END)
        except:
            pass


# ── Event classes (module level, exactly like reference) ──

class SKQuoteLibEvents:
    """Quote lib events - same pattern as reference Quote.py."""

    def OnConnection(self, nKind, nCode):
        kind_names = {3001: "Reply", 3002: "Quote", 3003: "Ready", 3021: "ConnError", 3033: "Abnormal"}
        _log(f"報價連線 QUOTE CONN: {kind_names.get(nKind, nKind)} code={nCode}")
        if _app:
            if nKind == 3003 and nCode == 0:
                _app._quote_connected = True
                _app.btn_fetch.config(state=tk.NORMAL)
                _app.status_var.set("已連線 Connected - 可查詢K線 Ready")
            elif nKind == 3002 and nCode == 0 and not _app._quote_connected:
                _app._quote_connected = True
                _app.btn_fetch.config(state=tk.NORMAL)
                _app.status_var.set("已連線 Connected (Quote) - 可查詢K線 Ready")

    def OnNotifyKLineData(self, bstrStockNo, bstrData):
        if _app:
            _app.kline_data.append(bstrData)
            n = len(_app.kline_data)
            if n <= 3 or n % 50 == 0:
                _log(f"K線 [{n}]: {bstrData[:80]}")

    def OnKLineComplete(self, nCode):
        n = len(_app.kline_data) if _app else 0
        _log(f"K線完成 KLine complete: {n} bars, code={nCode}")
        if _app:
            _app._process_kline_results()

    def OnNotifyQuoteLONG(self, sMarketNo, nStockIdx):
        pass

    def OnNotifyTicksLONG(self, sMarketNo, nStockIdx, nPtr):
        pass

    def OnNotifyBest5LONG(self, sMarketNo, nStockIdx):
        pass

    def OnNotifyServerTime(self, sHour, sMinute, sSecond, nTotal):
        pass


class SKReplyLibEvent:
    """Reply lib events - same pattern as reference Quote.py."""

    def OnReplyMessage(self, bstrUserID, bstrMessage):
        _log(f"回報 REPLY: {bstrMessage}")
        return -1  # sConfirmCode [out] param

    def OnConnect(self, bstrUserID, nErrorCode):
        _log(f"回報連線 REPLY CONN: code={nErrorCode}")

    def OnComplete(self, bstrUserID):
        pass

    def OnNewData(self, bstrUserID, bstrData):
        pass


# Register event handlers at MODULE LEVEL (exactly like reference Quote.py)
SKQuoteEvent = SKQuoteLibEvents()
SKQuoteLibEventHandler = comtypes.client.GetEvents(skQ, SKQuoteEvent)

SKReplyEvent = SKReplyLibEvent()
SKReplyLibEventHandler = comtypes.client.GetEvents(skR, SKReplyEvent)


class KLineViewer:
    def __init__(self, root: tk.Tk):
        global _app
        _app = self

        self.root = root
        self.root.title("tai-robot K線歷史資料 KLine History (Capital API)")
        self.root.geometry("1100x750")
        self.root.minsize(900, 600)

        self._settings = _load_settings()
        self.kline_data = []
        self.kline_rows = []
        self._logged_in = False
        self._quote_connected = False
        self._auto_fetch = "--auto-fetch" in sys.argv
        # For testing: allow overriding kline type from command line
        self._test_ktype = None
        for arg in sys.argv:
            if arg.startswith("--ktype="):
                self._test_ktype = arg.split("=", 1)[1]

        self._build_ui()

        # Apply test ktype after UI built
        if self._test_ktype:
            for name in KLINE_TYPES:
                if self._test_ktype in name:
                    self.ktype_var.set(name)
                    break

        # Auto-login after UI is built (runs in mainloop context)
        self.root.after(200, self._do_login)

    def _build_ui(self):
        ctrl_frame = ttk.LabelFrame(self.root, text="查詢設定 Query Settings", padding=8)
        ctrl_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        # Row 0
        ttk.Label(ctrl_frame, text="商品代碼 Symbol:").grid(row=0, column=0, sticky=tk.W, padx=4)
        self.symbol_var = tk.StringVar(value="TX00")
        ttk.Entry(ctrl_frame, textvariable=self.symbol_var, width=12).grid(row=0, column=1, padx=4)

        ttk.Label(ctrl_frame, text="K線週期 Type:").grid(row=0, column=2, sticky=tk.W, padx=4)
        self.ktype_var = tk.StringVar(value="日線 Daily")
        ktype_combo = ttk.Combobox(ctrl_frame, textvariable=self.ktype_var, width=16,
                                    state="readonly", values=list(KLINE_TYPES.keys()))
        ktype_combo.grid(row=0, column=3, padx=4)
        ktype_combo.current(6)  # "日線 Daily"

        ttk.Label(ctrl_frame, text="盤別 Session:").grid(row=0, column=4, sticky=tk.W, padx=4)
        self.session_var = tk.StringVar(value="全盤 Full Session")
        session_combo = ttk.Combobox(ctrl_frame, textvariable=self.session_var, width=16,
                                      state="readonly", values=list(TRADE_SESSIONS.keys()))
        session_combo.grid(row=0, column=5, padx=4)
        session_combo.current(0)

        # minute_num is auto-set by KLine type selection

        # Row 1: Date range
        ttk.Label(ctrl_frame, text="起始 Start:").grid(row=1, column=0, sticky=tk.W, padx=4, pady=4)
        default_start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
        self.start_var = tk.StringVar(value=default_start)
        ttk.Entry(ctrl_frame, textvariable=self.start_var, width=12).grid(row=1, column=1, padx=4)

        ttk.Label(ctrl_frame, text="結束 End:").grid(row=1, column=2, sticky=tk.W, padx=4)
        self.end_var = tk.StringVar(value=datetime.now().strftime("%Y%m%d"))
        ttk.Entry(ctrl_frame, textvariable=self.end_var, width=12).grid(row=1, column=3, padx=4)

        period_frame = ttk.Frame(ctrl_frame)
        period_frame.grid(row=1, column=4, columnspan=4, padx=4, sticky=tk.W)
        for label, days in [("1月", 30), ("3月", 90), ("6月", 180), ("1年", 365), ("2年", 730)]:
            ttk.Button(period_frame, text=label, width=4,
                       command=lambda d=days: self._set_period(d)).pack(side=tk.LEFT, padx=2)

        # Row 2: Buttons
        btn_frame = ttk.Frame(ctrl_frame)
        btn_frame.grid(row=2, column=0, columnspan=8, pady=(8, 0))

        self.btn_fetch = ttk.Button(btn_frame, text="★ 查詢K線 Fetch KLine",
                                     command=self._do_fetch, state=tk.DISABLED)
        self.btn_fetch.pack(side=tk.LEFT, padx=4)

        self.btn_export = ttk.Button(btn_frame, text="匯出CSV Export CSV",
                                      command=self._do_export, state=tk.DISABLED)
        self.btn_export.pack(side=tk.LEFT, padx=4)

        self.status_var = tk.StringVar(value="初始化中 Initializing...")
        ttk.Label(btn_frame, textvariable=self.status_var, foreground="gray").pack(side=tk.LEFT, padx=16)

        # Notebook
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        # Table tab
        table_frame = ttk.Frame(notebook)
        notebook.add(table_frame, text="K線資料 KLine Data")
        columns = ("date", "open", "high", "low", "close", "volume", "change", "change_pct")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=25)
        for col, (text, w) in {
            "date": ("日期 Date", 160), "open": ("開盤 Open", 100),
            "high": ("最高 High", 100), "low": ("最低 Low", 100),
            "close": ("收盤 Close", 100), "volume": ("成交量 Vol", 110),
            "change": ("漲跌 Chg", 90), "change_pct": ("漲跌% %", 80),
        }.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor=tk.E if col != "date" else tk.W)
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Stats tab
        stats_frame = ttk.Frame(notebook)
        notebook.add(stats_frame, text="統計 Statistics")
        self.stats_text = scrolledtext.ScrolledText(stats_frame, wrap=tk.WORD, font=("Consolas", 10))
        self.stats_text.pack(fill=tk.BOTH, expand=True)

        # Log tab
        log_frame = ttk.Frame(notebook)
        notebook.add(log_frame, text="紀錄 Log")
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _set_period(self, days):
        self.end_var.set(datetime.now().strftime("%Y%m%d"))
        self.start_var.set((datetime.now() - timedelta(days=days)).strftime("%Y%m%d"))

    def _do_login(self):
        """Login and connect quote service (runs in mainloop via root.after)."""
        try:
            user_id = self._settings["user_id"]
            password = self._settings["password"]
            authority_flag = self._settings.get("authority_flag", 0)

            # Set log path
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CapitalLog_KLine")
            skC.SKCenterLib_SetLogPath(log_dir)
            _log(f"Log path: {log_dir}")

            # Set authority flag
            if authority_flag:
                skC.SKCenterLib_SetAuthority(authority_flag)
                _log(f"Authority flag: {authority_flag}")

            # Login (use LoginSetQuote with "Y" to enable quote)
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

            # Connect reply service
            skR.SKReplyLib_ConnectByID(user_id)
            _log("回報服務連線中 Reply connecting...")

            # Enter quote monitor
            code = skQ.SKQuoteLib_EnterMonitorLONG()
            _log(f"進入報價監控 EnterMonitorLONG: code={code}")
            if code != 0:
                msg = skC.SKCenterLib_GetReturnCodeMessage(code)
                _log(f"  EnterMonitor result: {msg}")
            self.status_var.set("連線中 Connecting...")

            # Schedule connection check
            self.root.after(3000, self._check_connection)

        except Exception as e:
            _log(f"初始化錯誤 Init error: {e}")
            import traceback
            _log(traceback.format_exc())
            self.status_var.set(f"錯誤 Error: {e}")

    def _check_connection(self):
        """Periodically check if quote service is connected."""
        try:
            ic = skQ.SKQuoteLib_IsConnected()
            _log(f"連線檢查 IsConnected={ic} (1=connected, 2=connecting)")
            if ic == 1:
                self._quote_connected = True
                self.btn_fetch.config(state=tk.NORMAL)
                self.status_var.set("已連線 Connected - 可查詢K線 Ready")
                # Auto-fetch for testing (if --auto-fetch flag)
                if self._auto_fetch:
                    self._auto_fetch = False
                    self.root.after(500, self._do_fetch)
            elif not self._quote_connected:
                self.status_var.set(f"連線中 Connecting... (status={ic})")
                self.root.after(2000, self._check_connection)
        except Exception as e:
            _log(f"連線檢查錯誤: {e}")

    # ── KLine Fetch ──

    def _do_fetch(self):
        symbol = self.symbol_var.get().strip()
        if not symbol:
            return

        kline_type, minute_num = KLINE_TYPES.get(self.ktype_var.get(), (4, 1))
        trade_session = TRADE_SESSIONS.get(self.session_var.get(), 0)
        start_date = self.start_var.get().strip()
        end_date = self.end_var.get().strip()

        self.kline_data = []
        self.btn_fetch.config(state=tk.DISABLED)
        self.status_var.set(f"查詢中 Fetching {symbol}...")

        _log(f"請求K線 RequestKLine: {symbol} type={kline_type} "
             f"session={trade_session} {start_date}~{end_date} min={minute_num}")

        try:
            _log("發送請求 Sending request...")
            code = skQ.SKQuoteLib_RequestKLineAMByDate(
                symbol, kline_type, 1, trade_session, start_date, end_date, minute_num)
            _log(f"請求回傳 Request returned: code={code}")

            if code != 0:
                msg = skC.SKCenterLib_GetReturnCodeMessage(code)
                _log(f"請求結果 Result: code={code} {msg}")
                if code >= 3000:
                    self.status_var.set(f"錯誤 Error: {msg}")
                    self.btn_fetch.config(state=tk.NORMAL)
                    return

            if not self.kline_data:
                _log("等待資料 Waiting for data...")

        except Exception as e:
            _log(f"查詢錯誤 Fetch error: {e}")
            self.btn_fetch.config(state=tk.NORMAL)

    def _process_kline_results(self):
        """Called when OnKLineComplete fires."""
        rows = []
        prev_close = None
        for data_str in self.kline_data:
            try:
                fields = data_str.split(",")
                if len(fields) < 6:
                    continue
                dt_str = fields[0].strip()
                o, h, l, c = float(fields[1]), float(fields[2]), float(fields[3]), float(fields[4])
                v = int(float(fields[5]))
                chg = round(c - prev_close, 2) if prev_close else 0
                pct = round(chg / prev_close * 100, 2) if prev_close and prev_close != 0 else 0
                rows.append({"date": dt_str, "open": o, "high": h, "low": l,
                             "close": c, "volume": v, "change": chg, "change_pct": pct})
                prev_close = c
            except Exception as e:
                _log(f"  Parse err: {e} | {data_str[:60]}")

        self.kline_rows = rows
        self._populate_table(rows)

    def _populate_table(self, rows):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for r in rows:
            self.tree.insert("", tk.END, values=(
                r["date"], f"{r['open']:.2f}", f"{r['high']:.2f}",
                f"{r['low']:.2f}", f"{r['close']:.2f}",
                f"{r['volume']:,}", f"{r['change']:+.2f}", f"{r['change_pct']:+.2f}%"))

        sym = self.symbol_var.get()
        sess = self.session_var.get().split(" ")[0]
        self.status_var.set(f"完成 Done: {sym} {sess} - {len(rows)} bars")
        self.btn_fetch.config(state=tk.NORMAL)
        self.btn_export.config(state=tk.NORMAL)
        self._compute_stats(rows, sym)

    def _compute_stats(self, rows, symbol):
        self.stats_text.delete("1.0", tk.END)
        if not rows:
            self.stats_text.insert(tk.END, "無資料 No data\n")
            return
        closes = [r["close"] for r in rows]
        highs = [r["high"] for r in rows]
        lows = [r["low"] for r in rows]
        vols = [r["volume"] for r in rows]
        ph, pl = max(highs), min(lows)
        phd = rows[highs.index(ph)]["date"]
        pld = rows[lows.index(pl)]["date"]
        tc = closes[-1] - closes[0]
        tcp = tc / closes[0] * 100 if closes[0] else 0
        ub = sum(1 for r in rows[1:] if r["change"] > 0)
        db = sum(1 for r in rows[1:] if r["change"] < 0)
        rets = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes)) if closes[i-1]]
        vol = (sum((r - sum(rets)/len(rets))**2 for r in rets) / len(rets))**0.5 if rets else 0
        ma5 = sum(closes[-5:]) / min(5, len(closes))
        ma10 = sum(closes[-10:]) / min(10, len(closes))
        ma20 = sum(closes[-20:]) / min(20, len(closes))

        s = self.session_var.get()
        self.stats_text.insert(tk.END, f"=== {symbol} {s} ===\n\n")
        self.stats_text.insert(tk.END, f"期間: {rows[0]['date']} ~ {rows[-1]['date']} ({len(rows)} bars)\n")
        self.stats_text.insert(tk.END, f"起始: {closes[0]:.2f}  最終: {closes[-1]:.2f}  漲跌: {tc:+.2f} ({tcp:+.2f}%)\n\n")
        self.stats_text.insert(tk.END, f"最高: {ph:.2f} ({phd})\n最低: {pl:.2f} ({pld})\n振幅: {ph-pl:.2f}\n\n")
        self.stats_text.insert(tk.END, f"均量: {sum(vols)/len(vols):,.0f}\n")
        self.stats_text.insert(tk.END, f"上漲: {ub}  下跌: {db}\n")
        self.stats_text.insert(tk.END, f"波動率: {vol*100:.2f}%  年化: {vol*100*(252**0.5):.2f}%\n\n")
        self.stats_text.insert(tk.END, f"MA5: {ma5:.2f}  MA10: {ma10:.2f}  MA20: {ma20:.2f}\n")

    def _do_export(self):
        if not self.kline_rows:
            return
        from tkinter import filedialog
        sym = self.symbol_var.get().strip()
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"kline_{sym}_{datetime.now().strftime('%Y%m%d')}.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["date","open","high","low","close","volume","change","change_pct"])
            w.writeheader()
            w.writerows(self.kline_rows)
        self.status_var.set(f"已匯出 Exported: {path}")


if __name__ == "__main__":
    root = tk.Tk()
    app = KLineViewer(root)
    root.mainloop()
