"""Interactive GUI tester for Capital API connection.

Usage:
  python test_connection.py
"""

import os
import sys
import time
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime

try:
    import yaml
except ImportError:
    yaml = None


def _load_settings():
    """Load user_id, password, authority_flag from settings.yaml or env vars."""
    cfg = {"user_id": "", "password": "", "authority_flag": 0}

    # Try settings.yaml first, then settings.example.yaml
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

    # Env vars override yaml
    if os.environ.get("CAPITAL_API_USER"):
        cfg["user_id"] = os.environ["CAPITAL_API_USER"]
    if os.environ.get("CAPITAL_API_PASSWORD"):
        cfg["password"] = os.environ["CAPITAL_API_PASSWORD"]

    # Try keyring if no password from env
    if not cfg["password"]:
        try:
            import keyring
            pw = keyring.get_password("tai-robot", cfg["user_id"])
            if pw:
                cfg["password"] = pw
        except Exception:
            pass

    return cfg

# Add SDK to path and ensure dependent DLLs are discoverable
sdk_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "CapitalAPI_2.13.57", "CapitalAPI_2.13.57_PythonExample", "SKDLLPythonTester",
)
libs_path = os.path.join(sdk_path, "libs")

# Critical: add libs/ to DLL search path so SKCOM.dll can find its dependencies
# (SKProxyLIB.dll, SKTradeLib.dll, CTSecuritiesATL.dll, etc.)
if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(libs_path)
os.environ["PATH"] = libs_path + os.pathsep + os.environ.get("PATH", "")

sys.path.insert(0, sdk_path)
from SKDLLPython import SK


class ConnectionTester:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("tai-robot 連線測試工具 Connection Tester")
        self.root.geometry("960x720")
        self.root.minsize(800, 600)

        self.login_id = ""
        self.logged_in = False
        self.commodity_loaded = False
        self.tick_count = 0
        self.quote_count = 0
        self._settings = _load_settings()

        self._build_ui()
        self._register_callbacks()

    # ── UI Construction ──────────────────────────────────────────────

    def _build_ui(self):
        # Top frame: credentials
        cred_frame = ttk.LabelFrame(self.root, text="登入資訊 Credentials", padding=8)
        cred_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(cred_frame, text="帳號 User ID:").grid(row=0, column=0, sticky=tk.W, padx=4)
        self.user_var = tk.StringVar(value=self._settings["user_id"])
        ttk.Entry(cred_frame, textvariable=self.user_var, width=20).grid(row=0, column=1, padx=4)

        ttk.Label(cred_frame, text="密碼 Password:").grid(row=0, column=2, sticky=tk.W, padx=4)
        self.pass_var = tk.StringVar(value=self._settings["password"])
        ttk.Entry(cred_frame, textvariable=self.pass_var, width=20, show="*").grid(row=0, column=3, padx=4)

        ttk.Label(cred_frame, text="伺服器 Server:").grid(row=0, column=4, sticky=tk.W, padx=4)
        auth_flag = self._settings["authority_flag"]
        self.authority_var = tk.StringVar()
        auth_combo = ttk.Combobox(cred_frame, textvariable=self.authority_var, width=22, state="readonly",
                                  values=["0 - 正式環境 Production", "1 - 測試+正式 Test+Prod", "2 - 僅測試 Test Only"])
        auth_combo.grid(row=0, column=5, padx=4)
        auth_combo.current(min(auth_flag, 2))

        # Certificate row
        ttk.Label(cred_frame, text="憑證ID Cert ID:").grid(row=1, column=0, sticky=tk.W, padx=4)
        self.cert_var = tk.StringVar(value="")
        ttk.Entry(cred_frame, textvariable=self.cert_var, width=20).grid(row=1, column=1, padx=4)

        ttk.Label(cred_frame, text="憑證路徑 Cert Path:").grid(row=1, column=2, sticky=tk.W, padx=4)
        self.cert_path_var = tk.StringVar(value="")
        cert_entry = ttk.Entry(cred_frame, textvariable=self.cert_path_var, width=40)
        cert_entry.grid(row=1, column=3, columnspan=2, padx=4, sticky=tk.W)

        def _browse_cert():
            from tkinter import filedialog
            path = filedialog.askdirectory(title="選擇憑證資料夾 Select certificate folder")
            if path:
                self.cert_path_var.set(path)
        ttk.Button(cred_frame, text="瀏覽 Browse", command=_browse_cert).grid(row=1, column=5, padx=4)

        # Action buttons frame
        btn_frame = ttk.LabelFrame(self.root, text="操作 Actions", padding=8)
        btn_frame.pack(fill=tk.X, padx=8, pady=4)

        self.btn_login = ttk.Button(btn_frame, text="1. 登入 Login", command=self._do_login)
        self.btn_login.grid(row=0, column=0, padx=4, pady=2)

        self.btn_connect = ttk.Button(btn_frame, text="2. 連線服務 Connect", command=self._do_connect, state=tk.DISABLED)
        self.btn_connect.grid(row=0, column=1, padx=4, pady=2)

        self.btn_load = ttk.Button(btn_frame, text="3. 載入商品 Load", command=self._do_load_commodity, state=tk.DISABLED)
        self.btn_load.grid(row=0, column=2, padx=4, pady=2)

        ttk.Separator(btn_frame, orient=tk.VERTICAL).grid(row=0, column=3, sticky="ns", padx=8)

        # Subscribe frame
        ttk.Label(btn_frame, text="商品代碼 Symbol:").grid(row=0, column=4, padx=4)
        self.symbol_var = tk.StringVar(value="TX00")
        ttk.Entry(btn_frame, textvariable=self.symbol_var, width=12).grid(row=0, column=5, padx=4)

        self.btn_subscribe = ttk.Button(btn_frame, text="4. 訂閱 Subscribe", command=self._do_subscribe, state=tk.DISABLED)
        self.btn_subscribe.grid(row=0, column=6, padx=4, pady=2)

        self.btn_unsub = ttk.Button(btn_frame, text="取消訂閱 Unsub", command=self._do_unsubscribe, state=tk.DISABLED)
        self.btn_unsub.grid(row=0, column=7, padx=4, pady=2)

        ttk.Separator(btn_frame, orient=tk.VERTICAL).grid(row=0, column=8, sticky="ns", padx=8)

        self.btn_get_quote = ttk.Button(btn_frame, text="查詢報價 Quote", command=self._do_get_quote, state=tk.DISABLED)
        self.btn_get_quote.grid(row=0, column=9, padx=4, pady=2)

        # Second row: market no, stock list
        ttk.Label(btn_frame, text="市場 Market:").grid(row=1, column=0, padx=4, sticky=tk.W)
        self.market_var = tk.StringVar(value="2")
        market_combo = ttk.Combobox(btn_frame, textvariable=self.market_var, width=26, state="readonly",
                                    values=["2 - 日盤期貨 T-session Futures",
                                            "7 - 全盤期貨 Full-session Futures",
                                            "0 - 上市股票 Stocks (TSE)",
                                            "1 - 上櫃股票 Stocks (OTC)"])
        market_combo.grid(row=1, column=1, columnspan=2, padx=4, pady=2, sticky=tk.W)
        market_combo.current(0)

        self.btn_stocklist = ttk.Button(btn_frame, text="商品清單 Stock List", command=self._do_stock_list, state=tk.DISABLED)
        self.btn_stocklist.grid(row=1, column=6, padx=4, pady=2)

        self.btn_auto = ttk.Button(btn_frame, text="★ 一鍵啟動 Auto Start", command=self._do_auto_start)
        self.btn_auto.grid(row=1, column=7, columnspan=2, padx=4, pady=2)

        self.btn_clear = ttk.Button(btn_frame, text="清除紀錄 Clear", command=self._clear_log)
        self.btn_clear.grid(row=1, column=9, padx=4, pady=2)

        # Status bar
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, padx=8, pady=2)

        self.status_var = tk.StringVar(value="未連線 Not connected")
        ttk.Label(status_frame, textvariable=self.status_var, foreground="gray").pack(side=tk.LEFT)

        self.counter_var = tk.StringVar(value="逐筆 Ticks: 0 | 報價 Quotes: 0")
        ttk.Label(status_frame, textvariable=self.counter_var).pack(side=tk.RIGHT)

        # Notebook with tabs for different data types
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        # All Events tab
        all_frame = ttk.Frame(notebook)
        notebook.add(all_frame, text="所有事件 All Events")
        self.log_text = scrolledtext.ScrolledText(all_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Ticks tab
        tick_frame = ttk.Frame(notebook)
        notebook.add(tick_frame, text="逐筆成交 Ticks")
        self.tick_text = scrolledtext.ScrolledText(tick_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.tick_text.pack(fill=tk.BOTH, expand=True)

        # Quotes tab
        quote_frame = ttk.Frame(notebook)
        notebook.add(quote_frame, text="報價 Quotes")
        self.quote_text = scrolledtext.ScrolledText(quote_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.quote_text.pack(fill=tk.BOTH, expand=True)

        # Best5 tab
        best5_frame = ttk.Frame(notebook)
        notebook.add(best5_frame, text="五檔 Best 5")
        self.best5_text = scrolledtext.ScrolledText(best5_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.best5_text.pack(fill=tk.BOTH, expand=True)

        # Orders tab
        order_frame = ttk.Frame(notebook)
        notebook.add(order_frame, text="委託/成交 Orders/Fills")
        self.order_text = scrolledtext.ScrolledText(order_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.order_text.pack(fill=tk.BOTH, expand=True)

    # ── Logging ──────────────────────────────────────────────────────

    def _log(self, msg: str, *extra_widgets):
        """Thread-safe log to the All Events tab and any extra text widgets."""
        ts = datetime.now().strftime("%H:%M:%S.%f")[:12]
        line = f"[{ts}] {msg}\n"

        def _append():
            self.log_text.insert(tk.END, line)
            self.log_text.see(tk.END)
            for w in extra_widgets:
                w.insert(tk.END, line)
                w.see(tk.END)

        self.root.after(0, _append)

    def _clear_log(self):
        for w in [self.log_text, self.tick_text, self.quote_text, self.best5_text, self.order_text]:
            w.delete("1.0", tk.END)
        self.tick_count = 0
        self.quote_count = 0
        self._update_counters()

    def _update_counters(self):
        self.root.after(0, lambda: self.counter_var.set(
            f"逐筆 Ticks: {self.tick_count} | 報價 Quotes: {self.quote_count}"))

    def _set_status(self, msg: str):
        self.root.after(0, lambda: self.status_var.set(msg))

    # ── Callbacks (called from DLL threads) ──────────────────────────

    def _register_callbacks(self):
        SK.OnConnection(self._on_connection)
        SK.OnReplyMessage(self._on_reply)
        SK.OnComplete(self._on_complete)
        SK.OnNotifyTicksLONG(self._on_tick)
        SK.OnNotifyQuoteLONG(self._on_quote)
        SK.OnNotifyBest5LONG(self._on_best5)
        SK.OnProxyOrder(self._on_proxy_order)
        SK.OnNewData(self._on_new_data)

    def _on_connection(self, login_id, code):
        status = {0: "已連線 Connected", 1: "已斷線 Disconnected",
                  2: "連線中斷 Connection lost", 3: "重新連線中 Reconnecting"}
        s = status.get(code, f"未知 Unknown({code})")
        self._log(f"連線狀態 CONNECTION: {login_id} -> {s}")
        self._set_status(f"連線: {s}")

    def _on_reply(self, msg1, msg2):
        self._log(f"回報 REPLY: {msg1} | {msg2}")

    def _on_complete(self, login_id):
        self._log(f"完成 COMPLETE: {login_id}")

    def _on_tick(self, market_no, stockno, ptr, date, time_hms, time_ms,
                 bid, ask, close, qty, simulate):
        self.tick_count += 1
        h = time_hms // 10000
        m = (time_hms % 10000) // 100
        s = time_hms % 100
        ms = time_ms // 1000
        sim = " [SIM]" if simulate else ""
        msg = (f"TICK #{self.tick_count}: {stockno} {h:02d}:{m:02d}:{s:02d}.{ms:03d} "
               f"C={close} Q={qty} B={bid} A={ask}{sim}")
        self._log(msg, self.tick_text)
        self._update_counters()

    def _on_quote(self, market_no, stock_no):
        self.quote_count += 1
        try:
            s = SK.SKQuoteLib_GetStockByStockNo(market_no, stock_no)
            msg = (f"QUOTE: {s.strStockNo} {s.strStockName} "
                   f"O={s.nOpen} H={s.nHigh} L={s.nLow} C={s.nClose} "
                   f"V={s.nTQty} Bid={s.nBid}/{s.nBc} Ask={s.nAsk}/{s.nAc} "
                   f"Ref={s.nRef} TickQ={s.nTickQty}")
        except Exception as e:
            msg = f"QUOTE: {stock_no} (error fetching details: {e})"
        self._log(msg, self.quote_text)
        self._update_counters()

    def _on_best5(self, market_no, stockno, bids, bid_qtys, asks, ask_qtys,
                  ext_bid, ext_bid_qty, ext_ask, ext_ask_qty, simulate):
        sno = stockno.decode("ansi") if isinstance(stockno, bytes) else stockno
        lines = [f"BEST5: {sno}"]
        for i in range(5):
            lines.append(f"  Bid[{i}]: {bids[i]:>8} x {bid_qtys[i]:<6}  "
                         f"Ask[{i}]: {asks[i]:>8} x {ask_qtys[i]}")
        msg = "\n".join(lines)
        self._log(msg, self.best5_text)

    def _on_proxy_order(self, stamp_id, code, message):
        self._log(f"委託回報 ORDER RESPONSE: stamp={stamp_id} code={code} msg={message}", self.order_text)

    def _on_new_data(self, login_id, data):
        self._log(f"委託/成交資料 DATA: {data.Raw}", self.order_text)

    # ── Actions ──────────────────────────────────────────────────────

    def _get_authority_flag(self) -> int:
        return int(self.authority_var.get().split(" ")[0])

    def _get_market_no(self) -> int:
        return int(self.market_var.get().split(" ")[0])

    def _do_login(self):
        user = self.user_var.get().strip()
        pwd = self.pass_var.get().strip()
        if not user or not pwd:
            messagebox.showwarning("缺少資料 Missing", "請輸入帳號與密碼 Enter User ID and Password.")
            return

        flag = self._get_authority_flag()
        self._log(f"登入中 Logging in as {user} (authority_flag={flag})...")
        self.btn_login.config(state=tk.DISABLED)

        cert_id = self.cert_var.get().strip()
        cert_path = self.cert_path_var.get().strip()

        def _login():
            try:
                if cert_id and cert_path:
                    self._log(f"  Using cert: {cert_id} from {cert_path}")
                    result = SK.Login(user, pwd, flag, cert_id, cert_path)
                elif cert_id:
                    result = SK.Login(user, pwd, flag, cert_id)
                else:
                    result = SK.Login(user, pwd, flag)
                if result.Code != 0:
                    msg = SK.GetMessage(result.Code)
                    self._log(f"登入失敗 LOGIN FAILED (code={result.Code}): {msg}")
                    self._set_status(f"登入失敗 Login failed: {msg}")
                    self.root.after(0, lambda: self.btn_login.config(state=tk.NORMAL))
                    return

                self.login_id = user
                self.logged_in = True
                self._log(f"登入成功 LOGIN OK!")
                self._log(f"  原始資料 Raw: {result.RawAccountData[:300]}")
                self._log(f"  期貨帳號 TF accounts: {[a.FullAccount for a in result.TFAccounts]}")
                self._log(f"  證券帳號 TS accounts: {[a.FullAccount for a in result.TSAccounts]}")
                self._log(f"  海期帳號 OF accounts: {[a.FullAccount for a in result.OFAccounts]}")
                self._log(f"  海股帳號 OS accounts: {[a.FullAccount for a in result.OSAccounts]}")
                self._set_status(f"已登入 Logged in as {user}")

                self.root.after(0, self._enable_post_login)

            except Exception as e:
                self._log(f"登入錯誤 LOGIN ERROR: {e}")
                self.root.after(0, lambda: self.btn_login.config(state=tk.NORMAL))

        threading.Thread(target=_login, daemon=True).start()

    def _enable_post_login(self):
        self.btn_connect.config(state=tk.NORMAL)
        self.btn_load.config(state=tk.NORMAL)

    def _do_connect(self):
        self._log("連線服務中 Connecting services...")
        self.btn_connect.config(state=tk.DISABLED)

        def _connect():
            try:
                # Reply service
                code = SK.ManageServerConnection(self.login_id, 0, 0)
                self._log(f"  回報服務 Reply(0): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))
                time.sleep(1)

                # Quote service
                code = SK.ManageServerConnection(self.login_id, 0, 1)
                self._log(f"  報價服務 Quote(1): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))
                time.sleep(1)

                # Proxy order service
                code = SK.ManageServerConnection(self.login_id, 0, 4)
                self._log(f"  下單服務 Order(4): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))
                time.sleep(2)

                self._set_status("服務已連線 Services connected")
                self.root.after(0, self._enable_post_connect)
            except Exception as e:
                self._log(f"連線錯誤 CONNECT ERROR: {e}")
                self.root.after(0, lambda: self.btn_connect.config(state=tk.NORMAL))

        threading.Thread(target=_connect, daemon=True).start()

    def _enable_post_connect(self):
        self.btn_subscribe.config(state=tk.NORMAL)
        self.btn_unsub.config(state=tk.NORMAL)
        self.btn_get_quote.config(state=tk.NORMAL)
        self.btn_stocklist.config(state=tk.NORMAL)

    def _do_load_commodity(self):
        market_no = self._get_market_no()
        self._log(f"載入商品資料中 Loading commodity data...")

        def _load():
            try:
                # Must load TW stocks (market 0) first as prerequisite
                if market_no != 0:
                    self._log(f"  先載入上市資料 Loading TW stocks (market 0) first...")
                    code0 = SK.LoadCommodity(0)
                    self._log(f"  LoadCommodity(0): code={code0}" + (f" {SK.GetMessage(code0)}" if code0 != 0 else " OK"))
                    time.sleep(3)

                self._log(f"  載入市場 Loading market {market_no}...")
                code = SK.LoadCommodity(market_no)
                self._log(f"  LoadCommodity({market_no}): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))
                if code == 0:
                    time.sleep(3)
                    self.commodity_loaded = True
                    self._log(f"  載入完成 Load complete. 可查詢商品清單 Ready for stock list / subscribe.")
            except Exception as e:
                self._log(f"載入錯誤 LOAD ERROR: {e}")

        threading.Thread(target=_load, daemon=True).start()

    def _do_subscribe(self):
        symbol = self.symbol_var.get().strip()
        if not symbol:
            return
        self._log(f"訂閱中 Subscribing to {symbol}...")
        self.btn_subscribe.config(state=tk.DISABLED)

        def _subscribe():
            try:
                # Auto-load commodity data if not loaded yet
                if not self.commodity_loaded:
                    self._log("  自動載入商品資料 Auto-loading commodity data...")
                    self._log("  先載入上市資料 Loading TW stocks (market 0) first...")
                    code0 = SK.LoadCommodity(0)
                    self._log(f"  LoadCommodity(0): code={code0}" + (f" {SK.GetMessage(code0)}" if code0 != 0 else " OK"))
                    time.sleep(3)
                    market_no = self._get_market_no()
                    if market_no != 0:
                        self._log(f"  載入市場 Loading market {market_no}...")
                        code = SK.LoadCommodity(market_no)
                        self._log(f"  LoadCommodity({market_no}): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))
                        time.sleep(3)
                    self.commodity_loaded = True
                    self._log("  商品資料載入完成 Commodity data loaded.")

                code = SK.SKQuoteLib_RequestStocks(symbol)
                self._log(f"  訂閱報價 RequestStocks({symbol}): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))

                code = SK.SKQuoteLib_RequestTicks(0, symbol)
                self._log(f"  訂閱逐筆 RequestTicks({symbol}): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))

                self.root.after(0, lambda: self.btn_subscribe.config(state=tk.NORMAL))
            except Exception as e:
                self._log(f"訂閱錯誤 SUBSCRIBE ERROR: {e}")
                self.root.after(0, lambda: self.btn_subscribe.config(state=tk.NORMAL))

        threading.Thread(target=_subscribe, daemon=True).start()

    def _do_unsubscribe(self):
        symbol = self.symbol_var.get().strip()
        if not symbol:
            return
        code = SK.SKQuoteLib_CancelRequestStocks(symbol)
        self._log(f"已取消訂閱報價 Unsubscribed stocks {symbol}: code={code}")
        code = SK.SKQuoteLib_CancelRequestTicks(symbol)
        self._log(f"已取消訂閱逐筆 Unsubscribed ticks {symbol}: code={code}")

    def _do_get_quote(self):
        symbol = self.symbol_var.get().strip()
        market_no = self._get_market_no()
        if not symbol:
            return
        try:
            s = SK.SKQuoteLib_GetStockByStockNo(market_no, symbol)
            self._log(f"查詢報價 GET QUOTE {symbol} (code={s.nCode}):")
            self._log(f"  名稱 Name={s.strStockName} 代碼 StockNo={s.strStockNo}")
            self._log(f"  開 O={s.nOpen} 高 H={s.nHigh} 低 L={s.nLow} 收 C={s.nClose}")
            self._log(f"  參考價 Ref={s.nRef} 買價 Bid={s.nBid}/{s.nBc} 賣價 Ask={s.nAsk}/{s.nAc}")
            self._log(f"  總量 Volume={s.nTQty} 昨量 YVolume={s.nYQty} 單量 TickQty={s.nTickQty}")
            self._log(f"  未平倉 OI={s.nFutureOI} 小數位 Decimal={s.nDecimal} TypeNo={s.nTypeNo}")
            self._log(f"  模擬 Simulate={s.nSimulate} 當沖 DayTrade={s.nDayTrade}")
        except Exception as e:
            self._log(f"查詢報價錯誤 GET QUOTE ERROR: {e}")

    def _do_stock_list(self):
        market_no = self._get_market_no()
        self._log(f"取得商品清單中 Requesting stock list (market_no={market_no})...")

        def _request():
            try:
                parser = SK.RequestStockList(market_no)
                raw_preview = parser.raw_data[:500] if parser.raw_data else "(empty)"
                self._log(f"  原始資料 Raw data ({len(parser.raw_data)} chars): {raw_preview}")
                type_lists = parser.AllTypeLists
                self._log(f"  取得 Got {len(type_lists)} 類別 type groups")
                for tl in type_lists:
                    self._log(f"  類別 Type {tl.TypeNo}: {tl.TypeName} ({len(tl.Items)} 檔 items)")
                    for item in tl.Items[:10]:
                        self._log(f"    {item}")
                    if len(tl.Items) > 10:
                        self._log(f"    ... 還有 and {len(tl.Items)-10} more")
            except Exception as e:
                self._log(f"商品清單錯誤 STOCK LIST ERROR: {e}")
                import traceback
                self._log(traceback.format_exc())

        threading.Thread(target=_request, daemon=True).start()


    def _do_auto_start(self):
        """One-click: Login -> Connect -> Load -> Subscribe (runs full sequence)."""
        user = self.user_var.get().strip()
        pwd = self.pass_var.get().strip()
        if not user or not pwd:
            messagebox.showwarning("缺少資料 Missing", "請輸入帳號與密碼 Enter User ID and Password.")
            return

        symbol = self.symbol_var.get().strip() or "TX00"
        market_no = self._get_market_no()
        flag = self._get_authority_flag()

        self.btn_auto.config(state=tk.DISABLED)
        self.btn_login.config(state=tk.DISABLED)
        self._log(f"=== 一鍵啟動 Auto Start: {user} -> {symbol} ===")

        def _auto():
            try:
                # Step 1: Login
                self._log("--- 1. 登入 Login ---")
                cert_id = self.cert_var.get().strip()
                cert_path = self.cert_path_var.get().strip()
                if cert_id and cert_path:
                    result = SK.Login(user, pwd, flag, cert_id, cert_path)
                elif cert_id:
                    result = SK.Login(user, pwd, flag, cert_id)
                else:
                    result = SK.Login(user, pwd, flag)

                if result.Code != 0:
                    msg = SK.GetMessage(result.Code)
                    self._log(f"  登入失敗 LOGIN FAILED (code={result.Code}): {msg}")
                    self.root.after(0, lambda: self.btn_auto.config(state=tk.NORMAL))
                    self.root.after(0, lambda: self.btn_login.config(state=tk.NORMAL))
                    return

                self.login_id = user
                self.logged_in = True
                self._log(f"  登入成功 LOGIN OK!")
                self._log(f"  期貨帳號 TF: {[a.FullAccount for a in result.TFAccounts]}")
                self._set_status(f"已登入 Logged in as {user}")
                self.root.after(0, self._enable_post_login)
                time.sleep(1)

                # Step 2: Connect services
                self._log("--- 2. 連線服務 Connect Services ---")
                for svc, name in [(0, "回報 Reply"), (1, "報價 Quote"), (4, "下單 Order")]:
                    code = SK.ManageServerConnection(self.login_id, 0, svc)
                    self._log(f"  {name}({svc}): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))
                    time.sleep(1)
                time.sleep(2)
                self.root.after(0, self._enable_post_connect)

                # Step 3: Load commodity
                self._log("--- 3. 載入商品 Load Commodity ---")
                self._log("  載入上市資料 Loading TW stocks (market 0)...")
                code = SK.LoadCommodity(0)
                self._log(f"  LoadCommodity(0): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))
                time.sleep(3)

                if market_no != 0:
                    self._log(f"  載入市場 Loading market {market_no}...")
                    code = SK.LoadCommodity(market_no)
                    self._log(f"  LoadCommodity({market_no}): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))
                    time.sleep(3)

                self.commodity_loaded = True
                self._log("  商品資料載入完成 Commodity loaded.")

                # Step 4: Subscribe
                self._log(f"--- 4. 訂閱 Subscribe {symbol} ---")
                code = SK.SKQuoteLib_RequestStocks(symbol)
                self._log(f"  訂閱報價 RequestStocks({symbol}): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))

                code = SK.SKQuoteLib_RequestTicks(0, symbol)
                self._log(f"  訂閱逐筆 RequestTicks({symbol}): code={code}" + (f" {SK.GetMessage(code)}" if code != 0 else " OK"))

                # Step 5: Get quote snapshot
                time.sleep(2)
                self._log(f"--- 5. 查詢報價 Get Quote ---")
                try:
                    s = SK.SKQuoteLib_GetStockByStockNo(market_no, symbol)
                    self._log(f"  {s.strStockNo} {s.strStockName}")
                    self._log(f"  開 O={s.nOpen} 高 H={s.nHigh} 低 L={s.nLow} 收 C={s.nClose}")
                    self._log(f"  總量 V={s.nTQty} 參考 Ref={s.nRef} 未平倉 OI={s.nFutureOI}")
                except Exception as e:
                    self._log(f"  查詢失敗 Quote error: {e}")

                self._log("=== 一鍵啟動完成 Auto Start Complete! 等待即時資料... Waiting for live data... ===")
                self._set_status(f"已連線 Connected - 訂閱中 Subscribed to {symbol}")
                self.root.after(0, lambda: self.btn_auto.config(state=tk.NORMAL))

            except Exception as e:
                self._log(f"自動啟動錯誤 AUTO START ERROR: {e}")
                import traceback
                self._log(traceback.format_exc())
                self.root.after(0, lambda: self.btn_auto.config(state=tk.NORMAL))
                self.root.after(0, lambda: self.btn_login.config(state=tk.NORMAL))

        threading.Thread(target=_auto, daemon=True).start()


def main():
    root = tk.Tk()
    app = ConnectionTester(root)
    root.mainloop()


if __name__ == "__main__":
    main()
