"""CLI connection test: runs all steps in order with full debug output."""

import os
import sys
import time

# Add SDK to path and DLL search path
sdk_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "CapitalAPI_2.13.57", "CapitalAPI_2.13.57_PythonExample", "SKDLLPythonTester",
)
libs_path = os.path.join(sdk_path, "libs")
if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(libs_path)
os.environ["PATH"] = libs_path + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, sdk_path)

from SKDLLPython import SK

user_id = os.environ.get("CAPITAL_API_USER", "")
password = os.environ.get("CAPITAL_API_PASSWORD", "")

if not user_id or not password:
    print("ERROR: Set env vars first:")
    print("  set CAPITAL_API_USER=L124866242")
    print("  set CAPITAL_API_PASSWORD=yourpassword")
    sys.exit(1)

print(f"=== tai-robot CLI Connection Test ===")
print(f"User: {user_id}")
print()

# Step 0: Register all callbacks
print("[0] Registering callbacks...")

def on_connection(login_id, code):
    status = {0: "connected", 1: "disconnected", 2: "lost", 3: "reconnecting"}
    print(f"  >> CALLBACK OnConnection: {login_id} code={code} ({status.get(code, 'unknown')})")

def on_reply(msg1, msg2):
    print(f"  >> CALLBACK OnReply: {msg1} | {msg2}")

def on_complete(login_id):
    print(f"  >> CALLBACK OnComplete: {login_id}")

tick_count = 0
def on_tick(market_no, stockno, ptr, date, time_hms, time_ms, bid, ask, close, qty, simulate):
    global tick_count
    tick_count += 1
    h, m, s = time_hms // 10000, (time_hms % 10000) // 100, time_hms % 100
    sim = " [SIM]" if simulate else ""
    print(f"  TICK #{tick_count}: {stockno} {h:02d}:{m:02d}:{s:02d} C={close} Q={qty} B={bid} A={ask}{sim}")

def on_quote(market_no, stock_no):
    try:
        s = SK.SKQuoteLib_GetStockByStockNo(market_no, stock_no)
        print(f"  QUOTE: {s.strStockNo} {s.strStockName} O={s.nOpen} H={s.nHigh} L={s.nLow} C={s.nClose} V={s.nTQty}")
    except Exception as e:
        print(f"  QUOTE: {stock_no} error={e}")

def on_best5(market_no, stockno, bids, bid_qtys, asks, ask_qtys, *rest):
    sno = stockno.decode("ansi") if isinstance(stockno, bytes) else stockno
    print(f"  BEST5: {sno} Bid={bids[0]}x{bid_qtys[0]} Ask={asks[0]}x{ask_qtys[0]}")

SK.OnConnection(on_connection)
SK.OnReplyMessage(on_reply)
SK.OnComplete(on_complete)
SK.OnNotifyTicksLONG(on_tick)
SK.OnNotifyQuoteLONG(on_quote)
SK.OnNotifyBest5LONG(on_best5)
SK.OnProxyOrder(lambda sid, code, msg: print(f"  >> CALLBACK OnProxyOrder: stamp={sid} code={code} msg={msg}"))
print("  Done.\n")

# Step 1: Login
print(f"[1] Login (authority_flag=0, production)...")
result = SK.Login(user_id, password, 0)
print(f"  Code: {result.Code} -> {SK.GetMessage(result.Code)}")
if result.Code != 0:
    print(f"  FAILED. Aborting.")
    sys.exit(1)
print(f"  TF accounts: {[a.FullAccount for a in result.TFAccounts]}")
print(f"  TS accounts: {[a.FullAccount for a in result.TSAccounts]}")
print()

# Step 2: Connect services
print("[2] Connecting reply service (type=0)...")
code = SK.ManageServerConnection(user_id, 0, 0)
print(f"  Code: {code} -> {SK.GetMessage(code)}")
time.sleep(2)

print("[3] Connecting quote service (type=1)...")
code = SK.ManageServerConnection(user_id, 0, 1)
print(f"  Code: {code} -> {SK.GetMessage(code)}")
time.sleep(2)

print("[4] Connecting proxy order service (type=4)...")
code = SK.ManageServerConnection(user_id, 0, 4)
print(f"  Code: {code} -> {SK.GetMessage(code)}")
time.sleep(2)

# Step 3: Load commodity
print("[5] Loading commodity data (market_no=2)...")
code = SK.LoadCommodity(2)
print(f"  Code: {code} -> {SK.GetMessage(code)}")
time.sleep(2)
print()

# Step 4: Subscribe
symbol = "TXFD0"
print(f"[6] Subscribing to {symbol}...")
code = SK.SKQuoteLib_RequestStocks(symbol)
print(f"  RequestStocks: code={code} -> {SK.GetMessage(code)}")
code = SK.SKQuoteLib_RequestTicks(0, symbol)
print(f"  RequestTicks: code={code} -> {SK.GetMessage(code)}")
print()

# Step 5: Listen
print(f"=== Listening for 30 seconds (Ctrl+C to stop) ===")
print(f"=== Market hours: regular 08:45-13:45, after-hours 15:00-05:00 TST ===")
print()

try:
    start = time.time()
    while time.time() - start < 30:
        time.sleep(0.5)
except KeyboardInterrupt:
    print("\nInterrupted.")

print(f"\n=== Done. Received {tick_count} ticks. ===")
